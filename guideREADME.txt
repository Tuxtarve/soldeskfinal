==========================================================
 Ticketing 독립 배포 가이드
==========================================================

[이 가이드가 하는 일]
이 프로젝트를 자기 AWS 계정에 통째로 복제해서, 원본과 똑같은 구조로
독립적으로 운영/실험할 수 있게 만들어줍니다. 원본과 데이터/리소스는
완전히 분리됩니다.

[전체 흐름 — 명령 3줄이면 끝]
  1. 사전 준비물 설치 (한 번만 — 0단계)
  2. git clone + checkout FINAL        ← 1단계
  3. bash scripts/prepare.sh           ← 값 자동 세팅 (2단계)
  4. bash scripts/setup-all.sh         ← 한 방 배포 (3단계)

[결과물]
자기 AWS 계정에 다음이 자동 구축됩니다.
  - 네트워크: VPC / Subnet / NAT / ALB(internal)
  - 컴퓨트: EKS (t3.small 노드)
  - 데이터: RDS MySQL(Writer+Reader) / ElastiCache Redis / SQS(FIFO 2개)
  - 프론트: S3 정적 호스팅 + CloudFront
  - 인증: Cognito User Pool + Hosted UI
  - GitOps: ArgoCD (자기 git repo 감시)
  - 모니터링: Prometheus + Grafana + Loki + Promtail (EKS 내)
  - 애플리케이션: 영화·공연·극장 티켓팅 풀스택


==========================================================
 0. 사전 준비물 (한 번만 — 이미 있으면 건너뛰기)
==========================================================

──────────────────────────────────────────────────────────
[0-A] 필수 CLI 5개 + gh (선택)
──────────────────────────────────────────────────────────
  aws, kubectl, helm, terraform, docker  ← 필수
  gh                                     ← 선택 (GitHub Secrets 자동 등록용)

■ Windows (PowerShell 관리자 권한)
    winget install -e --id Amazon.AWSCLI
    winget install -e --id Kubernetes.kubectl
    winget install -e --id Helm.Helm
    winget install -e --id Hashicorp.Terraform
    winget install -e --id Docker.DockerDesktop
    winget install -e --id GitHub.cli      # 선택
  → 설치 끝나면 PowerShell 창 닫고 Git Bash 새로 열기.

■ macOS (Homebrew 후)
    brew install awscli kubectl helm terraform gh
    brew install --cask docker

■ Ubuntu / WSL / Linux
    # aws
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip \
      && unzip -q awscliv2.zip && sudo ./aws/install && rm -rf aws awscliv2.zip
    # kubectl
    curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
      && chmod +x kubectl && sudo mv kubectl /usr/local/bin/
    # helm
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    # terraform
    wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
      && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list \
      && sudo apt update && sudo apt install -y terraform gh
    # docker
    curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER && newgrp docker

확인 (전부 버전이 나오면 OK):
    aws --version && kubectl version --client && helm version --short \
      && terraform -version && docker --version

──────────────────────────────────────────────────────────
[0-B] AWS 자격증명 등록
──────────────────────────────────────────────────────────
AWS 콘솔 → IAM → 본인 user → Security credentials → "Create access key"

    aws configure
      AWS Access Key ID:      [발급받은 키 ID]
      AWS Secret Access Key:  [발급받은 시크릿]
      Default region name:    ap-northeast-2
      Default output format:  json

확인:
    aws sts get-caller-identity      # 12자리 계정 ID 나오면 OK

──────────────────────────────────────────────────────────
[0-C] Docker Desktop 실행
──────────────────────────────────────────────────────────
  Windows/macOS: Docker Desktop 앱 실행 (고래 아이콘 "Running")
  Linux: 위에서 설치했으면 자동 실행 중.

    docker ps                        # 에러 없으면 OK

──────────────────────────────────────────────────────────
[0-D] (선택) gh CLI 로그인
──────────────────────────────────────────────────────────
GitHub Secrets(AWS_ACCOUNT_ID) 를 prepare.sh 가 자동으로 등록하려면:

    gh auth login                    # 브라우저 열려서 로그인

gh 없으면 prepare.sh 가 수동 등록 방법을 안내합니다.


==========================================================
 1. Fork & Clone
==========================================================
  1) 브라우저에서 https://github.com/sxk34/soldesk 접속
  2) 우상단 "Fork" → 자기 계정으로 fork
  3) 터미널:
       git clone https://github.com/<본인GitHub아이디>/soldesk.git
       cd soldesk
       git checkout FINAL


==========================================================
 2. 자동 세팅 (prepare.sh)
==========================================================

    bash scripts/prepare.sh

스크립트가 자동으로:
  - terraform/terraform.tfvars 생성 + 값 자동 채움
      · cognito_domain_prefix  = myticket-auth-<계정ID 뒷6자리>  (전역 유일 보장)
      · github_repo            = 현재 git origin 에서 자동 감지
  - argocd/application.yaml 의 repoURL 을 본인 fork 로 교체 후
    FINAL 브랜치에 자동 commit & push
  - RDS 마스터 비밀번호를 대화형으로 입력받아 .env.local 에 저장
      · setup-all.sh 가 자동으로 source → 매번 export 할 필요 없음
      · .env.local 은 .gitignore 로 제외되어 git 에 안 올라감
  - (gh CLI 로그인 되어있으면) GitHub Secret AWS_ACCOUNT_ID 자동 등록

재실행 안전 — 이미 채워져 있으면 해당 단계는 skip.


==========================================================
 3. 한 방 배포 (setup-all.sh)
==========================================================

    bash scripts/setup-all.sh

스크립트 자동 수행 (총 14단계):
  [1]  Terraform 1차 apply  → VPC/EKS/RDS/Cognito/S3/CloudFront
  [2]  kubeconfig 설정
  [3]  AWS Load Balancer Controller
  [4]  Cluster Autoscaler
  [5]  KEDA
  [6]  Prometheus + Grafana + Loki + Promtail
  [7]  Kubernetes Secret 생성
  [8]  RDS 스키마 + 시드데이터 주입
  [9]  Docker 이미지 빌드 → ECR push
  [10] ArgoCD 설치 + Application 등록
  [11] ArgoCD Synced+Healthy 대기
  [12] 프론트엔드 S3 업로드
  [13] Internal ALB → tfvars 자동 기록 → Terraform 2차 apply
  [14] 모니터링/ArgoCD 접속 안내 출력


==========================================================
 4. 동작 확인
==========================================================

[A] 프론트엔드
  setup-all.sh 마지막 출력 "프론트엔드:" URL(CloudFront) 접속
  → 회원가입(Cognito) → 로그인 → 영화 목록 → 예매 테스트

[B] ArgoCD UI
  마지막 출력 "ArgoCD UI:" URL 접속
  로그인: admin / 아래 명령으로 비번 조회
    kubectl -n argocd get secret argocd-initial-admin-secret \
      -o jsonpath="{.data.password}" | base64 -d

[C] Grafana (메트릭 + 로그)
  마지막 출력 "Grafana:" URL 접속 (끝에 /grafana 붙어있음)
  로그인: admin / prom-operator (또는 아래 명령으로 확인)
    kubectl -n monitoring get secret kube-prometheus-stack-grafana \
      -o jsonpath="{.data.admin-password}" | base64 -d
  → Dashboards → "Node Exporter / Nodes", "Kubernetes / Views" 등
  → Explore → Loki → {namespace="ticketing"} 로 로그 검색

[D] ALB 접속 안 되거나 VPN 환경이면 port-forward fallback
    kubectl port-forward -n argocd svc/argocd-server 8080:80
    # http://localhost:8080
    kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
    # http://localhost:3000

[E] 파드 상태
    kubectl get pods -n ticketing      # Running / 1/1 이면 정상


==========================================================
 5. 전체 삭제 (과금 멈춤)
==========================================================

    bash scripts/destroy.sh

  - k8s 리소스 정리 (ingress/ALB 먼저 → orphan ENI 방지)
  - ArgoCD 제거
  - Terraform destroy
  - S3 버킷 비우기

  주의: destroy 후에도 CloudWatch Logs / ECR 이미지 등은 남아있을 수
        있으니 AWS 콘솔 Billing 에서 며칠 후 0원인지 확인.


==========================================================
 (선택) GitHub Actions CI/CD
==========================================================
이 가이드 범위 밖. 본인 FINAL 브랜치에 push 했을 때 자동으로 이미지
빌드 → ECR → ArgoCD 배포가 돌게 하려면:
  - terraform/modules/cicd 를 root main.tf 에 module 로 추가 연결
  - terraform apply 후 'terraform output github_actions_role_arn' 값을
    GitHub Secret AWS_ROLE_ARN 에 등록
  (기본 배포에는 불필요 — setup-all.sh 가 이미지 push 까지 전부 수행)


==========================================================
 GCP AI Advisor 가이드 (AWS 배포 완료 후 진행)
==========================================================

[이 가이드가 하는 일]
EKS 클러스터의 실시간 메트릭을 수집해 Gemini AI 에게 분석을 맡기고,
오토스케일 추천 결과를 GCP Cloud Logging 에 저장 + Slack 으로 알림.
AWS 인프라는 전혀 건드리지 않습니다.

[전체 흐름]
  collect_metrics.sh  →  recommend_scaling.py  →  recommendation_to_patches.py
                                                →  push_to_cloud_logging.py
                                                →  notify.py (Slack)

[결과물]
  - scripts/data/metrics-<ts>.json          EKS/SQS 스냅샷
  - scripts/data/recommendation-<ts>.json   Gemini 추천 JSON
  - scripts/data/patches-<ts>/              Kustomize 패치 YAML (바로 적용 가능)
  - GCP Logs Explorer                       추천 이력 영구 보관
  - Slack (선택)                            priority:now 항목 즉시 알림


==========================================================
 G-0. 사전 준비물 (한 번만)
==========================================================

──────────────────────────────────────────────────────────
[G-0-A] gcloud CLI 설치
──────────────────────────────────────────────────────────
■ macOS (Homebrew)
    brew install --cask google-cloud-sdk

■ Windows (PowerShell 관리자 권한)
    winget install -e --id Google.CloudSDK
  → 설치 후 PowerShell 재시작.

■ Ubuntu / WSL / Linux
    curl https://sdk.cloud.google.com | bash
    exec -l $SHELL

확인:
    gcloud --version          # 버전 나오면 OK

──────────────────────────────────────────────────────────
[G-0-B] Python 패키지 설치
──────────────────────────────────────────────────────────
■ macOS / Linux
    pip install google-genai pyyaml --break-system-packages

■ Windows (Git Bash)
    pip install google-genai pyyaml

확인:
    python3 -c "from google import genai; print('OK')"

──────────────────────────────────────────────────────────
[G-0-C] .env.local 생성 (git 에 올라가지 않음)
──────────────────────────────────────────────────────────
프로젝트 루트에 .env.local 파일을 만들고 아래 내용 입력.
이 파일은 .gitignore 로 제외되어 있어 git 에 절대 올라가지 않습니다.

    GEMINI_API_KEY=발급받은_키_입력
    GEMINI_MODEL=gemini-2.5-flash

    # Slack 알림 쓰려면 주석 해제 후 URL 입력
    # SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

Gemini API 키 발급:
  https://aistudio.google.com → "Get API key" → 키 복사

연결 확인:
    source .env.local
    python3 scripts/gemini_ping.py   # AI 응답 나오면 OK


==========================================================
 G-1. GCP 로그인 및 프로젝트 설정
==========================================================

    gcloud auth login
    gcloud config set project soldesk-gcp

확인:
    gcloud config get-value project      # soldesk-gcp 나오면 OK
    gcloud logging logs list             # 로그 목록 조회 되면 OK

※ setup-all.sh 실행 중 기다리는 동안 미리 해두면 시간 절약됩니다.


==========================================================
 G-2. AI Advisor 파이프라인 실행
==========================================================
※ EKS 클러스터가 Running 상태여야 합니다 (setup-all.sh 완료 후).

──────────────────────────────────────────────────────────
[G-2-1] 메트릭 수집
──────────────────────────────────────────────────────────
    source .env.local
    bash scripts/collect_metrics.sh

  → scripts/data/metrics-<타임스탬프>.json 생성
  → EKS 노드·Pod·HPA·KEDA·SQS 깊이를 하나의 JSON 으로 압축

──────────────────────────────────────────────────────────
[G-2-2] Gemini 추천 받기
──────────────────────────────────────────────────────────
    python3 scripts/recommend_scaling.py

  → Gemini 2.5 Flash 가 메트릭을 분석해 스케일링 추천 생성
  → scripts/data/recommendation-<타임스탬프>.json 저장
  → 터미널에 우선순위별 요약 표 출력 (NOW / WATCH / LATER)

──────────────────────────────────────────────────────────
[G-2-3] Kustomize 패치 파일 생성
──────────────────────────────────────────────────────────
    python3 scripts/recommendation_to_patches.py

  → scripts/data/patches-<타임스탬프>/ 디렉터리 생성
  → HPA·Deployment·KEDA 등 Kubernetes 리소스별 YAML 파일 자동 생성
  → 적용 전 반드시 README.md 확인 후 수동 검토

패치 적용 예시 (검토 후):
    kubectl apply -n ticketing --dry-run=server -f scripts/data/patches-<ts>/00-hpa-read-api.yaml
    kubectl apply -n ticketing -f scripts/data/patches-<ts>/00-hpa-read-api.yaml

──────────────────────────────────────────────────────────
[G-2-4] GCP Cloud Logging 전송
──────────────────────────────────────────────────────────
    python3 scripts/push_to_cloud_logging.py

  → 추천 JSON 을 GCP Cloud Logging 으로 전송
  → GCP 콘솔 → Logging → Logs Explorer 에서 조회:
      logName="projects/soldesk-gcp/logs/gemini-recommendations"

──────────────────────────────────────────────────────────
[G-2-5] Slack 알림 (선택 — SLACK_WEBHOOK_URL 설정 시)
──────────────────────────────────────────────────────────
    python3 scripts/notify.py

  → priority:now 항목만 Slack 채널로 알림 전송
  → SLACK_WEBHOOK_URL 미설정 시 stdout 출력으로 대체

──────────────────────────────────────────────────────────
[G-2-6] 한 방 실행 (전체 파이프라인)
──────────────────────────────────────────────────────────
    source .env.local && \
    bash scripts/collect_metrics.sh && \
    python3 scripts/recommend_scaling.py && \
    python3 scripts/recommendation_to_patches.py && \
    python3 scripts/push_to_cloud_logging.py && \
    python3 scripts/notify.py


==========================================================
 G-3. 데이터 관리
==========================================================

scripts/data/ 디렉터리는 .gitignore 로 제외되어 있습니다.
메트릭·추천·패치 파일이 누적되므로 주기적으로 정리하세요.

    ls scripts/data/                     # 누적 파일 확인
    rm scripts/data/metrics-*.json       # 오래된 메트릭 삭제 (선택)
