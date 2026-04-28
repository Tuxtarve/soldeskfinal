# AWS + GCP 티켓팅 인프라 — 면접·발표 정리

---

## 1. 전체 아키텍처 한 줄 요약

> **AWS EKS 위에서 영화·공연 티켓팅 서비스를 운영하고, GCP Gemini AI가 실시간 트래픽을 분석해 오토스케일링을 자동 추천·적용하는 풀스택 클라우드 인프라입니다.**

---

## 2. 전체 요청 흐름 (End-to-End)

### 사용자 요청 흐름
```
사용자 브라우저
  → CloudFront (CDN, WAF 포함)
  → S3 (정적 프론트엔드 제공)
  → API Gateway (외부 → 내부 VPC 라우팅)
  → Internal ALB (내부 로드밸런서)
  → EKS read-api  : 공연·영화 조회 (Redis 캐싱)
  → EKS write-api : 예매 요청 수신 → SQS FIFO 큐에 적재
  → EKS worker-svc: SQS에서 메시지 소비 → RDS MySQL 저장
```

### AI 자동화 사이클 (10분마다)
```
EKS CronJob (ai-advisor)
  → AWS 메트릭 수집 (kubectl + CloudWatch + Prometheus + SQS)
  → GCP Gemini AI 분석 (오토스케일 추천 생성)
  → GCP Cloud Logging 저장 (추천 이력 영구 보관)
  → Slack 알림 (긴급 항목만)
  → K8s YAML 패치 파일 자동 생성 (즉시 적용 가능)
```

---

## 3. AWS 서비스별 정리

### 네트워크

| 서비스 | 정의 | 역할 | 선택 이유 |
|--------|------|------|----------|
| **VPC** | 격리된 가상 네트워크 | Public/Private 서브넷 분리, NAT Gateway로 아웃바운드 제어 | 외부에 노출되면 안 되는 RDS·Redis를 Private 서브넷에 격리 |
| **ALB** | 애플리케이션 레이어 로드밸런서 | HTTP 트래픽을 EKS Pod로 분산 | Internal 설정으로 API Gateway VPC Link만 접근 가능, 직접 노출 차단 |
| **API Gateway** | 관리형 API 엔드포인트 | 외부 요청을 VPC Link를 통해 내부 ALB로 전달 | Cognito 토큰 검증, 속도 제한, 외부 노출 단일 창구 |

### 컴퓨팅

| 서비스 | 정의 | 역할 | 선택 이유 |
|--------|------|------|----------|
| **EKS** | 관리형 Kubernetes 서비스 | read-api·write-api·worker-svc 3개 서비스 컨테이너 실행 | 오토스케일링(HPA·KEDA·CA)과 GitOps(ArgoCD)를 네이티브로 지원 |
| **t3.small** | 2vCPU·2GiB 인스턴스 | EKS 워커 노드 (4~5대) | 프리티어 기준 비용 최소화, CA로 필요 시 자동 확장 |
| **ECR** | 컨테이너 이미지 레지스트리 | 빌드된 Docker 이미지 저장 | EKS와 동일 계정·리전으로 IAM 인증 자동화, 별도 설정 불필요 |

### 데이터

| 서비스 | 정의 | 역할 | 선택 이유 |
|--------|------|------|----------|
| **RDS MySQL** | 관리형 관계형 DB | 예매 데이터 영구 저장 (Writer + Reader 분리) | write-api는 쓰지 않고 worker만 쓰는 구조로 커넥션 집중 방지 |
| **ElastiCache Redis** | 인메모리 캐시 | read-api 조회 결과 캐싱, 대기열 상태 관리 | DB 직접 조회 부하를 캐시로 흡수, 응답속도 10배 향상 |
| **SQS FIFO** | 메시지 큐 서비스 | write-api가 예매 요청을 적재, worker가 순서대로 소비 | FIFO + MessageGroupId(콘서트ID)로 예매 공정성 보장, 초당 300건 처리 |

### 인증·CDN

| 서비스 | 정의 | 역할 | 선택 이유 |
|--------|------|------|----------|
| **Cognito** | 관리형 사용자 인증 | 회원가입·로그인·JWT 토큰 발급 | 자체 인증 서버 불필요, 비밀번호 해싱·세션 관리 AWS 위임 |
| **CloudFront** | 글로벌 CDN | S3 정적 프론트엔드 전세계 캐싱 배포 | WAF 연동으로 DDoS 방어, 엣지 캐싱으로 응답속도 단축 |
| **S3** | 객체 스토리지 | 프론트엔드(HTML·CSS·JS) 정적 호스팅 | 서버 없이 정적 웹사이트 운영, CloudFront와 연동해 비용 최소화 |

### 운영·자동화

| 서비스 | 정의 | 역할 | 선택 이유 |
|--------|------|------|----------|
| **HPA** | K8s 수평 Pod 자동확장 | CPU 55%(read-api)·45%(write-api) 초과 시 Pod 증설 | write-api는 SQS 전송 I/O로 CPU가 낮아도 응답지연 → 낮은 임계값 |
| **KEDA** | 이벤트 기반 자동확장 | SQS 큐 메시지 5개 이상 시 worker Pod 즉시 증설 | CPU 기반 HPA로는 SQS 부하를 감지 불가, 큐 깊이가 실제 부하 지표 |
| **Cluster Autoscaler** | K8s 노드 자동확장 | Pod Pending 발생 시 EC2 노드 자동 추가 | Pod 스케줄 실패를 감지해 노드 레이어를 자동 확장 |
| **ArgoCD** | GitOps 배포 도구 | GitHub FINAL 브랜치 감시 → 자동 배포 | 코드 push만으로 EKS 배포, 드리프트 자동 감지·복구 |
| **Prometheus + Grafana + Loki** | 모니터링 스택 | 메트릭 수집·시각화·로그 집계 | EKS 내부에 설치해 외부 의존 없이 운영, 경량화 적용(330Mi 절감) |

---

## 4. GCP 서비스별 정리

| 서비스 | 정의 | 역할 | 선택 이유 |
|--------|------|------|----------|
| **Gemini 2.5 Flash Lite** | Google의 경량 AI 언어모델 | EKS 메트릭을 분석해 HPA·KEDA 설정값 추천 | AWS에는 동급 AI 서비스 없음, Gemini는 구조화된 JSON 스키마 응답 지원 |
| **GCP Cloud Logging** | 클라우드 로그 저장소 | AI 추천 결과와 메트릭 이력을 영구 보관 | 30일 무료, 심각도별 필터링·알림 정책 연동 가능 |
| **Workload Identity Federation** | 키 파일 없는 크로스 클라우드 인증 | AWS IRSA 자격증명 → GCP 임시 토큰 자동 교환 | 서비스 계정 JSON 키 파일 없이 인증 → 키 유출 위험 0 |

### WIF 인증 흐름 (면접 단골 질문)
```
EKS Pod (IRSA 적용)
  → AWS STS: JWT 토큰 자동 발급
  → GCP Workload Identity Pool: AWS 자격증명 검증
  → GCP Service Account 임시 토큰 발급 (1시간 유효)
  → Cloud Logging / Gemini API 호출 가능
결과: 코드와 컨테이너에 비밀키 없음
```

---

## 5. 오토스케일링 구조

### 계층 1 — K8s 네이티브 (초 단위 반응)
- **HPA**: CPU 임계값 초과 → read-api·write-api 자동 증설
- **KEDA**: SQS 큐 5개 이상 → worker-svc 즉시 증설 (max 23대)
- **CA**: Pod Pending 발생 → 노드 자동 추가
- **특징**: 트래픽이 터진 후 반응, 10~30초 warm-up 지연

### 계층 2 — 예측 기반 실시간 (15초 단위)
- **realtime_monitor.py**: SQS 증가속도 측정 → 임계 도달 시간 예측
- **핵심 공식**: `ETA = (임계값 - 현재값) / 증가속도`
- **행동**: ETA < 30초이면 maxReplicas를 선제적으로 상향
- **특징**: 장애 발생 전에 미리 대응, HPA/KEDA 상한 병목 방지

### 계층 3 — 이벤트 사전 준비 (오픈 N분 전)
- **pre_scale.py**: 예상 동시 접속자 수 입력 → Gemini가 최적 replicas 계산
- **실측 기반**: 이분탐색으로 측정한 실제 임계값(read 202 RPS, write 102 RPS) 활용
- **자동 복구**: 오픈 30분 후 평시 값으로 자동 scale-down
- **특징**: 트래픽 폭주 전 Pod 워밍업 완료

### 계층 4 — AI 전략 추천 (10분 주기)
- **Gemini CronJob**: 메트릭 전체 분석 → HPA targetCPU·maxReplicas 구조 개선 제안
- **출력**: K8s YAML 패치 파일 자동 생성 → kubectl apply로 즉시 적용 가능
- **특징**: 단순 규칙이 아닌 RDS 커넥션 한계·비용·SLO를 함께 고려한 복합 판단

---

## 6. 차별화 포인트

### ① 키 파일 없는 크로스 클라우드 인증 (WIF)
AWS와 GCP를 연동할 때 서비스 계정 JSON 키 파일이 없습니다. AWS IRSA 토큰을 GCP가 직접 신뢰하는 방식이라 키 유출 위험이 구조적으로 제거됩니다. 이는 AWS와 Google 모두 권장하는 보안 모범 사례입니다.

### ② 실측 기반 임계값 설정
임계값을 추정이 아닌 실제 부하 테스트로 측정했습니다. Gemini가 이분 탐색으로 장애 직전 최대 부하를 자동 탐색(read-api 202 RPS, write-api 102 RPS)하고, 이 수치를 HPA·KEDA·사전 스케일링 계산에 실제로 반영합니다.

### ③ 반응형이 아닌 예측형 스케일링
기존 HPA/KEDA는 부하가 터진 후 반응합니다. 이 시스템은 SQS 증가속도를 측정해 "지금 추세라면 22초 후 임계에 도달한다"는 예측을 하고, 그 전에 미리 Pod를 늘립니다. 트래픽 스파이크 순간의 장애 가능성을 줄입니다.

### ④ 비용 최소화 설계
t3.small 4~5대(월 ~$75)로 평시 운영하고, 이벤트 시에만 CA가 노드를 추가합니다. 모니터링 스택도 retention·scrape 간격을 조정해 330Mi 메모리를 절감했고, GCP는 Gemini 월 $3 수준으로 유지합니다.

### ⑤ 완전 자동화된 한 방 배포
`bash scripts/setup-all.sh` 한 줄로 VPC·EKS·RDS·Cognito·ArgoCD·모니터링 전체가 구축됩니다. 팀원 각자의 AWS 계정에 독립 배포 가능하고, kubectl 아키텍처 불일치·EBS CSI 타임아웃 등 실제 발생한 오류를 스크립트 내에서 자동 복구합니다.

---

## 7. 발표용 요약

### 1분 버전 (3문장)

이 프로젝트는 AWS EKS 위에서 영화·공연 티켓팅 서비스를 운영하는 풀스택 클라우드 인프라입니다.

티켓팅 오픈 순간 수만 명이 몰리는 스파이크 트래픽에 대응하기 위해, 기존 HPA·KEDA 자동확장에 더해 GCP Gemini AI가 10분마다 메트릭을 분석해 오토스케일 설정을 자동으로 최적화하고, 이벤트 전에는 예상 접속자 수 기반으로 Pod를 미리 증설합니다.

특히 AWS와 GCP를 연동할 때 서비스 계정 키 파일 없이 Workload Identity Federation으로 인증하는 보안 설계와, 실측 부하 테스트로 측정한 임계값을 스케일링에 직접 반영하는 점이 차별화 포인트입니다.

---

### 3분 버전

**[구조 설명 — 1분]**

사용자 요청은 CloudFront → API Gateway → Internal ALB → EKS로 들어옵니다. EKS 안에는 역할이 분리된 3개 서비스가 있습니다. read-api는 Redis 캐싱으로 조회를 처리하고, write-api는 예매 요청을 SQS FIFO 큐에만 적재합니다. worker-svc가 큐에서 꺼내 RDS에 최종 저장하는 구조로, write-api가 DB 커넥션을 직접 소비하지 않아 트래픽 폭주 시 안정성이 높아집니다.

**[오토스케일링 — 1분]**

오토스케일링은 4개 계층으로 구성됩니다. K8s HPA·KEDA·CA가 초 단위로 Pod와 노드를 자동 확장하는 기본 레이어 위에, SQS 증가속도를 계산해 임계 도달 30초 전에 선제적으로 maxReplicas를 올리는 예측 레이어가 있습니다. 이벤트 10분 전에는 Gemini AI가 예상 접속자 수를 분석해 Pod를 미리 워밍업하고, 10분마다 돌아가는 CronJob은 전체 메트릭을 보고 HPA·KEDA 설정값을 전략적으로 조정합니다.

**[차별화 — 1분]**

가장 중요한 차별화 포인트는 세 가지입니다. 첫째, AWS와 GCP를 연동하면서 서비스 계정 JSON 키를 사용하지 않고 Workload Identity Federation으로 키 유출 위험을 구조적으로 없앴습니다. 둘째, 임계값을 추정이 아닌 Gemini 이분탐색으로 실제 측정해서 read-api 202 RPS, write-api 102 RPS라는 정확한 수치를 스케일링에 반영합니다. 셋째, 트래픽이 터진 후 반응하는 기존 방식이 아닌, 증가속도 예측으로 장애 발생 전에 미리 대응하는 구조입니다.
