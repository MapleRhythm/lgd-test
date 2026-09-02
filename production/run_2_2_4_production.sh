#!/usr/bin/env bash
# 大纲 2.2.4 生产环境联调流程（单机版：边缘网关 + 端侧发送同机运行）。
#
# 与真机三节点部署的差别：网关与发送器同机、卫星串口关闭、端口错开演示环境。
# 2.2.4 前提按开发侧语义先打开转发通道（云端一致性核对需要）——转发一经
# 激活，接入门打开后到达的真实业务流会发往生产中转 RELAY_HOST（默认
# 47.99.47.169，借用白名单设备 ID）；接入门打开前到达的报文只计接收统计
# （gate_drop），不转发、不追溯改判。
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
# 三终端设备 ID（借用中转白名单内在册设备）；名单外非法设备另用 ILLEGAL/UNKNOWN。
DEVICE_VIDEO="${DEVICE_VIDEO:-182D48D7}"
DEVICE_SENSOR="${DEVICE_SENSOR:-3C15DB07}"
DEVICE_ENV="${DEVICE_ENV:-990E261B}"
ILLEGAL_DEVICE_ID="${ILLEGAL_DEVICE_ID:-ILLEGAL-SENSOR}"
UNKNOWN_DEVICE_ID="${UNKNOWN_DEVICE_ID:-UNKNOWN-001}"
SOURCE_DURATION="${SOURCE_DURATION:-5}"
POST_DURATION="${POST_DURATION:-12}"
FIRE_INTERVAL="${FIRE_INTERVAL:-10}"
ILLEGAL_COUNT="${ILLEGAL_COUNT:-5}"
STATE_DIR="$PROD_DIR/.state"
EDGE_STATE="$PROD_DIR/edge/.state"
GW_PID=""
PIDS=()
LOGS=()

section() {
  local bar='════════════════════════════════════════════════════════════════════════════════'
  printf '\n%s\n  %s\n%s\n' "$bar" "$1" "$bar"
}

cleanup() {
  if ((${#PIDS[@]})); then
    for pid in "${PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    done
  fi
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

start_sender() {  # start_sender <日志名> <send_business 参数...>（后台运行并登记 PID）
  local log="$STATE_DIR/$1"; shift
  python3 "$PROD_DIR/device/send_business.py" --host 127.0.0.1 --port "$EDGE_JSON_PORT" "$@" \
      >"$log" 2>&1 &
  PIDS+=("$!")
  LOGS+=("$log")
  echo "  发送进程 pid=$! 日志=$log"
}

wait_senders() {  # 等本轮登记的发送进程全部退出，打印各自 [SUMMARY]
  local fail=0 pid rc log
  for pid in "${PIDS[@]}"; do
    rc=0
    wait "$pid" || rc=$?
    if [[ $rc -ne 0 ]]; then
      fail=$((fail + 1))
    fi
  done
  for log in "${LOGS[@]}"; do
    echo "  -- $(basename "$log")"
    grep -F '[SUMMARY]' "$log" || tail -n 2 "$log"
  done
  echo "  本轮发送进程全部结束（失败 $fail 个）"
  PIDS=()
  LOGS=()
}

section '0  环境自检'
for p in "$EDGE_JSON_PORT" "$EDGE_MEDIA_PORT" "$EDGE_BAOTONG_PORT"; do
  if ! port_free "$p"; then
    echo "端口 $p 已被占用（演示边缘终端在跑？）。停掉它，或用 EDGE_JSON_PORT/EDGE_MEDIA_PORT/EDGE_BAOTONG_PORT 换端口。" >&2
    exit 1
  fi
done
echo "接入端口 $EDGE_JSON_PORT/$EDGE_MEDIA_PORT 空闲；中转目标 $RELAY_HOST:11500"
echo "三终端设备 ID：视频 $DEVICE_VIDEO（有线）/ 传感器 $DEVICE_SENSOR（Wi-Fi）/ 环境监测 $DEVICE_ENV（轮换）"

section '1  启动边缘网关（生产参数，默认不受理、不转发）'
mkdir -p "$STATE_DIR"
# 会话复位：清掉残留标记（接入门/转发门/过滤门），异常退出残留的
# 转发标记会导致网关一启动就向生产中转转发。
bash "$PROD_DIR/edge/init_link_connect.sh" --reset >/dev/null 2>&1 || true
EDGE_STATE_DIR="$EDGE_STATE" \
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

section '2  打开边缘网关->云端转发通道（2.2.4 前提：云端一致性核对）'
# 多源接入门保持关闭（与 2.2.3 的 init_link_connect 不同）：开闸前到达的
# 端侧报文只计接收统计，用于多源接入前后的受理对比。
EDGE_STATE_DIR="$EDGE_STATE" bash "$PROD_DIR/edge/edge_forward.sh" --start
sleep 2
tail -n 5 "$STATE_DIR/gateway.log"

section '3  多源业务接入·接入门打开前（三终端并发发送，只计接收统计）'
# 视频流=有线，传感器=Wi-Fi，环境监测=三链路逐条轮换（与现网一致）。
# 本轮报文在接入门打开前到达：边缘只计 gate_drop，不校验白名单、不转发。
start_sender pre-video.log --device-id "$DEVICE_VIDEO" --biz-type video --link wired \
    --duration "$SOURCE_DURATION" --interval 1
start_sender pre-sensor.log --device-id "$DEVICE_SENSOR" --biz-type sensor --link wifi \
    --duration "$SOURCE_DURATION" --interval 1
start_sender pre-env.log --device-id "$DEVICE_ENV" --biz-type env --link rotate \
    --duration "$SOURCE_DURATION" --interval 1
wait_senders

section '4  多源接入门打开（multi_source_access）'
EDGE_STATE_DIR="$EDGE_STATE" bash "$PROD_DIR/edge/multi_source_access.sh"

section '5  接入门打开后三终端再发 + 视频流终端火情上报（受理并转发上云）'
# 火情随视频流终端上报：同一设备身份、有线链路，每 10s 一条 fire 报文
# （默认无火情 false；--fire true 模拟有火情）。
start_sender post-video.log --device-id "$DEVICE_VIDEO" --biz-type video --link wired \
    --duration "$POST_DURATION" --interval 1
start_sender post-sensor.log --device-id "$DEVICE_SENSOR" --biz-type sensor --link wifi \
    --duration "$POST_DURATION" --interval 1
start_sender post-env.log --device-id "$DEVICE_ENV" --biz-type env --link rotate \
    --duration "$POST_DURATION" --interval 1
start_sender post-fire.log --device-id "$DEVICE_VIDEO" --biz-type fire --link wired \
    --interval "$FIRE_INTERVAL" --duration "$POST_DURATION"
wait_senders

section '6  边缘网关服务日志（query_service_log）'
tail -n 40 "$STATE_DIR/gateway.log"

section '7  端到端链路数据查询（多源上云核对，生产只读）'
RELAY_HOST="$RELAY_HOST" bash "$PROD_DIR/cloud/query_relay_state.sh"
echo
echo '端侧发送审计（对账用，最近5条）：'
tail -n 5 "$PROD_DIR/device/.state/sent.jsonl" 2>/dev/null || echo '（无发送记录）'

section '8  可信接入·拉取服务器白名单并生效（trust_access_add_whitelist）'
# 白名单只读拉取（中转 HTTP 11502，与网关自身同一来源），随后名单过滤生效。
# 三台借用设备逐个在册校验；ILLEGAL-SENSOR 不在册，将出现 WARN 提示。
RELAY_HOST="$RELAY_HOST" EDGE_STATE_DIR="$EDGE_STATE" \
  bash "$PROD_DIR/edge/trust_access_add_whitelist.sh" \
    "$DEVICE_VIDEO" "$DEVICE_SENSOR" "$DEVICE_ENV" "$ILLEGAL_DEVICE_ID"

section '9  名单外设备发送（非法终端被边缘网关拒收）'
python3 "$PROD_DIR/device/send_business.py" --host 127.0.0.1 --port "$EDGE_JSON_PORT" \
    --device-id "$ILLEGAL_DEVICE_ID" --count "$ILLEGAL_COUNT"
python3 "$PROD_DIR/device/send_business.py" --host 127.0.0.1 --port "$EDGE_JSON_PORT" \
    --device-id "$UNKNOWN_DEVICE_ID" --count "$ILLEGAL_COUNT"
echo '网关拒收明细（最近5条）：'
grep -F '[WHITELIST][BLOCK]' "$STATE_DIR/gateway.log" | tail -n 5 || true

section '10  可信接入统计（trust_access_calculate）'
EDGE_LOG="$STATE_DIR/gateway.log" bash "$PROD_DIR/edge/trust_access_calculate.sh"

section '完成'
echo "清理：停止本轮发送进程与边缘网关（pid=$GW_PID）并移除接入门/转发门/过滤门标记"
