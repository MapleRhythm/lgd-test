#!/usr/bin/env bash
# 业务发送器公共入口（单终端合并形态）。
#
# 交互终端里默认**后台启动**：只打印一行「xx已启动」（pid/时长/日志路径）
# 即返回提示符，SOURCE 横幅与 progress 明细写日志文件，不再刷终端。
#   --stop           停止本业务的后台发送（只 kill 记录的 PID，绝不 pkill -f）
#   --fg / --foreground   前台直跑（一键回归脚本与调试用，行为同旧版）
# 未显式给 --duration/--count 时，后台发送默认 SENDER_DURATION（600s）限时
# 自行结束，避免后台发送器跨演示会话残留；显式给出的参数原样透传。

sender_run() {  # sender_run <业务中文名> <runtime子命令> <日志短名> [透传参数...]
  local label="$1" command="$2" short="$3"
  shift 3
  local state="${PROTOCOL_TEST_STATE_DIR:-${SCRIPT_DIR:-$(pwd)}/.protocol-test}"
  local pidfile="$state/sender-$short.pid"
  local logfile="$state/sender-$short.log"

  case "${1:-}" in
    --stop)
      local pid
      if [[ -f "$pidfile" ]] && pid="$(cat "$pidfile" 2>/dev/null)" \
          && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        rm -f "$pidfile"
        echo "${label}后台发送已停止（pid=${pid}）"
      else
        rm -f "$pidfile"
        echo "${label}当前无后台发送在运行"
      fi
      return 0
      ;;
    --fg|--foreground)
      shift
      exec python3 "${SCRIPT_DIR:?SCRIPT_DIR not set}/protocol_test_runtime.py" \
          "$command" "$@"
      ;;
  esac

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    echo "${label}已在运行（pid=$(cat "$pidfile")）；如需重启先 ${0##*/} --stop" >&2
    return 1
  fi

  local args=()
  case " $* " in
    *" --duration "*|*" --count "*|*" --duration="*) ;;
    *) args=(--duration "${SENDER_DURATION:-600}") ;;
  esac

  mkdir -p "$state"
  : > "$logfile"
  # -u：后台日志行缓冲化，progress/SOURCE 明细实时落盘可 tail。
  nohup python3 -u "$SCRIPT_DIR/protocol_test_runtime.py" "$command" "${args[@]}" "$@" \
      >>"$logfile" 2>&1 &
  local pid=$!
  echo "$pid" > "$pidfile"
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "${label}启动失败，日志尾部（$logfile）：" >&2
    tail -n 5 "$logfile" >&2 || true
    rm -f "$pidfile"
    return 1
  fi
  local duration_note=""
  if ((${#args[@]})); then duration_note="时长=${args[1]}s "; fi
  echo "${label}已启动：pid=${pid} ${duration_note}日志=$logfile（--stop 停止 / --fg 前台运行）"
}
