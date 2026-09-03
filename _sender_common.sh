#!/usr/bin/env bash
# 业务发送器公共入口（单终端合并形态，与生产包 send_business.py 同步）。
#
# 终端里默认后台启动：透传给 runtime 的 --background 由父进程打印一行
# [LAUNCH]（biz/device/link/时长或条数/pid/日志路径）即返回提示符，SOURCE
# 横幅与明细写 STATE_DIR/sender-<biz>-<时间>.log。
#   --fg / --foreground   前台直跑（一键回归脚本与调试用，输出进度到终端）
# 未给 --count/--duration 时 runtime 只发一条（count=1 兜底，同生产包）；
# 持续发送显式给 --duration；提前停止 kill [LAUNCH] 行打印的 pid 即可。

sender_run() {  # sender_run <业务中文名> <runtime子命令> [透传参数...]
  local label="$1" command="$2"
  shift 2
  case "${1:-}" in
    --fg|--foreground)
      shift
      exec python3 "${SCRIPT_DIR:?SCRIPT_DIR not set}/protocol_test_runtime.py" \
          "$command" "$@"
      ;;
  esac
  exec python3 "$SCRIPT_DIR/protocol_test_runtime.py" "$command" --background "$@"
}
