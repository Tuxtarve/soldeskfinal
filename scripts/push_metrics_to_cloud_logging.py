#!/usr/bin/env python3
"""EKS 메트릭 스냅샷을 GCP Cloud Logging 으로 전송.

Gemini 추천(gemini-recommendations)과 별도로, 메트릭 원본 자체를
GCP 에 보관해 Logs Explorer 에서 시계열 조회가 가능하게 한다.

조회:
  GCP 콘솔 → Logging → Logs Explorer
  쿼리: logName="projects/soldesk-gcp/logs/eks-metrics"

입력:
  scripts/data/metrics-*.json  (최신 자동 선택 또는 인자)

요구:
  - gcloud 가 PATH 에 있어야 함
  - `gcloud auth login` + `gcloud config set project soldesk-gcp` 완료
  - logging.googleapis.com 활성화

사용:
  python3 scripts/push_metrics_to_cloud_logging.py
  python3 scripts/push_metrics_to_cloud_logging.py scripts/data/metrics-xxxx.json

환경변수:
  GCP_METRICS_LOG_NAME   (기본: eks-metrics)
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_NAME = os.environ.get("GCP_METRICS_LOG_NAME", "eks-metrics")
GCLOUD_CANDIDATES = [
    "gcloud",
    "/root/google-cloud-sdk/bin/gcloud",
    "/usr/local/bin/gcloud",
    os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
]


def find_gcloud() -> str:
    for c in GCLOUD_CANDIDATES:
        try:
            subprocess.run([c, "--version"], capture_output=True, check=True, timeout=5)
            return c
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    sys.stderr.write("gcloud 바이너리를 찾을 수 없습니다. PATH 확인.\n")
    sys.exit(2)


def latest_metrics() -> Path:
    files = sorted(glob.glob(str(DATA_DIR / "metrics-*.json")))
    if not files:
        sys.stderr.write("metrics-*.json 없음. collect_metrics.sh 먼저 실행.\n")
        sys.exit(1)
    return Path(files[-1])


def derive_severity(metrics: dict) -> str:
    prom = metrics.get("prometheus", {})

    # 노드 메모리 압박
    if any(v.get("value", 0) == 1 for v in prom.get("nodeMemoryPressure", [])):
        return "WARNING"

    # Pod 재시작 5회 이상
    if any((v.get("value") or 0) >= 5 for v in prom.get("podRestarts", [])):
        return "WARNING"

    # SQS 큐 깊이 1000 이상
    sqs = metrics.get("sqs", {})
    if int(sqs.get("ApproximateNumberOfMessages", 0)) >= 1000:
        return "NOTICE"

    return "INFO"


def build_payload(metrics: dict, src: Path) -> dict:
    """Cloud Logging 필터용 요약 필드 + 원본 스냅샷 포함."""
    nodes = metrics.get("nodes", [])
    deployments = metrics.get("deployments", [])
    prom = metrics.get("prometheus", {})
    sqs = metrics.get("sqs", {})
    cw_rds = metrics.get("cloudwatchRds", {})
    cw_redis = metrics.get("cloudwatchRedis", {})

    # HPA 현재 replica 요약
    hpa_summary = [
        {
            "name": h.get("name"),
            "current": h.get("currentReplicas"),
            "desired": h.get("desiredReplicas"),
            "max": h.get("maxReplicas"),
        }
        for h in metrics.get("hpa", [])
    ]

    # Pod 재시작 요약 (5회 이상만)
    restarts_alert = [
        v for v in prom.get("podRestarts", [])
        if (v.get("value") or 0) >= 5
    ]

    return {
        "source": src.name,
        "collectedAt": metrics.get("timestamp", datetime.utcnow().isoformat() + "Z"),
        "pushedAt": datetime.utcnow().isoformat() + "Z",
        "namespace": metrics.get("namespace", "ticketing"),
        "region": metrics.get("region", "ap-northeast-2"),
        "summary": {
            "nodeCount": len(nodes),
            "deployments": [
                {
                    "name": d.get("name"),
                    "desired": d.get("desired"),
                    "available": d.get("available"),
                }
                for d in deployments
            ],
            "hpa": hpa_summary,
            "sqsDepth": {
                "waiting": int(sqs.get("ApproximateNumberOfMessages", 0)),
                "inFlight": int(sqs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            },
            "restartsAlert": restarts_alert,
        },
        "prometheus": {
            "cpuRatePerContainer": prom.get("cpuRatePerContainer", []),
            "memBytesPerContainer": prom.get("memBytesPerContainer", []),
            "podRestarts": prom.get("podRestarts", []),
            "hpaDesiredTrend15m": prom.get("hpaDesiredTrend15m", []),
            "httpRpsPerPod": prom.get("httpRpsPerPod", []),
            "nodeMemoryPressure": prom.get("nodeMemoryPressure", []),
        },
        "cloudwatch": {
            "rds": cw_rds,
            "redis": cw_redis,
        },
    }


def push(gcloud: str, log_name: str, severity: str, payload: dict) -> None:
    cmd = [
        gcloud, "logging", "write", log_name,
        json.dumps(payload, ensure_ascii=False),
        "--payload-type=json",
        f"--severity={severity}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"[push] 실패:\n{r.stderr}\n")
        sys.exit(r.returncode)
    if r.stderr.strip():
        print(r.stderr.strip())


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_metrics()
    metrics = json.loads(src.read_text(encoding="utf-8"))
    severity = derive_severity(metrics)
    payload = build_payload(metrics, src)

    gcloud = find_gcloud()
    print(f"[push] source:   {src}")
    print(f"[push] logName:  {LOG_NAME}")
    print(f"[push] severity: {severity}")
    push(gcloud, LOG_NAME, severity, payload)

    project = subprocess.run(
        [gcloud, "config", "get-value", "project"],
        capture_output=True, text=True,
    ).stdout.strip()
    print(f"[push] 전송 완료. Logs Explorer 에서 확인:")
    print(
        f"  https://console.cloud.google.com/logs/query;"
        f"query=logName%3D%22projects%2F{project}%2Flogs%2F{LOG_NAME}%22"
        f"?project={project}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
