#!/usr/bin/env bash
# 大纲 2.2.4（生产环境）：多源业务接入。
# 在边缘网关真机执行；网关通过标记文件感知，无需重启。
#
#   ./multi_source_access.sh            打开多源接入门：边缘开始受理端侧数据
#   ./multi_source_access.sh --status   查看门状态
#
# 与开发侧（final/ 根目录 protocol_test_runtime）语义一致：接入门打开前，
# 端侧报文只计接收统计（gate_drop），不校验白名单、不分类、不入转发队列；
# 本命令执行后开始受理。2.2.3 的 init_link_connect.sh 打开的是同一扇门，
# 单独跑 2.2.4 时用本命令对齐大纲步骤名。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${EDGE_STATE_DIR:-$SCRIPT_DIR/.state}"
GATE_MARKER="$STATE_DIR/multi_source_access.enabled"

case "${1:-}" in
  "")
    mkdir -p "$STATE_DIR"
    touch "$GATE_MARKER"
    echo "[MULTI-SOURCE] 多源接入门已打开：边缘网关开始受理端侧设备数据（marker=$GATE_MARKER）"
    ;;
  --status)
    if [[ -e "$GATE_MARKER" ]]; then
      echo "[MULTI-SOURCE] 多源接入门：已打开（受理端侧数据）"
    else
      echo "[MULTI-SOURCE] 多源接入门：未打开（端侧报文只计接收统计 gate_drop，不受理）"
    fi
    ;;
  *)
    echo "用法: $0 [--status]   （不带参数 = 打开接入门，大纲 2.2.4 步骤）" >&2
    exit 1
    ;;
esac
