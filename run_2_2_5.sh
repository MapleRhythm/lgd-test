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
# 重新开接入门（2.2.5 上行业务需要边缘受理）。
./init_link_connect.sh --reset
./init_link_connect.sh
./edge_forward.sh --start
./policy-route.sh --start
./msg-encap.sh --start
./cloud-mgr.sh --start

section '2.2.5 TEST STEPS'
./start_uplink_transfer.sh
./query_link_data.sh
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
