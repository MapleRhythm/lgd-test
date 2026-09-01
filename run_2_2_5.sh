#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

section() {
  printf '\n%s\n' '=============================================================================='
  printf '  %s\n' "$1"
  printf '%s\n' '=============================================================================='
}

section '2.2.5 PRECONDITIONS'
# 与 2.2.3/2.2.4 分节脚本一致：重跑自动清台账（--reset），随后普通 init
# 重新开接入门（2.2.5 上行业务需要边缘受理）、重开转发门（2.2.3 步骤6
# 语义延续）。策略路由/报文封装不前置——按大纲在条目3/4 进入流程。
./init_link_connect.sh --reset
./init_link_connect.sh
./edge_forward.sh --start
./cloud-mgr.sh --start

section '2.2.5 TEST STEPS'
./start_uplink_transfer.sh
./query_link_data.sh
./policy-route.sh --start   # 大纲 2.2.5 条目3：策略路由启动（5G 断开切换的分类依据）
./link-monitor.sh --low
./start_uplink_transfer.sh
./query_link_data.sh
./edge-query.sh --route-switch
./link-monitor.sh --normal
./start_uplink_transfer.sh
./query_link_data.sh
./msg-encap.sh --start
./set_channel.sh
./start_transfer.sh
./limit_rate.sh --rate 1
./cloud-mgr.sh --start
./cloud-query.sh --biz-type video/sensor/control-alarm
