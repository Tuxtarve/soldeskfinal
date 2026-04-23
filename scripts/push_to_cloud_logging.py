#!/usr/bin/env python3
"""Gemini 추천 JSON을 GCP Cloud Logging 으로 전송.

조회:
  GCP 콘솔 → Logging → Logs Explorer
  쿼리: logName="projects/soldesk-gcp/logs/gemini-recommendations"

입력:
  scripts/data/recommendation-*.json  (최신 자동 선택 또는 인자)

요구:
  - gcloud 가 PATH 에 있어야 함
  - `gcloud auth login` + `gcloud config set project soldesk-gcp` 완료
  - logging.googleapis.com 활성화

사용:
  python3 scripts/push_to_cloud_logging.py
  python3 scripts/push_to_cloud_logging.py scripts/data/recommendation-xxxx.json

환경변수:
  GCP_LOG_NAME   (기본: gemini-recommendations)
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
LOG_NAME = os.environ.get("GCP_LOG_NAME", "gemini-recommendations")
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
    """Logs Explorer 에서 필터하기 좋게 평탄화된 필드 추가."""
    recs = rec.get("recommendations", [])
    cost = rec.get("estimatedCostDelta") or {}
    return {
        "source": src.name,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "summary": rec.get("summary", ""),
        "counts": {
            "total": len(recs),
            "now":   sum(1 for r in recs if r.get("priority") == "now"),
            "watch": sum(1 for r in recs if r.get("priority") == "watch"),
            "later": sum(1 for r in recs if r.get("priority") == "later"),
            "highRisk": sum(1 for r in recs if r.get("risk") == "high"),
        },
        "estimatedCostDelta": cost,
        "warnings": rec.get("warnings", []),
        "openQuestions": rec.get("openQuestions", []),
        "recommendations": recs,
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
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_recommendation()
    rec = json.loads(src.read_text(encoding="utf-8"))
    severity = derive_severity(rec)
    payload = build_payload(rec, src)

    gcloud = find_gcloud()
    print(f"[push] source:   {src}")
    print(f"[push] logName:  projects/$(project)/logs/{LOG_NAME}")
    print(f"[push] severity: {severity}")
    push(gcloud, LOG_NAME, severity, payload)

    project = subprocess.run([gcloud, "config", "get-value", "project"],
                              capture_output=True, text=True).stdout.strip()
    print(f"[push] 전송 완료. Logs Explorer 에서 확인:")
    print(f"  https://console.cloud.google.com/logs/query;"
          f'query=logName%3D%22projects%2F{project}%2Flogs%2F{LOG_NAME}%22'
          f"?project={project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
