#!/usr/bin/env bash
# 大纲 2.2.4（生产环境）：可信接入统计——从边缘网关日志统计受理与拒收。
#
#   ./trust_access_calculate.sh          统计默认日志（$EDGE_STATE_DIR/gateway.log）
#   EDGE_LOG=/path/to/log ./trust_access_calculate.sh
#
# 统计口径与网关日志一致：[TRUST-ACCESS] 公告行、[WHITELIST][BLOCK] 逐条
# 拒收明细、周期统计行里的 whitelist_drop/gate_drop 计数（只看计数与设备
# ID，不看业务内容）。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${EDGE_STATE_DIR:-$SCRIPT_DIR/.state}"
EDGE_LOG="${EDGE_LOG:-$STATE_DIR/gateway.log}"

if [[ ! -r "$EDGE_LOG" ]]; then
  echo "[TRUST-CALC] 找不到网关日志：$EDGE_LOG（用 EDGE_LOG=<路径> 指定）" >&2
  exit 1
fi

echo "=== 可信接入统计（$EDGE_LOG） ==="
echo
echo "-- 接入/过滤门状态公告（最近4条） --"
grep -E '\[MULTI-SOURCE\]|\[TRUST-ACCESS\]' "$EDGE_LOG" | tail -n 4 || true
echo
echo "-- 名单外设备拒收明细（最近5条） --"
grep -F '[WHITELIST][BLOCK]' "$EDGE_LOG" | tail -n 5 || echo "  （无拒收记录）"
echo
BLOCK_TOTAL="$(grep -cF '[WHITELIST][BLOCK]' "$EDGE_LOG" || true)"
STATS="$(grep -E 'whitelist_drop=[0-9]+' "$EDGE_LOG" | tail -n 1 || true)"
echo "拒收累计（[WHITELIST][BLOCK] 行数）：${BLOCK_TOTAL:-0}"
echo "网关周期统计行（whitelist_drop/gate_drop 计数）："
echo "  ${STATS:-（统计行尚未输出，稍等网关周期打印后再查）}"
