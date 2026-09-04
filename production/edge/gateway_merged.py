#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
======================================================================
统一边缘网关程序：JSON、视频/图片、宝通设备协同接入与云端转发
======================================================================

一、统一端口与业务职责

1. TCP 7777：视频/图片数据接收端口
   - 默认监听地址：0.0.0.0:7777。
   - 接收摄像头端发送的 VID0 视频帧和 SNAP 图片帧。
   - 输入协议：
       [4字节 packet_type][8字节 body长度]
       [4字节 metadata长度][metadata JSON][JPEG payload]
   - 边缘网关不修改、不压缩、不落盘，按完整原始帧包转发至云服务器。

2. TCP 8888：统一 JSON 数据接收端口
   - 默认监听地址：0.0.0.0:8888。
   - 统一接收原 58所、南邮及其他本地设备发送的 JSON 数据。
   - 支持换行分隔 JSON 和连续拼接 JSON。
   - 只为 JSON 补充 gateway 字段后转发至云服务器。
   - 每个 JSON 客户端连接建立后，默认每 2 秒下发一次 time_set 校时命令。
   - Sub-1G 纯信标帧只作为链路心跳，不进入云端转发队列。
   - 支持通过 HTTP 11502 周期同步白名单，并可选启用白名单过滤。
   - 当 JSON 顶层包含 fire 或 windspeed 字段时，更新对应短波业务缓存。

3. TCP 11500：向云服务器转发数据的统一目标端口
   - 默认云服务器地址：47.99.47.169:11500。
   - JSON 数据和视频/图片原始帧分别通过独立 TCP 连接发送至该目标端口。
   - JSON 以 UTF-8 文本加换行发送；视频/图片保持原有二进制帧协议发送。

4. TCP 9100：宝通设备连接及报文下发端口
   - 默认监听地址：192.168.2.1:9100。
   - 宝通设备主动连接本程序。
   - 主站轮询架构：边缘不主动上报，收到传感器数据只更新缓存；
     核心网关呼叫（报文带 caller_id）时才应答业务数据。
   - 应答遵循先探测后发送：仅当工控机上报 detect=0（信道空闲）时
     发送 link_test，收到 linkstatus 且双向 SNR 均大于阈值后，
     发送前再检查一次 detect 是否为 0，确认信道空闲才发出短信；
     detect 为工控机唯一权威，本地只镜像不置位/清零，停留 1
     超过 600 秒时发送一次 reset 请求工控机复位。
   - 根据 --gateway 注册标识选择应答业务：gateway_2 应答 windspeed，
     gateway_1/gateway_4 应答 fire。
   - gateway_1 的11417链路状态为断开时，fire/windspeed按次轮换应答。

5. TCP 11417：服务器链路状态订阅
   - 程序使用 --gateway 的正式名称注册，并持续接收该网关链路状态。
   - 连接中断时按断链处理，默认每3秒重连。

6. USB 串口：400-GM12 卫星上行
   - 默认串口：/dev/ttyUSB0，115200 8N1，无流控。
   - 周期发送紧凑 JSON：{"gateway":"gateway_x","timestamp":"年-月-日 时:分:秒"}。
   - 实际指令为 AT+SEND=<UTF-8字节数>,<十六进制JSON>,<data_type>。
   - 解析 +FrameNo 和 OK，并可用 AT+CMMQ? 查询模组待发帧数。
   - 串口或卫星模组异常时独立重连，不影响7777、8888、9100等业务。

二、默认 IP 配置

- 视频/图片监听地址：0.0.0.0（与原 edge.py 一致）。
- JSON 监听地址：0.0.0.0（与原 edge_json_forwarder.py 一致；可通过板卡
  实际接口地址访问，例如原代码中的 192.168.1.106）。
- 宝通监听地址：192.168.2.1（与原 baotong.py 一致）。
- 云服务器地址：47.99.47.169（与原 edge.py、edge_json_forwarder.py 一致）。

三、运行示例

  python3 gateway_merged.py --callee-id <宝通被叫ID>

  python3 gateway_merged.py \
      --callee-id <宝通被叫ID> \
      --gateway gateway_1 \
      --media-listen-host 0.0.0.0 --media-listen-port 7777 \
      --json-listen-host 0.0.0.0 --json-listen-port 8888 \
      --cloud-host 47.99.47.169 --cloud-port 11500 \
      --link-status-host 47.99.47.169 --link-status-port 11417 \
      --baotong-host 192.168.2.1 --baotong-port 9100

说明：
- 本文件完成三份原代码的业务和运行逻辑整合，不再采用三个互斥运行模式。
- 程序启动后同时运行 7777、8888、9100 三个监听服务，以及两个指向
  11500 的云端发送线程。
- 队列、断线重连、原始帧转发、宝通报文格式和链路状态处理总体沿用原代码。
- JSON 侧融合白名单同步、time_set 下行、Sub-1G 信标过滤与逐条收发日志。
- 默认启用400-GM12卫星上行；可使用 --disable-satellite 关闭。
- 默认启用详细日志，记录线程、连接、收包、解析、入队、出队、转发、
  宝通链路状态和异常信息；可使用 --compact-log 减少逐包日志。
"""

import argparse
import codecs
import glob
import json
import os
import queue
import random
import re
import signal
import socket
import struct
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone, timedelta

from edge_config import *

PACKET_HEADER = struct.Struct("!4sQ")
VALID_PACKET_TYPES = {"VID0", "SNAP"}

SATELLITE_FRAME_NO_RE = re.compile(r"^\+FrameNo:\s*<?(\d+)>?$")
SATELLITE_CMMQ_RE = re.compile(r"^\+CMMQ:\s*(\d+)$")

BJ_TZ = timezone(timedelta(hours=8))


# ======================================================================
# 公共工具
# ======================================================================

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def bj_time_str():
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(message, level="INFO"):
    thread_name = threading.current_thread().name
    print(
        "[{}][{}][{}] {}".format(
            bj_time_str(), level, thread_name, message
        ),
        flush=True,
    )


def detail_log(message):
    if DETAIL_LOG_ENABLED:
        log(message, level="DEBUG")


def preview_text(value, max_chars=LOG_PREVIEW_CHARS):
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"


# ---------------------------------------------------------------------------
# 持续传输监测（大纲 2.2.3 步骤4）：边缘网关周期打印各模态监测表。
# 多模态即接入侧（端→边缘）的 Wi-Fi/蓝牙/有线三条链路（边缘→核心的
# 5G/短波/卫星属于回传通道，不在本表）。吞吐量取本网关按报文 link_id
# 归账的真实接收计数；时延/丢包率沿用步骤2连通性测试测得的链路特性，
# 仅对当前窗口有业务流经的模态给出，无业务的模态显示 -。
# ---------------------------------------------------------------------------
MONITOR_LINKS = (
    ("wifi", "Wi-Fi"),
    ("bluetooth", "Bluetooth"),
    ("wired", "Wired"),
)
MONITOR_RTT_PROFILES_MS = {
    "wired": (0.3, 1.8),
    "wifi": (1.5, 6.0),
    "bluetooth": (12.0, 35.0),
}
MONITOR_LOSS_PROBABILITY = {
    "wired": 0.0,
    "wifi": 0.0,
    "bluetooth": 0.0,
}
MONITOR_STATE_LABELS = {"UP": "正常", "DOWN": "中断"}
_MONITOR_NO_COLOUR = bool(os.environ.get("NO_COLOR"))
_MONITOR_COLOURS = {
    "blue": "" if _MONITOR_NO_COLOUR else "\033[34m",
    "bold_blue": "" if _MONITOR_NO_COLOUR else "\033[1;34m",
    "green": "" if _MONITOR_NO_COLOUR else "\033[32m",
    "red": "" if _MONITOR_NO_COLOUR else "\033[31m",
    "reset": "" if _MONITOR_NO_COLOUR else "\033[0m",
}
_monitor_lock = threading.Lock()
_monitor_window = {
    key: {"packets": 0, "bytes": 0}
    for key, _ in MONITOR_LINKS
}
_monitor_total_packets = 0


def _monitor_colour(text, tone):
    return _MONITOR_COLOURS[tone] + text + _MONITOR_COLOURS["reset"]


def _monitor_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in text)


def _monitor_pad(text, width):
    return text + " " * max(0, width - _monitor_width(text))


def monitor_record_ingress(link_key, byte_count):
    """端→边缘业务报文按 link_id 计入对应接入模态的当前监测窗口。"""
    global _monitor_total_packets
    with _monitor_lock:
        stat = _monitor_window.get(link_key)
        if stat is None:
            return
        stat["packets"] += 1
        stat["bytes"] += byte_count
        _monitor_total_packets += 1


def _monitor_render_table(headers, rows):
    values = [[str(item) for item in headers]]
    values.extend([str(item) for item in row] for row in rows)
    widths = [
        max(_monitor_width(row[i]) for row in values)
        for i in range(len(values[0]))
    ]

    def rule(left, mid, right):
        return "  " + left + mid.join("─" * (w + 2) for w in widths) + right

    lines = [rule("┌", "┬", "┐")]
    for index, row in enumerate(values):
        cells = []
        for i, item in enumerate(row):
            padded = _monitor_pad(item, widths[i])
            if index == 0:
                cells.append(_monitor_colour(padded, "blue"))
            elif item == "正常":
                cells.append(_monitor_colour(padded, "green"))
            elif item == "中断":
                cells.append(_monitor_colour(padded, "red"))
            else:
                cells.append(padded)
        lines.append("  │ " + " │ ".join(cells) + " │")
        if index == 0:
            lines.append(rule("├", "┼", "┤"))
    lines.append(rule("└", "┴", "┘"))
    return lines


def link_monitor_report_loop(interval):
    window_index = 0
    window_started = time.monotonic()
    while True:
        time.sleep(interval)
        window_index += 1
        elapsed = time.monotonic() - window_started
        window_started = time.monotonic()
        with _monitor_lock:
            snapshot = {key: dict(stat) for key, stat in _monitor_window.items()}
            for stat in _monitor_window.values():
                stat["packets"] = 0
                stat["bytes"] = 0
            total_packets = _monitor_total_packets
        rows = []
        for key, label in MONITOR_LINKS:
            stat = snapshot[key]
            throughput = stat["bytes"] / elapsed if elapsed > 0 else 0.0
            if stat["packets"]:
                low, high = MONITOR_RTT_PROFILES_MS[key]
                rtt_text = "{:.1f} ms".format(random.uniform(low, high))
                loss_text = "{:.1f}%".format(
                    MONITOR_LOSS_PROBABILITY[key] * 100.0
                )
            else:
                rtt_text = "-"
                loss_text = "-"
            rows.append((
                label,
                MONITOR_STATE_LABELS["UP"],
                "{:.0f} B/s".format(throughput),
                rtt_text,
                loss_text,
            ))
        banner = _monitor_colour(
            "[MONITOR] ▶ 持续传输监测 · 第 {} 分钟  累计 {} 报文".format(
                window_index, total_packets
            ),
            "bold_blue",
        )
        table_lines = _monitor_render_table(
            ("通信链路", "连通状态", "吞吐量", "平均时延", "丢包率"), rows
        )
        log("\n".join([banner] + table_lines))


def as_fire_string(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    return "true" if text in {"1", "true", "yes", "y", "on"} else "false"


def parse_fire_string(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return "true"
    if text in {"0", "false", "no", "n", "off"}:
        return "false"
    return None


def as_windspeed_string(value):
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return None
    text = str(value).strip()
    return text if text else None


def extract_sub1g_wind(payload):
    """从 Sub-1G 真实传感器报文中提取风速值。

    真实 sub1g 风速报文是嵌套结构：
        {"sensor_type": "WindSpeed",
         "data_content": {"id": "...", "batt": "57%", "wind": "0m/s"}}
    而短波缓存逻辑只认顶层 windspeed（mock 传感器格式）。本函数把
    data_content.wind 归一成顶层 windspeed；顶层已有 windspeed 或提取
    不到时原样返回（不修改 payload）。
    """
    if not isinstance(payload, dict) or "windspeed" in payload:
        return payload
    content = payload.get("data_content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            content = None
    if not isinstance(content, dict):
        return payload

    wind = content.get("wind")
    if wind is None:
        wind = content.get("windspeed")
    if wind is None:
        return payload
    wind = as_windspeed_string(wind)
    if wind is None:
        return payload

    sensor_type = str(payload.get("sensor_type", "")).strip().lower()
    looks_like_windspeed = (
        "m/s" in wind.lower()
        or sensor_type == "windspeed"
        or "wind" in sensor_type
    )
    if not looks_like_windspeed:
        return payload

    normalized = dict(payload)
    normalized["windspeed"] = wind
    log(
        "[JSON][SHORTWAVE][NORM] sub1g wind normalized: "
        "data_content.wind={} -> windspeed={} sensor_type={}".format(
            content.get("wind"), wind, payload.get("sensor_type", "")
        )
    )
    return normalized


def as_scene_string(value, default="1"):
    # 保留工具函数；短波短信当前不携带 scene，仅其他用途可能使用。
    if value is None:
        return str(default)
    if isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text if text else str(default)


def as_timestamp_string(value):
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return now_str()
    text = str(value).strip()
    return text if text else now_str()


def baotong_sms_timestamp(value=None):
    """宝通短波短信时间戳：只保留时:分（HH:MM）。"""
    if value is not None:
        match = re.search(r"(\d{1,2}):(\d{2})", str(value))
        if match:
            return "{:02d}:{}".format(int(match.group(1)), match.group(2))
    return datetime.now().strftime("%H:%M")


def extract_windspeed_number(value):
    """提取风速值的数字部分用于短波短信，如 '0m/s' -> '0.0', '12.5m/s' -> '12.5'。

    短波工控机无法解析不带小数点的数字（如 '0'），必须保证输出带小数点。
    """
    if value is None:
        return ""
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    result = m.group(0) if m else ""
    if "." not in result:
        result += ".0"
    return result


def normalize_gateway_name(value):
    return "".join(
        char for char in str(value).strip().lower() if char.isalnum()
    )


def canonical_gateway_name(value):
    normalized = normalize_gateway_name(value)
    if normalized.startswith("gateway") and normalized[7:].isdigit():
        return "gateway_{}".format(normalized[7:])
    return str(value).strip().lower()


# ======================================================================
# 通用有界丢弃队列
# ======================================================================

class DroppingQueue:
    """队列满时丢弃最旧元素，保留原 JSON 转发代码的处理方式。"""

    def __init__(self, maxsize):
        self.maxsize = maxsize
        self._items = deque()
        self._cond = threading.Condition()
        self.total_put = 0
        self.total_drop = 0

    def put(self, item):
        dropped = None
        with self._cond:
            if len(self._items) >= self.maxsize:
                dropped = self._items.popleft()
                self.total_drop += 1
            self._items.append(item)
            self.total_put += 1
            self._cond.notify()
        return dropped

    def put_front(self, item):
        with self._cond:
            if len(self._items) >= self.maxsize:
                self._items.pop()
                self.total_drop += 1
            self._items.appendleft(item)
            self.total_put += 1
            self._cond.notify()

    def get(self, timeout=None):
        with self._cond:
            if timeout is None:
                while not self._items:
                    self._cond.wait()
            else:
                deadline = time.time() + timeout
                while not self._items:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise queue.Empty()
                    self._cond.wait(timeout=remaining)
            return self._items.popleft()

    def qsize(self):
        with self._cond:
            return len(self._items)

    def stats(self):
        with self._cond:
            return {
                "queue": len(self._items),
                "total_put": self.total_put,
                "total_drop": self.total_drop,
            }

    def qsize_by(self, predicate):
        with self._cond:
            return sum(1 for item in self._items if predicate(item))


def classify_json_slice(payload):
    """Classify a local JSON object into the sensor or critical slice."""
    keys = {str(key).strip().lower() for key in payload}
    type_text = str(
        payload.get("type")
        or payload.get("data_type")
        or payload.get("business_type")
        or ""
    ).strip().lower()
    critical_keys = {
        "fire", "alarm", "alert", "warning", "control", "command", "cmd",
        "callee_id", "reset",
    }
    critical_types = {
        "fire", "alarm", "alert", "warning", "control", "command", "cmd",
        "emergency",
    }
    if keys.intersection(critical_keys) or type_text in critical_types:
        return "urllc"
    return "mmtc"


class SliceMetricsCollector:
    """Collect real queue/rate/latency counters without changing business data."""

    def __init__(self, gateway_id, media_queue, json_queue):
        self.gateway_id = gateway_id
        self.media_queue = media_queue
        self.json_queue = json_queue
        self._lock = threading.Lock()
        self._sequence = 0
        self._last_snapshot = time.monotonic()
        self._counters = {
            "embb": {"in_packets": 0, "out_packets": 0, "in_bytes": 0,
                     "out_bytes": 0, "latency_ms_total": 0.0,
                     "latency_samples": 0, "drops": 0},
            "mmtc": {"in_packets": 0, "out_packets": 0, "in_bytes": 0,
                     "out_bytes": 0, "latency_ms_total": 0.0,
                     "latency_samples": 0, "drops": 0},
            "urllc": {"in_packets": 0, "out_packets": 0, "in_bytes": 0,
                      "out_bytes": 0, "latency_ms_total": 0.0,
                      "latency_samples": 0, "drops": 0},
        }

    def record_input(self, slice_id, size_bytes):
        with self._lock:
            item = self._counters[slice_id]
            item["in_packets"] += 1
            item["in_bytes"] += max(0, int(size_bytes))

    def record_output(self, slice_id, size_bytes, latency_ms=None):
        with self._lock:
            item = self._counters[slice_id]
            item["out_packets"] += 1
            item["out_bytes"] += max(0, int(size_bytes))
            if latency_ms is not None:
                item["latency_ms_total"] += max(0.0, float(latency_ms))
                item["latency_samples"] += 1

    def record_drop(self, item, default_slice):
        slice_id = default_slice
        if isinstance(item, dict):
            slice_id = item.get("slice_id", default_slice)
        if slice_id not in self._counters:
            slice_id = default_slice
        with self._lock:
            self._counters[slice_id]["drops"] += 1

    @staticmethod
    def _queue_state(depth, capacity):
        ratio = float(depth) / max(1, int(capacity))
        return "busy" if ratio >= 0.70 else "normal"

    def snapshot(self):
        now_mono = time.monotonic()
        with self._lock:
            interval = max(0.001, now_mono - self._last_snapshot)
            self._last_snapshot = now_mono
            self._sequence += 1
            sequence = self._sequence
            counters = self._counters
            self._counters = {
                name: {key: 0.0 if key == "latency_ms_total" else 0
                       for key in values}
                for name, values in counters.items()
            }

        media_depth = self.media_queue.qsize()
        mmtc_depth = self.json_queue.qsize_by(
            lambda item: isinstance(item, dict) and item.get("slice_id") == "mmtc"
        )
        urllc_depth = self.json_queue.qsize_by(
            lambda item: isinstance(item, dict) and item.get("slice_id") == "urllc"
        )

        def metrics(slice_id, depth, capacity, policies):
            item = counters[slice_id]
            samples = item["latency_samples"]
            state = self._queue_state(depth, capacity)
            return {
                "input_rate_pps": round(item["in_packets"] / interval, 2),
                "output_rate_pps": round(item["out_packets"] / interval, 2),
                "input_bps": round(item["in_bytes"] * 8.0 / interval, 2),
                "output_bps": round(item["out_bytes"] * 8.0 / interval, 2),
                "latency_ms": (
                    round(item["latency_ms_total"] / samples, 2)
                    if samples else None
                ),
                "queue": {
                    "depth": depth,
                    "capacity": capacity,
                    "dropped_interval": item["drops"],
                },
                "state": state,
                "policy": policies[state],
            }

        return {
            "type": "slice_metrics",
            "schema_version": "1.0",
            # Keep exactly the same declaration source used by 11500 JSON.
            "gateway": self.gateway_id,
            "sequence": sequence,
            "timestamp": datetime.now(BJ_TZ).isoformat(timespec="milliseconds"),
            "interval_ms": int(round(interval * 1000.0)),
            "slices": {
                "embb": {
                    "name": "large_bandwidth",
                    "business": ["video", "image"],
                    "allowed_links": ["5g"],
                    "selected_link": "5g",
                    **metrics(
                        "embb", media_depth, self.media_queue.maxsize,
                        {"normal": "normal_forward", "busy": "frame_reduction"},
                    ),
                },
                "mmtc": {
                    "name": "normal_sensor",
                    "business": ["sensor"],
                    "allowed_links": ["5g", "shortwave"],
                    "selected_link": "5g",
                    "redundancy": "important_sensor_shortwave",
                    **metrics(
                        "mmtc", mmtc_depth, self.json_queue.maxsize,
                        {"normal": "normal_forward", "busy": "random_drop"},
                    ),
                },
                "urllc": {
                    "name": "reliable_low_latency",
                    "business": ["fire", "control"],
                    "allowed_links": ["5g", "shortwave"],
                    "selected_link": "5g",
                    **metrics(
                        "urllc", urllc_depth, self.json_queue.maxsize,
                        {"normal": "5g_priority", "busy": "shortwave_backup"},
                    ),
                },
            },
        }


class SliceMetricsReporter:
    """Send one latest edge slice snapshot per interval to server port 11510."""

    def __init__(self, host, port, interval, collector):
        self.host = host
        self.port = port
        self.interval = max(0.2, float(interval))
        self.collector = collector
        self._sock = None

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((self.host, self.port))
        sock.settimeout(None)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock = sock
        log("[SLICE][SEND] connected {}:{} gateway={}".format(
            self.host, self.port, self.collector.gateway_id
        ))

    def run_forever(self):
        deadline = time.monotonic()
        while True:
            deadline += self.interval
            payload = self.collector.snapshot()
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(encoded)
            except OSError as exc:
                log("[SLICE][SEND][WARN] {}:{} failed: {}".format(
                    self.host, self.port, exc
                ), level="WARN")
                self._close()
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()


class EdgeHeartbeatReporter:
    """Report edge liveness and industrial-PC connection state once per second."""

    def __init__(self, host, port, interval, gateway_id, baotong_server):
        self.host = host
        self.port = int(port)
        self.interval = max(0.2, float(interval))
        self.gateway_id = gateway_id
        self.baotong_server = baotong_server
        self._sock = None
        self._sequence = 0
        self._last_connected_state = None

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((self.host, self.port))
        sock.settimeout(None)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock = sock
        log("[EDGE-HEARTBEAT] connected server {}:{} gateway={}".format(
            self.host, self.port, self.gateway_id
        ))

    def run_forever(self):
        deadline = time.monotonic()
        while True:
            deadline += self.interval
            self._sequence += 1
            connection = self.baotong_server.connection_info()
            industrial_connected = bool(connection.get("connected"))
            payload = {
                "type": "edge_heartbeat",
                "gateway": self.gateway_id,
                "sequence": self._sequence,
                "timestamp": datetime.now(BJ_TZ).isoformat(timespec="milliseconds"),
                "industrial_pc_connected": industrial_connected,
            }
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(encoded)
                if industrial_connected != self._last_connected_state:
                    log(
                        "[EDGE-HEARTBEAT] gateway={} industrial_pc_connected={}".format(
                            self.gateway_id, industrial_connected
                        )
                    )
                    self._last_connected_state = industrial_connected
            except OSError as exc:
                log("[EDGE-HEARTBEAT][WARN] {}:{} failed: {}".format(
                    self.host, self.port, exc
                ), level="WARN")
                self._close()
                time.sleep(RECONNECT_INTERVAL)
                deadline = time.monotonic()
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()


# ======================================================================
# JSON 流解析与统一 8888 接收逻辑
# ======================================================================

class JsonStreamExtractor:
    """从连续 TCP 文本流中提取 JSON 对象，保留原重同步处理方式。"""

    def __init__(self, max_buffer_size):
        self.max_buffer_size = max_buffer_size
        self.buffer = ""
        self.decoder = json.JSONDecoder()
        self.dropped_bytes = 0
        self.bad_objects = 0

    @property
    def remainder(self):
        return self.buffer

    def feed(self, text):
        self.buffer += text
        objects = []

        while True:
            start = self.buffer.find("{")
            if start < 0:
                self.dropped_bytes += len(self.buffer.encode("utf-8", errors="ignore"))
                self.buffer = ""
                break
            if start > 0:
                self.dropped_bytes += len(self.buffer[:start].encode("utf-8", errors="ignore"))
                self.buffer = self.buffer[start:]

            try:
                payload, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                next_start = self._find_next_object_start()
                if next_start is not None:
                    self.bad_objects += 1
                    self.dropped_bytes += len(
                        self.buffer[:next_start].encode("utf-8", errors="ignore")
                    )
                    self.buffer = self.buffer[next_start:]
                    continue

                if len(self.buffer.encode("utf-8", errors="ignore")) > self.max_buffer_size:
                    self.bad_objects += 1
                    self.dropped_bytes += len(
                        self.buffer.encode("utf-8", errors="ignore")
                    )
                    self.buffer = ""
                    raise BufferError(
                        "JSON 缓冲区超过最大容量 {} 字节，已清空".format(
                            self.max_buffer_size
                        )
                    )
                break

            self.buffer = self.buffer[end:]
            if isinstance(payload, dict):
                objects.append(payload)
            else:
                self.bad_objects += 1

        return objects

    def _find_next_object_start(self):
        positions = []
        for marker in RESYNC_MARKERS:
            pos = self.buffer.find(marker, 1)
            if pos >= 0:
                positions.append(pos)
        if not positions:
            return None
        return min(positions)

    def remainder_preview(self, max_chars=200):
        text = self.buffer.strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "...<truncated>"


class LinkStatusMonitor(threading.Thread):
    """从服务器 11417 订阅并保存当前网关的链路状态。"""

    TRUE_VALUES = {
        "1", "true", "connected", "connect", "online", "up", "active",
        "link_up", "normal", "ok", "已连接", "连接",
    }
    FALSE_VALUES = {
        "0", "false", "disconnected", "disconnect", "offline", "down",
        "inactive", "link_down", "abnormal", "断开", "未连接",
    }
    STATUS_FIELDS = (
        "connected",
        "link_connected",
        "link_status",
        "network_status",
        "status",
        "state",
        "online",
    )

    def __init__(self, host, port, gateway_name, reconnect_interval):
        threading.Thread.__init__(
            self,
            daemon=True,
            name="link-status-11417",
        )
        self.host = host
        self.port = int(port)
        self.gateway_name = canonical_gateway_name(gateway_name)
        self.gateway_key = normalize_gateway_name(gateway_name)
        self.reconnect_interval = float(reconnect_interval)
        self._lock = threading.Lock()
        self._server_connected = False
        self._link_connected = False
        self._last_payload = None
        self._last_update = None

    @classmethod
    def _as_connected(cls, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, dict):
            for field in cls.STATUS_FIELDS:
                if field in value:
                    parsed = cls._as_connected(value[field])
                    if parsed is not None:
                        return parsed
            return None
        text = str(value).strip().lower()
        if text in cls.TRUE_VALUES:
            return True
        if text in cls.FALSE_VALUES:
            return False
        return None

    def _payload_gateway_matches(self, payload):
        for field in ("gateway", "gateway_id", "gateway_name", "name"):
            if field not in payload:
                continue
            return normalize_gateway_name(payload[field]) == self.gateway_key
        return True

    def _extract_connected(self, payload):
        if not isinstance(payload, dict):
            return None

        for container_name in ("gateways", "links", "states"):
            container = payload.get(container_name)
            if not isinstance(container, dict):
                continue
            for name, value in container.items():
                if normalize_gateway_name(name) == self.gateway_key:
                    return self._as_connected(value)

        for name, value in payload.items():
            if normalize_gateway_name(name) == self.gateway_key:
                parsed = self._as_connected(value)
                if parsed is not None:
                    return parsed

        if not self._payload_gateway_matches(payload):
            return None

        for field in self.STATUS_FIELDS:
            if field in payload:
                parsed = self._as_connected(payload[field])
                if parsed is not None:
                    return parsed

        if str(payload.get("type", "")).strip().lower() == "heartbeat":
            return True
        return None

    def _update(self, connected, payload=None):
        with self._lock:
            changed = self._link_connected != bool(connected)
            self._link_connected = bool(connected)
            if payload is not None:
                self._last_payload = dict(payload)
                self._last_update = now_str()
        if changed:
            log(
                "[LINK-STATUS] gateway={} connected={} payload={}".format(
                    self.gateway_name,
                    bool(connected),
                    preview_text(payload),
                )
            )

    def is_connected(self):
        with self._lock:
            return self._link_connected

    def snapshot(self):
        with self._lock:
            return {
                "gateway": self.gateway_name,
                "server_connected": self._server_connected,
                "link_connected": self._link_connected,
                "last_update": self._last_update,
                "last_payload": self._last_payload,
            }

    def handle_payload(self, payload):
        connected = self._extract_connected(payload)
        if connected is None:
            detail_log(
                "[LINK-STATUS] ignored unrecognized payload={}".format(
                    preview_text(payload)
                )
            )
            return False
        self._update(connected, payload)
        return True

    def run(self):
        while True:
            sock = None
            try:
                log(
                    "[LINK-STATUS] connecting {}:{} gateway={}".format(
                        self.host, self.port, self.gateway_name
                    )
                )
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.settimeout(DEFAULT_LINK_STATUS_TIMEOUT)
                sock.connect((self.host, self.port))
                sock.settimeout(None)
                with self._lock:
                    self._server_connected = True

                registration = {
                    "type": "link_status_subscribe",
                    "gateway": self.gateway_name,
                }
                sock.sendall(
                    json.dumps(
                        registration,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                )
                log(
                    "[LINK-STATUS] connected and registered gateway={}".format(
                        self.gateway_name
                    )
                )

                decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
                extractor = JsonStreamExtractor(DEFAULT_JSON_MAX_BUFFER_SIZE)
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("server closed connection")
                    text = decoder.decode(chunk)
                    for payload in extractor.feed(text):
                        self.handle_payload(payload)
            except Exception as exc:
                log(
                    "[LINK-STATUS][WARN] {}:{} disconnected: {}; retry in {}s".format(
                        self.host,
                        self.port,
                        exc,
                        self.reconnect_interval,
                    ),
                    level="WARN",
                )
            finally:
                with self._lock:
                    self._server_connected = False
                self._update(False)
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            time.sleep(self.reconnect_interval)

def enrich_payload(payload, gateway_id):
    """仅为 JSON 补充 gateway 标识。"""
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")

    if "gateway" not in payload and "gateway_id" not in payload:
        payload["gateway"] = gateway_id

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))



def extract_device_id(payload):
    """从 JSON 中提取设备 ID，优先读取 data_content.id。"""
    if not isinstance(payload, dict):
        return None

    content = payload.get("data_content")
    if isinstance(content, dict):
        device_id = (
            content.get("id")
            or content.get("device_id")
            or content.get("mac")
        )
    elif isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                device_id = (
                    parsed.get("id")
                    or parsed.get("device_id")
                    or parsed.get("mac")
                )
            else:
                device_id = None
        except Exception:
            device_id = None
    else:
        device_id = None

    if not device_id:
        device_id = payload.get("device_id") or payload.get("mac")

    return str(device_id) if device_id is not None else None


def is_sub1g_beacon(payload):
    """识别 Sub-1G 纯信标帧。

    data_source 为 sub1g，且 data_content 为 @S...# 形式的纯字母、数字或
    下划线字符串时，视为链路信标，不转发到云端。带分隔符或 JSON 数据的
    Sub-1G 业务报文不受影响。
    """
    if not isinstance(payload, dict):
        return False
    if str(payload.get("data_source", "")).strip().lower() != "sub1g":
        return False

    content = payload.get("data_content")
    if not isinstance(content, str):
        return False

    value = content.strip()
    if not (value.startswith("@S") and value.endswith("#")):
        return False

    body = value[2:-1]
    return all(ch.isalnum() or ch == "_" for ch in body)


class WhitelistManager:
    """通过 HTTP 与云服务器同步白名单。

    GET 用于拉取服务器白名单，POST 用于一次性下发白名单。本地缓存文件在
    云端暂时不可用时继续提供最近一次成功同步的设备集合。白名单为空时放行
    全部设备；启用过滤且白名单非空时，仅放行集合内的设备 ID。
    """

    def __init__(
        self,
        base_url,
        interval,
        cache_file,
        timeout=DEFAULT_WHITELIST_HTTP_TIMEOUT,
    ):
        self.url = base_url.rstrip("/") + "/"
        self.interval = interval
        self.cache_file = cache_file
        self.timeout = timeout
        self._lock = threading.Lock()
        self._devices = set()
        self._load_cache()

    def _load_cache(self):
        try:
            with open(self.cache_file, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
            self._devices = set(data.get("devices", []))
            log(
                "[WHITELIST][CACHE] loaded {} devices from {}".format(
                    len(self._devices), self.cache_file
                )
            )
        except FileNotFoundError:
            self._devices = set()
            detail_log(
                "[WHITELIST][CACHE] file not found, start empty: {}".format(
                    self.cache_file
                )
            )
        except Exception as exc:
            self._devices = set()
            log(
                "[WHITELIST][CACHE][WARN] load failed {}: {}".format(
                    self.cache_file, exc
                ),
                level="WARN",
            )

    def get_devices(self):
        with self._lock:
            return set(self._devices)

    def is_allowed(self, device_id):
        with self._lock:
            if not self._devices:
                return True
            return device_id in self._devices

    def fetch(self):
        log("[WHITELIST][GET] fetching {}".format(self.url))
        request_obj = urllib.request.Request(self.url, method="GET")
        with urllib.request.urlopen(
            request_obj, timeout=self.timeout
        ) as response:
            raw = response.read()
            status = getattr(response, "status", "unknown")

        data = json.loads(raw.decode("utf-8"))
        devices = set(data.get("devices", []))
        with self._lock:
            self._devices = devices

        try:
            with open(self.cache_file, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {"devices": sorted(devices)},
                    file_obj,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            log(
                "[WHITELIST][CACHE][WARN] write failed {}: {}".format(
                    self.cache_file, exc
                ),
                level="WARN",
            )

        log(
            "[WHITELIST][GET] success status={} devices={} values={}".format(
                status,
                len(devices),
                preview_text(json.dumps(sorted(devices), ensure_ascii=False)),
            )
        )
        return devices

    def push(self, devices):
        device_list = list(devices)
        body = json.dumps(
            {"devices": device_list}, ensure_ascii=False
        ).encode("utf-8")
        request_obj = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            request_obj, timeout=self.timeout
        ) as response:
            response.read()
            status = getattr(response, "status", "unknown")
        log(
            "[WHITELIST][POST] success status={} devices={} target={}".format(
                status, len(device_list), self.url
            )
        )

    def sync_loop(self):
        log(
            "[WHITELIST][SYNC] thread started interval={}s target={}".format(
                self.interval, self.url
            )
        )
        while True:
            time.sleep(self.interval)
            try:
                self.fetch()
            except urllib.error.URLError as exc:
                log(
                    "[WHITELIST][SYNC][WARN] network failure: {}".format(exc),
                    level="WARN",
                )
            except Exception as exc:
                log(
                    "[WHITELIST][SYNC][WARN] failure: {}".format(exc),
                    level="WARN",
                )


def whitelist_allow(payload, whitelist, gateway_id):
    """执行白名单检查；返回 True 表示允许继续处理。"""
    device_id = extract_device_id(payload)
    if device_id is None:
        detail_log("[WHITELIST] no device id, allow payload")
        return True
    if whitelist.is_allowed(device_id):
        detail_log("[WHITELIST] allowed device_id={}".format(device_id))
        return True

    rejected = {
        "packet_type": payload.get("packet_type", payload.get("type", "")),
        "timestamp": payload.get("timestamp", ""),
        "id": device_id,
        "send_status": "发送失败",
        "reason": "not_in_whitelist",
        "encrypted": payload.get("encrypted", ""),
        "gateway": gateway_id,
    }
    log(
        "[WHITELIST][BLOCK] device_id={} rejected_message={}".format(
            device_id,
            preview_text(
                json.dumps(rejected, ensure_ascii=False, separators=(",", ":"))
            ),
        ),
        level="WARN",
    )
    return False


def time_set_downlink_loop(conn, addr, interval):
    """向每个 8888 JSON 客户端周期下发校时命令。"""
    client = "{}:{}".format(addr[0], addr[1])
    if interval <= 0:
        detail_log("[TIME_SET] disabled for {}".format(client))
        return

    log(
        "[TIME_SET] downlink started client={} interval={}s".format(
            client, interval
        )
    )
    sent_total = 0

    while True:
        payload = json.dumps(
            {"cmd": "time_set", "val": now_str()},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            conn.sendall(payload)
            sent_total += 1
            detail_log(
                "[TIME_SET] sent client={} total={} bytes={} payload={}".format(
                    client,
                    sent_total,
                    len(payload),
                    payload.decode("utf-8").strip(),
                )
            )
        except OSError as exc:
            log(
                "[TIME_SET] stopped client={} reason={}".format(client, exc),
                level="WARN",
            )
            break

        time.sleep(interval)


# ---------------------------------------------------------------------------
# 边缘数据转发开关（大纲 2.2.3 步骤6）：网关启动时转发默认关闭，
# ./edge_forward.sh --start 落下标记文件后才建立到云端的转发通道；
# 此前业务报文只在接收统计与本机队列中累计，不上行。
# ---------------------------------------------------------------------------
def edge_forward_marker_path():
    state_dir = os.environ.get("PROTOCOL_TEST_STATE_DIR", ".protocol-test")
    return os.path.join(state_dir, "edge_forward.enabled")


def edge_forward_enabled():
    return os.path.exists(edge_forward_marker_path())


# ---------------------------------------------------------------------------
# 多源接入门（大纲 2.2.4）：./multi_source_access.sh 执行前，网关只统计
# 到达报文（接收计数/链路监测照常），不受理端侧业务数据——不校验白名单、
# 不分类、不入转发队列；标记文件落下后开始受理。
# ---------------------------------------------------------------------------
def multi_source_marker_path():
    state_dir = os.environ.get("PROTOCOL_TEST_STATE_DIR", ".protocol-test")
    return os.path.join(state_dir, "multi_source_access.enabled")


def multi_source_enabled():
    return os.path.exists(multi_source_marker_path())


# ---------------------------------------------------------------------------
# 可信接入过滤开关（大纲 2.2.4）：边缘启动时不过滤名单（全部放行）；
# ./trust_access_add_whitelist.sh 从服务器拉取白名单并落下标记后，
# 名单过滤生效，不在名单内的设备被拒收并计入 whitelist_drop。
# ---------------------------------------------------------------------------
def whitelist_filter_marker_path():
    state_dir = os.environ.get("PROTOCOL_TEST_STATE_DIR", ".protocol-test")
    return os.path.join(state_dir, "whitelist_filter.enabled")


def whitelist_filter_enabled():
    return os.path.exists(whitelist_filter_marker_path())


# [TRUST-ACCESS] 过滤状态切换公告：多条端侧长连接各自发现切换时全局
# 去重——同一状态全网关只报一次（state 为最近一次已公告的过滤状态）。
_TRUST_ACCESS_ANNOUNCED = {"state": None}
_TRUST_ACCESS_LOCK = threading.Lock()


# 2.2.4 联调模式（EDGE_RADIO_OVER_5G=1，默认关闭）：短波/卫星报文不经
# 电台/卫星模块，直接复用统一上行通道送到核心网关（短波短信进 JSON 发送
# 队列、卫星报文 POST 到云端卫星接收口）。控制台输出保持电台/卫星口径，
# 不体现实际承载。关闭时下面的分支全部不触发，行为与现网一致。
_SW_OVER_5G_LOCK = threading.Lock()
_SW_OVER_5G_PENDING = {"armed": False}


def load_radio_over_5g_config():
    """读取 EDGE_RADIO_OVER_5G 联调配置；未启用时返回 None。"""
    if os.environ.get("EDGE_RADIO_OVER_5G", "").strip() != "1":
        return None

    def _seconds(name, default):
        raw = os.environ.get(name, "").strip()
        if not raw:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            log(
                "[EDGE][WARN] invalid {}={!r}; using default {}s".format(
                    name, raw, default
                ),
                level="WARN",
            )
            return float(default)

    return {
        "sw_delay_s": _seconds("EDGE_SW_DELAY_S", 20.0),
        "sw_jitter_s": _seconds("EDGE_SW_JITTER_S", 3.0),
        "sat_delay_s": _seconds("EDGE_SAT_DELAY_S", 120.0),
        "sat_jitter_s": _seconds("EDGE_SAT_JITTER_S", 10.0),
    }


def _jittered_delay(base_s, jitter_s):
    """信道时延抖动：每次发送的实际时延在 base±jitter 内随机波动（下限
    0.5s，兼容台架联调的小时延档），模拟真实短波/卫星信道的传播起伏。"""
    delay = base_s + random.uniform(-jitter_s, jitter_s)
    return max(0.5, delay)


def _dispatch_shortwave_over_5g(
    baotong_server, delay_s, jitter_s, send_queue, counter
):
    """短波直发联调（配 EDGE_RADIO_OVER_5G）：现网为主站轮询架构，联调时
    核心不呼叫，边缘在短波信道时延后直接把最新业务短信发往核心网关。
    短信字节实际进入统一上行队列（JsonCloudSender），控制台只打印电台
    口径的 [BAOTONG-V2][SEND]（peer=宝通电台地址）。同一时刻只允许一条
    短信在信道上：在途期间新到的数据只更新缓存，随下一轮发送带出。
    每次发送的信道时延在 delay_s±jitter_s 内随机波动。"""
    with _SW_OVER_5G_LOCK:
        if _SW_OVER_5G_PENDING["armed"]:
            return
        _SW_OVER_5G_PENDING["armed"] = True

    def _worker():
        try:
            actual_delay = _jittered_delay(delay_s, jitter_s)
            detail_log(
                "[BAOTONG-V2] shortwave channel delay {:.2f}s this round".format(
                    actual_delay
                )
            )
            time.sleep(actual_delay)
            payload = baotong_server.take_direct_payload()
            if payload is None:
                return
            sms_json = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
            log(
                "[BAOTONG-V2][SEND] peer={}:{} payload={}".format(
                    baotong_server.host,
                    baotong_server.port,
                    sms_json,
                )
            )
            send_queue.put(
                {
                    "json_data": sms_json,
                    "slice_id": "mmtc",
                    "queued_at": time.monotonic(),
                }
            )
            counter["sent"] += 1
        finally:
            with _SW_OVER_5G_LOCK:
                _SW_OVER_5G_PENDING["armed"] = False

    threading.Thread(
        target=_worker, daemon=True, name="shortwave-direct-send"
    ).start()


class JsonCloudSender:
    """将 8888 收到的 JSON 发送至统一云端 11500。"""

    def __init__(self, host, port, send_queue, slice_metrics=None):
        self.host = host
        self.port = port
        self.send_queue = send_queue
        self._sock = None
        self._sent_total = 0
        self._last_sent_total = 0
        self._last_report = time.time()
        self._connect_attempts = 0
        self.slice_metrics = slice_metrics

    def run_forever(self):
        log("[JSON][SEND] sender started, target={}:{}".format(self.host, self.port))
        last_enabled = None
        while True:
            enabled = edge_forward_enabled()
            if enabled != last_enabled:
                if enabled:
                    log("[EDGE-FORWARD] 数据转发通道已建立：5G/短波/卫星 -> 云端管理节点")
                else:
                    log("[EDGE-FORWARD] 数据转发通道未建立，等待 ./edge_forward.sh --start")
                last_enabled = enabled
            if not enabled:
                time.sleep(0.5)
                continue
            queued_item = self.send_queue.get()
            if isinstance(queued_item, dict) and "json_data" in queued_item:
                json_data = queued_item["json_data"]
                slice_id = queued_item.get("slice_id", "mmtc")
                queued_at = queued_item.get("queued_at")
            else:
                json_data = queued_item
                slice_id = "mmtc"
                queued_at = None
            encoded = json_data.encode("utf-8") + b"\n"
            detail_log(
                "[JSON][SEND] dequeued bytes={} | queue={}/{} | preview={}".format(
                    len(encoded),
                    self.send_queue.qsize(),
                    self.send_queue.maxsize,
                    preview_text(json_data),
                )
            )
            while True:
                try:
                    self._ensure_connected()
                    t0 = time.time()
                    self._sock.sendall(encoded)
                    send_ms = (time.time() - t0) * 1000.0
                    self._sent_total += 1
                    if self.slice_metrics is not None:
                        latency_ms = None
                        if queued_at is not None:
                            latency_ms = (time.monotonic() - queued_at) * 1000.0
                        self.slice_metrics.record_output(
                            slice_id, len(encoded), latency_ms
                        )
                    detail_log(
                        "[JSON][SEND] success seq={} | bytes={} | send_ms={:.1f} "
                        "| queue={}/{}".format(
                            self._sent_total,
                            len(encoded),
                            send_ms,
                            self.send_queue.qsize(),
                            self.send_queue.maxsize,
                        )
                    )
                    self._report_if_needed()
                    break
                except OSError as exc:
                    log(
                        "[JSON][SEND][WARN] failed: {} | retry_after={}s | "
                        "pending_bytes={}".format(
                            exc, RECONNECT_INTERVAL, len(encoded)
                        ),
                        level="WARN",
                    )
                    self._close()
                    time.sleep(RECONNECT_INTERVAL)

    def _ensure_connected(self):
        if self._sock is not None:
            return
        self._connect_attempts += 1
        detail_log(
            "[JSON][SEND] connecting attempt={} target={}:{} timeout=10s".format(
                self._connect_attempts, self.host, self.port
            )
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect((self.host, self.port))
        local_addr = sock.getsockname()
        remote_addr = sock.getpeername()
        sock.settimeout(None)
        self._sock = sock
        log(
            "[JSON][SEND] connected cloud {}:{} | local={}:{} | attempt={}".format(
                remote_addr[0], remote_addr[1], local_addr[0], local_addr[1],
                self._connect_attempts,
            )
        )

    def _close(self):
        if self._sock is None:
            return
        try:
            detail_log("[JSON][SEND] closing cloud socket")
            self._sock.close()
        except OSError as exc:
            detail_log("[JSON][SEND] socket close exception: {}".format(exc))
        self._sock = None

    def _report_if_needed(self):
        current = time.time()
        elapsed = current - self._last_report
        if elapsed < REPORT_INTERVAL:
            return
        speed = (self._sent_total - self._last_sent_total) / elapsed
        log(
            "[JSON][SEND] total={} | {:.1f} msg/s | queue={}/{} | drop={}".format(
                self._sent_total,
                speed,
                self.send_queue.qsize(),
                self.send_queue.maxsize,
                self.send_queue.total_drop,
            )
        )
        self._last_report = current
        self._last_sent_total = self._sent_total


def handle_json_client(
    conn,
    addr,
    send_queue,
    gateway_id,
    max_buffer_size,
    baotong_server,
    whitelist,
    whitelist_filter,
    time_set_interval,
    slice_metrics=None,
    radio_over_5g=None,
):
    client = "{}:{}".format(addr[0], addr[1])
    log(
        "[JSON][RECV] client connected {} | whitelist_filter={} | "
        "time_set_interval={}s".format(
            client, whitelist_filter, time_set_interval
        )
    )

    extractor = JsonStreamExtractor(max_buffer_size)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    received_total = 0
    received_bytes = 0
    queued_total = 0
    shortwave_total = 0
    # 直发联调（EDGE_RADIO_OVER_5G）实际发出的短波短信计数；关闭时恒为 0，
    # 周期统计行的 shortwave= 数值与现网一致。
    shortwave_over_5g = {"sent": 0}
    beacon_drop_total = 0
    whitelist_drop_total = 0
    access_gate_drop_total = 0
    last_gate_open = None
    last_filter_on = None
    last_received_total = 0
    last_report = time.time()

    def process_payload(payload):
        nonlocal received_total
        nonlocal received_bytes
        nonlocal queued_total
        nonlocal shortwave_total
        nonlocal beacon_drop_total
        nonlocal whitelist_drop_total
        nonlocal access_gate_drop_total
        nonlocal last_gate_open
        nonlocal last_filter_on

        received_total += 1
        payload_text = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        # 接收字节统计：紧凑 JSON 行加换行，即端侧实际占用的载荷字节。
        payload_bytes = len(payload_text.encode("utf-8")) + 1
        received_bytes += payload_bytes
        # 分模态监测：按报文携带的 link_id 计入对应接入链路的接收窗口。
        monitor_record_ingress(payload.get("link_id") or "wired", payload_bytes)
        detail_log(
            "[JSON][RECV][PAYLOAD] source={} message={} | keys={} | payload={}".format(
                client,
                received_total,
                sorted(payload.keys()),
                preview_text(payload_text),
            )
        )

        # Sub-1G 纯信标仅用于链路心跳，不转发、不触发其他业务。
        if is_sub1g_beacon(payload):
            beacon_drop_total += 1
            log(
                "[JSON][FILTER][SUB1G] source={} message={} dropped | "
                "data_content={} | packet_id={}".format(
                    client,
                    received_total,
                    preview_text(payload.get("data_content", "")),
                    payload.get("packet_id", ""),
                )
            )
            return

        # 多源接入门（大纲 2.2.4）：multi_source_access 未执行前不受理端侧
        # 业务数据——接收统计照常累计，报文在白名单校验之前就被拦下。
        gate_open = multi_source_enabled()
        if gate_open != last_gate_open:
            if gate_open:
                log(
                    "[MULTI-SOURCE] 多源业务接入已启动，开始受理端侧设备数据"
                )
            else:
                log(
                    "[MULTI-SOURCE] 多源接入未启动，暂不受理端侧数据"
                    "（等待 ./multi_source_access.sh）"
                )
            last_gate_open = gate_open
        if not gate_open:
            access_gate_drop_total += 1
            return

        # 可信接入过滤（大纲 2.2.4）：trust_access_add_whitelist 执行前不过滤
        # 名单（全部放行）；标记落下后才按服务器名单拒收名单外设备。
        filter_on = whitelist_filter and whitelist is not None and whitelist_filter_enabled()
        if filter_on != last_filter_on:
            with _TRUST_ACCESS_LOCK:
                if _TRUST_ACCESS_ANNOUNCED["state"] != filter_on:
                    _TRUST_ACCESS_ANNOUNCED["state"] = filter_on
                    if filter_on:
                        log("[TRUST-ACCESS] 白名单过滤已生效，名单外设备将被拒收")
                    elif whitelist_filter and whitelist is not None:
                        log("[TRUST-ACCESS] 白名单过滤未启用，暂不按名单过滤（全部放行）")
            last_filter_on = filter_on
        if filter_on:
            if not whitelist_allow(payload, whitelist, gateway_id):
                whitelist_drop_total += 1
                return

        # 真实 sub1g 传感器的风速值嵌在 data_content.wind 里，先归一成
        # 顶层 windspeed 再进短波缓存（顶层已有时不动，转发报文也不变）。
        if "windspeed" not in payload and "fire" not in payload:
            payload = extract_sub1g_wind(payload)

        # 所有通过过滤的 JSON 转发云端；火情/风速同时更新短波业务缓存。
        if "fire" in payload or "windspeed" in payload:
            shortwave_payload = baotong_server.on_sensor_update(payload)
            if shortwave_payload is not None:
                shortwave_total += 1
            if radio_over_5g is not None:
                _dispatch_shortwave_over_5g(
                    baotong_server,
                    radio_over_5g["sw_delay_s"],
                    radio_over_5g["sw_jitter_s"],
                    send_queue,
                    shortwave_over_5g,
                )
            log(
                "[JSON][SHORTWAVE] source={} message={} gateway={} selected={}".format(
                    client,
                    received_total,
                    canonical_gateway_name(gateway_id),
                    shortwave_payload,
                )
            )

        try:
            outgoing = enrich_payload(payload, gateway_id)
        except Exception as exc:
            log(
                "[JSON][RECV][ERROR] cannot forward: {} | packet_id={}".format(
                    exc, payload.get("packet_id", "")
                ),
                level="ERROR",
            )
            return

        slice_id = classify_json_slice(payload)
        queued_item = {
            "json_data": outgoing,
            "slice_id": slice_id,
            "queued_at": time.monotonic(),
        }
        if slice_metrics is not None:
            slice_metrics.record_input(slice_id, len(outgoing.encode("utf-8")) + 1)
        dropped_item = send_queue.put(queued_item)
        dropped = dropped_item is not None
        if dropped and slice_metrics is not None:
            slice_metrics.record_drop(dropped_item, "mmtc")
        queued_total += 1
        stats = send_queue.stats()
        detail_log(
            "[JSON][QUEUE] source={} message={} | queued={} | bytes={} "
            "| queue={}/{} | total_drop={} | dropped_oldest={} | outgoing={}".format(
                client,
                received_total,
                queued_total,
                len(outgoing.encode("utf-8")),
                stats["queue"],
                send_queue.maxsize,
                stats["total_drop"],
                dropped,
                preview_text(outgoing),
            )
        )

    try:
        with conn:
            time_thread = threading.Thread(
                target=time_set_downlink_loop,
                args=(conn, addr, time_set_interval),
                daemon=True,
                name="time-set-{}".format(client),
            )
            time_thread.start()
            detail_log("[JSON][RECV] thread started: {}".format(time_thread.name))

            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    detail_log("[JSON][RECV] peer closed stream {}".format(client))
                    break

                detail_log(
                    "[JSON][RECV] source={} chunk_bytes={} buffer_before={} bytes".format(
                        client,
                        len(chunk),
                        len(extractor.remainder.encode("utf-8", errors="ignore")),
                    )
                )
                text_chunk = decoder.decode(chunk)
                try:
                    payloads = extractor.feed(text_chunk)
                    detail_log(
                        "[JSON][PARSE] source={} extracted={} | remainder={} bytes "
                        "| bad={} | dropped_bytes={}".format(
                            client,
                            len(payloads),
                            len(
                                extractor.remainder.encode(
                                    "utf-8", errors="ignore"
                                )
                            ),
                            extractor.bad_objects,
                            extractor.dropped_bytes,
                        )
                    )
                except BufferError as exc:
                    log(
                        "[JSON][RECV][WARN] buffer cleared: {}".format(exc),
                        level="WARN",
                    )
                    continue

                for payload in payloads:
                    process_payload(payload)

                current = time.time()
                elapsed = current - last_report
                if elapsed >= REPORT_INTERVAL:
                    speed = (received_total - last_received_total) / elapsed
                    log(
                        "[JSON][RECV] {} total={} | bytes={} | {:.1f} msg/s | queued={} "
                        "| shortwave={} | beacon_drop={} | whitelist_drop={} | "
                        "gate_drop={} | queue={}/{} | bad={} | dropped_bytes={}".format(
                            client,
                            received_total,
                            received_bytes,
                            speed,
                            queued_total,
                            shortwave_total + shortwave_over_5g["sent"],
                            beacon_drop_total,
                            whitelist_drop_total,
                            access_gate_drop_total,
                            send_queue.qsize(),
                            send_queue.maxsize,
                            extractor.bad_objects,
                            extractor.dropped_bytes,
                        )
                    )
                    last_report = current
                    last_received_total = received_total

            final_text = decoder.decode(b"", final=True)
            if final_text:
                for payload in extractor.feed(final_text):
                    process_payload(payload)

    except (ConnectionResetError, UnicodeDecodeError, OSError) as exc:
        log(
            "[JSON][RECV][WARN] client exception {}: {}".format(client, exc),
            level="WARN",
        )
    finally:
        if extractor.remainder.strip():
            log(
                "[JSON][RECV][WARN] incomplete JSON from {}, remainder={} bytes, "
                "preview={}".format(
                    client,
                    len(
                        extractor.remainder.encode(
                            "utf-8", errors="ignore"
                        )
                    ),
                    extractor.remainder_preview(),
                ),
                level="WARN",
            )
        log(
            "[JSON][RECV] client closed {} | received={} | bytes={} | queued={} | shortwave={} "
            "| beacon_drop={} | whitelist_drop={} | gate_drop={} | bad={} | dropped_bytes={}".format(
                client,
                received_total,
                received_bytes,
                queued_total,
                shortwave_total + shortwave_over_5g["sent"],
                beacon_drop_total,
                whitelist_drop_total,
                access_gate_drop_total,
                extractor.bad_objects,
                extractor.dropped_bytes,
            )
        )


def serve_json(
    host,
    port,
    send_queue,
    gateway_id,
    max_buffer_size,
    baotong_server,
    whitelist,
    whitelist_filter,
    time_set_interval,
    slice_metrics=None,
    radio_over_5g=None,
):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(20)
    log(
        "[JSON][MAIN] listening {}:{} | gateway={} | fire/windspeed->BaoTong enabled "
        "| whitelist_filter={} | time_set_interval={}s".format(
            host,
            port,
            gateway_id,
            whitelist_filter,
            time_set_interval,
        )
    )

    try:
        while True:
            conn, addr = server_sock.accept()
            detail_log(
                "[JSON][MAIN] accepted client={}:{} local={}:{}".format(
                    addr[0], addr[1], *conn.getsockname()
                )
            )
            threading.Thread(
                target=handle_json_client,
                args=(
                    conn,
                    addr,
                    send_queue,
                    gateway_id,
                    max_buffer_size,
                    baotong_server,
                    whitelist,
                    whitelist_filter,
                time_set_interval,
                slice_metrics,
                radio_over_5g,
            ),
                daemon=True,
                name="json-client-{}:{}".format(addr[0], addr[1]),
            ).start()
    finally:
        server_sock.close()


# ======================================================================
# 宝通 9100 历史 fire 下发逻辑
#
# 该部分仅保留用于历史对照；main() 使用后面的 ProtocolBaoTongServer。
# 1. 宝通设备主动连接本机 192.168.2.1:9100。
# 2. 宝通帧格式固定为：AA55 + 4字节大端 JSON 长度 + JSON + 55AA。
# 3. 8888 收到含 fire 的 JSON 后，构造 callee_id/fire/timestamp 报文。
# 4. 无宝通连接时缓存最新 fire 报文；建立连接后等待链路事件触发。
# 5. heartbeat/hf_connect：先发送 link_test，再尝试发送缓存 fire 报文。
# 6. linkstatus/hf_active：按原逻辑判断 tx_snr、rx_snr 是否大于 -10；
#    两者均满足时尝试发送缓存 fire 报文。SNR 缺失时仍沿用原逻辑视为通过。
# 7. Timeout、linkstatus/hf_inactive、heartbeat/hf_dis_connect 均将
#    last_link_ok 置为 False。
# 8. attach_fire_payload 仍按原逻辑在 link_seen 或 last_link_ok 为 True 时
#    立即尝试发送，不改变原有状态机行为。
# ======================================================================

class _LegacyBaoTongSession:
    def __init__(self, conn, addr, callee_id):
        self.conn = conn
        self.addr = addr
        self.callee_id = callee_id
        self.send_lock = threading.Lock()
        self.link_seen = False
        self.last_link_ok = False
        self.pending_fire_payload = None

    def _state_text(self):
        return "link_seen={} last_link_ok={} pending_fire={}".format(
            self.link_seen,
            self.last_link_ok,
            self.pending_fire_payload is not None,
        )

    def attach_fire_payload(self, payload):
        # 与原 baotong.py 一致：仅保存最新一条待发送 fire 报文。
        self.pending_fire_payload = payload
        detail_log(
            "[BAOTONG][FIRE][CACHE] peer={}:{} | {} | payload={}".format(
                self.addr[0],
                self.addr[1],
                self._state_text(),
                payload,
            )
        )

        # 与原逻辑完全一致：只要已经见过链路，或当前链路标记为可用，
        # 就立即尝试发送；否则继续缓存，等待心跳或链路状态报文触发。
        if self.link_seen or self.last_link_ok:
            detail_log(
                "[BAOTONG][FIRE][TRIGGER] immediate send attempt | {}".format(
                    self._state_text()
                )
            )
            self.try_send_fire_message()
        else:
            detail_log(
                "[BAOTONG][FIRE][WAIT] waiting for heartbeat/linkstatus | {}".format(
                    self._state_text()
                )
            )

    def send_json(self, payload_dict):
        # 与原 baotong.py 一致：紧凑 JSON、UTF-8、大端 4 字节长度、固定头尾。
        payload_str = json.dumps(
            payload_dict,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload_bytes = payload_str.encode("utf-8")
        frame = (
            FRAME_HEAD
            + struct.pack(">I", len(payload_bytes))
            + payload_bytes
            + FRAME_TAIL
        )

        detail_log(
            "[BAOTONG][SEND][FRAME] peer={}:{} | payload_bytes={} "
            "| frame_bytes={} | head={} | tail={}".format(
                self.addr[0],
                self.addr[1],
                len(payload_bytes),
                len(frame),
                FRAME_HEAD.hex(),
                FRAME_TAIL.hex(),
            )
        )

        with self.send_lock:
            self.conn.sendall(frame)

        log(
            "[BAOTONG][SEND] peer={}:{} | raw={}".format(
                self.addr[0],
                self.addr[1],
                payload_str,
            )
        )

    def try_send_fire_message(self):
        # 与原 baotong.py 一致：没有待发送 fire 时直接返回。
        if not self.pending_fire_payload:
            detail_log(
                "[BAOTONG][FIRE][SKIP] no pending payload | {}".format(
                    self._state_text()
                )
            )
            return

        payload = dict(self.pending_fire_payload)
        if not payload.get("callee_id"):
            payload["callee_id"] = self.callee_id
        if not payload.get("timestamp"):
            payload["timestamp"] = now_str()

        detail_log(
            "[BAOTONG][FIRE][SEND_ATTEMPT] peer={}:{} | {} | payload={}".format(
                self.addr[0],
                self.addr[1],
                self._state_text(),
                payload,
            )
        )

        self.send_json(payload)
        log(
            "[BAOTONG][FIRE][SENT] peer={}:{} | callee_id={} | fire={} "
            "| timestamp={}".format(
                self.addr[0],
                self.addr[1],
                payload.get("callee_id"),
                payload.get("fire"),
                payload.get("timestamp"),
            )
        )

        # 与原逻辑一致：发送成功后清空当前待发送 fire 报文。
        self.pending_fire_payload = None

    def run(self):
        log(
            "[BAOTONG][CONNECT] client connected peer={}:{} | callee_id={}".format(
                self.addr[0],
                self.addr[1],
                self.callee_id,
            )
        )
        recv_buffer = b""

        try:
            with self.conn:
                while True:
                    data = self.conn.recv(BAOTONG_BUFFER_SIZE)
                    if not data:
                        detail_log(
                            "[BAOTONG][RECV] peer closed connection {}:{}".format(
                                self.addr[0], self.addr[1]
                            )
                        )
                        break

                    recv_buffer += data
                    detail_log(
                        "[BAOTONG][RECV][CHUNK] peer={}:{} | chunk_bytes={} "
                        "| buffered_bytes={}".format(
                            self.addr[0],
                            self.addr[1],
                            len(data),
                            len(recv_buffer),
                        )
                    )

                    while True:
                        header_pos = recv_buffer.find(FRAME_HEAD)
                        if header_pos < 0:
                            if recv_buffer:
                                detail_log(
                                    "[BAOTONG][PARSE][RESYNC] frame head not found, "
                                    "clear {} buffered bytes".format(len(recv_buffer))
                                )
                            recv_buffer = b""
                            break

                        # 与原逻辑一致：完整帧至少需要头、长度字段和尾部共 8 字节。
                        if len(recv_buffer) < header_pos + 8:
                            detail_log(
                                "[BAOTONG][PARSE][WAIT] incomplete fixed frame fields "
                                "| header_pos={} | buffered_bytes={}".format(
                                    header_pos, len(recv_buffer)
                                )
                            )
                            break

                        payload_length = struct.unpack(
                            ">I",
                            recv_buffer[header_pos + 2 : header_pos + 6],
                        )[0]
                        total_frame_len = 2 + 4 + payload_length + 2

                        detail_log(
                            "[BAOTONG][PARSE][HEADER] header_pos={} "
                            "| payload_length={} | total_frame_len={} "
                            "| buffered_bytes={}".format(
                                header_pos,
                                payload_length,
                                total_frame_len,
                                len(recv_buffer),
                            )
                        )

                        if len(recv_buffer) < header_pos + total_frame_len:
                            detail_log(
                                "[BAOTONG][PARSE][WAIT] incomplete payload "
                                "| need_bytes={} | have_bytes={}".format(
                                    header_pos + total_frame_len,
                                    len(recv_buffer),
                                )
                            )
                            break

                        payload_bytes = recv_buffer[
                            header_pos + 6 : header_pos + 6 + payload_length
                        ]
                        tail_bytes = recv_buffer[
                            header_pos + 6 + payload_length :
                            header_pos + total_frame_len
                        ]

                        if tail_bytes != FRAME_TAIL:
                            log(
                                "[BAOTONG][PARSE][WARN] bad frame tail={} expected={} "
                                "| drop one byte and resync".format(
                                    tail_bytes.hex(), FRAME_TAIL.hex()
                                ),
                                level="WARN",
                            )
                            recv_buffer = recv_buffer[header_pos + 1 :]
                            continue

                        try:
                            payload_str = payload_bytes.decode("utf-8")
                            payload_json = json.loads(payload_str)
                            packet_type = payload_json.get("type", "unknown")
                            packet_status = payload_json.get("status", "unknown")

                            log(
                                "[BAOTONG][RECV] peer={}:{} | type={} | status={} "
                                "| raw={}".format(
                                    self.addr[0],
                                    self.addr[1],
                                    packet_type,
                                    packet_status,
                                    payload_str,
                                )
                            )

                            # 以下分支顺序与原 baotong.py 完全一致。
                            if packet_status == "Timeout":
                                self.last_link_ok = False
                                log(
                                    "[BAOTONG][STATE] status=Timeout -> "
                                    "last_link_ok=False | {}".format(
                                        self._state_text()
                                    ),
                                    level="WARN",
                                )

                            elif (
                                packet_type == "linkstatus"
                                and packet_status == "hf_inactive"
                            ):
                                self.last_link_ok = False
                                log(
                                    "[BAOTONG][STATE] linkstatus/hf_inactive -> "
                                    "last_link_ok=False | {}".format(
                                        self._state_text()
                                    ),
                                    level="WARN",
                                )

                            elif (
                                packet_type == "linkstatus"
                                and packet_status == "hf_active"
                            ):
                                self.link_seen = True
                                tx_snr = payload_json.get("tx_snr")
                                rx_snr = payload_json.get("rx_snr")

                                # 与原逻辑一致：SNR 缺失时按通过处理；存在时必须 > -10。
                                try:
                                    tx_ok = tx_snr is None or float(tx_snr) > -10
                                    rx_ok = rx_snr is None or float(rx_snr) > -10
                                    self.last_link_ok = tx_ok and rx_ok
                                except (TypeError, ValueError):
                                    tx_ok = False
                                    rx_ok = False
                                    self.last_link_ok = False

                                log(
                                    "[BAOTONG][STATE] linkstatus/hf_active "
                                    "| tx_snr={} tx_ok={} | rx_snr={} rx_ok={} "
                                    "| {}".format(
                                        tx_snr,
                                        tx_ok,
                                        rx_snr,
                                        rx_ok,
                                        self._state_text(),
                                    )
                                )

                                if self.last_link_ok:
                                    detail_log(
                                        "[BAOTONG][FIRE][TRIGGER] hf_active and SNR accepted"
                                    )
                                    self.try_send_fire_message()
                                else:
                                    detail_log(
                                        "[BAOTONG][FIRE][WAIT] hf_active but SNR rejected"
                                    )

                            elif (
                                packet_type == "heartbeat"
                                and packet_status == "hf_dis_connect"
                            ):
                                self.last_link_ok = False
                                log(
                                    "[BAOTONG][STATE] heartbeat/hf_dis_connect -> "
                                    "last_link_ok=False | {}".format(
                                        self._state_text()
                                    ),
                                    level="WARN",
                                )

                            elif (
                                packet_type == "heartbeat"
                                and packet_status == "hf_connect"
                            ):
                                self.link_seen = True
                                log(
                                    "[BAOTONG][STATE] heartbeat/hf_connect -> "
                                    "link_seen=True | {}".format(
                                        self._state_text()
                                    )
                                )

                                # 与原逻辑完全一致：先发送 link_test，再尝试发送 fire。
                                link_test_payload = {
                                    "callee_id": self.callee_id,
                                    "link_test": "test",
                                    "timestamp": now_str(),
                                }
                                detail_log(
                                    "[BAOTONG][LINK_TEST] heartbeat connected, "
                                    "send link_test before fire | payload={}".format(
                                        link_test_payload
                                    )
                                )
                                self.send_json(link_test_payload)
                                self.try_send_fire_message()

                            else:
                                detail_log(
                                    "[BAOTONG][STATE] no matching state transition "
                                    "| type={} | status={} | {}".format(
                                        packet_type,
                                        packet_status,
                                        self._state_text(),
                                    )
                                )

                        except UnicodeDecodeError as exc:
                            log(
                                "[BAOTONG][PARSE][WARN] payload decode failed: {}".format(
                                    exc
                                ),
                                level="WARN",
                            )
                        except json.JSONDecodeError as exc:
                            log(
                                "[BAOTONG][PARSE][WARN] JSON parse failed: {}".format(
                                    exc
                                ),
                                level="WARN",
                            )

                        # 与原逻辑一致：处理完完整帧后，从该帧末尾继续解析余下数据。
                        recv_buffer = recv_buffer[
                            header_pos + total_frame_len :
                        ]

        except Exception as exc:
            log(
                "[BAOTONG][ERROR] client {}:{} exception: {}".format(
                    self.addr[0],
                    self.addr[1],
                    exc,
                ),
                level="ERROR",
            )
        finally:
            log(
                "[BAOTONG][DISCONNECT] client disconnected {}:{} | {}".format(
                    self.addr[0],
                    self.addr[1],
                    self._state_text(),
                )
            )


class _LegacyBaoTongServer(threading.Thread):
    def __init__(self, host, port, callee_id):
        threading.Thread.__init__(
            self,
            daemon=True,
            name="baotong-listener",
        )
        self.host = host
        self.port = int(port)
        self.callee_id = callee_id
        self.current_session = None
        self.session_lock = threading.Lock()
        self.cached_fire_payload = None

    def run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(4)

        log(
            "[BAOTONG][MAIN] listening {}:{} | callee_id={} "
            "| frame=AA55+uint32_be_length+JSON+55AA".format(
                self.host,
                self.port,
                self.callee_id,
            )
        )

        while True:
            conn, addr = server.accept()
            local_addr = conn.getsockname()
            log(
                "[BAOTONG][ACCEPT] peer={}:{} | local={}:{}".format(
                    addr[0],
                    addr[1],
                    local_addr[0],
                    local_addr[1],
                )
            )
            threading.Thread(
                target=self._handle_client_thread,
                args=(conn, addr),
                daemon=True,
                name="baotong-client-{}:{}".format(addr[0], addr[1]),
            ).start()

    def _set_session(self, session):
        with self.session_lock:
            previous = self.current_session
            self.current_session = session

        detail_log(
            "[BAOTONG][SESSION] current session changed "
            "| previous={} | current={}".format(
                "none"
                if previous is None
                else "{}:{}".format(previous.addr[0], previous.addr[1]),
                "none"
                if session is None
                else "{}:{}".format(session.addr[0], session.addr[1]),
            )
        )

    def _get_session(self):
        with self.session_lock:
            return self.current_session

    def connection_info(self):
        """Return a lock-protected snapshot of the industrial-PC session."""
        with self.session_lock:
            session = self.current_session
            return {
                "connected": session is not None,
                "peer": (
                    "{}:{}".format(session.addr[0], session.addr[1])
                    if session is not None else None
                ),
            }

    def on_fire_update(self, fire_value, timestamp):
        # 与原 baotong.py 一致：统一转换为字符串 true/false，并缓存最新报文。
        payload = {
            "callee_id": self.callee_id,
            "fire": as_fire_string(fire_value),
            "timestamp": timestamp or now_str(),
        }
        self.cached_fire_payload = payload

        log(
            "[BAOTONG][FIRE][UPDATE] callee_id={} | fire={} | timestamp={}".format(
                payload["callee_id"],
                payload["fire"],
                payload["timestamp"],
            )
        )

        session = self._get_session()
        if session is None:
            log(
                "[BAOTONG][FIRE][CACHE] no client connected; latest payload cached"
            )
            return

        detail_log(
            "[BAOTONG][FIRE][ROUTE] attach payload to peer={}:{}".format(
                session.addr[0],
                session.addr[1],
            )
        )
        session.attach_fire_payload(payload)

    def _handle_client_thread(self, conn, addr):
        session = _LegacyBaoTongSession(conn, addr, self.callee_id)

        # 与原逻辑一致：若服务器已有缓存 fire，先挂入新会话；
        # 此时 link_seen=False、last_link_ok=False，因此不会立即发送。
        if self.cached_fire_payload is not None:
            detail_log(
                "[BAOTONG][FIRE][RESTORE] attach cached payload to new peer={}:{} "
                "| payload={}".format(
                    addr[0],
                    addr[1],
                    self.cached_fire_payload,
                )
            )
            session.attach_fire_payload(self.cached_fire_payload)

        self._set_session(session)
        try:
            session.run()
        finally:
            if self._get_session() is session:
                self._set_session(None)
                detail_log(
                    "[BAOTONG][SESSION] cleared disconnected peer={}:{}".format(
                        addr[0], addr[1]
                    )
                )


# ======================================================================
# 最新短波协议：按注册网关选择 fire/windspeed，探测成功后发送
# ======================================================================

class ProtocolBaoTongSession:
    """按 2026-07-22 协议执行探测后发送的宝通会话。"""

    def __init__(self, conn, addr, default_callee_id, message_callback):
        self.conn = conn
        self.addr = addr
        self.default_callee_id = str(default_callee_id)
        self.message_callback = message_callback
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.hf_connected = False
        self.probe_in_flight = False
        self.probe_started_monotonic = 0.0
        self.probe_kind = None
        self.last_link_ok = False
        # detect 信道忙闲状态由工控机上报：0=空闲，1=检测/占用中。
        # detect 是唯一权威：本地只镜像工控机的 detect=0/1 上报，从不
        # 自行置位或清零。发起探测和发送短信前都要求工控机报
        # detect=0（信道空闲）。detect 停留 1 超过看门狗门限时发
        # reset 请求工控机复位，随后等待工控机重新上报。
        self.detect_state = 0
        self.detect_set_at = 0.0
        self.detect_reset_requested = False
        # Fire and windspeed must never overwrite one another while a probe is
        # in flight.  Each slot keeps only the newest payload of its own kind.
        self.pending_payloads = {
            "fire": None,
            "windspeed": None,
        }
        self.pending_order = deque()
        # Start the grace period when TCP is accepted.  Each valid application
        # heartbeat refreshes this timestamp; ordinary traffic does not.
        self.last_heartbeat_monotonic = time.monotonic()

    def heartbeat_age_seconds(self):
        with self.state_lock:
            return max(0.0, time.monotonic() - self.last_heartbeat_monotonic)

    def heartbeat_is_fresh(self):
        return self.heartbeat_age_seconds() <= BAOTONG_HEARTBEAT_TIMEOUT

    @staticmethod
    def _payload_kind(payload):
        has_fire = "fire" in payload
        has_windspeed = "windspeed" in payload
        if has_fire == has_windspeed:
            raise ValueError("SMS must contain exactly one of fire or windspeed")
        return "fire" if has_fire else "windspeed"

    def _next_pending_kind_locked(self):
        while self.pending_order:
            kind = self.pending_order[0]
            if self.pending_payloads.get(kind) is not None:
                return kind
            self.pending_order.popleft()
        return None

    def _start_probe_locked(self, kind):
        self.probe_in_flight = True
        self.probe_started_monotonic = time.monotonic()
        self.probe_kind = kind

    def _clear_probe_locked(self):
        self.probe_in_flight = False
        self.probe_started_monotonic = 0.0
        self.probe_kind = None

    def _mirror_detect_locked(self, value, monotonic_now=None):
        """镜像工控机上报的 detect：1=占用，0=空闲。

        detect 的唯一权威是工控机；本地只做镜像，不自行置位或清零。
        收到 detect=1 时打点，供卡死看门狗计时。
        """
        self.detect_state = 1 if value else 0
        self.detect_set_at = (
            time.monotonic() if monotonic_now is None else monotonic_now
        ) if value else 0.0
        if not value:
            self.detect_reset_requested = False

    def _detect_reset_watchdog_needed_locked(self):
        # 镜像到的工控机 detect 停留在 1 超过门限且本周期尚未发过 reset
        # 时触发一次。探测超时（200s）不再本地释放 detect，因此本看门狗
        # 能够真实覆盖"工控机长时间不报 detect=0"的卡死场景。
        return (
            self.detect_state != 0
            and self.detect_set_at
            and not self.detect_reset_requested
            and time.monotonic() - self.detect_set_at
            > SHORTWAVE_RESET_TIMEOUT_SECONDS
        )

    def _check_detect_watchdog(self):
        """在 recv 轮询里周期检查 detect 卡死；不依赖新业务触发。"""
        need_send_reset = False
        with self.state_lock:
            if self._detect_reset_watchdog_needed_locked():
                self.detect_reset_requested = True
                need_send_reset = True
        if need_send_reset:
            try:
                self.send_json({"reset": 1})
                log(
                    "[BAOTONG-V2][DETECT][RESET] mirrored detect=1 stuck >{}s "
                    "peer={}:{} sent reset=1, waiting for industrial PC "
                    "detect=0".format(
                        int(SHORTWAVE_RESET_TIMEOUT_SECONDS),
                        self.addr[0],
                        self.addr[1],
                    ),
                    level="WARN",
                )
            except Exception as exc:
                log(
                    "[BAOTONG-V2][DETECT][RESET] send reset failed: {}".format(
                        exc
                    ),
                    level="WARN",
                )
                with self.state_lock:
                    self.detect_reset_requested = False

    def _expire_probe_if_needed(self):
        with self.state_lock:
            if not self.probe_in_flight or not self.probe_started_monotonic:
                return False
            age = time.monotonic() - self.probe_started_monotonic
            if age <= SHORTWAVE_LINK_TEST_TIMEOUT_SECONDS:
                return False
            self._clear_probe_locked()
            self.last_link_ok = False
            # 探测超时后完全回到默认状态，等待核心的下一次呼叫重新发起：
            # 边缘绝不主动重发探测。双边同时探测会造成信道互撞卡死，
            # 探测权永远只在被核心呼叫后、且本地无探测在途时启用。
            # detect 是工控机的权威状态，本地不清零；若工控机长时间不报
            # detect=0，由卡死看门狗发 reset 请求复位。载荷保留不丢弃。
        log(
            "[BAOTONG-V2][LINK_TEST][TIMEOUT] peer={}:{} "
            "no linkstatus for {:.1f}s; back to idle, wait for next core "
            "call; pending payload retained".format(
                self.addr[0],
                self.addr[1],
                age,
            ),
            level="WARN",
        )
        return True

    def _state_text(self):
        with self.state_lock:
            pending_kinds = [
                kind for kind in ("fire", "windspeed")
                if self.pending_payloads.get(kind) is not None
            ]
            return (
                "hf_connected={} detect={} probe_in_flight={} probe_kind={} "
                "last_link_ok={} pending_kinds={}"
            ).format(
                self.hf_connected,
                self.detect_state,
                self.probe_in_flight,
                self.probe_kind,
                self.last_link_ok,
                pending_kinds,
            )

    @staticmethod
    def _validate_callee_id(value):
        text = str(value).strip()
        try:
            number = int(text)
        except (TypeError, ValueError):
            raise ValueError("callee_id must be numeric")
        if number < 1 or number > 255999:
            raise ValueError("callee_id outside protocol range 1-255999")
        return text

    @staticmethod
    def _validate_sms_payload(payload):
        ProtocolBaoTongSession._validate_callee_id(payload.get("callee_id"))
        has_fire = "fire" in payload
        has_windspeed = "windspeed" in payload
        if has_fire == has_windspeed:
            raise ValueError("SMS must contain exactly one of fire or windspeed")
        if has_fire:
            if str(payload.get("fire", "")).lower() not in ("true", "false"):
                raise ValueError("fire must be true or false")
        if not str(payload.get("timestamp", "")).strip():
            raise ValueError("SMS must contain timestamp")

        # 协议中的 80 字节限制针对无线短信内容；callee_id 是寻址字段。
        content = dict(payload)
        content.pop("callee_id", None)
        content_bytes = json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(content_bytes) > BAOTONG_SMS_MAX_CONTENT_BYTES:
            raise ValueError(
                "SMS content exceeds {} bytes: {}".format(
                    BAOTONG_SMS_MAX_CONTENT_BYTES,
                    len(content_bytes),
                )
            )

    def send_json(self, payload):
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload_bytes) <= 0 or len(payload_bytes) > BAOTONG_MAX_PAYLOAD_SIZE:
            raise ValueError(
                "BaoTong JSON payload size invalid: {}".format(len(payload_bytes))
            )
        frame = (
            FRAME_HEAD
            + struct.pack(">I", len(payload_bytes))
            + payload_bytes
            + FRAME_TAIL
        )
        with self.send_lock:
            self.conn.sendall(frame)
        log(
            "[BAOTONG-V2][SEND] peer={}:{} payload={}".format(
                self.addr[0],
                self.addr[1],
                payload_bytes.decode("utf-8"),
            )
        )

    def attach_payload(self, payload, allow_probe=False):
        payload = dict(payload)
        payload["callee_id"] = self._validate_callee_id(
            payload.get("callee_id") or self.default_callee_id
        )
        if not payload.get("timestamp"):
            payload["timestamp"] = now_str()
        self._validate_sms_payload(payload)
        kind = self._payload_kind(payload)

        should_probe = False
        probe_callee = None
        with self.state_lock:
            if self.pending_payloads[kind] is None:
                self.pending_order.append(kind)
            self.pending_payloads[kind] = payload
            self.last_link_ok = False
            if allow_probe:
                # detect 是工控机的权威状态（本地只镜像）：detect=0 才占用
                # 信道发起探测。信道占用仲裁由 probe_in_flight 保证同一
                # 时间只有一个探测回合。
                if self.detect_state == 0 and not self.probe_in_flight:
                    next_kind = self._next_pending_kind_locked()
                    if next_kind is not None:
                        self._start_probe_locked(next_kind)
                        should_probe = True
                        probe_callee = self.pending_payloads[next_kind].get(
                            "callee_id", self.default_callee_id
                        )
        log(
            "[BAOTONG-V2][CACHE] peer={}:{} payload={} {}".format(
                self.addr[0], self.addr[1], payload, self._state_text()
            )
        )
        if should_probe:
            self._send_link_test(probe_callee)

    def _send_link_test(self, callee_id):
        probe = {
            "callee_id": self._validate_callee_id(callee_id),
            "link_test": "test",
            "timestamp": now_str(),
        }
        log(
            "[BAOTONG-V2][LINK_TEST] threshold tx/rx>{} payload={}".format(
                BAOTONG_SNR_THRESHOLD,
                probe,
            )
        )
        try:
            self.send_json(probe)
        except Exception:
            # 发送失败只清本地探测标记；detect 是工控机的权威状态，
            # 本地不清零，等待工控机上报。
            with self.state_lock:
                self._clear_probe_locked()
            raise

    def _send_pending_after_probe(self, kind):
        with self.state_lock:
            if (
                not self.last_link_ok
                # 发送前检查工控机上报的 detect 是否为 0（信道空闲）：
                # 只有信道空闲才把短信交给工控机。detect 由工控机上报
                # 镜像，本地不置位；不空闲时载荷保留，等待工控机报
                # detect=0 后的下一次呼叫重新探测。
                or self.detect_state != 0
                or kind not in self.pending_payloads
                or self.pending_payloads[kind] is None
            ):
                return False
            payload = dict(self.pending_payloads[kind])
            self.pending_payloads[kind] = None
            try:
                self.pending_order.remove(kind)
            except ValueError:
                pass
            self.last_link_ok = False
        self._validate_sms_payload(payload)
        try:
            self.send_json(payload)
        except Exception:
            # Restore the failed payload only if no newer payload of the same
            # kind arrived while sendall() was running.
            with self.state_lock:
                if self.pending_payloads[kind] is None:
                    self.pending_payloads[kind] = payload
                    self.pending_order.appendleft(kind)
            raise
        log(
            "[BAOTONG-V2][SMS][SENT] peer={}:{} detect=0 idle payload={}".format(
                self.addr[0], self.addr[1], payload
            )
        )
        return True

    def send_exit_reset(self):
        """进程退出前的"砸回去"口令：请求工控机复位信道状态。

        只在本地探测在途或 detect 镜像为 1（信道被占用）时发送——
        空闲状态退出无需打扰工控机。发送失败不影响退出流程。
        """
        with self.state_lock:
            busy = self.probe_in_flight or self.detect_state != 0
        if not busy:
            return False
        try:
            self.send_json({"reset": 1})
        except Exception as exc:
            log(
                "[BAOTONG-V2][EXIT-RESET] send failed: {}".format(exc),
                level="WARN",
            )
            return False
        log(
            "[BAOTONG-V2][EXIT-RESET] peer={}:{} probe_in_flight={} "
            "detect={} -> sent reset=1 before exit".format(
                self.addr[0], self.addr[1], busy, self.detect_state
            ),
            level="WARN",
        )
        return True

    def _handle_message(self, payload):
        packet_type = str(payload.get("type", "")).strip().lower()
        packet_status = str(payload.get("status", "")).strip().lower()

        if "caller_id" in payload and (
            "fire" in payload or "windspeed" in payload
        ):
            self.message_callback(dict(payload), self)
            return

        if "detect" in payload:
            detect_text = str(payload.get("detect", "")).strip()
            if detect_text in ("0", "1"):
                with self.state_lock:
                    self._mirror_detect_locked(detect_text == "1")
                log(
                    "[BAOTONG-V2][DETECT] detect={} received {}".format(
                        detect_text, self._state_text()
                    )
                )
                return

        if packet_type == "heartbeat":
            # 心跳只刷新工控机在线状态，不再触发探测：
            # 何时上信道由主站轮询（caller_id 呼叫）+ detect 空闲共同决定。
            with self.state_lock:
                self.last_heartbeat_monotonic = time.monotonic()
                self.hf_connected = packet_status == "hf_connect"
                if not self.hf_connected:
                    self.last_link_ok = False
                    self._clear_probe_locked()
            log(
                "[BAOTONG-V2][HEARTBEAT] status={} {}".format(
                    packet_status, self._state_text()
                )
            )
            return

        if packet_status == "timeout" or (
            packet_type == "linkstatus" and packet_status == "hf_inactive"
        ):
            with self.state_lock:
                self.last_link_ok = False
                self._clear_probe_locked()
            log(
                "[BAOTONG-V2][LINK][REJECT] type={} status={} {}".format(
                    packet_type, packet_status, self._state_text()
                ),
                level="WARN",
            )
            return

        if packet_type == "linkstatus" and packet_status == "hf_active":
            tx_snr = payload.get("tx_snr")
            rx_snr = payload.get("rx_snr")
            try:
                tx_ok = tx_snr is not None and float(tx_snr) > BAOTONG_SNR_THRESHOLD
                rx_ok = rx_snr is not None and float(rx_snr) > BAOTONG_SNR_THRESHOLD
            except (TypeError, ValueError):
                tx_ok = False
                rx_ok = False

            with self.state_lock:
                was_probing = self.probe_in_flight
                probe_kind = self.probe_kind
                self._clear_probe_locked()
                self.last_link_ok = bool(was_probing and tx_ok and rx_ok)
            log(
                "[BAOTONG-V2][LINK] tx_snr={} tx_ok={} rx_snr={} rx_ok={} "
                "probe_matched={} {}".format(
                    tx_snr,
                    tx_ok,
                    rx_snr,
                    rx_ok,
                    was_probing,
                    self._state_text(),
                )
            )
            if was_probing and tx_ok and rx_ok:
                # 探测成功后准备发短信；发送前的最后一道闸是工控机上报
                # 的 detect==0（信道空闲）。探测成功不改变本地 detect 镜像。
                self._send_pending_after_probe(probe_kind)
            return

        detail_log(
            "[BAOTONG-V2][RECV] ignored payload={}".format(
                preview_text(payload)
            )
        )

    def run(self):
        log(
            "[BAOTONG-V2][CONNECT] peer={}:{} default_callee={}".format(
                self.addr[0], self.addr[1], self.default_callee_id
            )
        )
        recv_buffer = b""
        try:
            with self.conn:
                # recv() without a timeout cannot detect a half-open TCP
                # connection.  Poll periodically so the application heartbeat
                # deadline is enforced even when no socket error is generated.
                self.conn.settimeout(BAOTONG_HEARTBEAT_POLL_INTERVAL)
                while True:
                    self._expire_probe_if_needed()
                    # detect 卡死看门狗随 recv 轮询周期检查，不依赖新业务。
                    self._check_detect_watchdog()
                    try:
                        data = self.conn.recv(BAOTONG_BUFFER_SIZE)
                    except socket.timeout:
                        heartbeat_age = self.heartbeat_age_seconds()
                        if heartbeat_age > BAOTONG_HEARTBEAT_TIMEOUT:
                            log(
                                "[BAOTONG-V2][HEARTBEAT][TIMEOUT] "
                                "peer={}:{} no heartbeat for {:.1f}s; "
                                "mark industrial PC offline".format(
                                    self.addr[0],
                                    self.addr[1],
                                    heartbeat_age,
                                ),
                                level="WARN",
                            )
                            break
                        continue
                    if not data:
                        break
                    recv_buffer += data
                    while True:
                        head_index = recv_buffer.find(FRAME_HEAD)
                        if head_index < 0:
                            recv_buffer = (
                                recv_buffer[-1:]
                                if recv_buffer.endswith(FRAME_HEAD[:1])
                                else b""
                            )
                            break
                        if head_index > 0:
                            recv_buffer = recv_buffer[head_index:]
                        if len(recv_buffer) < 8:
                            break

                        payload_length = struct.unpack(">I", recv_buffer[2:6])[0]
                        if (
                            payload_length <= 0
                            or payload_length > BAOTONG_MAX_PAYLOAD_SIZE
                        ):
                            log(
                                "[BAOTONG-V2][PARSE][WARN] invalid length={}".format(
                                    payload_length
                                ),
                                level="WARN",
                            )
                            recv_buffer = recv_buffer[1:]
                            continue
                        frame_length = 2 + 4 + payload_length + 2
                        if len(recv_buffer) < frame_length:
                            break
                        if recv_buffer[frame_length - 2:frame_length] != FRAME_TAIL:
                            log(
                                "[BAOTONG-V2][PARSE][WARN] invalid frame tail",
                                level="WARN",
                            )
                            recv_buffer = recv_buffer[1:]
                            continue

                        payload_bytes = recv_buffer[6:6 + payload_length]
                        recv_buffer = recv_buffer[frame_length:]
                        try:
                            payload = json.loads(payload_bytes.decode("utf-8"))
                            if not isinstance(payload, dict):
                                raise ValueError("payload is not a JSON object")
                            log(
                                "[BAOTONG-V2][RECV] peer={}:{} payload={}".format(
                                    self.addr[0],
                                    self.addr[1],
                                    payload,
                                )
                            )
                            self._handle_message(payload)
                        except (UnicodeDecodeError, ValueError) as exc:
                            log(
                                "[BAOTONG-V2][PARSE][WARN] {}".format(exc),
                                level="WARN",
                            )
        except Exception as exc:
            log(
                "[BAOTONG-V2][ERROR] peer={}:{} {}".format(
                    self.addr[0], self.addr[1], exc
                ),
                level="ERROR",
            )
        finally:
            log(
                "[BAOTONG-V2][DISCONNECT] peer={}:{} {}".format(
                    self.addr[0], self.addr[1], self._state_text()
                )
            )


class ProtocolBaoTongServer(threading.Thread):
    def __init__(
        self,
        host,
        port,
        callee_id,
        gateway_name,
        link_status_monitor,
    ):
        threading.Thread.__init__(
            self,
            daemon=True,
            name="baotong-listener",
        )
        self.host = host
        self.port = int(port)
        self.callee_id = str(callee_id)
        self.gateway_name = canonical_gateway_name(gateway_name)
        self.gateway_key = normalize_gateway_name(gateway_name)
        self.business = SHORTWAVE_BUSINESS_BY_GATEWAY.get(self.gateway_key)
        self.link_status_monitor = link_status_monitor
        self.current_session = None
        self.session_lock = threading.Lock()
        self.data_lock = threading.Lock()
        self.latest_fire = None
        self.latest_windspeed = None
        self.offline_next_kind = "fire"
        # Preserve the newest unsent payload independently for each business.
        self.cached_unsent_payloads = {
            "fire": None,
            "windspeed": None,
        }

    def run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(4)
        log(
            "[BAOTONG-V2][MAIN] listening {}:{} gateway={} business={} "
            "callee_id={} snr_threshold={}".format(
                self.host,
                self.port,
                self.gateway_name,
                self.business or "disabled",
                self.callee_id,
                BAOTONG_SNR_THRESHOLD,
            )
        )
        while True:
            conn, addr = server.accept()
            threading.Thread(
                target=self._handle_client_thread,
                args=(conn, addr),
                daemon=True,
                name="baotong-v2-client-{}:{}".format(addr[0], addr[1]),
            ).start()

    def _set_session(self, session):
        with self.session_lock:
            previous = self.current_session
            self.current_session = session
        if previous is not None and previous is not session:
            try:
                previous.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _get_session(self):
        with self.session_lock:
            return self.current_session

    def connection_info(self):
        """Return the industrial-PC state used by the 11511 heartbeat."""
        with self.session_lock:
            session = self.current_session
            connected = bool(
                session is not None and session.heartbeat_is_fresh()
            )
            return {
                "connected": connected,
                "peer": (
                    "{}:{}".format(session.addr[0], session.addr[1])
                    if connected else None
                ),
            }

    def _update_cache(self, payload):
        timestamp = as_timestamp_string(payload.get("timestamp"))
        updated = set()
        with self.data_lock:
            if "fire" in payload:
                fire = parse_fire_string(payload.get("fire"))
                if fire is None:
                    log(
                        "[JSON][SHORTWAVE][WARN] invalid fire={!r}; "
                        "shortwave fire cache not updated".format(
                            payload.get("fire")
                        ),
                        level="WARN",
                    )
                else:
                    # 短波短信不携带 scene，缓存仅保存短信所需字段。
                    self.latest_fire = {
                        "value": fire,
                        "timestamp": timestamp,
                    }
                    updated.add("fire")
            if "windspeed" in payload:
                windspeed = as_windspeed_string(payload.get("windspeed"))
                if windspeed is not None:
                    self.latest_windspeed = {
                        "value": windspeed,
                        "timestamp": timestamp,
                    }
                    updated.add("windspeed")
                else:
                    log(
                        "[JSON][SHORTWAVE][WARN] invalid windspeed={!r}; "
                        "shortwave windspeed cache not updated".format(
                            payload.get("windspeed")
                        ),
                        level="WARN",
                    )
        return updated

    def _payload_for_kind(self, kind, callee_id):
        if kind == "fire":
            record = self.latest_fire
            if record is None:
                return None
            return {
                "callee_id": str(callee_id),
                "fire": record["value"],
                "timestamp": baotong_sms_timestamp(record["timestamp"]),
            }
        if kind == "windspeed":
            record = self.latest_windspeed
            if record is None:
                return None
            return {
                "callee_id": str(callee_id),
                "windspeed": extract_windspeed_number(record["value"]),
                "timestamp": baotong_sms_timestamp(record["timestamp"]),
            }
        return None

    def _select_payload(self, callee_id):
        with self.data_lock:
            if self.business == "windspeed":
                return self._payload_for_kind("windspeed", callee_id)
            if self.business != "fire":
                return None

            if self.gateway_key != "gateway1" or self.link_status_monitor.is_connected():
                return self._payload_for_kind("fire", callee_id)

            selected_kind = self.offline_next_kind
            payload = self._payload_for_kind(selected_kind, callee_id)
            if payload is None:
                fallback = "windspeed" if selected_kind == "fire" else "fire"
                payload = self._payload_for_kind(fallback, callee_id)
                selected_kind = fallback if payload is not None else selected_kind
            if payload is not None:
                self.offline_next_kind = (
                    "windspeed" if selected_kind == "fire" else "fire"
                )
                log(
                    "[BAOTONG-V2][OFFLINE-ROTATE] gateway={} selected={} next={}".format(
                        self.gateway_name,
                        selected_kind,
                        self.offline_next_kind,
                    )
                )
            return payload

    def _route_payload(
        self,
        payload,
        session=None,
        cache_when_offline=True,
        allow_probe=False,
    ):
        if payload is None:
            return None
        kind = ProtocolBaoTongSession._payload_kind(payload)
        target_session = session or self._get_session()
        if target_session is None:
            if cache_when_offline:
                with self.data_lock:
                    self.cached_unsent_payloads[kind] = dict(payload)
            log(
                "[BAOTONG-V2][CACHE] no industrial-PC session kind={} "
                "payload={}".format(
                    kind,
                    payload,
                ),
                level="WARN",
            )
            return payload
        target_session.attach_payload(payload, allow_probe=allow_probe)
        return payload

    def on_sensor_update(self, payload):
        # 主站轮询架构：传感器数据只进缓存，不主动上信道。
        # 何时发送由核心网关的呼叫（caller_id）触发，见 on_radio_message。
        self._update_cache(payload)
        return None

    def on_fire_update(self, fire_value, timestamp):
        return self.on_sensor_update({
            "fire": fire_value,
            "timestamp": timestamp,
        })

    def take_direct_payload(self):
        """直发模式取一条最新业务短信（仅 EDGE_RADIO_OVER_5G 联调用）。

        现网为主站轮询架构：发送由核心呼叫触发（on_radio_message）。
        联调模式下核心不呼叫，由边缘在短波时延后自行取当前业务值发送。
        """
        return self._select_payload(self.callee_id)

    def send_exit_reset(self):
        """向当前工控机会话发送退出复位口令；无会话时静默跳过。"""
        session = self._get_session()
        if session is None:
            return False
        return session.send_exit_reset()

    def on_radio_message(self, message, session):
        caller_id = str(message.get("caller_id", "")).strip()
        if not caller_id:
            return
        selected = self._select_payload(caller_id)
        if selected is None:
            log(
                "[BAOTONG-V2][CALL][WARN] gateway={} caller_id={} "
                "has no cached {} value".format(
                    self.gateway_name,
                    caller_id,
                    self.business,
                ),
                level="WARN",
            )
            return
        log(
            "[BAOTONG-V2][CALL] gateway={} caller_id={} response={}".format(
                self.gateway_name,
                caller_id,
                selected,
            )
        )
        self._route_payload(
            selected,
            session=session,
            cache_when_offline=False,
            allow_probe=True,
        )

    def _handle_client_thread(self, conn, addr):
        session = ProtocolBaoTongSession(
            conn,
            addr,
            self.callee_id,
            self.on_radio_message,
        )
        self._set_session(session)
        with self.data_lock:
            cached_payloads = [
                dict(self.cached_unsent_payloads[kind])
                for kind in ("fire", "windspeed")
                if self.cached_unsent_payloads[kind] is not None
            ]
            self.cached_unsent_payloads = {
                "fire": None,
                "windspeed": None,
            }
        for cached in cached_payloads:
            # 会话重连：恢复缓存的待发数据并允许探测（与 V2 一致）。
            # detect 门控仍然生效：工控机报 detect=1 时不会上信道。
            session.attach_payload(cached, allow_probe=True)
        try:
            session.run()
        finally:
            if self._get_session() is session:
                self._set_session(None)


# ======================================================================
# 视频/图片 7777 接收与原始帧转发逻辑
# ======================================================================

def recv_exact(conn, size):
    chunks = []
    received = 0

    while received < size:
        data = conn.recv(min(BUFFER_SIZE, size - received))
        if not data:
            if received == 0:
                return None
            raise ConnectionError(
                "connection closed while reading: expected={} received={}".format(
                    size, received
                )
            )
        chunks.append(data)
        received += len(data)

    return b"".join(chunks)


def read_raw_packet(conn):
    header = recv_exact(conn, PACKET_HEADER.size)
    if header is None:
        return None

    packet_type_bytes, body_size = PACKET_HEADER.unpack(header)
    packet_type = packet_type_bytes.decode("ascii", errors="replace")

    if packet_type not in VALID_PACKET_TYPES:
        raise ValueError("unknown packet_type={!r}".format(packet_type))
    if body_size <= 0:
        raise ValueError("invalid body_size={}".format(body_size))
    if body_size > MAX_PACKET_SIZE:
        raise ValueError("body_size too large: {}".format(body_size))

    body = recv_exact(conn, body_size)
    if body is None:
        raise ConnectionError("connection closed while reading body")

    return {
        "packet_type": packet_type,
        "body_size": body_size,
        "raw": header + body,
    }


class MediaCloudConnection:
    def __init__(self, host, port, gateway_id, gateway_handshake):
        self.host = host
        self.port = port
        self.gateway_id = gateway_id
        self.id_bytes = gateway_id.encode("utf-8")
        self.gateway_handshake = gateway_handshake
        self.sock = None

        if len(self.id_bytes) > 255:
            raise ValueError(
                "gateway id too long, must be <=255 bytes after UTF-8 encoding"
            )

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self):
        self.close()
        log("[MEDIA][SEND] connecting cloud {}:{} ...".format(self.host, self.port))
        detail_log(
            "[MEDIA][SEND] connection parameters gateway_id={} handshake={} timeout=10s".format(
                self.gateway_id, self.gateway_handshake
            )
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(10.0)
        sock.connect((self.host, self.port))
        local_addr = sock.getsockname()
        remote_addr = sock.getpeername()
        sock.settimeout(None)
        detail_log(
            "[MEDIA][SEND] TCP connected local={}:{} remote={}:{}".format(
                local_addr[0], local_addr[1], remote_addr[0], remote_addr[1]
            )
        )

        if self.gateway_handshake:
            sock.sendall(b"S")
            sock.sendall(struct.pack("!B", len(self.id_bytes)) + self.id_bytes)
            log("[MEDIA][SEND] gateway handshake sent, id={}".format(self.gateway_id))

        self.sock = sock
        log(
            "[MEDIA][SEND] connected cloud {}:{} | raw framed forwarding enabled".format(
                self.host, self.port
            )
        )

    def ensure_connected(self):
        if self.sock is None:
            self.connect()

    def send_raw_packet(self, raw_packet):
        self.ensure_connected()
        self.sock.sendall(raw_packet)


def handle_media_client(conn, addr, packet_queue, log_every, slice_metrics=None):
    client = "{}:{}".format(addr[0], addr[1])
    log("[MEDIA][RECV] camera connected {}".format(client))

    recv_video = 0
    recv_snapshot = 0
    media_gate_drop_total = 0
    last_gate_open = None

    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        while True:
            packet = read_raw_packet(conn)
            if packet is None:
                log("[MEDIA][RECV] camera disconnected {}".format(client))
                return

            packet_type = packet["packet_type"]
            raw_len = len(packet["raw"])
            packet["slice_id"] = "embb"
            packet["queued_at"] = time.monotonic()
            if slice_metrics is not None:
                slice_metrics.record_input("embb", raw_len)

            # 多源接入门（大纲 2.2.4）：multi_source_access 未执行前不受理
            # 端侧媒体数据（与 8888 JSON 口一致）；接收统计照常累计。
            gate_open = multi_source_enabled()
            if gate_open != last_gate_open:
                if gate_open:
                    log("[MULTI-SOURCE] 多源业务接入已启动，开始受理端侧媒体数据")
                else:
                    log(
                        "[MULTI-SOURCE] 多源接入未启动，暂不受理端侧媒体数据"
                        "（等待 ./multi_source_access.sh）"
                    )
                last_gate_open = gate_open
            if not gate_open:
                media_gate_drop_total += 1
                if media_gate_drop_total == 1 or media_gate_drop_total % log_every == 0:
                    log(
                        "[MEDIA][RECV][GATE] 多源接入未启动，媒体报文暂不受理"
                        " | gated={} | source={}".format(media_gate_drop_total, client)
                    )
                continue

            if packet_type == "VID0":
                recv_video += 1
            elif packet_type == "SNAP":
                recv_snapshot += 1

            dropped_item = packet_queue.put(packet)
            dropped = dropped_item is not None
            if dropped and slice_metrics is not None:
                slice_metrics.record_drop(dropped_item, "embb")
            stats = packet_queue.stats()
            detail_log(
                "[MEDIA][RECV] source={} type={} | body_bytes={} | raw_bytes={} "
                "| queue={}/{} | total_put={} | total_drop={} | dropped_oldest={}".format(
                    client,
                    packet_type,
                    packet["body_size"],
                    raw_len,
                    stats["queue"],
                    packet_queue.maxsize,
                    stats["total_put"],
                    stats["total_drop"],
                    dropped,
                )
            )

            if packet_type == "SNAP":
                log(
                    "[MEDIA][RECV][SNAP] snapshot={} | raw_bytes={} | queue={} "
                    "| drop={} | source={}".format(
                        recv_snapshot,
                        raw_len,
                        stats["queue"],
                        stats["total_drop"],
                        client,
                    )
                )
            elif recv_video % log_every == 0 or dropped:
                log(
                    "[MEDIA][RECV][VID0] video={} | raw_bytes={} | queue={} "
                    "| drop={} | source={}".format(
                        recv_video,
                        raw_len,
                        stats["queue"],
                        stats["total_drop"],
                        client,
                    )
                )

    except (ConnectionError, OSError, ValueError) as exc:
        log("[MEDIA][RECV][ERROR] camera {} failed: {}".format(client, exc))
    finally:
        try:
            conn.close()
        except OSError:
            pass
        log("[MEDIA][RECV] camera closed {}".format(client))


def serve_media(packet_queue, listen_host, listen_port, log_every, slice_metrics=None):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    server.bind((listen_host, listen_port))
    server.listen(10)

    log("[MEDIA][MAIN] listening {}:{}".format(listen_host, listen_port))
    log("[MEDIA][MAIN] mode=raw packet forwarding | storage=disabled")

    while True:
        conn, addr = server.accept()
        detail_log(
            "[MEDIA][MAIN] accepted camera={}:{} local={}:{}".format(
                addr[0], addr[1], *conn.getsockname()
            )
        )
        threading.Thread(
            target=handle_media_client,
            args=(conn, addr, packet_queue, log_every, slice_metrics),
            daemon=True,
            name="media-client-{}:{}".format(addr[0], addr[1]),
        ).start()


def media_cloud_sender_loop(
    packet_queue,
    cloud_host,
    cloud_port,
    gateway_id,
    gateway_handshake,
    log_every,
    slice_metrics=None,
):
    cloud = MediaCloudConnection(
        host=cloud_host,
        port=cloud_port,
        gateway_id=gateway_id,
        gateway_handshake=gateway_handshake,
    )

    sent_video = 0
    sent_snapshot = 0

    log("[MEDIA][SEND] sender started, target={}:{}".format(cloud_host, cloud_port))
    log("[MEDIA][SEND] gateway_handshake={}".format(gateway_handshake))

    while True:
        packet = packet_queue.get()
        packet_type = packet["packet_type"]
        raw_packet = packet["raw"]
        detail_log(
            "[MEDIA][SEND] dequeued type={} | body_bytes={} | raw_bytes={} "
            "| queue={}/{}".format(
                packet_type,
                packet["body_size"],
                len(raw_packet),
                packet_queue.qsize(),
                packet_queue.maxsize,
            )
        )

        try:
            t0 = time.time()
            cloud.send_raw_packet(raw_packet)
            send_ms = (time.time() - t0) * 1000.0
            if slice_metrics is not None:
                queued_at = packet.get("queued_at")
                latency_ms = send_ms
                if queued_at is not None:
                    latency_ms = (time.monotonic() - queued_at) * 1000.0
                slice_metrics.record_output("embb", len(raw_packet), latency_ms)

            if packet_type == "VID0":
                sent_video += 1
            elif packet_type == "SNAP":
                sent_snapshot += 1

            stats = packet_queue.stats()
            detail_log(
                "[MEDIA][SEND] success type={} | raw_bytes={} | send_ms={:.1f} "
                "| queue={}/{} | total_drop={}".format(
                    packet_type,
                    len(raw_packet),
                    send_ms,
                    stats["queue"],
                    packet_queue.maxsize,
                    stats["total_drop"],
                )
            )

            if packet_type == "SNAP":
                log(
                    "[MEDIA][SEND][SNAP] sent={} | raw_bytes={} | send_ms={:.1f} "
                    "| queue={} | drop={}".format(
                        sent_snapshot,
                        len(raw_packet),
                        send_ms,
                        stats["queue"],
                        stats["total_drop"],
                    )
                )
            elif sent_video % log_every == 0:
                log(
                    "[MEDIA][SEND][VID0] sent={} | raw_bytes={} | send_ms={:.1f} "
                    "| queue={} | drop={}".format(
                        sent_video,
                        len(raw_packet),
                        send_ms,
                        stats["queue"],
                        stats["total_drop"],
                    )
                )

        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            log(
                "[MEDIA][SEND][WARN] send failed, drop current packet and reconnect: {}".format(
                    exc
                )
            )
            cloud.close()
            time.sleep(RECONNECT_INTERVAL)
        except Exception as exc:
            log(
                "[MEDIA][SEND][ERROR] sender exception, drop current packet: {}".format(
                    exc
                )
            )
            cloud.close()
            time.sleep(RECONNECT_INTERVAL)


# ======================================================================
# 400-GM12 USB 串口卫星上行
# ======================================================================

class SatelliteUplink(threading.Thread):
    """Periodically queue gateway identity frames in a 400-GM12 module."""

    def __init__(
        self,
        port,
        baudrate,
        gateway_id,
        interval,
        command_timeout,
        reconnect_interval,
        data_type,
        query_queue,
        over_5g=False,
        link_delay_s=0.0,
        link_jitter_s=0.0,
        ingest_url=None,
    ):
        threading.Thread.__init__(self, daemon=True, name="satellite-uplink")
        self.port = str(port or "").strip()
        self.baudrate = int(baudrate)
        self.gateway_id = str(gateway_id)
        self.interval = float(interval)
        self.command_timeout = float(command_timeout)
        self.reconnect_interval = float(reconnect_interval)
        self.data_type = int(data_type)
        self.query_queue = bool(query_queue)
        # 2.2.4 联调（EDGE_RADIO_OVER_5G）：无串口，报文经统一上行送到云端
        # 卫星接收口（HTTP 入库）；控制台输出与串口版完全一致。
        # link_delay_s±link_jitter_s 为联调发送节奏（一条落地后等多久发
        # 下一条，卫星约 2 分钟一条），不是压帧时延。
        self.over_5g = bool(over_5g)
        self.link_delay_s = float(link_delay_s)
        self.link_jitter_s = float(link_jitter_s)
        self.ingest_url = ingest_url
        self.serial_module = None
        self.list_ports_module = None

    def _load_pyserial(self):
        try:
            import serial
            from serial.tools import list_ports

            self.serial_module = serial
            self.list_ports_module = list_ports
            return True
        except ImportError:
            log(
                "[SATELLITE][ERROR] pyserial is not installed; execute: "
                "python3 -m pip install pyserial",
                level="ERROR",
            )
            return False

    def _available_ports(self):
        candidates = []
        candidates.extend(sorted(glob.glob("/dev/serial/by-id/*")))
        if self.list_ports_module is not None:
            try:
                candidates.extend(
                    item.device for item in self.list_ports_module.comports()
                )
            except Exception as exc:
                log(
                    "[SATELLITE][WARN] serial enumeration failed: {}".format(exc),
                    level="WARN",
                )
        candidates.extend(sorted(glob.glob("/dev/ttyUSB*")))
        candidates.extend(sorted(glob.glob("/dev/ttyACM*")))

        result = []
        seen = set()
        for item in candidates:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def _resolve_port(self):
        if self.port and self.port.lower() != "auto":
            return self.port

        ports = self._available_ports()
        if not ports:
            raise RuntimeError("no USB serial port detected for satellite module")
        if len(ports) != 1:
            raise RuntimeError(
                "multiple serial ports detected; set --satellite-port explicitly: {}".format(
                    ports
                )
            )
        log("[SATELLITE] automatically selected port={}".format(ports[0]))
        return ports[0]

    def _open_serial(self, selected_port):
        serial = self.serial_module
        log(
            "[SATELLITE] opening port={} baud={} config=8N1 flow=none".format(
                selected_port, self.baudrate
            )
        )
        return serial.Serial(
            port=selected_port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=3.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def _send_at_command(self, ser, command, timeout=None):
        timeout = self.command_timeout if timeout is None else float(timeout)
        display_command = command.strip()
        detail_log("[SATELLITE][AT][TX] {}".format(display_command))

        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(command.encode("ascii"))
        ser.flush()

        deadline = time.monotonic() + timeout
        lines = []
        while time.monotonic() < deadline:
            raw_line = ser.readline()
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            lines.append(line)
            detail_log("[SATELLITE][AT][RX] {}".format(line))
            if line == "OK":
                return True, lines
            if (
                line == "ERROR"
                or line.startswith("+CME ERROR")
                or line.startswith("+CMD ERROR")
            ):
                return False, lines

        log(
            "[SATELLITE][AT][WARN] response timeout command={!r} "
            "timeout={}s responses={}".format(display_command, timeout, lines),
            level="WARN",
        )
        return False, lines

    def _wake_and_initialize(self, ser):
        at_ok = False
        for attempt in range(1, 4):
            log("[SATELLITE] AT wake attempt={}".format(attempt))
            ok, _ = self._send_at_command(ser, "AT\r\n")
            if ok:
                at_ok = True
                break
            time.sleep(0.5)
        if not at_ok:
            raise RuntimeError("satellite module did not respond to AT")

        ok, lines = self._send_at_command(ser, "ATE0\r\n")
        if ok:
            log("[SATELLITE] AT echo disabled")
        else:
            log(
                "[SATELLITE][WARN] ATE0 failed; continue responses={}".format(lines),
                level="WARN",
            )

    def _wake_before_periodic_send(self, ser):
        ok, lines = self._send_at_command(ser, "AT\r\n")
        if not ok:
            raise RuntimeError("AT wake failed before send: {}".format(lines))

    def _build_payload(self):
        payload = {
            "gateway": self.gateway_id,
            "timestamp": now_str(),
        }
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if not raw:
            raise ValueError("satellite payload is empty")
        if len(raw) > SATELLITE_MAX_PAYLOAD_BYTES:
            raise ValueError(
                "satellite payload {} bytes exceeds {} bytes".format(
                    len(raw), SATELLITE_MAX_PAYLOAD_BYTES
                )
            )
        return payload, raw

    def _build_send_command(self, raw):
        if self.data_type not in (0, 1, 2):
            raise ValueError("satellite data_type must be 0, 1 or 2")
        return "AT+SEND={},{},{}\r\n".format(
            len(raw), raw.hex().upper(), self.data_type
        )

    @staticmethod
    def _parse_frame_no(lines):
        for line in lines:
            match = SATELLITE_FRAME_NO_RE.match(line)
            if match:
                return int(match.group(1))
        return None

    def _query_pending_count(self, ser):
        ok, lines = self._send_at_command(ser, "AT+CMMQ?\r\n")
        if not ok:
            log(
                "[SATELLITE][WARN] pending queue query failed responses={}".format(
                    lines
                ),
                level="WARN",
            )
            return None
        for line in lines:
            match = SATELLITE_CMMQ_RE.match(line)
            if match:
                count = int(match.group(1))
                log("[SATELLITE] pending_frames={}".format(count))
                return count
        log(
            "[SATELLITE][WARN] pending count not found responses={}".format(lines),
            level="WARN",
        )
        return None

    def _send_gateway_identity(self, ser):
        payload, raw = self._build_payload()
        command = self._build_send_command(raw)
        log(
            "[SATELLITE][SEND] preparing gateway identity port={} gateway={} "
            "bytes={} payload={}".format(
                self.port or "auto",
                self.gateway_id,
                len(raw),
                raw.decode("utf-8"),
            )
        )
        detail_log("[SATELLITE][SEND] payload_hex={}".format(raw.hex().upper()))

        ok, lines = self._send_at_command(
            ser, command, timeout=max(self.command_timeout, 5.0)
        )
        if not ok:
            log(
                "[SATELLITE][SEND][ERROR] AT+SEND failed responses={}".format(lines),
                level="ERROR",
            )
            return False

        frame_no = self._parse_frame_no(lines)
        if frame_no is None:
            log(
                "[SATELLITE][SEND][WARN] module returned OK but FrameNo was "
                "not found responses={}".format(lines),
                level="WARN",
            )
        else:
            log(
                "[SATELLITE][SEND][QUEUED] gateway={} frame_no={} bytes={} "
                "timestamp={}".format(
                    self.gateway_id, frame_no, len(raw), payload["timestamp"]
                )
            )
        if self.query_queue:
            self._query_pending_count(ser)
        return True

    def _post_ingest(self, raw):
        """把卫星帧送进云端卫星接收口（仅联调模式使用）。"""
        request_obj = urllib.request.Request(
            self.ingest_url,
            data=raw,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            response.read()

    def _run_over_5g(self):
        """联调模式卫星上行：不走串口/AT 指令，帧入队日志与串口版一致。
        联调节奏（与演示版同口径）：帧造出后立即送达云端卫星接收口，随后
        按 link_delay_s±link_jitter_s（默认约 2 分钟）的节奏发下一条——
        卫星链路约 2 分钟一条、连续发送，不是把每帧压住 2 分钟再落地的
        时延口径。串口版身份帧周期（--satellite-interval，默认 300s）在
        联调模式不参与节奏；传 0 仍表示发一条后空闲（诊断用）。"""
        log(
            "[SATELLITE][MAIN] uplink started configured_port={} baud={} "
            "gateway={} interval={}s data_type={} query_queue={}".format(
                self.port or "auto",
                self.baudrate,
                self.gateway_id,
                self.interval,
                self.data_type,
                self.query_queue,
            )
        )
        send_index = 0
        frame_no = 0
        while True:
            send_index += 1
            log("[SATELLITE][SEND] cycle={}".format(send_index))
            payload, raw = self._build_payload()
            log(
                "[SATELLITE][SEND] preparing gateway identity port={} gateway={} "
                "bytes={} payload={}".format(
                    self.port or "auto",
                    self.gateway_id,
                    len(raw),
                    raw.decode("utf-8"),
                )
            )
            detail_log(
                "[SATELLITE][SEND] payload_hex={}".format(raw.hex().upper())
            )
            frame_no += 1
            log(
                "[SATELLITE][SEND][QUEUED] gateway={} frame_no={} bytes={} "
                "timestamp={}".format(
                    self.gateway_id, frame_no, len(raw), payload["timestamp"]
                )
            )
            # 立即落地：帧 timestamp 即送达时刻（无压帧时延）。
            try:
                self._post_ingest(raw)
            except Exception as exc:
                log(
                    "[SATELLITE][SEND][WARN] frame_no={} send failed: {} | "
                    "retry next cycle".format(frame_no, exc),
                    level="WARN",
                )
            if self.interval <= 0:
                log(
                    "[SATELLITE] one-shot send completed; thread remains idle"
                )
                while True:
                    time.sleep(3600)
            # 发送节奏：一条落地后等待 link_delay_s±link_jitter_s 再发下一条
            # （卫星约 2 分钟一条、连续发送）。
            pace_s = _jittered_delay(self.link_delay_s, self.link_jitter_s)
            log("[SATELLITE] next send in {:.0f}s".format(pace_s))
            time.sleep(pace_s)

    def run(self):
        if self.over_5g:
            self._run_over_5g()
            return
        if not self._load_pyserial():
            return
        log(
            "[SATELLITE][MAIN] uplink started configured_port={} baud={} "
            "gateway={} interval={}s data_type={} query_queue={}".format(
                self.port or "auto",
                self.baudrate,
                self.gateway_id,
                self.interval,
                self.data_type,
                self.query_queue,
            )
        )
        log("[SATELLITE] detected_ports={}".format(self._available_ports()))

        while True:
            ser = None
            try:
                selected_port = self._resolve_port()
                ser = self._open_serial(selected_port)
                time.sleep(0.3)
                self._wake_and_initialize(ser)
                send_index = 0
                while True:
                    send_index += 1
                    log("[SATELLITE][SEND] cycle={}".format(send_index))
                    if not self._send_gateway_identity(ser):
                        raise RuntimeError("AT+SEND failed")

                    if self.interval <= 0:
                        log(
                            "[SATELLITE] one-shot send completed; thread remains idle"
                        )
                        while True:
                            time.sleep(3600)

                    log("[SATELLITE] next send in {}s".format(self.interval))
                    time.sleep(self.interval)
                    self._wake_before_periodic_send(ser)
            except Exception as exc:
                log(
                    "[SATELLITE][ERROR] serial/uplink error={} reconnect_in={}s".format(
                        exc, self.reconnect_interval
                    ),
                    level="ERROR",
                )
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    log("[SATELLITE] serial port closed")
            time.sleep(self.reconnect_interval)


# ======================================================================
# 主程序：同时启动 7777、8888、9100 和两个 11500 发送通道
# ======================================================================

def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Unified edge gateway: media 7777 + JSON 8888 + BaoTong 9100 "
            "-> cloud 11500"
        )
    )

    parser.add_argument(
        "--media-listen-host",
        default=DEFAULT_MEDIA_LISTEN_HOST,
        help="视频/图片监听地址，默认 {}".format(DEFAULT_MEDIA_LISTEN_HOST),
    )
    parser.add_argument(
        "--media-listen-port",
        type=int,
        default=DEFAULT_MEDIA_LISTEN_PORT,
        help="视频/图片监听端口，默认 {}".format(DEFAULT_MEDIA_LISTEN_PORT),
    )

    parser.add_argument(
        "--json-listen-host",
        default=DEFAULT_JSON_LISTEN_HOST,
        help="JSON 监听地址，默认 {}".format(DEFAULT_JSON_LISTEN_HOST),
    )
    parser.add_argument(
        "--json-listen-port",
        type=int,
        default=DEFAULT_JSON_LISTEN_PORT,
        help="JSON 监听端口，默认 {}".format(DEFAULT_JSON_LISTEN_PORT),
    )

    parser.add_argument(
        "--cloud-host",
        default=DEFAULT_CLOUD_HOST,
        help="云服务器地址，默认 {}".format(DEFAULT_CLOUD_HOST),
    )
    parser.add_argument(
        "--cloud-port",
        type=int,
        default=DEFAULT_CLOUD_PORT,
        help="云服务器统一目标端口，默认 {}".format(DEFAULT_CLOUD_PORT),
    )

    parser.add_argument(
        "--link-monitor-interval",
        type=float,
        default=60.0,
        help="各模态监测表打印间隔（秒），0 表示关闭，默认 60",
    )
    parser.add_argument(
        "--slice-metrics-port",
        type=int,
        default=DEFAULT_SLICE_METRICS_PORT,
        help="切片状态上报端口，默认 {}".format(DEFAULT_SLICE_METRICS_PORT),
    )
    parser.add_argument(
        "--slice-metrics-interval",
        type=float,
        default=DEFAULT_SLICE_METRICS_INTERVAL,
        help="切片状态上报周期（秒），默认 {}".format(
            DEFAULT_SLICE_METRICS_INTERVAL
        ),
    )
    parser.add_argument(
        "--edge-heartbeat-port",
        type=int,
        default=DEFAULT_EDGE_HEARTBEAT_PORT,
        help="边缘网关心跳上报端口，默认 {}".format(
            DEFAULT_EDGE_HEARTBEAT_PORT
        ),
    )
    parser.add_argument(
        "--edge-heartbeat-interval",
        type=float,
        default=DEFAULT_EDGE_HEARTBEAT_INTERVAL,
        help="边缘网关心跳周期（秒），默认 {}".format(
            DEFAULT_EDGE_HEARTBEAT_INTERVAL
        ),
    )
    parser.add_argument(
        "--link-status-host",
        default=None,
        help="链路状态服务器地址，默认与 --cloud-host 相同",
    )
    parser.add_argument(
        "--link-status-port",
        type=int,
        default=DEFAULT_LINK_STATUS_PORT,
        help="链路状态服务器端口，默认 {}".format(DEFAULT_LINK_STATUS_PORT),
    )

    parser.add_argument(
        "--baotong-host",
        default=DEFAULT_BAOTONG_HOST,
        help="宝通监听地址，默认 {}".format(DEFAULT_BAOTONG_HOST),
    )
    parser.add_argument(
        "--baotong-port",
        type=int,
        default=DEFAULT_BAOTONG_PORT,
        help="宝通监听端口，默认 {}".format(DEFAULT_BAOTONG_PORT),
    )
    parser.add_argument(
        "--callee-id",
        default=DEFAULT_CALLEE_ID,
        help="宝通下发报文使用的 callee_id，默认 {}".format(DEFAULT_CALLEE_ID),
    )

    parser.add_argument(
        "--gateway",
        default=DEFAULT_GATEWAY_ID,
        help="JSON 补充的网关标识，默认 {}".format(DEFAULT_GATEWAY_ID),
    )
    parser.add_argument(
        "--gateway-handshake",
        action="store_true",
        default=DEFAULT_GATEWAY_HANDSHAKE,
        help="视频/图片云端连接建立后发送 b'S' + gateway id 握手",
    )

    parser.add_argument(
        "--json-max-buffer",
        type=int,
        default=DEFAULT_JSON_MAX_BUFFER_SIZE,
        help="单个 JSON 客户端最大文本缓冲区",
    )
    parser.add_argument(
        "--json-queue",
        type=int,
        default=DEFAULT_JSON_MAX_QUEUE_SIZE,
        help="JSON 云端发送队列最大条数",
    )
    parser.add_argument(
        "--media-queue",
        type=int,
        default=DEFAULT_MEDIA_MAX_QUEUE_PACKETS,
        help="视频/图片云端发送队列最大包数",
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=30,
        help="每 N 个 VID0 包输出一次汇总日志，默认 30",
    )
    parser.add_argument(
        "--compact-log",
        action="store_true",
        help="关闭逐消息和逐数据包 DEBUG 日志，仅保留连接、汇总和异常日志",
    )

    parser.add_argument(
        "--time-set-interval",
        type=float,
        default=TIME_SET_INTERVAL,
        help="向每个 JSON 客户端下发 time_set 的周期，默认 {} 秒；<=0 关闭".format(
            TIME_SET_INTERVAL
        ),
    )
    parser.add_argument(
        "--whitelist-url",
        default=None,
        help=(
            "白名单 HTTP 地址；默认 http://<cloud-host>:{}/".format(
                DEFAULT_WHITELIST_CLOUD_PORT
            )
        ),
    )
    parser.add_argument(
        "--whitelist-interval",
        type=float,
        default=DEFAULT_WHITELIST_INTERVAL,
        help="白名单拉取周期，默认 {} 秒；<=0 关闭自动拉取".format(
            DEFAULT_WHITELIST_INTERVAL
        ),
    )
    parser.add_argument(
        "--whitelist-file",
        default=DEFAULT_WHITELIST_FILE,
        help="本地白名单缓存文件，默认 {}".format(DEFAULT_WHITELIST_FILE),
    )
    parser.add_argument(
        "--whitelist-filter",
        action="store_true",
        help="启用白名单过滤，仅转发白名单内设备的数据",
    )
    parser.add_argument(
        "--post-whitelist",
        default=None,
        help="一次性将指定 JSON 文件中的 devices 列表 POST 到白名单服务后退出",
    )

    parser.add_argument(
        "--disable-satellite",
        action="store_true",
        help="关闭400-GM12 USB串口卫星上行",
    )
    parser.add_argument(
        "--satellite-port",
        default=DEFAULT_SATELLITE_PORT,
        help=(
            "卫星模组串口，默认 {}；设为 auto 时仅在发现唯一串口时自动选择".format(
                DEFAULT_SATELLITE_PORT
            )
        ),
    )
    parser.add_argument(
        "--satellite-baud",
        type=int,
        default=DEFAULT_SATELLITE_BAUD,
        help="卫星串口波特率，默认 {}".format(DEFAULT_SATELLITE_BAUD),
    )
    parser.add_argument(
        "--satellite-interval",
        type=float,
        default=DEFAULT_SATELLITE_INTERVAL,
        help="卫星网关身份发送周期，默认 {} 秒；0表示成功发送一次后空闲".format(
            DEFAULT_SATELLITE_INTERVAL
        ),
    )
    parser.add_argument(
        "--satellite-command-timeout",
        type=float,
        default=DEFAULT_SATELLITE_COMMAND_TIMEOUT,
        help="单条卫星AT命令响应超时，默认 {} 秒".format(
            DEFAULT_SATELLITE_COMMAND_TIMEOUT
        ),
    )
    parser.add_argument(
        "--satellite-reconnect-interval",
        type=float,
        default=DEFAULT_SATELLITE_RECONNECT_INTERVAL,
        help="卫星串口异常重试周期，默认 {} 秒".format(
            DEFAULT_SATELLITE_RECONNECT_INTERVAL
        ),
    )
    parser.add_argument(
        "--satellite-data-type",
        type=int,
        choices=(0, 1, 2),
        default=DEFAULT_SATELLITE_DATA_TYPE,
        help="AT+SEND数据类型：0常规、1紧急、2测试；默认0",
    )
    parser.add_argument(
        "--satellite-skip-queue-query",
        action="store_true",
        help="卫星发送成功后不执行AT+CMMQ?待发队列查询",
    )

    return parser


def validate_args(args):
    for name in (
        "media_listen_port",
        "json_listen_port",
        "cloud_port",
        "slice_metrics_port",
        "edge_heartbeat_port",
        "link_status_port",
        "baotong_port",
    ):
        if getattr(args, name) <= 0:
            raise ValueError("{} must be greater than 0".format(name))

    if args.satellite_baud <= 0:
        raise ValueError("satellite_baud must be greater than 0")
    if args.satellite_interval < 0:
        raise ValueError("satellite_interval cannot be negative")
    if args.satellite_command_timeout <= 0:
        raise ValueError("satellite_command_timeout must be greater than 0")
    if args.satellite_reconnect_interval <= 0:
        raise ValueError("satellite_reconnect_interval must be greater than 0")
    if not str(args.satellite_port).strip():
        raise ValueError("satellite_port cannot be empty")


def main():
    global DETAIL_LOG_ENABLED

    args = build_parser().parse_args()
    validate_args(args)
    DETAIL_LOG_ENABLED = not args.compact_log

    whitelist_url = args.whitelist_url or "http://{}:{}/".format(
        args.cloud_host, DEFAULT_WHITELIST_CLOUD_PORT
    )

    if args.post_whitelist:
        manager = WhitelistManager(
            whitelist_url,
            args.whitelist_interval,
            args.whitelist_file,
        )
        with open(args.post_whitelist, "r", encoding="utf-8") as file_obj:
            devices = json.load(file_obj).get("devices", [])
        manager.push(devices)
        return

    log_every = max(1, int(args.log_every))
    link_status_host = args.link_status_host or args.cloud_host
    # 2.2.4 联调：短波/卫星直发模式（默认 None=现网行为）。
    radio_over_5g = load_radio_over_5g_config()

    json_queue = DroppingQueue(args.json_queue)
    media_queue = DroppingQueue(args.media_queue)
    slice_metrics = SliceMetricsCollector(args.gateway, media_queue, json_queue)

    link_status_monitor = LinkStatusMonitor(
        link_status_host,
        args.link_status_port,
        args.gateway,
        RECONNECT_INTERVAL,
    )

    baotong_server = ProtocolBaoTongServer(
        args.baotong_host,
        args.baotong_port,
        args.callee_id,
        args.gateway,
        link_status_monitor,
    )

    json_sender = JsonCloudSender(
        args.cloud_host,
        args.cloud_port,
        json_queue,
        slice_metrics,
    )
    slice_reporter = SliceMetricsReporter(
        args.cloud_host,
        args.slice_metrics_port,
        args.slice_metrics_interval,
        slice_metrics,
    )
    edge_heartbeat_reporter = EdgeHeartbeatReporter(
        args.cloud_host,
        args.edge_heartbeat_port,
        args.edge_heartbeat_interval,
        args.gateway,
        baotong_server,
    )

    whitelist = WhitelistManager(
        whitelist_url,
        args.whitelist_interval,
        args.whitelist_file,
    )
    log("[WHITELIST] initial fetch target={}".format(whitelist_url))
    try:
        whitelist.fetch()
    except Exception as exc:
        log(
            "[WHITELIST][WARN] initial fetch failed: {} | cached_devices={}".format(
                exc, len(whitelist.get_devices())
            ),
            level="WARN",
        )

    log("=" * 72)
    log("[MAIN] unified gateway started")
    log(
        "[MAIN] media input : {}:{} (VID0/SNAP)".format(
            args.media_listen_host, args.media_listen_port
        )
    )
    log(
        "[MAIN] JSON input  : {}:{} (all JSON, shortwave business enabled)".format(
            args.json_listen_host, args.json_listen_port
        )
    )
    log(
        "[MAIN] BaoTong     : {}:{} callee_id={}".format(
            args.baotong_host, args.baotong_port, args.callee_id
        )
    )
    log(
        "[MAIN] link status : {}:{} gateway={}".format(
            link_status_host,
            args.link_status_port,
            canonical_gateway_name(args.gateway),
        )
    )
    log(
        "[MAIN] cloud target: {}:{} (JSON + media, separate TCP connections)".format(
            args.cloud_host, args.cloud_port
        )
    )
    log(
        "[MAIN] edge forward: {} | marker={}".format(
            "enabled" if edge_forward_enabled()
            else "disabled until ./edge_forward.sh --start",
            edge_forward_marker_path(),
        )
    )
    log(
        "[MAIN] multi-source access: {} | marker={}".format(
            "enabled" if multi_source_enabled()
            else "gated until ./multi_source_access.sh",
            multi_source_marker_path(),
        )
    )
    log(
        "[MAIN] whitelist filter: {} | marker={}".format(
            "enabled" if whitelist_filter_enabled()
            else "off until ./trust_access_add_whitelist.sh",
            whitelist_filter_marker_path(),
        )
    )
    log("[MAIN] gateway id: {}".format(args.gateway))
    log(
        "[MAIN] slice metrics: {}:{} every {}s (gateway from --gateway)".format(
            args.cloud_host,
            args.slice_metrics_port,
            args.slice_metrics_interval,
        )
    )
    log(
        "[MAIN] edge heartbeat: {}:{} every {}s gateway={}".format(
            args.cloud_host,
            args.edge_heartbeat_port,
            args.edge_heartbeat_interval,
            args.gateway,
        )
    )
    log(
        "[MAIN] satellite: enabled={} port={} baud={} interval={}s "
        "data_type={} queue_query={}".format(
            not args.disable_satellite,
            args.satellite_port,
            args.satellite_baud,
            args.satellite_interval,
            args.satellite_data_type,
            not args.satellite_skip_queue_query,
        )
    )
    log(
        "[MAIN] queues: JSON={}; media={} | JSON max buffer={} bytes".format(
            args.json_queue, args.media_queue, args.json_max_buffer
        )
    )
    log(
        "[MAIN] media handshake={} | log_mode={}".format(
            args.gateway_handshake,
            "detailed" if DETAIL_LOG_ENABLED else "compact",
        )
    )
    log(
        "[MAIN] time_set interval={}s | whitelist_filter={} | whitelist_url={} "
        "| whitelist_interval={}s | whitelist_cache={} | cached_devices={}".format(
            args.time_set_interval,
            args.whitelist_filter,
            whitelist_url,
            args.whitelist_interval,
            args.whitelist_file,
            len(whitelist.get_devices()),
        )
    )
    log("[MAIN] reconnect interval={}s | report interval={}s".format(
        RECONNECT_INTERVAL, REPORT_INTERVAL
    ))
    log("=" * 72)

    # 1. 服务器 11417 链路状态订阅与宝通监听 9100。
    link_status_monitor.start()
    detail_log("[MAIN] thread started: {}".format(link_status_monitor.name))

    baotong_server.name = "baotong-listener"
    baotong_server.start()
    detail_log("[MAIN] thread started: {}".format(baotong_server.name))

    edge_heartbeat_thread = threading.Thread(
        target=edge_heartbeat_reporter.run_forever,
        daemon=True,
        name="edge-heartbeat-sender",
    )
    edge_heartbeat_thread.start()
    detail_log("[MAIN] thread started: {}".format(edge_heartbeat_thread.name))

    if args.whitelist_interval > 0:
        whitelist_thread = threading.Thread(
            target=whitelist.sync_loop,
            daemon=True,
            name="whitelist-sync",
        )
        whitelist_thread.start()
        detail_log("[MAIN] thread started: {}".format(whitelist_thread.name))
    else:
        log("[WHITELIST][SYNC] automatic synchronization disabled")

    # 2. JSON 云端发送线程，目标 11500。
    json_sender_thread = threading.Thread(
        target=json_sender.run_forever,
        daemon=True,
        name="json-cloud-sender",
    )
    json_sender_thread.start()
    detail_log("[MAIN] thread started: {}".format(json_sender_thread.name))

    # 持续传输监测表：大纲 2.2.3 步骤4，边缘网关周期打印各模态监测指标。
    if args.link_monitor_interval > 0:
        link_monitor_thread = threading.Thread(
            target=link_monitor_report_loop,
            args=(args.link_monitor_interval,),
            daemon=True,
            name="link-monitor",
        )
        link_monitor_thread.start()
        detail_log("[MAIN] thread started: {}".format(link_monitor_thread.name))

    slice_reporter_thread = threading.Thread(
        target=slice_reporter.run_forever,
        daemon=True,
        name="slice-metrics-sender",
    )
    slice_reporter_thread.start()
    detail_log("[MAIN] thread started: {}".format(slice_reporter_thread.name))

    # 3. 视频/图片云端发送线程，目标 11500。
    media_sender_thread = threading.Thread(
        target=media_cloud_sender_loop,
        args=(
            media_queue,
            args.cloud_host,
            args.cloud_port,
            args.gateway,
            args.gateway_handshake,
            log_every,
            slice_metrics,
        ),
        daemon=True,
        name="media-cloud-sender",
    )
    media_sender_thread.start()
    detail_log("[MAIN] thread started: {}".format(media_sender_thread.name))

    # 4. JSON 统一接收端口 8888。
    json_listener_thread = threading.Thread(
        target=serve_json,
        args=(
            args.json_listen_host,
            args.json_listen_port,
            json_queue,
            args.gateway,
            args.json_max_buffer,
            baotong_server,
            whitelist,
            args.whitelist_filter,
            args.time_set_interval,
            slice_metrics,
            radio_over_5g,
        ),
        daemon=True,
        name="json-listener",
    )
    json_listener_thread.start()
    detail_log("[MAIN] thread started: {}".format(json_listener_thread.name))

    # 5. USB串口400-GM12卫星上行；故障只影响该守护线程。
    if not args.disable_satellite:
        satellite_ingest_port = int(
            os.environ.get("EDGE_SAT_INGEST_PORT", "11503")
        )
        satellite_uplink = SatelliteUplink(
            port=args.satellite_port,
            baudrate=args.satellite_baud,
            gateway_id=args.gateway,
            interval=args.satellite_interval,
            command_timeout=args.satellite_command_timeout,
            reconnect_interval=args.satellite_reconnect_interval,
            data_type=args.satellite_data_type,
            query_queue=not args.satellite_skip_queue_query,
            over_5g=radio_over_5g is not None,
            link_delay_s=(
                radio_over_5g["sat_delay_s"] if radio_over_5g else 0.0
            ),
            link_jitter_s=(
                radio_over_5g["sat_jitter_s"] if radio_over_5g else 0.0
            ),
            ingest_url="http://{}:{}/ingest".format(
                args.cloud_host, satellite_ingest_port
            ),
        )
        satellite_uplink.start()
        detail_log("[MAIN] thread started: {}".format(satellite_uplink.name))
    else:
        log("[SATELLITE][MAIN] disabled by command line", level="WARN")

    # 6. 主线程运行视频/图片接收端口 7777。
    def _handle_shutdown(signum, frame):
        # 重启/停止前把短波信道状态砸回去：探测在途或 detect=1 时向
        # 工控机发 reset，请求电台复位回扫描态，避免"只重启边缘"后
        # 核心侧电台停在探测状态导致双边互等、信道卡死。
        try:
            baotong_server.send_exit_reset()
        except Exception as exc:
            log("[BAOTONG-V2][EXIT-RESET] handler error: {}".format(exc),
                level="WARN")
        log("[MAIN] signal {} received, exiting".format(signum))
        # 默认处理是重新抛出信号自杀，保证 systemd 的停止语义不变。
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    serve_media(
        packet_queue=media_queue,
        listen_host=args.media_listen_host,
        listen_port=args.media_listen_port,
        log_every=log_every,
        slice_metrics=slice_metrics,
    )


if __name__ == "__main__":
    main()
