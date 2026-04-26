#!/usr/bin/env python3
"""Gemini 추천 JSON을 GCP Cloud Logging 으로 전송.
gcloud CLI 불필요 — google-cloud-logging SDK + ADC 사용.
Windows / macOS / Linux 전부 동작.

인증:
  gcloud auth application-default login  (한 번만)

조회:
  GCP 콘솔 → Logging → Logs Explorer
  쿼리: logName="projects/soldesk-gcp/logs/gemini-recommendations"

사용:
  python3 scripts/push_to_cloud_logging.py
  python3 scripts/push_to_cloud_logging.py scripts/data/recommendation-xxxx.json

환경변수:
  GCP_PROJECT_ID  (기본: soldesk-gcp)
  GCP_LOG_NAME    (기본: gemini-recommendations)
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

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "soldesk-gcp")
LOG_NAME   = os.environ.get("GCP_LOG_NAME",   "gemini-recommendations")


def latest_recommendation() -> Path:
    files = sorted(glob.glob(str(DATA_DIR / "recommendation-*.json")))
    if not files:
        sys.stderr.write("recommendation-*.json 없음.\n")
        sys.exit(1)
    return Path(files[-1])


def derive_severity(rec: dict) -> str:
    recs = rec.get("recommendations", [])
    if any(r.get("risk") == "high" for r in recs):
        return "WARNING"
    if any(r.get("priority") == "now" for r in recs):
        return "NOTICE"
    return "INFO"


def build_payload(rec: dict, src: Path) -> dict:
    recs = rec.get("recommendations", [])
    cost = rec.get("estimatedCostDelta") or {}
    return {
        "source":    src.name,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "summary":   rec.get("summary", ""),
        "counts": {
            "total":    len(recs),
            "now":      sum(1 for r in recs if r.get("priority") == "now"),
            "watch":    sum(1 for r in recs if r.get("priority") == "watch"),
            "later":    sum(1 for r in recs if r.get("priority") == "later"),
            "highRisk": sum(1 for r in recs if r.get("risk") == "high"),
        },
        "estimatedCostDelta": cost,
        "warnings":      rec.get("warnings",       []),
        "openQuestions": rec.get("openQuestions",   []),
        "recommendations": recs,
    }


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_recommendation()
    rec = json.loads(src.read_text(encoding="utf-8"))

    severity = derive_severity(rec)
    payload  = build_payload(rec, src)

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
