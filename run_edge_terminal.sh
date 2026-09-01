#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# This terminal talks to the gateway/relay over plain HTTP.  A desktop proxy
# exported as http_proxy is usually unreachable from WSL (NAT mode) and would
# break every urllib-based sync (whitelist, queries), so drop it here.
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="127.0.0.1,localhost"
export NO_PROXY="$no_proxy"
# The gateway flushes per log line, so piping stays live.
export PYTHONUNBUFFERED=1

EDGE_PID=""
RELAY_PID=""

port_open() {
  python3 - "$1" <<'PY'
import socket, sys
with socket.socket() as probe:
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        sys.exit(1)
PY
}

# Single knob for the deployment topology: the relay the edge gateway sends
# to.  Default 127.0.0.1 keeps everything local; set PROTOCOL_TEST_RELAY_HOST
# (e.g. 47.99.47.169) to send through the relay running elsewhere.  The relay
# itself is never shown in any terminal.
RELAY_HOST="${PROTOCOL_TEST_RELAY_HOST:-127.0.0.1}"

# The edge-cloud transfer service runs detached from every terminal: its
# console output goes to a file only and no terminal ever shows it.  It is
# only started for a local relay; a remote one is managed on its own side.
ensure_transfer_service() {
  local port="${EDGE_RELAY_PORT:-11500}"
  if [[ "$RELAY_HOST" != "127.0.0.1" ]]; then
    return 0
  fi
  if port_open "$port"; then
    return 0
  fi
  local state_dir="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}"
  mkdir -p "$state_dir"
  python3 -u "$SCRIPT_DIR/original/server_v8.py" \
    --host 127.0.0.1 \
    >>"$state_dir/server_v8.log" 2>&1 &
  RELAY_PID=$!
  local attempt
  for attempt in $(seq 1 20); do
    if port_open "$port"; then
      return 0
    fi
    sleep 0.5
  done
  printf '  background transfer service failed to start (see %s)\n' "$state_dir/server_v8.log" >&2
  return 1
}

cleanup() {
  if [[ -n "$EDGE_PID" ]]; then
    kill "$EDGE_PID" 2>/dev/null || true
    wait "$EDGE_PID" 2>/dev/null || true
  fi
  if [[ -n "$RELAY_PID" ]]; then
    kill "$RELAY_PID" 2>/dev/null || true
    wait "$RELAY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

BAR='════════════════════════════════════════════════════════════════════════════════'
printf '\n\033[36m  %s\033[0m\n' "$BAR"
printf '\033[1;36m  %s\033[0m\n' 'EDGE GATEWAY TERMINAL'
printf '\033[36m  %s\033[0m\n' "$BAR"

ensure_transfer_service

# 大纲 2.2.3 步骤6：转发通道初始关闭，由 ./edge_forward.sh --start 建立。
FORWARD_MARKER="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}/edge_forward.enabled"
mkdir -p "$(dirname "$FORWARD_MARKER")"
rm -f "$FORWARD_MARKER"

# 大纲 2.2.4 多源接入门同样随边缘终端启动复位：./multi_source_access.sh
# 执行前不受理端侧数据；可信接入过滤也复位为未启用（trust_access_
# add_whitelist 执行后生效）。真网关标记与本地模型状态一起关，两层一致。
GATE_MARKER="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}/multi_source_access.enabled"
FILTER_MARKER="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}/whitelist_filter.enabled"
rm -f "$GATE_MARKER" "$FILTER_MARKER"
STATE_FILE="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}/state.json"
if [[ -f "$STATE_FILE" ]]; then
  python3 - "$STATE_FILE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    state = json.load(handle)
changed = False
for key in ("multi_source_enabled", "whitelist_filter_enabled"):
    if state.get(key):
        state[key] = False
        changed = True
if changed:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
PY
fi

# 显示层把 gateway_1/2/4(5) 之类的通道标识统一映射为 scene_1/2/3
#（仅控制台显示；协议字段与发往中转的报文不变）。
./edge_node.sh \
  --media-listen-host "${EDGE_MEDIA_HOST:-127.0.0.1}" \
  --media-listen-port "${EDGE_MEDIA_PORT:-7777}" \
  --json-listen-host "${EDGE_JSON_HOST:-127.0.0.1}" \
  --json-listen-port "${EDGE_JSON_PORT:-8888}" \
  --cloud-host "${EDGE_CLOUD_HOST:-$RELAY_HOST}" \
  --cloud-port "${EDGE_CLOUD_PORT:-11500}" \
  --link-status-host "${EDGE_LINK_STATUS_HOST:-$RELAY_HOST}" \
  --link-status-port "${EDGE_LINK_STATUS_PORT:-11417}" \
  --edge-heartbeat-port "${EDGE_HEARTBEAT_PORT:-11511}" \
  --slice-metrics-port "${EDGE_SLICE_METRICS_PORT:-11510}" \
  --baotong-host "${EDGE_BAOTONG_HOST:-127.0.0.1}" \
  --baotong-port "${EDGE_BAOTONG_PORT:-19100}" \
  --disable-satellite \
  --time-set-interval 0 \
  --whitelist-interval "${EDGE_WHITELIST_INTERVAL:-30}" \
  --whitelist-filter \
  --link-monitor-interval "${EDGE_LINK_MONITOR_INTERVAL:-60}" \
  --compact-log > >(grep --line-buffered -vE '\[WHITELIST\]|\[EDGE-HEARTBEAT\]|\[JSON\]\[SHORTWAVE\]' \
    | sed -u 's/gateway_1/scene_1/g; s/gateway_2/scene_2/g; s/gateway_4/scene_3/g; s/gateway_5/scene_3/g; s/Gateway1/scene_1/g; s/Gateway2/scene_2/g; s/Gateway4/scene_3/g; s/Gateway5/scene_3/g') &
EDGE_PID=$!

sleep "${EDGE_STARTUP_WAIT:-3}"
if ! kill -0 "$EDGE_PID" 2>/dev/null; then
  wait "$EDGE_PID" 2>/dev/null || true
  printf '  edge gateway failed to start\n' >&2
  exit 1
fi
export PROTOCOL_TEST_LIVE=1
export PROTOCOL_TEST_CLOUD_LIVE=1
export PROTOCOL_TEST_GATEWAY_HOST="${PROTOCOL_TEST_GATEWAY_HOST:-127.0.0.1}"
export PROTOCOL_TEST_GATEWAY_PORT="${PROTOCOL_TEST_GATEWAY_PORT:-${EDGE_JSON_PORT:-8888}}"
export PROTOCOL_TEST_STATE_DIR="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}"
# The edge gateway fetches its whitelist from the relay (read-only); mirror
# the same source for the local model so both agree on ACCEPT/BLOCK.
export PROTOCOL_TEST_WHITELIST_URL="${PROTOCOL_TEST_WHITELIST_URL:-http://${EDGE_CLOUD_HOST:-$RELAY_HOST}:11502/whitelist}"

printf '\n\033[32m  OK\033[0m   edge gateway started, enter the document commands in this terminal\n'
printf '  exit the prompt to stop the edge gateway\n\n'

PS1='edge> ' bash --noprofile --norc -i
