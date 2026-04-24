#!/usr/bin/env bash
# EKS에 ArgoCD 설치 (Helm) + ticketing Application 등록.
# 사전: helm, kubectl (kubeconfig 설정), terraform output 가능 상태
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v helm >/dev/null 2>&1; then
  echo "helm 이 필요합니다." >&2
  exit 1
fi

NAMESPACE="argocd"
RELEASE="argocd"

helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
helm repo update >/dev/null

# ── values: ALB/Ingress 안 만듦 (port-forward 접근). 메모리 가벼운 옵션 ──
VALUES="$(mktemp)"
trap 'rm -f "$VALUES"' EXIT
cat >"$VALUES" <<'EOF'
global:
  priorityClassName: ticketing-priority-platform
configs:
  params:
    server.insecure: true
  cm:
    # 기본 admin 계정 비활성화, root 계정 신설
    admin.enabled: "false"
    accounts.root: "login"
  rbac:
    # root에게 admin 권한 부여
    policy.csv: |
      g, root, role:admin
    policy.default: ""
  secret:
    extra:
      # bcrypt(soldesk1.) — argocd account bcrypt --password 'soldesk1.'로 생성
      accounts.root.password: "$2a$10$0Sn244C61FveDwgHGeC2qe/8TAcl7j6NN2MpQe9rDSFZwYp1sk4i6"
server:
  service:
    type: ClusterIP
controller:
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
repoServer:
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
applicationSet:
  enabled: false
notifications:
  enabled: false
dex:
  enabled: false
EOF

# DNS/API 서버 연결 안정화 대기 (Git Bash 환경에서 간헐적 EKS endpoint DNS lookup 실패 방지)
for i in $(seq 1 12); do
  if kubectl get ns >/dev/null 2>&1; then break; fi
  echo "  kubectl 응답 대기 중... ($i/12)"
  sleep 5
done

if helm list -n "$NAMESPACE" 2>/dev/null | grep -q "^${RELEASE}\b"; then
  echo "이미 설치됨 → upgrade"
  helm upgrade "$RELEASE" argo/argo-cd -n "$NAMESPACE" -f "$VALUES" --wait
else
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply --validate=false -f -
  helm install "$RELEASE" argo/argo-cd -n "$NAMESPACE" -f "$VALUES" --wait
fi

echo "argocd-server rollout 대기..."
kubectl rollout status deployment/argocd-server -n "$NAMESPACE" --timeout=300s

# ── ticketing Application 등록 ────────────────────────────────────
# application.rendered.yaml 은 terraform/argocd.tf 의 local_file 이 생성.
# terraform apply 가 선행돼야 존재한다. git 에 커밋되지 않으므로 fork 한 팀원마다
# 본인 github_repo 기준으로 새로 렌더된 파일이 각 로컬에 떨어진다.
APP_MANIFEST="$ROOT/argocd/application.rendered.yaml"
if [[ ! -f "$APP_MANIFEST" ]]; then
  echo "ERROR: $APP_MANIFEST 없음." >&2
  echo "       terraform apply 가 먼저 실행돼야 렌더된다 (terraform/argocd.tf)." >&2
  exit 1
fi
echo "Application 등록: $APP_MANIFEST"
kubectl apply -f "$APP_MANIFEST"

# ── ArgoCD UI 외부 노출 Ingress (internet-facing ALB) ─────────────
INGRESS_MANIFEST="$ROOT/argocd/argocd-ingress.yaml"
if [[ -f "$INGRESS_MANIFEST" ]]; then
  echo "UI Ingress 등록: $INGRESS_MANIFEST"
  kubectl apply -f "$INGRESS_MANIFEST"
fi

# ── ALB 주소 대기 (ALB Controller가 internet-facing ALB 프로비저닝까지 2~3분) ─
echo "ArgoCD UI ALB 주소 대기 중..."
ARGOCD_UI_HOST=""
for i in $(seq 1 30); do
  ARGOCD_UI_HOST=$(kubectl get ingress argocd-server -n "$NAMESPACE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [[ -n "$ARGOCD_UI_HOST" ]] && break
  sleep 10
done

cat <<EOF

==================================================================
ArgoCD 설치 완료

UI 접속:
  ${ARGOCD_UI_HOST:+http://$ARGOCD_UI_HOST}
  ${ARGOCD_UI_HOST:-(ALB 주소 대기 중 — 잠시 후 kubectl get ingress argocd-server -n $NAMESPACE)}

대안 (port-forward):
  kubectl port-forward -n $NAMESPACE svc/argocd-server 8080:80
  → http://localhost:8080

로그인:
  username: root
  password: soldesk1.
  (기본 admin은 비활성화됨. 계정/비밀번호는 helm values에 bcrypt로 박혀있음)

Application 상태:
  kubectl get application -n $NAMESPACE
==================================================================
EOF
