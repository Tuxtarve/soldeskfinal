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

gh 없으면 prepare.sh 가 수동 설치 방법을 안내합니다.

■ macOS (Homebrew)
    brew install gh

■ Windows (PowerShell, 관리자 권한)
    winget install -e --id GitHub.cli

■ Ubuntu / WSL2 / Linux
    sudo apt update && sudo apt install -y gh

■ 설치 확인
    gh --version        # gh version 2.x.x 나오면 OK

■ 로그인 (자동 설정용)
    gh auth login

──────────────────────────────────────────────────────────
[0-E] OS별 주의사항 ★ Windows 팀원 필독
──────────────────────────────────────────────────────────

이 프로젝트의 배포 스크립트(setup-all.sh, setup-gcp.sh)는 bash 기반입니다.
OS에 따라 아래 내용을 반드시 확인하세요.

│ OS              │ 배포 방법         │ kubectl 아키텍처 │
│─────────────────┼─────────────────┼────────────────│ 
│ Mac (M1/M2/M3)  │ 터미널 직접 실행    │ ARM64  ← 주의!  │
│ Windows         │ WSL2(Ubuntu) 필수 │ AMD64 (WSL2 기준) │

▶ Windows — WSL2 필수 설치
  PowerShell에서 bash 스크립트를 직접 실행할 수 없습니다.
  반드시 WSL2(Ubuntu)를 설치하고, 그 안에서 모든 작업을 진행하세요.

  1) WSL2 + Ubuntu 설치 (PowerShell 관리자 권한)
       wsl --install -d Ubuntu
     → 재부팅 후 Ubuntu 터미널 열기

  2) WSL2 안에서 kubectl 설치 (AMD64 버전)
       curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
       chmod +x kubectl && sudo mv kubectl /usr/local/bin/kubectl
       kubectl version --client       # 버전 나오면 OK


▶ Mac Apple Silicon (M1/M2/M3) — kubectl ARM64 버전 필수
  Homebrew로 설치하면 자동으로 ARM64 버전이 설치됩니다.
  다른 경로(curl, 직접 다운로드)로 설치했다면 아래로 확인하세요.

  아키텍처 확인:
       file $(which kubectl)
     → "arm64" 나오면 OK
     → "x86-64" 나오면 아래 명령으로 교체:

  교체 방법:
       curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/darwin/arm64/kubectl"
       chmod +x kubectl && sudo mv kubectl /usr/local/bin/kubectl
       kubectl version --client       # 버전 나오면 OK

▶ Windows 시간 ↔ WSL 시간 동기화
sudo apt install ntpdate -y

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
EKS 메트릭을 10분마다 자동 수집해 Gemini AI 가 오토스케일 추천을 생성하고
GCP Cloud Logging 에 저장 + Slack 알림까지 자동으로 돌아갑니다.
setup-gcp.sh 한 번 실행하면 이후 수동 조작 없이 자동 운영됩니다.

[전체 흐름 — 명령 3줄이면 끝]
  1. 사전 준비 (한 번만)          ← G-0
  2. source .env.local
     bash scripts/setup-gcp.sh   ← G-1 (한 방 배포)
  3. 이후 자동 실행 (10분마다 CronJob)


==========================================================
 G-0. 사전 준비 (한 번만 — 이미 했으면 건너뛰기)
==========================================================

──────────────────────────────────────────────────────────
[G-0-1] gcloud CLI 설치
──────────────────────────────────────────────────────────
■ Windows (WSL2 Ubuntu 터미널) / Linux
    curl https://sdk.cloud.google.com | bash
    exec -l $SHELL

■ Mac → MacREADME.txt 참고

확인:
    gcloud --version          # 버전 나오면 OK

──────────────────────────────────────────────────────────
[G-0-2] GCP 로그인 (브라우저 필요 — 한 번만)
──────────────────────────────────────────────────────────
    gcloud auth login
    gcloud config set project soldesk-gcp
    gcloud auth application-default login \
      --scopes="https://www.googleapis.com/auth/cloud-platform"

  → 브라우저에서 구글 계정 로그인 → 허용 클릭
  → 이후 setup-gcp.sh 실행 시 자동 사용됨

확인:
    gcloud config get-value project      # soldesk-gcp 나오면 OK

──────────────────────────────────────────────────────────
[G-0-3] .env.local 생성 (git 에 올라가지 않음)
──────────────────────────────────────────────────────────
Gemini API 키 발급: https://aistudio.google.com → "Get API key"

■ Windows (WSL2) / Linux:
    cat > .env.local << 'EOF'
    export DB_PASSWORD='본인_DB_비밀번호'
    export TF_VAR_db_password='본인_DB_비밀번호'
    export GEMINI_API_KEY=발급받은_키_입력
    export GEMINI_MODEL=gemini-2.5-flash-lite
    export AWS_REGION=ap-northeast-2
    # export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
    EOF

  ※ DB_PASSWORD 는 prepare.sh 실행 시 입력한 값과 동일
  ※ gemini-1.5-flash / gemini-2.0-flash 는 지원 종료 — 사용 불가

연결 확인:
    source .env.local && python3 scripts/gemini_ping.py
    # OK 나오면 정상


==========================================================
 G-1. GCP 한 방 배포
==========================================================

    source .env.local
    bash scripts/setup-gcp.sh

[자동 처리 항목 — 수동 개입 없음]
  [1]  사전 요건 확인 (gcloud / python3 / docker / kubectl)
  [2]  Python 패키지 자동 설치
  [3]  Workload Identity Federation 구성 (서비스 계정 키 없음)
       - WIF Pool / AWS Provider / GCP Service Account 자동 생성
       - IRSA 역할 + EKS 노드 역할 WIF 바인딩 자동 설정
  [4]  terraform apply — IRSA 역할 + ECR 리포지터리 생성
  [5]  GCP Credential Config 생성 → K8s ConfigMap 적용
       (IMDSv2 활성화, EKS 노드 hop limit 자동 설정)
  [6]  ServiceAccount IRSA ARN 자동 주입
  [7]  Docker 이미지 빌드 → ECR push
       (ARM64/AMD64 자동 감지 후 분기)
  [8]  RBAC ClusterRole 생성 (K8s 리소스 읽기 권한)
  [9]  K8s Secret + CronJob 배포
  [10] 즉시 실행 테스트

[완료 후 자동으로 돌아가는 것들 (수동 실행 불필요)]
  EKS CronJob 이 10분마다:
    AWS 메트릭 수집 → GCP Cloud Logging 저장 (eks-metrics)
    → Gemini 오토스케일 추천 → GCP Cloud Logging 저장 (gemini-recommendations)
    → Slack 알림 (SLACK_WEBHOOK_URL 설정 시)

[로그 / 결과 확인]
  kubectl logs -n ticketing -l job-name=ai-advisor-test -f

  GCP Logs Explorer (브라우저):
    logName="projects/soldesk-gcp/logs/eks-metrics"
    logName="projects/soldesk-gcp/logs/gemini-recommendations"


==========================================================
 G-2. 선택 — 수동 실행 / 패치 적용 / 임계값 탐색
==========================================================
※ CronJob 이 자동 실행되므로 평시에는 불필요.
  디버깅·즉시 분석·부하 테스트 시만 사용.

──────────────────────────────────────────────────────────
[G-2-1] 파이프라인 즉시 실행 (한 방)
──────────────────────────────────────────────────────────
    source .env.local && \
    python3 scripts/collect_metrics.py && \
    python3 scripts/recommend_scaling.py && \
    python3 scripts/recommendation_to_patches.py && \
    python3 scripts/push_to_cloud_logging.py && \
    python3 scripts/notify.py

──────────────────────────────────────────────────────────
[G-2-2] Gemini 추천 패치 적용 순서
──────────────────────────────────────────────────────────
STEP 1 — 서버 검증
    kubectl apply -n ticketing --dry-run=server \
      -f scripts/data/patches-<ts>/00-hpa-read-api-hpa.yaml

STEP 2 — diff 확인 ⭐ 강추
    kubectl diff -n ticketing \
      -f scripts/data/patches-<ts>/00-hpa-read-api-hpa.yaml
  → "-" 현재값, "+" 변경될 값 확인

STEP 3 — 실제 적용
    kubectl apply -n ticketing \
      -f scripts/data/patches-<ts>/00-hpa-read-api-hpa.yaml

STEP 4 — 결과 확인
    kubectl get hpa -n ticketing
    kubectl get scaledobject -n ticketing

  ※ <ts> 는 실제 타임스탬프로 교체. 예) patches-20260427-063737

──────────────────────────────────────────────────────────
[G-2-3] 오토스케일링 임계값 탐색
──────────────────────────────────────────────────────────
Gemini 가 이분 탐색으로 장애 직전 최대 부하 임계값을 자동 탐색합니다.
약 20분, 10회 반복 후 HPA/KEDA 권장 설정값까지 출력됩니다.

    source .env.local

    # KEDA 임계값 (SQS 메시지 수 기준, worker-svc)
    python3 scripts/find_threshold.py --mode keda

    # HPA 임계값 (HTTP RPS 기준)
    python3 scripts/find_threshold.py --mode hpa-read
    python3 scripts/find_threshold.py --mode hpa-write

    # 현재 상태만 분석 (부하 없음, 비용 0)
    python3 scripts/find_threshold.py --mode keda --dry-run

  → 결과: scripts/data/threshold-<mode>-<ts>.json


==========================================================
 G-3. 데이터 관리
==========================================================

scripts/data/ 는 .gitignore 로 제외되어 있습니다.
메트릭·추천·패치 파일이 누적되므로 주기적으로 정리하세요.

    ls scripts/data/                     # 누적 파일 확인
    rm scripts/data/metrics-*.json       # 오래된 메트릭 삭제 (선택)
