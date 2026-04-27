#!/usr/bin/env bash
# GCP AI Advisor 전체 자동 설치 스크립트.
# AWS setup-all.sh 가 완료된 후 실행하세요.
#
# 사용:  bash scripts/setup-gcp.sh
#
# 포함 작업:
#   [1]  사전 요건 확인 (gcloud / python3 / docker / kubectl)
#   [2]  Python 패키지 설치
#   [3]  GCP 로그인 (application-default login)
#   [4]  Workload Identity Federation 구성
#   [5]  terraform apply (IRSA 역할 + ECR 리포지터리)
#   [6]  GCP Credential Config 생성 → K8s ConfigMap 적용
#   [7]  ServiceAccount IRSA ARN 주입
#   [8]  Docker 이미지 빌드 → ECR push
#   [9]  K8s Secret + CronJob 배포
#   [10] 즉시 실행 테스트
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── 공통 헬퍼 ──
hr()  { printf '\n==========================================================\n'; }
ok()  { printf ' ✓ %s\n' "$*"; }
err() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
skip(){ printf ' → skip (%s)\n' "$*"; }

NS="ticketing"

hr; echo " setup-gcp.sh — GCP AI Advisor 자동 설치"; hr

# ── .env.local 자동 source ──
if [[ -f "$ROOT/.env.local" ]]; then
  set -a; source "$ROOT/.env.local"; set +a
fi

# ==========================================================
# [1] 사전 요건 확인
# ==========================================================
hr; echo " [1] 사전 요건 확인"

need() {
  command -v "$1" >/dev/null 2>&1 || \
    err "'$1' 미설치. guideREADME.txt [G-0-A] 참조하여 설치 후 재실행."
}
need gcloud
need python3
need docker
need kubectl
need aws
need terraform
ok "필수 CLI 확인 완료"

# AWS 자격증명
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "AWS 자격증명 없음. 'aws configure' 먼저 실행."
fi
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-northeast-2)}"
ok "AWS 계정: $AWS_ACCOUNT / 리전: $AWS_REGION"

# kubectl 클러스터 연결
if ! kubectl cluster-info >/dev/null 2>&1; then
  err "kubectl 클러스터 연결 안 됨. setup-all.sh 먼저 실행했는지 확인."
fi
ok "kubectl 클러스터 연결 확인"

# GEMINI_API_KEY 확인
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo
  read -rp "GEMINI_API_KEY 를 입력하세요 (aistudio.google.com 에서 발급): " GEMINI_API_KEY
  [[ -z "$GEMINI_API_KEY" ]] && err "GEMINI_API_KEY 는 필수입니다."
  echo "GEMINI_API_KEY=$GEMINI_API_KEY" >> "$ROOT/.env.local"
  ok "GEMINI_API_KEY .env.local 에 저장 완료"
fi
ok "GEMINI_API_KEY 확인"

# ==========================================================
# [2] Python 패키지 설치
# ==========================================================
hr; echo " [2] Python 패키지 설치"

pip_cmd=""
for c in pip3 pip "python3 -m pip"; do
  if $c --version >/dev/null 2>&1; then pip_cmd="$c"; break; fi
done
[[ -z "$pip_cmd" ]] && err "pip 를 찾을 수 없습니다. Python 설치 확인."

$pip_cmd install -r "$ROOT/scripts/requirements.txt" --break-system-packages -q \
  || $pip_cmd install -r "$ROOT/scripts/requirements.txt" -q
ok "패키지 설치 완료 (boto3, google-cloud-logging, google-genai 등)"

# ==========================================================
# [3] GCP 로그인
# ==========================================================
hr; echo " [3] GCP 로그인"

GCP_PROJECT="${GCP_PROJECT_ID:-soldesk-gcp}"

# gcloud 프로젝트 설정
gcloud config set project "$GCP_PROJECT" --quiet
ok "gcloud project: $GCP_PROJECT"

# Application Default Credentials
ADC_FILE="$HOME/.config/gcloud/application_default_credentials.json"
if [[ -f "$ADC_FILE" ]]; then
  skip "ADC 이미 존재 ($ADC_FILE)"
else
  echo " → 브라우저가 열립니다. 구글 계정으로 로그인하세요."
  gcloud auth application-default login --quiet
  ok "ADC 로그인 완료"
fi

# gcloud 일반 로그인도 확인
if ! gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null | grep -q "@"; then
  echo " → gcloud 계정 로그인이 필요합니다."
  gcloud auth login --quiet
fi
ok "gcloud 인증 완료"

# ==========================================================
# [4] Workload Identity Federation 구성
# ==========================================================
hr; echo " [4] Workload Identity Federation 구성"

GCP_PROJECT_NUMBER=$(gcloud projects describe "$GCP_PROJECT" --format="value(projectNumber)")
POOL_ID="eks-pool"
PROVIDER_ID="eks-provider"
GCP_SA_NAME="eks-ai-advisor"
GCP_SA_EMAIL="${GCP_SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
CRED_CONFIG="$ROOT/k8s/ai-advisor/gcp-credential-config.json"

# 4-1. Identity Pool
if gcloud iam workload-identity-pools describe "$POOL_ID" \
    --project="$GCP_PROJECT" --location="global" &>/dev/null; then
  skip "Workload Identity Pool 이미 존재"
else
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --project="$GCP_PROJECT" --location="global" \
    --display-name="EKS AI Advisor Pool" --quiet
  ok "Workload Identity Pool 생성"
fi

# 4-2. AWS Provider
if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
    --project="$GCP_PROJECT" --location="global" \
    --workload-identity-pool="$POOL_ID" &>/dev/null; then
  skip "AWS Provider 이미 존재"
else
  gcloud iam workload-identity-pools providers create-aws "$PROVIDER_ID" \
    --project="$GCP_PROJECT" --location="global" \
    --workload-identity-pool="$POOL_ID" \
    --account-id="$AWS_ACCOUNT" \
    --attribute-mapping="google.subject=assertion.arn,attribute.aws_role=assertion.arn.extract('assumed-role/{role}/')" \
    --quiet
  ok "AWS Provider 생성"
fi

# 4-3. GCP Service Account
if gcloud iam service-accounts describe "$GCP_SA_EMAIL" \
    --project="$GCP_PROJECT" &>/dev/null; then
  skip "GCP Service Account 이미 존재"
else
  gcloud iam service-accounts create "$GCP_SA_NAME" \
    --project="$GCP_PROJECT" \
    --display-name="EKS AI Advisor (WIF)" --quiet
  ok "GCP Service Account 생성"
fi

# 4-4. Cloud Logging 쓰기 권한 (SA 생성 직후 IAM 전파 대기)
echo " → IAM 전파 대기 중 (최대 15초)..."
for i in $(seq 1 5); do
  if gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:${GCP_SA_EMAIL}" \
    --role="roles/logging.logWriter" --quiet 2>/dev/null; then
    ok "Cloud Logging Writer 권한 부여"
    break
  fi
  echo "   재시도 $i/5..."
  sleep 3
done

# ==========================================================
# [5] terraform apply (IRSA 역할 + ECR)
# ==========================================================
hr; echo " [5] terraform apply — IRSA 역할 + ECR 리포지터리"

cd "$ROOT/terraform"
terraform init -upgrade -input=false -no-color 2>&1 | tail -3
terraform apply \
  -target=module.ai_advisor \
  -target=aws_ecr_repository.ai_advisor \
  -auto-approve -input=false -no-color 2>&1 | tail -10

IRSA_ROLE_ARN=$(terraform output -raw ai_advisor_role_arn)
ECR_URL=$(terraform output -raw ai_advisor_ecr_url)
cd "$ROOT"
ok "IRSA 역할: $IRSA_ROLE_ARN"
ok "ECR URL  : $ECR_URL"

# ==========================================================
# [4 continued] IRSA 역할 + 노드 역할 → GCP SA 바인딩
# ==========================================================
# attribute.aws_role 은 extract('assumed-role/{role}/') 결과 = role 이름만 사용
ROLE_NAME=$(echo "$IRSA_ROLE_ARN" | awk -F'/' '{print $NF}')
IRSA_MEMBER="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.aws_role/${ROLE_NAME}"

gcloud iam service-accounts add-iam-policy-binding "$GCP_SA_EMAIL" \
  --project="$GCP_PROJECT" \
  --role="roles/iam.workloadIdentityUser" \
  --member="$IRSA_MEMBER" --quiet
ok "WIF 바인딩 (IRSA 역할): $ROLE_NAME → $GCP_SA_EMAIL"

# EKS 노드 인스턴스 프로파일 역할도 WIF 바인딩 (IMDS 경유 인증에 필요)
CLUSTER_NAME="${CLUSTER_NAME:-ticketing-eks}"
NODE_ROLE_NAME=$(aws iam get-instance-profile \
  --instance-profile-name "$(aws ec2 describe-instances \
    --filters "Name=tag:eks:cluster-name,Values=${CLUSTER_NAME}" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' \
    --region "$AWS_REGION" --output text 2>/dev/null | awk -F'/' '{print $NF}')" \
  --query 'InstanceProfile.Roles[0].RoleName' \
  --output text 2>/dev/null || echo "")

if [[ -n "$NODE_ROLE_NAME" && "$NODE_ROLE_NAME" != "None" ]]; then
  NODE_MEMBER="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.aws_role/${NODE_ROLE_NAME}"
  gcloud iam service-accounts add-iam-policy-binding "$GCP_SA_EMAIL" \
    --project="$GCP_PROJECT" \
    --role="roles/iam.workloadIdentityUser" \
    --member="$NODE_MEMBER" --quiet
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="$NODE_MEMBER" \
    --role="roles/logging.logWriter" --quiet
  ok "WIF 바인딩 (노드 역할): $NODE_ROLE_NAME → $GCP_SA_EMAIL"
else
  echo " → 노드 역할 자동 감지 실패 — 수동으로 추가 필요 (guideREADME.txt [0-F] 참고)"
fi

# ==========================================================
# [6] GCP Credential Config → K8s ConfigMap
# ==========================================================
hr; echo " [6] GCP Credential Config 생성 + ConfigMap 적용"

gcloud iam workload-identity-pools create-cred-config \
  "//iam.googleapis.com/projects/${GCP_PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}" \
  --service-account="$GCP_SA_EMAIL" \
  --aws \
  --enable-imdsv2 \
  --output-file="$CRED_CONFIG" --quiet
ok "Credential Config 생성 (IMDSv2): $CRED_CONFIG"

# audience 중복 제거 (gcloud 버그: //iam.googleapis.com 이 두 번 들어가는 경우)
# bash 변수를 직접 Python 문자열로 치환 — 환경변수 export 순서 문제 없음
python3 -c "
import json
path = '${CRED_CONFIG}'
if not path:
    print(' → CRED_CONFIG 경로 없음 (skip)')
else:
    with open(path) as f: d = json.load(f)
    aud = d.get('audience', '')
    if aud.startswith('//iam.googleapis.com///iam.googleapis.com'):
        d['audience'] = aud.replace(
            '//iam.googleapis.com///iam.googleapis.com',
            '//iam.googleapis.com', 1)
        with open(path, 'w') as f: json.dump(d, f, indent=2)
        print(' → audience 중복 수정 완료')
    else:
        print(' → audience 정상:', aud[:60])
"

kubectl create configmap gcp-credential-config \
  --from-file=config.json="$CRED_CONFIG" \
  -n "$NS" --dry-run=client -o yaml | kubectl apply -f -
ok "K8s ConfigMap gcp-credential-config 적용"

# IMDS hop limit 2 설정 (EKS Pod → IMDS 접근 허용)
echo " → EKS 노드 IMDS hop limit 2 설정 중..."
aws ec2 describe-instances \
  --filters "Name=tag:eks:cluster-name,Values=${CLUSTER_NAME:-ticketing-eks}" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' \
  --region "$AWS_REGION" --output text 2>/dev/null | tr '\t' '\n' | while read -r iid; do
    [[ -z "$iid" ]] && continue
    aws ec2 modify-instance-metadata-options \
      --instance-id "$iid" \
      --http-put-response-hop-limit 2 \
      --http-endpoint enabled \
      --region "$AWS_REGION" --output text >/dev/null 2>&1 && printf "   hop limit 2: %s\n" "$iid"
done
ok "IMDS hop limit 설정 완료"

# ==========================================================
# [7] serviceaccount.yaml IRSA ARN 주입
# ==========================================================
hr; echo " [7] ServiceAccount IRSA ARN 주입"

SA_FILE="$ROOT/k8s/ai-advisor/serviceaccount.yaml"
if grep -q "PLACEHOLDER_AI_ADVISOR_ROLE_ARN" "$SA_FILE"; then
  sed -i.bak "s|PLACEHOLDER_AI_ADVISOR_ROLE_ARN|${IRSA_ROLE_ARN}|" "$SA_FILE"
  rm -f "${SA_FILE}.bak"
  ok "serviceaccount.yaml ARN 교체 완료"
else
  skip "ARN 이미 설정됨"
fi

# ==========================================================
# [8] Docker 이미지 빌드 → ECR push
# ==========================================================
hr; echo " [8] Docker 이미지 빌드 → ECR push"

aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin \
  "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com" 2>&1 | grep -v "^$" || true
ok "ECR 로그인 완료"

# EKS 노드는 항상 linux/amd64 — Mac M1/M2/M3(arm64)에서 실행해도 amd64로 빌드
HOST_ARCH=$(uname -m)
if [[ "$HOST_ARCH" == "aarch64" || "$HOST_ARCH" == "arm64" ]]; then
  echo " → Apple Silicon / ARM64 감지 — buildx 로 linux/amd64 크로스 빌드"
  docker buildx build \
    --platform linux/amd64 \
    -t "${ECR_URL}:latest" \
    -f "$ROOT/services/ai-advisor/Dockerfile" \
    --push \
    "$ROOT"
  ok "ECR push 완료 (amd64 크로스빌드): ${ECR_URL}:latest"
else
  echo " → x86_64 감지 — 네이티브 빌드"
  docker build -t ai-advisor:latest \
    -f "$ROOT/services/ai-advisor/Dockerfile" "$ROOT" --quiet
  docker tag ai-advisor:latest "${ECR_URL}:latest"
  docker push "${ECR_URL}:latest" 2>&1 | tail -3
  ok "ECR push 완료: ${ECR_URL}:latest"
fi

# cronjob.yaml 이미지 URI 주입
CRON_FILE="$ROOT/k8s/ai-advisor/cronjob.yaml"
if grep -q "PLACEHOLDER_ECR_URI" "$CRON_FILE"; then
  sed -i.bak "s|PLACEHOLDER_ECR_URI/ai-advisor:latest|${ECR_URL}:latest|" "$CRON_FILE"
  rm -f "${CRON_FILE}.bak"
  ok "cronjob.yaml 이미지 URI 교체 완료"
else
  skip "이미지 URI 이미 설정됨"
fi

# ==========================================================
# [9] K8s Secret + CronJob 배포
# ==========================================================
hr; echo " [9] K8s Secret + CronJob 배포"

# GEMINI_API_KEY Secret
if kubectl get secret ai-advisor-secrets -n "$NS" &>/dev/null; then
  skip "ai-advisor-secrets 이미 존재"
else
  kubectl create secret generic ai-advisor-secrets \
    --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
    -n "$NS"
  ok "ai-advisor-secrets 생성"
fi

# Slack Webhook (선택)
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  kubectl patch secret ai-advisor-secrets -n "$NS" \
    -p "{\"stringData\":{\"SLACK_WEBHOOK_URL\":\"${SLACK_WEBHOOK_URL}\"}}" \
    --type=merge 2>/dev/null || true
  ok "SLACK_WEBHOOK_URL 주입"
fi

# RBAC — ai-advisor-sa 가 클러스터 리소스를 읽을 수 있도록
kubectl apply -f - <<'RBACEOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ai-advisor-reader
rules:
  - apiGroups: [""]
    resources: ["nodes", "pods", "events", "namespaces"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list"]
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list"]
  - apiGroups: ["keda.sh"]
    resources: ["scaledobjects"]
    verbs: ["get", "list"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ai-advisor-reader
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ai-advisor-reader
subjects:
  - kind: ServiceAccount
    name: ai-advisor-sa
    namespace: ticketing
RBACEOF
ok "RBAC ClusterRole/ClusterRoleBinding 적용"

# ServiceAccount + CronJob 배포
kubectl apply -k "$ROOT/k8s/ai-advisor/"
ok "K8s 리소스 배포 완료"

# ==========================================================
# [10] 즉시 실행 테스트
# ==========================================================
hr; echo " [10] 즉시 실행 테스트"

# 이전 테스트 Job 정리
kubectl delete job ai-advisor-test -n "$NS" --ignore-not-found --wait=false 2>/dev/null || true

kubectl create job ai-advisor-test \
  --from=cronjob/ai-advisor -n "$NS"
ok "테스트 Job 생성 완료"

echo
echo " 로그 확인 (약 30초 후):"
echo "   kubectl logs -n $NS -l job-name=ai-advisor-test -f"
echo
echo " GCP Logs Explorer:"
echo "   eks-metrics          → https://console.cloud.google.com/logs/query;query=logName%3D%22projects%2F${GCP_PROJECT}%2Flogs%2Feks-metrics%22?project=${GCP_PROJECT}"
echo "   gemini-recommendations → https://console.cloud.google.com/logs/query;query=logName%3D%22projects%2F${GCP_PROJECT}%2Flogs%2Fgemini-recommendations%22?project=${GCP_PROJECT}"

hr
echo " 설치 완료! CronJob 이 10분마다 자동 실행됩니다."
hr
