#!/usr/bin/env bash
# 大纲 2.2.3 步骤 6（生产环境）：建立/断开边缘网关至云端管理节点的转发通道。
# 在边缘网关真机执行；网关通过标记文件感知，无需重启。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${EDGE_STATE_DIR:-$SCRIPT_DIR/.state}"
MARKER="$STATE_DIR/edge_forward.enabled"

case "${1:-}" in
  --start)
    mkdir -p "$STATE_DIR"
    touch "$MARKER"
    echo "[EDGE-FORWARD] 转发通道已建立：5G/短波/卫星 -> 云端管理节点（marker=$MARKER）"
    ;;
  --stop)
    rm -f "$MARKER"
    echo "[EDGE-FORWARD] 转发通道已断开（marker 已移除）"
    ;;
  --status)
    if [[ -e "$MARKER" ]]; then
      echo "[EDGE-FORWARD] 转发通道：已建立"
    else
      echo "[EDGE-FORWARD] 转发通道：未建立"
    fi
    ;;
  *)
    echo "用法: $0 --start|--stop|--status" >&2
    exit 1
    ;;
esac
