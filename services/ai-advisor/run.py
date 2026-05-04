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

def _serialize(obj):
    """datetime 등 JSON 직렬화 불가 타입을 문자열로 변환."""
    from datetime import datetime, date
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    return obj


_gcp_client_cache: "gcp_logging.Client | None" = None

def _get_gcp_client() -> "gcp_logging.Client":
    """boto3로 IRSA 자격증명을 가져와 env var에 주입 후 WIF 교환.

    기존 방식(IMDS 직접 접근)의 문제:
      - IMDS에서 가져오는 것은 노드 역할 자격증명이며, 노드 역할은 GCP WIF 바인딩에
        없거나 hop limit(기본값 1)으로 Pod에서 IMDS 접근이 실패할 수 있다.

    이 방식:
      - boto3가 AWS_WEB_IDENTITY_TOKEN_FILE + AWS_ROLE_ARN을 자동 처리해
        IRSA 역할(ticketing-eks-ai-advisor)의 임시 자격증명을 취득한다.
      - google.auth 라이브러리는 AWS_ACCESS_KEY_ID env var를 IMDS보다 우선 확인하므로,
        boto3 자격증명을 env var에 주입하면 IRSA 역할로 WIF 교환이 성공한다.
      - IRSA 역할은 setup-gcp.sh에서 GCP WIF에 이미 바인딩되어 있다.
    """
    global _gcp_client_cache
    if _gcp_client_cache is not None:
        return _gcp_client_cache

    # boto3로 IRSA 임시 자격증명 취득
    frozen = boto3.Session(region_name=REGION).get_credentials().get_frozen_credentials()
    os.environ["AWS_ACCESS_KEY_ID"]     = frozen.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
    if frozen.token:
        os.environ["AWS_SESSION_TOKEN"] = frozen.token

    import google.auth
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/etc/gcp/config.json")
    try:
        credentials, _ = google.auth.load_credentials_from_file(
            cred_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        _gcp_client_cache = gcp_logging.Client(project=PROJECT_ID, credentials=credentials)
        print("[*] GCP 클라이언트 초기화 완료 (WIF via IRSA)", flush=True)
        return _gcp_client_cache
    except Exception as e:
        print(f"[!] WIF 자격증명 로드 실패 ({cred_path}): {e}", flush=True)
        raise


def gcp_push(log_name: str, payload: dict, severity: str) -> None:
    client = _get_gcp_client()
    client.logger(log_name).log_struct(_serialize(payload), severity=severity)
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

    # 🔥 retry 로직
    for i in range(3):
        try:
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

        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"[WARN] Gemini 503 retry {i+1}/3", flush=True)
                time.sleep(5 * (i + 1))
            else:
                # 다른 에러는 그대로 터뜨림
                raise

    # 🔥 최종 fallback (이게 핵심)
    print("[WARN] Gemini 전체 실패 → 빈 결과 반환", flush=True)

    return {
        "summary": "Gemini unavailable",
        "recommendations": []
    }


# ============================================================
# 4. 자동 패치 적용  (안전장치 포함)
# ============================================================

# 자동으로 건드릴 수 있는 필드만 허용 — 이 외는 절대 변경하지 않음
# Gemini 는 실제 YAML 경로(spec.maxReplicas 등)로 반환하므로 그 형식에 맞춤
_SAFE_FIELDS      = {"spec.maxReplicas", "spec.maxReplicaCount"}
_MAX_SCALE_FACTOR = 2.0   # 현재값 대비 최대 2배까지만 증가 허용
_MAX_PATCHES_RUN  = 3     # 1회 실행당 최대 패치 건수
_COOLDOWN_MIN     = 30    # 동일 대상 재패치 금지 시간(분)
_COOLDOWN_CM      = "ai-advisor-cooldown"
_DRY_RUN          = os.environ.get("AI_PATCH_DRY_RUN", "false").lower() == "true"


def _cooldown_load(ns: str) -> dict:
    try:
        cm = _k8s_client().CoreV1Api().read_namespaced_config_map(_COOLDOWN_CM, ns)
        return json.loads(cm.data.get("patches", "{}"))
    except Exception:
        return {}


def _cooldown_save(ns: str, data: dict) -> None:
    v1 = _k8s_client().CoreV1Api()
    body = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": _COOLDOWN_CM, "namespace": ns},
        "data": {"patches": json.dumps(data)},
    }
    try:
        v1.replace_namespaced_config_map(_COOLDOWN_CM, ns, body)
    except Exception:
        try:
            v1.create_namespaced_config_map(ns, body)
        except Exception as e:
            print(f"[!] 쿨다운 저장 실패: {e}", flush=True)


def _get_current_value(target: str, field: str, ns: str) -> int | None:
    """K8s API 에서 현재 실제값을 읽어 반환. Gemini 출력의 from 값은 믿지 않음."""
    parts         = [p.strip() for p in target.split("/")]
    resource_type = parts[0].lower() if parts else ""
    resource_name = parts[-1]        if parts else target.strip()
    k = _k8s_client()
    try:
        if field == "spec.maxReplicas" and resource_type == "hpa":
            hpa = k.AutoscalingV2Api().read_namespaced_horizontal_pod_autoscaler(resource_name, ns)
            return hpa.spec.max_replicas
        if field == "spec.maxReplicaCount" and resource_type == "keda":
            obj = k.CustomObjectsApi().get_namespaced_custom_object(
                "keda.sh", "v1alpha1", ns, "scaledobjects", resource_name)
            return obj.get("spec", {}).get("maxReplicaCount")
    except Exception as e:
        print(f"[!] 현재값 조회 실패 {target}/{field}: {e}", flush=True)
    return None


def _do_k8s_patch(target: str, field: str, value: int, ns: str) -> bool:
    """target 형식: "hpa/read-api-hpa"  또는  "keda/scaledobject-name"
    field 형식: "spec.maxReplicas"  또는  "spec.maxReplicaCount"
    """
    k = _k8s_client()
    # "hpa/read-api-hpa"              → type="hpa",  name="read-api-hpa"
    # "keda/scaledobject/worker-svc-sqs" → type="keda", name="worker-svc-sqs"
    parts         = [p.strip() for p in target.split("/")]
    resource_type = parts[0].lower() if parts else ""
    resource_name = parts[-1]        if parts else target.strip()
    try:
        if field == "spec.maxReplicas" and resource_type == "hpa":
            k.AutoscalingV2Api().patch_namespaced_horizontal_pod_autoscaler(
                resource_name, ns, {"spec": {"maxReplicas": value}}
            )
        elif field == "spec.maxReplicaCount" and resource_type == "keda":
            k.CustomObjectsApi().patch_namespaced_custom_object(
                "keda.sh", "v1alpha1", ns, "scaledobjects", resource_name,
                {"spec": {"maxReplicaCount": value}},
            )
        else:
            print(f"[!] 패치 불가 필드: target={target} field={field}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[!] 패치 실패 {target}/{field}: {e}", flush=True)
        return False


def apply_safe_patches(recs: list, ns: str) -> list:
    """안전 조건을 통과한 추천만 K8s에 직접 적용하고 적용 목록을 반환.

    안전 조건:
      - priority=now  AND  confidence=high  AND  risk∈{none,low}
      - field 가 _SAFE_FIELDS 에 포함
      - 값이 현재보다 증가하는 방향 (감소 거부)
      - 현재값의 2배 초과 시 클램프
      - 30분 이내 동일 대상 재패치 금지
      - 1회 실행 최대 3건
    """
    now = datetime.now(timezone.utc)
    cooldown = _cooldown_load(ns)
    applied = []

    candidates = [
        r for r in recs
        if r.get("priority") == "now"
        and r.get("confidence") == "high"
        and r.get("risk") in ("none", "low")
        and r.get("field") in _SAFE_FIELDS
    ][:_MAX_PATCHES_RUN]

    if not candidates:
        print("[~] 자동 패치 대상 없음 (안전 조건 미달)", flush=True)
        return []

    for rec in candidates:
        key = f"{rec['target']}/{rec['field']}"

        # 쿨다운: 30분 이내 동일 대상 재패치 금지
        last_str = cooldown.get(key)
        if last_str:
            elapsed = (now - datetime.fromisoformat(last_str)).total_seconds() / 60
            if elapsed < _COOLDOWN_MIN:
                print(f"[~] 쿨다운 스킵: {key} ({elapsed:.0f}분 전 적용됨)", flush=True)
                continue

        # Gemini의 from 값은 할루시네이션이 잦으므로 K8s 실제값으로 대체
        import re as _re
        def _parse_int(s: str) -> int | None:
            m = _re.search(r'\d+', str(s))
            return int(m.group()) if m else None

        actual_v = _get_current_value(rec["target"], rec["field"], ns)
        from_v   = actual_v if actual_v is not None else _parse_int(rec.get("from", ""))
        to_v     = _parse_int(rec.get("to", ""))

        if from_v is None or to_v is None:
            print(f"[!] 값 파싱 실패, 스킵: {key}  실제={actual_v} gemini_from={rec.get('from')} to={rec.get('to')}", flush=True)
            continue

        print(f"[~] 현재값 확인: {key}  실제={from_v} (Gemini주장={rec.get('from')}) → 추천={to_v}", flush=True)

        # 감소 거부 — 장애 대응 중 스케일 다운은 금지
        if to_v <= from_v:
            print(f"[!] 감소 거부: {key}  실제={from_v} → 추천={to_v}", flush=True)
            continue

        # 2배 초과 클램프 — 급격한 변경 방지
        cap = max(from_v + 1, int(from_v * _MAX_SCALE_FACTOR))
        if to_v > cap:
            print(f"[~] 2배 클램프: {key}  요청={to_v} → 적용={cap}", flush=True)
            to_v = cap

        tag = "[DRY]" if _DRY_RUN else "[PATCH]"
        print(f"{tag} {key}: {from_v} → {to_v}  | {rec.get('reason','')[:80]}", flush=True)

        ok = True if _DRY_RUN else _do_k8s_patch(rec["target"], rec["field"], to_v, ns)

        if ok:
            cooldown[key] = now.isoformat()
            applied.append({**rec, "to": str(to_v), "appliedAt": now.isoformat(), "dryRun": _DRY_RUN})

    if applied and not _DRY_RUN:
        _cooldown_save(ns, cooldown)

    return applied


def _slack_patch_notify(applied: list) -> None:
    if not SLACK_WEBHOOK or not applied:
        return
    mode  = " *(DRY RUN)*" if _DRY_RUN else ""
    lines = [f":wrench: [AI 자동 패치 {len(applied)}건{mode}]"]
    for p in applied:
        lines.append(
            f"• `{p['target']}` `{p['field']}` : {p.get('from','?')} → {p['to']}"
            f"  [위험:{p.get('risk','-')}]  _{p.get('reason','')[:80]}_"
        )
    data = json.dumps({"text": "\n".join(lines)}).encode("utf-8")
    req  = urllib.request.Request(
        SLACK_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[+] Slack 패치 알림 전송 (HTTP {r.status})", flush=True)
    except Exception as e:
        print(f"[!] Slack 패치 알림 실패: {e}", flush=True)


# ============================================================
# 5. Slack 알림
# ============================================================

def slack_notify(rec: dict) -> None:
    if not SLACK_WEBHOOK:
        return
    now_items = [r for r in rec.get("recommendations", [])
                 if r.get("priority") == "now"]
    if not now_items:
        return

    header = f":robot_face: [AI Advisor] NOW {len(now_items)} items"
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

    # ── GCP 클라이언트 사전 초기화 (병렬 수집 중 WIF 인증 완료) ──
    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=1) as _p:
        _gcp_future = _p.submit(_get_gcp_client)

    # ── 메트릭 수집 (병렬) ───────────────────────────────────
    # k8s·Prometheus·SQS·CloudWatch 를 동시에 수집해 총 소요시간 단축
    print("[*] 메트릭 병렬 수집 시작...", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _collect_k8s_task():
        return "k8s", collect_k8s(NS)

    def _collect_prom_task():
        return "prometheus", collect_prometheus(NS)

    def _collect_sqs_task():
        return "sqs", collect_sqs(QUEUE_NAME, REGION)

    def _collect_cw_task():
        return "cloudwatch", collect_cloudwatch(REGION)

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_collect_k8s_task),
            pool.submit(_collect_prom_task),
            pool.submit(_collect_sqs_task),
            pool.submit(_collect_cw_task),
        ]
        for f in as_completed(futures):
            try:
                key, val = f.result(timeout=30)
                results[key] = val
            except Exception as ex:
                print(f"[!] 수집 실패: {ex}", flush=True)

    k8s_data         = results.get("k8s", {})
    prometheus       = results.get("prometheus", {})
    sqs              = results.get("sqs", {})
    cw_rds, cw_redis = results.get("cloudwatch", ({}, {}))
    print("[*] 메트릭 수집 완료", flush=True)

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
    recs = rec.get("recommendations", [])
    print(f"[+] 추천 수신: NOW {sum(1 for r in recs if r.get('priority')=='now')}건", flush=True)
    print(f"[Gemini 요약] {rec.get('summary','')}", flush=True)
    for r in recs:
        pri = r.get('priority','?').upper()
        print(f"  [{pri}][{r.get('risk','-')}위험][신뢰:{r.get('confidence','-')}] {r.get('target','')} / {r.get('field','')} : {r.get('reason','')[:80]}", flush=True)

    # ── GCP: 추천 전송 ────────────────────────────────────────
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

    # ── Slack 알림 (추천) ─────────────────────────────────────
    slack_notify(rec)

    # ── 자동 패치 적용 ────────────────────────────────────────
    mode = "DRY RUN" if _DRY_RUN else "실제 적용"
    print(f"[*] 자동 패치 시작 ({mode})...", flush=True)
    applied = apply_safe_patches(recs, NS)
    if applied:
        print(f"[+] 자동 패치 완료: {len(applied)}건 ({mode})", flush=True)
        gcp_push("ai-auto-patches", {
            "source":    "cronjob",
            "timestamp": ts,
            "dryRun":    _DRY_RUN,
            "count":     len(applied),
            "patches":   applied,
        }, severity="INFO" if _DRY_RUN else "WARNING")
        _slack_patch_notify(applied)

    print(f"[*] AI Advisor 완료", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
