#!/usr/bin/env python3
"""EKS 메트릭 스냅샷을 GCP Cloud Logging 으로 전송.
gcloud CLI 불필요 — google-cloud-logging SDK + ADC 사용.
Windows / macOS / Linux 전부 동작.

인증:
  gcloud auth application-default login  (한 번만)

조회:
  GCP 콘솔 → Logging → Logs Explorer
  쿼리: logName="projects/soldesk-gcp/logs/eks-metrics"

사용:
  python3 scripts/push_metrics_to_cloud_logging.py
  python3 scripts/push_metrics_to_cloud_logging.py scripts/data/metrics-xxxx.json

환경변수:
  GCP_PROJECT_ID       (기본: soldesk-gcp)
  GCP_METRICS_LOG_NAME (기본: eks-metrics)
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from google.cloud import logging as gcp_logging
except ImportError:
    sys.stderr.write("pip install google-cloud-logging 필요\n")
    sys.exit(2)

ROOT     = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

PROJECT_ID = os.environ.get("GCP_PROJECT_ID",       "soldesk-gcp")
LOG_NAME   = os.environ.get("GCP_METRICS_LOG_NAME", "eks-metrics")


def latest_metrics() -> Path:
    files = sorted(glob.glob(str(DATA_DIR / "metrics-*.json")))
    if not files:
        sys.stderr.write("metrics-*.json 없음. collect_metrics.py 먼저 실행.\n")
        sys.exit(1)
    return Path(files[-1])


def derive_severity(metrics: dict) -> str:
    prom = metrics.get("prometheus", {})

    if any((v.get("value") or 0) == 1
           for v in prom.get("nodeMemoryPressure", [])):
        return "WARNING"

    if any((v.get("value") or 0) >= 5
           for v in prom.get("podRestarts", [])):
        return "WARNING"

    sqs = metrics.get("sqs", {})
    if int(sqs.get("ApproximateNumberOfMessages", 0)) >= 1000:
        return "NOTICE"

    return "INFO"


def build_payload(metrics: dict, src: Path) -> dict:
    nodes       = metrics.get("nodes", [])
    deployments = metrics.get("deployments", [])
    prom        = metrics.get("prometheus", {})
    sqs         = metrics.get("sqs", {})
    cw_rds      = metrics.get("cloudwatchRds", {})
    cw_redis    = metrics.get("cloudwatchRedis", {})

    hpa_summary = [
        {
            "name":    h.get("name"),
            "current": h.get("currentReplicas"),
            "desired": h.get("desiredReplicas"),
            "max":     h.get("maxReplicas"),
        }
        for h in metrics.get("hpa", [])
    ]
    restarts_alert = [
        v for v in prom.get("podRestarts", [])
        if (v.get("value") or 0) >= 5
    ]

    return {
        "source":      src.name,
        "collectedAt": metrics.get("timestamp",
                                   datetime.utcnow().isoformat() + "Z"),
        "pushedAt":    datetime.utcnow().isoformat() + "Z",
        "namespace":   metrics.get("namespace", "ticketing"),
        "region":      metrics.get("region",    "ap-northeast-2"),
        "summary": {
            "nodeCount":   len(nodes),
            "deployments": [
                {"name": d.get("name"),
                 "desired": d.get("desired"),
                 "available": d.get("available")}
                for d in deployments
            ],
            "hpa":        hpa_summary,
            "sqsDepth": {
                "waiting":  int(sqs.get("ApproximateNumberOfMessages",          0)),
                "inFlight": int(sqs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            },
            "restartsAlert": restarts_alert,
        },
        "prometheus": {
            "cpuRatePerContainer":  prom.get("cpuRatePerContainer",  []),
            "memBytesPerContainer": prom.get("memBytesPerContainer", []),
            "podRestarts":          prom.get("podRestarts",          []),
            "hpaDesiredTrend15m":   prom.get("hpaDesiredTrend15m",  []),
            "httpRpsPerPod":        prom.get("httpRpsPerPod",        []),
            "nodeMemoryPressure":   prom.get("nodeMemoryPressure",   []),
        },
        "cloudwatch": {
            "rds":   cw_rds,
            "redis": cw_redis,
        },
    }


def main() -> int:
    src     = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_metrics()
    metrics = json.loads(src.read_text(encoding="utf-8"))

    severity = derive_severity(metrics)
    payload  = build_payload(metrics, src)

    # ADC 자동 사용 (gcloud auth application-default login)
    client = gcp_logging.Client(project=PROJECT_ID)
    logger = client.logger(LOG_NAME)

    print(f"[push] source:   {src}")
    print(f"[push] project:  {PROJECT_ID}")
    print(f"[push] logName:  {LOG_NAME}")
    print(f"[push] severity: {severity}")

    logger.log_struct(payload, severity=severity)

    print(f"[push] 전송 완료. Logs Explorer 에서 확인:")
    print(f"  https://console.cloud.google.com/logs/query;"
          f"query=logName%3D%22projects%2F{PROJECT_ID}%2Flogs%2F{LOG_NAME}%22"
          f"?project={PROJECT_ID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
