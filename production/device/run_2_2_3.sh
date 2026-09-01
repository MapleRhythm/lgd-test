#!/usr/bin/env bash
# 大纲 2.2.3 生产环境流程（在端侧设备真机执行）。
#
# 前提：
#   - 边缘网关真机已运行 ../edge/run_gateway.sh（默认不转发）
#   - EDGE_HOST 指向边缘网关地址（默认 127.0.0.1，设备与网关同机时）
#   - 可选 EDGE_SSH=<user@edge-host>：<edge-dir> —— 步骤 5/6 自动经 ssh 在
#     边缘网关上执行；未设置时打印人工执行指令
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export EDGE_HOST="${EDGE_HOST:-127.0.0.1}"
DEVICE_ID="${DEVICE_ID:-3C15DB07}"
KEEP_DURATION="${KEEP_DURATION:-600}"
BANDWIDTH_DURATION="${BANDWIDTH_DURATION:-5}"
START_COUNT="${START_COUNT:-5}"
EDGE_SSH="${EDGE_SSH:-}"
EDGE_REMOTE_DIR="${EDGE_REMOTE_DIR:-$SCRIPT_DIR/../edge}"
EDGE_LOG="${EDGE_LOG:-gateway.log}"

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

section '2.2.3  接入链路连通性检查（check_link_connect）'
./check_link.sh

section '2.2.3  接入链路时延实测（ping_link_test）'
./ping_link.sh

section '2.2.3  业务数据发送（start_test）'
python3 send_business.py --host "$EDGE_HOST" --device-id "$DEVICE_ID" --count "$START_COUNT"

section '2.2.3  边缘网关服务日志查询（query_service_log，在边缘网关执行）'
on_edge "tail -n 40 '$EDGE_LOG'"

section '2.2.3  持续传输（keep_transfer，仅有线）'
python3 send_business.py --host "$EDGE_HOST" --device-id "$DEVICE_ID" \
  --link wired --duration "$KEEP_DURATION" --interval 1

section '2.2.3  多模态并发传输（multi_link_bandwidth：Wi-Fi/蓝牙/有线）'
python3 send_business.py --host "$EDGE_HOST" --device-id "$DEVICE_ID" \
  --link all --duration "$BANDWIDTH_DURATION" --interval 1

section '2.2.3  建立边缘网关->云端转发通道（edge_forward --start，在边缘网关执行）'
on_edge "./edge_forward.sh --start"

section '2.2.3  转发通道建立后业务数据端到端发送（start_test）'
python3 send_business.py --host "$EDGE_HOST" --device-id "$DEVICE_ID" --count "$START_COUNT"

section '2.2.3  端到端链路数据查询（query_link_data，生产只读验证）'
bash "$SCRIPT_DIR/../cloud/query_relay_state.sh"
echo
echo '  端侧发送审计（对账用，最近3条）：'
tail -n 3 "$SCRIPT_DIR/.state/sent.jsonl"
