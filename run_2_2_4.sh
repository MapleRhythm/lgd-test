#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SOURCE_DURATION="${SOURCE_DURATION:-3}"
# 大纲 2.2.4：传感器终端扮演非法设备（不在服务器白名单内）。
export PROTOCOL_TEST_DEVICE_SENSOR="${PROTOCOL_TEST_DEVICE_SENSOR:-ILLEGAL-SENSOR}"

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
./trust_access_add_whitelist.sh "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}" "${PROTOCOL_TEST_DEVICE_SENSOR:-ILLEGAL-SENSOR}"
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
