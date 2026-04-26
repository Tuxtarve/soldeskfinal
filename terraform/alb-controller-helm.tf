resource "null_resource" "install_aws_load_balancer_controller" {
  triggers = {
    cluster_name = module.eks.cluster_name
    region       = var.aws_region
    vpc_id       = module.network.vpc_id
    role_arn     = module.eks.alb_controller_role_arn
  }

  depends_on = [
    data.external.terraform_host_exec_clis,
    module.eks,
  ]

  provisioner "local-exec" {
    # -l 은 ~/.bashrc 등을 실행해 빈 명령/깨진 alias 줄이 있으면 "bash: : 명령을 찾을 수 없습니다"만 반복될 수 있음.
    interpreter = ["bash", "-c"]
    environment = {
      CLUSTER_NAME = self.triggers.cluster_name
      AWS_REGION   = self.triggers.region
      VPC_ID       = self.triggers.vpc_id
      ROLE_ARN     = self.triggers.role_arn
      # AWS CLI v2 기본 pager 비활성화 — TTY 환경(Git Bash)에서 "(END)" 로 멈춤 방지.
      AWS_PAGER = ""
    }

    # HGFS/Windows line endings can introduce CRLF; strip CRs at runtime.
    command = "tr -d '\\r' < \"${path.module}/scripts/install_aws_load_balancer_controller.sh\" | bash"
  }
}

