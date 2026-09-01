#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

KEEP_DURATION="${KEEP_DURATION:-600}"
BANDWIDTH_DURATION="${BANDWIDTH_DURATION:-5}"

section() {
  printf '\n%s\n' '=============================================================================='
  printf '  %s\n' "$1"
  printf '%s\n' '=============================================================================='
}

section 'TEST OUTLINE 2.2.3 - 2.2.5'

section '2.2.3 PRECONDITIONS'
./init_link_connect.sh --reset
./policy-route.sh --start
./msg-encap.sh --start

section '2.2.3'
./init_link_connect.sh
./check_link_connect.sh
./ping_link_test.sh
./start_test.sh
./keep_transfer.sh --duration "$KEEP_DURATION"
./multi_link_bandwidth.sh --duration "$BANDWIDTH_DURATION"
./edge_forward.sh --start
./start_test.sh
./query_link_data.sh

section '2.2.4'
./start_video_stream.sh
./start_sensor_data.sh
./start_env_data.sh
./multi_source_access.sh
./query_service_log.sh
./query_cloud_log.sh --device-type video/sensor/env
./trust_access_add_whitelist.sh "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}" "${PROTOCOL_TEST_DEVICE_SENSOR:-3C15DB07}"
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

section '2.2.5'
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
