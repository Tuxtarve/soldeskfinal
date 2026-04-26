#!/usr/bin/env bash
# EKS에 Cluster Autoscaler 설치 (Helm).
# 사전: helm, kubectl, aws eks update-kubeconfig, terraform apply (IRSA 역할 생성됨)
#
# ASG 자동발견(autoDiscovery)은 노드 그룹에 붙은 태그가 필요함 — terraform/modules/eks 의
# aws_eks_node_group.tags 에 k8s.io/cluster-autoscaler/* 가 있어야 scale-up 이 동작함.
#
# Windows 공유 폴더(hgfs/vm-share)에서 CRLF가 섞이면 "set: pipefail: invalid option" 이 난다.
# 아래 블록이 한 번 파일을 LF로 고친 뒤 같은 스크립트를 다시 실행한다.

_script="${BASH_SOURCE[0]}"
if command -v grep >/dev/null 2>&1 && grep -q $'\r' "$_script" 2>/dev/null; then
  if sed --version >/dev/null 2>&1; then
    sed -i 's/\r$//' "$_script"
  else
    sed -i '' 's/\r$//' "$_script"
  fi
  exec bash "$_script" "$@"
fi

set -eu
set -o pipefail 2>/dev/null || true

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/terraform"
cd "$TF_DIR"

# 같은 apply 내부에서 호출 시 Windows strict state lock 과 충돌하므로, 부모 apply 가
# 환경변수(EKS_CLUSTER_NAME / CLUSTER_AUTOSCALER_ROLE_ARN / AWS_REGION)로 주입했으면 그걸 우선 사용.
# 스크립트를 터미널에서 직접 실행할 때는 기존대로 `terraform output` fallback.
CLUSTER_NAME="${EKS_CLUSTER_NAME:-$(terraform output -raw eks_cluster_name)}"
ROLE_ARN="${CLUSTER_AUTOSCALER_ROLE_ARN:-$(terraform output -raw cluster_autoscaler_role_arn)}"
REGION="${AWS_REGION:-$(terraform output -raw aws_region)}"

# helm --wait 가 클러스터 Pending(노드 없음/부족) 상황에서 무한 대기처럼 보이는 것을 방지한다.
# 필요 시 환경변수로 늘릴 수 있음.
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"
HELM_TIMEOUT_SEC="${HELM_TIMEOUT_SEC:-}"

_ts() { date +"%Y-%m-%d %H:%M:%S"; }

_to_seconds() {
  # "10m", "30s", "2h" 등 helm timeout 포맷을 초로 변환 (fallback: 600)
  python3 - "$1" <<'PY' 2>/dev/null || echo "600"
import re,sys
s=sys.argv[1].strip()
m=re.fullmatch(r"(\d+)([smh])", s)
if not m:
    print(600); raise SystemExit(0)
n=int(m.group(1)); u=m.group(2)
print(n if u=="s" else n*60 if u=="m" else n*3600)
PY
}

if [[ -z "${HELM_TIMEOUT_SEC:-}" ]]; then
  HELM_TIMEOUT_SEC="$(_to_seconds "$HELM_TIMEOUT")"
fi

_run_helm() {
  # helm 자체 --timeout 이 동작해도, 네트워크/helm repo update 등에서 멈추는 케이스가 있어 OS timeout으로 한 번 더 감싼다.
  if command -v timeout >/dev/null 2>&1; then
    timeout --foreground "${HELM_TIMEOUT_SEC}s" helm "$@"
  else
    helm "$@"
  fi
}

_ready_nodes_count() {
  # Ready, Ready,SchedulingDisabled 모두 카운트. kubectl 권한/연결 문제면 0으로 취급.
  kubectl get nodes --no-headers 2>/dev/null | grep -E 'Ready' | wc -l | tr -d ' ' || echo "0"
}

echo "cluster=$CLUSTER_NAME region=$REGION"

if ! command -v helm >/dev/null 2>&1; then
  echo "helm 이 필요합니다. 예: curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
  exit 1
fi

echo "[$(_ts)] helm repo add/update (timeout=$HELM_TIMEOUT, hard=${HELM_TIMEOUT_SEC}s)"
helm repo add autoscaler https://kubernetes.github.io/autoscaler 2>/dev/null || true
if ! _run_helm repo update; then
  echo "[warn] helm repo update failed or timed out" >&2
  exit 1
fi

VALUES="$(mktemp)"
trap 'rm -f "$VALUES"' EXIT
cat >"$VALUES" <<EOF
fullnameOverride: cluster-autoscaler
nameOverride: cluster-autoscaler
priorityClassName: system-cluster-critical
# QoS: requests=limits → Guaranteed. CA 가 죽으면 노드 scale-up 불가 → 연쇄 장애라 최후 생존.
resources:
  requests:
    cpu: 100m
    memory: 300Mi
  limits:
    cpu: 100m
    memory: 300Mi
image:
  repository: registry.k8s.io/autoscaling/cluster-autoscaler
  # EKS 1.30.x 에 맞춰 pin (신버전은 ResourceClaim/DeviceClass 등으로 로그가 오염되고 동작이 불안정할 수 있음)
  tag: v1.30.7
autoDiscovery:
  clusterName: ${CLUSTER_NAME}
awsRegion: ${REGION}
rbac:
  serviceAccount:
    create: true
    name: cluster-autoscaler
    annotations:
      eks.amazonaws.com/role-arn: ${ROLE_ARN}
extraArgs:
  v: 4
  balance-similar-node-groups: true
  skip-nodes-with-local-storage: false
  expander: least-waste
  # Pending(Unschedulable) 감지 주기. 더 빨리 감지해 scale-up 시작을 당김.
  scan-interval: 5s
  # 새 파드가 생성된 직후 scale-up 판단을 미루지 않음(기본보다 공격적으로).
  new-pod-scale-up-delay: 0s
  scale-down-delay-after-add: 7m
  scale-down-unneeded-time: 10m
  # 노드 준비 지연(특히 신규 ASG 인스턴스) 시 scale-up 타임아웃 완화
  max-node-provision-time: 15m
EOF

READY_NODES="$(_ready_nodes_count)"
WAIT_ARGS=()
if [[ "${READY_NODES:-0}" =~ ^[0-9]+$ ]] && (( READY_NODES > 0 )); then
  WAIT_ARGS=(--wait --timeout "$HELM_TIMEOUT")
else
  echo "[warn] Ready nodes=0. helm --wait 은 의미가 없어 skip 합니다. (노드/ASG 먼저 복구 필요)" >&2
fi

if helm list -n kube-system | grep -q cluster-autoscaler; then
  echo "이미 설치됨 → upgrade"
  echo "[$(_ts)] helm upgrade ${WAIT_ARGS[*]:-} (timeout=$HELM_TIMEOUT, hard=${HELM_TIMEOUT_SEC}s)"
  if ! _run_helm upgrade cluster-autoscaler autoscaler/cluster-autoscaler \
    -n kube-system -f "$VALUES" "${WAIT_ARGS[@]}"; then
    echo "[warn] helm upgrade failed or timed out (timeout=$HELM_TIMEOUT)" >&2
    echo "[diag] kubectl get nodes" >&2
    kubectl get nodes -o wide >&2 || true
    echo "[diag] kube-system pods (non-Running/Completed)" >&2
    kubectl get pods -n kube-system -o wide 2>/dev/null | egrep -v 'Running|Completed' >&2 || true
    echo "[diag] recent events (kube-system)" >&2
    kubectl get events -n kube-system --sort-by=.lastTimestamp 2>/dev/null | tail -n 40 >&2 || true
    exit 1
  fi
else
  echo "[$(_ts)] helm install ${WAIT_ARGS[*]:-} (timeout=$HELM_TIMEOUT, hard=${HELM_TIMEOUT_SEC}s)"
  if ! _run_helm install cluster-autoscaler autoscaler/cluster-autoscaler \
    -n kube-system -f "$VALUES" "${WAIT_ARGS[@]}"; then
    echo "[warn] helm install failed or timed out (timeout=$HELM_TIMEOUT)" >&2
    echo "[diag] kubectl get nodes" >&2
    kubectl get nodes -o wide >&2 || true
    echo "[diag] kube-system pods (non-Running/Completed)" >&2
    kubectl get pods -n kube-system -o wide 2>/dev/null | egrep -v 'Running|Completed' >&2 || true
    echo "[diag] recent events (kube-system)" >&2
    kubectl get events -n kube-system --sort-by=.lastTimestamp 2>/dev/null | tail -n 40 >&2 || true
    exit 1
  fi
fi

echo "완료: kubectl get deployment -n kube-system cluster-autoscaler"
echo "logs: kubectl -n kube-system logs deploy/cluster-autoscaler --tail=200"
