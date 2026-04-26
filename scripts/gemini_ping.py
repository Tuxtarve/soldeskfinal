#!/usr/bin/env python3
"""Gemini API 연결 확인용 핑 스크립트.

사용:
    export GEMINI_API_KEY=...   # 또는 `source .env.local`
    python3 scripts/gemini_ping.py
    python3 scripts/gemini_ping.py "원하는 프롬프트"
"""
import os
import sys

try:
    from google import genai
except ImportError:
    sys.stderr.write(
        "google-genai 패키지가 없습니다. 설치:\n"
        "  pip install google-genai --break-system-packages\n"
    )
    sys.exit(2)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.stderr.write("GEMINI_API_KEY 환경변수가 비어 있습니다.\n")
        return 1

    prompt = sys.argv[1] if len(sys.argv) > 1 else (
        "한 문장으로 자기소개 해줘. 한국어로."
    )

    client = genai.Client(api_key=api_key)

    print(f"[model] {MODEL}")
    print(f"[prompt] {prompt}")
    print("-" * 40)

    resp = client.models.generate_content(model=MODEL, contents=prompt)
    print(resp.text.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
