#!/usr/bin/env bash
# EKS + SQS + Prometheus + CloudWatch 상태를 하나의 JSON 스냅샷으로 모은다.
# Gemini 에게 오토스케일 추천을 요청할 때 입력으로 쓸 압축 요약.
#
# 사용:
#   scripts/collect_metrics.sh                 # ./scripts/data/metrics-YYYYmmdd-HHMMSS.json 생성
#   scripts/collect_metrics.sh /tmp/out.json   # 출력 경로 지정
#
# 필요: kubectl, aws, jq, python3
set -euo pipefail

NS="${NS:-ticketing}"
QUEUE_NAME="${QUEUE_NAME:-ticketing-reservation.fifo}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-northeast-2)}"

# Prometheus (kube-prometheus-stack)
PROM_SVC="${PROM_SVC:-kube-prometheus-stack-prometheus}"
PROM_NS="${PROM_NS:-monitoring}"
PROM_LOCAL_PORT=19090

# CloudWatch 리소스 ID (terraform 고정값)
RDS_INSTANCE_ID="prod-ticketing-writer"
REDIS_GROUP_ID="ticketing-redis"

OUT="${1:-}"
if [[ -z "$OUT" ]]; then
  DATA_DIR="$(dirname "$0")/data"
  mkdir -p "$DATA_DIR"
  OUT="$DATA_DIR/metrics-$(date +%Y%m%d-%H%M%S).json"
fi

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
need kubectl; need aws; need jq; need python3

echo "[*] namespace=$NS queue=$QUEUE_NAME region=$REGION" >&2

# macOS(BSD) / Linux(GNU) date 호환 — N분 전 ISO8601
_ago_iso() {
  local mins=$1
  if date --version >/dev/null 2>&1; then
    date -u -d "$mins minutes ago" +%Y-%m-%dT%H:%M:%SZ
  else
    date -u -v "-${mins}M" +%Y-%m-%dT%H:%M:%SZ
  fi
}

# ---------- nodes ----------
NODES_JSON=$(kubectl get nodes -o json | jq '[
  .items[] | {
    name: .metadata.name,
    instanceType: (.metadata.labels["node.kubernetes.io/instance-type"] // ""),
    capacity: {cpu: .status.capacity.cpu, memory: .status.capacity.memory},
    allocatable: {cpu: .status.allocatable.cpu, memory: .status.allocatable.memory},
    conditions: [.status.conditions[] | select(.type=="Ready") | {status, reason}]
  }
]')

NODE_TOP=$(kubectl top nodes --no-headers 2>/dev/null | awk '{print "{\"name\":\""$1"\",\"cpu\":\""$2"\",\"cpuPct\":\""$3"\",\"mem\":\""$4"\",\"memPct\":\""$5"\"}"}' | jq -s '.' || echo '[]')

# ---------- deployments ----------
DEPLOYS_JSON=$(kubectl get deploy -n "$NS" -o json | jq '[
  .items[] | {
    name: .metadata.name,
    replicas: .status.replicas,
    available: .status.availableReplicas,
    desired: .spec.replicas,
    containers: [.spec.template.spec.containers[] | {
      name: .name,
      resources: .resources
    }]
  }
]')

POD_TOP=$(kubectl top pods -n "$NS" --no-headers 2>/dev/null | awk '{print "{\"pod\":\""$1"\",\"cpu\":\""$2"\",\"mem\":\""$3"\"}"}' | jq -s '.' || echo '[]')

# ---------- HPA ----------
HPA_JSON=$(kubectl get hpa -n "$NS" -o json | jq '[
  .items[] | {
    name: .metadata.name,
    target: (.spec.scaleTargetRef.kind + "/" + .spec.scaleTargetRef.name),
    minReplicas: .spec.minReplicas,
    maxReplicas: .spec.maxReplicas,
    currentReplicas: .status.currentReplicas,
    desiredReplicas: .status.desiredReplicas,
    metrics: .spec.metrics,
    currentMetrics: .status.currentMetrics
  }
]')

# ---------- KEDA ScaledObject ----------
SO_JSON=$(kubectl get scaledobject -n "$NS" -o json 2>/dev/null | jq '[
  .items[] | {
    name: .metadata.name,
    target: .spec.scaleTargetRef.name,
    minReplicaCount: .spec.minReplicaCount,
    maxReplicaCount: .spec.maxReplicaCount,
    triggers: .spec.triggers,
    status: {
      ready: (.status.conditions[]? | select(.type=="Ready") | .status),
      active: (.status.conditions[]? | select(.type=="Active") | .status)
    }
  }
]' || echo '[]')

# ---------- SQS depth ----------
QUEUE_URL=$(aws sqs get-queue-url --queue-name "$QUEUE_NAME" --region "$REGION" --query QueueUrl --output text 2>/dev/null || echo "")
SQS_JSON='{}'
if [[ -n "$QUEUE_URL" ]]; then
  SQS_JSON=$(aws sqs get-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed \
    --region "$REGION" \
    --query 'Attributes' --output json 2>/dev/null || echo '{}')
fi

# ---------- recent scaling events ----------
EVENTS_JSON=$(kubectl get events -n "$NS" --sort-by=.lastTimestamp -o json 2>/dev/null | jq '[
  .items[] | select(.reason | test("Scal|Scheduled|FailedScheduling|Evicted"))
  | {time: .lastTimestamp, reason, object: (.involvedObject.kind + "/" + .involvedObject.name), message}
] | .[-20:]' || echo '[]')

# ---------- prometheus (시계열 트렌드) ----------
echo "[*] Prometheus 포트포워드 시도..." >&2
_prom_up=false
kubectl port-forward -n "$PROM_NS" svc/"$PROM_SVC" "${PROM_LOCAL_PORT}:9090" \
  >/dev/null 2>&1 &
PROM_PID=$!
sleep 3

if curl -sf "http://localhost:${PROM_LOCAL_PORT}/-/healthy" >/dev/null 2>&1; then
  _prom_up=true
  echo "[*] Prometheus 연결 OK (pid=$PROM_PID)" >&2
else
  echo "[!] Prometheus 연결 실패 — Prometheus 메트릭 생략" >&2
  kill "$PROM_PID" 2>/dev/null || true
fi

# URL 인코딩 헬퍼
_prom_encode() {
  python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$1"
}

# 즉시 쿼리 (현재 시각 기준)
prom_query() {
  local q="$1"
  curl -sf "http://localhost:${PROM_LOCAL_PORT}/api/v1/query?query=$(_prom_encode "$q")" 2>/dev/null \
    | jq '[.data.result[] | {metric: .metric, value: (.value[1] | tonumber? // .value[1])}]' 2>/dev/null \
    || echo '[]'
}

# 범위 쿼리 (최근 15분, 1분 간격)
prom_query_range() {
  local q="$1"
  local now; now=$(date -u +%s)
  local ago=$((now - 900))
  curl -sf "http://localhost:${PROM_LOCAL_PORT}/api/v1/query_range?query=$(_prom_encode "$q")&start=${ago}&end=${now}&step=60" 2>/dev/null \
    | jq '.data.result' 2>/dev/null \
    || echo '[]'
}

PROM_JSON='{}'
if $_prom_up; then
  # 컨테이너별 CPU 사용률 (5분 이동평균)
  CPU_RATE=$(prom_query \
    'sum by (pod, container) (rate(container_cpu_usage_seconds_total{namespace="'"$NS"'",container!="",container!="POD"}[5m]))')

  # 컨테이너별 메모리 실사용량 (working set)
  MEM_BYTES=$(prom_query \
    'sum by (pod, container) (container_memory_working_set_bytes{namespace="'"$NS"'",container!="",container!="POD"})')

  # Pod 재시작 횟수 (OOMKill 등 불안정 신호)
  POD_RESTARTS=$(prom_query \
    'sum by (pod, container) (kube_pod_container_status_restarts_total{namespace="'"$NS"'"})')

  # HPA desired replicas 최근 15분 추이
  HPA_DESIRED_TREND=$(prom_query_range \
    'kube_horizontalpodautoscaler_status_desired_replicas{namespace="'"$NS"'"}')

  # HPA 현재 replica 수
  HPA_CURRENT_REPLICAS=$(prom_query \
    'kube_horizontalpodautoscaler_status_current_replicas{namespace="'"$NS"'"}')

  # 노드 메모리 압박 여부
  NODE_MEM_PRESSURE=$(prom_query \
    'kube_node_status_condition{condition="MemoryPressure",status="true"}')

  # 노드 CPU 압박 여부
  NODE_CPU_PRESSURE=$(prom_query \
    'kube_node_status_condition{condition="DiskPressure",status="true"}')

  # HTTP 요청 RPS (앱이 http_requests_total 노출 시 — 없으면 빈 배열)
  HTTP_RPS=$(prom_query \
    'sum by (pod) (rate(http_requests_total{namespace="'"$NS"'"}[5m]))' 2>/dev/null || echo '[]')

  PROM_JSON=$(jq -n \
    --argjson cpuRate        "$CPU_RATE" \
    --argjson memBytes       "$MEM_BYTES" \
    --argjson podRestarts    "$POD_RESTARTS" \
    --argjson hpaDesiredTrend "$HPA_DESIRED_TREND" \
    --argjson hpaCurrent     "$HPA_CURRENT_REPLICAS" \
    --argjson nodePressure   "$NODE_MEM_PRESSURE" \
    --argjson diskPressure   "$NODE_CPU_PRESSURE" \
    --argjson httpRps        "$HTTP_RPS" \
    '{
      cpuRatePerContainer:   $cpuRate,
      memBytesPerContainer:  $memBytes,
      podRestarts:           $podRestarts,
      hpaDesiredTrend15m:    $hpaDesiredTrend,
      hpaCurrentReplicas:    $hpaCurrent,
      nodeMemoryPressure:    $nodePressure,
      nodeDiskPressure:      $diskPressure,
      httpRpsPerPod:         $httpRps
    }')

  kill "$PROM_PID" 2>/dev/null || true
  wait "$PROM_PID" 2>/dev/null || true
  echo "[*] Prometheus 수집 완료" >&2
fi

# ---------- CloudWatch: RDS 커넥션 ----------
echo "[*] CloudWatch RDS 메트릭 수집..." >&2
START_TIME=$(_ago_iso 15)
END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)

RDS_CW=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name DatabaseConnections \
  --dimensions Name=DBInstanceIdentifier,Value="$RDS_INSTANCE_ID" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period 300 \
  --statistics Average Maximum \
  --region "$REGION" \
  --query 'sort_by(Datapoints, &Timestamp)' \
  --output json 2>/dev/null || echo '[]')

RDS_CPU_CW=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name CPUUtilization \
  --dimensions Name=DBInstanceIdentifier,Value="$RDS_INSTANCE_ID" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period 300 \
  --statistics Average \
  --region "$REGION" \
  --query 'sort_by(Datapoints, &Timestamp)' \
  --output json 2>/dev/null || echo '[]')

RDS_JSON=$(jq -n \
  --argjson connections "$RDS_CW" \
  --argjson cpu         "$RDS_CPU_CW" \
  --arg instanceId      "$RDS_INSTANCE_ID" \
  '{instanceId: $instanceId, connections15m: $connections, cpu15m: $cpu}')

# ---------- CloudWatch: ElastiCache Redis ----------
echo "[*] CloudWatch Redis 메트릭 수집..." >&2

REDIS_CONN_CW=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/ElastiCache \
  --metric-name CurrConnections \
  --dimensions Name=ReplicationGroupId,Value="$REDIS_GROUP_ID" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period 300 \
  --statistics Average Maximum \
  --region "$REGION" \
  --query 'sort_by(Datapoints, &Timestamp)' \
  --output json 2>/dev/null || echo '[]')

REDIS_MEM_CW=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/ElastiCache \
  --metric-name BytesUsedForCache \
  --dimensions Name=ReplicationGroupId,Value="$REDIS_GROUP_ID" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period 300 \
  --statistics Average \
  --region "$REGION" \
  --query 'sort_by(Datapoints, &Timestamp)' \
  --output json 2>/dev/null || echo '[]')

REDIS_EVICT_CW=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/ElastiCache \
  --metric-name Evictions \
  --dimensions Name=ReplicationGroupId,Value="$REDIS_GROUP_ID" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period 300 \
  --statistics Sum \
  --region "$REGION" \
  --query 'sort_by(Datapoints, &Timestamp)' \
  --output json 2>/dev/null || echo '[]')

REDIS_JSON=$(jq -n \
  --argjson connections "$REDIS_CONN_CW" \
  --argjson memory      "$REDIS_MEM_CW" \
  --argjson evictions   "$REDIS_EVICT_CW" \
  --arg groupId         "$REDIS_GROUP_ID" \
  '{groupId: $groupId, connections15m: $connections, bytesUsed15m: $memory, evictions15m: $evictions}')

# ---------- assemble ----------
jq -n \
  --arg ts          "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg ns          "$NS" \
  --arg region      "$REGION" \
  --argjson nodes        "$NODES_JSON" \
  --argjson nodeTop      "$NODE_TOP" \
  --argjson deployments  "$DEPLOYS_JSON" \
  --argjson podTop       "$POD_TOP" \
  --argjson hpa          "$HPA_JSON" \
  --argjson scaledObjects "$SO_JSON" \
  --argjson sqs          "$SQS_JSON" \
  --argjson events       "$EVENTS_JSON" \
  --argjson prometheus   "$PROM_JSON" \
  --argjson cloudwatchRds   "$RDS_JSON" \
  --argjson cloudwatchRedis "$REDIS_JSON" \
  '{
    timestamp: $ts,
    namespace: $ns,
    region: $region,
    nodes: $nodes,
    nodeTop: $nodeTop,
    deployments: $deployments,
    podTop: $podTop,
    hpa: $hpa,
    scaledObjects: $scaledObjects,
    sqs: $sqs,
    events: $events,
    prometheus: $prometheus,
    cloudwatchRds: $cloudwatchRds,
    cloudwatchRedis: $cloudwatchRedis
  }' > "$OUT"

echo "[+] wrote $OUT ($(wc -c <"$OUT") bytes)"
