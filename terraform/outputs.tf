output "rds_writer_endpoint" {
  value     = module.rds.writer_endpoint
  sensitive = true
}

output "rds_reader_endpoint" {
  value     = module.rds.reader_endpoint
  sensitive = true
}

output "redis_endpoint" {
  value     = module.elasticache.redis_endpoint
  sensitive = true
}

output "elasticache_primary_endpoint" {
  value     = module.elasticache.elasticache_primary_endpoint
  sensitive = true
}

# 부하 테스트·스크립트 기본 큐 (ticketing-reservation.fifo)
output "sqs_queue_url" {
  value = module.sqs.reservation_queue_url
}

output "eks_cluster_name" {
  value = module.eks.cluster_name
}

output "eks_app_node_group_name" {
  value       = module.eks.app_node_group_name
  description = "App EKS managed node group (read/write nodes)."
}

output "eks_node_role_arn" {
  description = "IAM role ARN used by EKS worker nodes (for aws-auth mapRoles)."
  value       = module.eks.node_role_arn
}

output "eks_node_group_scaling_summary" {
  description = <<-EOT
    노드 그룹 min / desired / max. max = Cluster Autoscaler가 늘릴 수 있는 상한(ASG). 무제한 아님.
    Pending 지속 시: max 여유·k8s.io/cluster-autoscaler/* 태그·CA 파드 Running·CA 로그(scale-up fail) 확인.
  EOT
  value = {
    min     = var.eks_app_node_min_size
    desired = var.eks_app_node_desired_size
    max     = var.eks_app_node_max_size
  }
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "alb_controller_role_arn" {
  value = module.eks.alb_controller_role_arn
}

output "cluster_autoscaler_role_arn" {
  value = module.eks.cluster_autoscaler_role_arn
}

output "sqs_access_role_arn" {
  value = module.eks.sqs_access_role_arn
}

output "ai_advisor_role_arn" {
  description = "IRSA role ARN for EKS ai-advisor CronJob (WIF용 바인딩에 필요)."
  value       = module.ai_advisor.role_arn
}

output "ai_advisor_ecr_url" {
  description = "ai-advisor Docker 이미지 ECR URI."
  value       = aws_ecr_repository.ai_advisor.repository_url
}

output "keda_operator_role_arn" {
  description = "IRSA for KEDA operator (SQS scaler). Helm release sets keda:keda-operator SA annotation."
  value       = module.eks.keda_operator_role_arn
}

output "aws_region" {
  value = var.aws_region
}

output "aws_account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "frontend_bucket_name" {
  description = "Frontend static site bucket name (v2 module)."
  value       = var.enable_s3_hosting_v2_module ? module.s3_hosting_v2.frontend_bucket_name : null
}

output "frontend_website_url" {
  description = "Frontend static site website URL (v2 module)."
  value       = var.enable_s3_hosting_v2_module ? module.s3_hosting_v2.frontend_website_url : null
}

output "zzzzzz_url" {
  description = "Same as frontend_website_url."
  value       = var.enable_s3_hosting_v2_module ? module.s3_hosting_v2.frontend_website_url : null
}

output "frontend_cloudfront_url" {
  description = "CloudFront URL for frontend (v2 module)."
  value       = var.enable_s3_hosting_v2_module ? module.s3_hosting_v2.cloudfront_url : null
}

# sync-s3-endpoints-from-ingress.sh 등에서 모드 확인용 (CloudFront 없는 스택인지 terraform 이 한글로 알려줌).
output "frontend_routing_mode" {
  description = "none | s3_website_alb_origin_js | cloudfront_alb — 프론트·API 오리진 연결 방식."
  value = (
    !var.enable_s3_hosting_v2_module ? "none"
    : var.enable_cloudfront_for_frontend ? "cloudfront_alb"
    : "s3_website_alb_origin_js"
  )
}

output "zzzzz" {
  description = "Commands to run after apply."
  value       = <<-EOT

  .............................

  용량: terraform output eks_node_group_scaling_summary — max 는 반드시 desired 보다 커야 CA 가 노드 증설.
  ASG 태그: 노드 그룹에 k8s.io/cluster-autoscaler/enabled, k8s.io/cluster-autoscaler/<클러스터명>=owned (Terraform 반영).

  export DB_USER=root
  export DB_PASSWORD=

  bash ../scripts/normalize-line-endings.sh
  bash ../k8s/scripts/apply-secrets-from-terraform.sh
  bash ../scripts/install-cluster-autoscaler.sh
  kubectl apply -k ../k8s
  bash ../k8s/scripts/sync-s3-endpoints-from-ingress.sh
  kubectl -n ${var.ticketing_namespace} patch cm ${var.ticketing_configmap_name} --type merge -p '{"data":{"DB_NAME":"ticketing"}}'
  kubectl -n ${var.ticketing_namespace} rollout restart deploy/${var.worker_deployment_name} deploy/${var.worker_deployment_name}-burst || true
  kubectl -n ${var.ticketing_namespace} rollout restart deploy/${var.read_api_deployment_name} deploy/${var.read_api_deployment_name}-burst || true
  kubectl -n ${var.ticketing_namespace} rollout restart deploy/${var.write_api_deployment_name} deploy/${var.write_api_deployment_name}-burst || true
  .............................
  EOT
}

# ── 내 FINAL outputs 보존 (Cognito / API GW / CloudFront 모듈 참조) ─────────

output "cloudfront_domain" {
  value = module.cloudfront.cloudfront_domain
}

output "cognito_user_pool_id" {
  value = module.cognito.user_pool_id
}

output "cognito_client_id" {
  value = module.cognito.user_pool_client_id
}

output "cognito_user_pool_arn" {
  value = module.cognito.user_pool_arn
}

output "cognito_domain" {
  value = module.cognito.cognito_domain
}

output "api_gateway_endpoint" {
  description = "API Gateway HTTP API invoke URL — CloudFront origin으로 사용됨"
  value       = module.api_gateway.api_endpoint
}

output "api_gateway_endpoint_host" {
  description = "API Gateway 도메인만 (https:// 제외)"
  value       = module.api_gateway.api_endpoint_host
}
