#!/usr/bin/env bash
# 大纲 2.2.3 步骤 2（生产环境）：端侧设备到边缘网关接入链路连通性检查。
# 真实 TCP 探测：JSON 接入 8888 与媒体接入 7777。
set -Eeuo pipefail

EDGE_HOST="${EDGE_HOST:-127.0.0.1}"
JSON_PORT="${EDGE_JSON_PORT:-8888}"
MEDIA_PORT="${EDGE_MEDIA_PORT:-7777}"

probe() {
  local port="$1" label="$2"
  if python3 - "$EDGE_HOST" "$port" <<'PY'
import socket, sys
with socket.socket() as s:
    s.settimeout(3.0)
    try:
        s.connect((sys.argv[1], int(sys.argv[2])))
    except OSError:
        sys.exit(1)
PY
  then
    echo "[LINK] $label  $EDGE_HOST:$port  连通"
  else
    echo "[LINK] $label  $EDGE_HOST:$port  不通" >&2
    return 1
  fi
}

rc=0
probe "$JSON_PORT" "JSON接入" || rc=1
probe "$MEDIA_PORT" "媒体接入" || rc=1
if [[ $rc -eq 0 ]]; then
  echo "[LINK] 端侧设备 -> 边缘网关 接入链路正常"
fi
exit $rc
