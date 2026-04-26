#!/usr/bin/env bash
set -eu
set -o pipefail 2>/dev/null || true

# v7 전용 진입점: 구현은 run_concert5_then_snapshot.sh 한 곳에만 둔다.
#
# 사용 예:
#   bash ../scripts/run_concert7_then_snapshot.sh --http-concurrency 2000 --duration-sec 10 --show-id 100 -n 30

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# 1) 부하 실행 + 스냅샷
bash "$SCRIPT_DIR/run_concert5_then_snapshot.sh" "$@" -v7

# 2) 스냅샷 이후에도 SQS 처리가 남아있을 수 있어, 큐가 완전히 비는 데 걸린 시간을 출력
bash "$SCRIPT_DIR/wait-sqs-then-print-elapsed.sh"

