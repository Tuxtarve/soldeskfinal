#!/usr/bin/env python3
"""AI Advisor — EKS CronJob 내부 실행 스크립트.

로컬 스크립트(collect_metrics.py 등)와 동일한 파이프라인을 클러스터 내부에서 수행한다.
- Prometheus: 클러스터 내부 서비스 DNS 직접 쿼리 (port-forward 불필요)
- CloudWatch/SQS: boto3 + IRSA (자격증명 자동)
- GCP Cloud Logging: google-cloud-logging SDK + Workload Identity Federation
  (GOOGLE_APPLICATION_CREDENTIALS → gcp-credential-config.json)
- Gemini: google-genai SDK + GEMINI_API_KEY Secret

환경변수:
  GEMINI_API_KEY         (필수, K8s Secret)
  GEMINI_MODEL           (기본: gemini-2.5-flash)
  GCP_PROJECT_ID         (기본: soldesk-gcp)
  SLACK_WEBHOOK_URL      (선택)
  NS                     (기본: ticketing)
  QUEUE_NAME             (기본: ticketing-reservation.fifo)
  AWS_REGION             (기본: ap-northeast-2)
  PROMETHEUS_URL         (기본: http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------- 패키지 임포트 ----------
try:
    import boto3
    import requests
    from google.cloud import logging as gcp_logging
    from google import genai
    from google.genai import types
    import kubernetes
    import yaml
except ImportError as e:
    sys.stderr.write(f"패키지 누락: {e}\n  pip install -r requirements.txt\n")
    sys.exit(2)

# ---------- 설정 ----------
NS           = os.environ.get("NS",         "ticketing")
QUEUE_NAME   = os.environ.get("QUEUE_NAME", "ticketing-reservation.fifo")
REGION       = os.environ.get("AWS_REGION", "ap-northeast-2")
PROJECT_ID   = os.environ.get("GCP_PROJECT_ID", "soldesk-gcp")
MODEL        = os.environ.get("GEMINI_MODEL",   "gemini-2.5-flash")
PROM_URL     = os.environ.get("PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

CONTEXT_FILE = Path(__file__).parent / "context" / "system.md"


# ============================================================
# 1. 메트릭 수집
# ============================================================

def _k8s_client():
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
    return kubernetes.client


def collect_k8s(ns: str) -> dict:
    k = _k8s_client()
    core   = k.CoreV1Api()
    apps   = k.AppsV1Api()
    hpa_api = k.AutoscalingV2Api()
    custom = k.CustomObjectsApi()

    def _nodes():
        items = core.list_node().items
        return [
            {
                "name": n.metadata.name,
                "instanceType": (n.metadata.labels or {}).get(
                    "node.kubernetes.io/instance-type", ""),
                "capacity":    {"cpu": n.status.capacity.get("cpu"),
                                "memory": n.status.capacity.get("memory")},
                "allocatable": {"cpu": n.status.allocatable.get("cpu"),
                                "memory": n.status.allocatable.get("memory")},
            }
            for n in items
        ]

    def _node_top():
        try:
            raw = custom.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes")
            return [
                {"name": i["metadata"]["name"],
                 "cpu":  i["usage"]["cpu"],
                 "mem":  i["usage"]["memory"]}
                for i in raw.get("items", [])
            ]
        except Exception:
            return []

    def _deployments():
        items = apps.list_namespaced_deployment(ns).items
        return [
            {
                "name":      d.metadata.name,
                "desired":   d.spec.replicas,
                "available": d.status.available_replicas,
                "replicas":  d.status.replicas,
                "containers": [
                    {"name": c.name,
                     "resources": {
                         "requests": {
                             "cpu":    c.resources.requests.get("cpu")    if c.resources.requests else None,
                             "memory": c.resources.requests.get("memory") if c.resources.requests else None,
                         } if c.resources else {},
                         "limits": {
                             "cpu":    c.resources.limits.get("cpu")    if c.resources.limits else None,
                             "memory": c.resources.limits.get("memory") if c.resources.limits else None,
                         } if c.resources else {},
                     }}
                    for c in (d.spec.template.spec.containers or [])
                ],
            }
            for d in items
        ]

    def _pod_top():
        try:
            raw = custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", ns, "pods")
            return [
                {"pod": p["metadata"]["name"],
                 "cpu": p["containers"][0]["usage"]["cpu"] if p.get("containers") else None,
                 "mem": p["containers"][0]["usage"]["memory"] if p.get("containers") else None}
                for p in raw.get("items", [])
            ]
        except Exception:
            return []

    def _hpa():
        items = hpa_api.list_namespaced_horizontal_pod_autoscaler(ns).items
        result = []
        for h in items:
            ref = h.spec.scale_target_ref
            result.append({
                "name":            h.metadata.name,
                "target":          f"{ref.kind}/{ref.name}",
                "minReplicas":     h.spec.min_replicas,
                "maxReplicas":     h.spec.max_replicas,
                "currentReplicas": h.status.current_replicas,
                "desiredReplicas": h.status.desired_replicas,
                "currentMetrics":  [m.to_dict() for m in (h.status.current_metrics or [])],
            })
        return result

    def _scaled_objects():
        try:
            raw = custom.list_namespaced_custom_object(
                "keda.sh", "v1alpha1", ns, "scaledobjects")
            result = []
            for item in raw.get("items", []):
                conds = item.get("status", {}).get("conditions", [])
                result.append({
                    "name":            item["metadata"]["name"],
                    "target":          item["spec"].get("scaleTargetRef", {}).get("name"),
                    "minReplicaCount": item["spec"].get("minReplicaCount"),
                    "maxReplicaCount": item["spec"].get("maxReplicaCount"),
                    "triggers":        item["spec"].get("triggers", []),
                    "status": {
                        "ready":  next((c["status"] for c in conds if c["type"] == "Ready"),  None),
                        "active": next((c["status"] for c in conds if c["type"] == "Active"), None),
                    },
                })
            return result
        except Exception:
            return []

    def _events():
        keywords = ("Scal", "Scheduled", "FailedScheduling", "Evicted")
        items = core.list_namespaced_event(ns).items
        filtered = [
            {
                "time":    str(e.last_timestamp),
                "reason":  e.reason,
                "object":  f"{e.involved_object.kind}/{e.involved_object.name}",
                "message": e.message,
            }
            for e in items
            if e.reason and any(k in e.reason for k in keywords)
        ]
        return filtered[-20:]

    return {
        "nodes":       _nodes(),
        "nodeTop":     _node_top(),
        "deployments": _deployments(),
        "podTop":      _pod_top(),
        "hpa":         _hpa(),
        "scaledObjects": _scaled_objects(),
        "events":      _events(),
    }


def _prom(query: str) -> list:
    try:
        resp = requests.get(f"{PROM_URL}/api/v1/query",
                            params={"query": query}, timeout=10)
        results = resp.json().get("data", {}).get("result", [])
        return [{"metric": r["metric"],
                 "value": _num(r["value"][1])} for r in results]
    except Exception:
        return []


def _prom_range(query: str, minutes: int = 15) -> list:
    now = int(time.time())
    try:
        resp = requests.get(f"{PROM_URL}/api/v1/query_range",
                            params={"query": query,
                                    "start": now - minutes * 60,
                                    "end": now, "step": 60},
                            timeout=15)
        return resp.json().get("data", {}).get("result", [])
    except Exception:
        return []


def _num(v: str):
    try:
        return int(v) if "." not in v else float(v)
    except Exception:
        return v


def collect_prometheus(ns: str) -> dict:
    print("[*] Prometheus 수집 중...", flush=True)
    return {
        "cpuRatePerContainer": _prom(
            f'sum by(pod,container)(rate(container_cpu_usage_seconds_total'
            f'{{namespace="{ns}",container!="",container!="POD"}}[5m]))'),
        "memBytesPerContainer": _prom(
            f'sum by(pod,container)(container_memory_working_set_bytes'
            f'{{namespace="{ns}",container!="",container!="POD"}})'),
        "podRestarts": _prom(
            f'sum by(pod,container)(kube_pod_container_status_restarts_total'
            f'{{namespace="{ns}"}})'),
        "hpaDesiredTrend15m": _prom_range(
            f'kube_horizontalpodautoscaler_status_desired_replicas{{namespace="{ns}"}}'),
        "hpaCurrentReplicas": _prom(
            f'kube_horizontalpodautoscaler_status_current_replicas{{namespace="{ns}"}}'),
        "nodeMemoryPressure": _prom(
            'kube_node_status_condition{condition="MemoryPressure",status="true"}'),
        "httpRpsPerPod": _prom(
            f'sum by(pod)(rate(http_requests_total{{namespace="{ns}"}}[5m]))'),
    }


def _cw_stats(cw, namespace, metric, dims, stats, minutes=15) -> list:
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(now.timestamp() - minutes * 60, tz=timezone.utc)
    try:
        resp = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric, Dimensions=dims,
            StartTime=start.isoformat(), EndTime=now.isoformat(),
            Period=300, Statistics=stats)
        return sorted(resp.get("Datapoints", []),
                      key=lambda x: str(x.get("Timestamp", "")))
    except Exception as e:
        print(f"[!] CloudWatch {metric}: {e}", flush=True)
        return []


def collect_cloudwatch(region: str) -> tuple[dict, dict]:
    print("[*] CloudWatch 수집 중...", flush=True)
    cw = boto3.client("cloudwatch", region_name=region)
    rds_dims   = [{"Name": "DBInstanceIdentifier", "Value": "prod-ticketing-writer"}]
    redis_dims = [{"Name": "ReplicationGroupId",   "Value": "ticketing-redis"}]
    return (
        {
            "instanceId":    "prod-ticketing-writer",
            "connections15m": _cw_stats(cw, "AWS/RDS", "DatabaseConnections",
                                        rds_dims, ["Average", "Maximum"]),
            "cpu15m":         _cw_stats(cw, "AWS/RDS", "CPUUtilization",
                                        rds_dims, ["Average"]),
        },
        {
            "groupId":        "ticketing-redis",
            "connections15m": _cw_stats(cw, "AWS/ElastiCache", "CurrConnections",
                                        redis_dims, ["Average", "Maximum"]),
            "bytesUsed15m":   _cw_stats(cw, "AWS/ElastiCache", "BytesUsedForCache",
                                        redis_dims, ["Average"]),
            "evictions15m":   _cw_stats(cw, "AWS/ElastiCache", "Evictions",
                                        redis_dims, ["Sum"]),
        },
    )


def collect_sqs(queue_name: str, region: str) -> dict:
    try:
        client = boto3.client("sqs", region_name=region)
        url = client.get_queue_url(QueueName=queue_name)["QueueUrl"]
        return client.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        )["Attributes"]
    except Exception as e:
        print(f"[!] SQS: {e}", flush=True)
        return {}


# ============================================================
# 2. GCP 전송
# ============================================================

def gcp_push(log_name: str, payload: dict, severity: str) -> None:
    client = gcp_logging.Client(project=PROJECT_ID)
    client.logger(log_name).log_struct(payload, severity=severity)
    print(f"[+] GCP 전송 완료: {log_name} [{severity}]", flush=True)


def severity_from_metrics(metrics: dict) -> str:
    prom = metrics.get("prometheus", {})
    if any((v.get("value") or 0) == 1
           for v in prom.get("nodeMemoryPressure", [])):
        return "WARNING"
    if any((v.get("value") or 0) >= 5
           for v in prom.get("podRestarts", [])):
        return "WARNING"
    if int(metrics.get("sqs", {}).get("ApproximateNumberOfMessages", 0)) >= 1000:
        return "NOTICE"
    return "INFO"


def severity_from_rec(rec: dict) -> str:
    recs = rec.get("recommendations", [])
    if any(r.get("risk") == "high" for r in recs):
        return "WARNING"
    if any(r.get("priority") == "now" for r in recs):
        return "NOTICE"
    return "INFO"


# ============================================================
# 3. Gemini 추천
# ============================================================

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target":     {"type": "string"},
                    "field":      {"type": "string"},
                    "from":       {"type": "string"},
                    "to":         {"type": "string"},
                    "reason":     {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "priority":   {"type": "string", "enum": ["now", "watch", "later"]},
                    "risk":       {"type": "string", "enum": ["none", "low", "medium", "high"]},
                },
                "required": ["target", "field", "from", "to",
                             "reason", "confidence", "priority", "risk"],
            },
        },
        "warnings":            {"type": "array", "items": {"type": "string"}},
        "estimatedCostDelta": {
            "type": "object",
            "properties": {
                "direction":          {"type": "string",
                                       "enum": ["increase", "decrease", "neutral"]},
                "approxUSDPerMonth":  {"type": "number"},
                "rationale":          {"type": "string"},
            },
            "required": ["direction", "approxUSDPerMonth", "rationale"],
        },
        "openQuestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "recommendations", "warnings",
                 "estimatedCostDelta", "openQuestions"],
}


def call_gemini(metrics: dict, api_key: str) -> dict:
    context_md = CONTEXT_FILE.read_text(encoding="utf-8")
    prompt = (
        f"{context_md}\n\n---\n\n"
        f"## L. 현재 메트릭 스냅샷 (DYNAMIC)\n\n"
        f"```json\n{json.dumps(metrics, ensure_ascii=False, indent=2, default=str)}\n```\n\n"
        f"---\n\n## M. 요청\n\n"
        f"§H 의 12개 항목을 기준으로 §K 스키마에 맞춰 추천하라.\n"
        f"- §J 필드 해설을 참고해 prometheus / cloudwatch 데이터를 적극 활용하라.\n"
        f"- 빈 배열/객체 필드는 수집 실패로 간주하고 해당 항목 추천에서 제외하라.\n"
        f"- `from`/`to` 는 문자열로 표기."
    )
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.2,
        ),
    )
    return json.loads(resp.text)


# ============================================================
# 4. Slack 알림
# ============================================================

def slack_notify(rec: dict) -> None:
    if not SLACK_WEBHOOK:
        return
    now_items = [r for r in rec.get("recommendations", [])
                 if r.get("priority") == "now"]
    if not now_items:
        return

    header = f":robot_face: [AI Advisor] NOW 추천 {len(now_items)}건"
    lines  = [f"• *{r['target']}* `{r['field']}` `{r['from']} → {r['to']}`"
               f"  _{r['risk']}/{r['confidence']}_\n   ↳ {r['reason'][:160]}"
               for r in now_items[:10]]
    payload = {
        "text": header,
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": header}},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"*요약*\n{rec.get('summary', '')}\n\n"
                               + "\n".join(lines)}},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        SLACK_WEBHOOK, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"[+] Slack 전송 완료 (HTTP {r.status})", flush=True)


# ============================================================
# 메인
# ============================================================

def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        sys.stderr.write("GEMINI_API_KEY 미설정\n")
        return 1
    if not CONTEXT_FILE.exists():
        sys.stderr.write(f"컨텍스트 파일 없음: {CONTEXT_FILE}\n")
        return 1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[*] AI Advisor CronJob 시작: {ts}", flush=True)

    # ── 메트릭 수집 ──────────────────────────────────────────
    print("[*] k8s 리소스 수집...", flush=True)
    k8s_data   = collect_k8s(NS)
    prometheus = collect_prometheus(NS)
    sqs        = collect_sqs(QUEUE_NAME, REGION)
    cw_rds, cw_redis = collect_cloudwatch(REGION)

    metrics = {
        "timestamp":      ts,
        "namespace":      NS,
        "region":         REGION,
        **k8s_data,
        "sqs":            sqs,
        "prometheus":     prometheus,
        "cloudwatchRds":  cw_rds,
        "cloudwatchRedis": cw_redis,
    }

    # ── GCP: 메트릭 전송 ─────────────────────────────────────
    gcp_push("eks-metrics", {
        "source":      "cronjob",
        "collectedAt": ts,
        "summary": {
            "nodeCount":   len(metrics.get("nodes", [])),
            "sqsDepth": {
                "waiting":  int(sqs.get("ApproximateNumberOfMessages", 0)),
                "inFlight": int(sqs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            },
        },
        "prometheus":  prometheus,
        "cloudwatch":  {"rds": cw_rds, "redis": cw_redis},
    }, severity=severity_from_metrics(metrics))

    # ── Gemini 추천 ───────────────────────────────────────────
    print("[*] Gemini 추천 요청...", flush=True)
    rec = call_gemini(metrics, api_key)
    print(f"[+] 추천 수신: NOW {sum(1 for r in rec.get('recommendations',[]) if r.get('priority')=='now')}건", flush=True)

    # ── GCP: 추천 전송 ────────────────────────────────────────
    recs = rec.get("recommendations", [])
    gcp_push("gemini-recommendations", {
        "source":   "cronjob",
        "timestamp": ts,
        "summary":  rec.get("summary", ""),
        "counts": {
            "total":    len(recs),
            "now":      sum(1 for r in recs if r.get("priority") == "now"),
            "watch":    sum(1 for r in recs if r.get("priority") == "watch"),
            "later":    sum(1 for r in recs if r.get("priority") == "later"),
            "highRisk": sum(1 for r in recs if r.get("risk") == "high"),
        },
        "estimatedCostDelta": rec.get("estimatedCostDelta"),
        "warnings":      rec.get("warnings", []),
        "openQuestions": rec.get("openQuestions", []),
        "recommendations": recs,
    }, severity=severity_from_rec(rec))

    # ── Slack 알림 ────────────────────────────────────────────
    slack_notify(rec)

    print(f"[*] AI Advisor 완료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
