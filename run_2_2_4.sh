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
# init --reset 会重新关上转发门，这里按 2.2.3 步骤6 的语义先打开（2.2.4
# 需要云端一致性核对）。策略路由/报文封装不前置：大纲里属 2.2.5 条目3/4，
# 未启动期间业务走默认 5G 上行，路由查询如实显示默认路由。
./init_link_connect.sh --reset
./edge_forward.sh --start

section '2.2.4 TEST STEPS'
./start_video_stream.sh --duration "$SOURCE_DURATION"
./start_sensor_data.sh --duration "$SOURCE_DURATION"
./start_env_data.sh --duration "$SOURCE_DURATION"
./multi_source_access.sh
# 交互流程里三个发送终端是持续发送、跨越开闸时点的；单终端回归的限时
# 发送在开闸前就结束了，这里按同样时长补一轮——多源接入门已开，本轮起
# 边缘受理、转发上云（开闸前到达的报文保持 gate_closed 不追溯改判）。
./start_video_stream.sh --duration "$SOURCE_DURATION"
./start_sensor_data.sh --duration "$SOURCE_DURATION"
./start_env_data.sh --duration "$SOURCE_DURATION"
./query_service_log.sh
./query_cloud_log.sh --device-type video/sensor/env
# 起步无名单过滤（全部放行）；本指令拉取并打印服务器白名单后过滤生效，
# 传感器终端随发送自动换无关 ID，成为名单外非法设备被拒收。
./trust_access_add_whitelist.sh "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}" "${PROTOCOL_TEST_DEVICE_SENSOR:-3C15DB07}" ILLEGAL-SENSOR
./start_sensor_data.sh --duration "$SOURCE_DURATION"
./start_test.sh --device-id UNKNOWN-001
./trust_access_calculate.sh
./edge-query.sh --route-log --biz-type video/sensor/critical-sensor/fire
./edge-query.sh --route-switch
./cloud-query.sh --biz-type video/sensor/control-alarm
./cloud-query.sh --msg-id-check
./cloud-query.sh --link-id-check
./query_cloud_log.sh --device-type video/sensor/env --verify
./edge-query.sh --device-id "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}"
./cloud-query.sh --route-decision
./cloud-query.sh --link-switch
