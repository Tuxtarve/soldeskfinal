#!/usr/bin/env python3
"""
실시간 예측 기반 자동 스케일링 제어기.
모니터링은 Grafana/Prometheus 에 맡기고, 이 스크립트는 아래만 담당합니다.

  1. 핵심 메트릭 수집  — SQS / RDS / HPA / KEDA / Node / Pod
  2. 증가율 계산       — 슬라이딩 윈도우 rate
  3. 임계 도달 시간 예측 — (threshold - current) / rate
  4. 병목 분류         — 규칙 기반 (CRITICAL / WARNING / OK)
  5. 스케일링 결정 & 실행 — kubectl patch HPA·KEDA
  6. 세분화 메트릭 출력 — CLI 또는 파일

사용:
  source .env.local
  python3 scripts/realtime_monitor.py              # 반복 출력 (15초 간격)
  python3 scripts/realtime_monitor.py --auto       # 자동 스케일링 포함
  python3 scripts/realtime_monitor.py --once       # 1회만 출력하고 종료
  python3 scripts/realtime_monitor.py --file       # 파일로 저장 (반복)
  python3 scripts/realtime_monitor.py --once --file  # 1회 파일 저장
  python3 scripts/realtime_monitor.py --interval 10 --auto
"""
from __future__ import annotations

import argparse
import json
import math
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

NS         = os.environ.get("NS", "ticketing")
REGION     = os.environ.get("AWS_REGION", "ap-northeast-2")
QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "ticketing-reservation.fifo")
RDS_ID     = "prod-ticketing-writer"

THRESHOLDS = {
    "sqs_backlog":  1000,
    "rds_conn":      180,
    "node_cpu_pct":   80,
    "node_mem_pct":   85,
}

SCALE_AHEAD_SEC = 30
RATE_WINDOW     = 6

R = "\033[1;31m"; Y = "\033[1;33m"; G = "\033[1;32m"
C = "\033[1;36m"; W = "\033[1m";    DIM = "\033[2m"; RST = "\033[0m"


# ── 증가율 & ETA ───────────────────────────────────────────────────────────────
class Tracker:
    def __init__(self, window: int = RATE_WINDOW):
        self._q: deque[tuple[float, float]] = deque(maxlen=window)

    def push(self, val: float) -> None:
        self._q.append((time.monotonic(), val))

    def current(self) -> Optional[float]:
        return self._q[-1][1] if self._q else None

    def rate(self) -> float:
        if len(self._q) < 2:
            return 0.0
        t0, v0 = self._q[0]
        t1, v1 = self._q[-1]
        dt = t1 - t0
        return (v1 - v0) / dt if dt > 0 else 0.0

    def eta(self, threshold: float) -> Optional[float]:
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
            StartTime=datetime.fromtimestamp(
                now.timestamp() - minutes * 60, tz=timezone.utc).isoformat(),
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

    # RDS
    v = _cw_latest(cw_client, "AWS/RDS", "DatabaseConnections",
                   [{"Name": "DBInstanceIdentifier", "Value": RDS_ID}])
    snap["rds_conn"] = int(v) if v is not None else 0

    # HPA
    hpa_raw = _kubectl(["get", "hpa", "-n", NS])
    snap["hpa"] = []
    for h in hpa_raw.get("items", []):
        cpu_pct = 0
        for m in (h["status"].get("currentMetrics") or []):
            if m.get("type") == "Resource":
                res = m.get("resource", {})
                cpu_pct = (res.get("current", {}).get("averageUtilization") or
                           res.get("currentAverageUtilization") or 0)
        snap["hpa"].append({
            "name":    h["metadata"]["name"],
            "current": h["status"].get("currentReplicas", 0),
            "desired": h["status"].get("desiredReplicas", 0),
            "min":     h["spec"].get("minReplicas", 1),
            "max":     h["spec"].get("maxReplicas", 10),
            "cpu_pct": int(cpu_pct),
        })

    # KEDA ScaledObjects
    so_raw = _kubectl(["get", "scaledobject", "-n", NS])
    snap["keda"] = []
    for s in so_raw.get("items", []):
        target_name = s["spec"].get("scaleTargetRef", {}).get("name", "")
        q_thr = int(s["spec"].get("triggers", [{}])[0]
                    .get("metadata", {}).get("queueLength", 1))
        snap["keda"].append({
            "name":        s["metadata"]["name"],
            "target":      target_name,
            "min":         s["spec"].get("minReplicaCount", 0),
            "max":         s["spec"].get("maxReplicaCount", 10),
            "queue_thr":   q_thr,
        })

    # Pods — 서비스별 running/pending 집계
    pods_raw = _kubectl(["get", "pods", "-n", NS])
    pod_map: dict[str, dict] = {}
    for p in pods_raw.get("items", []):
        labels  = p["metadata"].get("labels", {})
        app     = labels.get("app", p["metadata"]["name"].rsplit("-", 2)[0])
        phase   = p["status"].get("phase", "Unknown")
        restarts = sum(cs.get("restartCount", 0)
                       for cs in p["status"].get("containerStatuses") or [])
        if app not in pod_map:
            pod_map[app] = {"running": 0, "pending": 0, "restarts": 0}
        pod_map[app]["restarts"] += restarts
        if phase == "Running":
            pod_map[app]["running"] += 1
        elif phase == "Pending":
            pod_map[app]["pending"] += 1
    snap["pods"] = pod_map
    snap["pending_pods"] = sum(v["pending"] for v in pod_map.values())

    # Node (kubectl top)
    nodes_raw = _kubectl(["get", "nodes"])
    snap["node_count"] = len(nodes_raw.get("items", []))
    try:
        r = subprocess.run(["kubectl", "top", "nodes", "--no-headers"],
                           capture_output=True, text=True, timeout=10)
        cpus, mems = [], []
        for line in r.stdout.strip().splitlines():
            p = line.split()
            if len(p) >= 5:
                cpus.append(float(p[2].rstrip("%")))
                mems.append(float(p[4].rstrip("%")))
        snap["node_cpu_pct"] = round(sum(cpus)/len(cpus), 1) if cpus else 0
        snap["node_mem_pct"] = round(sum(mems)/len(mems), 1) if mems else 0
    except Exception:
        snap["node_cpu_pct"] = snap["node_mem_pct"] = 0

    return snap


# ── 병목 분류 ──────────────────────────────────────────────────────────────────
def classify(snap: dict, trackers: dict) -> list[tuple[str, str]]:
    results = []
    for key, label, warn in [
        ("sqs_backlog",  "SQS backlog", 0.70),
        ("rds_conn",     "RDS conn",    0.80),
        ("node_cpu_pct", "Node CPU%",   0.80),
        ("node_mem_pct", "Node MEM%",   0.80),
    ]:
        val = snap.get(key, 0)
        thr = THRESHOLDS[key]
        r   = trackers[key].rate()
        eta = trackers[key].eta(thr)
        rate_s = f"{r:+.1f}/s" if abs(r) > 0.01 else "±0"
        eta_s  = f" → {eta:.0f}s" if eta is not None and eta < 300 else ""
        if val >= thr:
            results.append(("CRITICAL", f"{label}={val} 임계({thr}) 초과 {rate_s}{eta_s}"))
        elif val >= thr * warn or (eta is not None and eta < SCALE_AHEAD_SEC * 2):
            results.append(("WARNING",  f"{label}={val}/{thr} {rate_s}{eta_s}"))
        else:
            results.append(("OK",       f"{label}={val}/{thr} {rate_s}"))
    if snap.get("pending_pods", 0) > 0:
        results.append(("WARNING", f"Pending Pods={snap['pending_pods']} (CA 필요)"))
    return results


# ── 자동 스케일링 ──────────────────────────────────────────────────────────────
def _patch(resource: str, name: str, patch: dict, dry: bool) -> str:
    cmd = ["kubectl", "patch", resource, name, "-n", NS,
           "--type=merge", f"--patch={json.dumps(patch)}"]
    if dry:
        return f"[DRY] {' '.join(cmd)}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return ("✓" if r.returncode == 0 else "✗") + f" {resource}/{name}"
    except Exception as ex:
        return f"✗ {ex}"


def decide_and_scale(snap: dict, trackers: dict, auto: bool) -> list[str]:
    actions: list[str] = []
    eta = trackers["sqs_backlog"].eta(THRESHOLDS["sqs_backlog"])
    if eta is not None and eta < SCALE_AHEAD_SEC:
        for so in snap.get("keda", []):
            new_max = min(so["max"] + 5, 39)
            if new_max > so["max"]:
                actions.append(_patch("scaledobject", so["name"],
                                      {"spec": {"maxReplicaCount": new_max}},
                                      dry=not auto))
    for hpa in snap.get("hpa", []):
        if hpa.get("cpu_pct", 0) >= 80 or hpa["desired"] >= hpa["max"]:
            new_max = min(hpa["max"] + 3, 23)
            if new_max > hpa["max"]:
                actions.append(_patch("hpa", hpa["name"],
                                      {"spec": {"maxReplicas": new_max}},
                                      dry=not auto))
    return actions


# ── 세분화 출력 (요청 형식) ────────────────────────────────────────────────────
def format_output(snap: dict, trackers: dict,
                  levels: list[tuple[str, str]],
                  actions: list[str], args) -> str:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "AUTO" if args.auto else "READ-ONLY"
    lines: list[str] = []

    def add(s: str = "") -> None:
        lines.append(s)

    add(f"=== EKS 실시간 모니터  [{ts}  {mode}] ===")
    add()

    # ── 현재 상태 ──
    add("[현재 상태]")
    add(f"SQS backlog: {snap.get('sqs_backlog', 0)}")
    add(f"RDS conn   : {snap.get('rds_conn', 0)}")
    add()

    # ── KEDA ──
    for so in snap.get("keda", []):
        add(f"[KEDA - {so['target']}]")
        add(f"queueLength  : {snap.get('sqs_backlog', 0)}")

        # current replicas: worker pod 수
        worker_pods = next(
            (v["running"] for k, v in snap.get("pods", {}).items()
             if so["target"] in k or k in so["target"]), 0)
        # desired: ceil(backlog / queue_thr) 로 추정, max 범위 내
        backlog = snap.get("sqs_backlog", 0)
        desired = min(math.ceil(backlog / max(so["queue_thr"], 1)), so["max"])
        desired = max(desired, so["min"])

        add(f"current replicas: {worker_pods}")
        add(f"desired replicas: {desired}")
        add()

    # ── HPA ──
    for hpa in snap.get("hpa", []):
        label = hpa["name"].replace("-hpa", "").replace("-", " ")
        add(f"[HPA - {label}]")
        add(f"cpu    : {hpa.get('cpu_pct', 0)}%")
        add(f"current: {hpa['current']}")
        add(f"desired: {hpa['desired']}")
        add(f"max    : {hpa['max']}")
        add()

    # ── Node / CA ──
    add("[Node / CA]")
    add(f"node count  : {snap.get('node_count', 0)}")
    add(f"cpu usage   : {snap.get('node_cpu_pct', 0)}%")
    add(f"memory usage: {snap.get('node_mem_pct', 0)}%")
    add(f"pending pods: {snap.get('pending_pods', 0)}")
    add()

    # ── Pod 상태 ──
    add("[Pod 상태]")
    KEY_ORDER = ["worker-svc", "read-api", "write-api"]
    shown = set()
    for key in KEY_ORDER:
        for app, st in snap.get("pods", {}).items():
            if key in app and app not in shown:
                shown.add(app)
                p_str = f"  {st['pending']} pending" if st["pending"] else ""
                r_str = f"  재시작={st['restarts']}" if st["restarts"] else ""
                add(f"{app}: {st['running']} running{p_str}{r_str}")
    for app, st in snap.get("pods", {}).items():
        if app not in shown:
            p_str = f"  {st['pending']} pending" if st["pending"] else ""
            add(f"{app}: {st['running']} running{p_str}")
    add()

    # ── 예측 ──
    add("[예측]")
    sqs_rate = trackers["sqs_backlog"].rate()
    sqs_eta  = trackers["sqs_backlog"].eta(THRESHOLDS["sqs_backlog"])
    rds_rate = trackers["rds_conn"].rate()
    rds_eta  = trackers["rds_conn"].eta(THRESHOLDS["rds_conn"])

    add(f"SQS 증가 속도: {sqs_rate:+.1f} msg/sec")
    if sqs_eta is not None:
        add(f"SQS 임계까지: {sqs_eta:.1f} sec")
    else:
        add("SQS 임계까지: 안정 (증가 없음)")

    add(f"RDS 증가 속도: {rds_rate:+.1f} conn/sec")
    if rds_eta is not None:
        add(f"RDS 임계까지: {rds_eta:.1f} sec")
    else:
        add("RDS 임계까지: 안정")
    add()

    # ── 병목 ──
    add("[병목 분석]")
    for lvl, msg in levels:
        prefix = {"CRITICAL": "!! CRITICAL", "WARNING": "!  WARNING ", "OK": "   OK      "}.get(lvl, lvl)
        add(f"{prefix} {msg}")
    add()

    # ── 스케일링 ──
    if actions:
        add("[스케일링 행동]")
        for act in actions:
            add(f"  {act}")
        add()

    return "\n".join(lines)


# ── 메인 ──────────────────────────────────────────────────────────────────────
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

    parser = argparse.ArgumentParser(description="EKS 실시간 세분화 메트릭 + 예측 스케일러")
    parser.add_argument("--auto",     action="store_true",  help="자동 스케일링")
    parser.add_argument("--once",     action="store_true",  help="1회 출력 후 종료")
    parser.add_argument("--file",     action="store_true",  help="파일로 저장 (scripts/data/monitor-latest.txt)")
    parser.add_argument("--interval", type=int, default=15, help="갱신 주기(초), 기본 15")
    args = parser.parse_args()

    out_path = ROOT / "scripts" / "data" / "monitor-latest.txt"
    if args.file:
        out_path.parent.mkdir(exist_ok=True)

    sqs_client = boto3.client("sqs",        region_name=REGION)
    cw_client  = boto3.client("cloudwatch", region_name=REGION)

    trackers = {
        "sqs_backlog":  Tracker(),
        "rds_conn":     Tracker(),
        "node_cpu_pct": Tracker(),
        "node_mem_pct": Tracker(),
    }

    while True:
        try:
            snap = collect(sqs_client, cw_client)
            trackers["sqs_backlog"].push(snap.get("sqs_backlog", 0))
            trackers["rds_conn"].push(snap.get("rds_conn", 0))
            trackers["node_cpu_pct"].push(snap.get("node_cpu_pct", 0))
            trackers["node_mem_pct"].push(snap.get("node_mem_pct", 0))

            levels  = classify(snap, trackers)
            actions = decide_and_scale(snap, trackers, args.auto)
            output  = format_output(snap, trackers, levels, actions, args)

            if args.file:
                out_path.write_text(output, encoding="utf-8")
                ts_path = out_path.parent / f"monitor-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
                ts_path.write_text(output, encoding="utf-8")
                print(f"저장: {out_path}  ({ts_path.name})")
            else:
                print(output)

        except KeyboardInterrupt:
            print("\n종료")
            return 0
        except Exception as ex:
            print(f"오류: {ex}")

        if args.once:
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
