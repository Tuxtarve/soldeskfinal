#!/usr/bin/env bash
# GCP Workload Identity Federation 세팅 스크립트.
# EKS Pod(IRSA)이 서비스 계정 키 없이 GCP Cloud Logging에 쓸 수 있게 한다.
#
# 사전 조건:
#   gcloud auth login && gcloud config set project soldesk-gcp
#   terraform output aws_account_id  # AWS 계정 ID 확인용
#
# 사용:
#   bash scripts/setup-wif.sh
#   bash scripts/setup-wif.sh --aws-account-id 032098305878
#
# 결과:
#   k8s/ai-advisor/gcp-credential-config.json  (ConfigMap 에 올릴 파일)
set -euo pipefail

# ---------- 설정 ----------
GCP_PROJECT_ID="${GCP_PROJECT_ID:-soldesk-gcp}"
POOL_ID="${WIF_POOL_ID:-eks-pool}"
PROVIDER_ID="${WIF_PROVIDER_ID:-eks-provider}"
GCP_SA_NAME="${GCP_SA_NAME:-eks-ai-advisor}"
GCP_SA_EMAIL="${GCP_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
OUT_DIR="$(dirname "$0")/../k8s/ai-advisor"

# AWS 계정 ID 자동 감지
AWS_ACCOUNT_ID="${1:-}"
if [[ "$1" == "--aws-account-id" ]]; then AWS_ACCOUNT_ID="$2"; fi
if [[ -z "$AWS_ACCOUNT_ID" ]]; then
  AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
fi
if [[ -z "$AWS_ACCOUNT_ID" ]]; then
  echo "AWS 계정 ID를 가져올 수 없습니다. --aws-account-id 옵션으로 직접 입력하세요." >&2
  exit 1
fi

echo "[*] GCP Project : $GCP_PROJECT_ID"
echo "[*] AWS Account : $AWS_ACCOUNT_ID"
echo "[*] Pool ID     : $POOL_ID"
echo "[*] Provider ID : $PROVIDER_ID"
echo "[*] GCP SA      : $GCP_SA_EMAIL"

# GCP 프로젝트 번호 조회
GCP_PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT_ID" \
  --format="value(projectNumber)")
echo "[*] Project Num : $GCP_PROJECT_NUMBER"

# ---------- 1. Workload Identity Pool ----------
echo
echo "[1] Workload Identity Pool 생성..."
if gcloud iam workload-identity-pools describe "$POOL_ID" \
    --project="$GCP_PROJECT_ID" --location="global" &>/dev/null; then
  echo "    이미 존재 — skip"
else
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --project="$GCP_PROJECT_ID" \
    --location="global" \
    --display-name="EKS AI Advisor Pool"
  echo "    생성 완료"
fi

# ---------- 2. AWS Provider ----------
echo
echo "[2] AWS Provider 생성..."
if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
    --project="$GCP_PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_ID" &>/dev/null; then
  echo "    이미 존재 — skip"
else
  gcloud iam workload-identity-pools providers create-aws "$PROVIDER_ID" \
    --project="$GCP_PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_ID" \
    --account-id="$AWS_ACCOUNT_ID" \
    --attribute-mapping="google.subject=assertion.arn,attribute.aws_role=assertion.arn.extract('assumed-role/{role}/')"
  echo "    생성 완료"
fi

# ---------- 3. GCP Service Account ----------
echo
echo "[3] GCP Service Account 생성..."
if gcloud iam service-accounts describe "$GCP_SA_EMAIL" \
    --project="$GCP_PROJECT_ID" &>/dev/null; then
  echo "    이미 존재 — skip"
else
  gcloud iam service-accounts create "$GCP_SA_NAME" \
    --project="$GCP_PROJECT_ID" \
    --display-name="EKS AI Advisor (WIF)"
  echo "    생성 완료"
fi

# ---------- 4. Cloud Logging 쓰기 권한 ----------
echo
echo "[4] Cloud Logging Writer 권한 부여..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member="serviceAccount:${GCP_SA_EMAIL}" \
  --role="roles/logging.logWriter" \
  --quiet
echo "    완료"

# ---------- 5. IRSA 역할 → GCP SA 위임 ----------
# Terraform apply 후 IRSA 역할 ARN을 알아야 바인딩 가능.
# terraform output ai_advisor_role_arn 으로 확인 후 아래를 실행하세요.
IRSA_ROLE_ARN="${IRSA_ROLE_ARN:-}"
if [[ -n "$IRSA_ROLE_ARN" ]]; then
  ROLE_NAME=$(echo "$IRSA_ROLE_ARN" | awk -F'/' '{print $NF}')
  MEMBER="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.aws_role/arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${ROLE_NAME}"
  echo
  echo "[5] IRSA 역할 → GCP SA 위임 바인딩..."
  gcloud iam service-accounts add-iam-policy-binding "$GCP_SA_EMAIL" \
    --project="$GCP_PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="$MEMBER" \
    --quiet
  echo "    완료: $MEMBER"
else
  echo
  echo "[5] IRSA_ROLE_ARN 미설정 — terraform apply 후 아래 명령을 수동으로 실행하세요:"
  echo
  echo "    IRSA_ROLE_ARN=\$(cd terraform && terraform output -raw ai_advisor_role_arn)"
  echo "    IRSA_ROLE_ARN=\$IRSA_ROLE_ARN bash scripts/setup-wif.sh"
fi

# ---------- 6. Credential Config 파일 생성 ----------
echo
echo "[6] GCP Credential Config 파일 생성..."
mkdir -p "$OUT_DIR"
gcloud iam workload-identity-pools create-cred-config \
  "//iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}" \
  --service-account="${GCP_SA_EMAIL}" \
  --aws \
  --output-file="${OUT_DIR}/gcp-credential-config.json"

echo "    생성: ${OUT_DIR}/gcp-credential-config.json"
echo
echo "================================================"
echo " 완료! 다음 단계:"
echo "================================================"
echo " 1. terraform apply (IRSA 역할 생성)"
echo " 2. IRSA_ROLE_ARN=\$(cd terraform && terraform output -raw ai_advisor_role_arn)"
echo "    IRSA_ROLE_ARN=\$IRSA_ROLE_ARN bash scripts/setup-wif.sh   # 5번 바인딩"
echo " 3. kubectl apply -k k8s/ai-advisor/"
echo " 4. kubectl create secret generic ai-advisor-secrets \\"
echo "      --from-literal=GEMINI_API_KEY=<키> -n ticketing"
echo "================================================"
