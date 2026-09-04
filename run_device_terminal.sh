#!/usr/bin/env bash
set -Eeuo pipefail

# 端侧设备终端启动器（单终端合并形态，无需参数）。
#
# 三台端侧设备合并为一个终端：视频流（182D48D7/有线）、传感器（3C15DB07/
# Wi-Fi）、环境监测（990E261B/轮换）三路业务在同一终端依次输入发送命令即可
# （可同时运行：各 start 命令是独立进程、独立 TCP 长连接到边缘网关，状态
# 写入与 msg_id 分配均有跨进程锁）。默认业务身份为环境监测（承载大纲 2.2.3
# 端侧流程）；视频流附带真实媒体口 7777 与每 20s 火情上报。

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $# -gt 0 ]]; then
  echo "用法: $0   （无需参数：视频流/传感器/环境监测三路业务已在单终端内合并）" >&2
  exit 1
fi

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

# 视频流业务：视频帧同时走真实媒体接入口 7777（VID0 帧，与现网摄像头协议一致）。
export PROTOCOL_TEST_LIVE_MEDIA=1
export PROTOCOL_TEST_MEDIA_HOST="${DEVICE_MEDIA_HOST:-127.0.0.1}"
export PROTOCOL_TEST_MEDIA_PORT="${DEVICE_MEDIA_PORT:-7777}"
# 默认业务身份为环境监测（2.2.3 端侧流程：start_test/keep_transfer 等默认 env）。
export PROTOCOL_TEST_DEFAULT_BIZ=env

# 本终端的发送会话 ID（每次开终端唯一）：后台发送器 fork 后把 pid 登记到
# $STATE_DIR/sender-sessions/<会话>/pids，退出本终端时据此结束它们。
export PROTOCOL_TEST_SENDER_SESSION="device-$$-$(date +%Y%m%d%H%M%S)"

BAR='════════════════════════════════════════════════════════════════════════════════'
printf '\n\033[36m  %s\033[0m\n' "$BAR"
printf '\033[1;36m  %s\033[0m\n' '端侧设备终端 · END DEVICE TERMINAL（单终端合并形态）'
printf '\033[36m  %s\033[0m\n' "$BAR"
printf '  视频流    : %s  有线（含真实媒体口 %s；每20s附带火情上报）\n' \
  "${PROTOCOL_TEST_DEVICE_VIDEO:-182D48D7}" "$PROTOCOL_TEST_MEDIA_PORT"
printf '  传感器    : %s  Wi-Fi（名单过滤生效后自动换无关 ID）\n' \
  "${PROTOCOL_TEST_DEVICE_SENSOR:-3C15DB07}"
printf '  环境监测  : %s  Wi-Fi/蓝牙/有线（轮换；默认业务身份）\n' \
  "${PROTOCOL_TEST_DEVICE_ENV:-990E261B}"
printf '  gateway   : %s:%s（JSON）/ %s:%s（媒体）\n' \
  "$PROTOCOL_TEST_GATEWAY_HOST" "$PROTOCOL_TEST_GATEWAY_PORT" \
  "$PROTOCOL_TEST_MEDIA_HOST" "$PROTOCOL_TEST_MEDIA_PORT"
printf '\n'
printf '  业务发送（三路依次输入即可，可同时运行；默认后台持续发送——一行 [LAUNCH]\n'
printf '  （pid/日志路径）即回提示符，明细写 .protocol-test/sender-*.log，与生产包一致）:\n'
printf '    ./start_video_stream.sh                  # 后台持续发送\n'
printf '    ./start_video_stream.sh --duration 30 --interval 1\n'
printf '    ./start_sensor_data.sh --count 5   # 一并拉起光照设备 DEV-001\n'
printf '    ./start_env_data.sh --duration 60\n'
printf '    ./start_xxx.sh --fg ...    # 前台直跑（输出进度到终端，同旧版）\n'
printf '  停止后台发送：kill [LAUNCH] 行里打印的 pid（--count/--duration 到限自行结束；\n'
printf '  退出本终端时自动结束本终端启动的后台发送）\n'
printf '\n'
printf '  火情（随视频流上报，每20s一条，默认无火情 false）:\n'
printf '    ./fire_alarm.sh --on        # 触发火情：后续火情上报载荷变为 true\n'
printf '    ./fire_alarm.sh --off       # 解除火情：恢复 false\n'
printf '\n'
printf '  光照强度模拟（独立光照设备 DEV-001，仅有线接入、回传 5G；随传感器命令一并拉起）:\n'
printf '    ./start_light_data.sh\n'
printf '\n'
printf '  2.2.3 commands（大纲 2.2.3 端侧流程，默认即环境监测身份）:\n'
printf '    ./check_link_connect.sh\n'
printf '    ./ping_link_test.sh\n'
printf '    ./start_test.sh\n'
printf '    ./keep_transfer.sh --duration 600 --interval 1\n'
printf '    ./multi_link_bandwidth.sh --duration 5\n'
printf '\n'
printf '  未限条数/时长的后台发送一直跑到退出本终端；边缘网关受理前\n'
printf '  需先执行 ./multi_source_access.sh\n'
printf '\n'
printf '  exit the prompt to close the end device terminal\n\n'
PS1='device> ' bash --noprofile --norc -i || true

# 退出终端：自动结束本终端启动的后台业务发送（只 kill 会话目录里登记的
# pid，绝不 pkill -f；--count/--duration 到限已退出的发送器 kill 落空属正常）。
session_dir="${PROTOCOL_TEST_STATE_DIR}/sender-sessions/${PROTOCOL_TEST_SENDER_SESSION}"
if [[ -s "$session_dir/pids" ]]; then
  session_pids="$(tr '\n' ' ' < "$session_dir/pids")"
  for sender_pid in $session_pids; do
    kill "$sender_pid" 2>/dev/null || true
  done
  rm -rf "$session_dir"
  printf '\n  后台业务发送已随终端退出结束（pid:%s）\n\n' "$session_pids"
fi
