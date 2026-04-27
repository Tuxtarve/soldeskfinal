#!/usr/bin/env python3
"""
적응형 이분 탐색 기반 오토스케일링 임계값 탐색기.

HPA / KEDA / CA 각각의 부하 기준으로 장애 직전 최대 임계값을 자동으로 탐색합니다.
Gemini가 메트릭을 분석해 이분 탐색 방향(go_higher/go_lower/converged)을 결정합니다.

사용:
  python3 scripts/find_threshold.py --mode keda       # worker-svc KEDA (SQS 메시지 수)
  python3 scripts/find_threshold.py --mode hpa-read   # read-api HPA (HTTP RPS)
  python3 scripts/find_threshold.py --mode hpa-write  # write-api HPA (HTTP RPS)
  python3 scripts/find_threshold.py --mode combined   # 전체 동시 부하

옵션:
  --min N          탐색 하한 (기본: 모드별 자동)
  --max N          탐색 상한 (기본: 모드별 자동)
  --iters N        최대 이분 탐색 횟수 (기본: 10)
  --load-sec N     부하 지속 시간(초) (기본: 60)
  --wait-sec N     스케일링 안정화 대기(초) (기본: 45)
  --endpoint URL   API Gateway 엔드포인트 (기본: .env.local / terraform output)
  --dry-run        실제 부하 없이 메트릭만 수집해서 Gemini 분석

환경변수:
  GEMINI_API_KEY   (필수)
  AWS_REGION       (기본: ap-northeast-2)
  SQS_QUEUE_URL    (기본: terraform output에서 자동 감지)
  API_GW_ENDPOINT  (기본: terraform output에서 자동 감지)
  GEMINI_MODEL     (기본: gemini-2.5-flash-lite)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import boto3
    import requests
    from google import genai
    from google.genai import types
except ImportError as e:
    sys.exit(f"패키지 누락: {e}\n  pip install boto3 requests google-genai")

ROOT = Path(__file__).resolve().parent.parent

# ── 설정 ──────────────────────────────────────────────────────────────────────
NS      = os.environ.get("NS", "ticketing")
REGION  = os.environ.get("AWS_REGION", "ap-northeast-2")
MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

# 모드별 기본 탐색 범위
MODE_DEFAULTS = {
    "keda":      {"min": 10,   "max": 3000, "unit": "SQS 메시지 수",    "desc": "worker-svc KEDA (SQS 큐 깊이)"},
    "hpa-read":  {"min": 5,    "max": 500,  "unit": "RPS (read-api)",   "desc": "read-api HPA (CPU 기반)"},
    "hpa-write": {"min": 5,    "max": 300,  "unit": "RPS (write-api)",  "desc": "write-api HPA (CPU 기반)"},
    "combined":  {"min": 10,   "max": 1000, "unit": "총 RPS",           "desc": "전체 동시 부하 (HPA+KEDA)"},
}


# ── 환경 자동 감지 ─────────────────────────────────────────────────────────────
def _tf_output(key: str) -> str:
    try:
        r = subprocess.run(
            ["terraform", "output", "-raw", key],
            cwd=ROOT / "terraform", capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def get_sqs_url() -> str:
    url = os.environ.get("SQS_QUEUE_URL", "")
    if not url:
        url = _tf_output("sqs_queue_url")
    if not url:
        sqs = boto3.client("sqs", region_name=REGION)
        try:
            url = sqs.get_queue_url(QueueName="ticketing-reservation.fifo")["QueueUrl"]
        except Exception:
            pass
    return url


def get_api_endpoint() -> str:
    ep = os.environ.get("API_GW_ENDPOINT", "")
    if not ep:
        ep = _tf_output("api_gateway_endpoint")
    return ep.rstrip("/")


# ── 메트릭 수집 ────────────────────────────────────────────────────────────────
def _kubectl(args: list[str]) -> dict | list:
    try:
        r = subprocess.run(["kubectl"] + args + ["-o", "json"],
                           capture_output=True, text=True, timeout=20)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}


def _prom_query(query: str, port: int = 19090) -> list:
    try:
        r = requests.get(f"http://localhost:{port}/api/v1/query",
                         params={"query": query}, timeout=8)
        return r.json().get("data", {}).get("result", [])
    except Exception:
        return []


def _num(v):
    try:
        return int(v) if "." not in str(v) else float(v)
    except Exception:
        return v


def collect_metrics(sqs_url: str) -> dict:
    snap: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}

    # kubectl
    pods_raw = _kubectl(["get", "pods", "-n", NS])
    pods = [{"name": p["metadata"]["name"],
              "phase": p["status"].get("phase"),
              "restarts": sum(cs.get("restartCount", 0)
                              for cs in p["status"].get("containerStatuses") or [])}
            for p in pods_raw.get("items", [])]
    pending = [p for p in pods if p["phase"] == "Pending"]
    restarts = sum(p["restarts"] for p in pods)

    hpa_raw  = _kubectl(["get", "hpa", "-n", NS])
    hpa_list = [{"name": h["metadata"]["name"],
                 "current": h["status"].get("currentReplicas"),
                 "desired": h["status"].get("desiredReplicas"),
                 "min": h["spec"].get("minReplicas"),
                 "max": h["spec"].get("maxReplicas"),
                 "metrics": h["status"].get("currentMetrics", [])}
                for h in hpa_raw.get("items", [])]

    so_raw  = _kubectl(["get", "scaledobject", "-n", NS])
    so_list = [{"name": s["metadata"]["name"],
                "target": s["spec"].get("scaleTargetRef", {}).get("name"),
                "min": s["spec"].get("minReplicaCount"),
                "max": s["spec"].get("maxReplicaCount"),
                "ready": next((c["status"] for c in s.get("status", {}).get("conditions", [])
                               if c["type"] == "Ready"), None)}
               for s in so_raw.get("items", [])]

    nodes_raw = _kubectl(["get", "nodes"])
    node_count = len(nodes_raw.get("items", []))

    events_raw = _kubectl(["get", "events", "-n", NS, "--sort-by=.lastTimestamp"])
    scale_events = [{"reason": e.get("reason"), "msg": e.get("message"),
                     "obj": e.get("involvedObject", {}).get("name")}
                    for e in events_raw.get("items", [])
                    if any(k in (e.get("reason") or "") for k in ("Scal", "OOM", "Evict", "FailedScheduling"))][-10:]

    snap["k8s"] = {
        "pods_total": len(pods),
        "pods_pending": len(pending),
        "pod_restarts_total": restarts,
        "pending_names": [p["name"] for p in pending],
        "hpa": hpa_list,
        "keda_scaled_objects": so_list,
        "node_count": node_count,
        "scale_events": scale_events,
    }

    # SQS
    try:
        sqs = boto3.client("sqs", region_name=REGION)
        attrs = sqs.get_queue_attributes(
            QueueUrl=sqs_url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"]
        )["Attributes"]
        snap["sqs"] = {
            "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        }
    except Exception as ex:
        snap["sqs"] = {"error": str(ex)}

    # CloudWatch (최근 5분 RDS CPU)
    try:
        cw = boto3.client("cloudwatch", region_name=REGION)
        now = datetime.now(timezone.utc)
        start = datetime.fromtimestamp(now.timestamp() - 300, tz=timezone.utc)
        resp = cw.get_metric_statistics(
            Namespace="AWS/RDS", MetricName="CPUUtilization",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": "prod-ticketing-writer"}],
            StartTime=start.isoformat(), EndTime=now.isoformat(),
            Period=60, Statistics=["Average"]
        )
        pts = sorted(resp.get("Datapoints", []), key=lambda x: str(x.get("Timestamp", "")))
        snap["rds_cpu_pct"] = round(pts[-1]["Average"], 1) if pts else None
    except Exception:
        snap["rds_cpu_pct"] = None

    # Prometheus (port-forward 열려있으면)
    pf_proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", "monitoring",
         "svc/kube-prometheus-stack-prometheus", "19090:9090"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    try:
        snap["prometheus"] = {
            "cpu_rate":   [{"pod": r["metric"].get("pod"), "val": _num(r["value"][1])}
                           for r in _prom_query(
                               f'sum by(pod)(rate(container_cpu_usage_seconds_total'
                               f'{{namespace="{NS}",container!="",container!="POD"}}[2m]))')],
            "mem_bytes":  [{"pod": r["metric"].get("pod"), "val": _num(r["value"][1])}
                           for r in _prom_query(
                               f'sum by(pod)(container_memory_working_set_bytes'
                               f'{{namespace="{NS}",container!="",container!="POD"}})')],
            "restarts":   [{"pod": r["metric"].get("pod"), "val": _num(r["value"][1])}
                           for r in _prom_query(
                               f'sum by(pod)(kube_pod_container_status_restarts_total'
                               f'{{namespace="{NS}"}})') if _num(r["value"][1]) > 0],
        }
    except Exception:
        snap["prometheus"] = {}
    finally:
        pf_proc.kill(); pf_proc.wait()

    return snap


# ── 부하 생성기 ────────────────────────────────────────────────────────────────
def _send_sqs_batch(sqs_client, queue_url: str, n: int) -> int:
    sent = 0
    batch = []
    for i in range(n):
        msg_id = f"t-{i}"
        batch.append({
            "Id": str(i % 10),
            "MessageBody": json.dumps({"concert_id": 7, "seat": i, "user_id": i + 1000}),
            "MessageGroupId": f"g{i % 64}",
            "MessageDeduplicationId": f"{int(time.time()*1000)}-{i}",
        })
        if len(batch) == 10:
            try:
                r = sqs_client.send_message_batch(QueueUrl=queue_url, Entries=batch)
                sent += len(r.get("Successful", []))
            except Exception:
                pass
            batch = []
    if batch:
        try:
            r = sqs_client.send_message_batch(QueueUrl=queue_url, Entries=batch)
            sent += len(r.get("Successful", []))
        except Exception:
            pass
    return sent


def run_keda_load(sqs_url: str, n_messages: int, duration_sec: int,
                  stop_event: threading.Event) -> dict:
    sqs = boto3.client("sqs", region_name=REGION)
    total_sent = 0
    start = time.time()
    # 첫 번째 배치 즉시 전송
    sent = _send_sqs_batch(sqs, sqs_url, min(n_messages, 1000))
    total_sent += sent
    print(f"   [load] SQS {sent}개 전송 완료 (목표: {n_messages})")

    # 남은 메시지를 duration 동안 분산 전송
    remaining = n_messages - sent
    if remaining > 0 and duration_sec > 5:
        interval = duration_sec / max(remaining // 100, 1)
        while not stop_event.is_set() and time.time() - start < duration_sec and remaining > 0:
            chunk = min(100, remaining)
            s = _send_sqs_batch(sqs, sqs_url, chunk)
            total_sent += s
            remaining -= chunk
            time.sleep(min(interval, 5))

    return {"total_sent": total_sent}


def run_http_load(endpoint: str, path: str, method: str,
                  target_rps: float, duration_sec: int,
                  stop_event: threading.Event) -> dict:
    url = f"{endpoint}{path}"
    interval = 1.0 / max(target_rps, 0.1)
    results = {"ok": 0, "err": 0, "latencies_ms": []}
    lock = threading.Lock()

    def _req():
        try:
            t0 = time.time()
            r = (requests.get(url, timeout=5) if method == "GET"
                 else requests.post(url, json={"concert_id": 7, "user_id": 9999,
                                               "seat_row": 1, "seat_col": 1}, timeout=5))
            lat = (time.time() - t0) * 1000
            with lock:
                if r.status_code < 500:
                    results["ok"] += 1
                else:
                    results["err"] += 1
                if len(results["latencies_ms"]) < 500:
                    results["latencies_ms"].append(round(lat, 1))
        except Exception:
            with lock:
                results["err"] += 1

    start = time.time()
    with ThreadPoolExecutor(max_workers=min(int(target_rps * 2), 200)) as pool:
        while not stop_event.is_set() and time.time() - start < duration_sec:
            pool.submit(_req)
            time.sleep(interval)

    lats = sorted(results["latencies_ms"])
    n = len(lats)
    results["p50_ms"] = lats[int(n * 0.5)] if lats else None
    results["p95_ms"] = lats[int(n * 0.95)] if lats else None
    results["error_rate_pct"] = round(results["err"] / max(results["ok"] + results["err"], 1) * 100, 1)
    return results


# ── Gemini 이분 탐색 분류기 ───────────────────────────────────────────────────
GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "health":           {"type": "string", "enum": ["HEALTHY", "WARNING", "FAILURE"]},
        "confidence":       {"type": "string", "enum": ["high", "medium", "low"]},
        "bottleneck":       {"type": "string", "enum": [
                                "none", "hpa_cpu", "hpa_memory", "keda_sqs",
                                "rds_connections", "node_memory", "oom_kill",
                                "pending_pods", "latency", "error_rate"]},
        "scale_status":     {"type": "string", "enum": ["scaling_up", "scaling_down", "stable", "thrashing"]},
        "capacity_used_pct":{"type": "number"},
        "binary_signal":    {"type": "string", "enum": ["go_higher", "go_lower", "converged"]},
        "reason":           {"type": "string"},
        "estimated_max_safe_load": {"type": "number"},
    },
    "required": ["health", "confidence", "bottleneck", "scale_status",
                 "capacity_used_pct", "binary_signal", "reason", "estimated_max_safe_load"],
}


def gemini_classify(api_key: str, mode: str, load_level: float,
                    metrics: dict, load_result: dict,
                    history: list[dict]) -> dict:
    client = genai.Client(api_key=api_key)

    history_txt = "\n".join(
        f"  load={h['load']}: {h['health']} / {h['bottleneck']} / {h['binary_signal']}"
        for h in history[-5:]
    ) or "  (첫 번째 시도)"

    prompt = f"""당신은 Kubernetes 오토스케일링 전문가입니다.
아래 메트릭을 분석해 시스템 건강 상태와 이분 탐색 방향을 결정하세요.

## 테스트 모드
{mode} — 현재 부하 레벨: {load_level} {MODE_DEFAULTS.get(mode, {}).get('unit', '')}

## 시스템 제약 (하드 캡)
- 노드: t3.small × {metrics['k8s']['node_count']}대 (최대 40대 — CA 상한)
- RDS max_connections: 200 (worker × pool_size + write-api × pool_size ≤ 180)
- SQS FIFO: 그룹당 300 msg/s
- HPA maxReplicas: read-api 23, write-api 23
- KEDA maxReplicaCount: worker-svc 39

## 현재 메트릭 스냅샷
```json
{json.dumps(metrics, ensure_ascii=False, indent=2, default=str)}
```

## 부하 생성 결과
```json
{json.dumps(load_result, ensure_ascii=False, indent=2)}
```

## 이전 탐색 이력
{history_txt}

## 판단 기준
- HEALTHY: 모든 Pod Running, 재시작 없음, SLO 이내, 스케일링 정상
- WARNING: 한계에 근접 (CPU 70%+, 메모리 80%+, Pod Pending 시작)
- FAILURE: OOMKill/재시작, Pod Pending, RDS 연결 한계, 에러율 1%+

## binary_signal 결정 규칙
- HEALTHY + 여유 많음 → go_higher (부하를 더 올릴 수 있음)
- WARNING + 한계 근접  → go_higher (but 조심) 또는 converged
- FAILURE              → go_lower (부하 줄여야 함)
- WARNING + FAILURE 사이에서 수렴 → converged (임계값 발견)

estimated_max_safe_load: 현재 메트릭 기반으로 추정한 안전한 최대 부하 수치
"""

    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GEMINI_SCHEMA,
            temperature=0.1,
        ),
    )
    return json.loads(resp.text)


# ── 이분 탐색 엔진 ──────────────────────────────────────────────────────────────
def binary_search(args, api_key: str, sqs_url: str, api_endpoint: str) -> dict:
    mode = args.mode
    low  = float(args.min)
    high = float(args.max)
    history: list[dict] = []
    best_safe: float | None = None
    best_failure: float | None = None

    print(f"\n{'='*60}")
    print(f" 이분 탐색 시작: {MODE_DEFAULTS[mode]['desc']}")
    print(f" 탐색 범위: {low} ~ {high} {MODE_DEFAULTS[mode]['unit']}")
    print(f" 최대 반복: {args.iters}회 | 부하 시간: {args.load_sec}초 | 대기: {args.wait_sec}초")
    print(f"{'='*60}\n")

    for iteration in range(1, args.iters + 1):
        mid = (low + high) / 2
        load_level = round(mid) if mode == "keda" else round(mid, 1)

        print(f"[iter {iteration}/{args.iters}] 부하 레벨: {load_level} {MODE_DEFAULTS[mode]['unit']}")
        print(f"          탐색 구간: [{low:.0f}, {high:.0f}]")

        # 부하 주입
        stop_ev = threading.Event()
        load_result: dict = {"skipped": True}

        if not args.dry_run:
            t0 = time.time()
            if mode == "keda":
                load_result = run_keda_load(sqs_url, int(load_level), args.load_sec, stop_ev)
            elif mode == "hpa-read":
                load_result = run_http_load(
                    api_endpoint, "/api/read/movies", "GET",
                    load_level, args.load_sec, stop_ev)
            elif mode == "hpa-write":
                load_result = run_http_load(
                    api_endpoint, "/api/write/booking", "POST",
                    load_level, args.load_sec, stop_ev)
            elif mode == "combined":
                # 70% read, 30% write, SQS 주입
                t_stop = threading.Event()
                futs = []
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futs.append(pool.submit(run_http_load, api_endpoint,
                                            "/api/read/movies", "GET",
                                            load_level * 0.7, args.load_sec, t_stop))
                    futs.append(pool.submit(run_http_load, api_endpoint,
                                            "/api/write/booking", "POST",
                                            load_level * 0.3, args.load_sec, t_stop))
                    futs.append(pool.submit(run_keda_load, sqs_url,
                                            int(load_level * 2), args.load_sec, t_stop))
                load_result = {"combined": [f.result() for f in futs]}
            stop_ev.set()
            elapsed = time.time() - t0
            print(f"   부하 완료 ({elapsed:.0f}초)")
        else:
            print("   [dry-run] 부하 생략")
            time.sleep(2)

        # 스케일링 안정화 대기
        print(f"   안정화 대기 중 ({args.wait_sec}초)...")
        for remaining in range(args.wait_sec, 0, -10):
            time.sleep(min(10, remaining))
            # 조기 종료 체크: Pending Pod 발생
            pods_raw = _kubectl(["get", "pods", "-n", NS])
            pending_count = sum(1 for p in pods_raw.get("items", [])
                                if p["status"].get("phase") == "Pending")
            if pending_count > 2:
                print(f"   ⚠️  Pending Pod {pending_count}개 감지 — 조기 종료")
                break

        # 메트릭 수집
        print("   메트릭 수집 중...")
        metrics = collect_metrics(sqs_url)

        # Gemini 분석
        print("   Gemini 분석 중...")
        try:
            result = gemini_classify(api_key, mode, load_level, metrics, load_result, history)
        except Exception as ex:
            print(f"   Gemini 오류: {ex} — UNKNOWN으로 처리")
            result = {"health": "WARNING", "confidence": "low",
                      "bottleneck": "none", "scale_status": "stable",
                      "capacity_used_pct": 50, "binary_signal": "go_higher",
                      "reason": str(ex), "estimated_max_safe_load": load_level}

        # 결과 기록
        entry = {
            "iter": iteration, "load": load_level,
            "health": result["health"],
            "bottleneck": result["bottleneck"],
            "scale_status": result["scale_status"],
            "capacity_pct": result["capacity_used_pct"],
            "binary_signal": result["binary_signal"],
            "confidence": result["confidence"],
            "reason": result["reason"],
            "estimated_max": result["estimated_max_safe_load"],
            "metrics_summary": {
                "pods_total": metrics["k8s"]["pods_total"],
                "pods_pending": metrics["k8s"]["pods_pending"],
                "pod_restarts": metrics["k8s"]["pod_restarts_total"],
                "node_count": metrics["k8s"]["node_count"],
                "sqs_visible": metrics.get("sqs", {}).get("visible", "-"),
                "rds_cpu": metrics.get("rds_cpu_pct"),
            },
        }
        history.append(entry)

        # 출력
        health_icon = {"HEALTHY": "✅", "WARNING": "⚠️", "FAILURE": "❌"}.get(result["health"], "?")
        signal_icon = {"go_higher": "↑", "go_lower": "↓", "converged": "★"}.get(result["binary_signal"], "?")
        print(f"\n   {health_icon} {result['health']} | {signal_icon} {result['binary_signal']}")
        print(f"   병목: {result['bottleneck']} | 용량사용: {result['capacity_used_pct']:.0f}%")
        print(f"   {result['reason'][:120]}")
        print(f"   추정 최대 안전 부하: {result['estimated_max_safe_load']}")

        # 이분 탐색 경계 업데이트
        signal = result["binary_signal"]
        if signal == "go_higher":
            best_safe = load_level
            low = mid
        elif signal == "go_lower":
            best_failure = load_level
            high = mid
        elif signal == "converged":
            best_safe = load_level
            print(f"\n★ 임계값 수렴! 안전 최대 부하: {load_level}")
            break

        # 수렴 체크 (구간이 충분히 좁아진 경우)
        tolerance = max((args.max - args.min) * 0.05, 5)
        if high - low <= tolerance:
            print(f"\n★ 탐색 수렴 (구간 {high-low:.1f} ≤ 허용오차 {tolerance:.1f})")
            break

        print()

    return {
        "mode": mode, "unit": MODE_DEFAULTS[mode]["unit"],
        "best_safe_load": best_safe,
        "first_failure_load": best_failure,
        "threshold_estimate": best_safe or (best_failure and best_failure * 0.8),
        "history": history,
    }


# ── 최종 보고서 ────────────────────────────────────────────────────────────────
def print_report(result: dict) -> None:
    print(f"\n{'='*60}")
    print(" 이분 탐색 결과 보고서")
    print(f"{'='*60}")
    print(f" 모드  : {result['mode']} ({result['unit']})")
    print(f" 안전  : {result['best_safe_load']}")
    print(f" 첫 장애: {result['first_failure_load']}")
    print(f" 추천 임계값: {result['threshold_estimate']}")
    print()
    print(f" {'iter':>4} {'load':>8} {'health':>8} {'bottleneck':>16} {'signal':>10} {'capa%':>6}")
    print(f" {'-'*60}")
    for h in result["history"]:
        signal_icon = {"go_higher": "↑", "go_lower": "↓", "converged": "★"}.get(h["binary_signal"], "?")
        health_short = h["health"][:7]
        print(f" {h['iter']:>4} {h['load']:>8.1f} {health_short:>8} {h['bottleneck']:>16} "
              f" {signal_icon} {h['binary_signal']:>8} {h['capacity_pct']:>5.0f}%")

    print()
    print(" 권장 HPA/KEDA 설정:")
    threshold = result["threshold_estimate"]
    if threshold:
        if result["mode"] == "keda":
            print(f"   KEDA queueLength  → {max(1, int(threshold * 0.1))} (현재 1)")
            print(f"   KEDA maxReplicas  → 10 이하로 유지 (RDS 커넥션 한계)")
        elif "hpa" in result["mode"]:
            print(f"   HPA targetCPU 조정: 현재 임계 RPS {threshold:.0f}")
            print(f"   Pod당 처리 가능 RPS ≈ {threshold / 5:.0f} (replica 5 기준)")
    print(f"{'='*60}\n")


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
        sys.exit("GEMINI_API_KEY 미설정 — .env.local 또는 환경변수 확인")

    parser = argparse.ArgumentParser(description="적응형 이분 탐색 임계값 탐색기")
    parser.add_argument("--mode",     required=True, choices=list(MODE_DEFAULTS))
    parser.add_argument("--min",      type=float, default=None)
    parser.add_argument("--max",      type=float, default=None)
    parser.add_argument("--iters",    type=int,   default=10)
    parser.add_argument("--load-sec", type=int,   default=60)
    parser.add_argument("--wait-sec", type=int,   default=45)
    parser.add_argument("--endpoint", type=str,   default=None)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    # 기본값 적용
    defaults = MODE_DEFAULTS[args.mode]
    if args.min is None: args.min = defaults["min"]
    if args.max is None: args.max = defaults["max"]

    sqs_url      = get_sqs_url()
    api_endpoint = args.endpoint or get_api_endpoint()

    if not sqs_url:
        sys.exit("SQS URL 감지 실패 — SQS_QUEUE_URL 환경변수 또는 terraform output 확인")

    if args.mode in ("hpa-read", "hpa-write", "combined") and not api_endpoint:
        sys.exit("API Gateway 엔드포인트 감지 실패 — --endpoint 또는 API_GW_ENDPOINT 환경변수 설정")

    print(f"\n SQS URL : {sqs_url}")
    print(f" API 엔드: {api_endpoint or '(SQS 전용 모드)'}")
    print(f" 모델    : {MODEL}")

    result = binary_search(args, api_key, sqs_url, api_endpoint)

    # JSON 저장
    out_dir = ROOT / "scripts" / "data"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"threshold-{args.mode}-{ts}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f" 결과 저장: {out_path}")

    print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
