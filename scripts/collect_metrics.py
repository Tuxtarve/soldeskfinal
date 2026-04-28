#!/usr/bin/env python3
"""EKS + SQS + Prometheus + CloudWatch 상태를 하나의 JSON 스냅샷으로 모은다.
Windows / macOS / Linux 전부 동작 (bash 불필요).

사용:
  python3 scripts/collect_metrics.py
  python3 scripts/collect_metrics.py /tmp/out.json

필요 패키지:
  pip install boto3 requests
  (kubectl, AWS 자격증명은 사전 설정 필요)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import boto3
    import requests
except ImportError:
    sys.stderr.write("pip install boto3 requests 필요\n")
    sys.exit(2)

def _get_aws_region() -> str:
    try:
        result = subprocess.run(
            ["aws", "configure", "get", "region"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "ap-northeast-2"
    except Exception:
        return "ap-northeast-2"


# ---------- 설정 ----------
NS = os.environ.get("NS", "ticketing")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "ticketing-reservation.fifo")
REGION = os.environ.get("AWS_REGION") or _get_aws_region()

PROM_SVC = os.environ.get("PROM_SVC", "kube-prometheus-stack-prometheus")
PROM_NS  = os.environ.get("PROM_NS",  "monitoring")
PROM_LOCAL_PORT = 19090

RDS_INSTANCE_ID = "prod-ticketing-writer"
REDIS_GROUP_ID  = "ticketing-redis"

ROOT     = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


# ---------- kubectl 헬퍼 ----------
def kubectl(args: list[str]) -> dict | list:
    """kubectl ... -o json 결과를 파이썬 객체로 반환. 실패 시 빈 값."""
    cmd = ["kubectl"] + args + ["-o", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def kubectl_top_nodes() -> list[dict]:
    try:
        r = subprocess.run(
            ["kubectl", "top", "nodes", "--no-headers"],
            capture_output=True, text=True, timeout=15,
        )
        rows = []
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 5:
                rows.append({"name": parts[0], "cpu": parts[1], "cpuPct": parts[2],
                              "mem": parts[3], "memPct": parts[4]})
        return rows
    except Exception:
        return []


def kubectl_top_pods(ns: str) -> list[dict]:
    try:
        r = subprocess.run(
            ["kubectl", "top", "pods", "-n", ns, "--no-headers"],
            capture_output=True, text=True, timeout=15,
        )
        rows = []
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows.append({"pod": parts[0], "cpu": parts[1], "mem": parts[2]})
        return rows
    except Exception:
        return []


# ---------- k8s 리소스 파싱 ----------
def parse_nodes(raw: dict) -> list[dict]:
    result = []
    for item in raw.get("items", []):
        result.append({
            "name": item["metadata"]["name"],
            "instanceType": item["metadata"].get("labels", {}).get(
                "node.kubernetes.io/instance-type", ""),
            "capacity": item["status"].get("capacity", {}),
            "allocatable": item["status"].get("allocatable", {}),
            "conditions": [
                {"status": c["status"], "reason": c.get("reason", "")}
                for c in item["status"].get("conditions", [])
                if c["type"] == "Ready"
            ],
        })
    return result


def parse_deployments(raw: dict) -> list[dict]:
    result = []
    for item in raw.get("items", []):
        result.append({
            "name": item["metadata"]["name"],
            "replicas": item["status"].get("replicas"),
            "available": item["status"].get("availableReplicas"),
            "desired": item["spec"].get("replicas"),
            "containers": [
                {"name": c["name"], "resources": c.get("resources", {})}
                for c in item["spec"]["template"]["spec"].get("containers", [])
            ],
        })
    return result


def parse_hpa(raw: dict) -> list[dict]:
    result = []
    for item in raw.get("items", []):
        spec = item.get("spec", {})
        status = item.get("status", {})
        ref = spec.get("scaleTargetRef", {})
        result.append({
            "name": item["metadata"]["name"],
            "target": f"{ref.get('kind', '')}/{ref.get('name', '')}",
            "minReplicas": spec.get("minReplicas"),
            "maxReplicas": spec.get("maxReplicas"),
            "currentReplicas": status.get("currentReplicas"),
            "desiredReplicas": status.get("desiredReplicas"),
            "metrics": spec.get("metrics", []),
            "currentMetrics": status.get("currentMetrics", []),
        })
    return result


def parse_scaled_objects(raw: dict) -> list[dict]:
    result = []
    for item in raw.get("items", []):
        spec = item.get("spec", {})
        conditions = item.get("status", {}).get("conditions", [])
        ready  = next((c["status"] for c in conditions if c["type"] == "Ready"),  None)
        active = next((c["status"] for c in conditions if c["type"] == "Active"), None)
        result.append({
            "name": item["metadata"]["name"],
            "target": spec.get("scaleTargetRef", {}).get("name"),
            "minReplicaCount": spec.get("minReplicaCount"),
            "maxReplicaCount": spec.get("maxReplicaCount"),
            "triggers": spec.get("triggers", []),
            "status": {"ready": ready, "active": active},
        })
    return result


def parse_events(raw: dict) -> list[dict]:
    keywords = ("Scal", "Scheduled", "FailedScheduling", "Evicted")
    items = raw.get("items", [])
    filtered = [
        {
            "time": e.get("lastTimestamp"),
            "reason": e.get("reason"),
            "object": (
                f"{e.get('involvedObject', {}).get('kind', '')}/"
                f"{e.get('involvedObject', {}).get('name', '')}"
            ),
            "message": e.get("message"),
        }
        for e in items
        if any(k in (e.get("reason") or "") for k in keywords)
    ]
    return filtered[-20:]


# ---------- SQS ----------
def collect_sqs(queue_name: str, region: str) -> dict:
    try:
        client = boto3.client("sqs", region_name=region)
        url = client.get_queue_url(QueueName=queue_name)["QueueUrl"]
        attrs = client.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ],
        )["Attributes"]
        return attrs
    except Exception as e:
        sys.stderr.write(f"[!] SQS 수집 실패: {e}\n")
        return {}


# ---------- Prometheus ----------
def _prom_query(port: int, query: str) -> list[dict]:
    encoded = urllib.parse.quote(query)
    try:
        resp = requests.get(
            f"http://localhost:{port}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        results = resp.json().get("data", {}).get("result", [])
        return [
            {"metric": r["metric"], "value": _to_number(r["value"][1])}
            for r in results
        ]
    except Exception:
        return []


def _prom_query_range(port: int, query: str, minutes: int = 15) -> list:
    now = int(time.time())
    start = now - minutes * 60
    try:
        resp = requests.get(
            f"http://localhost:{port}/api/v1/query_range",
            params={"query": query, "start": start, "end": now, "step": 60},
            timeout=15,
        )
        return resp.json().get("data", {}).get("result", [])
    except Exception:
        return []


def _to_number(v: str):
    try:
        return int(v) if "." not in v else float(v)
    except (ValueError, TypeError):
        return v


def collect_prometheus(ns: str) -> dict:
    print("[*] Prometheus 포트포워드 시도...", file=sys.stderr)
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", PROM_NS,
         f"svc/{PROM_SVC}", f"{PROM_LOCAL_PORT}:9090"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)

    try:
        resp = requests.get(
            f"http://localhost:{PROM_LOCAL_PORT}/-/healthy", timeout=3)
        if resp.status_code != 200:
            raise ConnectionError
    except Exception:
        proc.kill()
        proc.wait()
        print("[!] Prometheus 연결 실패 — 생략", file=sys.stderr)
        return {}

    print(f"[*] Prometheus 연결 OK (pid={proc.pid})", file=sys.stderr)
    p = PROM_LOCAL_PORT

    result = {
        "cpuRatePerContainer": _prom_query(p,
            f'sum by (pod,container)(rate(container_cpu_usage_seconds_total'
            f'{{namespace="{ns}",container!="",container!="POD"}}[5m]))'),
        "memBytesPerContainer": _prom_query(p,
            f'sum by (pod,container)(container_memory_working_set_bytes'
            f'{{namespace="{ns}",container!="",container!="POD"}})'),
        "podRestarts": _prom_query(p,
            f'sum by (pod,container)(kube_pod_container_status_restarts_total'
            f'{{namespace="{ns}"}})'),
        "hpaDesiredTrend15m": _prom_query_range(p,
            f'kube_horizontalpodautoscaler_status_desired_replicas{{namespace="{ns}"}}'),
        "hpaCurrentReplicas": _prom_query(p,
            f'kube_horizontalpodautoscaler_status_current_replicas{{namespace="{ns}"}}'),
        "nodeMemoryPressure": _prom_query(p,
            'kube_node_status_condition{condition="MemoryPressure",status="true"}'),
        "httpRpsPerPod": _prom_query(p,
            f'sum by (pod)(rate(http_requests_total{{namespace="{ns}"}}[5m]))'),
    }

    proc.kill()
    proc.wait()
    print("[*] Prometheus 수집 완료", file=sys.stderr)
    return result


# ---------- CloudWatch ----------
def _cw_stats(client, namespace: str, metric: str,
               dimensions: list[dict], region: str,
               stats: list[str], minutes: int = 15) -> list[dict]:
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(now.timestamp() - minutes * 60, tz=timezone.utc)
    try:
        resp = client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=start.isoformat(),
            EndTime=now.isoformat(),
            Period=300,
            Statistics=stats,
        )
        return sorted(resp.get("Datapoints", []), key=lambda x: x.get("Timestamp", ""))
    except Exception as e:
        sys.stderr.write(f"[!] CloudWatch {metric} 수집 실패: {e}\n")
        return []


def collect_cloudwatch(region: str) -> tuple[dict, dict]:
    cw = boto3.client("cloudwatch", region_name=region)

    rds_dims = [{"Name": "DBInstanceIdentifier", "Value": RDS_INSTANCE_ID}]
    rds = {
        "instanceId": RDS_INSTANCE_ID,
        "connections15m": _cw_stats(cw, "AWS/RDS", "DatabaseConnections",
                                    rds_dims, region, ["Average", "Maximum"]),
        "cpu15m":         _cw_stats(cw, "AWS/RDS", "CPUUtilization",
                                    rds_dims, region, ["Average"]),
    }

    redis_dims = [{"Name": "ReplicationGroupId", "Value": REDIS_GROUP_ID}]
    redis = {
        "groupId": REDIS_GROUP_ID,
        "connections15m": _cw_stats(cw, "AWS/ElastiCache", "CurrConnections",
                                    redis_dims, region, ["Average", "Maximum"]),
        "bytesUsed15m":   _cw_stats(cw, "AWS/ElastiCache", "BytesUsedForCache",
                                    redis_dims, region, ["Average"]),
        "evictions15m":   _cw_stats(cw, "AWS/ElastiCache", "Evictions",
                                    redis_dims, region, ["Sum"]),
    }

    return rds, redis


# ---------- 메인 ----------
def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if out_path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = DATA_DIR / f"metrics-{ts}.json"

    print(f"[*] namespace={NS}  queue={QUEUE_NAME}  region={REGION}", file=sys.stderr)

    # k8s
    nodes_raw     = kubectl(["get", "nodes"])
    deploys_raw   = kubectl(["get", "deploy", "-n", NS])
    hpa_raw       = kubectl(["get", "hpa",    "-n", NS])
    so_raw        = kubectl(["get", "scaledobject", "-n", NS]) or {}
    events_raw    = kubectl(["get", "events", "-n", NS, "--sort-by=.lastTimestamp"])

    node_top  = kubectl_top_nodes()
    pod_top   = kubectl_top_pods(NS)

    nodes        = parse_nodes(nodes_raw)
    deployments  = parse_deployments(deploys_raw)
    hpa          = parse_hpa(hpa_raw)
    scaled_objs  = parse_scaled_objects(so_raw)
    events       = parse_events(events_raw)

    # SQS
    sqs = collect_sqs(QUEUE_NAME, REGION)

    # Prometheus
    prometheus = collect_prometheus(NS)

    # CloudWatch
    print("[*] CloudWatch 메트릭 수집...", file=sys.stderr)
    cw_rds, cw_redis = collect_cloudwatch(REGION)

    snapshot = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "namespace": NS,
        "region": REGION,
        "nodes": nodes,
        "nodeTop": node_top,
        "deployments": deployments,
        "podTop": pod_top,
        "hpa": hpa,
        "scaledObjects": scaled_objs,
        "sqs": sqs,
        "events": events,
        "prometheus": prometheus,
        "cloudwatchRds": cw_rds,
        "cloudwatchRedis": cw_redis,
    }

    out_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    size = out_path.stat().st_size
    print(f"[+] wrote {out_path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
