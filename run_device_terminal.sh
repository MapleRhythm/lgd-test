#!/usr/bin/env bash
set -Eeuo pipefail

# 端侧设备终端启动器。
#
# 大纲 2.2.4 的多源接入需要三个端侧终端（同一目录、同一状态区，各自独立
# TCP 长连接到边缘网关，状态写入与 msg_id 分配均有跨进程锁）：
#   ./run_device_terminal.sh video    视频流终端        182D48D7  有线（每10s附带火情上报）
#   ./run_device_terminal.sh sensor   传感器终端        3C15DB07  Wi-Fi（名单过滤生效后自动换无关 ID）
#   ./run_device_terminal.sh env      环境监测模块终端  990E261B  Wi-Fi/蓝牙/有线（轮换）
# 不带参数 = 原有通用端侧终端。

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ROLE="${1:-}"
case "$ROLE" in
  video|sensor|env|"") ;;
  *)
    echo "用法: $0 [video|sensor|env]   （不带参数 = 通用端侧终端）" >&2
    exit 1
    ;;
esac

# Drop any desktop proxy: from WSL (NAT mode) it is unreachable and breaks
# the urllib-based whitelist mirror in this terminal.
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="127.0.0.1,localhost"
export NO_PROXY="$no_proxy"

export PROTOCOL_TEST_LIVE=1
export PROTOCOL_TEST_CLOUD_LIVE=1
export PROTOCOL_TEST_GATEWAY_HOST="${DEVICE_GATEWAY_HOST:-127.0.0.1}"
export PROTOCOL_TEST_GATEWAY_PORT="${DEVICE_GATEWAY_PORT:-8888}"
export PROTOCOL_TEST_STATE_DIR="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}"
# Read-only mirror of the whitelist the relay distributes to the edge gateway,
# so local ACCEPT/BLOCK verdicts match the real edge (127.0.0.1 or the remote).
export PROTOCOL_TEST_WHITELIST_URL="${PROTOCOL_TEST_WHITELIST_URL:-http://${PROTOCOL_TEST_RELAY_HOST:-127.0.0.1}:11502/whitelist}"

BAR='════════════════════════════════════════════════════════════════════════════════'
printf '\n\033[36m  %s\033[0m\n' "$BAR"

case "$ROLE" in
  video)
    # 视频帧同时走真实媒体接入口 7777（VID0 帧，与现网摄像头协议一致）。
    export PROTOCOL_TEST_LIVE_MEDIA=1
    export PROTOCOL_TEST_MEDIA_HOST="${DEVICE_MEDIA_HOST:-127.0.0.1}"
    export PROTOCOL_TEST_MEDIA_PORT="${DEVICE_MEDIA_PORT:-7777}"
    printf '\033[1;36m  %s\033[0m\n' '视频流终端 · VIDEO STREAM TERMINAL'
    printf '\033[36m  %s\033[0m\n' "$BAR"
    printf '  device id : %s（白名单借用）\n' "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}"
    printf '  gateway   : %s:%s（JSON）/ %s:%s（媒体）\n' \
      "$PROTOCOL_TEST_GATEWAY_HOST" "$PROTOCOL_TEST_GATEWAY_PORT" \
      "$PROTOCOL_TEST_MEDIA_HOST" "$PROTOCOL_TEST_MEDIA_PORT"
    printf '  link      : 有线\n'
    printf '  fire      : 每10s一条火情上报（随 start_video_stream 自动发送，无火情=false）\n'
    printf '\n'
    printf '  available commands:\n'
    printf '    ./start_video_stream.sh\n'
    printf '    ./start_video_stream.sh --duration 30 --interval 1\n'
    printf '    ./fire_alarm.sh --on        # 触发火情：后续火情上报载荷变为 true\n'
    printf '    ./fire_alarm.sh --off       # 解除火情：恢复 false\n'
    printf '\n'
    printf '  持续发送至 Ctrl-C 停止；边缘网关受理前需先执行 ./multi_source_access.sh\n'
    printf '\n'
    printf '  exit the prompt to close the video stream terminal\n\n'
    PS1='video> ' bash --noprofile --norc -i
    ;;
  sensor)
    # 大纲 2.2.4 可信接入演示：起步不过滤名单，本终端用正常白名单身份；
    # ./trust_access_add_whitelist.sh 执行（过滤生效）后，发送端逐报文自动
    # 切换为无关设备 ID（ILLEGAL-SENSOR），被边缘网关拒收并记阻断日志。
    printf '\033[1;36m  %s\033[0m\n' '传感器终端 · SENSOR DATA TERMINAL'
    printf '\033[36m  %s\033[0m\n' "$BAR"
    printf '  device id : %s（白名单借用；trust_access_add_whitelist 后自动换无关 ID）\n' "${PROTOCOL_TEST_DEVICE_SENSOR:-3C15DB07}"
    printf '  gateway   : %s:%s\n' "$PROTOCOL_TEST_GATEWAY_HOST" "$PROTOCOL_TEST_GATEWAY_PORT"
    printf '  link      : Wi-Fi\n'
    printf '\n'
    printf '  available commands:\n'
    printf '    ./start_sensor_data.sh\n'
    printf '    ./start_sensor_data.sh --duration 30 --interval 1\n'
    printf '\n'
    printf '  持续发送至 Ctrl-C 停止；边缘网关受理前需先执行 ./multi_source_access.sh\n'
    printf '\n'
    printf '  exit the prompt to close the sensor data terminal\n\n'
    PS1='sensor> ' bash --noprofile --norc -i
    ;;
  env)
    # 本终端同时承载大纲 2.2.3 的端侧流程：默认业务身份切换为环境监测。
    export PROTOCOL_TEST_DEFAULT_BIZ=env
    printf '\033[1;36m  %s\033[0m\n' '环境监测模块终端 · ENV MONITOR TERMINAL'
    printf '\033[36m  %s\033[0m\n' "$BAR"
    printf '  device id : %s（白名单借用）\n' "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}"
    printf '  gateway   : %s:%s\n' "$PROTOCOL_TEST_GATEWAY_HOST" "$PROTOCOL_TEST_GATEWAY_PORT"
    printf '  link      : Wi-Fi / 蓝牙 / 有线（轮换）\n'
    printf '\n'
    printf '  available commands:\n'
    printf '    ./start_env_data.sh\n'
    printf '    ./start_env_data.sh --duration 30 --interval 1\n'
    printf '\n'
    printf '  持续发送至 Ctrl-C 停止；边缘网关受理前需先执行 ./multi_source_access.sh\n'
    printf '\n'
    printf '  2.2.3 commands（大纲 2.2.3 端侧流程在本终端执行，默认即环境监测身份）:\n'
    printf '    ./check_link_connect.sh\n'
    printf '    ./ping_link_test.sh\n'
    printf '    ./start_test.sh\n'
    printf '    ./keep_transfer.sh --duration 600 --interval 1\n'
    printf '    ./multi_link_bandwidth.sh --duration 5\n'
    printf '\n'
    printf '  exit the prompt to close the env monitor terminal\n\n'
    PS1='env> ' bash --noprofile --norc -i
    ;;
  *)
    printf '\033[1;36m  %s\033[0m\n' 'END DEVICE TERMINAL'
    printf '\033[36m  %s\033[0m\n' "$BAR"
    printf '  device id : %s\n' "${DEVICE_ID:-182D48D7}"
    printf '  gateway   : %s:%s\n' "$PROTOCOL_TEST_GATEWAY_HOST" "$PROTOCOL_TEST_GATEWAY_PORT"
    printf '\n'
    printf '  available commands:\n'
    printf '    ./start_sensor_data.sh --device-id %s\n' "${DEVICE_ID:-182D48D7}"
    printf '    ./start_env_data.sh\n'
    printf '    ./start_test.sh --device-id %s --biz-type sensor\n' "${DEVICE_ID:-182D48D7}"
    printf '\n'
    printf '  exit the prompt to close the end device terminal\n\n'
    PS1='device> ' bash --noprofile --norc -i
    ;;
esac
