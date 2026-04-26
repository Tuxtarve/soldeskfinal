#!/usr/bin/env bash
# terraform apply 마지막에 null_resource 가 호출 — 두 번째 terraform apply 없이
# 시크릿 적용 → 매니페스트 → (옵션) S3 api-origin.js 동기화 → 롤아웃까지 한 번에 수행.
set -euo pipefail

# tr … | bash 으로 실행될 때 BASH_SOURCE 가 비므로 TF 가 REPO_ROOT 를 넘김
: "${REPO_ROOT:?}"
TF_DIR="$REPO_ROOT/terraform"

: "${EKS_CLUSTER_NAME:?}"
: "${AWS_REGION:?}"
: "${DB_PASSWORD:?}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl not found. Install kubectl before terraform apply (same host that runs local-exec)." >&2
  exit 127
fi

NS="${TICKETING_NAMESPACE:-ticketing}"
CM="${TICKETING_CONFIGMAP_NAME:-ticketing-config}"
WORKER="${WORKER_DEPLOYMENT_NAME:-worker-svc}"
READ_API="${READ_API_DEPLOYMENT_NAME:-read-api}"
WRITE_API="${WRITE_API_DEPLOYMENT_NAME:-write-api}"
INGRESS_NAME="${K8S_INGRESS_NAME:-ticketing-ingress}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
ECR_REPO_TICKETING_WAS="${ECR_REPO_TICKETING_WAS:-ticketing/ticketing-was}"
ECR_REPO_WORKER_SVC="${ECR_REPO_WORKER_SVC:-ticketing/worker-svc}"
DB_SCHEMA_NAME="${DB_SCHEMA_NAME:-ticketing}"

normalize_crlf() {
  local f
  for f in "$REPO_ROOT"/k8s/scripts/*.sh "$REPO_ROOT"/terraform/scripts/*.sh "$REPO_ROOT"/scripts/*.sh; do
    [ -f "$f" ] || continue
    sed -i 's/\r$//' "$f" 2>/dev/null || true
  done
  while IFS= read -r -d '' f; do
    sed -i 's/\r$//' "$f" 2>/dev/null || true
  done < <(find "$REPO_ROOT/terraform/modules" -type f -name '*.sh' -print0 2>/dev/null || true)
}

if [[ -f "$REPO_ROOT/scripts/normalize-line-endings.sh" ]]; then
  bash "$REPO_ROOT/scripts/normalize-line-endings.sh" >/dev/null 2>&1 || true
else
  normalize_crlf
fi

echo "=== post_apply_k8s_bootstrap: kubeconfig ($EKS_CLUSTER_NAME) ==="
unset KUBECONFIG 2>/dev/null || true
_TMP_KUBECONFIG="$(mktemp)"
export KUBECONFIG="$_TMP_KUBECONFIG"
trap 'rm -f "$_TMP_KUBECONFIG"' EXIT
aws eks update-kubeconfig --name "$EKS_CLUSTER_NAME" --region "$AWS_REGION" --kubeconfig "$_TMP_KUBECONFIG"

echo "=== wait for metrics-server (HPA uses metrics.k8s.io) ==="
for _ in $(seq 1 90); do
  if kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1; then
    _st="$(kubectl get apiservice v1beta1.metrics.k8s.io -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || true)"
    if [[ "$_st" == "True" ]]; then
      break
    fi
  fi
  sleep 2
done

# EKS 관리 metrics-server 애드온 CreateAddon configuration_values 에는 Helm 의 replicaCount 가 없음(스키마 거부).
# 원하는 레플리카는 여기서만 반영 (METRICS_SERVER_REPLICAS 는 k8s_bootstrap.tf local-exec env).
MS_REPLICAS="${METRICS_SERVER_REPLICAS:-1}"
echo "=== metrics-server scale (replicas=$MS_REPLICAS) ==="
for _ in $(seq 1 90); do
  if kubectl get deploy metrics-server -n kube-system >/dev/null 2>&1; then
    kubectl -n kube-system scale deploy metrics-server --replicas="$MS_REPLICAS" || true
    break
  fi
  sleep 2
done
kubectl wait --for=condition=available deployment/metrics-server -n kube-system --timeout=180s 2>/dev/null || true

if ! command -v helm >/dev/null 2>&1; then
  echo "ERROR: helm 이 필요합니다 (Cluster Autoscaler Helm). apply 호스트에 helm 을 설치하거나 verify_terraform_host_cli 가 설치되도록 Linux/macOS 에서 apply 하세요." >&2
  exit 1
fi
echo "=== cluster-autoscaler (Helm, 노드 ASG — Pending 파드 시 스케일아웃) ==="
bash "$REPO_ROOT/scripts/install-cluster-autoscaler.sh"

# parent apply 가 state lock 중이면 nested `terraform output` 이 Windows 에서 실패.
# 이 정보는 부가 로그용이라 스킵해도 무방. 직접 실행 시에는 원래대로 출력.
if [ -z "${AWS_ACCOUNT_ID:-}" ] && command -v terraform >/dev/null 2>&1; then
  echo "=== eks_node_group_scaling_summary (max > desired 여야 CA scale-up 가능) ==="
  terraform -chdir="$TF_DIR" output eks_node_group_scaling_summary 2>/dev/null || true
fi

export DB_PASSWORD
# apply-secrets 이 Secret 을 ns=ticketing 에 만들므로 네임스페이스 선생성 필수.
# (kustomize 의 namespace.yaml 은 "kubectl apply -k" 에서 만들어지므로 더 뒤에 생성됨.)
echo "=== ensure namespace $NS ==="
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null

echo "=== apply-secrets-from-terraform ==="
NAMESPACE="$NS" bash "$REPO_ROOT/k8s/scripts/apply-secrets-from-terraform.sh"

# sqs-access-sa 는 ArgoCD/Kustomize 관리 밖(k8s/_runtime/)으로 분리.
# 배포자마다 AWS 계정 ID 가 달라 IRSA role arn 도 다르므로 git 단일진실원으로
# 두면 selfHeal 이 live annotation 을 덮어 IRSA 가 영구 파손된다(과거 이슈).
# 여기서 parent apply 가 주입한 AWS_ACCOUNT_ID + SQS_ACCESS_ROLE_ARN 으로 직접 적용.
echo "=== apply sqs-access-sa (IRSA annotation 주입) ==="
_sa_role_arn="${SQS_ACCESS_ROLE_ARN:-}"
if [ -z "$_sa_role_arn" ] && [ -n "${AWS_ACCOUNT_ID:-}" ]; then
  _sa_role_arn="arn:aws:eks:${AWS_REGION:-ap-northeast-2}:${AWS_ACCOUNT_ID}:role/ticketing-eks-sqs-access-role"
fi
if [ -z "$_sa_role_arn" ]; then
  echo "ERROR: SQS_ACCESS_ROLE_ARN 미주입 — IRSA SA 생성 불가" >&2
  exit 1
fi
kubectl -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: sqs-access-sa
  namespace: $NS
  annotations:
    eks.amazonaws.com/role-arn: "$_sa_role_arn"
EOF

# db-backup-sa: hourly mysqldump → S3 assets bucket. role arn 도 배포자별로 다르므로 동일 패턴.
echo "=== apply db-backup-sa (IRSA annotation 주입) ==="
_db_backup_role_arn="${DB_BACKUP_ROLE_ARN:-}"
if [ -z "$_db_backup_role_arn" ] && [ -n "${AWS_ACCOUNT_ID:-}" ]; then
  _db_backup_role_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:role/ticketing-eks-db-backup-role"
fi
if [ -n "$_db_backup_role_arn" ]; then
  kubectl -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: db-backup-sa
  namespace: $NS
  annotations:
    eks.amazonaws.com/role-arn: "$_db_backup_role_arn"
EOF
else
  echo "WARN: DB_BACKUP_ROLE_ARN 미주입 — db-backup CronJob 의 S3 PutObject 가 실패할 것" >&2
fi

echo "=== kubectl apply -k (rendered) ==="

# parent apply 가 주입한 env 우선 (Windows state lock 회피). 없으면 terraform output fallback.
ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(terraform -chdir="$TF_DIR" output -raw aws_account_id 2>/dev/null)}"
REGION="${AWS_REGION:-$(terraform -chdir="$TF_DIR" output -raw aws_region 2>/dev/null)}"
SQS_ROLE_ARN="${SQS_ACCESS_ROLE_ARN:-$(terraform -chdir="$TF_DIR" output -raw sqs_access_role_arn 2>/dev/null)}"
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
WAS_IMAGE="${ECR_REGISTRY}/${ECR_REPO_TICKETING_WAS}:${IMAGE_TAG}"
WORKER_IMAGE="${ECR_REGISTRY}/${ECR_REPO_WORKER_SVC}:${IMAGE_TAG}"

tmp_k8s="$(mktemp -d)"
cp -R "$REPO_ROOT/k8s" "$tmp_k8s/k8s"

# 루트 kustomization: ticketing + kube-system(metrics-server PDB). -n 을 붙이면 멀티 네임스페이스 매니페스트와 충돌할 수 있음.
# PDB 는 spec 에 minAvailable XOR maxUnavailable 만 허용. 예전 매니페스트/병합으로 둘 다 남으면 apply 가 실패하므로 선삭제.
kubectl delete pdb metrics-server -n kube-system --ignore-not-found >/dev/null 2>&1 || true
kubectl apply -k "$tmp_k8s/k8s"

# EKS 애드온 Deployment 는 kustomize 리소스에 넣지 않음(전체 덮어쓰기 위험). PDB 만 apply-k 로 관리.
echo "=== metrics-server priorityClass (addon Deployment patch) ==="
if kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1; then
  kubectl -n kube-system patch deployment metrics-server --type=strategic -p \
    '{"spec":{"template":{"spec":{"priorityClassName":"system-cluster-critical"}}}}' 2>/dev/null || \
    echo "WARN: metrics-server priorityClass patch failed (addon may reject)" >&2
  kubectl -n kube-system rollout status deployment/metrics-server --timeout=120s 2>/dev/null || true
fi

# kustomize 바이너리 없이도 동일하게 이미지 태그/레지스트리를 반영.
# (kustomization.yaml 의 images/newTag 을 편집하는 대신, 실제 Deployment에 이미지 주입)
kubectl -n "$NS" set image deploy/"$READ_API" "read-api=${WAS_IMAGE}" >/dev/null
kubectl -n "$NS" set image deploy/"${READ_API}-burst" "read-api=${WAS_IMAGE}" >/dev/null 2>&1 || true
kubectl -n "$NS" set image deploy/"$WRITE_API" "write-api=${WAS_IMAGE}" >/dev/null
kubectl -n "$NS" set image deploy/"${WRITE_API}-burst" "write-api=${WAS_IMAGE}" >/dev/null 2>&1 || true
kubectl -n "$NS" set image deploy/"$WORKER" "worker-svc=${WORKER_IMAGE}" >/dev/null
kubectl -n "$NS" set image deploy/"${WORKER}-burst" "worker-svc=${WORKER_IMAGE}" >/dev/null 2>&1 || true

kubectl -n "$NS" annotate sa sqs-access-sa "eks.amazonaws.com/role-arn=${SQS_ROLE_ARN}" --overwrite >/dev/null 2>&1 || true
if [ -n "${_db_backup_role_arn:-}" ]; then
  kubectl -n "$NS" annotate sa db-backup-sa "eks.amazonaws.com/role-arn=${_db_backup_role_arn}" --overwrite >/dev/null 2>&1 || true
fi

# CronJob 의 S3 버킷 이름을 env 로 patch — 같은 manifest 가 어떤 AWS 계정에서도 동작하도록.
if [ -n "${DB_BACKUP_S3_BUCKET:-}" ] && kubectl -n "$NS" get cronjob db-backup >/dev/null 2>&1; then
  kubectl -n "$NS" set env cronjob/db-backup S3_BUCKET="$DB_BACKUP_S3_BUCKET" >/dev/null 2>&1 || true
fi

# KEDA operator 는 terraform helm_release 로 설치됨. 여기서는 CRD 준비 후 k8s/keda 만 적용(paused ScaledObject). INSTALL_KEDA=0 이면 생략.
if [[ "${INSTALL_KEDA:-1}" != "0" ]]; then
  echo "=== KEDA CRD 대기 (terraform helm_release 이후) ==="
  for _ in $(seq 1 72); do
    if kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  kubectl wait --for=condition=established "crd/scaledobjects.keda.sh" --timeout=120s 2>/dev/null || true
  echo "=== kubectl apply -k k8s/keda (TriggerAuthentication 등) ==="
  kubectl apply -k "$tmp_k8s/k8s/keda"
fi
echo "=== KEDA ScaledObject 유지 (paused 기본, 필요 시 scripts/worker-autoscale-on.sh 로 unpause) ==="

if [[ "${SYNC_S3_ENDPOINTS:-0}" == "1" ]]; then
  echo "=== sync S3 api-origin.js from Ingress (same apply, no second terraform) ==="
  TICKETING_NAMESPACE="$NS" INGRESS_NAME="$INGRESS_NAME" bash "$REPO_ROOT/k8s/scripts/sync-s3-endpoints-from-ingress.sh"
fi

echo "=== patch configmap + rollouts ==="
kubectl -n "$NS" patch cm "$CM" --type merge -p "{\"data\":{\"DB_NAME\":\"${DB_SCHEMA_NAME}\"}}" || true
kubectl -n "$NS" patch cm "$CM" --type merge -p "{\"data\":{\"AWS_REGION\":\"$AWS_REGION\"}}" || true
kubectl -n "$NS" rollout restart deploy/"$WORKER" || true
kubectl -n "$NS" rollout restart deploy/"${WORKER}-burst" || true
kubectl -n "$NS" rollout restart deploy/"$READ_API" || true
kubectl -n "$NS" rollout restart deploy/"${READ_API}-burst" || true
kubectl -n "$NS" rollout restart deploy/"$WRITE_API" || true
kubectl -n "$NS" rollout restart deploy/"${WRITE_API}-burst" || true

echo "=== post_apply_k8s_bootstrap done ==="
