#!/usr/bin/env bash
# 业务发送器公共入口（单终端合并形态，与生产包 send_business.py 同步）。
#
# 默认后台持续发送：runtime 打印一行 [LAUNCH]（biz/device/link/持续或条数
# 或时长/间隔/pid/日志路径）即返回提示符，SOURCE 横幅与明细写
# STATE_DIR/sender-<biz>-<时间>.log，直到 kill [LAUNCH] 行的 pid；
# --count/--duration 到限自行结束。
#   --fg / --foreground   前台直跑（一键回归脚本与调试用，输出进度到终端）

sender_run() {  # sender_run <runtime子命令> [透传参数...]（默认后台，--fg 前台）
  local command="$1"
  shift
  exec python3 "${SCRIPT_DIR:?SCRIPT_DIR not set}/protocol_test_runtime.py" "$command" "$@"
}
