#!/usr/bin/env python3
"""
실시간 예측 기반 자동 스케일링 제어기 — 세분화 메트릭 출력.

사용:
  source .env.local
  python3 scripts/realtime_monitor.py              # 반복 출력 (15초 간격)
  python3 scripts/realtime_monitor.py --auto       # 자동 스케일링 포함
  python3 scripts/realtime_monitor.py --once       # 1회 출력 후 종료
  python3 scripts/realtime_monitor.py --file       # 파일 저장
  python3 scripts/realtime_monitor.py --interval 10
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
REDIS_GID  = "ticketing-redis"

THRESHOLDS = {
    "sqs_backlog":  1000,
    "rds_conn":      180,
    "node_cpu_pct":   80,
    "node_mem_pct":   85,
}
SCALE_AHEAD_SEC = 30
RATE_WINDOW     = 6


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


def _cw(cw, namespace: str, metric: str, dims: list,
        stat: str = "Average", minutes: int = 5) -> Optional[float]:
    try:
        now = datetime.now(timezone.utc)
        r = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric, Dimensions=dims,
            StartTime=datetime.fromtimestamp(
                now.timestamp() - minutes * 60, tz=timezone.utc).isoformat(),
            EndTime=now.isoformat(), Period=60, Statistics=[stat]
        )
        pts = sorted(r.get("Datapoints", []),
                     key=lambda x: str(x.get("Timestamp", "")))
        return round(pts[-1][stat], 1) if pts else None
    except Exception:
        return None


def collect(sqs_client, cw_client) -> dict:
    snap: dict = {}

    # ── SQS ──
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

    # ── RDS (CloudWatch) ──
    rds_dims = [{"Name": "DBInstanceIdentifier", "Value": RDS_ID}]
    snap["rds_conn"]    = int(_cw(cw_client, "AWS/RDS", "DatabaseConnections", rds_dims) or 0)
    snap["rds_cpu_pct"] = _cw(cw_client, "AWS/RDS", "CPUUtilization", rds_dims) or 0

    # ── Redis (CloudWatch) ──
    redis_dims = [{"Name": "ReplicationGroupId", "Value": REDIS_GID}]
    snap["redis_mem_pct"]  = _cw(cw_client, "AWS/ElastiCache", "DatabaseMemoryUsagePercentage", redis_dims) or 0
    snap["redis_evictions"]= int(_cw(cw_client, "AWS/ElastiCache", "Evictions", redis_dims, stat="Sum") or 0)

    # ── HPA ──
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

    # ── KEDA ScaledObjects ──
    so_raw = _kubectl(["get", "scaledobject", "-n", NS])
    snap["keda"] = []
    for s in so_raw.get("items", []):
        snap["keda"].append({
            "name":      s["metadata"]["name"],
            "target":    s["spec"].get("scaleTargetRef", {}).get("name", ""),
            "min":       s["spec"].get("minReplicaCount", 0),
            "max":       s["spec"].get("maxReplicaCount", 10),
            "queue_thr": int(s["spec"].get("triggers", [{}])[0]
                             .get("metadata", {}).get("queueLength", 1)),
        })

    # ── Pods (서비스별 집계) ──
    pods_raw = _kubectl(["get", "pods", "-n", NS])
    pod_map: dict[str, dict] = {}
    for p in pods_raw.get("items", []):
        app     = p["metadata"].get("labels", {}).get("app",
                      p["metadata"]["name"].rsplit("-", 2)[0])
        phase   = p["status"].get("phase", "Unknown")
        restarts = sum(cs.get("restartCount", 0)
                       for cs in p["status"].get("containerStatuses") or [])
        # OOMKill 감지
        oom = any(
            cs.get("lastState", {}).get("terminated", {}).get("reason") == "OOMKilled"
            for cs in p["status"].get("containerStatuses") or []
        )
        if app not in pod_map:
            pod_map[app] = {"running": 0, "pending": 0, "restarts": 0, "oom": False}
        pod_map[app]["restarts"] += restarts
        pod_map[app]["oom"] = pod_map[app]["oom"] or oom
        if phase == "Running":
            pod_map[app]["running"] += 1
        elif phase == "Pending":
            pod_map[app]["pending"] += 1
    snap["pods"] = pod_map
    snap["pending_pods"] = sum(v["pending"] for v in pod_map.values())

    # ── 최근 스케일 이벤트 ──
    ev_raw = _kubectl(["get", "events", "-n", NS, "--sort-by=.lastTimestamp"])
    scale_events = []
    for e in ev_raw.get("items", []):
        reason = e.get("reason", "")
        if any(k in reason for k in ("Scal", "SuccessfulRescale")):
            ts_raw = (e.get("lastTimestamp") or e.get("eventTime") or "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                ts_str = ts.astimezone().strftime("%H:%M:%S")
            except Exception:
                ts_str = ts_raw[:19]
            obj  = e.get("involvedObject", {})
            kind = obj.get("kind", "")
            name = obj.get("name", "")
            msg  = (e.get("message") or "")[:80]
            scale_events.append(f"{ts_str}  {kind}/{name}  {msg}")
    snap["scale_events"] = scale_events[-5:]  # 최근 5건

    # ── 노드별 상세 (kubectl top nodes) ──
    nodes_raw = _kubectl(["get", "nodes"])
    snap["node_count"] = len(nodes_raw.get("items", []))
    snap["nodes_detail"] = []
    snap["node_cpu_pct"] = 0
    snap["node_mem_pct"] = 0
    try:
        r = subprocess.run(["kubectl", "top", "nodes", "--no-headers"],
                           capture_output=True, text=True, timeout=10)
        cpus, mems = [], []
        for line in r.stdout.strip().splitlines():
            p = line.split()
            if len(p) >= 5:
                cpu = float(p[2].rstrip("%"))
                mem = float(p[4].rstrip("%"))
                cpus.append(cpu)
                mems.append(mem)
                # 노드 이름 단축 (ip-10-0-x-y → 마지막 두 옥텟만)
                short = p[0].split(".")[-1] if "." in p[0] else p[0][-12:]
                snap["nodes_detail"].append({
                    "name":    short,
                    "cpu_pct": cpu,
                    "mem_pct": mem,
                })
        snap["node_cpu_pct"] = round(sum(cpus) / len(cpus), 1) if cpus else 0
        snap["node_mem_pct"] = round(sum(mems) / len(mems), 1) if mems else 0
    except Exception:
        pass

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
    if snap.get("redis_evictions", 0) > 0:
        results.append(("WARNING", f"Redis Eviction={snap['redis_evictions']} (maxmemory 확인)"))
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


# ── 세분화 출력 ────────────────────────────────────────────────────────────────
def format_output(snap: dict, trackers: dict,
                  levels: list[tuple[str, str]],
                  actions: list[str], args) -> str:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "AUTO" if args.auto else "READ-ONLY"
    lines: list[str] = []
    add = lines.append

    add(f"=== EKS 실시간 모니터  [{ts}  {mode}] ===")
    add("")

    # ── [현재 상태] ──────────────────────────────────────────────────────────
    add("[현재 상태]")
    add(f"SQS backlog: {snap.get('sqs_backlog', 0)}")
    add(f"RDS conn   : {snap.get('rds_conn', 0)}")
    add("")

    # ── [KEDA] ───────────────────────────────────────────────────────────────
    for so in snap.get("keda", []):
        backlog  = snap.get("sqs_backlog", 0)
        inflight = snap.get("sqs_inflight", 0)
        q_thr    = max(so["queue_thr"], 1)

        # current replicas: worker pod 수
        worker_run = next(
            (v["running"] for k, v in snap.get("pods", {}).items()
             if so["target"] in k or k in so["target"]), 0)

        # desired: ceil(backlog / q_thr), 범위 클램프
        desired = max(so["min"], min(math.ceil(backlog / q_thr), so["max"]))

        # worker 처리 속도: in-flight tracker 기반
        sqs_rate     = trackers["sqs_backlog"].rate()   # msg/s (양수=증가)
        # 처리속도 = worker가 소화하는 속도: inflight × (1/처리시간)
        # rate가 음수면 worker가 더 빠른 것 → -rate 가 처리속도
        consume_rate = -sqs_rate if sqs_rate < 0 else 0
        produce_rate =  sqs_rate if sqs_rate > 0 else 0

        # backlog 소화까지: backlog / consume_rate
        if consume_rate > 0 and backlog > 0:
            clear_sec = backlog / consume_rate
            clear_str = f"{clear_sec:.0f} sec"
        elif produce_rate > 0:
            clear_str = "증가 중 (worker 추가 필요)"
        else:
            clear_str = "안정"

        add(f"[KEDA - {so['target']}]")
        add(f"queueLength     : {backlog}")
        add(f"in-flight       : {inflight}")
        add(f"current replicas: {worker_run}")
        add(f"desired replicas: {desired}")
        add(f"처리 속도       : {sqs_rate:+.1f} msg/sec"
            f"  (worker {consume_rate:.1f} 소화 / {produce_rate:.1f} 유입)")
        add(f"backlog 소화까지: {clear_str}")
        add("")

    # ── [HPA] ────────────────────────────────────────────────────────────────
    for hpa in snap.get("hpa", []):
        label = hpa["name"].replace("-hpa", "")
        sat   = "  ← 상한 포화!" if hpa["desired"] >= hpa["max"] else ""
        add(f"[HPA - {label}]")
        add(f"cpu    : {hpa.get('cpu_pct', 0)}%")
        add(f"current: {hpa['current']}")
        add(f"desired: {hpa['desired']}{sat}")
        add(f"max    : {hpa['max']}")
        add("")

    # ── [Node / CA] ──────────────────────────────────────────────────────────
    add("[Node / CA]")
    add(f"node count  : {snap.get('node_count', 0)}")
    for nd in snap.get("nodes_detail", []):
        cpu_w = "!!" if nd["cpu_pct"] >= 80 else "  "
        mem_w = "!!" if nd["mem_pct"] >= 85 else "  "
        add(f"  {nd['name']:<30}"
            f"  CPU {cpu_w}{nd['cpu_pct']:5.1f}%"
            f"  MEM {mem_w}{nd['mem_pct']:5.1f}%")
    pending = snap.get("pending_pods", 0)
    add(f"pending pods: {pending}"
        + ("  ← CA 노드 추가 대기 중" if pending > 0 else ""))
    add("")

    # ── [데이터 계층] ─────────────────────────────────────────────────────────
    add("[데이터 계층]")
    rds_conn = snap.get("rds_conn", 0)
    rds_cpu  = snap.get("rds_cpu_pct", 0)
    # 서비스별 커넥션 추정 (worker: pool≈40, write-api: pool≈5)
    worker_run_total = sum(
        v["running"] for k, v in snap.get("pods", {}).items() if "worker" in k)
    write_run_total = sum(
        v["running"] for k, v in snap.get("pods", {}).items() if "write" in k)
    est_worker_conn = worker_run_total * 40
    est_write_conn  = write_run_total  * 5
    add(f"RDS CPU  : {rds_cpu}%")
    add(f"RDS conn : {rds_conn} / 180  "
        f"(worker 추정 {est_worker_conn}  write-api 추정 {est_write_conn})")
    redis_mem = snap.get("redis_mem_pct", 0)
    redis_ev  = snap.get("redis_evictions", 0)
    ev_warn   = "  ← maxmemory-policy 확인!" if redis_ev > 0 else ""
    add(f"Redis MEM: {redis_mem}%"
        f"  evictions: {redis_ev}{ev_warn}")
    add("")

    # ── [Pod 상태] ───────────────────────────────────────────────────────────
    add("[Pod 상태]")
    KEY_ORDER = ["worker-svc", "read-api", "write-api"]
    shown = set()
    for key in KEY_ORDER:
        for app, st in snap.get("pods", {}).items():
            if key in app and app not in shown:
                shown.add(app)
                parts = [f"{st['running']} running"]
                if st["pending"]:
                    parts.append(f"{st['pending']} pending")
                if st["restarts"] > 0:
                    oom_flag = " [OOMKill!]" if st["oom"] else ""
                    parts.append(f"재시작={st['restarts']}{oom_flag}")
                add(f"  {app:<22} {' / '.join(parts)}")
    for app, st in snap.get("pods", {}).items():
        if app not in shown:
            parts = [f"{st['running']} running"]
            if st["pending"]:
                parts.append(f"{st['pending']} pending")
            if st["restarts"]:
                oom_flag = " [OOMKill!]" if st["oom"] else ""
                parts.append(f"재시작={st['restarts']}{oom_flag}")
            add(f"  {app:<22} {' / '.join(parts)}")
    add("")

    # ── [스케일 이벤트] ──────────────────────────────────────────────────────
    events = snap.get("scale_events", [])
    if events:
        add("[스케일 이력]")
        for ev in events:
            add(f"  {ev}")
        add("")

    # ── [예측] ───────────────────────────────────────────────────────────────
    add("[예측]")
    sqs_rate = trackers["sqs_backlog"].rate()
    sqs_eta  = trackers["sqs_backlog"].eta(THRESHOLDS["sqs_backlog"])
    rds_rate = trackers["rds_conn"].rate()
    rds_eta  = trackers["rds_conn"].eta(THRESHOLDS["rds_conn"])
    cpu_rate = trackers["node_cpu_pct"].rate()
    cpu_eta  = trackers["node_cpu_pct"].eta(THRESHOLDS["node_cpu_pct"])

    add(f"SQS 증가 속도: {sqs_rate:+.1f} msg/sec"
        + (f"  임계까지: {sqs_eta:.1f} sec" if sqs_eta is not None else "  안정"))
    add(f"RDS 증가 속도: {rds_rate:+.1f} conn/sec"
        + (f"  임계까지: {rds_eta:.1f} sec" if rds_eta is not None else "  안정"))
    add(f"Node CPU 속도: {cpu_rate:+.2f} %/sec"
        + (f"  임계까지: {cpu_eta:.1f} sec" if cpu_eta is not None else "  안정"))
    add("")

    # ── [병목 분석] ──────────────────────────────────────────────────────────
    add("[병목 분석]")
    for lvl, msg in levels:
        prefix = {
            "CRITICAL": "!! CRITICAL",
            "WARNING":  "!  WARNING ",
            "OK":       "   OK      ",
        }.get(lvl, lvl)
        add(f"  {prefix} {msg}")
    add("")

    # ── [스케일링 행동] ──────────────────────────────────────────────────────
    if actions:
        add("[스케일링 행동]")
        for act in actions:
            add(f"  {act}")
        add("")

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
    parser.add_argument("--file",     action="store_true",  help="파일 저장")
    parser.add_argument("--interval", type=int, default=15, help="갱신 주기(초)")
    args = parser.parse_args()

    out_dir = ROOT / "scripts" / "data"
    if args.file:
        out_dir.mkdir(exist_ok=True)

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
            snap    = collect(sqs_client, cw_client)
            trackers["sqs_backlog"].push(snap.get("sqs_backlog", 0))
            trackers["rds_conn"].push(snap.get("rds_conn", 0))
            trackers["node_cpu_pct"].push(snap.get("node_cpu_pct", 0))
            trackers["node_mem_pct"].push(snap.get("node_mem_pct", 0))

            levels  = classify(snap, trackers)
            actions = decide_and_scale(snap, trackers, args.auto)
            output  = format_output(snap, trackers, levels, actions, args)

            if args.file:
                latest = out_dir / "monitor-latest.txt"
                latest.write_text(output, encoding="utf-8")
                ts_file = out_dir / f"monitor-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
                ts_file.write_text(output, encoding="utf-8")
                print(f"저장: {latest}  ({ts_file.name})")
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
