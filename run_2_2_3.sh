#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

KEEP_DURATION="${KEEP_DURATION:-600}"
BANDWIDTH_DURATION="${BANDWIDTH_DURATION:-5}"
# 2.2.3 端侧流程用环境监测终端身份（设备/业务均为 env）。
export PROTOCOL_TEST_DEFAULT_BIZ=env

section() {
  local bar='════════════════════════════════════════════════════════════════════════════════'
  printf '\n\033[36m  %s\033[0m\n' "$bar"
  printf '\033[1;36m  %s\033[0m\n' "$1"
  printf '\033[36m  %s\033[0m\n' "$bar"
}

section '2.2.3 PRECONDITIONS'
./init_link_connect.sh --reset
./policy-route.sh --start
./msg-encap.sh --start

section '2.2.3 TEST STEPS'
./init_link_connect.sh
./check_link_connect.sh
./ping_link_test.sh
./start_test.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env
./query_service_log.sh
./keep_transfer.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env --duration "$KEEP_DURATION"
./multi_link_bandwidth.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env --duration "$BANDWIDTH_DURATION"
./edge_forward.sh --start
./start_test.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env
./query_link_data.sh
