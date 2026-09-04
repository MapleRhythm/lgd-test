#!/usr/bin/env bash
# 短波/卫星专用转发链路接收记录查询（大纲 2.2.4/2.2.5 云端查询·只读）。
#
# 数据源是 radio_link_relay（与 server_v8 同机的加法部署，默认端口 19400）：
# 短连接 GET /records，带 seq 游标——没有可僵尸化的长连接（坑17），且历史
# 可回放（调整前/调整后各查一次，用 --after 游标增量取新记录）。
# 链路时延/节奏在边缘网关实现（短波 20±3s 占道、卫星立即落地约 2 分钟
# 一条），本表 received_at-sent_at 为专用链路网络时延（含两端时钟偏差，
# 仅供参考，不含短波信道时延）。
#
# 用法:
#   ./query_radio_records.sh                       # 最近 20 条（短波+卫星各一张表）
#   ./query_radio_records.sh --limit 50            # 每链路最近 50 条
#   ./query_radio_records.sh --after 12            # 只取 seq>12 的增量（调整后新记录）
#   ./query_radio_records.sh --link satellite      # 只看卫星
#   RELAY_HOST=1.2.3.4 ./query_radio_records.sh    # 自建/测试中转
set -Eeuo pipefail

RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
RELAY_PORT="${RADIO_RELAY_PORT:-19400}"

python3 - "$RELAY_HOST" "$RELAY_PORT" "$@" <<'PY'
import datetime
import json
import sys
import urllib.request

host, port = sys.argv[1], sys.argv[2]
args = sys.argv[3:]

link = "all"
after = 0
limit = 20
i = 0
while i < len(args):
    if args[i] == "--link" and i + 1 < len(args):
        link = args[i + 1]; i += 2
    elif args[i] == "--after" and i + 1 < len(args):
        after = int(args[i + 1]); i += 2
    elif args[i] == "--limit" and i + 1 < len(args):
        limit = int(args[i + 1]); i += 2
    else:
        print("用法: %s [--link shortwave|satellite|all] [--after N] [--limit N]" % sys.argv[0])
        sys.exit(1)
if link not in ("shortwave", "satellite", "all"):
    print("--link 仅支持 shortwave/satellite/all"); sys.exit(1)

def fetch(link_name):
    url = "http://%s:%s/records?link=%s&after=%d&limit=%d" % (
        host, port, link_name, after, limit)
    with urllib.request.urlopen(url, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))

def parse_time(text):
    text = str(text or "").strip()
    if not text:
        return None
    # py3.6 的 strptime %z 不认 "+08:00"（带冒号）偏移，归一成 "+0800"。
    if len(text) >= 6 and text[-6] in "+-" and text[-3] == ":":
        text = text[:-3] + text[-2:]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None

def link_delay(record):
    sent = parse_time(record.get("sent_at"))
    received = parse_time(record.get("received_at"))
    if sent is None or received is None:
        return "-"
    delta = (received - sent).total_seconds()
    if delta < 0 or delta > 3600:
        return "-"
    return "%.1f" % delta

def summarize(payload):
    for key in ("type", "business", "msg_id"):
        if key in payload:
            return "%s=%s" % (key, payload[key])
    for key in ("fire", "windspeed", "device_id", "gateway"):
        if key in payload:
            return "%s=%s" % (key, payload[key])
    text = json.dumps(payload, ensure_ascii=False)
    return text[:36] + ("…" if len(text) > 36 else "")

TABLES = (
    ("shortwave", "短波工控设备接收记录"),
    ("satellite", "卫星接入模块接收记录"),
)
links = TABLES if link == "all" else \
    tuple(row for row in TABLES if row[0] == link)

print("=== 短波/卫星专用转发链路接收记录  %s:%s  (after=%d limit=%d) ===" % (
    host, port, after, limit))
max_seq = after
try:
    for link_name, title in links:
        result = fetch(link_name)
        records = result.get("records", [])
        print()
        print("%s（%d 条）" % (title, len(records)))
        if records:
            print("%-6s %-20s %-24s %8s  %s" % (
                "Seq", "Sent at", "Received at", "时延(s)", "业务"))
            for record in records:
                print("%-6d %-20s %-24s %8s  %s" % (
                    record["seq"],
                    str(record.get("sent_at", ""))[:19],
                    str(record.get("received_at", ""))[:23],
                    link_delay(record),
                    summarize(record.get("payload", {})),
                ))
                max_seq = max(max_seq, record["seq"])
        else:
            print("  （无记录）")
    print()
    print("增量游标: --after %d （下次查询只取这之后的新记录）" % max_seq)
except Exception as exc:
    print("[RADIO-RELAY] 拉取失败: %s" % exc)
    print("（确认 radio_link_relay 已在中转机上运行: ssh %s 'curl -s http://127.0.0.1:%s/health'）" % (
        host, port))
    sys.exit(1)
PY
