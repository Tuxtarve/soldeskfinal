# EKS CronJob(ai-advisor) 용 IRSA 역할.
# CloudWatch(읽기) + SQS 속성 조회 권한만 부여.
# GCP 인증은 Workload Identity Federation — AWS 키를 GCP에 노출하지 않는다.

resource "aws_iam_role" "ai_advisor" {
  name = "${var.cluster_name}-ai-advisor"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.oidc_issuer}:aud" = "sts.amazonaws.com"
          "${var.oidc_issuer}:sub" = "system:serviceaccount:${var.namespace}:${var.service_account}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ai_advisor" {
  name = "${var.cluster_name}-ai-advisor-policy"
  role = aws_iam_role.ai_advisor.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchRead"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics",
          "cloudwatch:GetMetricData",
        ]
        Resource = "*"
      },
      {
        Sid    = "SQSRead"
        Effect = "Allow"
        Action = [
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
        ]
        Resource = var.sqs_queue_arns
      },
    ]
  })
}
