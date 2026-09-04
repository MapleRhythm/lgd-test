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
# 周期统计行（[JSON][RECV]/[JSON][SEND]）降频：演示默认 30 秒一条
#（EDGE_REPORT_INTERVAL 可覆盖；连接断开时的汇总行不受影响）。
export EDGE_REPORT_INTERVAL="${EDGE_REPORT_INTERVAL:-30}"

# 5G 链路切换显示色：恢复/正常=绿、中断/降级=红（大纲 2.2.5）。着色只在
# 显示层做（本脚本的管道 sed），original/ 里的网关源码不动。控制台不是
# 终端或设置 NO_COLOR 时关闭，与模型层 colour() 同规则。
COLOUR_RED=$'\033[31m'
COLOUR_GREEN=$'\033[32m'
COLOUR_RESET=$'\033[0m'
if [[ ! -t 1 ]] || [[ -n "${NO_COLOR:-}" ]]; then
  COLOUR_RED=""
  COLOUR_GREEN=""
  COLOUR_RESET=""
fi

EDGE_PID=""
RELAY_PID=""
RADIO_PID=""
RADIO_RELAY_OK=0

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

# 短波/卫星专用转发链路（radio relay）与中转同机部署（生产口径）：本地
# 演示由本终端随本地中转一起拉起同一份 production/relay/radio_link_relay.py
# （入口 127.0.0.1:11450 / 出口 127.0.0.1:11550），状态台账放演示状态目录
# 并随新会话重置——与模型台账同一“每次重跑自动清零”口径。远端中转模式
# 下不在本地拉起（远端自带，EDGE_RADIO_RELAY_URL 默认跟随中转机）。
# 联调帧推专用链路不经统一上行，服务器 /stop1 类业务屏蔽指令天然影响
# 不到这条通路（与生产一致）。
ensure_radio_relay() {
  if [[ "$RELAY_HOST" != "127.0.0.1" ]]; then
    return 0
  fi
  if port_open 11450; then
    return 0
  fi
  local state_dir="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}"
  local relay_state="$state_dir/radio-relay"
  mkdir -p "$state_dir"
  rm -rf "$relay_state"
  python3 -u "$SCRIPT_DIR/production/relay/radio_link_relay.py" \
    --ingress-host 127.0.0.1 --ingress-port 11450 \
    --egress-host 127.0.0.1 --egress-port 11550 \
    --state-dir "$relay_state" \
    >>"$state_dir/radio_relay.log" 2>&1 &
  RADIO_PID=$!
  local attempt
  for attempt in $(seq 1 20); do
    if port_open 11450; then
      return 0
    fi
    sleep 0.5
  done
  printf '  background radio relay failed to start (see %s); fallback to unified uplink\n' \
    "$state_dir/radio_relay.log" >&2
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
  if [[ -n "$RADIO_PID" ]]; then
    kill "$RADIO_PID" 2>/dev/null || true
    wait "$RADIO_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

BAR='════════════════════════════════════════════════════════════════════════════════'
printf '\n\033[36m  %s\033[0m\n' "$BAR"
printf '\033[1;36m  %s\033[0m\n' 'EDGE GATEWAY TERMINAL'
printf '\033[36m  %s\033[0m\n' "$BAR"

ensure_transfer_service
# 本地中转就位后随即拉起专用转发链路；失败不阻断演示（网关自动回退
# 统一上行，仅打印 WARN）。
ensure_radio_relay && RADIO_RELAY_OK=1

# 与生产 run_gateway.sh 同款默认（生产侧对齐）：
# - 短波/卫星承载走 5G（EDGE_RADIO_OVER_5G=0 恢复电台/串口硬件口径）；
# - 联调帧优先推专用转发链路入口 11450，失败自动回退统一上行/11503；
#   默认跟随中转机（EDGE_CLOUD_HOST 覆盖时跟随），置空 EDGE_RADIO_RELAY_URL=
#   可恢复不经专用链路。
export EDGE_RADIO_OVER_5G="${EDGE_RADIO_OVER_5G:-1}"
DEMO_RADIO_RELAY_URL="http://${EDGE_CLOUD_HOST:-$RELAY_HOST}:11450"
export EDGE_RADIO_RELAY_URL="${EDGE_RADIO_RELAY_URL-$DEMO_RADIO_RELAY_URL}"
if [[ "$EDGE_RADIO_RELAY_URL" == "$DEMO_RADIO_RELAY_URL" && "$RELAY_HOST" == "127.0.0.1" \
      && "$RADIO_RELAY_OK" != "1" ]]; then
  # 默认指向本地入口但本地 relay 没起来：置空回到不经专用链路的旧路径，
  # 免得网关每帧 WARN。用户显式设置的 URL 不动。
  export EDGE_RADIO_RELAY_URL=
fi

# 卫星上行：联调承载（EDGE_RADIO_OVER_5G=1，默认）下默认启用——身份帧按
# 约 2 分钟一条的节奏即时落专用转发链路（与生产一致）；恢复硬件承载口径
# 时演示机无 400-GM12 串口，维持禁用。EDGE_DISABLE_SATELLITE=1 可显式关闭。
if [[ -z "${EDGE_DISABLE_SATELLITE:-}" && "${EDGE_RADIO_OVER_5G:-1}" != "1" ]]; then
  EDGE_DISABLE_SATELLITE=1
fi

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
import json, os, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as handle:
        state = json.load(handle)
except (OSError, ValueError):
    # 空文件/半截文件（上次启动重写门状态时在 /mnt 上被中断截断）：
    # 会话状态已不可用，而开边缘终端本就是开新会话——直接删除，
    # 让后续命令按“无状态文件=全新会话”起步，别把 load_state 卡死在
    # “存在但读不出”的拒绝分支上。
    os.unlink(path)
    sys.exit(0)
changed = False
for key in ("multi_source_enabled", "whitelist_filter_enabled"):
    if state.get(key):
        state[key] = False
        changed = True
# 上行链路与流程标志同样随新会话回基线（转发门标记已在上面删除，模型侧
# forwarder/上行 online 一起回“转发通道未建立”——上行由 2.2.3 步骤6
# edge_forward --start 拉起；route/encap 为模型侧标志，随台账清零回到
# 全新会话口径）。不复位的话，上一会话残留（例如 link_block --stop 后
# 的模型 5G 状态）会与本会话的服务器/网关实况各说各话。
for key in ("route_enabled", "forwarder_enabled", "encapsulation_enabled"):
    if state.get(key):
        state[key] = False
        changed = True
links = state.get("links")
if isinstance(links, dict):
    for name in ("5g", "shortwave", "satellite"):
        node = links.get(name)
        if isinstance(node, dict) and node.get("online"):
            node["online"] = False
            changed = True
if changed:
    # tmp + os.replace 原子替换（与运行时 save_state 同法）：杜绝
    # open("w") 先截断、写一半被打断留下 0 字节 state.json。
    tmp = path + ".gatetmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
PY
fi

# 开启边缘终端即开启一次新演示会话：清空模型台账（与 init --reset 同一份
# LOG_FILES 清单，含云端实时回传缓存），历史统计不跨会话残留——每次重跑
# trust_access_calculate 等统计自然从零开始，无需手动 init --reset。
PROTOCOL_TEST_STATE_DIR="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}" \
  python3 -c 'import protocol_test_runtime as rt; rt.clear_records(); (rt.STATE_DIR / "cloud_rx_live.jsonl").unlink(missing_ok=True)'
printf '  OK   ledger files cleared (fresh demo session)\n'

# 服务器层同样回新会话基线：本地中转进程常驻、不随终端退出，上一会话
# link_block --stop 下发的组屏蔽（group_enabled=False）会一直粘着——面板
# 持续 5G DOWN 而边缘/云端终端重启无从复位。本启动器新起的中转默认全组
# 启用；只有复用已在跑的中转时补一发幂等 /recover（模型层 5G 已随上面
# 回基线，两层同步）。仅本地中转且演示云端指向本机时执行——远端中转是
# 生产环境（只读），云端指向别处（旁路台架）时也绝不触碰。
if [[ ( "$RELAY_HOST" == "127.0.0.1" || "$RELAY_HOST" == "localhost" ) && -z "$RELAY_PID" ]] \
   && [[ "${EDGE_CLOUD_HOST:-127.0.0.1}" == "127.0.0.1" || "${EDGE_CLOUD_HOST:-127.0.0.1}" == "localhost" ]]; then
  if PROTOCOL_TEST_RELAY_HOST="$RELAY_HOST" \
     PROTOCOL_TEST_STATE_DIR="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}" \
     python3 -c 'import sys, protocol_test_runtime as rt; sys.exit(0 if rt._relay_group_control(True) else 1)' \
     >/dev/null 2>&1; then
    printf '  OK   server 5G group re-enabled (stale block from a previous session cleared)\n'
  else
    printf '  WARN server 5G group recover not delivered (control API unreachable)\n' >&2
  fi
fi

# 显示层把 gateway_1/2/4(5) 之类的通道标识统一映射为 scene_1/2/3
#（仅控制台显示；协议字段与发往中转的报文不变）。
GATEWAY_ARGS=(
  --media-listen-host "${EDGE_MEDIA_HOST:-127.0.0.1}"
  --media-listen-port "${EDGE_MEDIA_PORT:-7777}"
  --json-listen-host "${EDGE_JSON_HOST:-127.0.0.1}"
  --json-listen-port "${EDGE_JSON_PORT:-8888}"
  --cloud-host "${EDGE_CLOUD_HOST:-$RELAY_HOST}"
  --cloud-port "${EDGE_CLOUD_PORT:-11500}"
  --link-status-host "${EDGE_LINK_STATUS_HOST:-$RELAY_HOST}"
  --link-status-port "${EDGE_LINK_STATUS_PORT:-11417}"
  --edge-heartbeat-port "${EDGE_HEARTBEAT_PORT:-11511}"
  --slice-metrics-port "${EDGE_SLICE_METRICS_PORT:-11510}"
  --baotong-host "${EDGE_BAOTONG_HOST:-127.0.0.1}"
  --baotong-port "${EDGE_BAOTONG_PORT:-19100}"
  --time-set-interval 0
  --whitelist-interval "${EDGE_WHITELIST_INTERVAL:-30}"
  --whitelist-filter
  --link-monitor-interval "${EDGE_LINK_MONITOR_INTERVAL:-60}"
  --compact-log
)
if [[ "${EDGE_DISABLE_SATELLITE:-0}" == "1" ]]; then
  GATEWAY_ARGS+=(--disable-satellite)
fi
./edge_node.sh "${GATEWAY_ARGS[@]}" \
  > >(grep --line-buffered -vE '\[WHITELIST\]|\[EDGE-HEARTBEAT\]|\[JSON\]\[SHORTWAVE\]' \
    | sed -u 's/gateway_1/scene_1/g; s/gateway_2/scene_2/g; s/gateway_4/scene_3/g; s/gateway_5/scene_3/g; s/Gateway1/scene_1/g; s/Gateway2/scene_2/g; s/Gateway4/scene_3/g; s/Gateway5/scene_3/g' \
    | sed -u -E "/\[LINK-STATUS\].*connected=False/s/.*/${COLOUR_RED}&${COLOUR_RESET}/; /\[LINK-STATUS\].*connected=True/s/.*/${COLOUR_GREEN}&${COLOUR_RESET}/") &
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
