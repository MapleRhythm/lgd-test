#!/usr/bin/env bash
# 短波/卫星专用转发链路启动脚本（部署在与 server_v8 同机的中转服务器）
#
# 对远端只做"加法"：不动 server_v8，只以新端口（默认 19400）跑本转发器。
# 边缘网关注入侧开关：EDGE_RADIO_RELAY_URL=http://<本机IP>:19400
# （gateway_merged.py 联调模式读取；不设则维持统一上行/11503 既有路径）。
#
# 日志追加落盘 relay.log；历史记录在 radio-relay-state/radio-relay.jsonl，
# 重启不丢、seq 连续。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RELAY_ARGS=(
  --host "${RADIO_RELAY_HOST:-0.0.0.0}"
  --port "${RADIO_RELAY_PORT:-19400}"
)
# 台架/演示可覆盖状态目录；默认 <脚本目录>/radio-relay-state。
[[ -n "${RADIO_RELAY_STATE_DIR:-}" ]] && RELAY_ARGS+=(--state-dir "$RADIO_RELAY_STATE_DIR")

exec > >(tee -a relay.log) 2>&1

exec python3 -u radio_link_relay.py "${RELAY_ARGS[@]}"
