#!/usr/bin/env bash
# 大纲 2.2.3 步骤 3（生产环境）：接入链路时延实测。
# 对边缘网关 JSON 接入端口做 N 次 TCP 建连往返，统计 min/avg/max（真实链路时延，
# 非模拟档案）。
set -Eeuo pipefail

EDGE_HOST="${EDGE_HOST:-127.0.0.1}"
JSON_PORT="${EDGE_JSON_PORT:-8888}"
SAMPLES="${SAMPLES:-10}"

python3 - "$EDGE_HOST" "$JSON_PORT" "$SAMPLES" <<'PY'
import socket, statistics, sys, time

host, port, samples = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rtts = []
for _ in range(samples):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=3.0):
            pass
        rtts.append((time.perf_counter() - start) * 1000.0)
    except OSError as exc:
        print("[PING] 连接失败: %s:%s %s" % (host, port, exc))
        sys.exit(1)
    time.sleep(0.2)

print("[PING] %s:%s  样本 %d 次" % (host, port, len(rtts)))
print("  时延: min=%.2f ms  avg=%.2f ms  max=%.2f ms" %
      (min(rtts), statistics.mean(rtts), max(rtts)))
PY
