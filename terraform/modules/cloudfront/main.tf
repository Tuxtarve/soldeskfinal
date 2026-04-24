# CloudFront Origin Access Control (S3 접근 제어)
resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "ticketing-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  web_acl_id          = var.waf_acl_arn
  price_class         = "PriceClass_200"
  wait_for_deployment = true

  # destroy 전에 distribution을 비활성화하고 전파 완료를 기다림.
  # 이렇게 해야 OAC 삭제 시 "OriginAccessControlInUse" 에러가 발생하지 않음.
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-c"]
    environment = {
      # AWS CLI v2 기본 pager 비활성화 — destroy 중 멈춤 방지.
      AWS_PAGER = ""
    }
    command = <<-EOT
      set +e
      echo "=== CloudFront 삭제 준비: distribution 비활성화 ==="
      DIST_ID="${self.id}"

      ETAG=$(aws cloudfront get-distribution-config --id "$DIST_ID" \
        --query 'ETag' --output text 2>&1)
      if [ $? -ne 0 ]; then
        echo "CloudFront distribution을 찾을 수 없음 (이미 삭제됨). 스킵합니다."
        exit 0
      fi

      if [ -n "$ETAG" ] && [ "$ETAG" != "None" ]; then
        CF_TMP=$(mktemp)
        trap 'rm -f "$CF_TMP"' EXIT

        aws cloudfront get-distribution-config --id "$DIST_ID" \
          --query 'DistributionConfig' > "$CF_TMP"

        python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    cfg = json.load(f)
cfg['Enabled'] = False
with open(sys.argv[1], 'w') as f:
    json.dump(cfg, f)
" "$CF_TMP"

        # Windows Git Bash 에서 mktemp 는 MSYS 경로(/c/Users/...)를 반환하는데
        # aws CLI 의 file:// 로더는 이를 인식하지 못해 "No such file" 로 실패.
        # cygpath -m 으로 Windows native 경로(C:/Users/...)로 변환. Linux/macOS
        # 에는 cygpath 가 없으므로 그대로 $CF_TMP 사용.
        CF_TMP_PATH="$CF_TMP"
        if command -v cygpath >/dev/null 2>&1; then
          CF_TMP_PATH=$(cygpath -m "$CF_TMP")
        fi

        aws cloudfront update-distribution \
          --id "$DIST_ID" \
          --distribution-config "file://$CF_TMP_PATH" \
          --if-match "$ETAG" > /dev/null || true

        echo "Distribution 비활성화 요청 완료. 전파 대기 중..."
        aws cloudfront wait distribution-deployed --id "$DIST_ID" || true

        echo "=== 전파 완료. Terraform이 삭제를 진행합니다 ==="
      fi
    EOT
  }

  # Origin 1: S3 (정적 프론트엔드)
  origin {
    domain_name              = var.frontend_domain
    origin_id                = "S3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  # Origin 2: API Gateway (HTTP API) — endpoint가 비어있으면 생략
  # API GW는 항상 HTTPS만 받음 (자체 *.execute-api 인증서)
  # CloudFront → API GW → VPC Link → Internal ALB → EKS 흐름
  dynamic "origin" {
    for_each = var.api_gateway_endpoint_host != "" ? [var.api_gateway_endpoint_host] : []
    content {
      domain_name = origin.value
      origin_id   = "APIGW-api"
      custom_origin_config {
        http_port              = 80
        https_port             = 443
        origin_protocol_policy = "https-only"
        origin_ssl_protocols   = ["TLSv1.2"]
      }
    }
  }

  # 기본 캐시 (SPA)
  default_cache_behavior {
    target_origin_id       = "S3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
  }

  # API 경로 캐시 (캐싱 없음) — API GW origin이 있을 때만 생성
  # x-user-email은 클라이언트가 보내도 API GW가 검증된 값으로 덮어씀
  # Authorization 헤더를 forward 해야 API GW JWT Authorizer가 검증 가능
  dynamic "ordered_cache_behavior" {
    for_each = var.api_gateway_endpoint_host != "" ? [1] : []
    content {
      path_pattern           = "/api/*"
      target_origin_id       = "APIGW-api"
      viewer_protocol_policy = "redirect-to-https"
      allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods         = ["GET", "HEAD"]
      compress               = true

      forwarded_values {
        query_string = true
        # Host는 API GW가 자체 도메인을 기대하므로 forward 하면 안 됨
        headers = ["Authorization", "Content-Type", "CloudFront-Forwarded-Proto"]
        cookies { forward = "none" }
      }

      min_ttl     = 0
      default_ttl = 0
      max_ttl     = 0
    }
  }

  # SPA 라우팅 (404 → index.html). 사용자가 /concert 같은 URL 직접 치면
  # S3 에 해당 object 가 없어 404 → SPA 엔트리로 fallback.
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  # 403 은 rewrite 하지 않음. WAF 차단(예: 의심 UA) 시 403 이 발생하는데
  # 이걸 /index.html 로 200 rewrite 하면:
  #   1) /js/*.js 요청이 WAF 에 막히면 응답이 HTML(index.html) → 브라우저에서
  #      "Unexpected token '<'" SyntaxError 로 페이지 전체가 깨짐
  #   2) CF 엣지가 그 잘못된 응답을 캐싱하면 정상 UA 도 캐시된 HTML 을 받음
  # 따라서 403 은 그대로 403 을 돌려보내 원인을 명확히 드러내는 편이 안전.
  # (S3 object 부재로 인한 fallback 은 404 rewrite 로 충분)

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "ticketing-cloudfront", Environment = var.env }
}

# S3 버킷 정책: CloudFront만 접근 허용
resource "aws_s3_bucket_policy" "frontend" {
  bucket = var.frontend_bucket_id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${var.frontend_bucket_arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.main.arn
        }
      }
    }]
  })
}
