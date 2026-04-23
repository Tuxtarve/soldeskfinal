variable "env" { type = string }
variable "aws_region" { type = string }
variable "subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "cluster_name" {
  type        = string
  description = "EKS cluster resource name"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID for destroy-time cleanup of LB/ENI/EIP"
}

variable "sqs_queue_arns" {
  type        = list(string)
  description = "SQS queue ARNs for IRSA"
  default     = []
}

variable "assets_bucket_arn" {
  type        = string
  description = "S3 assets bucket ARN — db-backup CronJob 가 mysqldump 결과를 backups/ prefix 에 PutObject 한다."
  default     = ""
}

# count 가 다른 리소스 attribute(unknown at plan)에 걸리면 "Invalid count argument" 에러.
# destroy/refresh 에서도 안전하도록 plan-time known bool 로 분리.
variable "enable_db_backup_to_assets" {
  type        = bool
  default     = false
  description = "db-backup IRSA policy 생성 여부. assets_bucket_arn 이 plan 시점 unknown 이 될 수 있어 count 는 이 bool 로 분기."
}

variable "app_node_instance_types" {
  type        = list(string)
  description = "워커 노드 인스턴스 타입(평시 1대·max 확장 시 수평 증설)."
}

variable "app_node_desired_size" {
  type        = number
  description = "평시 desired 노드 수."
}

variable "app_node_min_size" {
  type        = number
  description = "최소 노드(비용 바닥)."
}

variable "app_node_max_size" {
  type        = number
  description = "피크 시 상한. Cluster Autoscaler + HPA(read-api 등)와 함께 쓸 것."
}
