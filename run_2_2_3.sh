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
# 只做会话复位。转发门在步骤6 edge_forward 打开后业务即持续上云（默认
# 5G 上行），不依赖策略路由——策略路由/报文封装按大纲属 2.2.5 条目3/4。
./init_link_connect.sh --reset

section '2.2.3 TEST STEPS'
./init_link_connect.sh
./check_link_connect.sh
./ping_link_test.sh || true    # 测量步骤：回传链路此时尚未建立（步骤6才开转发），异常链路属预期
./start_test.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env
./query_service_log.sh
./keep_transfer.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env --duration "$KEEP_DURATION"
./multi_link_bandwidth.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env --duration "$BANDWIDTH_DURATION"
./edge_forward.sh --start
./start_test.sh --device-id "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}" --biz-type env
./query_link_data.sh
