output "cluster_name" { value = aws_eks_cluster.main.name }
output "cluster_endpoint" { value = aws_eks_cluster.main.endpoint }
output "cluster_ca" { value = aws_eks_cluster.main.certificate_authority[0].data }
output "alb_controller_role_arn" { value = aws_iam_role.alb_controller.arn }
output "node_role_arn" { value = aws_iam_role.eks_node.arn }
output "node_role_name" { value = aws_iam_role.eks_node.name }
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.eks.arn }
output "cluster_autoscaler_role_arn" { value = aws_iam_role.cluster_autoscaler.arn }
output "cluster_security_group_id" { value = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id }
output "sqs_access_role_arn" { value = aws_iam_role.sqs_access.arn }
output "keda_operator_role_arn" { value = aws_iam_role.keda_operator.arn }
output "db_backup_role_arn" { value = aws_iam_role.db_backup.arn }

output "app_node_group_name" {
  value       = aws_eks_node_group.app.node_group_name
  description = "Managed node group name for app nodes (read/write, etc)."
}


