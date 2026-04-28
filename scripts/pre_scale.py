#!/usr/bin/env python3
"""
티켓팅 오픈 사전 스케일링 스크립트.

지정한 오픈 시각 N분 전에 Gemini가 예상 트래픽을 분석하고
HPA maxReplicas / KEDA maxReplicaCount / 최소 replicas 를 미리 증설합니다.
오픈 후 지정 시간이 지나면 자동 복구(scale-down)도 수행합니다.

사용:
  # 오늘 12:00 오픈, 예상 사용자 5000명, 10분 전 자동 스케일
  python3 scripts/pre_scale.py \\
    --event-time 12:00 \\
    --event-name "콘서트 7회차" \\
    --expected-users 5000 \\
    --auto

  # 지금 바로 스케일 적용 (--now)
  python3 scripts/pre_scale.py \\
    --event-name "테스트 오픈" \\
    --expected-users 10000 \\
    --now --auto

  # 수동 검토용 (실제 적용 안 함, kubectl 명령만 출력)
  python3 scripts/pre_scale.py \\
    --event-time 12:00 \\
    --expected-users 3000

옵션:
  --event-time HH:MM      오픈 시각 (기본: 지금부터 10분 후)
  --event-name TEXT       이벤트 이름 (Gemini 분석 컨텍스트)
  --expected-users N      예상 동시 접속자 수
  --pre-minutes N         오픈 N분 전에 스케일 (기본: 10)
  --down-minutes N        오픈 후 N분 뒤 복구 (기본: 30, 0=복구 안 함)
  --auto                  자동 적용 (없으면 명령어만 출력)
  --now                   대기 없이 즉시 스케일 적용
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import boto3
    from google import genai
    from google.genai import types
except ImportError as e:
    sys.exit(f"패키지 누락: {e}\n  pip install boto3 google-genai")

ROOT = Path(__file__).resolve().parent.parent
NS   = os.environ.get("NS", "ticketing")

# ANSI
R = "\033[1;31m"; Y = "\033[1;33m"; G = "\033[1;32m"
C = "\033[1;36m"; W = "\033[1m";    DIM = "\033[2m"; RST = "\033[0m"


# ── 현재 클러스터 상태 수집 ────────────────────────────────────────────────────
def _kubectl(args: list[str]) -> dict:
    try:
        r = subprocess.run(["kubectl"] + args + ["-o", "json"],
                           capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}


def current_state() -> dict:
    hpa_raw = _kubectl(["get", "hpa", "-n", NS])
    hpa = [
        {
            "name":    h["metadata"]["name"],
            "current": h["status"].get("currentReplicas", 0),
            "min":     h["spec"].get("minReplicas", 1),
            "max":     h["spec"].get("maxReplicas", 10),
        }
        for h in hpa_raw.get("items", [])
    ]

    so_raw = _kubectl(["get", "scaledobject", "-n", NS])
    keda = [
        {
            "name":   s["metadata"]["name"],
            "target": s["spec"].get("scaleTargetRef", {}).get("name", ""),
            "min":    s["spec"].get("minReplicaCount", 0),
            "max":    s["spec"].get("maxReplicaCount", 10),
        }
        for s in so_raw.get("items", [])
    ]

    deploy_raw = _kubectl(["get", "deploy", "-n", NS])
    deploys = [
        {
            "name":    d["metadata"]["name"],
            "current": d["status"].get("replicas", 0),
            "desired": d["spec"].get("replicas", 1),
        }
        for d in deploy_raw.get("items", [])
    ]

    nodes_raw = _kubectl(["get", "nodes"])
    node_count = len(nodes_raw.get("items", []))

    return {"hpa": hpa, "keda": keda, "deploys": deploys, "node_count": node_count}


# ── Gemini 사전 스케일 추천 ────────────────────────────────────────────────────
GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "read_api_min_replicas":  {"type": "integer"},
        "write_api_min_replicas": {"type": "integer"},
        "worker_min_replicas":    {"type": "integer"},
        "hpa_read_max":           {"type": "integer"},
        "hpa_write_max":          {"type": "integer"},
        "keda_worker_max":        {"type": "integer"},
        "keda_queue_length":      {"type": "integer"},
        "reason":                 {"type": "string"},
        "warnings":               {"type": "array", "items": {"type": "string"}},
        "down_replicas": {
            "type": "object",
            "properties": {
                "read_api_min":  {"type": "integer"},
                "write_api_min": {"type": "integer"},
                "worker_min":    {"type": "integer"},
                "hpa_read_max":  {"type": "integer"},
                "hpa_write_max": {"type": "integer"},
                "keda_max":      {"type": "integer"},
            },
            "required": ["read_api_min", "write_api_min", "worker_min",
                         "hpa_read_max", "hpa_write_max", "keda_max"],
        },
    },
    "required": [
        "read_api_min_replicas", "write_api_min_replicas", "worker_min_replicas",
        "hpa_read_max", "hpa_write_max", "keda_worker_max", "keda_queue_length",
        "reason", "warnings", "down_replicas",
    ],
}


def _load_thresholds() -> dict:
    """이분탐색으로 측정한 실측 임계값 로드."""
    import glob
    thresholds = {}
    for mode in ["keda", "hpa-read", "hpa-write"]:
        files = sorted(glob.glob(str(ROOT / "scripts" / "data" / f"threshold-{mode}-*.json")))
        if files:
            try:
                d = json.loads(Path(files[-1]).read_text())
                thresholds[mode] = {
                    "safe_max": d.get("threshold_estimate"),
                    "unit": d.get("unit", ""),
                    "file": Path(files[-1]).name,
                }
            except Exception:
                pass
    return thresholds


def _fmt_thresholds(t: dict) -> str:
    if not t:
        return "  (아직 이분탐색 미실행 — find_threshold.py 실행 후 정확도 향상)"
    lines = []
    mapping = {
        "keda":      "KEDA worker-svc 안전 최대 SQS",
        "hpa-read":  "read-api HPA 안전 최대",
        "hpa-write": "write-api HPA 안전 최대",
    }
    for mode, label in mapping.items():
        if mode in t:
            d = t[mode]
            lines.append(f"  {label}: {d['safe_max']} {d['unit']}  (측정파일: {d['file']})")
    return "\n".join(lines) if lines else "  (측정값 없음)"


def ask_gemini(api_key: str, event_name: str, expected_users: int,
               state: dict) -> dict:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    client = genai.Client(api_key=api_key)
    thresholds = _load_thresholds()

    prompt = f"""당신은 Kubernetes 오토스케일링 전문가입니다.
티켓팅 오픈 직전 사전 스케일링 값을 추천해주세요.

## 이벤트 정보
- 이벤트명: {event_name}
- 예상 동시 접속자: {expected_users:,}명

## 시스템 제약 (하드 캡)
- 노드: t3.small × {state['node_count']}대 (총 {state['node_count']*2} vCPU / {state['node_count']*2} GiB)
- 실가용 메모리: ~{state['node_count']*2 - 2} GiB (시스템 파드 제외)
- RDS max_connections: 200 (worker × pool_size + write-api × pool_size ≤ 180)
- SQS FIFO: 그룹당 300 msg/s
- worker-svc 처리량: pod당 5 msg/s (200ms/msg 기준)

## 서비스 역할
- read-api  : 조회 (HPA, CPU 기반). requests 200m/200Mi
- write-api : 예매 요청 → SQS 발행 (HPA, CPU 기반). requests 300m/500Mi
- worker-svc: SQS 소비 → RDS write (KEDA, SQS depth 기반). requests 200m/200Mi

## 현재 설정
HPA: {json.dumps(state['hpa'], ensure_ascii=False)}
KEDA: {json.dumps(state['keda'], ensure_ascii=False)}

## 실측 임계값 (find_threshold.py 이분탐색 결과 — 이 수치를 최우선으로 사용)
{_fmt_thresholds(thresholds)}

## 부하 예측
- 예상 사용자 {expected_users:,}명 동시 접속
- 오픈 순간 write RPS ≈ {min(expected_users // 10, 500)}
- 오픈 순간 read RPS  ≈ {min(expected_users // 5, 2000)}
- SQS 피크 예상: {min(expected_users // 2, 10000)} 메시지

## 요청
오픈 직전에 미리 설정할 값을 추천하세요.
- 실측 임계값을 절대 초과하지 않는 min_replicas/max_replicas 추천
- 실측 임계값 초과가 예상되면 warnings 에 명시
- RDS 커넥션 한계(180) 초과하지 않도록 worker + write-api replicas 합산 고려
- min_replicas 는 워밍업용 (오픈 전 준비), max 는 피크 대응용
- down_replicas 는 오픈 30분 후 복구할 값 (평시 기준)
- 노드 메모리 한계를 초과하지 않도록 총 requests 합산 고려
"""

    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GEMINI_SCHEMA,
            temperature=0.1,
        ),
    )
    return json.loads(resp.text)


# ── 스케일링 적용 ──────────────────────────────────────────────────────────────
def _patch(resource: str, name: str, patch: dict, dry: bool) -> str:
    cmd = ["kubectl", "patch", resource, name, "-n", NS,
           "--type=merge", f"--patch={json.dumps(patch)}"]
    cmd_str = " ".join(cmd)
    if dry:
        return f"  {DIM}[DRY]{RST} {cmd_str}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        icon = "✓" if r.returncode == 0 else "✗"
        return f"  {G if r.returncode==0 else R}{icon}{RST} {resource}/{name} 패치 완료"
    except Exception as ex:
        return f"  {R}✗{RST} {ex}"


def _set_replicas(deploy: str, replicas: int, dry: bool) -> str:
    cmd = ["kubectl", "scale", "deployment", deploy,
           "-n", NS, f"--replicas={replicas}"]
    if dry:
        return f"  {DIM}[DRY]{RST} {' '.join(cmd)}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        icon = "✓" if r.returncode == 0 else "✗"
        return f"  {G if r.returncode==0 else R}{icon}{RST} deploy/{deploy} → {replicas}"
    except Exception as ex:
        return f"  {R}✗{RST} {ex}"


def apply_scale(rec: dict, state: dict, dry: bool) -> list[str]:
    logs = []

    # HPA maxReplicas
    for hpa in state["hpa"]:
        if "read" in hpa["name"]:
            new_max = rec["hpa_read_max"]
        elif "write" in hpa["name"]:
            new_max = rec["hpa_write_max"]
        else:
            continue
        logs.append(_patch("hpa", hpa["name"],
                            {"spec": {"maxReplicas": new_max}}, dry))

    # HPA minReplicas (워밍업)
    for hpa in state["hpa"]:
        if "read" in hpa["name"]:
            new_min = rec["read_api_min_replicas"]
        elif "write" in hpa["name"]:
            new_min = rec["write_api_min_replicas"]
        else:
            continue
        logs.append(_patch("hpa", hpa["name"],
                            {"spec": {"minReplicas": new_min}}, dry))

    # KEDA maxReplicaCount + queueLength
    for so in state["keda"]:
        logs.append(_patch("scaledobject", so["name"], {
            "spec": {
                "maxReplicaCount": rec["keda_worker_max"],
                "triggers": [{
                    "type": "aws-sqs-queue",
                    "metadata": {"queueLength": str(rec["keda_queue_length"])}
                }]
            }
        }, dry))

    # worker stable deployment 최소 replicas
    for d in state["deploys"]:
        if d["name"] == "worker-svc":
            logs.append(_set_replicas("worker-svc",
                                      rec["worker_min_replicas"], dry))

    return logs


def apply_down(rec: dict, state: dict, dry: bool) -> list[str]:
    """오픈 후 복구용 (down_replicas)."""
    dr = rec["down_replicas"]
    logs = []
    for hpa in state["hpa"]:
        if "read" in hpa["name"]:
            logs.append(_patch("hpa", hpa["name"], {
                "spec": {"minReplicas": dr["read_api_min"],
                         "maxReplicas": dr["hpa_read_max"]}}, dry))
        elif "write" in hpa["name"]:
            logs.append(_patch("hpa", hpa["name"], {
                "spec": {"minReplicas": dr["write_api_min"],
                         "maxReplicas": dr["hpa_write_max"]}}, dry))
    for so in state["keda"]:
        logs.append(_patch("scaledobject", so["name"], {
            "spec": {"maxReplicaCount": dr["keda_max"]}}, dry))
    logs.append(_set_replicas("worker-svc", dr["worker_min"], dry))
    return logs


# ── 출력 ──────────────────────────────────────────────────────────────────────
def print_rec(rec: dict, event_name: str, expected_users: int,
              event_dt: datetime, pre_minutes: int) -> None:
    print(f"\n{W}{'─'*56}{RST}")
    print(f"{W} Gemini 사전 스케일링 추천{RST}")
    print(f"{W}{'─'*56}{RST}")
    print(f"  이벤트  : {event_name}")
    print(f"  오픈    : {event_dt.strftime('%H:%M')}  "
          f"(현재~오픈까지 {int((event_dt-datetime.now()).total_seconds()/60)}분)")
    print(f"  예상    : {expected_users:,}명 동시 접속\n")

    print(f"  {C}[오픈 {pre_minutes}분 전 적용]{RST}")
    print(f"  read-api  minReplicas → {W}{rec['read_api_min_replicas']}{RST}  "
          f"maxReplicas → {W}{rec['hpa_read_max']}{RST}")
    print(f"  write-api minReplicas → {W}{rec['write_api_min_replicas']}{RST}  "
          f"maxReplicas → {W}{rec['hpa_write_max']}{RST}")
    print(f"  worker-svc replicas   → {W}{rec['worker_min_replicas']}{RST}  "
          f"maxReplicas → {W}{rec['keda_worker_max']}{RST}")
    print(f"  KEDA queueLength      → {W}{rec['keda_queue_length']}{RST}")

    dr = rec["down_replicas"]
    print(f"\n  {DIM}[오픈 후 복구 값]{RST}")
    print(f"  read-api  min={dr['read_api_min']}  max={dr['hpa_read_max']}")
    print(f"  write-api min={dr['write_api_min']}  max={dr['hpa_write_max']}")
    print(f"  worker    min={dr['worker_min']}  max={dr['keda_max']}")

    print(f"\n  {Y}근거:{RST} {rec['reason']}")
    for w in rec.get("warnings", []):
        print(f"  {R}주의:{RST} {w}")
    print(f"{W}{'─'*56}{RST}\n")


# ── 카운트다운 + 실행 ──────────────────────────────────────────────────────────
def countdown_and_run(scale_dt: datetime, event_dt: datetime,
                      rec: dict, state: dict,
                      auto: bool, down_minutes: int) -> None:
    now = datetime.now()
    wait_sec = (scale_dt - now).total_seconds()

    if wait_sec > 0:
        print(f"\n{Y}사전 스케일링 대기 중...{RST}")
        print(f"  스케일 적용 시각 : {scale_dt.strftime('%H:%M:%S')}")
        print(f"  오픈 시각        : {event_dt.strftime('%H:%M:%S')}")
        while True:
            remaining = (scale_dt - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            m, s = divmod(int(remaining), 60)
            print(f"\r  남은 시간: {Y}{m:02d}:{s:02d}{RST}  ", end="", flush=True)
            time.sleep(1)
        print()

    # 스케일 UP 적용
    print(f"\n{G}▶ 사전 스케일링 실행{RST}  "
          f"{'(자동 적용)' if auto else '(DRY-RUN — --auto 옵션으로 실제 적용)'}")
    logs = apply_scale(rec, state, dry=not auto)
    for log in logs:
        print(log)

    if not auto:
        return

    # 오픈 후 복구 대기
    if down_minutes > 0:
        down_dt = event_dt + timedelta(minutes=down_minutes)
        wait_down = (down_dt - datetime.now()).total_seconds()
        if wait_down > 0:
            print(f"\n{DIM}오픈 후 {down_minutes}분 뒤 자동 복구 대기 중... "
                  f"({down_dt.strftime('%H:%M')}){RST}")
            while True:
                r2 = (down_dt - datetime.now()).total_seconds()
                if r2 <= 0:
                    break
                m, s = divmod(int(r2), 60)
                print(f"\r  복구까지: {DIM}{m:02d}:{s:02d}{RST}  ", end="", flush=True)
                time.sleep(1)
            print()

        print(f"\n{C}▶ 복구 스케일링 실행{RST}")
        # 복구 시점의 최신 상태 재수집
        fresh_state = current_state()
        down_logs = apply_down(rec, fresh_state, dry=False)
        for log in down_logs:
            print(log)
        print(f"\n{G}✓ 복구 완료{RST}")


# ── 메인 ──────────────────────────────────────────────────────────────────────
def main() -> int:
    # .env.local 로드
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        sys.exit("GEMINI_API_KEY 미설정 — .env.local 확인")

    parser = argparse.ArgumentParser(description="티켓팅 오픈 사전 스케일링")
    parser.add_argument("--event-time",     default=None,
                        help="오픈 시각 HH:MM (기본: 지금+10분)")
    parser.add_argument("--event-name",     default="티켓팅 오픈",
                        help="이벤트 이름 (Gemini 컨텍스트용)")
    parser.add_argument("--expected-users", type=int, default=5000,
                        help="예상 동시 접속자 수 (기본: 5000)")
    parser.add_argument("--pre-minutes",    type=int, default=10,
                        help="오픈 N분 전에 스케일 (기본: 10)")
    parser.add_argument("--down-minutes",   type=int, default=30,
                        help="오픈 후 N분 뒤 복구 (0=복구 안 함, 기본: 30)")
    parser.add_argument("--auto",           action="store_true",
                        help="자동 적용 (없으면 명령어만 출력)")
    parser.add_argument("--now",            action="store_true",
                        help="대기 없이 즉시 스케일 적용")
    args = parser.parse_args()

    # 시각 계산
    now = datetime.now()
    if args.now or args.event_time is None:
        event_dt = now + timedelta(minutes=args.pre_minutes + 1)
        scale_dt = now
    else:
        h, m = map(int, args.event_time.split(":"))
        event_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if event_dt <= now:
            event_dt += timedelta(days=1)
        scale_dt = event_dt - timedelta(minutes=args.pre_minutes)
        if scale_dt <= now:
            scale_dt = now  # 이미 지났으면 즉시

    print(f"\n{W}{'='*56}{RST}")
    print(f"{W} 티켓팅 사전 스케일링 준비{RST}")
    print(f"{W}{'='*56}{RST}")
    print(f"  이벤트  : {args.event_name}")
    print(f"  오픈    : {event_dt.strftime('%Y-%m-%d %H:%M')}")
    print(f"  스케일  : {scale_dt.strftime('%H:%M')} "
          f"(오픈 {args.pre_minutes}분 전)")
    print(f"  예상    : {args.expected_users:,}명")
    print(f"  모드    : {'자동 적용' if args.auto else 'DRY-RUN (명령어 출력만)'}")

    # 현재 상태 수집
    print(f"\n{DIM}현재 클러스터 상태 수집 중...{RST}")
    state = current_state()
    print(f"  HPA  : {[h['name'] for h in state['hpa']]}")
    print(f"  KEDA : {[s['name'] for s in state['keda']]}")
    print(f"  노드  : {state['node_count']}대")

    # Gemini 추천
    print(f"\n{DIM}Gemini 분석 중...{RST}")
    try:
        rec = ask_gemini(api_key, args.event_name, args.expected_users, state)
    except Exception as ex:
        sys.exit(f"Gemini 오류: {ex}")

    # 추천 출력
    print_rec(rec, args.event_name, args.expected_users, event_dt, args.pre_minutes)

    # 결과 저장
    out_dir = ROOT / "scripts" / "data"
    out_dir.mkdir(exist_ok=True)
    ts = now.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"pre-scale-{ts}.json"
    out_path.write_text(json.dumps(
        {"event": args.event_name, "expected_users": args.expected_users,
         "event_time": event_dt.isoformat(), "scale_time": scale_dt.isoformat(),
         "recommendation": rec, "current_state": state},
        ensure_ascii=False, indent=2
    ))
    print(f"  결과 저장: {out_path}")

    # 카운트다운 + 실행
    try:
        countdown_and_run(scale_dt, event_dt, rec, state,
                          args.auto, args.down_minutes)
    except KeyboardInterrupt:
        print(f"\n{Y}중단됨{RST}")
        return 1

    if not args.auto:
        print(f"\n{Y}※ 위 내용을 검토 후 --auto 옵션을 추가해서 실행하세요.{RST}")
        print(f"  python3 scripts/pre_scale.py "
              f"--event-time {event_dt.strftime('%H:%M')} "
              f"--event-name \"{args.event_name}\" "
              f"--expected-users {args.expected_users} --auto\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
