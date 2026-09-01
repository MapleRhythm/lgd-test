#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SOURCE_DURATION="${SOURCE_DURATION:-3}"

section() {
  printf '\n%s\n' '=============================================================================='
  printf '  %s\n' "$1"
  printf '%s\n' '=============================================================================='
}

section '2.2.4 PRECONDITIONS'
./init_link_connect.sh --reset
./edge_forward.sh --start
./policy-route.sh --start
./msg-encap.sh --start

section '2.2.4 TEST STEPS'
./start_video_stream.sh --duration "$SOURCE_DURATION"
./start_sensor_data.sh --duration "$SOURCE_DURATION"
./start_env_data.sh --duration "$SOURCE_DURATION"
./multi_source_access.sh
./query_service_log.sh
./query_cloud_log.sh --device-type video/sensor/env
# 起步无名单过滤（全部放行）；本指令拉取并打印服务器白名单后过滤生效，
# 传感器终端随发送自动换无关 ID，成为名单外非法设备被拒收。
./trust_access_add_whitelist.sh "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}" "${PROTOCOL_TEST_DEVICE_SENSOR:-3C15DB07}" ILLEGAL-SENSOR
./start_sensor_data.sh --duration "$SOURCE_DURATION"
./start_test.sh --device-id UNKNOWN-001
./trust_access_calculate.sh
./policy-route.sh --start
./edge-query.sh --route-log --biz-type video/sensor/critical-sensor/fire
./edge-query.sh --route-switch
./cloud-query.sh --biz-type video/sensor/control-alarm
./cloud-query.sh --msg-id-check
./cloud-query.sh --link-id-check
./query_cloud_log.sh --device-type video/sensor/env
./edge-query.sh --device-id "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}"
./cloud-query.sh --route-decision
./cloud-query.sh --link-switch
