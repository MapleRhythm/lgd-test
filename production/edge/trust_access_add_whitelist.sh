#!/usr/bin/env bash
# 大纲 2.2.4（生产环境）：可信接入——从服务器拉取白名单并打印，随后名单
# 过滤生效。在边缘网关真机执行；网关通过标记文件感知，无需重启。
#
#   ./trust_access_add_whitelist.sh [设备ID...]   拉取+打印服务器白名单，
#                                                  指定的借用设备 ID 逐个在册校验
#
# 白名单只读拉取自中转 HTTP 11502（与网关自身拉取的同一来源），本命令
# 不向服务器写入任何内容。落下 whitelist_filter.enabled 后，不在名单内的
# 设备被边缘网关拒收并计入 whitelist_drop；此前到达的全部放行。
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${EDGE_STATE_DIR:-$SCRIPT_DIR/.state}"
RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
WHITELIST_URL="${WHITELIST_URL:-http://$RELAY_HOST:11502/whitelist}"

echo "[TRUST-ACCESS] 拉取服务器白名单：$WHITELIST_URL"
python3 - "$WHITELIST_URL" "$@" <<'PY'
import json, sys, urllib.request

url, check_ids = sys.argv[1], sys.argv[2:]
try:
    with urllib.request.urlopen(url, timeout=5.0) as resp:
        status = resp.status
        devices = json.loads(resp.read().decode("utf-8", "replace")).get("devices", [])
    print("[TRUST-ACCESS] HTTP %d，白名单共 %d 台设备：" % (status, len(devices)))
    for dev in devices:
        print("    %s" % dev)
    if check_ids:
        missing = [d for d in check_ids if d not in devices]
        if missing:
            print("[TRUST-ACCESS] WARN 指定设备不在白名单: %s（将同样被拒收）" % ", ".join(missing))
        else:
            print("[TRUST-ACCESS] OK 指定设备全部在册")
except Exception as exc:
    print("[TRUST-ACCESS] 拉取失败: %s（网关回退本地缓存白名单）" % exc)
PY

mkdir -p "$STATE_DIR"
touch "$STATE_DIR/whitelist_filter.enabled"
echo "[TRUST-ACCESS] 名单过滤已生效：名单外设备将被边缘网关拒收（marker=$STATE_DIR/whitelist_filter.enabled）"
