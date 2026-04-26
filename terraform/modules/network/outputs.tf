output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }

# Pod 전용 서브넷(secondary CIDR 100.64.0.0/16 내).
# EKS Custom Networking(ENIConfig) 에서만 참조되며, EKS 클러스터/노드그룹에는 넘기지 않는다.
output "pod_subnet_ids" { value = aws_subnet.pod[*].id }
output "pod_subnet_azs" { value = aws_subnet.pod[*].availability_zone }

output "eks_sg_id" { value = aws_security_group.was.id }
output "rds_sg_id" { value = aws_security_group.db.id }
output "redis_sg_id" { value = aws_security_group.cache.id }
output "monitoring_sg_id" { value = aws_security_group.web.id }
