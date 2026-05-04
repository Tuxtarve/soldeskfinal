================================================================================
  AWS + GCP 티켓팅 인프라 — 발표·면접·영상 촬영용 명령어 총정리
================================================================================

  클러스터  : ticketing-eks (ap-northeast-2)
  네임스페이스: ticketing / monitoring
  GCP 프로젝트: soldesk-gcp
  SQS 큐    : ticketing-reservation.fifo
  계정 ID   : 032098305878

  공통 사전 준비
  ──────────────
  export AWS_PAGER=""
  aws eks update-kubeconfig --region ap-northeast-2 --name ticketing-eks

================================================================================
  PART 1. AWS 메트릭 수집 → Prometheus → Grafana → Slack 알림
================================================================================

────────────────────────────────────────────────────────────────────────────────
  1-1. 클러스터 & Pod 상태 확인
────────────────────────────────────────────────────────────────────────────────

  # 노드 상태
  kubectl get nodes -o wide

  # 전체 Pod 상태 (ticketing)
  kubectl get pods -n ticketing

  # CPU / 메모리 실시간 사용량
  kubectl top nodes
  kubectl top pods -n ticketing

  # HPA 상태 (CPU 기반 오토스케일러)
  kubectl get hpa -n ticketing
    → read-api-hpa  : CPU 55% 임계값, 1~40 replicas
    → write-api-hpa : CPU 45% 임계값, 1~30 replicas

  # KEDA 상태 (SQS 기반 오토스케일러)
  kubectl get scaledobject -n ticketing
    → worker-svc-sqs : SQS 5개 이상 시 스케일, 0~46 replicas
    → READY=True / ACTIVE=False(대기) 가 정상

────────────────────────────────────────────────────────────────────────────────
  1-2. Prometheus — 메트릭 수집 확인
────────────────────────────────────────────────────────────────────────────────

  # Prometheus 포트 포워드
  kubectl port-forward -n monitoring \
    svc/kube-prometheus-stack-prometheus 9090:9090 &
  # 브라우저: http://localhost:9090

  # 등록된 알림 규칙 조회
  kubectl get prometheusrule -n monitoring

  # 현재 발화 중인 알림 확인
  kubectl get --raw \
    /api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/alerts \
    | python3 -m json.tool | grep -E "alertname|state|value"

  # 유용한 PromQL 쿼리 예시
  #   Pod CPU 사용률
  rate(container_cpu_usage_seconds_total{namespace="ticketing"}[5m]) * 100

  #   HPA 현재 replica 수
  kube_horizontalpodautoscaler_status_current_replicas{namespace="ticketing"}

  #   노드 CPU 사용률
  100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

────────────────────────────────────────────────────────────────────────────────
  1-3. Grafana — 대시보드 확인
────────────────────────────────────────────────────────────────────────────────

  # Grafana 포트 포워드
  kubectl port-forward -n monitoring \
    svc/kube-prometheus-stack-grafana 3000:80 &
  # 브라우저: http://localhost:3000
  # 계정: root / soldesk1.

  # Grafana Pod 상태 확인
  kubectl get pods -n monitoring | grep grafana

  # 등록된 대시보드 ConfigMap 목록
  kubectl get configmap -n monitoring -l grafana_dashboard=1

  # AlertManager 상태 확인
  kubectl port-forward -n monitoring \
    svc/kube-prometheus-stack-alertmanager 9093:9093 &
  # 브라우저: http://localhost:9093

────────────────────────────────────────────────────────────────────────────────
  1-4. Slack 알림 확인 및 테스트
────────────────────────────────────────────────────────────────────────────────

  # AlertManager에 등록된 Slack Webhook URL 확인
  kubectl get secret -n monitoring \
    alertmanager-kube-prometheus-stack-alertmanager \
    -o jsonpath='{.data.alertmanager\.yaml}' | base64 -d | grep api_url

  # Slack Webhook 직접 테스트 (메시지 즉시 전송)
  curl -X POST \
    -H "Content-Type: application/json" \
    -d '{"text": "🔔 [TEST] Slack 알림 연동 확인 — AWS+GCP 티켓팅 인프라"}' \
    $(kubectl get secret -n monitoring alertmanager-kube-prometheus-stack-alertmanager \
      -o jsonpath='{.data.alertmanager\.yaml}' | base64 -d | grep api_url | awk '{print $2}')

  # 등록된 알림 규칙 목록 (발화 조건 확인)
  kubectl describe prometheusrule ticketing-rules -n monitoring \
    | grep -E "Alert:|Summary:|Description:"

  # Slack 알림 종류 (부하 시 자동 발화)
  #   🚨 HPA Scale Out 발생      → read/write-api Pod 증설 시
  #   ⚡ KEDA Worker Scale Out   → SQS 메시지로 worker 증설 시
  #   📈 Pod 급증 감지            → 전체 Pod 6개 초과 시
  #   🔴 서비스 다운 감지         → Pod Health Check 실패 시
  #   ⚠️  RDS/Redis/노드 경고     → 임계값 초과 시

================================================================================
  PART 2. GCP — WIF (키 없는 크로스 클라우드 인증)
================================================================================

  WIF 인증 흐름
  ─────────────
  EKS Pod (IRSA JWT 토큰 자동 발급)
    → GCP Workload Identity Pool (AWS 자격증명 검증)
      → GCP Service Account 임시 토큰 발급 (1시간)
        → Cloud Logging / Gemini API 호출
  결과: 코드와 컨테이너에 비밀키(JSON) 없음

────────────────────────────────────────────────────────────────────────────────
  2-1. WIF 구성 요소 확인
────────────────────────────────────────────────────────────────────────────────

  # GCP 프로젝트 확인
  gcloud config get-value project
    → soldesk-gcp

  # Workload Identity Pool 상태
  gcloud iam workload-identity-pools describe eks-pool \
    --project=soldesk-gcp \
    --location=global \
    --format="value(state,displayName)"
    → ACTIVE 확인

  # AWS Provider 상태
  gcloud iam workload-identity-pools providers describe eks-provider \
    --project=soldesk-gcp \
    --location=global \
    --workload-identity-pool=eks-pool \
    --format="value(state,aws.accountId)"
    → ACTIVE + AWS 계정ID 확인

  # GCP Service Account 권한 확인
  gcloud iam service-accounts get-iam-policy \
    eks-ai-advisor@soldesk-gcp.iam.gserviceaccount.com \
    --project=soldesk-gcp \
    --format="yaml" | grep -A3 "principalSet"
    → principalSet://...eks-pool/attribute.aws_role/... 확인

────────────────────────────────────────────────────────────────────────────────
  2-2. EKS Pod에 WIF 자격증명 주입 확인
────────────────────────────────────────────────────────────────────────────────

  # ai-advisor 서비스어카운트 IRSA 어노테이션 확인
  kubectl describe sa ai-advisor-sa -n ticketing \
    | grep "eks.amazonaws.com/role-arn"
    → IRSA 역할 ARN 확인

  # Pod에 마운트된 WIF credential 파일 확인
  kubectl get configmap gcp-credential-config -n ticketing -o yaml \
    | grep -E "audience|token_url|service_account_impersonation"
    → eks-pool / eks-provider / eks-ai-advisor 3개 확인

  # 환경변수로 주입된 GCP 자격증명 경로 확인
  kubectl get cronjob ai-advisor -n ticketing -o yaml \
    | grep -A1 "GOOGLE_APPLICATION_CREDENTIALS"
    → value: /etc/gcp/config.json 확인

────────────────────────────────────────────────────────────────────────────────
  2-3. WIF 실제 인증 동작 확인 (GCP 로그 조회 성공 = 인증 성공)
────────────────────────────────────────────────────────────────────────────────

  # WIF 인증 경유 GCP Cloud Logging 조회
  gcloud logging read 'logName=~"eks-metrics"' \
    --project=soldesk-gcp \
    --limit=1 \
    --format="value(timestamp,jsonPayload.message)"
    → 출력되면 WIF 인증 성공 (키 파일 없이 인증됨)

================================================================================
  PART 3. CronJob + Gemini AI 추천 + 오토스케일링
================================================================================

────────────────────────────────────────────────────────────────────────────────
  3-1. AI Advisor CronJob 상태 확인
────────────────────────────────────────────────────────────────────────────────

  # CronJob 스케줄 확인 (10분마다 자동 실행)
  kubectl get cronjob ai-advisor -n ticketing
    → SCHEDULE: */10 * * * *

  # 최근 실행 이력
  kubectl get jobs -n ticketing \
    --sort-by=.metadata.creationTimestamp | grep ai-advisor | tail -5
    → COMPLETIONS 1/1 = 정상 완료

  # 최근 실행 로그 확인
  kubectl logs -n ticketing \
    -l job-name=$(kubectl get jobs -n ticketing \
      --sort-by=.metadata.creationTimestamp \
      -o jsonpath='{.items[-1].metadata.name}') 2>/dev/null

────────────────────────────────────────────────────────────────────────────────
  3-2. AI Advisor 즉시 수동 실행 (데모용)
────────────────────────────────────────────────────────────────────────────────

  # 수동 실행
  kubectl create job ai-demo-$(date +%s) \
    --from=cronjob/ai-advisor -n ticketing

  # 실시간 로그 출력 (약 30~60초 소요)
  kubectl logs -n ticketing \
    -l job-name=$(kubectl get jobs -n ticketing \
      --sort-by=.metadata.creationTimestamp \
      -o jsonpath='{.items[-1].metadata.name}') \
    -f

  # 로그에서 확인할 항목
  #   [*] GCP 클라이언트 초기화 완료 (WIF via IRSA)   ← WIF 인증 성공
  #   [*] Gemini 추천 요청...                          ← Gemini 호출 시작
  #   [+] 추천 수신: NOW 19건                          ← AI 분석 완료
  #   [PATCH] hpa/write-api-hpa/... 20 → 30           ← 실제 K8s 패치
  #   [+] 자동 패치 완료: 1건 (실제 적용)             ← 적용 확인

────────────────────────────────────────────────────────────────────────────────
  3-3. Gemini AI 추천 결과 조회 (GCP Cloud Logging)
────────────────────────────────────────────────────────────────────────────────

  # 최신 AI 추천 결과 전체 조회
  gcloud logging read 'logName=~"gemini-recommendations"' \
    --project=soldesk-gcp \
    --limit=1 \
    --format=json | python3 -c "
import json, sys
logs = json.load(sys.stdin)
entry = logs[0]
p = entry.get('jsonPayload', {})
print('=' * 55)
print('  Gemini AI 분석 결과')
print('=' * 55)
print('분석 시각:', entry.get('timestamp','')[:19].replace('T',' '))
print()
print('[AI 요약]')
print(p.get('summary','')[:200])
print()
print('[추천 목록]', len(p.get('recommendations',[])), '건')
print('-' * 55)
for r in p.get('recommendations', []):
    print(f\"[{r['priority'].upper()}][위험:{r['risk']}] {r['target']}\")
    print(f\"  항목  : {r['field']}\")
    print(f\"  변경  : {r['from']} → {r['to']}\")
    print(f\"  이유  : {r['reason'][:80]}\")
    print()
"

  # 최근 3회 분석 시각만 빠르게 확인
  gcloud logging read 'logName=~"gemini-recommendations"' \
    --project=soldesk-gcp \
    --limit=3 \
    --format="value(timestamp)"

────────────────────────────────────────────────────────────────────────────────
  3-4. AI가 실제로 패치한 이력 조회
────────────────────────────────────────────────────────────────────────────────

  # AI 패치 적용 이력
  gcloud logging read 'logName=~"ai-auto-patches"' \
    --project=soldesk-gcp \
    --limit=3 \
    --format=json | python3 -c "
import json, sys
logs = json.load(sys.stdin)
for entry in logs:
    p = entry.get('jsonPayload', {})
    print('패치 시각:', entry.get('timestamp','')[:19].replace('T',' '))
    for patch in p.get('patches', []):
        print(f\"  [PATCH] {patch.get('target')} / {patch.get('field')}\")
        print(f\"          {patch.get('from')} → {patch.get('to')}\")
    print()
"

  # AI 패치 후 HPA 실제 값 변화 확인
  kubectl describe hpa write-api-hpa -n ticketing \
    | grep -E "Max Replicas|Current Replicas|Desired Replicas"

================================================================================
  PART 4. 실제 부하 발생 → 오토스케일링 전체 시연
================================================================================

────────────────────────────────────────────────────────────────────────────────
  4-0. 데모 초기화 (부하 테스트 전 반드시 실행)
────────────────────────────────────────────────────────────────────────────────

  # HPA scaleDown 대기시간 0으로 → 즉시 스케일 다운
  kubectl patch hpa read-api-hpa -n ticketing --type=merge \
    -p '{"spec":{"behavior":{"scaleDown":{"stabilizationWindowSeconds":0}}}}'
  kubectl patch hpa write-api-hpa -n ticketing --type=merge \
    -p '{"spec":{"behavior":{"scaleDown":{"stabilizationWindowSeconds":0}}}}'

  # 잠시 후 확인 (burst Pod 1대로 내려와야 함)
  kubectl get pods -n ticketing --no-headers | grep -v Completed

  # 초기화 완료 후 대기시간 원상복구
  kubectl patch hpa read-api-hpa -n ticketing --type=merge \
    -p '{"spec":{"behavior":{"scaleDown":{"stabilizationWindowSeconds":300}}}}'
  kubectl patch hpa write-api-hpa -n ticketing --type=merge \
    -p '{"spec":{"behavior":{"scaleDown":{"stabilizationWindowSeconds":300}}}}'

────────────────────────────────────────────────────────────────────────────────
  4-1. 실시간 감시 터미널 (촬영 내내 켜두기)
────────────────────────────────────────────────────────────────────────────────

  watch -n 2 '
  echo "━━━ PODS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  kubectl get pods -n ticketing --no-headers | grep Running | grep -v Completed
  echo ""
  echo "━━━ HPA (CPU 기반 오토스케일러) ━━━━━━━"
  kubectl get hpa -n ticketing
  echo ""
  echo "━━━ KEDA (SQS 기반 오토스케일러) ━━━━━━"
  kubectl get scaledobject -n ticketing
  '

────────────────────────────────────────────────────────────────────────────────
  4-2. PHASE 1 — KEDA 스케일 아웃 (SQS → worker 0 → N대)
────────────────────────────────────────────────────────────────────────────────

  # SQS 50개 주입 → worker-svc-burst 즉시 반응 (5개당 1대)
  for i in $(seq 1 50); do
    aws sqs send-message \
      --queue-url "https://sqs.ap-northeast-2.amazonaws.com/032098305878/ticketing-reservation.fifo" \
      --message-body "{\"concert_id\":1,\"user_id\":$i,\"seat_row\":$((i%100)),\"seat_col\":$i}" \
      --message-group-id "demo-$((i % 10))" \
      --message-deduplication-id "phase1-$(date +%s)-$i" \
      --output text > /dev/null
    echo -n "."
  done
  echo " ✅ SQS 50개 전송 완료"

  # SQS 현재 메시지 수 확인
  aws sqs get-queue-attributes \
    --queue-url "https://sqs.ap-northeast-2.amazonaws.com/032098305878/ticketing-reservation.fifo" \
    --attribute-names ApproximateNumberOfMessages \
    --query "Attributes.ApproximateNumberOfMessages"

  # 예상 결과: worker-svc-burst 0 → 5 → 10대 (약 30초 내)

────────────────────────────────────────────────────────────────────────────────
  4-3. PHASE 2 — read-api HPA 스케일 아웃 (CPU → 1 → 4대)
────────────────────────────────────────────────────────────────────────────────

  # 클러스터 내부에서 read-api 직접 타격 (ALB internal 우회)
  for j in 1 2 3; do
    kubectl run read-load-$j -n ticketing \
      --image=alpine/curl \
      --restart=Never \
      --labels="demo=load" \
      -- sh -c "while true; do curl -s http://read-api:5000/concerts -o /dev/null; done" &
  done
  echo "✅ read-load 파드 3개 기동 (CPU 55% 초과 시 HPA 반응)"

  # HPA 반응 확인 (cpu: 3%/55% → 60%+ 로 올라감)
  kubectl get hpa read-api-hpa -n ticketing

  # 예상 결과: read-api-burst 1 → 3 → 4대 (약 60초 내)

────────────────────────────────────────────────────────────────────────────────
  4-4. PHASE 3 — write-api HPA 스케일 아웃 (CPU → 1 → 3대)
────────────────────────────────────────────────────────────────────────────────

  for j in 1 2; do
    kubectl run write-load-$j -n ticketing \
      --image=alpine/curl \
      --restart=Never \
      --labels="demo=load" \
      -- sh -c "while true; do curl -s http://write-api:5001/health -o /dev/null; done" &
  done
  echo "✅ write-load 파드 2개 기동 (CPU 45% 초과 시 HPA 반응)"

  # 예상 결과: write-api-burst 1 → 2 → 3대 (약 60초 내)

────────────────────────────────────────────────────────────────────────────────
  4-5. PHASE 4 — AI Advisor 수동 실행 (Gemini 분석 + 자동 패치)
────────────────────────────────────────────────────────────────────────────────

  # 부하 상태에서 AI 분석 실행 (가장 극적인 추천 결과 나옴)
  kubectl create job ai-load-demo-$(date +%s) \
    --from=cronjob/ai-advisor -n ticketing

  # 로그 실시간 출력
  sleep 5 && kubectl logs -n ticketing \
    -l job-name=$(kubectl get jobs -n ticketing \
      --sort-by=.metadata.creationTimestamp \
      -o jsonpath='{.items[-1].metadata.name}') -f

────────────────────────────────────────────────────────────────────────────────
  4-6. 스케일 아웃 이벤트 확인
────────────────────────────────────────────────────────────────────────────────

  # HPA 스케일 이벤트 로그
  kubectl describe hpa read-api-hpa -n ticketing | tail -15
  kubectl describe hpa write-api-hpa -n ticketing | tail -15

  # KEDA 스케일 이벤트
  kubectl describe scaledobject worker-svc-sqs -n ticketing | tail -20

  # 전체 이벤트 (스케일 관련)
  kubectl get events -n ticketing \
    --sort-by=.lastTimestamp \
    | grep -iE "scale|replica|keda" | tail -10

  # Slack 알림 수신 확인 (#alerts 채널)
  #   🚨 HPA Scale Out 발생     → 서비스명, Pod 수, 네임스페이스
  #   ⚡ KEDA Worker Scale Out  → worker Pod 수, SQS 트리거
  #   📈 Pod 급증 감지           → 전체 Running Pod 수

────────────────────────────────────────────────────────────────────────────────
  4-7. 데모 종료 후 정리
────────────────────────────────────────────────────────────────────────────────

  # 부하 파드 전체 삭제
  kubectl delete pods -n ticketing -l demo=load

  # SQS 큐 비우기
  aws sqs purge-queue \
    --queue-url "https://sqs.ap-northeast-2.amazonaws.com/032098305878/ticketing-reservation.fifo"

  # 완료 잡 정리
  kubectl delete jobs -n ticketing \
    $(kubectl get jobs -n ticketing --no-headers \
      | grep -E "Complete|Failed" | awk '{print $1}') 2>/dev/null

================================================================================
  발표 전 5분 체크리스트
================================================================================

  □ kubectl get nodes                        → Ready 노드 4~5대
  □ kubectl get pods -n ticketing            → 모두 Running
  □ kubectl get hpa -n ticketing             → read/write-api-hpa READY
  □ kubectl get scaledobject -n ticketing    → READY=True
  □ gcloud iam workload-identity-pools describe eks-pool ... → ACTIVE
  □ gcloud logging read 'logName~"gemini-recommendations"'  → 최근 로그 존재
  □ kubectl get cronjob ai-advisor -n ticketing → SCHEDULE: */10 * * * *
  □ curl [SLACK_WEBHOOK_URL] → "ok" 응답 + Slack 메시지 수신
  □ kubectl port-forward grafana 3000:80     → http://localhost:3000 접속

================================================================================
