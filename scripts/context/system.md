# Soldesk Ticketing — Gemini 오토스케일/튜닝 어드바이저 컨텍스트

> 이 파일은 Gemini 프롬프트의 **정적 컨텍스트**로 그대로 주입된다.
> SLO / 부하 / 비용 수치는 **추정 플레이스홀더**이며, 실측값이 생기면 해당 줄만 갱신한다.

---

## A. 아키텍처

```
User ─▶ CloudFront (+WAF) ─▶ S3 (frontend 정적)
             └▶ API Gateway (VPC Link) ─▶ Internal ALB ─▶ EKS(ticketing NS)
                                                          ├─ read-api      (Flask :5000)
                                                          ├─ write-api     (Flask :5001)
                                                          └─ worker-svc    (SQS consumer)
데이터 계층: RDS (MySQL/Aurora), ElastiCache(Redis), SQS(FIFO: ticketing-reservation.fifo)
스케일: HPA(read/write), KEDA ScaledObject(worker-svc, SQS depth 기반)
GitOps: ArgoCD
관측: kube-prometheus-stack + Loki + Grafana
```

## B. 서비스별 역할 & 특성

| 서비스 | 역할 | I/O 특성 | 주요 병목 |
|---|---|---|---|
| `read-api` | 공연/좌석/리뷰 조회 | RDS 읽기 + Redis 캐시 히트 | CPU 낮음, 캐시 미스 시 RDS read 쿼리 |
| `write-api` | 요청 검증 + **SQS enqueue만** (쓰기 자체는 안 함) | in: HTTP, out: SQS send | Cognito 토큰 검증, SQS throughput |
| `worker-svc` | SQS 소비 → RDS write + Redis 대기열 갱신 | in: SQS, out: RDS write + Redis | RDS 커넥션 풀, FIFO 300 msg/s |

## C. SLO (추정 — 실측 시 교체)

- `read-api`  p95 latency **< 200 ms**, 가용성 99.9%
- `write-api` p95 latency **< 500 ms** (SQS enqueue 성공 기준)
- **예매 접수 → worker 처리 완료 지연** < **30 s** (피크 한정 60 s 허용)
- 에러율 < 0.5%

## D. 부하 프로파일 (추정 — 실측 시 교체)

| 구간 | 설명 | RPS | 지속 |
|---|---|---|---|
| 평시 | 조회 위주 | read 5 / write 1 | 상시 |
| 오픈 직전 | 대기실 진입 폭증 | read 200 / write 10 | 5 분 |
| 오픈 순간 | 좌석 선점 경쟁 | **read 2000 / write 500** | **1 분** |
| 오픈 직후 | 결제·환불 | read 100 / write 50 | 10 분 |

- SQS 큐 깊이 피크 추정: **10,000 메시지** (1분 스파이크 시)
- worker-svc 목표 처리량: 메시지당 평균 200 ms → **pod당 5 msg/s** 가정

## E. 의존성 한계 (하드 캡)

- **노드**: `t3.small` × 4 = 총 **8 vCPU / 8 GiB** (시스템 파드 약 1.5 GiB 소비 가정 → 실가용 ≈ 6.5 GiB)
- **RDS `max_connections`**: 200 (인스턴스 `db.t3.micro`/`t3.small` 기준)
  - write 경로 총 커넥션 = worker replicas × pool_size + write-api replicas × pool_size ≤ 180 (관리 마진)
- **SQS FIFO**: 그룹당 300 msg/s (배치 사용 시 3000 msg/s 까지)
- **Redis**: 1 primary (대기열 + 캐시 혼용), maxmemory 약 0.5 GiB 추정
- **ALB**: internal, `10.0.0.0/16` 허용

## F. 비용/과금 제약

- **AWS 체험판 잔액 ≈ $100** (EKS control plane $0.10/h 는 상시 소모)
- **GCP 크레딧 ≈ ₩429,024** — Gemini API 호출에만 소모 예정 (호출당 1센트 미만)
- **월 예산 상한**: 체험판 소진 방지 — 노드 수 4대, 인스턴스 타입 `t3.small` 고정 선호
- 버스트 허용 조건: 단기(분 단위) 스파이크 시에만 burst Deployment 가동

## G. 현재 고정 설계 결정 (이유 있음, 바꾸기 전 경고)

- Ingress `scheme: internal` — VPC Link 라우팅 때문. 절대 `internet-facing`으로 바꾸지 말 것.
- Ingress `/` fallback → read-api: CORS 응답 보장용.
- `PriorityClass` 존재 (`priorityclass-ticketing`) + `PodDisruptionBudget`(`pdb-user-facing`): 노드 교체 시 user-facing 서비스 보호 목적.
- KEDA `minReplicaCount: 0` (worker) — 평시 비용 절감. 스파이크 시 warm-up 지연 허용 조건.

---

## H. 추천 범위 (Gemini가 다뤄야 할 항목)

1. **HPA** `minReplicas` / `maxReplicas` / target util + **`behavior`** (scaleUp stabilizationWindow, scaleDown policies)
2. **KEDA ScaledObject**: `minReplicaCount` / `maxReplicaCount` / `queueLength` / `pollingInterval` / `cooldownPeriod` / `activationQueueLength`
3. **Deployment `resources`**: requests/limits 재조정 (현재 Mem 81% 노드 존재 — request 저평가 의심)
4. **PDB** `minAvailable` / `maxUnavailable` 재검토
5. **`topologySpreadConstraints`** — AZ 균등 분산 (노드 4대가 AZ 분산돼 있다면)
6. **Probe** readiness/liveness initialDelay, period, failureThreshold
7. **PriorityClass** 적용 누락 여부 (user-facing에만 붙어 있는지)
8. **RDS 커넥션 풀** 크기 (write-api, worker-svc 의 SQLAlchemy pool_size × replicas ≤ max_connections)
9. **Redis `maxmemory-policy`** (대기열과 캐시가 섞이면 `allkeys-lru` 금지)
10. **노드/인스턴스 믹스** — t3.small 유지 vs t3.medium 일부 혼합 (비용 증가분 제시)
11. **예상 월 비용 증감** — 추천 적용 시 EC2 시간 증가·RDS 변경 여부 기반 대략 추정
12. **우선순위 분류** — `now`(즉시 반영) / `watch`(관찰 후 결정) / `later`(다음 릴리스)

## I. 추천하지 말 것 (범위 밖)

- 코드 변경 제안 (Flask 라우팅, ORM 쿼리 등)
- 관측 스택 구성 변경 (Prometheus rule 외)
- IAM/보안 그룹 재설계
- 멀티 리전 확장
- **파괴적 변경**: RDS 교체, EKS 재생성, 데이터 마이그레이션

## J. 메트릭 스냅샷 필드 해설 (동적 입력 참조용)

동적으로 주입되는 메트릭 JSON(§L)의 각 필드 의미. 빈 배열/객체는 수집 실패이므로 해당 항목 추천 제외.

| 필드 | 설명 | 활용 포인트 |
|------|------|------------|
| `nodeTop[].cpuPct` | 노드 CPU 사용률 % | 60% 이상이면 스케줄 여유 부족 신호 |
| `hpa[].currentMetrics` | HPA 가 실제로 보는 utilization | target 대비 비율로 scale 방향 판단 |
| `hpa[].desiredReplicas` | HPA 계산 결과 — 실제 적용과 다를 수 있음 | currentReplicas 와 차이 크면 stabilization 문제 |
| `sqs.ApproximateNumberOfMessages` | SQS 대기 메시지 수 | KEDA queueLength 기준 초과 여부 판단 |
| `prometheus.cpuRatePerContainer` | 5분 이동평균 CPU 사용 코어 수 | requests 대비 실사용 비교 → request 저평가 탐지 |
| `prometheus.memBytesPerContainer` | working set 메모리 실사용량 (bytes) | limits 대비 비율 계산 → OOMKill 리스크 |
| `prometheus.podRestarts` | 누적 재시작 횟수 | ≥3 이면 OOMKill/CrashLoop 의심 |
| `prometheus.hpaDesiredTrend15m` | HPA desired 15분 시계열 | 반복적 scaleUp/Down → stabilizationWindow 문제 |
| `cloudwatchRds.connections15m` | RDS 커넥션 수 최근 15분 | max_connections(200) 대비 여유 계산 |
| `cloudwatchRds.cpu15m` | RDS CPU % | 70% 이상이면 쿼리 최적화 or scale up 신호 |
| `cloudwatchRedis.bytesUsed15m` | Redis 메모리 사용량 (bytes) | maxmemory 한계(약 500MiB) 대비 비율 |
| `cloudwatchRedis.evictions15m` | Redis eviction 발생 건수 | > 0 이면 maxmemory-policy 점검 필요 |

## K. 출력 스키마 (엄격)

```json
{
  "summary": "한 줄 요약",
  "recommendations": [
    {
      "target": "hpa/read-api-hpa",
      "field": "spec.maxReplicas",
      "from": 23,
      "to": 10,
      "reason": "평시 1 replica, 피크 추정 2000 RPS 대비 10 replica 충분 + t3.small 용량 한계",
      "confidence": "high|medium|low",
      "priority": "now|watch|later",
      "risk": "none|low|medium|high"
    }
  ],
  "warnings": ["worker burst maxReplicaCount=39 는 t3.small×4 용량으로 스케줄 불가 가능성"],
  "estimatedCostDelta": {
    "direction": "increase|decrease|neutral",
    "approxUSDPerMonth": 0,
    "rationale": "노드 수 유지·replica 상한만 조정 → EC2 비용 동일"
  },
  "openQuestions": ["실측 p95 latency 수치 필요"]
}
```

## K. 답변 규칙

- 산문/잡담 금지. 위 스키마 외 필드 추가 금지.
- 수치는 반드시 근거(`reason`) 포함.
- 확신 없으면 `confidence: "low"` + `openQuestions`에 추가.
- **파괴적 변경은 `risk: "high"`로만 제안**, `priority: "watch"` 강제.
