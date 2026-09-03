#!/usr/bin/env bash
# 大纲 2.2.4 生产环境流程（在端侧设备真机执行）。
# 一键版，联调自检用；现场正式执行按 ../STEP_BY_STEP.md 逐步输入命令。
#
# 前提：
#   - 边缘网关真机已运行 ../edge/run_gateway.sh（默认不受理、不转发）；
#     短波/卫星默认走 5G 统一上行直发核心网关（EDGE_RADIO_OVER_5G 默认 1，
#     真机接电台/串口时设 0，见 README「大纲 2.2.4 流程」）
#   - EDGE_HOST 指向边缘网关地址（默认 127.0.0.1，设备与网关同机时）
#   - 可选 EDGE_SSH=<user@edge-host>：<edge-dir> —— 边缘侧步骤自动经 ssh
#     在边缘网关上执行；未设置时打印人工执行指令
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export EDGE_HOST="${EDGE_HOST:-127.0.0.1}"
EDGE_JSON_PORT="${EDGE_JSON_PORT:-8888}"
RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
DEVICE_VIDEO="${DEVICE_VIDEO:-182D48D7}"
DEVICE_SENSOR="${DEVICE_SENSOR:-3C15DB07}"
DEVICE_ENV="${DEVICE_ENV:-990E261B}"
ILLEGAL_DEVICE_ID="${ILLEGAL_DEVICE_ID:-ILLEGAL-SENSOR}"
UNKNOWN_DEVICE_ID="${UNKNOWN_DEVICE_ID:-UNKNOWN-001}"
SOURCE_DURATION="${SOURCE_DURATION:-5}"
POST_DURATION="${POST_DURATION:-12}"
FIRE_INTERVAL="${FIRE_INTERVAL:-10}"
ILLEGAL_COUNT="${ILLEGAL_COUNT:-5}"
EDGE_SSH="${EDGE_SSH:-}"
EDGE_REMOTE_DIR="${EDGE_REMOTE_DIR:-$SCRIPT_DIR/../edge}"
EDGE_LOG="${EDGE_LOG:-gateway.log}"
SEND_LOG_DIR="$SCRIPT_DIR/.state"
LOGS=()

section() {
  local bar='════════════════════════════════════════════════════════════════════════════════'
  printf '\n%s\n  %s\n%s\n' "$bar" "$1" "$bar"
}

on_edge() {  # 在边缘网关执行（有 EDGE_SSH 则远程，否则打印指令）
  local cmd="$1"
  if [[ -n "$EDGE_SSH" ]]; then
    ssh "$EDGE_SSH" "cd '$EDGE_REMOTE_DIR' && $cmd"
  else
    echo "  [边缘网关执行] $cmd"
  fi
}

# 后台发送器按 --duration 自行结束（中断时最长残留一轮时长），无需清理。
launch_sender() {  # launch_sender <日志名> <send_business 参数...>（单终端形态：只回显启动日志）
  local log="$SEND_LOG_DIR/$1"; shift
  mkdir -p "$SEND_LOG_DIR"
  rm -f "$log"
  python3 send_business.py --host "$EDGE_HOST" --port "$EDGE_JSON_PORT" \
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

section '2.2.4  前提：会话复位并打开边缘->云端转发通道（在边缘网关执行）'
# 复位会关上接入门/转发门/过滤门（2.2.4 从"不受理"起步），随后按云端
# 一致性核对需要单独打开转发通道；接入门保持关闭到步骤 multi_source_access。
echo '  前提确认：边缘网关须以 2.2.4 直发模式启动（见 README 2.2.4 节）'
on_edge "./init_link_connect.sh --reset && ./edge_forward.sh --start"

section '2.2.4  多源业务接入·接入门打开前（单终端依次启动三路业务）'
# 单终端合并形态：三条业务发送指令顺序输入，每条只打印 [LAUNCH] 业务
# 发送启动日志即返回提示符，三路业务在后台同时发送（明细写各自日志）。
# 视频流=有线，传感器=Wi-Fi，环境监测=三链路逐条轮换（与现网一致）。
# 本轮报文在接入门打开前到达：边缘只计接收统计（gate_drop），不转发。
launch_sender pre-video.log --device-id "$DEVICE_VIDEO" --biz-type video --link wired \
    --duration "$SOURCE_DURATION" --interval 1
launch_sender pre-sensor.log --device-id "$DEVICE_SENSOR" --biz-type sensor --link wifi \
    --duration "$SOURCE_DURATION" --interval 1
launch_sender pre-env.log --device-id "$DEVICE_ENV" --biz-type env --link rotate \
    --duration "$SOURCE_DURATION" --interval 1
wait_round "$SOURCE_DURATION"

section '2.2.4  多源接入门打开（multi_source_access，在边缘网关执行）'
on_edge "./multi_source_access.sh"

section '2.2.4  接入门打开后再发 + 视频流终端火情上报（受理并转发上云）'
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

section '2.2.4  边缘网关服务日志查询（query_service_log，在边缘网关执行）'
on_edge "tail -n 40 '$EDGE_LOG' 2>/dev/null || echo '  (gateway.log 尚未生成：run_gateway.sh 现已自动落盘，重启网关即可)'"

section '2.2.4  端到端链路数据查询（多源上云核对，生产只读）'
RELAY_HOST="$RELAY_HOST" bash "$SCRIPT_DIR/../cloud/query_relay_state.sh"
echo
echo '  端侧发送审计（对账用，最近5条）：'
tail -n 5 "$SCRIPT_DIR/.state/sent.jsonl" 2>/dev/null || echo '  （无发送记录）'

section '2.2.4  可信接入·拉取服务器白名单并生效（在边缘网关执行）'
# 白名单只读拉取（中转 HTTP 11502）；三台借用设备逐个在册校验，
# ILLEGAL-SENSOR 不在册将出现 WARN 提示。
on_edge "RELAY_HOST='$RELAY_HOST' ./trust_access_add_whitelist.sh '$DEVICE_VIDEO' '$DEVICE_SENSOR' '$DEVICE_ENV' '$ILLEGAL_DEVICE_ID'"

section '2.2.4  名单外设备发送（非法终端被边缘网关拒收）'
python3 send_business.py --host "$EDGE_HOST" --port "$EDGE_JSON_PORT" \
    --device-id "$ILLEGAL_DEVICE_ID" --count "$ILLEGAL_COUNT" --fg
python3 send_business.py --host "$EDGE_HOST" --port "$EDGE_JSON_PORT" \
    --device-id "$UNKNOWN_DEVICE_ID" --count "$ILLEGAL_COUNT" --fg
echo '  （拒收明细见边缘网关日志 [WHITELIST][BLOCK] 行，下一步统计汇总）'

section '2.2.4  可信接入统计（trust_access_calculate，在边缘网关执行）'
on_edge "EDGE_LOG='$EDGE_LOG' ./trust_access_calculate.sh"
