#!/usr/bin/env python3
"""
실시간 예측 기반 자동 스케일링 제어기.
모니터링은 Grafana/Prometheus 에 맡기고, 이 스크립트는 아래만 담당합니다.

  1. 핵심 메트릭 수집  — SQS / RDS / HPA / KEDA / Pending Pod
  2. 증가율 계산       — 슬라이딩 윈도우 rate
  3. 임계 도달 시간 예측 — (threshold - current) / rate
  4. 병목 분류         — 규칙 기반 (CRITICAL / WARNING / OK)
  5. 스케일링 결정 & 실행 — kubectl patch HPA·KEDA
  6. 행동 로그 출력    — 예측 수치 + 실행한 스케일링

사용:
  source .env.local
  python3 scripts/realtime_monitor.py              # 모니터링 + 예측만 (read-only)
  python3 scripts/realtime_monitor.py --auto       # 예측 + 자동 스케일링
  python3 scripts/realtime_monitor.py --interval 10 --auto
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import boto3
except ImportError:
    sys.exit("pip install boto3 필요")

ROOT = Path(__file__).resolve().parent.parent

# ── 설정 ──────────────────────────────────────────────────────────────────────
NS         = os.environ.get("NS", "ticketing")
REGION     = os.environ.get("AWS_REGION", "ap-northeast-2")
QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "ticketing-reservation.fifo")
RDS_ID     = "prod-ticketing-writer"

THRESHOLDS = {
    "sqs_backlog": 1000,   # 메시지 수
    "rds_conn":     180,   # max_connections 200 - 여유 20
    "node_cpu_pct":  80,   # %
    "node_mem_pct":  85,   # %
}

SCALE_AHEAD_SEC = 30   # 임계 도달 N초 전에 스케일링 실행
RATE_WINDOW     = 6    # 증가율 계산에 쓸 샘플 수

# ANSI
R = "\033[1;31m"; Y = "\033[1;33m"; G = "\033[1;32m"
C = "\033[1;36m"; W = "\033[1m";    DIM = "\033[2m"; RST = "\033[0m"


# ── 증가율 & ETA 추적기 ────────────────────────────────────────────────────────
class Tracker:
    def __init__(self, window: int = RATE_WINDOW):
        self._q: deque[tuple[float, float]] = deque(maxlen=window)

    def push(self, val: float) -> None:
        self._q.append((time.monotonic(), val))

    def current(self) -> Optional[float]:
        return self._q[-1][1] if self._q else None

    def rate(self) -> float:
        """값/초. 샘플 부족하거나 시간 0이면 0."""
        if len(self._q) < 2:
            return 0.0
        t0, v0 = self._q[0]
        t1, v1 = self._q[-1]
        dt = t1 - t0
        return (v1 - v0) / dt if dt > 0 else 0.0

    def eta(self, threshold: float) -> Optional[float]:
        """임계까지 남은 초. 감소 중이거나 이미 초과면 None."""
        cur = self.current()
        r   = self.rate()
        if cur is None or r <= 0:
            return None
        gap = threshold - cur
        return None if gap <= 0 else gap / r


# ── 메트릭 수집 ────────────────────────────────────────────────────────────────
def _kubectl(args: list[str]) -> dict:
    try:
        r = subprocess.run(["kubectl"] + args + ["-o", "json"],
                           capture_output=True, text=True, timeout=10)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}


def _cw_latest(cw, namespace, metric, dims, minutes=5) -> Optional[float]:
    try:
        now = datetime.now(timezone.utc)
        r = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric, Dimensions=dims,
            StartTime=datetime.fromtimestamp(now.timestamp() - minutes * 60,
                                             tz=timezone.utc).isoformat(),
            EndTime=now.isoformat(), Period=60, Statistics=["Average"]
        )
        pts = sorted(r.get("Datapoints", []),
                     key=lambda x: str(x.get("Timestamp", "")))
        return round(pts[-1]["Average"], 1) if pts else None
    except Exception:
        return None


def collect(sqs_client, cw_client) -> dict:
    snap: dict = {}

    # SQS
    try:
        url = sqs_client.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
        a = sqs_client.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"]
        )["Attributes"]
        snap["sqs_backlog"]  = int(a.get("ApproximateNumberOfMessages", 0))
        snap["sqs_inflight"] = int(a.get("ApproximateNumberOfMessagesNotVisible", 0))
    except Exception:
        snap["sqs_backlog"] = snap["sqs_inflight"] = 0

    # RDS 커넥션 (CloudWatch)
    v = _cw_latest(cw_client, "AWS/RDS", "DatabaseConnections",
                   [{"Name": "DBInstanceIdentifier", "Value": RDS_ID}])
    snap["rds_conn"] = int(v) if v is not None else 0

    # HPA
    hpa_raw = _kubectl(["get", "hpa", "-n", NS])
    snap["hpa"] = [
        {
            "name":    h["metadata"]["name"],
            "current": h["status"].get("currentReplicas", 0),
            "desired": h["status"].get("desiredReplicas", 0),
            "max":     h["spec"].get("maxReplicas", 10),
            "cpu_pct": next(
                (m["resource"].get("current", {}).get("averageUtilization") or
                 m["resource"].get("currentAverageUtilization", 0)
                 for m in (h["status"].get("currentMetrics") or [])
                 if m.get("type") == "Resource" and
                    m.get("resource", {}).get("name") == "cpu"),
                0
            ),
        }
        for h in hpa_raw.get("items", [])
    ]

    # KEDA ScaledObjects
    so_raw = _kubectl(["get", "scaledobject", "-n", NS])
    snap["keda"] = [
        {
            "name":   s["metadata"]["name"],
            "target": s["spec"].get("scaleTargetRef", {}).get("name", ""),
            "min":    s["spec"].get("minReplicaCount", 0),
            "max":    s["spec"].get("maxReplicaCount", 10),
            "q_thr":  int(s["spec"].get("triggers", [{}])[0]
                          .get("metadata", {}).get("queueLength", 1)),
        }
        for s in so_raw.get("items", [])
    ]

    # Pending pods
    pods_raw = _kubectl(["get", "pods", "-n", NS])
    snap["pending_pods"] = sum(
        1 for p in pods_raw.get("items", [])
        if p["status"].get("phase") == "Pending"
    )

    # Node 수
    nodes_raw = _kubectl(["get", "nodes"])
    snap["node_count"] = len(nodes_raw.get("items", []))

    # Node CPU/MEM (kubectl top)
    try:
        r = subprocess.run(["kubectl", "top", "nodes", "--no-headers"],
                           capture_output=True, text=True, timeout=10)
        cpus, mems = [], []
        for line in r.stdout.strip().splitlines():
            p = line.split()
            if len(p) >= 5:
                cpus.append(float(p[2].rstrip("%")))
                mems.append(float(p[4].rstrip("%")))
        snap["node_cpu_pct"] = round(sum(cpus) / len(cpus), 1) if cpus else 0
        snap["node_mem_pct"] = round(sum(mems) / len(mems), 1) if mems else 0
    except Exception:
        snap["node_cpu_pct"] = snap["node_mem_pct"] = 0

    return snap


# ── 병목 분류 (규칙 기반) ─────────────────────────────────────────────────────
Level = str  # "CRITICAL" | "WARNING" | "OK"

def classify(snap: dict, trackers: dict) -> list[tuple[Level, str]]:
    results: list[tuple[Level, str]] = []

    def _check(key: str, label: str, warn_ratio: float = 0.75):
        val = snap.get(key, 0)
        thr = THRESHOLDS[key]
        eta = trackers[key].eta(thr)
        r   = trackers[key].rate()
        rate_str = f"{r:+.1f}/s" if abs(r) > 0.01 else "±0"
        eta_str  = f"→ {eta:.0f}s" if eta is not None and eta < 120 else ""
        if val >= thr:
            results.append(("CRITICAL", f"{label}={val} 임계({thr}) 초과 {rate_str} {eta_str}"))
        elif val >= thr * warn_ratio or (eta is not None and eta < SCALE_AHEAD_SEC * 2):
            results.append(("WARNING",  f"{label}={val} / {thr} {rate_str} {eta_str}"))
        else:
            results.append(("OK",       f"{label}={val} / {thr} {rate_str}"))

    _check("sqs_backlog",  "SQS backlog")
    _check("rds_conn",     "RDS conn",    warn_ratio=0.80)
    _check("node_cpu_pct", "Node CPU%",   warn_ratio=0.80)
    _check("node_mem_pct", "Node MEM%",   warn_ratio=0.80)

    if snap.get("pending_pods", 0) > 0:
        results.append(("WARNING", f"Pending Pods={snap['pending_pods']} (CA 필요)"))

    return results


# ── 자동 스케일링 결정 & 실행 ─────────────────────────────────────────────────
def _patch(resource: str, name: str, patch: dict) -> bool:
    try:
        r = subprocess.run(
            ["kubectl", "patch", resource, name, "-n", NS,
             "--type=merge", f"--patch={json.dumps(patch)}"],
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0
    except Exception:
        return False


def decide_and_scale(snap: dict, trackers: dict, auto: bool) -> list[str]:
    actions: list[str] = []

    # ── KEDA: SQS backlog ETA 기반 ──────────────────────────────────────────
    eta = trackers["sqs_backlog"].eta(THRESHOLDS["sqs_backlog"])
    if eta is not None and eta < SCALE_AHEAD_SEC:
        for so in snap.get("keda", []):
            new_max = min(so["max"] + 5, 39)
            if new_max > so["max"]:
                reason = f"SQS ETA {eta:.0f}s < {SCALE_AHEAD_SEC}s"
                if auto:
                    ok = _patch("scaledobject", so["name"],
                                {"spec": {"maxReplicaCount": new_max}})
                    actions.append(
                        f"{'✓' if ok else '✗'} KEDA {so['name']}: "
                        f"maxReplicas {so['max']}→{new_max}  ({reason})"
                    )
                else:
                    actions.append(
                        f"[DRY] KEDA {so['name']}: "
                        f"maxReplicas {so['max']}→{new_max}  ({reason})"
                    )

    # ── HPA: CPU > 80% or desired == max (상한 포화) ─────────────────────────
    for hpa in snap.get("hpa", []):
        cpu = hpa.get("cpu_pct", 0)
        saturated = hpa["desired"] >= hpa["max"] and hpa["current"] >= hpa["max"] - 1
        if cpu >= 80 or saturated:
            new_max = min(hpa["max"] + 3, 23)
            if new_max > hpa["max"]:
                reason = (f"CPU {cpu:.0f}%" if cpu >= 80
                          else f"maxReplicas 포화({hpa['max']})")
                if auto:
                    ok = _patch("hpa", hpa["name"],
                                {"spec": {"maxReplicas": new_max}})
                    actions.append(
                        f"{'✓' if ok else '✗'} HPA {hpa['name']}: "
                        f"maxReplicas {hpa['max']}→{new_max}  ({reason})"
                    )
                else:
                    actions.append(
                        f"[DRY] HPA {hpa['name']}: "
                        f"maxReplicas {hpa['max']}→{new_max}  ({reason})"
                    )

    return actions


# ── 출력 ──────────────────────────────────────────────────────────────────────
_LVL_COLOR = {"CRITICAL": R, "WARNING": Y, "OK": G}

def render(snap: dict, trackers: dict,
           levels: list[tuple[Level, str]],
           actions: list[str], args) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    mode = f"{R}AUTO{RST}" if args.auto else f"{DIM}READ-ONLY{RST}"

    print(f"\n{'─'*56}")
    print(f" 예측 스케일러  {DIM}[{ts}  {args.interval}s  {mode}]{RST}")
    print(f"{'─'*56}")

    # 현재 값 + ETA 한 줄 요약
    def _line(key, label, unit=""):
        val = snap.get(key, 0)
        thr = THRESHOLDS.get(key, 0)
        r   = trackers[key].rate()
        eta = trackers[key].eta(thr)
        pct = val / thr * 100 if thr else 0
        bar_filled = int(pct / 5)
        bar = f"{'█' * bar_filled}{'░' * (20 - bar_filled)}"
        color = R if pct >= 100 else (Y if pct >= 70 else G)
        eta_s = f"  → {eta:.0f}s" if eta is not None and eta < 300 else ""
        return (f"  {label:<14} {color}{val:>6}{unit}{RST}  "
                f"{DIM}[{bar}]{RST} {r:+.1f}/s{eta_s}")

    print(_line("sqs_backlog",  "SQS backlog",  " msg"))
    print(_line("rds_conn",     "RDS conn",     " conn"))
    print(_line("node_cpu_pct", "Node CPU",     "%"))
    print(_line("node_mem_pct", "Node MEM",     "%"))

    # HPA / KEDA 상태
    print()
    for hpa in snap.get("hpa", []):
        sat = "⚠️" if hpa["desired"] >= hpa["max"] else " "
        print(f"  HPA {hpa['name']:<24} "
              f"cur={W}{hpa['current']}{RST} "
              f"desired={hpa['desired']} max={hpa['max']} "
              f"CPU={hpa.get('cpu_pct',0):.0f}% {sat}")
    for so in snap.get("keda", []):
        sqs_rate = trackers["sqs_backlog"].rate()
        print(f"  KEDA {so['name']:<23} "
              f"max={so['max']}  qThr={so['q_thr']}  "
              f"rate={sqs_rate:+.1f}msg/s")

    if snap.get("pending_pods", 0):
        print(f"  {R}Pending Pods: {snap['pending_pods']}{RST}  ← CA 노드 추가 필요")

    # 병목 분류
    print()
    for lvl, msg in levels:
        c = _LVL_COLOR.get(lvl, "")
        print(f"  {c}{lvl:<8}{RST} {msg}")

    # 스케일링 행동
    if actions:
        print()
        for act in actions:
            print(f"  {act}")


# ── 진입점 ────────────────────────────────────────────────────────────────────
def main() -> int:
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    parser = argparse.ArgumentParser(description="실시간 예측 기반 자동 스케일러")
    parser.add_argument("--auto",     action="store_true",
                        help="자동 스케일링 활성화 (없으면 DRY-RUN 출력만)")
    parser.add_argument("--interval", type=int, default=15,
                        help="수집 주기(초), 기본 15")
    args = parser.parse_args()

    sqs_client = boto3.client("sqs",        region_name=REGION)
    cw_client  = boto3.client("cloudwatch", region_name=REGION)

    trackers = {
        "sqs_backlog":  Tracker(),
        "rds_conn":     Tracker(),
        "node_cpu_pct": Tracker(),
        "node_mem_pct": Tracker(),
    }

    print(f"예측 스케일러 시작  mode={'AUTO' if args.auto else 'READ-ONLY'}  "
          f"interval={args.interval}s")
    print("Ctrl+C 로 종료\n")

    while True:
        try:
            snap = collect(sqs_client, cw_client)

            trackers["sqs_backlog"].push(snap.get("sqs_backlog", 0))
            trackers["rds_conn"].push(snap.get("rds_conn", 0))
            trackers["node_cpu_pct"].push(snap.get("node_cpu_pct", 0))
            trackers["node_mem_pct"].push(snap.get("node_mem_pct", 0))

            levels  = classify(snap, trackers)
            actions = decide_and_scale(snap, trackers, args.auto)

            render(snap, trackers, levels, actions, args)

        except KeyboardInterrupt:
            print("\n종료")
            return 0
        except Exception as ex:
            print(f"오류: {ex}")

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
