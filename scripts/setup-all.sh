#!/usr/bin/env bash
# terraform apply 후 EKS 클러스터 전체 셋업을 자동으로 수행합니다.
# 사용법: bash scripts/setup-all.sh
# DB_PASSWORD 환경변수가 필요합니다: export DB_PASSWORD='your-password'
set -euo pipefail

# AWS CLI v2 가 TTY 환경에서 출력 길이와 무관하게 기본 pager(less/more)로 stdout 을 보내,
# Git Bash/Windows 등에서 화면 하단에 "(END)" 만 떠 있고 키 입력 대기로 멈추는 증상이
# 반복적으로 발생한다. 모든 자식 프로세스(terraform local-exec, install-*.sh, helm 의
# `aws eks get-token` exec 등)가 상속받도록 entry-point 에서 한 번에 비활성화한다.
export AWS_PAGER=""

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
TF_DIR="$ROOT/terraform"

# ── 0. .env.local 자동 source (prepare.sh 가 생성) ──
# DB_PASSWORD / TF_VAR_db_password 를 매 세션마다 export 하지 않아도 되게 함.
if [[ -f "$ROOT/.env.local" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.env.local"
fi

cd "$TF_DIR"

# ── 0. DB_PASSWORD 확인 ──
if [[ -z "${DB_PASSWORD:-}" ]]; then
  echo "ERROR: DB_PASSWORD 환경변수가 비었습니다." >&2
  echo "  → 'bash scripts/prepare.sh' 를 먼저 실행하거나" >&2
  echo "    수동으로: export DB_PASSWORD='your-password'" >&2
  exit 1
fi

# ── 0.1. helm 자동 설치 (install-* 스크립트 3종이 전부 helm 필요) ──
# Git Bash(MINGW) / Linux / macOS 모두 지원. $HOME/bin에 배치 + PATH 주입.
# 다음 세션부터 PATH 유지되도록 ~/.bashrc에 1회 등록.
ensure_helm() {
  if command -v helm >/dev/null 2>&1; then
    return 0
  fi
  # 이전 실행에서 $HOME/bin에 깔았는데 PATH만 빠진 경우
  if [[ -x "$HOME/bin/helm" || -x "$HOME/bin/helm.exe" ]]; then
    export PATH="$HOME/bin:$PATH"
    command -v helm >/dev/null 2>&1 && { echo "helm 기존 설치 발견 (PATH 갱신)"; return 0; }
  fi

  echo "helm 미설치 → 자동 설치 시작"
  local HELM_VERSION="v3.16.3"
  local TMP_DIR
  TMP_DIR="$(mktemp -d)"
  mkdir -p "$HOME/bin"

  local UNAME
  UNAME="$(uname -s 2>/dev/null || echo unknown)"
  case "$UNAME" in
    MINGW*|MSYS*|CYGWIN*)
      local ZIP_NAME="helm-${HELM_VERSION}-windows-amd64.zip"
      curl -fsSL "https://get.helm.sh/${ZIP_NAME}" -o "$TMP_DIR/helm.zip" \
        || { echo "ERROR: helm 다운로드 실패"; rm -rf "$TMP_DIR"; return 1; }
      # Windows에서 가장 확실한 unzip은 PowerShell Expand-Archive
      local ZIP_WIN OUT_WIN
      ZIP_WIN="$(cygpath -w "$TMP_DIR/helm.zip" 2>/dev/null || echo "$TMP_DIR/helm.zip")"
      OUT_WIN="$(cygpath -w "$TMP_DIR" 2>/dev/null || echo "$TMP_DIR")"
      powershell -NoProfile -Command \
        "Expand-Archive -Path '$ZIP_WIN' -DestinationPath '$OUT_WIN' -Force" \
        || { echo "ERROR: helm 압축 해제 실패"; rm -rf "$TMP_DIR"; return 1; }
      cp "$TMP_DIR/windows-amd64/helm.exe" "$HOME/bin/helm.exe"
      ;;
    Linux)
      local TAR_NAME="helm-${HELM_VERSION}-linux-amd64.tar.gz"
      curl -fsSL "https://get.helm.sh/${TAR_NAME}" -o "$TMP_DIR/helm.tgz" \
        || { echo "ERROR: helm 다운로드 실패"; rm -rf "$TMP_DIR"; return 1; }
      tar xzf "$TMP_DIR/helm.tgz" -C "$TMP_DIR"
      cp "$TMP_DIR/linux-amd64/helm" "$HOME/bin/helm"
      chmod +x "$HOME/bin/helm"
      ;;
    Darwin)
      local TAR_NAME="helm-${HELM_VERSION}-darwin-amd64.tar.gz"
      curl -fsSL "https://get.helm.sh/${TAR_NAME}" -o "$TMP_DIR/helm.tgz" \
        || { echo "ERROR: helm 다운로드 실패"; rm -rf "$TMP_DIR"; return 1; }
      tar xzf "$TMP_DIR/helm.tgz" -C "$TMP_DIR"
      cp "$TMP_DIR/darwin-amd64/helm" "$HOME/bin/helm"
      chmod +x "$HOME/bin/helm"
      ;;
    *)
      echo "ERROR: 미지원 OS ($UNAME). helm을 직접 설치 후 재시도하세요." >&2
      rm -rf "$TMP_DIR"
      return 1
      ;;
  esac

  rm -rf "$TMP_DIR"
  export PATH="$HOME/bin:$PATH"

  if ! command -v helm >/dev/null 2>&1; then
    echo "ERROR: helm 자동 설치 실패" >&2
    return 1
  fi

  echo "helm 설치 완료: $(helm version --short 2>/dev/null || echo unknown) → $HOME/bin"

  # ~/.bashrc에 PATH 영구 등록 (중복 방지)
  if [[ -f "$HOME/.bashrc" ]] && ! grep -q 'HOME/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/bin:$PATH"' >> "$HOME/.bashrc"
    echo "  → ~/.bashrc에 PATH 영구 등록"
  fi
}

ensure_helm

# ── 0.2. kubectl 자동 설치 (EKS 1.30 호환 kubectl v1.30.0) ──
# helm은 내부 k8s 클라이언트를 써서 kubectl 없이도 돌지만,
# apply-ticketing-k8s.sh · DB 스키마 초기화 · rollout · ingress 조회 등에서 kubectl 필수.
ensure_kubectl() {
  if command -v kubectl >/dev/null 2>&1; then
    return 0
  fi
  if [[ -x "$HOME/bin/kubectl" || -x "$HOME/bin/kubectl.exe" ]]; then
    export PATH="$HOME/bin:$PATH"
    command -v kubectl >/dev/null 2>&1 && { echo "kubectl 기존 설치 발견 (PATH 갱신)"; return 0; }
  fi

  echo "kubectl 미설치 → 자동 설치 시작"
  mkdir -p "$HOME/bin"
  local KUBECTL_VERSION="v1.30.0"
  local UNAME
  UNAME="$(uname -s 2>/dev/null || echo unknown)"

  case "$UNAME" in
    MINGW*|MSYS*|CYGWIN*)
      curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/windows/amd64/kubectl.exe" \
        -o "$HOME/bin/kubectl.exe" \
        || { echo "ERROR: kubectl 다운로드 실패"; return 1; }
      ;;
    Linux)
      curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o "$HOME/bin/kubectl" \
        || { echo "ERROR: kubectl 다운로드 실패"; return 1; }
      chmod +x "$HOME/bin/kubectl"
      ;;
    Darwin)
      curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/darwin/amd64/kubectl" \
        -o "$HOME/bin/kubectl" \
        || { echo "ERROR: kubectl 다운로드 실패"; return 1; }
      chmod +x "$HOME/bin/kubectl"
      ;;
    *)
      echo "ERROR: 미지원 OS ($UNAME). kubectl 수동 설치 후 재시도." >&2
      return 1
      ;;
  esac

  export PATH="$HOME/bin:$PATH"
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "ERROR: kubectl 자동 설치 실패" >&2
    return 1
  fi
  echo "kubectl 설치 완료 → $HOME/bin"
}

ensure_kubectl

# destroy.sh를 거치지 않고 setup-all.sh 단독 실행 또는 destroy 실패 후
# 재시도 시, tfvars에 옛 ALB ARN이 박혀 있으면 main.tf의 data
# "aws_lb_listener" 가 NotFound로 첫 apply 자체를 fail시킨다.
TFVARS="$TF_DIR/terraform.tfvars"
REGION_PRE="$(terraform output -raw aws_region 2>/dev/null || echo "ap-northeast-2")"
if [ -f "$TFVARS" ] && grep -q '^alb_listener_arn' "$TFVARS"; then
  CURRENT_ARN=$(grep '^alb_listener_arn' "$TFVARS" | sed 's/.*= *"//;s/".*//')
  if [ -n "$CURRENT_ARN" ]; then
    if ! aws elbv2 describe-listeners --listener-arns "$CURRENT_ARN" \
        --region "$REGION_PRE" >/dev/null 2>&1; then
      echo "tfvars: 옛 alb_listener_arn이 invalid → 빈값으로 reset"
      sed -i 's|^alb_listener_arn.*|alb_listener_arn = ""|' "$TFVARS"
      sed -i 's|^frontend_callback_domain.*|frontend_callback_domain = ""|' "$TFVARS"
    fi
  fi
fi

# ── 1. Terraform Init + Apply ──
echo "=========================================="
echo " [1/14] Terraform Init + Apply"
echo "=========================================="
# fork 클론 직후(.terraform 없음) 또는 provider 가 추가된 경우 필수.
# terraform init 은 idempotent — 이미 초기화된 상태면 즉시 끝나고,
# 새 provider 만 있을 때 그것만 추가 다운로드한다. -input=false 로 자동화 안전.
terraform init -input=false
terraform apply -auto-approve

# ── 2. kubeconfig 설정 ──
echo ""
echo "=========================================="
echo " [2/14] kubeconfig 설정"
echo "=========================================="
CLUSTER_NAME="$(terraform output -raw eks_cluster_name)"
REGION="$(terraform output -raw aws_region)"
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION"
echo "kubeconfig 설정 완료"

# Pod 수 절약: coredns 2→1, ebs-csi-controller 2→1
kubectl scale deployment coredns -n kube-system --replicas=1 2>/dev/null || true
kubectl scale deployment ebs-csi-controller -n kube-system --replicas=1 2>/dev/null || true

# ── 3. AWS Load Balancer Controller ──
# terraform/alb-controller-helm.tf (null_resource) 가 [1/14] terraform apply 단계에서
# 이미 helm upgrade --install + webhook endpoint 대기까지 수행하므로 여기서 별도 호출 X.
# 과거엔 이 단계에서 한 번 더 install 을 시도했는데, helm release 가 이미 있어
# "이미 설치됨 → upgrade" 경로로 빠지면서 webhook config 만 강제 삭제하고
# controller pod 는 재시작되지 않아 self-signed cert 와 새 caBundle 이 어긋나
# webhook cert 검증이 매번 실패했다. 중복 단계 제거.
echo ""
echo "=========================================="
echo " [3/14] AWS Load Balancer Controller (terraform apply 에서 이미 설치됨 — skip)"
echo "=========================================="

# ── 4. Cluster Autoscaler 설치 ──
echo ""
echo "=========================================="
echo " [4/14] Cluster Autoscaler 설치"
echo "=========================================="
bash "$SCRIPTS/install-cluster-autoscaler.sh"

# ── 5. KEDA 설치 (SQS 큐 길이 기반 worker-svc 자동 스케일링) ──
echo ""
echo "=========================================="
echo " [5/14] KEDA 설치"
echo "=========================================="
bash "$SCRIPTS/install-keda.sh"

# ── 5.5. kube-prometheus-stack 설치 (모니터링) ──
echo ""
echo "=========================================="
echo " [6/14] kube-prometheus-stack 설치"
echo "=========================================="
if [[ -f "$SCRIPTS/install-monitoring.sh" ]]; then
  bash "$SCRIPTS/install-monitoring.sh"
else
  echo "WARNING: install-monitoring.sh 없음 — 모니터링 설치 건너뜀"
fi

# ── 7. GitOps bootstrap: namespace + Secret ──
# ArgoCD가 k8s/ 매니페스트를 sync 하기 전에 필요한 최소한의 리소스.
# 나머지(Deployment/Service/HPA/Ingress/ScaledObject...)는 ArgoCD가 생성.
echo ""
echo "=========================================="
echo " [7/14] GitOps bootstrap (namespace + secret)"
echo "=========================================="
bash "$SCRIPTS/bootstrap-ticketing-secret.sh"

# ── 8. DB 스키마 초기화 ──
echo ""
echo "=========================================="
echo " [8/14] DB 스키마 초기화"
echo "=========================================="
DB_WRITER_HOST="$(terraform output -raw rds_writer_endpoint)"

kubectl run mysql-init --image=mysql:8.0 --restart=Never -n ticketing \
  --command -- sleep 3600 2>/dev/null || true
echo "MySQL 클라이언트 파드 대기 중..."
kubectl wait --for=condition=Ready pod/mysql-init -n ticketing --timeout=120s

cat "$ROOT/db-schema/create.sql" | kubectl exec -i mysql-init -n ticketing -- \
  mysql --force --default-character-set=utf8mb4 -h "$DB_WRITER_HOST" -u root -p"$DB_PASSWORD" 2>&1 || true

cat "$ROOT/db-schema/Insert.sql" | kubectl exec -i mysql-init -n ticketing -- \
  mysql --default-character-set=utf8mb4 -h "$DB_WRITER_HOST" -u root -p"$DB_PASSWORD" ticketing 2>&1 || true

kubectl delete pod mysql-init -n ticketing --wait=false
echo "DB 스키마 + 시드 데이터 적용 완료"

# ── 9. Docker 이미지 빌드 & ECR Push ──
# GitOps 순서: ArgoCD가 Deployment를 만들기 전에 이미지가 ECR에 올라가 있어야
# 첫 pull이 성공한다. 이미지가 없어도 ArgoCD는 Deployment를 만들고 파드는
# ImagePullBackOff로 기다리다가 이미지가 올라오면 자동 복구되긴 하지만,
# Synced+Healthy 상태를 일찍 달성하기 위해 ArgoCD 설치 직전에 push 한다.
echo ""
echo "=========================================="
echo " [9/14] Docker 이미지 빌드 & ECR Push"
echo "=========================================="
ACCOUNT_ID="$(terraform output -raw aws_account_id)"
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

if ! command -v docker >/dev/null 2>&1; then
  echo "WARNING: docker CLI 미설치 — 이미지 빌드 skip."
  echo "  ArgoCD가 Deployment를 만들면 ImagePullBackOff 상태로 대기함."
  echo "  나중에 GitHub Actions (build-and-publish.yml) 또는 CloudShell에서 push:"
  echo "    aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR_REGISTRY"
  echo "    for SVC in ticketing-was worker-svc; do"
  echo "      docker build -t $ECR_REGISTRY/ticketing/\$SVC:latest services/\$SVC"
  echo "      docker push $ECR_REGISTRY/ticketing/\$SVC:latest"
  echo "    done"
  echo "    docker build -t $ECR_REGISTRY/ticketing/frontend:latest frontend/"
  echo "    docker push $ECR_REGISTRY/ticketing/frontend:latest"
else
  aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"
  for SVC in ticketing-was worker-svc; do
    echo "빌드 & 푸시: $SVC"
    docker build -t "${ECR_REGISTRY}/ticketing/${SVC}:latest" "$ROOT/services/${SVC}"
    docker push "${ECR_REGISTRY}/ticketing/${SVC}:latest"
  done
  echo "빌드 & 푸시: frontend"
  docker build -t "${ECR_REGISTRY}/ticketing/frontend:latest" "$ROOT/frontend"
  docker push "${ECR_REGISTRY}/ticketing/frontend:latest"
  echo "이미지 push 완료 (이 시점엔 아직 Deployment 없음 — ArgoCD가 step 10에서 생성)"
fi

# ── 9.5. PLACEHOLDER_ACCOUNT_ID → 팀원 계정 ID 자동 치환 ──
# K8s 매니페스트에 PLACEHOLDER_ACCOUNT_ID 로 표기된 계정 ID 자리들(ECR 이미지
# registry, IAM role ARN)을 현재 팀원 계정 ID 로 일괄 치환한 뒤 fork 레포에
# 자동 커밋·푸시한다. ArgoCD 는 git 을 단일 진실로 삼으므로, 이 값이 틀리면
# ImagePullBackOff(잘못된 ECR) 또는 IRSA token 실패(존재하지 않는 role ARN)로
# 배포가 터진다.
echo ""
echo "=========================================="
echo " [9.5/14] PLACEHOLDER_ACCOUNT_ID → ${ACCOUNT_ID} 자동 치환"
echo "=========================================="
PLACEHOLDER_FILES=(
  "k8s/kustomization.yaml"                 # ECR 이미지 registry
  "k8s/_runtime/sqs-service-account.yaml"  # KEDA IRSA role ARN (ArgoCD 관리 밖)
)
CHANGED_FILES=()
for REL in "${PLACEHOLDER_FILES[@]}"; do
  FULL="$ROOT/$REL"
  [[ -f "$FULL" ]] || { echo "WARN: $REL 없음 — 건너뜀" >&2; continue; }
  # 두 패턴 모두 커버:
  #   1) PLACEHOLDER_ACCOUNT_ID         (git 원본)
  #   2) 이전 배포자의 12자리 AWS 계정 ID (다른 사람이 substitute 한 상태를 fork 뜬 경우)
  # 결과: ECR registry / IRSA role ARN 둘 다 현재 ACCOUNT_ID 로 고정.
  TMP="$(mktemp)"
  # sed 구분자는 # (regex 내 | 는 alternation 이므로 outer 구분자와 충돌 금지).
  sed -E "s#(PLACEHOLDER_ACCOUNT_ID|[0-9]{12})(\.dkr\.ecr\.)#${ACCOUNT_ID}\2#g;
          s#(arn:aws:iam::)(PLACEHOLDER_ACCOUNT_ID|[0-9]{12})(:)#\1${ACCOUNT_ID}\3#g" "$FULL" > "$TMP"
  if ! cmp -s "$TMP" "$FULL"; then
    mv "$TMP" "$FULL"
    CHANGED_FILES+=("$REL")
  else
    rm -f "$TMP"
  fi
done

if [[ ${#CHANGED_FILES[@]} -eq 0 ]]; then
  echo "placeholder 이미 본인 계정(${ACCOUNT_ID}) 으로 치환됨 — skip"
else
  echo "치환된 파일: ${CHANGED_FILES[*]}"
  # kustomization.yaml 은 ArgoCD 가 git 에서 직접 읽으므로 원격(본인 fork) 에 push 필수.
  # _runtime/ 파일들은 ArgoCD 관리 밖이지만 일관성을 위해 같이 커밋.
  (
    cd "$ROOT"
    git add "${CHANGED_FILES[@]}"
    if ! git diff --cached --quiet; then
      git commit -m "chore: substitute PLACEHOLDER_ACCOUNT_ID → ${ACCOUNT_ID}" >/dev/null
      BRANCH=$(git rev-parse --abbrev-ref HEAD)
      if git push origin "$BRANCH" 2>&1; then
        echo "  origin/$BRANCH 로 push 완료 → ArgoCD 가 이 값을 읽음"
      else
        echo "  WARN: git push 실패. 본인 fork 인지 확인 후 수동 push:" >&2
        echo "        git push origin $BRANCH" >&2
        echo "  ArgoCD 가 PLACEHOLDER 를 읽어 ImagePullBackOff 발생할 수 있음." >&2
      fi
    fi
  )
fi

# ── 10. ArgoCD 설치 + ticketing Application 등록 ──
echo ""
echo "=========================================="
echo " [10/14] ArgoCD 설치 + Application 등록"
echo "=========================================="
bash "$SCRIPTS/install-argocd.sh"

# ── 11. ArgoCD Application Synced + Healthy 대기 ──
# ArgoCD가 git을 폴링하여 k8s/ 전체를 cluster에 적용. Deployment가 생성되면
# pod가 위에서 push한 이미지를 pull하고 Ready 상태가 되어야 Healthy 판정.
# 첫 sync는 보통 1~3분 내 완료. Ingress가 ALB를 provision하는 데 추가 1~2분.
echo ""
echo "=========================================="
echo " [11/14] ArgoCD Application Synced 대기"
echo "=========================================="
echo "ticketing Application 상태 폴링 (최대 10분)..."
# ArgoCD 'Suspended' 는 의도된 paused 상태 (KEDA ScaledObject 가 paused-replicas annotation 을
# 갖고 있으면 발생). Deploy 자체는 성공이므로 Healthy/Suspended 둘 다 종료 조건.
for i in $(seq 1 60); do
  SYNC=$(kubectl get application ticketing -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null || echo "Unknown")
  HEALTH=$(kubectl get application ticketing -n argocd -o jsonpath='{.status.health.status}' 2>/dev/null || echo "Unknown")
  if [[ "$SYNC" == "Synced" && ( "$HEALTH" == "Healthy" || "$HEALTH" == "Suspended" ) ]]; then
    echo "  Synced+${HEALTH} 달성 ($((i*10))s)"
    break
  fi
  echo "  [$((i*10))s] sync=$SYNC health=$HEALTH"
  if [[ "$i" -eq 60 ]]; then
    echo "WARNING: 10분 내 Synced+Healthy/Suspended 안 됨. ArgoCD UI에서 확인 필요." >&2
    kubectl get application ticketing -n argocd -o jsonpath='{.status.conditions}' >&2 || true
    echo "" >&2
  fi
  sleep 10
done
kubectl get pods -n ticketing

# ── 12. 프론트엔드 S3 배포 ──
echo ""
echo "=========================================="
echo " [12/14] 프론트엔드 S3 배포"
echo "=========================================="
BUCKET="ticketing-frontend-${ACCOUNT_ID}"
COGNITO_CLIENT_ID="$(terraform output -raw cognito_client_id)"
COGNITO_USER_POOL_ID="$(terraform output -raw cognito_user_pool_id)"
API_GW_ENDPOINT="$(terraform output -raw api_gateway_endpoint)"

# index.html에 Cognito + API GW 설정을 인라인 주입
# (api-origin.js 별도 파일 방식은 CloudFront 캐시 이슈 발생하여 인라인으로 전환)
sed "s|<script src=\"/api-origin.js\"></script>|<script>window.__TICKETING_API_ORIGIN__=\"${API_GW_ENDPOINT}\";window.COGNITO_CONFIG={REGION:\"${REGION}\",CLIENT_ID:\"${COGNITO_CLIENT_ID}\",USER_POOL_ID:\"${COGNITO_USER_POOL_ID}\"};</script>|" \
  "$ROOT/frontend/src/index.html" > /tmp/index.html

# 프론트엔드 파일 sync (index.html은 치환본으로 덮어쓰기)
aws s3 sync "$ROOT/frontend/src/" "s3://${BUCKET}/" --region "$REGION" --delete
aws s3 cp /tmp/index.html "s3://${BUCKET}/index.html" \
  --content-type "text/html; charset=utf-8" --region "$REGION"
rm -f /tmp/index.html

CF_DIST_ID="$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Origins.Items[?Id=='S3-frontend']].Id | [0]" \
  --output text 2>/dev/null || true)"
if [[ -n "$CF_DIST_ID" && "$CF_DIST_ID" != "None" ]]; then
  aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" --paths "/*" >/dev/null
  echo "CloudFront 캐시 무효화 완료"
fi

CLOUDFRONT_DOMAIN="$(terraform output -raw cloudfront_domain)"

# ── 13. API Gateway VPC Link Integration 설정 ──
# Internal ALB가 ingress로 만들어진 후, listener ARN을 추출하여
# tfvars에 박고 terraform apply 재실행 → API GW Integration/Route 생성
# 흐름: 브라우저 → CloudFront → API GW → VPC Link → Internal ALB → EKS
echo ""
echo "=========================================="
echo " [13/14] API Gateway VPC Link Integration 연결"
echo "=========================================="
echo "Internal ALB 주소 대기 중 (ingress가 ALB 만들 때까지)..."
for i in $(seq 1 30); do
  ALB_ADDRESS="$(kubectl get ingress -n ticketing -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "$ALB_ADDRESS" ]]; then break; fi
  echo "  대기 중... ($i/30)"
  sleep 10
done

if [[ -z "$ALB_ADDRESS" ]]; then
  echo "WARNING: Internal ALB 주소를 가져올 수 없습니다. API GW Integration이 생성되지 않습니다."
else
  echo "Internal ALB: $ALB_ADDRESS"

  # ALB의 HTTP listener ARN 추출 (API GW VPC Link Integration의 target)
  echo "ALB listener ARN 조회 중..."
  ALB_ARN="$(aws elbv2 describe-load-balancers --region "$REGION" \
    --query "LoadBalancers[?DNSName=='${ALB_ADDRESS}'].LoadBalancerArn | [0]" \
    --output text 2>/dev/null || true)"

  if [[ -z "$ALB_ARN" || "$ALB_ARN" == "None" ]]; then
    echo "WARNING: ALB ARN을 찾을 수 없습니다. ALB가 아직 등록 중일 수 있습니다."
  else
    LISTENER_ARN="$(aws elbv2 describe-listeners --region "$REGION" \
      --load-balancer-arn "$ALB_ARN" \
      --query "Listeners[?Port==\`80\`].ListenerArn | [0]" \
      --output text 2>/dev/null || true)"

    if [[ -z "$LISTENER_ARN" || "$LISTENER_ARN" == "None" ]]; then
      echo "WARNING: ALB listener ARN을 찾을 수 없습니다."
    else
      echo "Listener ARN: $LISTENER_ARN"

      # terraform.tfvars에 alb_listener_arn + frontend_callback_domain 저장
      # → 다음 apply에서 API GW Integration/Route 생성 + Cognito 콜백 URL 실제 도메인으로 갱신
      TFVARS="$TF_DIR/terraform.tfvars"
      if [[ -f "$TFVARS" ]] && grep -q '^alb_listener_arn' "$TFVARS"; then
        sed -i "s|^alb_listener_arn.*|alb_listener_arn = \"$LISTENER_ARN\"|" "$TFVARS"
      else
        echo "alb_listener_arn = \"$LISTENER_ARN\"" >> "$TFVARS"
      fi
      if [[ -f "$TFVARS" ]] && grep -q '^frontend_callback_domain' "$TFVARS"; then
        sed -i "s|^frontend_callback_domain.*|frontend_callback_domain = \"$CLOUDFRONT_DOMAIN\"|" "$TFVARS"
      else
        echo "frontend_callback_domain = \"$CLOUDFRONT_DOMAIN\"" >> "$TFVARS"
      fi
      echo "terraform.tfvars에 alb_listener_arn + frontend_callback_domain 저장 완료"

      # Terraform 재실행 → API GW Integration + Route 생성, Cognito 콜백 URL 갱신
      echo "Terraform apply 재실행 중 (API GW Integration 생성 + Cognito 콜백 갱신)..."
      terraform -chdir="$TF_DIR" apply -auto-approve
      echo "API GW Integration 생성 완료. CloudFront 전파 3~5분 소요"
    fi
  fi

  # ── 14. 모니터링 확인 ──
  # kube-prometheus-stack이 EKS 클러스터 내에 설치되어 있으므로
  # Grafana/Prometheus는 kubectl port-forward로 접근한다.
  echo ""
  echo "=========================================="
  echo " [14/14] 모니터링 확인"
  echo "=========================================="
  if kubectl get namespace monitoring >/dev/null 2>&1; then
    echo "kube-prometheus-stack이 monitoring namespace에 설치됨"
    echo "  Grafana:    kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80"
    echo "  Prometheus: kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090"
  else
    echo "WARNING: monitoring namespace 없음 — install-monitoring.sh 확인 필요"
  fi
fi

echo ""
echo "=========================================="
echo " 전체 셋업 완료!"
echo "=========================================="
API_GW_ENDPOINT="$(terraform output -raw api_gateway_endpoint 2>/dev/null || echo '(아직 미생성)')"

# Grafana / ArgoCD ALB 는 ALB Controller 가 k8s Ingress 로 만들어 terraform state 밖에 있음.
# 여기서 kubectl 로 직접 조회해 한눈에 보이게 안내한다(ALB DNS 전파에 수 분 걸릴 수 있음).
GRAFANA_ALB="$(kubectl -n monitoring get ingress grafana -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
ARGOCD_ALB="$(kubectl -n argocd get ingress argocd-server -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
GRAFANA_PW="$(kubectl -n monitoring get secret kube-prometheus-stack-grafana -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null || true)"

echo "  프론트엔드:    https://${CLOUDFRONT_DOMAIN}"
echo "  API (사용자):  https://${CLOUDFRONT_DOMAIN}/api/events"
echo "  API GW (직접): ${API_GW_ENDPOINT}/api/events"
echo "  Internal ALB:  ${ALB_ADDRESS:-미확인} (VPC 내부에서만 접근 가능)"
echo "  Grafana:       http://${GRAFANA_ALB:-(ALB DNS 대기 중 — 'kubectl -n monitoring get ingress grafana' 재확인)}/grafana"
echo "                 (admin / ${GRAFANA_PW:-prom-operator})"
echo "  ArgoCD UI:     http://${ARGOCD_ALB:-(ALB DNS 대기 중 — 'kubectl -n argocd get ingress argocd-server' 재확인)}"
echo ""
echo "  kubectl get nodes"
echo "  kubectl get pods -n ticketing"
