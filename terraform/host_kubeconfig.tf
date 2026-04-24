# apply 가 끝난 뒤, Terraform 을 실행하는 호스트의 ~/.kube/config 를 한 번만 정리한다.
# - 병렬 local-exec 는 임시 kubeconfig 만 쓰므로 기본 파일이 오래된 깨진 상태로 남을 수 있음
# - 손상 시 삭제 후 aws eks update-kubeconfig (sync_host_kubeconfig.sh)
# depends_on 은 정적 리스트만 허용되므로 bootstrap 유무로 리소스를 둘로 나눈다.
# CI 등에서 끄려면 sync_host_kubeconfig_after_apply = false

resource "null_resource" "host_kubeconfig_sync_after_bootstrap" {
  count = (
    var.sync_host_kubeconfig_after_apply && var.run_k8s_bootstrap_after_apply
  ) ? 1 : 0

  depends_on = [
    module.eks,
    null_resource.install_aws_load_balancer_controller,
    null_resource.k8s_bootstrap_after_apply[0],
  ]

  triggers = {
    cluster_name = module.eks.cluster_name
    aws_region   = var.aws_region
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    environment = {
      CLUSTER_NAME = self.triggers.cluster_name
      AWS_REGION   = self.triggers.aws_region
      # AWS CLI v2 기본 pager 비활성화 — TTY 환경(Git Bash)에서 "(END)" 로 멈춤 방지.
      AWS_PAGER = ""
    }
    command = "tr -d '\\r' < \"${path.module}/scripts/sync_host_kubeconfig.sh\" | bash"
  }
}

resource "null_resource" "host_kubeconfig_sync_no_bootstrap" {
  count = (
    var.sync_host_kubeconfig_after_apply && !var.run_k8s_bootstrap_after_apply
  ) ? 1 : 0

  depends_on = [
    module.eks,
    null_resource.install_aws_load_balancer_controller,
  ]

  triggers = {
    cluster_name = module.eks.cluster_name
    aws_region   = var.aws_region
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    environment = {
      CLUSTER_NAME = self.triggers.cluster_name
      AWS_REGION   = self.triggers.aws_region
      # AWS CLI v2 기본 pager 비활성화 — TTY 환경(Git Bash)에서 "(END)" 로 멈춤 방지.
      AWS_PAGER = ""
    }
    command = "tr -d '\\r' < \"${path.module}/scripts/sync_host_kubeconfig.sh\" | bash"
  }
}
