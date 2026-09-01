#!/usr/bin/env bash
# 大纲 2.2.3 步骤 1（生产环境）：初始化接入链路。
# 在边缘网关真机执行；网关通过标记文件感知，无需重启。
#
#   ./init_link_connect.sh           打开多源接入门：边缘开始受理端侧数据
#   ./init_link_connect.sh --reset   会话复位：关三扇门（接入/转发/过滤），
#                                     清掉异常退出残留的标记后重新走大纲流程
#
# 与开发侧（final/ 根目录 protocol_test_runtime）语义一致：网关启动后
# 接入门默认关闭，端侧报文只计接收统计（gate_drop）不受理；本命令执行后
# 开始受理。名单过滤门同样默认关闭（全部放行），2.2.4 的
# trust_access_add_whitelist 才会落下 whitelist_filter.enabled。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${EDGE_STATE_DIR:-$SCRIPT_DIR/.state}"
GATE_MARKER="$STATE_DIR/multi_source_access.enabled"
FORWARD_MARKER="$STATE_DIR/edge_forward.enabled"
FILTER_MARKER="$STATE_DIR/whitelist_filter.enabled"

case "${1:-}" in
  "")
    mkdir -p "$STATE_DIR"
    touch "$GATE_MARKER"
    echo "[INIT-LINK] 多源接入门已打开：边缘网关开始受理端侧数据（marker=$GATE_MARKER）"
    ;;
  --reset)
    rm -f "$GATE_MARKER" "$FORWARD_MARKER" "$FILTER_MARKER"
    echo "[INIT-LINK] 会话复位：接入/转发/过滤三扇门已关闭（marker 已移除）"
    ;;
  --status)
    if [[ -e "$GATE_MARKER" ]]; then
      echo "[INIT-LINK] 多源接入门：已打开（受理端侧数据）"
    else
      echo "[INIT-LINK] 多源接入门：未打开（端侧报文只计接收统计，不受理）"
    fi
    ;;
  *)
    echo "用法: $0 [--reset|--status]   （不带参数 = 打开接入门，大纲步骤1）" >&2
    exit 1
    ;;
esac
