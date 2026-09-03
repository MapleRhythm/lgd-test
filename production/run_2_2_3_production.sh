#!/usr/bin/env bash
# 大纲 2.2.3 生产环境联调流程（单机版：边缘网关 + 端侧发送同机运行）。
#
# 与真机三节点部署的差别：网关与发送器同机、卫星串口关闭、端口错开演示环境。
# 转发通道一经 --start 激活，将把真实业务流发往生产中转 RELAY_HOST
# （默认 47.99.47.169，借用白名单设备 ID）——请确认后再跑后续步骤。
#
# 端口默认 18888/17777/19118，与演示三终端（8888/7777/19100）完全错开；
# 若仍被占用（本脚本启动前检测）会直接退出，绝不自动清理他人进程。
set -Eeuo pipefail

PROD_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROD_DIR"

RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
EDGE_JSON_PORT="${EDGE_JSON_PORT:-18888}"
EDGE_MEDIA_PORT="${EDGE_MEDIA_PORT:-17777}"
EDGE_BAOTONG_PORT="${EDGE_BAOTONG_PORT:-19118}"
DEVICE_ID="${DEVICE_ID:-3C15DB07}"
KEEP_DURATION="${KEEP_DURATION:-60}"
BANDWIDTH_DURATION="${BANDWIDTH_DURATION:-5}"
START_COUNT="${START_COUNT:-5}"
STATE_DIR="$PROD_DIR/.state"
GW_PID=""

section() {
  local bar='════════════════════════════════════════════════════════════════════════════════'
  printf '\n%s\n  %s\n%s\n' "$bar" "$1" "$bar"
}

cleanup() {
  if [[ -n "$GW_PID" ]]; then
    kill "$GW_PID" 2>/dev/null || true
    wait "$GW_PID" 2>/dev/null || true
  fi
  bash "$PROD_DIR/edge/init_link_connect.sh" --reset >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

port_free() {
  python3 - "$1" <<'PY'
import socket, sys
with socket.socket() as s:
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        sys.exit(0)   # 连不上=端口空闲
sys.exit(1)
PY
}

section '0  环境自检'
for p in "$EDGE_JSON_PORT" "$EDGE_MEDIA_PORT" "$EDGE_BAOTONG_PORT"; do
  if ! port_free "$p"; then
    echo "端口 $p 已被占用（演示边缘终端在跑？）。停掉它，或用 EDGE_JSON_PORT/EDGE_MEDIA_PORT/EDGE_BAOTONG_PORT 换端口。" >&2
    exit 1
  fi
done
echo "接入端口 $EDGE_JSON_PORT/$EDGE_MEDIA_PORT 空闲；中转目标 $RELAY_HOST:11500；设备 ID $DEVICE_ID"

section '1  启动边缘网关（生产参数，默认不受理、不转发）'
mkdir -p "$STATE_DIR"
# 会话复位：清掉残留标记（接入门/转发门/过滤门），异常退出残留的
# 转发标记会导致网关一启动就向生产中转转发。
bash "$PROD_DIR/edge/init_link_connect.sh" --reset >/dev/null 2>&1 || true
EDGE_STATE_DIR="$PROD_DIR/edge/.state" \
EDGE_JSON_PORT="$EDGE_JSON_PORT" EDGE_MEDIA_PORT="$EDGE_MEDIA_PORT" \
EDGE_BAOTONG_PORT="$EDGE_BAOTONG_PORT" EDGE_BAOTONG_HOST=127.0.0.1 \
EDGE_CLOUD_HOST="$RELAY_HOST" \
EDGE_DISABLE_SATELLITE=1 \
bash "$PROD_DIR/edge/run_gateway.sh" >"$STATE_DIR/gateway.log" 2>&1 &
GW_PID=$!
echo "gateway pid=$GW_PID  log=$STATE_DIR/gateway.log"

for i in $(seq 1 20); do
  if port_free "$EDGE_JSON_PORT"; then sleep 0.5; else break; fi
done
if port_free "$EDGE_JSON_PORT"; then
  echo "边缘网关未能监听 $EDGE_JSON_PORT，日志尾部：" >&2
  tail -n 20 "$STATE_DIR/gateway.log" >&2
  exit 1
fi
echo "边缘网关就绪（JSON $EDGE_JSON_PORT / 媒体 $EDGE_MEDIA_PORT）"

export EDGE_HOST=127.0.0.1
export EDGE_JSON_PORT EDGE_MEDIA_PORT
SEND="python3 $PROD_DIR/device/send_business.py --host 127.0.0.1 --port $EDGE_JSON_PORT --device-id $DEVICE_ID"

section '2  初始化接入链路（init_link_connect，开始受理端侧数据）'
bash "$PROD_DIR/edge/init_link_connect.sh"

section '3  接入链路连通性检查'
bash "$PROD_DIR/device/check_link.sh"

section '4  接入链路时延实测'
bash "$PROD_DIR/device/ping_link.sh"

section '5  业务数据发送（start_test）'
$SEND --count "$START_COUNT" --fg

section '6  边缘网关服务日志'
tail -n 30 "$STATE_DIR/gateway.log"

section '7  持续传输（keep_transfer，仅有线）'
$SEND --link wired --duration "$KEEP_DURATION" --interval 1 --fg

section '8  多模态并发传输（Wi-Fi/蓝牙/有线）'
$SEND --link all --duration "$BANDWIDTH_DURATION" --interval 1 --fg

section '9  建立边缘网关->云端转发通道（真实流量将发往生产中转）'
bash "$PROD_DIR/edge/edge_forward.sh" --start
sleep 2
tail -n 5 "$STATE_DIR/gateway.log"

section '10  转发通道建立后业务数据端到端发送'
$SEND --count "$START_COUNT" --fg

section '11  端到端链路数据查询（生产只读验证）'
bash "$PROD_DIR/cloud/query_relay_state.sh"
echo
echo '端侧发送审计（对账用，最近3条）：'
tail -n 3 "$PROD_DIR/device/.state/sent.jsonl" 2>/dev/null || echo '（无发送记录）'

section '完成'
echo "清理：停止边缘网关（pid=$GW_PID）并移除转发标记"
