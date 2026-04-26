variable "cluster_name" {
  type = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "oidc_issuer" {
  type = string
}

variable "namespace" {
  type    = string
  default = "ticketing"
}

variable "service_account" {
  type    = string
  default = "ai-advisor-sa"
}

variable "sqs_queue_arns" {
  type    = list(string)
  default = ["*"]
}
