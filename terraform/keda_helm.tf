# KEDA operator — terraform apply 시 Helm 으로 설치 (apply 호스트에 helm CLI 불필요).
# ScaledObject 등 CR 은 post_apply_k8s_bootstrap.sh 가 kubectl 로 적용(paused 기본).
#
# hashicorp/helm v3+: kubernetes 블록이 아니라 kubernetes = { ... } 객체 형식.

provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_ca)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
    }
  }
}

# KEDA 는 내장 system-cluster-critical — 앱(ticketing-priority-*) 보다 항상 위
resource "null_resource" "apply_ticketing_priority_classes" {
  triggers = {
    priority_md5 = filemd5(abspath("${path.root}/../k8s/priorityclass-ticketing.yaml"))
    cluster_name = module.eks.cluster_name
  }

  depends_on = [
    module.eks,
    data.external.terraform_host_exec_clis,
  ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    environment = {
      CLUSTER_NAME = module.eks.cluster_name
      AWS_REGION   = var.aws_region
      PC_FILE      = abspath("${path.root}/../k8s/priorityclass-ticketing.yaml")
      # AWS CLI v2 기본 pager 비활성화 — TTY 환경(Git Bash)에서 "(END)" 로 멈춤 방지.
      AWS_PAGER = ""
    }
    command = <<-EOT
set -euo pipefail
_K="$(mktemp)"
trap 'rm -f "$_K"' EXIT
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" --kubeconfig "$_K"
export KUBECONFIG="$_K"
kubectl apply -f "$PC_FILE"
EOT
  }
}

resource "helm_release" "keda" {
  count = var.install_keda ? 1 : 0

  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda"
  create_namespace = true
  version          = "2.15.2"

  wait            = true
  timeout         = 180
  atomic          = true
  cleanup_on_fail = true

  values = [
    yamlencode({
      priorityClassName = "system-cluster-critical"
      serviceAccount = {
        create      = true
        name        = "keda-operator"
        annotations = { "eks.amazonaws.com/role-arn" = module.eks.keda_operator_role_arn }
      }
    })
  ]

  # KEDA 설치 중 생성되는 Service 등이 ALB Controller webhook을 호출할 수 있어,
  # 컨트롤러(webhook endpoints)가 준비되기 전에 실행되면 실패할 수 있다.
  depends_on = [
    module.eks,
    null_resource.install_aws_load_balancer_controller,
    null_resource.apply_ticketing_priority_classes,
  ]
}

resource "null_resource" "keda_cleanup_on_destroy" {
  count = var.install_keda ? 1 : 0

  triggers = {
    cluster_name = module.eks.cluster_name
    aws_region   = var.aws_region
    # 스크립트 변경 시 재실행
    script_md5 = filemd5("${path.module}/scripts/keda_cleanup_on_destroy.sh")
  }

  depends_on = [
    data.external.terraform_host_exec_clis,
    module.eks,
    null_resource.install_aws_load_balancer_controller,
    helm_release.keda,
  ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    when        = destroy
    environment = {
      CLUSTER_NAME      = self.triggers.cluster_name
      AWS_REGION        = self.triggers.aws_region
      KEDA_NAMESPACE    = "keda"
      KEDA_RELEASE_NAME = "keda"
      # AWS CLI v2 기본 pager 비활성화 — destroy 중 멈춤 방지.
      AWS_PAGER = ""
      # destroy가 길어지지 않게 빠르게 정리(필요 시 finalizer 조기 제거)
      KEDA_CLEANUP_WAIT_SEC = "120"
      # 1이면 namespace finalizers 강제 제거(최후 수단). 기본 0.
      KEDA_FORCE_REMOVE_FINALIZERS = "1"
      # Terminating이 지속되면 N초 뒤 finalizer 제거 시도
      KEDA_FORCE_FINALIZERS_AFTER_SEC = "20"
    }
    command = "tr -d '\\r' < \"${path.module}/scripts/keda_cleanup_on_destroy.sh\" | bash"
  }
}
