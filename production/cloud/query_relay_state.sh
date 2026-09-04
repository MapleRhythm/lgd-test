#!/usr/bin/env bash
# 大纲 2.2.3 步骤 9（生产环境）：端到端链路数据查询——只读方式。
#
# 远端 47.99.47.169 仅有两个只读 HTTP 面对外开放：
#   11502 /whitelist  中转设备白名单（实时）
#   11501             云端设备状态（报警灯/水阀等）
# 业务通道(11401/11406-11410/11416/11420/11421)与云节点 HTTP(10008+)不对外，
# 禁止本地云终端直连远端业务通道。最终接收核对在中转服务器上执行：
#   ssh <server> 后查看 server_v8 日志 STAT 计数（只看计数，不看 payload）。
#
# 短波/卫星专用转发链路接收记录（radio relay，可选·加法部署）也在这里查：
# 云端短连接拉取出口 11550 GET /records（seq 游标、历史可回放），不需要
# 单开脚本——跟随大纲查询步骤一并打印；未部署/不可达时仅提示一行并跳过。
# 环境变量：RADIO_RELAY_PORT（默认 11550）、RADIO_AFTER（游标，默认 0 取
# 全部）、RADIO_LIMIT（每链路条数，默认 20）、RADIO_LINK（shortwave/
# satellite/all，默认 all）。
#
# 大纲 2.2.5 条目1（云端管理节点，生产只读版）同一入口：接收表之后打印
# 云端解析判类与转发路径——逐条按业务字段判定类别（视频类/传感类/
# 控制/告警类）并对应处理入口（显式 biz_type/type 字段优先，短波短信
# 载荷带 fire 即告警、windspeed 即关键传感器）；卫星身份帧非业务报文
# 只计数。5G 上行业务经中转进核心网关，只读通道不回放明细，核对走中转
# STAT 计数（见文末）。
set -Eeuo pipefail

RELAY_HOST="${RELAY_HOST:-47.99.47.169}"
# 演示借用设备 ID（以白名单实时返回为准）
BORROWED_IDS="${BORROWED_IDS:-182D48D7 3C15DB07 990E261B EA1D2801}"
# 短波/卫星专用转发链路（radio relay）查询参数
RADIO_RELAY_PORT="${RADIO_RELAY_PORT:-11550}"
RADIO_AFTER="${RADIO_AFTER:-0}"
RADIO_LIMIT="${RADIO_LIMIT:-20}"
RADIO_LINK="${RADIO_LINK:-all}"

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
echo "=== 短波/卫星专用转发链路接收记录（可选·加法部署） ==="
python3 - "$RELAY_HOST" "$RADIO_RELAY_PORT" "$RADIO_AFTER" "$RADIO_LIMIT" "$RADIO_LINK" <<'PY'
import datetime, json, sys, urllib.request

host, port, after, limit, link = (
    sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
if link not in ("shortwave", "satellite", "all"):
    print("[RADIO] RADIO_LINK 仅支持 shortwave/satellite/all")
    sys.exit(0)

def fetch(link_name):
    url = "http://%s:%s/records?link=%s&after=%d&limit=%d" % (
        host, port, link_name, after, limit)
    with urllib.request.urlopen(url, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))

LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo

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
            parsed = datetime.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    # sent_at 常为无时区格式（边缘本地时间），received_at 带时区——统一补上
    # 本地时区，否则 aware-naive 相减抛 TypeError。
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed

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

try:
    max_seq = after
    pulled = []
    for link_name, title in links:
        result = fetch(link_name)
        records = result.get("records", [])
        pulled.extend((link_name, record) for record in records)
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

    # 云端解析判类与转发路径（2.2.5 条目1 生产只读版）：与演示版
    # cloud-mgr --start 的两张表同口径——显式业务字段优先，短波短信
    # 载荷带 fire/windspeed 按字段判类；判不上的是非业务帧（卫星身份帧）。
    BIZ_CLASSES = (
        ("视频类", ("video", "image"), "视频处理入口"),
        ("传感类", ("sensor", "env", "critical-sensor", "windspeed"),
         "传感数据处理入口"),
        ("控制/告警类", ("fire", "control", "alarm", "control-alarm"),
         "控制/告警处理入口"),
    )
    LINK_NAMES = {"shortwave": "短波", "satellite": "卫星"}

    def classify(payload):
        biz = ""
        for key in ("biz_type", "type", "business"):
            value = str(payload.get(key) or "").strip().lower().replace("_", "-")
            if value:
                biz = value
                break
        if not biz and "fire" in payload:
            biz = "fire"        # 短波短信口径：载荷带 fire 即火情告警
        if not biz and "windspeed" in payload:
            biz = "windspeed"   # 风速=关键传感器读数（经短波/卫星承载）
        for label, members, entry in BIZ_CLASSES:
            if biz in members:
                return biz, label, entry
        return biz, None, None

    biz_rows = []
    counts = {}
    non_business = 0
    for link_name, record in pulled:
        biz, label, entry = classify(record.get("payload") or {})
        if label is None:
            non_business += 1
            continue
        counts[label] = counts.get(label, 0) + 1
        biz_rows.append((record["seq"], biz, label,
                         LINK_NAMES.get(link_name, link_name), entry))
    print("云端解析判类与转发路径（业务帧 %d 条）" % len(biz_rows))
    if biz_rows:
        # CJK 判类/入口占两格，按显示宽度补齐（仅显示层，不改数据）。
        import unicodedata

        def _w(text):
            return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1
                       for ch in str(text))

        def pad(text, width):
            text = str(text)
            return text + " " * max(0, width - _w(text))

        rows = [("Seq", "业务字段", "判定类别", "链路", "处理入口")]
        rows.extend(tuple(str(cell) for cell in row) for row in biz_rows)
        widths = [max(_w(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(pad(row[i], widths[i]) for i in range(len(widths))))
    else:
        print("  （无业务帧）")
    summary = "、".join("%s %d 条" % (label, counts.get(label, 0))
                        for label, _m, _e in BIZ_CLASSES)
    tail = ("；另有非业务帧 %d 条（卫星身份帧等，链路保持）" % non_business
            if non_business else "")
    print("业务类别接收统计：%s%s" % (summary or "无", tail))
    print()

    print("增量游标: RADIO_AFTER=%d （下次查询只取这之后的新记录）" % max_seq)
except Exception as exc:
    print("[RADIO] 专用转发链路未启用或不可达: %s" % exc)
    print("（可选加法部署：production/relay/ 的 radio_link_relay.py 跑在中转机，")
    print("  出口 %s 供本表拉取；未部署不影响其余核对项。）" % port)
PY

echo
echo "=== 端到端接收核对（在中转服务器上执行） ==="
echo "  ssh 登录 $RELAY_HOST 后查看 server_v8 日志："
echo "    grep STAT <server_v8日志路径> | tail    # 各通道收发计数"
echo "  只看 STAT 计数，与端侧 sent.jsonl 的 msg 计数对账。"
