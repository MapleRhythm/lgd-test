#!/usr/bin/env bash
# 生产环境边缘网关启动脚本（真机部署）
#
# 除监听地址/端口外不覆盖任何参数：中转 47.99.47.169:11500、白名单
# http://<cloud-host>:11502、宝通 192.168.2.1:9100、卫星 /dev/ttyUSB0@115200
# 均直接使用 original/edge_config.py 的生产默认值。
#
# 转发门（大纲 2.2.3 步骤 6）：网关启动后默认不向云端转发，需在本机执行
#   ./edge_forward.sh --start
# 激活；标记文件位于 $EDGE_STATE_DIR/edge_forward.enabled。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export EDGE_STATE_DIR="${EDGE_STATE_DIR:-$SCRIPT_DIR/.state}"
mkdir -p "$EDGE_STATE_DIR"

# 转发门标记目录（gateway_merged.py 通过 PROTOCOL_TEST_STATE_DIR 定位）
export PROTOCOL_TEST_STATE_DIR="$EDGE_STATE_DIR"

GATEWAY_ARGS=(
  --json-listen-host "${EDGE_JSON_HOST:-0.0.0.0}"
  --json-listen-port "${EDGE_JSON_PORT:-8888}"
  --media-listen-host "${EDGE_MEDIA_HOST:-0.0.0.0}"
  --media-listen-port "${EDGE_MEDIA_PORT:-7777}"
  --whitelist-interval "${EDGE_WHITELIST_INTERVAL:-30}"
  --whitelist-filter
  --link-monitor-interval "${EDGE_LINK_MONITOR_INTERVAL:-60}"
  --compact-log
)

# 台架/联调无 400-GM12 串口时设 EDGE_DISABLE_SATELLITE=1；真机保持默认启用。
if [[ "${EDGE_DISABLE_SATELLITE:-0}" == "1" ]]; then
  GATEWAY_ARGS+=(--disable-satellite)
fi

# 台架联调可覆盖中转/宝通目标；真机留空即用生产默认值。
[[ -n "${EDGE_CLOUD_HOST:-}" ]] && GATEWAY_ARGS+=(--cloud-host "$EDGE_CLOUD_HOST")
[[ -n "${EDGE_CLOUD_PORT:-}" ]] && GATEWAY_ARGS+=(--cloud-port "$EDGE_CLOUD_PORT")
[[ -n "${EDGE_BAOTONG_HOST:-}" ]] && GATEWAY_ARGS+=(--baotong-host "$EDGE_BAOTONG_HOST")
[[ -n "${EDGE_BAOTONG_PORT:-}" ]] && GATEWAY_ARGS+=(--baotong-port "$EDGE_BAOTONG_PORT")

if [[ -n "${EDGE_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  GATEWAY_ARGS+=($EDGE_EXTRA_ARGS)
fi

exec python3 -u gateway_merged.py "${GATEWAY_ARGS[@]}"
