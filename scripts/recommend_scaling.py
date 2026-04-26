#!/usr/bin/env python3
"""Gemini 기반 EKS 오토스케일/튜닝 추천기.

입력:
  - scripts/context/system.md       (정적 컨텍스트)
  - scripts/data/metrics-*.json     (collect_metrics.sh 가 만든 최신 스냅샷)

출력:
  - scripts/data/recommendation-<ts>.json  (스키마 강제된 Gemini 응답)
  - stdout: 사람이 읽기 좋은 요약 표

사용:
  python3 scripts/recommend_scaling.py
  python3 scripts/recommend_scaling.py scripts/data/metrics-20260423-165041.json
환경변수:
  GEMINI_API_KEY  (필수)
  GEMINI_MODEL    (기본: gemini-2.5-flash, 품질 올리려면 gemini-2.5-pro)
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from google import genai
    from google.genai import types
except ImportError:
    sys.stderr.write("pip install google-genai --break-system-packages 필요\n")
    sys.exit(2)

ROOT = Path(__file__).resolve().parent
CONTEXT_FILE = ROOT / "context" / "system.md"
DATA_DIR = ROOT / "data"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ---------- 응답 JSON 스키마 (system.md §J 와 일치) ----------
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "field": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "priority": {"type": "string", "enum": ["now", "watch", "later"]},
                    "risk": {"type": "string", "enum": ["none", "low", "medium", "high"]},
                },
                "required": ["target", "field", "from", "to", "reason", "confidence", "priority", "risk"],
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
        "estimatedCostDelta": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["increase", "decrease", "neutral"]},
                "approxUSDPerMonth": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["direction", "approxUSDPerMonth", "rationale"],
        },
        "openQuestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "recommendations", "warnings", "estimatedCostDelta", "openQuestions"],
}


def latest_metrics() -> Path:
    candidates = sorted(glob.glob(str(DATA_DIR / "metrics-*.json")))
    if not candidates:
        sys.stderr.write(f"{DATA_DIR}/metrics-*.json 없음. 먼저 scripts/collect_metrics.sh 실행.\n")
        sys.exit(1)
    return Path(candidates[-1])


def build_prompt(context_md: str, metrics_json: dict) -> str:
    return f"""{context_md}

---

## L. 현재 메트릭 스냅샷 (DYNAMIC)

```json
{json.dumps(metrics_json, ensure_ascii=False, indent=2)}
```

---

## M. 요청

§H 의 12개 항목을 기준으로 §K 스키마에 맞춰 추천하라.
- 서비스별(read-api / write-api / worker-svc)로 타겟을 분리하라.
- §G 고정 결정은 변경 금지. §I 범위 밖 금지.
- 파괴적 변경(`risk:"high"`)은 `priority:"watch"` 로만 제안.
- 확신 없으면 `confidence:"low"` + openQuestions 에 추가.
- `from`/`to` 는 문자열로 표기 (예: "23", "10", "55%", "100m", "256Mi").
- §J 의 필드 해설을 참고해 prometheus / cloudwatch 데이터를 적극 활용하라.
- 빈 배열/객체 필드는 수집 실패로 간주하고 해당 항목 추천에서 제외하라.
"""


def call_gemini(prompt: str, api_key: str) -> dict:
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


# ---------- 사람이 읽는 요약 ----------
def _color(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def _priority_tag(p: str) -> str:
    return {
        "now":   _color("NOW  ", "1;31"),
        "watch": _color("WATCH", "1;33"),
        "later": _color("LATER", "1;34"),
    }.get(p, p)


def _risk_tag(r: str) -> str:
    return {
        "none":   _color("·",    "2;37"),
        "low":    _color("low",  "0;32"),
        "medium": _color("med",  "0;33"),
        "high":   _color("HIGH", "1;31"),
    }.get(r, r)


def print_summary(rec: dict) -> None:
    print()
    print(_color("=== Gemini 추천 요약 ===", "1"))
    print(f"요약: {rec.get('summary', '')}")
    print()

    items = rec.get("recommendations", [])
    if items:
        print(f"{'PRI':<6} {'RISK':<5} {'CONF':<6} {'TARGET':<32} {'FIELD':<32} {'FROM → TO'}")
        print("-" * 120)
        for r in items:
            line = (
                f"{_priority_tag(r['priority']):<16} "
                f"{_risk_tag(r['risk']):<14} "
                f"{r['confidence']:<6} "
                f"{r['target']:<32} "
                f"{r['field']:<32} "
                f"{r['from']} → {r['to']}"
            )
            print(line)
            print(f"       ↳ {r['reason']}")

    warnings = rec.get("warnings", [])
    if warnings:
        print()
        print(_color("경고:", "1;33"))
        for w in warnings:
            print(f"  - {w}")

    cd = rec.get("estimatedCostDelta") or {}
    if cd:
        print()
        sign = {"increase": "+", "decrease": "-", "neutral": "±"}.get(cd.get("direction"), "?")
        print(f"비용 영향: {sign}${cd.get('approxUSDPerMonth', 0):.2f}/월 — {cd.get('rationale', '')}")

    oq = rec.get("openQuestions", [])
    if oq:
        print()
        print(_color("추가로 확인 필요:", "1;36"))
        for q in oq:
            print(f"  - {q}")
    print()


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.stderr.write("GEMINI_API_KEY 미설정\n")
        return 1

    if not CONTEXT_FILE.exists():
        sys.stderr.write(f"컨텍스트 파일 없음: {CONTEXT_FILE}\n")
        return 1

    metrics_path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_metrics()
    if not metrics_path.exists():
        sys.stderr.write(f"스냅샷 없음: {metrics_path}\n")
        return 1

    context_md = CONTEXT_FILE.read_text(encoding="utf-8")
    metrics_json = json.loads(metrics_path.read_text(encoding="utf-8"))

    print(f"[*] model={MODEL}")
    print(f"[*] context={CONTEXT_FILE}")
    print(f"[*] metrics={metrics_path}")
    print("[*] Gemini 호출 중...")

    prompt = build_prompt(context_md, metrics_json)
    rec = call_gemini(prompt, api_key)

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_path = DATA_DIR / f"recommendation-{ts}.json"
    out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] saved {out_path}")

    print_summary(rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
