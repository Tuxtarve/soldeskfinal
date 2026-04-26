#!/usr/bin/env bash
set -eu
if (set -o pipefail) 2>/dev/null; then
  set -o pipefail
fi

echo "Installing aws-load-balancer-controller via Helm..."

: "${CLUSTER_NAME:?CLUSTER_NAME is required}"
: "${AWS_REGION:?AWS_REGION is required}"
: "${VPC_ID:?VPC_ID is required}"
: "${ROLE_ARN:?ROLE_ARN is required}"

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: aws CLI not found. Install AWS CLI v2 and ensure it is on PATH." >&2
  exit 127
fi
if ! command -v helm >/dev/null 2>&1; then
  echo "ERROR: helm not found. Terraform apply runs this script on the machine where you execute terraform;" >&2
  echo "      install Helm 3 (https://helm.sh/docs/intro/install/) and retry." >&2
  exit 127
fi

# Parallel Terraform local-exec provisioners can run update-kubeconfig against the same
# ~/.kube/config and corrupt YAML. Use an isolated file per invocation.
unset KUBECONFIG 2>/dev/null || true
_TMP_KUBECONFIG="$(mktemp)"
export KUBECONFIG="$_TMP_KUBECONFIG"
trap 'rm -f "$_TMP_KUBECONFIG"' EXIT
aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${AWS_REGION}" --kubeconfig "$_TMP_KUBECONFIG"

helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set "clusterName=${CLUSTER_NAME}" \
  --set "region=${AWS_REGION}" \
  --set "vpcId=${VPC_ID}" \
  --set-string priorityClassName=system-cluster-critical \
  --set replicaCount=1 \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn=${ROLE_ARN}" \
  --set "resources.requests.cpu=100m" \
  --set "resources.requests.memory=200Mi" \
  --set "resources.limits.cpu=100m" \
  --set "resources.limits.memory=200Mi"

# helm_release 완료 ≠ Controller Pod/Webhook Ready.
# 후속 helm_release(예: KEDA) 가 Service 를 만들면 ALB Controller 의 mutating
# webhook "mservice.elbv2.k8s.aws" 가 가로채는데, endpoint 가 아직 올라오지 않으면
# "no endpoints available for service aws-load-balancer-webhook-service" 로
# 후속 apply 가 터진다. Deployment Available + Service endpoint 둘 다 대기해 경쟁조건 제거.
echo "waiting aws-load-balancer-controller rollout..."
kubectl -n kube-system rollout status deployment/aws-load-balancer-controller --timeout=300s
kubectl -n kube-system wait --for=condition=Available deployment/aws-load-balancer-controller --timeout=180s
for i in $(seq 1 60); do
  ip="$(kubectl -n kube-system get endpoints aws-load-balancer-webhook-service -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null || true)"
  if [ -n "$ip" ]; then
    echo "aws-load-balancer-webhook-service endpoint ready ($ip)"
    exit 0
  fi
  sleep 2
done
echo "ERROR: aws-load-balancer-webhook-service endpoint not ready in 120s" >&2
kubectl -n kube-system get pods,deploy,endpoints -l app.kubernetes.io/name=aws-load-balancer-controller >&2 || true
exit 1

