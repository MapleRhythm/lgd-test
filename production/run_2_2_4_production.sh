#!/usr/bin/env bash
# 大纲 2.2.4 生产环境联调流程（单机版：边缘网关 + 端侧发送同机运行）。
#
# 与真机三节点部署的差别：网关与发送器同机、端口错开演示环境。
# 三台终端设备合并为单终端：三条业务发送指令（+火情上报指令）在同一
# 终端顺序输入，每条只打印 [LAUNCH] 业务发送启动日志即返回，实际发送
# 在后台进行（send_business.py 默认即后台；本脚本各轮均显式给 --duration，
# 按 --duration 自行结束）。
# 2.2.4 前提按开发侧语义先打开转发通道（云端一致性核对需要）——转发一经
# 激活，接入门打开后到达的真实业务流会发往生产中转 RELAY_HOST（默认
# 47.99.47.169，借用白名单设备 ID）；接入门打开前到达的报文只计接收统计
# （gate_drop），不转发、不追溯改判。
#
# 端口默认 18888/17777/19118，与演示三终端（8888/7777/19100）完全错开；
# 若仍被占用（本脚本启动前检测）会直接退出，绝不自动清理他人进程。
#
# 2.2.4 直发模式（EDGE_RADIO_OVER_5G=1；run_gateway.sh 默认已启用，此处
# 显式写出便于识别；真机恢复硬件通道设 0）：
# 短波/卫星报文不经电台/卫星模块，复用统一上行通道直发核心网关——短波
# 时延 EDGE_SW_DELAY_S（默认 20s）；卫星为发送节奏 EDGE_SAT_DELAY_S（默认
# 120s）：帧立即落地，一条落地后按基准±抖动发下一条（约 2 分钟一条、连续
# 发送），短波时延在 ±EDGE_SW_JITTER_S（默认 3s）/ 卫星节奏在
# ±EDGE_SAT_JITTER_S（默认 10s）内逐条随机波动（模拟信道传播起伏）；
# 网关控制台仍按电台/卫星口径打印
# （[BAOTONG-V2][SEND] / [SATELLITE][*]），不体现实际承载。
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
# 直发模式链路参数（秒）：短波为发送时延（基准±抖动，逐报文随机波动）；
# 卫星为发送节奏（一条立即落地后等基准±抖动再发下一条，约 2 分钟一条）。
# 卫星串口版身份帧周期（EDGE_SATELLITE_INTERVAL，联调模式不参与节奏，
# 传 0 表示发一条后空闲）与回看等待秒数。
SW_DELAY_S="${EDGE_SW_DELAY_S:-20}"
SW_JITTER_S="${EDGE_SW_JITTER_S:-3}"
SAT_DELAY_S="${EDGE_SAT_DELAY_S:-120}"
SAT_JITTER_S="${EDGE_SAT_JITTER_S:-10}"
SAT_INTERVAL="${EDGE_SATELLITE_INTERVAL:-150}"
SAT_LAND_WAIT="${SAT_LAND_WAIT:-1}"
STATE_DIR="$PROD_DIR/.state"
EDGE_STATE="$PROD_DIR/edge/.state"
GW_PID=""
LOGS=()

section() {
  local bar='════════════════════════════════════════════════════════════════════════════════'
  printf '\n%s\n  %s\n%s\n' "$bar" "$1" "$bar"
}

cleanup() {
  # 后台发送器按 --duration 自行结束（中断时最长残留一轮时长），无需杀。
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

launch_sender() {  # launch_sender <日志名> <send_business 参数...>（单终端形态：只回显启动日志）
  local log="$STATE_DIR/$1"; shift
  rm -f "$log"
  python3 "$PROD_DIR/device/send_business.py" --host 127.0.0.1 --port "$EDGE_JSON_PORT" \
      --background --bg-log "$log" "$@"
  LOGS+=("$log")
}

wait_round() {  # wait_round <本轮时长秒>：等本轮后台发送器写出 [SUMMARY] 再打印摘要
  local budget log left waited
  budget="$(python3 -c 'import sys; print(int(float(sys.argv[1]) * 2 + 15))' "$1")"
  waited=0
  left=1
  while (( left )) && (( waited < budget )); do
    left=0
    for log in "${LOGS[@]}"; do
      if ! grep -qF '[SUMMARY]' "$log" 2>/dev/null; then left=1; fi
    done
    if (( left )); then
      sleep 1
      waited=$((waited + 1))
    fi
  done
  for log in "${LOGS[@]}"; do
    echo "  -- $(basename "$log")"
    grep -F '[SUMMARY]' "$log" 2>/dev/null || tail -n 3 "$log" 2>/dev/null || echo '  （无输出）'
  done
  if (( left )); then
    echo "  （等待超时 ${budget}s，以上为日志尾部）"
  else
    echo "  本轮后台发送全部结束"
  fi
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
echo "直发模式：短波时延 ${SW_DELAY_S}±${SW_JITTER_S}s / 卫星节奏 ${SAT_DELAY_S}±${SAT_JITTER_S}s 一条（立即落地、连续发送；回看等待 SAT_LAND_WAIT=${SAT_LAND_WAIT}s）"
if [[ -n "${EDGE_RADIO_RELAY_URL:-}" ]]; then
  echo "短波/卫星专用转发链路: $EDGE_RADIO_RELAY_URL（联调帧改推专用链路；接收记录并入第 7 步 query_relay_state.sh 的接收表打印）"
fi

section '1  启动边缘网关（生产参数，默认不受理、不转发）'
mkdir -p "$STATE_DIR"
# 会话复位：清掉残留标记（接入门/转发门/过滤门），异常退出残留的
# 转发标记会导致网关一启动就向生产中转转发。
bash "$PROD_DIR/edge/init_link_connect.sh" --reset >/dev/null 2>&1 || true
EDGE_STATE_DIR="$EDGE_STATE" \
EDGE_JSON_PORT="$EDGE_JSON_PORT" EDGE_MEDIA_PORT="$EDGE_MEDIA_PORT" \
EDGE_BAOTONG_PORT="$EDGE_BAOTONG_PORT" EDGE_BAOTONG_HOST=127.0.0.1 \
EDGE_CLOUD_HOST="$RELAY_HOST" \
EDGE_RADIO_OVER_5G=1 \
EDGE_SW_DELAY_S="$SW_DELAY_S" EDGE_SW_JITTER_S="$SW_JITTER_S" \
EDGE_SAT_DELAY_S="$SAT_DELAY_S" EDGE_SAT_JITTER_S="$SAT_JITTER_S" \
EDGE_SATELLITE_INTERVAL="$SAT_INTERVAL" \
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

section '3  多源业务接入·接入门打开前（单终端依次启动三路业务，只计接收统计）'
# 单终端合并形态：三条业务发送指令顺序输入，每条只打印 [LAUNCH] 业务
# 发送启动日志即返回提示符，三路业务在后台同时发送（明细写各自日志）。
# 视频流=有线，传感器=Wi-Fi，环境监测=三链路逐条轮换（与现网一致）。
# 本轮报文在接入门打开前到达：边缘只计 gate_drop，不校验白名单、不转发。
launch_sender pre-video.log --device-id "$DEVICE_VIDEO" --biz-type video --link wired \
    --duration "$SOURCE_DURATION" --interval 1
launch_sender pre-sensor.log --device-id "$DEVICE_SENSOR" --biz-type sensor --link wifi \
    --duration "$SOURCE_DURATION" --interval 1
launch_sender pre-env.log --device-id "$DEVICE_ENV" --biz-type env --link rotate \
    --duration "$SOURCE_DURATION" --interval 1
wait_round "$SOURCE_DURATION"

section '4  多源接入门打开（multi_source_access）'
EDGE_STATE_DIR="$EDGE_STATE" bash "$PROD_DIR/edge/multi_source_access.sh"

section '5  接入门打开后再发 + 视频流终端火情上报（受理并转发上云）'
# 单终端依次输入三条业务指令 + 火情上报指令（同视频流终端设备身份、
# 有线链路，每 10s 一条 fire 报文，默认无火情 false；--fire true 模拟有火情）。
launch_sender post-video.log --device-id "$DEVICE_VIDEO" --biz-type video --link wired \
    --duration "$POST_DURATION" --interval 1
launch_sender post-sensor.log --device-id "$DEVICE_SENSOR" --biz-type sensor --link wifi \
    --duration "$POST_DURATION" --interval 1
launch_sender post-env.log --device-id "$DEVICE_ENV" --biz-type env --link rotate \
    --duration "$POST_DURATION" --interval 1
launch_sender post-fire.log --device-id "$DEVICE_VIDEO" --biz-type fire --link wired \
    --interval "$FIRE_INTERVAL" --duration "$POST_DURATION"
wait_round "$POST_DURATION"

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
    --device-id "$ILLEGAL_DEVICE_ID" --count "$ILLEGAL_COUNT" --fg
python3 "$PROD_DIR/device/send_business.py" --host 127.0.0.1 --port "$EDGE_JSON_PORT" \
    --device-id "$UNKNOWN_DEVICE_ID" --count "$ILLEGAL_COUNT" --fg
echo '网关拒收明细（最近5条）：'
grep -F '[WHITELIST][BLOCK]' "$STATE_DIR/gateway.log" | tail -n 5 || true

section '10  可信接入统计（trust_access_calculate）'
EDGE_LOG="$STATE_DIR/gateway.log" bash "$PROD_DIR/edge/trust_access_calculate.sh"

section '11  卫星上行回看（立即落地，节奏约 2 分钟一条）'
# 卫星上行无压帧时延：帧入队即送达云端卫星接收口，随后按
# SAT_DELAY_S±SAT_JITTER_S（默认约 2 分钟）的节奏发下一条（连续发送）。
# 这里只固定等 SAT_LAND_WAIT 秒（默认 1，0 跳过）让 HTTP 往返与日志落盘，
# 再回看网关的卫星/短波上行日志。
if [[ "$SAT_LAND_WAIT" != "0" ]]; then
  echo "卫星立即落地、约 ${SAT_DELAY_S}±${SAT_JITTER_S}s 一条：固定等待 ${SAT_LAND_WAIT}s 后回看日志"
  sleep "$SAT_LAND_WAIT"
fi
echo '网关卫星上行日志（最近12条）：'
grep -E '\[SATELLITE\]' "$STATE_DIR/gateway.log" | tail -n 12 || true
echo '网关短波上行日志（最近4条）：'
grep -F '[BAOTONG-V2][SEND]' "$STATE_DIR/gateway.log" | tail -n 4 || true

section '完成'
echo "清理：停止本轮发送进程与边缘网关（pid=$GW_PID）并移除接入门/转发门/过滤门标记"
