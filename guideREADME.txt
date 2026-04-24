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
