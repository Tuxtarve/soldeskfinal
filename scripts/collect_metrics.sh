#!/usr/bin/env bash
# EKS + SQS 상태를 하나의 JSON 스냅샷으로 모은다.
# Gemini 에게 오토스케일 추천을 요청할 때 입력으로 쓸 압축 요약.
#
# 사용:
#   scripts/collect_metrics.sh                 # ./scripts/data/metrics-YYYYmmdd-HHMMSS.json 생성
#   scripts/collect_metrics.sh /tmp/out.json   # 출력 경로 지정
#
# 필요: kubectl, aws, jq
set -euo pipefail

NS="${NS:-ticketing}"
QUEUE_NAME="${QUEUE_NAME:-ticketing-reservation.fifo}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-northeast-2)}"

OUT="${1:-}"
if [[ -z "$OUT" ]]; then
  DATA_DIR="$(dirname "$0")/data"
  mkdir -p "$DATA_DIR"
  OUT="$DATA_DIR/metrics-$(date +%Y%m%d-%H%M%S).json"
fi

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
need kubectl; need aws; need jq

echo "[*] namespace=$NS queue=$QUEUE_NAME region=$REGION" >&2

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

# ---------- deployments (read-api, write-api, worker-svc; burst 변종 포함) ----------
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

# ---------- assemble ----------
jq -n \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg ns "$NS" \
  --arg region "$REGION" \
  --argjson nodes "$NODES_JSON" \
  --argjson nodeTop "$NODE_TOP" \
  --argjson deployments "$DEPLOYS_JSON" \
  --argjson podTop "$POD_TOP" \
  --argjson hpa "$HPA_JSON" \
  --argjson scaledObjects "$SO_JSON" \
  --argjson sqs "$SQS_JSON" \
  --argjson events "$EVENTS_JSON" \
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
    events: $events
  }' > "$OUT"

echo "[+] wrote $OUT ($(wc -c <"$OUT") bytes)"
