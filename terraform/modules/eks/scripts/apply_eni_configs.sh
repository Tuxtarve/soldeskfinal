#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# apply_eni_configs.sh
#
# EKS VPC Custom Networking 용 ENIConfig(CRD) 를 AZ 별로 1개씩 생성.
#
# 호출 위치:
#   terraform/modules/eks/main.tf 의 null_resource.pod_eni_configs
#
# 환경 변수 (모두 필수):
#   CLUSTER_NAME     : EKS cluster 이름
#   AWS_REGION       : 리전
#   POD_SUBNET_IDS   : 콤마 구분 pod subnet ID 목록 (예: subnet-aaa,subnet-bbb)
#   POD_SUBNET_AZS   : 콤마 구분 AZ 풀네임 목록  (예: ap-northeast-2a,ap-northeast-2b)
#                      → POD_SUBNET_IDS 와 인덱스 쌍이 일치해야 함
#   POD_SG_IDS       : 콤마 구분 SG ID 목록 (파드 ENI 에 붙일 SG)
#
# 동작:
#   1) 호스트 ~/.kube/config 오염 없이 임시 KUBECONFIG 로 aws eks update-kubeconfig
#   2) AZ 수만큼 `ENIConfig` 매니페스트를 heredoc 으로 만들어 kubectl apply
#   3) apply 결과는 declarative → 재실행 시에도 안전(upsert)
#
# 실패 시나리오 방어:
#   - vpc-cni 애드온이 ENIConfig CRD 를 등록하기 전이면 apply 가 "no matches for kind"
#     로 실패. terraform dep 순서(aws_eks_addon.vpc_cni → this) 로 이를 보장.
#   - kubectl 이 인증 실패(API aggregator 기동 전 등) 하면 exit != 0 → terraform 이
#     error 로 잡아줌. 재실행은 triggers 변경 또는 taint.
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

: "${CLUSTER_NAME:?CLUSTER_NAME is required}"
: "${AWS_REGION:?AWS_REGION is required}"
: "${POD_SUBNET_IDS:?POD_SUBNET_IDS is required}"
: "${POD_SUBNET_AZS:?POD_SUBNET_AZS is required}"
: "${POD_SG_IDS:?POD_SG_IDS is required}"

# 임시 kubeconfig 로 작업 — 호스트 ~/.kube/config 를 건드리지 않는다.
# (병렬 apply 에서 다른 null_resource 와 kubeconfig 경합이 나지 않도록)
KUBECONFIG_TMP="$(mktemp)"
trap 'rm -f "$KUBECONFIG_TMP"' EXIT

KUBECONFIG="$KUBECONFIG_TMP" aws eks update-kubeconfig \
  --name "$CLUSTER_NAME" \
  --region "$AWS_REGION" >/dev/null

IFS=',' read -ra SUBNETS <<< "$POD_SUBNET_IDS"
IFS=',' read -ra AZS     <<< "$POD_SUBNET_AZS"
IFS=',' read -ra SGS     <<< "$POD_SG_IDS"

if [ "${#SUBNETS[@]}" -ne "${#AZS[@]}" ]; then
  echo "[apply_eni_configs] ERROR: POD_SUBNET_IDS(${#SUBNETS[@]}) 와 POD_SUBNET_AZS(${#AZS[@]}) 개수 불일치" >&2
  exit 2
fi

# SG 리스트를 YAML 배열 라인으로 렌더링 ("    - sg-xxxx\n" * N)
SG_YAML=""
for sg in "${SGS[@]}"; do
  SG_YAML+="    - ${sg}"$'\n'
done

for i in "${!SUBNETS[@]}"; do
  AZ="${AZS[$i]}"
  SUBNET="${SUBNETS[$i]}"
  echo "[apply_eni_configs] ENIConfig name=${AZ} subnet=${SUBNET}"
  KUBECONFIG="$KUBECONFIG_TMP" kubectl apply -f - <<EOF
apiVersion: crd.k8s.amazonaws.com/v1alpha1
kind: ENIConfig
metadata:
  name: ${AZ}
spec:
  subnet: ${SUBNET}
  securityGroups:
${SG_YAML}
EOF
done

echo "[apply_eni_configs] Done. Applied ${#SUBNETS[@]} ENIConfig(s): ${AZS[*]}"
