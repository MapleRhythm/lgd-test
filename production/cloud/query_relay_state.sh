#!/usr/bin/env bash
# 大纲 2.2.3 步骤 9（生产环境）：端到端链路数据查询——只读方式。
#
# 远端 47.99.47.169 仅有两个只读 HTTP 面对外开放：
#   11502 /whitelist  中转设备白名单（实时）
#   11501             云端设备状态（报警灯/水阀等）
# 业务通道(11401/11406-11410/11416/11420/11421)与云节点 HTTP(10008+)不对外，
# 禁止本地云终端直连远端业务通道。最终接收核对在中转服务器上执行：
#   ssh <server> 后查看 server_v8 日志 STAT 计数（只看计数，不看 payload）。
set -Eeuo pipefail

RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
# 演示借用设备 ID（以白名单实时返回为准）
BORROWED_IDS="${BORROWED_IDS:-182D48D7 3C15DB07 990E261B EA1D2801}"

echo "=== 生产中转只读状态  $RELAY_HOST ==="

python3 - "$RELAY_HOST" $BORROWED_IDS <<'PY'
import json, sys, urllib.request

host = sys.argv[1]
borrowed = sys.argv[2:]

def get(url):
    with urllib.request.urlopen(url, timeout=5.0) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")

# 1) 白名单
try:
    status, body = get("http://%s:11502/whitelist" % host)
    devices = json.loads(body).get("devices", [])
    print("[WHITELIST] HTTP %d，共 %d 台设备：" % (status, len(devices)))
    for dev in devices:
        print("    %s" % dev)
    missing = [d for d in borrowed if d not in devices]
    if missing:
        print("[WHITELIST] WARN 借用设备不在白名单: %s（改用白名单内 ID）" % ", ".join(missing))
    else:
        print("[WHITELIST] OK 借用设备全部在册")
except Exception as exc:
    print("[WHITELIST] 拉取失败: %s" % exc)

# 2) 云端设备状态
try:
    status, body = get("http://%s:11501/" % host)
    print("[DEVSTATE]  HTTP %d %s" % (status, body.strip()))
except Exception as exc:
    print("[DEVSTATE]  拉取失败: %s" % exc)
PY

echo
echo "=== 端到端接收核对（在中转服务器上执行） ==="
echo "  ssh 登录 $RELAY_HOST 后查看 server_v8 日志："
echo "    grep STAT <server_v8日志路径> | tail    # 各通道收发计数"
echo "  只看 STAT 计数，与端侧 sent.jsonl 的 msg 计数对账。"
