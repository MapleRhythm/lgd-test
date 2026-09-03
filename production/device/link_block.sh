#!/usr/bin/env bash
# 大纲 2.2.5 生产环境流程:5G 链路屏蔽(真机部署,在端侧设备执行)。
#
# 直接调用中转 server_v8 的控制 HTTP(默认 11507):
#   --stop     POST /stop1   组1 group_enabled=False——服务器向核心网关下发
#             心跳 status=0/edge_online=False、向边缘网关下发链路状态
#             connected=false,组内 JSON/媒体报文全部丢弃(等价 5G 天线
#             加屏蔽罩,核心与边缘双双断开);
#   --recover  POST /recover1 恢复转发。
# 与仓库根目录的演示版 link_block.sh(走本地模型)不同:本脚本无本地
# 模型,服务器即真实中转,指令真实生效——注意 /stop1 作用于服务器上
# 组1 的全部边缘网关,下发前确认没有别的组1边缘在用。
#
# 环境变量:RELAY_HOST 中转地址(默认 47.99.47.169)、
#           CONTROL_PORT 控制口(默认 11507)、CONTROL_GROUP 组号(默认 1)。
set -Eeuo pipefail

RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
CONTROL_PORT="${CONTROL_PORT:-11507}"
CONTROL_GROUP="${CONTROL_GROUP:-1}"

usage() {
  echo "用法: $0 --stop | --recover    (当前目标 $RELAY_HOST:$CONTROL_PORT 组$CONTROL_GROUP)"
  exit 2
}

[[ $# -eq 1 ]] || usage
case "$1" in
  --stop)    ACTION="stop" ;;
  --recover) ACTION="recover" ;;
  *)         usage ;;
esac

python3 - "$RELAY_HOST" "$CONTROL_PORT" "$ACTION" "$CONTROL_GROUP" <<'PY'
import json, sys, urllib.request

host, port, action, group = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
url = "http://%s:%s/%s%s" % (host, port, action, group)
request = urllib.request.Request(url, data=b"", method="POST")
try:
    with urllib.request.urlopen(request, timeout=3.0) as response:
        body = json.loads(response.read().decode("utf-8"))
except (OSError, ValueError) as exc:
    print("[LINK-BLOCK] 指令未送达 %s: %s" % (url, exc))
    sys.exit(1)

print("[LINK-BLOCK] POST %s -> HTTP 200" % url)
print("[LINK-BLOCK] 服务器响应: %s" % json.dumps(body, ensure_ascii=False))
if action == "stop":
    print("[LINK-BLOCK] 服务器将向核心网关下发心跳 status=0/edge_online=False,")
    print("[LINK-BLOCK] 并向边缘网关下发链路状态 connected=false,组%s报文全部丢弃" % group)
    print("[LINK-BLOCK] 观察点: 边缘网关日志出现 [LINK-STATUS] connected=False,")
    print("[LINK-BLOCK]         核心板日志心跳中断")
else:
    print("[LINK-BLOCK] 组%s恢复转发:核心网关心跳与边缘链路状态恢复" % group)
    print("[LINK-BLOCK] 观察点: 边缘网关日志出现 [LINK-STATUS] connected=True")
PY
