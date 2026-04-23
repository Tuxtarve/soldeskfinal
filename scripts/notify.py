#!/usr/bin/env python3
"""Gemini 추천 중 `priority: now` 가 있으면 Slack 으로 알림.

입력:
  scripts/data/recommendation-*.json  (최신 자동 선택 또는 인자)

환경변수:
  SLACK_WEBHOOK_URL   (필수) — 없으면 stdout 출력만

사용:
  python3 scripts/notify.py
  python3 scripts/notify.py scripts/data/recommendation-xxxx.json
  ONLY_NEW=1 python3 scripts/notify.py   # 이전에 알림 보낸 파일은 재전송 안 함

파이프라인 예시:
  scripts/collect_metrics.sh && python3 scripts/recommend_scaling.py && python3 scripts/notify.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SENT_LOG = DATA_DIR / ".notify-sent.log"


def latest_recommendation() -> Path:
    files = sorted(glob.glob(str(DATA_DIR / "recommendation-*.json")))
    if not files:
        sys.stderr.write("recommendation-*.json 없음.\n")
        sys.exit(1)
    return Path(files[-1])


def already_sent(path: Path) -> bool:
    if not SENT_LOG.exists():
        return False
    return path.name in SENT_LOG.read_text(encoding="utf-8").splitlines()


def mark_sent(path: Path) -> None:
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SENT_LOG.open("a", encoding="utf-8") as f:
        f.write(path.name + "\n")


def build_slack_payload(rec: dict, src: Path) -> dict:
    now_items = [r for r in rec.get("recommendations", []) if r.get("priority") == "now"]
    high_risk = [r for r in now_items if r.get("risk") == "high"]
    warnings = rec.get("warnings", [])
    cost = rec.get("estimatedCostDelta") or {}

    header = f":robot_face: Gemini 오토스케일 추천 — NOW {len(now_items)}건"
    if high_risk:
        header += f"  :warning: HIGH risk {len(high_risk)}건"

    fields_text = []
    for r in now_items[:12]:  # Slack message 길이 제한 고려
        fields_text.append(
            f"• *{r['target']}* `{r['field']}`  "
            f"`{r['from']} → {r['to']}`  _{r['risk']}/{r['confidence']}_\n"
            f"   ↳ {r['reason'][:180]}"
        )
    if len(now_items) > 12:
        fields_text.append(f"_(외 {len(now_items)-12}건 생략)_")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*요약*\n{rec.get('summary','')}\n\n*출처*: `{src.name}`"}},
    ]
    if fields_text:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*즉시 적용 권고 (NOW)*\n" + "\n".join(fields_text)}})
    if warnings:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*:warning: 경고*\n" + "\n".join(f"• {w}" for w in warnings[:5])}})
    if cost:
        sign = {"increase": "+", "decrease": "-", "neutral": "±"}.get(cost.get("direction"), "?")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"비용 영향: {sign}${cost.get('approxUSDPerMonth',0):.2f}/월 — {cost.get('rationale','')}"}]})

    return {"text": header, "blocks": blocks}


def post_slack(webhook: str, payload: dict) -> int:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(webhook, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def print_stdout(rec: dict, src: Path) -> None:
    now_items = [r for r in rec.get("recommendations", []) if r.get("priority") == "now"]
    print(f"[notify] SLACK_WEBHOOK_URL 미설정 — stdout 출력")
    print(f"[notify] source: {src}")
    print(f"[notify] NOW 추천 {len(now_items)}건:")
    for r in now_items:
        print(f"  - {r['target']} {r['field']}: {r['from']} → {r['to']}  ({r['risk']}/{r['confidence']})")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_recommendation()
    rec = json.loads(src.read_text(encoding="utf-8"))

    if os.environ.get("ONLY_NEW") and already_sent(src):
        print(f"[notify] skip — 이미 알림 전송됨: {src.name}")
        return 0

    now_items = [r for r in rec.get("recommendations", []) if r.get("priority") == "now"]
    if not now_items:
        print("[notify] NOW 추천 없음 — 알림 건너뜀")
        return 0

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        print_stdout(rec, src)
        return 0

    payload = build_slack_payload(rec, src)
    try:
        status = post_slack(webhook, payload)
    except Exception as e:
        sys.stderr.write(f"[notify] Slack 전송 실패: {e}\n")
        return 1

    print(f"[notify] Slack 전송 완료 (HTTP {status}) — NOW {len(now_items)}건")
    mark_sent(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
