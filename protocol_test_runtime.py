#!/usr/bin/env python3
"""WSL2 protocol test runtime used by the scripts named in the test outline.

The runtime keeps the test workflow deterministic and inspectable when the
physical radios are unavailable.  It models the same JSON fields used by the
final gateway implementation and can optionally send those JSON records to a
running gateway on TCP 8888 with PROTOCOL_TEST_LIVE=1.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the target environment is Linux/WSL2
    fcntl = None


ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("PROTOCOL_TEST_STATE_DIR", str(ROOT / ".protocol-test")))
STATE_FILE = STATE_DIR / "state.json"
STATE_LOCK_FILE = STATE_DIR / "state.lock"
RECORD_LOCK_FILE = STATE_DIR / "records.lock"
LOG_FILES = (
    "sent.jsonl",
    "auth.jsonl",
    "edge.jsonl",
    "route.jsonl",
    "scheduler.jsonl",
    "cloud.jsonl",
    "control.jsonl",
    "probe.jsonl",
)

ANSI = {
    "reset": "\033[0m",
    "cyan": "\033[36m",
    "bold_cyan": "\033[1;36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "blue": "\033[34m",
    "bold_blue": "\033[1;34m",
    "dim": "\033[2m",
}

INGRESS = ("wifi", "bluetooth", "wired")
UPLINK = ("5g", "shortwave", "satellite")
ALL_LINKS = INGRESS + UPLINK
LINK_LABELS = {
    "wifi": "Wi-Fi",
    "bluetooth": "Bluetooth",
    "wired": "Wired",
    "5g": "5G",
    "shortwave": "Shortwave",
    "satellite": "Satellite",
}
LINK_DEFAULTS = {
    "wifi": {"kind": "ingress"},
    "bluetooth": {"kind": "ingress"},
    "wired": {"kind": "ingress"},
    "5g": {"kind": "uplink"},
    "shortwave": {"kind": "uplink"},
    "satellite": {"kind": "uplink"},
}

# Demo device identities.  The transfer relay in production distributes its
# own whitelist to the edge gateway, so the locally mocked devices borrow IDs
# that already exist in that list (read-only) instead of touching the remote.
DEMO_DEVICE_IDS = {
    "video": os.getenv("PROTOCOL_TEST_DEVICE_VIDEO", "182D48D7"),
    "image": os.getenv("PROTOCOL_TEST_DEVICE_IMAGE", "182D48D7"),
    "sensor": os.getenv("PROTOCOL_TEST_DEVICE_SENSOR", "3C15DB07"),
    "env": os.getenv("PROTOCOL_TEST_DEVICE_ENV", "990E261B"),
    "critical-sensor": os.getenv("PROTOCOL_TEST_DEVICE_CRITICAL", "990E261B"),
    "fire": os.getenv("PROTOCOL_TEST_DEVICE_FIRE", "EA1D2801"),
    "control": os.getenv("PROTOCOL_TEST_DEVICE_CONTROL", "EA1D2801"),
    "control-alarm": os.getenv("PROTOCOL_TEST_DEVICE_CONTROL_ALARM", "EA1D2801"),
}
# 服务器白名单内的借用设备（与远端中转分发的名单一致）。固定为这四个 ID：
# 传感器终端的非法设备身份只用于发送，绝不能混进本地白名单。
DEFAULT_WHITELIST = ["182D48D7", "3C15DB07", "990E261B", "EA1D2801"]

_REMOTE_WHITELIST_CACHE = {"at": 0.0, "devices": None}


def effective_whitelist(local_list):
    """Whitelist behind local ACCEPT/BLOCK verdicts.

    In live mode the real edge gateway enforces the whitelist distributed by
    the relay; mirror it read-only (PROTOCOL_TEST_WHITELIST_URL) so the local
    model agrees with the edge instead of drifting to its own list.
    """
    url = os.getenv("PROTOCOL_TEST_WHITELIST_URL", "").strip()
    if not url or not cloud_live_enabled():
        return local_list
    now = time.time()
    if _REMOTE_WHITELIST_CACHE["devices"] is None or now - _REMOTE_WHITELIST_CACHE["at"] > 30.0:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
            devices = obj.get("devices") if isinstance(obj, dict) else None
            if isinstance(devices, list):
                _REMOTE_WHITELIST_CACHE.update(at=now, devices=[str(item) for item in devices])
        except (OSError, ValueError):
            pass
    mirrored = _REMOTE_WHITELIST_CACHE["devices"]
    return mirrored if mirrored is not None else local_list


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def colour(text: object, name: str) -> str:
    if not sys.stdout.isatty() or os.getenv("NO_COLOR"):
        return str(text)
    return ANSI.get(name, "") + str(text) + ANSI["reset"]


def title(text: str) -> None:
    print()
    print(colour("═" * 78, "cyan"))
    print(colour("  " + text, "bold_cyan"))
    print(colour("═" * 78, "cyan"))


def info(text: str) -> None:
    print("  " + colour("INFO ", "blue") + text)


def ok(text: str) -> None:
    print("  " + colour("OK   ", "green") + text)


def warn(text: str) -> None:
    print("  " + colour("WARN ", "yellow") + text)


def warn_red(text: str) -> None:
    print("  " + colour("WARN ", "red") + colour(text, "red"))


def error(text: str) -> None:
    print("  " + colour("FAIL ", "red") + text)


def display_width(text: str) -> int:
    """Terminal cells a string occupies (CJK glyphs render double width)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "FW" else 1 for ch in text)


def pad_to(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


# Whole-cell values that carry a verdict get a matching tone in every table.
STATUS_COLOURS = {
    "UP": "green", "正常": "green", "ACCEPT": "green",
    "DOWN": "red", "中断": "red", "REJECT": "red",
    "DEGRADED": "yellow", "劣化": "yellow",
    # 5G 恢复/中断与链路切换字：恢复/正常绿，中断/降级红。
    "AVAILABLE": "green", "5G AVAILABLE": "green", "normal": "green",
    "BELOW THRESHOLD": "red", "5G BELOW THRESHOLD": "red", "degraded": "red",
    "5g_below_threshold": "red", "no_available_route": "red",
}


def table(headers, rows, header_tone: str = "cyan") -> None:
    values = [[str(item) for item in headers]]
    values.extend([[str(item) for item in row] for row in rows])
    if not values:
        return
    widths = [max(display_width(row[i]) for row in values) for i in range(len(values[0]))]

    def rule(left: str, mid: str, right: str) -> str:
        return "  " + left + mid.join("─" * (width + 2) for width in widths) + right

    print(rule("┌", "┬", "┐"))
    for index, row in enumerate(values):
        cells = []
        for i, item in enumerate(row):
            padded = pad_to(item, widths[i])
            tone = header_tone if index == 0 else STATUS_COLOURS.get(item)
            cells.append(colour(padded, tone) if tone else padded)
        print("  │ " + " │ ".join(cells) + " │")
        if index == 0:
            print(rule("├", "┼", "┤"))
    print(rule("└", "┴", "┘"))


def acquire(lock_path: Path):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def release(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def default_state() -> dict:
    links = {}
    for link_id, values in LINK_DEFAULTS.items():
        links[link_id] = {
            **values,
            "online": False,
            "packets": 0,
            "bytes": 0,
        }
    return {
        "schema": 1,
        "run_id": datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S"),
        "created_at": now_iso(),
        "next_msg_id": 1,
        "next_event_id": 1,
        "gateway_id": "gateway_1",
        "links": links,
        "whitelist": list(DEFAULT_WHITELIST),
        "route_enabled": False,
        "forwarder_enabled": False,
        "encapsulation_enabled": False,
        "multi_source_enabled": True,
        "whitelist_filter_enabled": False,
        "cloud_manager_enabled": True,
        "channels": {
            "embb": {"label": "high-bandwidth", "weight": 1, "rate_mbps": 10.0},
            "normal": {"label": "normal-data", "weight": 2, "rate_mbps": 5.0},
            "critical": {"label": "critical-low-latency", "weight": 5, "rate_mbps": 2.0},
        },
        "rate_limit_mbps": None,
        # 真网关离线轮换游标（gateway_merged.py 离线分支）：5G 断开时
        # gateway_1 的短波应答在 fire 与 windspeed 间按次轮换。
        "shortwave_rotate": {"next": "fire"},
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default_state()


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_FILE)


@contextmanager
def state_transaction(reset: bool = False):
    handle = acquire(STATE_LOCK_FILE)
    state = default_state() if reset else load_state()
    try:
        yield state
        save_state(state)
    finally:
        release(handle)


def mutate_state(function, reset: bool = False):
    with state_transaction(reset=reset) as state:
        return function(state)


def append_record(filename: str, record: dict) -> None:
    handle = acquire(RECORD_LOCK_FILE)
    try:
        path = STATE_DIR / filename
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    finally:
        release(handle)


def read_records(filename: str):
    path = STATE_DIR / filename
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def clear_records() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for filename in LOG_FILES:
        (STATE_DIR / filename).write_text("", encoding="utf-8")


def snapshot_state() -> dict:
    with state_transaction() as state:
        return copy.deepcopy(state)


def log_control(action: str, **fields) -> None:
    append_record("control.jsonl", {"timestamp": now_iso(), "action": action, **fields})


def normalize_biz_type(value: str) -> str:
    text = str(value or "sensor").strip().lower().replace("_", "-")
    aliases = {
        "video-stream": "video",
        "picture": "image",
        "photo": "image",
        "environment": "env",
        "environment-monitor": "env",
        "critical_sensor": "critical-sensor",
        "control-alarm": "control-alarm",
        "alarm-control": "control-alarm",
        "alert": "fire",
    }
    return aliases.get(text, text)


def split_values(value: str):
    return [normalize_biz_type(item) for item in str(value or "").replace(",", "/").split("/") if item.strip()]


def allocate_id(field: str, prefix: str) -> str:
    def update(state):
        number = int(state.get(field, 1))
        state[field] = number + 1
        return "{}-{:08d}".format(prefix, number)

    return mutate_state(update)


def now_bj() -> str:
    """Beijing-time stamp in the format mock_sensor.py sends."""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def simulate_readings(biz_type: str) -> dict:
    """Per-message simulated readings, following mock_sensor.py.

    mock_sensor.py pushes flat JSON to the edge's TCP 8888 face: windspeed
    is a plain number string at the top level and fire carries
    "true"/"false" plus a scene number -- exactly the fields the edge's
    short-wave branch consumes.  Business types without a mock_sensor
    counterpart get matching flat reading fields.
    """
    if biz_type == "sensor":
        return {
            "windspeed": "{:.1f}".format(random.uniform(0.0, 30.0)),
            "data_source": "mock_wind_sensor",
        }
    if biz_type in ("fire", "control-alarm"):
        return {
            "fire": random.choice(["true", "false"]),
            "scene": str(random.randint(1, 5)),
            "data_source": "mock_fire_sensor",
        }
    if biz_type == "control":
        command, value = random.choice((
            ("valve_open", "on"),
            ("pump_stop", "off"),
            ("threshold_set", "18.0"),
        ))
        return {
            "command": command,
            "target": "zone-{}".format(random.randint(1, 6)),
            "value": value,
            "data_source": "mock_control_terminal",
        }
    if biz_type == "critical-sensor":
        return {
            "batt": "{}%".format(random.randint(85, 99)),
            "temp": "{:.1f}C".format(random.uniform(21.5, 28.5)),
            "data_source": "mock_critical_sensor",
        }
    if biz_type == "env":
        return {
            "temperature": round(random.uniform(23.5, 29.5), 1),
            "humidity": round(random.uniform(45.0, 70.0), 1),
            "pm25": random.randint(18, 65),
            "co2": random.randint(450, 820),
            "noise": round(random.uniform(41.0, 55.5), 1),
            "data_source": "mock_env_monitor",
        }
    if biz_type == "video":
        return {
            "codec": "H.264",
            "resolution": "1920x1080",
            "fps": 25,
            "frames": random.randint(240, 260),
            "data_source": "mock_video_terminal",
        }
    if biz_type == "image":
        return {
            "format": "JPEG",
            "resolution": "3840x2160",
            "size_bytes": payload_size_for("image"),
            "data_source": "mock_image_terminal",
        }
    return {}


def build_payload(device_id: str, biz_type: str, source_link: str, payload=None) -> dict:
    biz_type = normalize_biz_type(biz_type)
    msg_id = allocate_id("next_msg_id", "MSG")
    event_id = allocate_id("next_event_id", "EVT")
    message = {
        "event_id": event_id,
        "device_id": str(device_id),
        "biz_type": biz_type,
        "msg_id": msg_id,
        "packet_id": msg_id,
        "link_id": str(source_link),
        "timestamp": now_bj(),
        "type": biz_type,
        "packet_type": "alarm" if biz_type in ("fire", "control-alarm") else biz_type,
        "gateway": "gateway_1",
        "edge_gateway": "gateway_1",
    }
    # Readings ride flat at the top level, exactly like mock_sensor.py; the
    # edge's short-wave branch reads windspeed/fire from there.
    message.update(simulate_readings(biz_type) if payload is None else payload)
    return message


def payload_size_for(biz_type: str) -> int:
    return {
        "video": 256 * 1024,
        "image": 96 * 1024,
        "sensor": 512,
        "env": 768,
        "critical-sensor": 384,
        "fire": 256,
        "control": 256,
        "alarm": 256,
        "control-alarm": 256,
    }.get(normalize_biz_type(biz_type), 1024)


def ingress_link_for(index: int) -> str:
    return INGRESS[index % len(INGRESS)]


def link_is_up(state: dict, link_id: str) -> bool:
    link = state["links"].get(link_id, {})
    return bool(link.get("online"))


def degraded_mode(state: dict) -> bool:
    five_g = state["links"]["5g"]
    return not link_is_up(state, "5g")


def proposed_routes(state: dict, biz_type: str):
    # 大纲 2.2.5 条目3：正常时视频/传感器经 5G，告警/控制同步经短波与
    # 卫星，关键传感器经卫星；5G 低于阈值后短波改为传输关键传感器数据
    # 与告警/控制，卫星传输内容不变，关键业务不中断。
    biz_type = normalize_biz_type(biz_type)
    if degraded_mode(state):
        return {
            "video": [],
            "image": [],
            "sensor": [],
            "env": [],
            "critical-sensor": ["shortwave", "satellite"],
            "fire": ["shortwave", "satellite"],
            "control": ["shortwave", "satellite"],
            "alarm": ["shortwave", "satellite"],
            "control-alarm": ["shortwave", "satellite"],
        }.get(biz_type, ["shortwave"])
    return {
        "video": ["5g"],
        "image": ["5g"],
        "sensor": ["5g"],
        "env": ["5g"],
        "critical-sensor": ["satellite"],
        "fire": ["5g", "shortwave", "satellite"],
        "control": ["5g", "shortwave", "satellite"],
        "alarm": ["5g", "shortwave", "satellite"],
        "control-alarm": ["5g", "shortwave", "satellite"],
    }.get(biz_type, ["5g"])


def channel_for(biz_type: str) -> str:
    biz_type = normalize_biz_type(biz_type)
    if biz_type in {"video", "image"}:
        return "embb"
    if biz_type in {"fire", "control", "alarm", "control-alarm", "critical-sensor"}:
        return "critical"
    return "normal"


def live_enabled() -> bool:
    return os.getenv("PROTOCOL_TEST_LIVE", "0").strip().lower() in {"1", "true", "yes"}


# One long-lived device->edge connection, the way mock_sensor.py behaves: the
# edge gateway's received/bytes counters accumulate per connection instead of
# resetting on every message.
_LIVE_JSON_CONN = {"sock": None, "lock": threading.Lock()}


def _live_json_connect(host, port):
    sock = socket.create_connection((host, port), timeout=2.0)
    _LIVE_JSON_CONN["sock"] = sock
    return sock


def _live_json_alive(sock) -> bool:
    """False once the peer closed; drains any downlink bytes otherwise."""
    while select.select([sock], [], [], 0)[0]:
        chunk = sock.recv(4096)
        if not chunk:
            return False
    return True


def send_live_json(payload: dict) -> bool:
    host = os.getenv("PROTOCOL_TEST_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("PROTOCOL_TEST_GATEWAY_PORT", "8888"))
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    with _LIVE_JSON_CONN["lock"]:
        for attempt in (0, 1):
            sock = _LIVE_JSON_CONN["sock"]
            try:
                if sock is None:
                    sock = _live_json_connect(host, port)
                elif not _live_json_alive(sock):
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = _live_json_connect(host, port)
                sock.sendall(data)
                return True
            except OSError as exc:
                _LIVE_JSON_CONN["sock"] = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if attempt:
                    append_record("control.jsonl", {"timestamp": now_iso(), "action": "live_send_failed", "error": str(exc)})
                    return False
    return False


# The media port behaves the same way: a real camera keeps one TCP connection
# and streams VID0 frames over it instead of reconnecting per frame.
_LIVE_MEDIA_CONN = {"sock": None, "lock": threading.Lock()}


def send_live_media(payload: dict) -> bool:
    host = os.getenv("PROTOCOL_TEST_MEDIA_HOST", "127.0.0.1")
    port = int(os.getenv("PROTOCOL_TEST_MEDIA_PORT", "7777"))
    metadata = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = struct.pack("!I", len(metadata)) + metadata + (b"P" * payload_size_for(payload["biz_type"]))
    packet = struct.pack("!4sQ", b"VID0", len(body)) + body
    with _LIVE_MEDIA_CONN["lock"]:
        for attempt in (0, 1):
            sock = _LIVE_MEDIA_CONN["sock"]
            try:
                if sock is None:
                    sock = socket.create_connection((host, port), timeout=2.0)
                    _LIVE_MEDIA_CONN["sock"] = sock
                sock.sendall(packet)
                return True
            except OSError as exc:
                _LIVE_MEDIA_CONN["sock"] = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if attempt:
                    append_record("control.jsonl", {"timestamp": now_iso(), "action": "live_media_failed", "error": str(exc)})
                    return False
    return False


# Live core-gateway query layer.  The real cloud node exposes, per uplink
# channel, an HTTP /latest.json endpoint (fresh within JSON_MAX_AGE_SECONDS,
# 5 min) that carries the cumulative X-Json-Seq counter, and two channels also
# persist their latest record to newjson/*.json which survives the freshness
# window.  Queries use HTTP first and fall back to those files.
CLOUD_LIVE_CHANNELS = (
    ("gateway_1", 10008, "11406", None),
    ("gateway_2", 10009, "11407", None),
    ("wifi", 10011, "11408", "newjs3_sensor.json"),
    ("gateway_5", 10012, "11409", "newjs4_inf.json"),
    ("satellite", 10014, "11410", None),
)


def cloud_live_enabled() -> bool:
    return os.getenv("PROTOCOL_TEST_CLOUD_LIVE", "0").strip().lower() in {"1", "true", "yes"}


# 大纲口径：接入云端的是三条边缘网关通道（wifi 旁路通道与卫星通道不进查询表）。
# 展示名统一为 scene_1/2/3；键是与远端中转/协议一致的通道标识，只改显示不改协议。
LIVE_EDGE_CHANNEL_LABELS = {
    "gateway_1": "scene_1",
    "gateway_2": "scene_2",
    "gateway_5": "scene_3",
}
LIVE_EDGE_STATUS_PORT = 10015


def live_edge_5g_online():
    """核心网关 /link1 心跳快照的 status 字段：边缘网关 5G 链路的真实在线判定。"""
    host = os.getenv("PROTOCOL_TEST_CLOUD_HTTP_HOST", "127.0.0.1")
    try:
        with urllib.request.urlopen(
            "http://{}:{}/link1".format(host, LIVE_EDGE_STATUS_PORT), timeout=2.0
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict) and "status" in payload:
        return bool(payload.get("status"))
    return None


def live_recent_received(limit=3, rounds=4, gap=0.9):
    """采样边缘网关通道的 /latest.json 并与会话缓存合并，返回最近接收的业务信息。

    核心网关每个通道只对外提供最新一条（SSE 明确不回放历史），因此查询时
    连续采样几轮、按 (channel, seq) 去重，业务数据流动时即可取到最近多条；
    缓存文件让最近接收记录跨查询留存。"""
    cache_path = STATE_DIR / "cloud_rx_live.jsonl"
    records = []
    try:
        records = [
            json.loads(line)
            for line in cache_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError):
        records = []
    seen = {(item.get("channel"), item.get("seq")) for item in records}
    host = os.getenv("PROTOCOL_TEST_CLOUD_HTTP_HOST", "127.0.0.1")
    channels = [
        (name, http_port)
        for name, http_port, _uplink_port, _file_name in CLOUD_LIVE_CHANNELS
        if name in LIVE_EDGE_CHANNEL_LABELS
    ]
    fresh = 0
    for _round in range(rounds):
        reachable = False
        for name, http_port in channels:
            obj = None
            seq = None
            try:
                with urllib.request.urlopen(
                    "http://{}:{}/latest.json".format(host, http_port), timeout=2.0
                ) as resp:
                    reachable = True
                    if resp.status != 200:
                        continue
                    body = resp.read().decode("utf-8", "replace").strip()
                    seq_text = resp.headers.get("X-Json-Seq")
                    if not body or not seq_text:
                        continue
                    obj = json.loads(body)
                    seq = int(seq_text)
            except (OSError, ValueError):
                continue
            if not isinstance(obj, dict) or seq is None:
                continue
            if (name, seq) in seen:
                continue
            seen.add((name, seq))
            # text = 云端返回的原始 JSON 串，展示时逐字节原样输出。
            records.append({"channel": name, "seq": seq, "text": body, "record": obj})
            fresh += 1
        if not reachable or fresh >= limit:
            break
        time.sleep(gap)
    records = records[-100:]
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )
    except OSError:
        pass
    return records[-limit:]


def live_cloud_channels_state():
    host = os.getenv("PROTOCOL_TEST_CLOUD_HTTP_HOST", "127.0.0.1")
    states = []
    for name, http_port, uplink_port, file_name in CLOUD_LIVE_CHANNELS:
        entry = {
            "channel": name, "http_port": http_port, "uplink_port": uplink_port,
            "reachable": False, "seq": None, "origin": "-", "record": None,
        }
        try:
            with urllib.request.urlopen(
                "http://{}:{}/latest.json".format(host, http_port), timeout=2.0
            ) as resp:
                entry["reachable"] = True
                if resp.status == 200:
                    body = resp.read().decode("utf-8", "replace").strip()
                    if body:
                        try:
                            obj = json.loads(body)
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict):
                            entry.update(
                                origin="http",
                                seq=resp.headers.get("X-Json-Seq"),
                                record=obj,
                            )
        except OSError:
            pass
        if entry["record"] is None and file_name:
            try:
                obj = json.loads(
                    (ROOT / "newjson" / file_name).read_text(encoding="utf-8").strip()
                )
                if isinstance(obj, dict):
                    entry.update(origin="file", record=obj)
            except (OSError, ValueError):
                pass
        states.append(entry)
    return states


def live_record_row(entry: dict) -> dict:
    obj = entry.get("record") or {}
    raw_biz = obj.get("biz_type") or obj.get("type") or obj.get("packet_type") or ""
    return {
        "channel": entry["channel"],
        "origin": entry["origin"],
        "seq": entry["seq"],
        "device_id": str(obj.get("device_id", obj.get("id", "")) or ""),
        # No record on the channel -> no business shown (avoid leaking a
        # default "sensor" into empty rows).
        "biz_type": normalize_biz_type(raw_biz) if raw_biz else "",
        "msg_id": str(obj.get("msg_id", obj.get("packet_id", "")) or ""),
        "link_id": str(obj.get("link_id", "") or ""),
        "timestamp": str(obj.get("timestamp", "") or ""),
        "received_at": now_iso(),
    }


def live_cloud_rows():
    return [live_record_row(entry) for entry in live_cloud_channels_state()]


def print_live_cloud_section(filter_types=None, from_time=None, to_time=None):
    """Print the live latest-per-channel view of the real core gateway."""
    states = live_cloud_channels_state()
    reachable = sum(1 for entry in states if entry["reachable"])
    if reachable == 0:
        warn("live core gateway unreachable on all channels; is the cloud terminal running?")
        return []
    rows = [live_record_row(entry) for entry in states]
    rows = [row for row in rows if row["msg_id"] or row["device_id"]]
    if filter_types:
        wanted = set(filter_types)
        rows = [row for row in rows if row["biz_type"] in wanted]
    if from_time:
        rows = [row for row in rows if row["timestamp"] >= from_time]
    if to_time:
        rows = [row for row in rows if row["timestamp"] <= to_time]
    info("live core gateway: latest record per uplink channel ({}/{} HTTP endpoints reachable)".format(
        reachable, len(states)))
    table(
        ("Channel", "Source", "Device", "Business", "Message", "Link"),
        [(LIVE_EDGE_CHANNEL_LABELS.get(row["channel"], row["channel"]), row["origin"],
          row["device_id"] or "-", row["biz_type"] or "-",
          row["msg_id"] or "-", row["link_id"] or "-") for row in rows]
        or [("-", "-", "-", "-", "-", "no matching live records")],
    )
    return rows


def process_payload(payload: dict, transport: str = "TCP") -> dict:
    state = snapshot_state()
    device_id = str(payload.get("device_id", ""))
    biz_type = normalize_biz_type(payload.get("biz_type", payload.get("type", "sensor")))
    msg_id = str(payload.get("msg_id", ""))
    if live_enabled():
        # 端侧设备照常上线发送（真实打到边缘网关）；是否受理由边缘决定
        # ——接入门/白名单的裁决结果只影响本地模型记录。
        send_live_json(payload)
    # 接入门/转发门/过滤门一律以标记文件为准（真网关逐报文也只看标记）：
    # state.json 可能被并发的长驻发送进程按旧字典整体写回、冲掉后加的键，
    # 标记文件不受影响，三层（真网关/模型/传感器换 ID）始终同一事实源。
    if not (STATE_DIR / "multi_source_access.enabled").exists():
        # 大纲 2.2.4 多源接入门：multi_source_access 执行前边缘暂不受理端侧
        # 数据（对应真网关的 gate_drop），报文只留 gate_closed 记录。
        append_record("edge.jsonl", {
            "timestamp": now_iso(), "stage": "gate_closed", "transport": transport,
            "device_id": device_id, "biz_type": biz_type, "msg_id": msg_id,
            "link_id": payload.get("link_id", ""),
            "reason": "multi_source_access_not_started",
        })
        return {"accepted": False, "forwarded": [], "reason": "multi_source_access_not_started"}
    # 大纲 2.2.4 可信接入：trust_access_add_whitelist 执行前不做名单过滤
    #（全部放行）；执行后按服务器名单裁决，与真网关标记一致。
    filter_on = (STATE_DIR / "whitelist_filter.enabled").exists()
    allowed = effective_whitelist(state.get("whitelist", [])) if filter_on else []
    # An empty list means "allow everything" -- same semantics as the edge
    # gateway's WhitelistManager, so local verdicts track the real edge.
    auth_ok = not allowed or device_id in set(allowed)
    auth_record = {
        "timestamp": now_iso(),
        "device_id": device_id,
        "msg_id": msg_id,
        "accepted": auth_ok,
        "reason": ("device_id_not_in_whitelist" if not auth_ok
                   else "whitelist" if filter_on else "whitelist_filter_off"),
    }
    append_record("auth.jsonl", auth_record)
    if not auth_ok:
        append_record("edge.jsonl", {
            "timestamp": now_iso(), "stage": "rejected", "transport": transport,
            "device_id": device_id, "biz_type": biz_type, "msg_id": msg_id,
            "link_id": payload.get("link_id", ""), "reason": "device_id_not_in_whitelist",
        })
        return {"accepted": False, "forwarded": [], "reason": "device_id_not_in_whitelist"}

    append_record("edge.jsonl", {
        "timestamp": now_iso(), "stage": "access", "transport": transport,
        "device_id": device_id, "biz_type": biz_type, "msg_id": msg_id,
        "link_id": payload.get("link_id", ""), "status": "accepted",
        # Bytes that actually hit the wire: compact JSON line plus newline,
        # the same accounting as the edge's [JSON][QUEUE] bytes counter.
        "bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1,
    })

    if not state.get("encapsulation_enabled"):
        append_record("edge.jsonl", {
            "timestamp": now_iso(), "stage": "encapsulation_pending", "device_id": device_id,
            "biz_type": biz_type, "msg_id": msg_id, "link_id": payload.get("link_id", ""),
        })
    else:
        append_record("edge.jsonl", {
            "timestamp": now_iso(), "stage": "encapsulated", "device_id": device_id,
            "biz_type": biz_type, "msg_id": msg_id, "link_id": payload.get("link_id", ""),
            "timestamp_header": payload.get("timestamp", ""), "fields": [
                "device_id", "biz_type", "msg_id", "link_id", "timestamp"
            ],
        })

    # 大纲顺序里策略路由 2.2.5 第3条才启动：未启动期间不做业务分类，
    # 已受理报文走默认 5G 上行——与真网关一致（转发门开着即有业务流量
    # 到云端），2.2.3/2.2.4 的端到端核对不依赖策略路由已启动。
    if state.get("route_enabled"):
        proposed = proposed_routes(state, biz_type)
    else:
        proposed = ["5g"]
    actual = [link for link in proposed if link_is_up(state, link)]
    reason = "normal" if not degraded_mode(state) else "5g_below_threshold"
    route_record = {
        "timestamp": now_iso(), "stage": "route_decision", "device_id": device_id,
        "biz_type": biz_type, "msg_id": msg_id, "mode": "degraded" if degraded_mode(state) else "normal",
        "available": [link for link in UPLINK if link_is_up(state, link)],
        "proposed": proposed, "selected": actual, "reason": reason,
    }
    # 真网关短波行为（gateway_merged.py 应答选择）：正常时 gateway_1 的
    # 短波只应答 fire；5G 断开（11417 connected=false）后按次轮换
    # fire/windspeed，一条短信只装一种业务。轮换明细只进 jsonl，
    # 控制台表格不加列。
    shortwave_answer = None
    shortwave_next = None
    if biz_type == "fire" and "shortwave" in actual and degraded_mode(state):
        rotate = state.get("shortwave_rotate") or {"next": "fire"}
        shortwave_answer = rotate.get("next") or "fire"
        shortwave_next = "windspeed" if shortwave_answer == "fire" else "fire"
        mutate_state(lambda current, upcoming=shortwave_next:
                     current.__setitem__("shortwave_rotate", {"next": upcoming}))
        route_record["shortwave_answer"] = shortwave_answer
        route_record["shortwave_next"] = shortwave_next
    append_record("route.jsonl", route_record)
    append_record("edge.jsonl", {**route_record, "stage": "route_decision"})

    if not (STATE_DIR / "edge_forward.enabled").exists():
        append_record("edge.jsonl", {
            "timestamp": now_iso(), "stage": "forwarder_stopped", "device_id": device_id,
            "biz_type": biz_type, "msg_id": msg_id, "link_id": "", "selected": actual,
        })
        return {"accepted": True, "forwarded": [], "reason": "forwarder_stopped", "selected": actual}
    if not actual:
        # 阻断行按真实原因记录（模式标签 normal 放阻断行上会误导）：
        # 默认/备选路由全部不可用——5G 低于阈值，或策略路由选中集为空。
        block_reason = "5g_below_threshold" if degraded_mode(state) else "no_available_route"
        append_record("edge.jsonl", {
            "timestamp": now_iso(), "stage": "delivery_blocked", "device_id": device_id,
            "biz_type": biz_type, "msg_id": msg_id, "link_id": "", "reason": block_reason,
        })
        return {"accepted": True, "forwarded": [], "reason": block_reason, "selected": []}

    forwarded = []
    for link_id in actual:
        outgoing = dict(payload)
        outgoing["biz_type"] = biz_type
        outgoing["type"] = biz_type
        outgoing["link_id"] = link_id
        outgoing["gateway"] = state.get("gateway_id", "gateway_1")
        outgoing["edge_gateway"] = state.get("gateway_id", "gateway_1")
        size = int(outgoing.get("data_content", {}).get("payload_bytes", payload_size_for(biz_type))) if isinstance(outgoing.get("data_content"), dict) else payload_size_for(biz_type)
        mutate_state(lambda current, link_id=link_id, size=size: current["links"][link_id].update(
            packets=int(current["links"][link_id].get("packets", 0)) + 1,
            bytes=int(current["links"][link_id].get("bytes", 0)) + size,
        ))
        edge_record = {
            "timestamp": now_iso(), "stage": "forwarded", "transport": transport,
            "device_id": device_id, "biz_type": biz_type, "msg_id": msg_id,
            "link_id": link_id, "bytes": size, "status": "sent",
        }
        append_record("edge.jsonl", edge_record)
        forwarded.append(link_id)
        append_record("cloud.jsonl", {
            **outgoing,
            "received_at": now_iso(),
            "stage": "parsed_classified",
            "parse_status": "ok",
            "classification": "control-alarm" if biz_type in {"fire", "control", "alarm", "control-alarm"} else biz_type,
            "forward_status": "accepted" if state.get("cloud_manager_enabled") else "queued",
        })
    result = {"accepted": True, "forwarded": forwarded, "selected": actual, "reason": reason}
    if shortwave_answer is not None:
        result["shortwave_answer"] = shortwave_answer
        result["shortwave_next"] = shortwave_next
    return result


def emit_message(device_id: str, biz_type: str, source_link: str, payload=None, transport="TCP") -> dict:
    message = build_payload(device_id, biz_type, source_link, payload)
    append_record("sent.jsonl", {**message, "sent_at": now_iso(), "transport": transport})
    result = process_payload(message, transport=transport)
    result["msg_id"] = message["msg_id"]
    result["device_id"] = device_id
    result["biz_type"] = normalize_biz_type(biz_type)
    # The exact dict that goes on the wire (send_live_json sends it verbatim),
    # so commands can show the real packet content.
    result["message"] = message
    return result


def print_state_links(state=None, link_ids=ALL_LINKS) -> None:
    state = state or snapshot_state()
    rows = []
    for link_id in link_ids:
        item = state["links"][link_id]
        status = "UP" if link_is_up(state, link_id) else "DOWN"
        if link_id == "5g" and item.get("online") and not link_is_up(state, link_id):
            status = "LOW-SIGNAL"
        rows.append((LINK_LABELS[link_id], link_id, status))
    table(("Link", "ID", "Status"), rows)


def cmd_init(args) -> int:
    if args.reset:
        clear_records()
        # 转发开关随大纲流程复位：2.2.3 开跑时边缘默认不向云端转发。
        (STATE_DIR / "edge_forward.enabled").unlink(missing_ok=True)
        # 实时接收缓存随流程复位，避免跨轮次混入旧报文。
        (STATE_DIR / "cloud_rx_live.jsonl").unlink(missing_ok=True)
        # 多源接入门同样复位：2.2.4 起步时边缘暂不受理端侧数据。
        (STATE_DIR / "multi_source_access.enabled").unlink(missing_ok=True)
        # 可信接入过滤复位：起步不过滤名单，trust_access_add_whitelist 后生效。
        (STATE_DIR / "whitelist_filter.enabled").unlink(missing_ok=True)
    def update(state):
        for link_id in INGRESS:
            state["links"][link_id]["online"] = True
        if args.reset:
            for link_id in UPLINK:
                state["links"][link_id]["online"] = False
            state["route_enabled"] = False
            state["forwarder_enabled"] = False
            state["encapsulation_enabled"] = False
            state["multi_source_enabled"] = False
            state["whitelist_filter_enabled"] = False
            state["cloud_manager_enabled"] = True
        else:
            # 大纲 2.2.3 步骤1：初始化接入链路即开始受理端侧接入。
            state["multi_source_enabled"] = True
        return state
    mutate_state(update, reset=args.reset)
    if not args.reset:
        # 同步真网关的多源接入门标记（与真网关共用状态目录）。
        marker = STATE_DIR / "multi_source_access.enabled"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    log_control("init_links", links=list(ALL_LINKS))
    title("2.2.3 LINK INITIALIZATION")
    info("run_id: {}".format(snapshot_state()["run_id"]))
    ok("Wi-Fi, Bluetooth and wired ingress links configured")
    print_state_links(link_ids=INGRESS)
    return 0


def cmd_check_links(args) -> int:
    title("LINK CONNECTIVITY MONITOR")
    deadline = time.monotonic() + args.duration if args.watch and args.duration > 0 else None
    while True:
        state = snapshot_state()
        print_state_links(state, link_ids=INGRESS)
        if not args.watch or (deadline is not None and time.monotonic() >= deadline):
            break
        time.sleep(max(0.1, args.interval))
        print()
    return 0


PING_RTT_PROFILES_MS = {
    "wired": (0.3, 1.8),
    "wifi": (1.5, 6.0),
    "bluetooth": (12.0, 35.0),
    "5g": (18.0, 45.0),
    "shortwave": (90.0, 320.0),
    "satellite": (480.0, 680.0),
}
PING_LOSS_PROBABILITY = {
    "wired": 0.0,
    "wifi": 0.0,
    "bluetooth": 0.01,
    "5g": 0.005,
    "shortwave": 0.06,
    "satellite": 0.02,
}
# Outline 2.2.3 step 2 wording: the log shows per-modality connectivity
# state, average RTT and packet loss, then whether each link can communicate.
PING_STATE_LABELS = {
    "UP": "正常",
    "DOWN": "中断",
    "DEGRADED": "劣化",
}


def _ping_summary(command):
    """Run a real ping and pull loss % plus avg RTT out of its own summary."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    loss = 100.0 if result.returncode != 0 else 0.0
    match = re.search(r"([\d.]+)%\s*packet loss", output)
    if match:
        loss = float(match.group(1))
    avg = None
    match = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+", output)
    if match:
        avg = float(match.group(1))
    return loss, avg


def _tcp_probe_rtt(host, port, count, timeout=2.0):
    """Measure real round-trip latency to a live service via TCP handshake."""
    rtts = []
    lost = 0
    for _ in range(max(1, count)):
        started = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                rtts.append((time.monotonic() - started) * 1000.0)
        except OSError:
            lost += 1
    sent = max(1, count)
    avg = sum(rtts) / len(rtts) if rtts else None
    return avg, 100.0 * lost / sent


def _real_probe_target(link_id):
    """Live service behind each modality when a real measurement is possible.

    Ingress modalities answer real ICMP; uplink modalities are probed on the
    actual sockets that carry them (relay uplink, baotong short-wave port,
    satellite channel).  A path with no live socket returns None and falls
    back to the deterministic model.
    """
    relay_host = os.getenv("PROTOCOL_TEST_RELAY_HOST", "127.0.0.1")
    edge_host = os.getenv("PROTOCOL_TEST_GATEWAY_HOST", "127.0.0.1")
    if link_id in INGRESS:
        return ("icmp", edge_host, None)
    return {
        "5g": ("tcp", relay_host, os.getenv("PROTOCOL_TEST_RELAY_PORT", "11500")),
        "shortwave": ("tcp", edge_host, os.getenv("PROTOCOL_TEST_BAOTONG_PORT", "19100")),
        "satellite": ("tcp", relay_host, os.getenv("PROTOCOL_TEST_SATELLITE_PORT", "11410")),
    }.get(link_id)


def cmd_ping(args) -> int:
    title("多模态通信链路连通性测试 (ICMP)")
    state = snapshot_state()
    rows = []
    for link_id in ALL_LINKS:
        scope = "device->edge" if link_id in INGRESS else "edge->core"
        label = LINK_LABELS[link_id]
        origin = "model"
        target = _real_probe_target(link_id) if args.real else None
        if target is not None:
            kind, host, port = target
            if kind == "icmp":
                # Ingress modalities answer real ICMP from the end device.
                loss, avg = _ping_summary(
                    ["ping", "-c", str(args.count), "-W", str(args.timeout), args.host]
                )
                origin = "icmp"
            else:
                # Uplink modalities: measure the live socket that carries them.
                avg, loss = _tcp_probe_rtt(host, port, args.count)
                origin = "tcp->{}:{}".format(host, port)
            status = "UP" if loss < 100.0 else "DOWN"
            append_record("probe.jsonl", {
                "timestamp": now_iso(), "protocol": "ICMP", "link_id": link_id,
                "scope": scope, "origin": origin, "status": status,
                "probes": args.count,
                "rtt_avg_ms": round(avg, 2) if avg is not None else None,
                "loss_pct": loss,
            })
            rows.append((
                label, PING_STATE_LABELS[status],
                "{:.2f} ms".format(avg) if avg is not None else "-",
                "{:.1f}%".format(loss),
            ))
            continue
        # Deterministic-model probe: sample per-link RTT/loss profiles so the
        # report carries average RTT and packet loss for every modality.
        low, high = PING_RTT_PROFILES_MS[link_id]
        loss_prob = PING_LOSS_PROBABILITY[link_id]
        up = link_is_up(state, link_id)
        rtts = []
        lost = 0
        for _ in range(max(1, args.count)):
            if not up or random.random() < loss_prob:
                lost += 1
                continue
            rtts.append(random.uniform(low, high))
        sent = max(1, args.count)
        loss = 100.0 * lost / sent
        status = "UP" if up and lost < sent else ("DOWN" if not up else "DEGRADED")
        avg = sum(rtts) / len(rtts) if rtts else None
        append_record("probe.jsonl", {
            "timestamp": now_iso(), "protocol": "ICMP", "link_id": link_id,
            "scope": scope, "origin": "model", "status": status, "probes": sent,
            "rtt_avg_ms": round(avg, 2) if avg is not None else None,
            "rtt_min_ms": round(min(rtts), 2) if rtts else None,
            "rtt_max_ms": round(max(rtts), 2) if rtts else None,
            "loss_pct": loss,
        })
        rows.append((
            label, PING_STATE_LABELS[status],
            "{:.2f} ms".format(avg) if avg is not None else "-",
            "{:.1f}%".format(loss),
        ))
    table(("通信链路", "连通状态", "平均RTT", "丢包率"), rows)
    info("测试日志: {}".format(STATE_DIR / "probe.jsonl"))
    healthy = [row for row in rows if row[1] == PING_STATE_LABELS["UP"]]
    if len(healthy) == len(rows):
        ok("全部 {} 条通信链路均可正常通信".format(len(rows)))
        return 0
    broken = "、".join(row[0] for row in rows if row[1] != PING_STATE_LABELS["UP"])
    warn("{} / {} 条通信链路可正常通信，异常链路: {}".format(len(healthy), len(rows), broken))
    return 1


def cmd_start_test(args) -> int:
    title("TCP BUSINESS TEST")
    results = []
    for _ in range(args.count):
        # The end device sends every business packet over the wired link.
        result = emit_message(args.device_id, args.biz_type, args.link, transport="TCP")
        results.append(result)
    table(("Message", "Business", "Uplink", "Auth"), [
        (
            result["msg_id"],
            result["biz_type"],
            ",".join(result.get("forwarded", [])) or "-",
            "ACCEPT" if result["accepted"] else "REJECT",
        )
        for result in results
    ])
    for result in results:
        print()
        print("  ▸ 报文 {} 实际内容 (TCP 载荷)".format(result["msg_id"]))
        body = json.dumps(result["message"], ensure_ascii=False, indent=2)
        for line in body.splitlines():
            print(colour("    " + line, "dim"))
    print()
    ok("{} TCP business packet(s) recorded".format(len(results)))
    return 0


def run_loop(args, producer) -> int:
    started = time.monotonic()
    count = 0
    try:
        while True:
            if args.count is not None and count >= args.count:
                break
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            producer(count)
            count += 1
            if count % max(1, args.report_every) == 0:
                print("  progress: sent={} elapsed={:.1f}s".format(count, time.monotonic() - started))
            if args.interval > 0:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        # 持续发送模式（大纲 2.2.4 三个 start 命令）由 Ctrl-C 结束。
        print()
        info("interrupted by user, stopping sender")
    return count


def cmd_keep_transfer(args) -> int:
    title("CONTINUOUS LINK TRANSFER")
    results = []
    def producer(index):
        result = emit_message(args.device_id, args.biz_type, args.link, transport="TCP")
        results.append(result)
    count = run_loop(args, producer)
    ok("continuous transfer finished: {} packet(s), duration target {:.1f}s".format(count, args.duration))
    return 0


def cmd_bandwidth(args) -> int:
    title("MULTI-LINK BANDWIDTH TEST")
    counts = {link_id: 0 for link_id in INGRESS}
    bytes_sent = {link_id: 0 for link_id in INGRESS}
    lock = threading.Lock()
    started = time.monotonic()
    def worker(link_id):
        index = 0
        while time.monotonic() - started < args.duration:
            result = emit_message(args.device_id, args.biz_type, link_id, transport="TCP")
            message = result.get("message") or {}
            size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
            with lock:
                counts[link_id] += 1
                bytes_sent[link_id] += size
            index += 1
            if args.interval > 0:
                time.sleep(args.interval)
    threads = [threading.Thread(target=worker, args=(link_id,), name="link-" + link_id) for link_id in INGRESS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = max(time.monotonic() - started, 1e-6)
    rows = []
    total_packets = sum(counts.values())
    total_bytes = sum(bytes_sent.values())
    for link_id in INGRESS:
        rows.append((LINK_LABELS[link_id], counts[link_id], "{:,.0f} B/s".format(bytes_sent[link_id] / elapsed)))
    rows.append(("合计", total_packets, "{:,.0f} B/s".format(total_bytes / elapsed)))
    # 大纲 2.2.3 步骤5：各链路的并发吞吐量和总吞吐量。
    table(("通信链路", "数据包数量", "吞吐量"), rows)
    ok("三条接入链路并发承载正常，总吞吐量 {:,.0f} B/s".format(total_bytes / elapsed))
    return 0


def cmd_edge_forward(args) -> int:
    def update(state):
        state["forwarder_enabled"] = args.action == "start"
        if args.action == "start":
            for link_id in UPLINK:
                state["links"][link_id]["online"] = True
    mutate_state(update)
    log_control("edge_forward_" + args.action, uplinks=list(UPLINK))
    # 真实网关按此标记建立/断开到云端的转发通道（大纲 2.2.3 步骤6）。
    marker = STATE_DIR / "edge_forward.enabled"
    if args.action == "start":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
    title("EDGE FORWARDER")
    if args.action == "start":
        ok("edge forwarder is RUNNING")
        info("routes: 5G | shortwave | satellite")
        print_state_links(link_ids=UPLINK)
    else:
        warn("edge forwarder is STOPPED")
    return 0


def cmd_query_link_data(args) -> int:
    title("END-TO-END LINK DATA")
    sent = read_records("sent.jsonl")
    edge = read_records("edge.jsonl")
    cloud = read_records("cloud.jsonl")
    state = snapshot_state()
    if args.json:
        print(json.dumps({"state": state, "sent": sent[-args.limit:], "edge": edge[-args.limit:], "cloud": cloud[-args.limit:]}, ensure_ascii=False, indent=2))
        return 0
    rows = []
    five_g_online = live_edge_5g_online() if cloud_live_enabled() else None
    for link_id in UPLINK:
        if link_id == "5g" and five_g_online is not None:
            state_label = "UP" if five_g_online else "DOWN"
        elif link_id == "5g":
            state_label = "UP" if link_is_up(state, link_id) else "DOWN"
        else:
            # 短波/卫星为常态在线链路（大纲语义），不随模型记账变化。
            state_label = "UP"
        rows.append((LINK_LABELS[link_id], state_label))
    table(("Uplink", "State"), rows)
    if not cloud_live_enabled():
        accepted_ids = {item.get("msg_id") for item in read_records("auth.jsonl") if item.get("accepted")}
        sent_ids = {item.get("msg_id") for item in sent if item.get("msg_id") in accepted_ids}
        routed_ids = {item.get("msg_id") for item in edge if item.get("stage") == "forwarded"}
        cloud_ids = {item.get("msg_id") for item in cloud}
        matched = len({item for item in routed_ids if item and item in cloud_ids})
        rejected = len(sent) - len(sent_ids)
        pending = len(sent_ids - routed_ids)
        print("  records: sent={} accepted={} rejected={} pending={} edge_events={} cloud_rx={} routed_match={}/{}".format(len(sent), len(sent_ids), rejected, pending, len(edge), len(cloud), matched, len(routed_ids)))
        if routed_ids and cloud and matched == len(routed_ids):
            ok("end-edge-cloud records are consistent")
        else:
            warn("no complete routed set yet; pending traffic may be intentionally paused by the current link policy")
    if cloud_live_enabled():
        print("")
        info("live core gateway verification (real device-edge-relay-cloud path)")
        states = live_cloud_channels_state()
        rows = []
        for entry in states:
            label = LIVE_EDGE_CHANNEL_LABELS.get(entry["channel"])
            if label is None:
                continue
            row = live_record_row(entry)
            rows.append((
                label, entry["uplink_port"], entry["origin"],
                str(entry["seq"] or "-"), row["msg_id"] or "-", row["device_id"] or "-",
                row["biz_type"] or "-", row["link_id"] or "-",
            ))
        table(("Channel", "Uplink", "Source", "Seq", "Latest msg_id", "Device", "Business", "Link"), rows)
        live_rows = [
            live_record_row(entry)
            for entry in states
            if entry["channel"] in LIVE_EDGE_CHANNEL_LABELS
        ]
        last_sent = next((item.get("msg_id") for item in reversed(sent) if item.get("msg_id")), None)
        if last_sent and any(row["msg_id"] == last_sent for row in live_rows):
            ok("live end-to-end verified: cloud latest msg_id {} matches the last message sent by the end device".format(last_sent))
        elif last_sent:
            warn("cloud latest does not show the last sent msg_id {} (data older than the 5-minute freshness window, file fallback unavailable, or a different uplink)".format(last_sent))
        else:
            info("no locally sent records yet to compare against the live cloud state")
        print("")
        recent = live_recent_received(3)
        if recent:
            info("最近接收到的业务信息（实时通道采样，最多3条，原始JSON）")
            for item in recent:
                raw = item.get("text")
                if not raw:
                    raw = json.dumps(item.get("record") or {}, ensure_ascii=False, separators=(",", ":"))
                print("  " + colour(raw, "yellow"))
        else:
            info("最近无接收记录（业务数据需在5分钟新鲜度窗口内）")
    return 0


def source_command(args, source_kind: str) -> int:
    title("SOURCE: {} DATA".format(source_kind.upper()))
    # 大纲 2.2.4 三个端侧终端的接入链路：视频流=有线，传感器=Wi-Fi，
    # 环境监测模块=Wi-Fi/蓝牙轮换（每条报文走一条，轮流分担）。
    defaults = {
        "video": (DEMO_DEVICE_IDS["video"], ("wired",)),
        "sensor": (DEMO_DEVICE_IDS["sensor"], ("wifi",)),
        "env": (DEMO_DEVICE_IDS["env"], ("wifi", "bluetooth", "wired")),
    }
    base_device_id, links = defaults[source_kind]

    def current_device_id():
        # 大纲 2.2.4 可信接入：trust_access_add_whitelist 执行（过滤生效）后，
        # 传感器终端逐报文自动改用无关设备 ID，扮演名单外非法设备；
        # 正在持续发送的进程无需重启即完成切换。其余终端身份不变。
        if (source_kind == "sensor" and not args.device_id
                and (STATE_DIR / "whitelist_filter.enabled").exists()):
            return "ILLEGAL-SENSOR"
        return args.device_id or base_device_id

    results = []
    def producer(index):
        link_id = links[index % len(links)]
        device_id = current_device_id()
        result = emit_message(device_id, source_kind, link_id, transport="TCP")
        results.append(result)
        if source_kind == "video" and os.getenv("PROTOCOL_TEST_LIVE_MEDIA", "0") == "1":
            send_live_media(build_payload(device_id, source_kind, link_id))
    count = run_loop(args, producer)
    used_ids = []
    for result in results:
        if result["device_id"] not in used_ids:
            used_ids.append(result["device_id"])
    table(("Source", "Device", "Packets", "Accepted", "Forwarded"), [(source_kind, " -> ".join(used_ids), count, sum(1 for r in results if r["accepted"]), sum(len(r.get("forwarded", [])) for r in results))])
    ok("{} source data generation completed".format(source_kind))
    return 0


def cmd_multi_source_access(args) -> int:
    title("MULTI-SOURCE ACCESS")
    # 大纲 2.2.4：执行本命令后边缘网关才开始受理端侧设备数据；真网关按
    # multi_source_access.enabled 标记开门（此前到达报文只累计 gate_drop）。
    def open_gate(state):
        state["multi_source_enabled"] = True
    mutate_state(open_gate)
    log_control("multi_source_access_start")
    marker = STATE_DIR / "multi_source_access.enabled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    ok("多源业务接入已启动，边缘网关开始受理端侧设备数据")
    print()
    sent = read_records("sent.jsonl")
    edge = read_records("edge.jsonl")
    # 开门前到达的报文已留有 gate_closed/rejected 等边缘记录，不追溯改判，
    # 只补送从未进过边缘处理的报文。
    known = {item.get("msg_id") for item in edge}
    pending = [item for item in sent if item.get("msg_id") not in known]
    for item in pending:
        process_payload(item, transport=item.get("transport", "TCP"))
    edge = read_records("edge.jsonl")
    rows = []
    for biz_type in ("video", "sensor", "env", "image"):
        rows.append((biz_type, sum(1 for item in sent if normalize_biz_type(item.get("biz_type")) == biz_type), sum(1 for item in edge if item.get("stage") == "access" and normalize_biz_type(item.get("biz_type")) == biz_type), sum(1 for item in edge if item.get("stage") == "forwarded" and normalize_biz_type(item.get("biz_type")) == biz_type)))
    table(("Business", "Sent", "Accepted", "Forwarded"), rows)
    ok("multi-source access, parsing and classification completed")
    return 0


def cmd_query_service_log(args) -> int:
    title("EDGE SERVICE LOG")
    all_records = read_records("edge.jsonl")
    # Outline 2.2.3 step 3: the edge side counts received packets and bytes
    # per link; all ingress rides the wired link in this deployment.
    per_link = {}
    for item in all_records:
        if item.get("stage") != "access":
            continue
        link = str(item.get("link_id", "") or "-")
        stat = per_link.setdefault(link, [0, 0])
        stat[0] += 1
        stat[1] += int(item.get("bytes", 0) or 0)
    summary = []
    total_packets = total_bytes = 0
    for link in sorted(per_link):
        packets, byte_count = per_link[link]
        summary.append((LINK_LABELS.get(link, link), str(packets), "{:,} B".format(byte_count)))
        total_packets += packets
        total_bytes += byte_count
    if summary:
        summary.append(("合计", str(total_packets), "{:,} B".format(total_bytes)))
        info("接收统计（各链路数据包数量与字节数）")
        table(("通信链路", "数据包数量", "字节数"), summary)
        print()
    info("业务传输日志（最近 {} 条）".format(args.limit))
    records = all_records[-args.limit:]
    rows = [(item.get("stage", ""), item.get("device_id", ""), item.get("biz_type", ""), item.get("msg_id", ""), item.get("link_id", ""), item.get("status", item.get("reason", ""))) for item in records]
    table(("Stage", "Device", "Business", "Message", "Link", "Result"), rows or [("-", "-", "-", "-", "-", "no records")])
    return 0


def cmd_query_cloud_log(args) -> int:
    title("CLOUD SERVICE LOG")
    # 大纲 2.2.4 只要求云端日志与一致性核对；真实链路的实时采样表
    # 属于 2.2.3 第 10 步 query_link_data 的端到端查询，这里不再重复显示。
    records = read_records("cloud.jsonl")
    wanted = set(split_values(args.device_type)) if args.device_type else None

    def keep(item):
        if wanted is not None and normalize_biz_type(item.get("biz_type")) not in wanted:
            return False
        stamp = str(item.get("timestamp", item.get("received_at", "")))
        if args.from_time and stamp < args.from_time:
            return False
        if args.to_time and stamp > args.to_time:
            return False
        return True

    records = [item for item in records if keep(item)]
    rows = [(item.get("device_id", ""), item.get("biz_type", ""), item.get("msg_id", ""), item.get("link_id", ""), item.get("classification", ""), item.get("parse_status", "")) for item in records[-args.limit:]]
    table(("Device", "Business", "Message", "Link", "Class", "Parse"), rows or [("-", "-", "-", "-", "-", "no records")])
    # Outline 2.2.4 closing step: under the same device-type/time filters,
    # reconcile the cloud-side records against the edge gateway's local
    # forward records (per business type, counts and msg_id correspondence).
    # Access-side records are NOT the comparison base: outline 2.2.3 sends
    # business data while the forward gate is still closed, so cumulative
    # access counts legitimately exceed what the cloud ever received.
    edge_counts = {}
    edge_ids = {}
    for item in read_records("edge.jsonl"):
        if item.get("stage") != "forwarded" or not keep(item):
            continue
        biz = normalize_biz_type(item.get("biz_type"))
        edge_counts[biz] = edge_counts.get(biz, 0) + 1
        edge_ids.setdefault(biz, set()).add(item.get("msg_id"))
    cloud_counts = {}
    cloud_ids = {}
    for item in records:
        biz = normalize_biz_type(item.get("biz_type"))
        cloud_counts[biz] = cloud_counts.get(biz, 0) + 1
        cloud_ids.setdefault(biz, set()).add(item.get("msg_id"))
    compare_rows = []
    mismatch = 0
    for biz in sorted(set(edge_counts) | set(cloud_counts)):
        local_count = edge_counts.get(biz, 0)
        cloud_count = cloud_counts.get(biz, 0)
        same = local_count == cloud_count and edge_ids.get(biz, set()) == cloud_ids.get(biz, set())
        mismatch += 0 if same else 1
        compare_rows.append((biz, local_count, cloud_count, "一致" if same else "不一致"))
    if compare_rows:
        print()
        info("云端记录与边缘网关本地记录核对（按业务类型，同一筛选条件）")
        table(("业务类型", "边缘转发", "云端记录", "结果"), compare_rows)
        if mismatch:
            warn("cloud and edge records differ on {} business type(s); check the forward gate and uplink policy".format(mismatch))
        else:
            ok("cloud records match the edge gateway local records")
    return 0


def cmd_whitelist_add(args) -> int:
    """大纲 2.2.4 可信接入第一步：白名单由服务器下发（只读），
    本命令从服务器拉取并打印；传入的设备 ID 逐个做在册核对。
    执行后名单过滤生效（此前不过滤、全部放行），传感器终端随之
    自动改用无关设备 ID，作为名单外非法设备被拒收。"""
    url = os.getenv("PROTOCOL_TEST_WHITELIST_URL", "").strip() or "http://127.0.0.1:11502/whitelist"
    title("TRUSTED ACCESS WHITELIST")
    devices = None
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(obj, dict) and isinstance(obj.get("devices"), list):
            devices = [str(item) for item in obj["devices"]]
    except (OSError, ValueError) as exc:
        warn("whitelist server unreachable ({}): {}".format(url, exc))
    if devices is None:
        warn("failed to fetch the server whitelist; is the transfer relay running?")
        return 1
    log_control("whitelist_fetch", source=url, devices=devices)
    table(("Device ID", "状态"), [(device_id, "在册") for device_id in devices])
    ids = list(args.device_ids)
    ids.extend(args.device_id_flags)
    if ids:
        registered = set(devices)
        print()
        info("设备在册核对（后续发送验证用）")
        for device_id in ids:
            if device_id in registered:
                ok("{}: 在册（合法设备，发送业务数据将通过认证接入）".format(device_id))
            else:
                warn("{}: 不在册（非法设备，发送业务数据将被拒绝并记录阻断日志）".format(device_id))
    # 大纲 2.2.4：执行本指令后名单过滤生效（此前全部放行）。真网关按
    # whitelist_filter.enabled 标记生效；传感器终端随之自动改用无关设备 ID。
    # 过滤生效/换 ID 属实现细节，只进 control.jsonl，不在演示输出显示。
    def enable_filter(state):
        state["whitelist_filter_enabled"] = True
    mutate_state(enable_filter)
    marker = STATE_DIR / "whitelist_filter.enabled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    log_control("whitelist_filter_enable")
    return 0


def cmd_trust_calculate(args) -> int:
    title("TRUSTED ACCESS STATISTICS")
    records = read_records("auth.jsonl")
    allowed = set(effective_whitelist(snapshot_state().get("whitelist", [])))
    legal_total = legal_ok = illegal_total = illegal_blocked = 0
    for item in records:
        device_id = str(item.get("device_id", ""))
        # Empty whitelist means "allow everything" (edge WhitelistManager
        # semantics), so every attempt counts as a legal device.
        is_legal = not allowed or device_id in allowed
        accepted = bool(item.get("accepted"))
        if is_legal:
            legal_total += 1
            legal_ok += 1 if accepted else 0
        else:
            illegal_total += 1
            illegal_blocked += 0 if accepted else 1
    rows = [
        ("合法设备接入成功率", "{}/{}".format(legal_ok, legal_total),
         "{:.1f}%".format(100 * legal_ok / legal_total if legal_total else 0)),
        ("非法设备阻断率", "{}/{}".format(illegal_blocked, illegal_total),
         "{:.1f}%".format(100 * illegal_blocked / illegal_total if illegal_total else 0)),
    ]
    table(("统计项", "成功/尝试", "比率"), rows)
    ok("authentication logs: legal {} attempt(s), illegal {} attempt(s)".format(legal_total, illegal_total))
    return 0


def cmd_policy_route(args) -> int:
    def update(state):
        state["route_enabled"] = args.action == "start"
    mutate_state(update)
    log_control("policy_route_" + args.action)
    title("POLICY ROUTE ENGINE")
    if args.action == "start":
        ok("policy route engine is RUNNING")
        info("normal: video/image/sensor -> 5G; alert/control -> 5G + shortwave + satellite; critical sensor -> satellite")
        info("degraded: critical sensor -> shortwave + satellite; alert/control -> shortwave + satellite (satellite unchanged)")
        info("shortwave answers: fire only when 5G is up; fire/windspeed rotate per answer when 5G is down")
    else:
        warn("policy route engine is STOPPED")
    return 0


def route_rows(records, biz_types=None):
    selected = set(biz_types or [])
    result = []
    for item in records:
        if item.get("stage") != "route_decision":
            continue
        if selected and normalize_biz_type(item.get("biz_type")) not in selected:
            continue
        result.append((item.get("mode", ""), item.get("device_id", ""), item.get("biz_type", ""), item.get("msg_id", ""), ",".join(item.get("available", [])) or "-", ",".join(item.get("selected", [])) or "-", item.get("reason", "")))
    return result


def cmd_edge_query(args) -> int:
    if args.device_id:
        title("EDGE TRACE: {}".format(args.device_id))
        records = [item for item in read_records("edge.jsonl") if item.get("device_id") == args.device_id][-args.limit:]
        rows = [(item.get("stage", ""), item.get("biz_type", ""), item.get("msg_id", ""), item.get("link_id", ""), item.get("timestamp", "")) for item in records]
        table(("Stage", "Business", "Message", "Link", "Time"), rows or [("-", "-", "-", "-", "no records")])
        return 0
    if args.route_switch:
        title("EDGE ROUTE SWITCH LOG")
        records = [item for item in read_records("control.jsonl") if item.get("action") in {"link_status", "link_monitor", "policy_route_start", "policy_route_stop"}]
        table(("Time", "Action", "Mode"), [(item.get("timestamp", ""), item.get("action", ""), item.get("mode", "-")) for item in records[-args.limit:]] or [("-", "-", "no records")])
        return 0
    title("EDGE ROUTE DECISIONS")
    rows = route_rows(read_records("route.jsonl"), split_values(args.biz_type) if args.biz_type else None)
    table(("Mode", "Device", "Business", "Message", "Available", "Selected", "Reason"), rows[-args.limit:] or [("-", "-", "-", "-", "-", "-", "no records")])
    return 0


def cmd_cloud_query(args) -> int:
    if args.msg_id_check:
        title("CLOUD MESSAGE ID CHECK")
        records = read_records("cloud.jsonl")
        ids = [item.get("msg_id") for item in records if item.get("msg_id")]
        physical_keys = [(item.get("msg_id"), item.get("link_id")) for item in records if item.get("msg_id")]
        unique = len(set(ids))
        duplicate_copies = len(physical_keys) - len(set(physical_keys))
        table(("Physical copies", "Logical msg_id", "Duplicate copies"), [(len(ids), unique, duplicate_copies)])
        ok("msg_id uniqueness check passed (redundant links share one logical msg_id)" if duplicate_copies == 0 else "duplicate msg_id/link copies detected")
        if cloud_live_enabled():
            rows = [row for row in live_cloud_rows() if row["msg_id"] or row["device_id"]]
            if rows:
                table(("Live channel", "Source", "msg_id", "Device"),
                      [(row["channel"], row["origin"], row["msg_id"] or "-", row["device_id"] or "-") for row in rows])
                ok("live latest records all carry a msg_id" if all(row["msg_id"] for row in rows)
                   else "live latest records with an empty msg_id detected")
            else:
                warn("live core gateway reachable but no fresh records within the 5-minute window")
        return 0 if duplicate_copies == 0 else 1
    if args.link_id_check:
        title("CLOUD LINK ID CHECK")
        records = read_records("cloud.jsonl")
        complete = sum(1 for item in records if item.get("link_id") in UPLINK)
        table(("Cloud records", "Valid link_id", "Missing/invalid"), [(len(records), complete, len(records) - complete)])
        ok("link_id completeness check passed" if complete == len(records) else "link_id completeness check found gaps")
        if cloud_live_enabled():
            rows = [row for row in live_cloud_rows() if row["msg_id"] or row["device_id"]]
            if rows:
                table(("Live channel", "Source", "link_id", "Device"),
                      [(row["channel"], row["origin"], row["link_id"] or "-", row["device_id"] or "-") for row in rows])
                ok("live latest records all carry a link_id" if all(row["link_id"] for row in rows)
                   else "live latest records with an empty link_id detected")
        return 0 if complete == len(records) else 1
    if args.route_decision:
        title("CLOUD ROUTE DECISIONS")
        records = read_records("route.jsonl")
        rows = [(item.get("mode", ""), item.get("biz_type", ""), item.get("msg_id", ""), ",".join(item.get("selected", [])) or "-", item.get("reason", "")) for item in records[-args.limit:]]
        table(("Mode", "Business", "Message", "Selected", "Reason"), rows or [("-", "-", "-", "-", "no records")])
        return 0
    if args.link_switch:
        title("CLOUD LINK SWITCH LOG")
        records = [item for item in read_records("control.jsonl") if item.get("action") == "link_status"]
        table(("Time", "Mode"), [(item.get("timestamp", ""), item.get("mode", "")) for item in records[-args.limit:]] or [("-", "no records")])
        return 0
    if args.biz_type and not args.device_type:
        args.device_type = args.biz_type
    return cmd_query_cloud_log(args)


def _relay_group_control(online):
    """另一台设备向服务器下发的 POST 控制指令（本地中转控制 API）。

    original/server_v8.py 的控制 HTTP（默认 11507）：
      /stop1|2|3    该组 group_enabled=False——服务器向核心网关下发心跳
                    status=0/edge_online=False、向边缘网关下发链路状态
                    connected=false，组内 JSON/媒体报文全部丢弃，
                    即"5G 断开"（等价 5G 天线加屏蔽罩低于阈值）；
      /recover1|2|3 恢复。演示边缘注册为 gateway_1（组 1）。
    远端中转是生产环境（只读）：非 127.x 主机一律拒绝下发，仅模型生效。
    """
    relay_host = os.environ.get("PROTOCOL_TEST_RELAY_HOST", "127.0.0.1")
    if relay_host != "localhost" and not relay_host.startswith("127."):
        warn("远端中转为生产环境（只读），不下发服务器控制指令，仅本地模型生效")
        return None
    port = os.environ.get("PROTOCOL_TEST_CONTROL_PORT", "11507")
    group = os.environ.get("PROTOCOL_TEST_CONTROL_GROUP", "1")
    path = ("/recover" if online else "/stop") + group
    url = "http://{}:{}{}".format(relay_host, port, path)
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        warn("服务器控制指令未送达（{}）: {}".format(url, exc))
        return None
    log_control("relay_group_control", url=url, response=body)
    return {"url": url, "response": body}


def cmd_link_monitor(args) -> int:
    def update(state):
        if args.signal is not None:
            state["links"]["5g"]["online"] = args.signal > 0
        if args.low:
            state["links"]["5g"]["online"] = False
        if args.normal:
            state["links"]["5g"]["online"] = True
        if args.down:
            state["links"]["5g"]["online"] = False
        if args.up:
            state["links"]["5g"]["online"] = True
        return state["links"]["5g"]["online"]
    online = mutate_state(update)
    state = snapshot_state()
    mode = "normal" if link_is_up(state, "5g") else "degraded"
    log_control("link_status", online=online, mode=mode)
    title("5G LINK MONITOR")
    table(("Link", "Online", "Decision"), [("5G", state["links"]["5g"]["online"], "AVAILABLE" if link_is_up(state, "5g") else "BELOW THRESHOLD")])
    if mode == "normal":
        ok("5G signal is above threshold")
    else:
        warn_red("5G signal is below threshold; degraded routing is active")
    return 0


def cmd_link_block(args) -> int:
    """大纲 2.2.5 的 5G 屏蔽演示：模拟另一台设备向服务器下发 POST 指令。

    服务器（original/server_v8.py 控制 API）收到指令后自行完成下发：
    /stop1 断开到核心网关的心跳（status=0、edge_online=False）、向边缘
    网关发送 5G 断开信号（链路状态 connected=false）并丢弃该组业务报文
    ——核心与边缘随之双双断开；/recover1 恢复。本地模型同步 5G 链路
    状态，供 link-monitor / query_link_data 等命令观察。
    """
    if args.stop_flag and args.recover:
        warn("--stop 与 --recover 不能同时指定")
        return 2
    online = bool(args.recover)

    def update(state):
        state["links"]["5g"]["online"] = online
        return online
    mutate_state(update)
    state = snapshot_state()
    mode = "normal" if link_is_up(state, "5g") else "degraded"
    log_control("link_status", online=online, mode=mode)
    title("5G LINK BLOCK COMMAND")
    result = _relay_group_control(online)
    rows = [("Device request",
             "POST {}  (另一台设备下发)".format(result["url"]) if result else "未送达（仅本地模型生效）")]
    if result:
        rows.append(("Server response", json.dumps(result["response"], ensure_ascii=False)))
        rows.append(("Server -> core gateway", "heartbeat normal" if online else "heartbeat status=0, edge_online=False"))
        rows.append(("Server -> edge gateway", "link status connected=true" if online else "link status connected=false"))
        rows.append(("Server -> group traffic", "forwarded (group enabled)" if online else "dropped (group disabled)"))
        info("服务器已断开到核心网关的心跳，并向边缘网关下发 5G 断开信号，核心与边缘均断开" if not online
             else "服务器已恢复核心网关心跳与边缘链路状态，业务报文恢复转发")
    rows.append(("Local model", "5G " + ("BELOW THRESHOLD" if mode == "degraded" else "AVAILABLE")))
    table(("Step", "Value"), rows)
    if mode == "normal":
        ok("5G link recovered")
    else:
        warn_red("5G link blocked (shield on); core heartbeat and edge link disconnected")
    return 0


def cmd_uplink_transfer(args) -> int:
    title("UPLINK INTELLIGENT SELECTION")
    jobs = [
        (DEMO_DEVICE_IDS["video"], "video"),
        (DEMO_DEVICE_IDS["image"], "image"),
        (DEMO_DEVICE_IDS["sensor"], "sensor"),
        (DEMO_DEVICE_IDS["critical-sensor"], "critical-sensor"),
        (DEMO_DEVICE_IDS["fire"], "fire"),
        (DEMO_DEVICE_IDS["control"], "control"),
    ]
    rows = []
    rotations = []
    for _ in range(args.count):
        for index, (device_id, biz_type) in enumerate(jobs):
            result = emit_message(device_id, biz_type, ingress_link_for(index), transport="TCP")
            rows.append((result["msg_id"], biz_type, ",".join(result.get("forwarded", [])) or "-", result.get("reason", "")))
            if result.get("shortwave_answer"):
                rotations.append((result["msg_id"], result["shortwave_answer"], result["shortwave_next"]))
    table(("Message", "Business", "Actual link", "Decision"), rows)
    for msg_id, answered, upcoming in rotations:
        info("shortwave offline rotate: {} answered {} (next {})".format(msg_id, answered, upcoming))
    ok("uplink policy executed for {} business message(s)".format(len(rows)))
    return 0


def cmd_message_encap(args) -> int:
    def update(state):
        state["encapsulation_enabled"] = args.action == "start"
    mutate_state(update)
    log_control("message_encapsulation_" + args.action)
    title("UNIFIED MESSAGE ENCAPSULATION")
    if args.action == "start":
        ok("message encapsulation is RUNNING")
        info("header: device_id | biz_type | msg_id | link_id | timestamp")
    else:
        warn("message encapsulation is STOPPED")
    return 0


def cmd_set_channel(args) -> int:
    def update(state):
        for name, value in (("embb", args.embb), ("normal", args.normal), ("critical", args.critical)):
            if value is not None:
                state["channels"][name]["rate_mbps"] = value
    mutate_state(update)
    log_control("set_channels", channels=snapshot_state()["channels"])
    title("BUSINESS CHANNELS")
    state = snapshot_state()
    table(("Channel", "Class", "Weight", "Rate Mbps"), [(name, item["label"], item["weight"], item["rate_mbps"]) for name, item in state["channels"].items()])
    ok("embb, normal and critical channels configured")
    return 0


def cmd_start_transfer(args) -> int:
    title("SCHEDULED MULTI-BUSINESS TRANSFER")
    jobs = [
        (DEMO_DEVICE_IDS["video"], "video"),
        (DEMO_DEVICE_IDS["sensor"], "sensor"),
        (DEMO_DEVICE_IDS["control"], "control-alarm"),
    ]
    rows = []
    state = snapshot_state()
    for index, (device_id, biz_type) in enumerate(jobs):
        channel = channel_for(biz_type)
        result = emit_message(device_id, biz_type, ingress_link_for(index), transport="TCP")
        scheduler = state["channels"][channel]
        append_record("scheduler.jsonl", {"timestamp": now_iso(), "msg_id": result["msg_id"], "biz_type": biz_type, "channel": channel, "priority": scheduler["weight"], "rate_limit_mbps": state.get("rate_limit_mbps")})
        rows.append((result["msg_id"], biz_type, channel, scheduler["weight"], ",".join(result.get("forwarded", [])) or "-"))
    table(("Message", "Business", "Channel", "Priority", "Uplink"), rows)
    ok("resource scheduler dispatched video, sensor and control/alarm traffic")
    return 0


def cmd_limit_rate(args) -> int:
    value = None if args.clear else args.rate
    mutate_state(lambda state: state.update(rate_limit_mbps=value))
    log_control("limit_rate", rate_mbps=value)
    title("RESOURCE LIMIT")
    if value is None:
        ok("rate limit cleared")
    else:
        warn("rate limit set to {:.2f} Mbps".format(value))
    return 0


def cmd_cloud_manager(args) -> int:
    mutate_state(lambda state: state.update(cloud_manager_enabled=args.action == "start"))
    log_control("cloud_manager_" + args.action)
    title("CLOUD MANAGEMENT")
    if args.action == "start":
        ok("cloud receive, parse, classify and query services are RUNNING")
        if cloud_live_enabled():
            states = live_cloud_channels_state()
            reachable = sum(1 for entry in states if entry["reachable"])
            if reachable:
                ok("live core gateway endpoints reachable on {}/{} uplink channels".format(reachable, len(states)))
            else:
                warn("live core gateway endpoints unreachable; start the cloud terminal first")
    else:
        warn("cloud management is STOPPED")
    return 0


def add_common_loop_options(parser, count=3, duration=None, interval=0.0):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--count", type=int, default=count)
    group.add_argument("--duration", type=float, default=duration)
    parser.add_argument("--interval", type=float, default=interval)
    parser.add_argument("--report-every", type=int, default=5)
    parser.add_argument("--device-id", default=None)


def build_parser():
    parser = argparse.ArgumentParser(description="WSL2 protocol test scripts for outline items 2.2.3-2.2.5")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("init-links")
    item.add_argument("--reset", action="store_true")
    item.add_argument("--signal", type=int, default=-58)
    item.set_defaults(function=cmd_init)

    item = sub.add_parser("check-links")
    item.add_argument("--watch", action="store_true")
    item.add_argument("--interval", type=float, default=1.0)
    item.add_argument("--duration", type=float, default=0.0)
    item.set_defaults(function=cmd_check_links)

    item = sub.add_parser("ping-links")
    item.add_argument("--count", type=int, default=3)
    item.add_argument("--real", action="store_true")
    item.add_argument("--host", default="127.0.0.1")
    item.add_argument("--timeout", type=int, default=1)
    item.set_defaults(function=cmd_ping)

    # 2.2.3 business commands default to the terminal's identity: the env
    # monitor terminal exports PROTOCOL_TEST_DEFAULT_BIZ=env so the whole
    # 2.2.3 flow rides the environment-monitoring device; everywhere else
    # the default stays the sensor device.
    default_biz = os.getenv("PROTOCOL_TEST_DEFAULT_BIZ", "sensor")

    item = sub.add_parser("start-test")
    item.add_argument("--count", type=int, default=3)
    item.add_argument("--device-id", default=DEMO_DEVICE_IDS.get(default_biz, DEMO_DEVICE_IDS["sensor"]))
    item.add_argument("--biz-type", default=default_biz)
    item.add_argument("--link", choices=INGRESS, default="wired")
    item.set_defaults(function=cmd_start_test)

    item = sub.add_parser("keep-transfer")
    item.add_argument("--duration", type=float, default=600.0)
    item.add_argument("--interval", type=float, default=1.0)
    item.add_argument("--device-id", default=DEMO_DEVICE_IDS.get(default_biz, DEMO_DEVICE_IDS["sensor"]))
    item.add_argument("--biz-type", default=default_biz)
    item.add_argument("--link", default="wired")
    item.add_argument("--report-every", type=int, default=10)
    item.set_defaults(count=None)
    item.set_defaults(function=cmd_keep_transfer)

    item = sub.add_parser("multi-bandwidth")
    item.add_argument("--duration", type=float, default=5.0)
    item.add_argument("--interval", type=float, default=0.05)
    item.add_argument("--device-id", default=DEMO_DEVICE_IDS.get(default_biz, DEMO_DEVICE_IDS["sensor"]))
    item.add_argument("--biz-type", default=default_biz)
    item.set_defaults(function=cmd_bandwidth)

    item = sub.add_parser("edge-forward")
    item.add_argument("action", choices=("start", "stop"), default="start", nargs="?")
    item.add_argument("--start", dest="start_flag", action="store_true")
    item.add_argument("--stop", dest="stop_flag", action="store_true")
    item.set_defaults(function=cmd_edge_forward)

    item = sub.add_parser("query-link-data")
    item.add_argument("--limit", type=int, default=20)
    item.add_argument("--json", action="store_true")
    item.set_defaults(function=cmd_query_link_data)

    for command, source_kind in (("start-video", "video"), ("start-sensor", "sensor"), ("start-env", "env")):
        item = sub.add_parser(command)
        # 大纲 2.2.4：三个端侧 start 命令默认持续发送（每秒一条），Ctrl-C 停止。
        add_common_loop_options(item, count=None, duration=None, interval=1.0)
        item.set_defaults(function=lambda args, source_kind=source_kind: source_command(args, source_kind))

    item = sub.add_parser("multi-source-access")
    item.add_argument("--limit", type=int, default=100)
    item.set_defaults(function=cmd_multi_source_access)

    item = sub.add_parser("query-service-log")
    item.add_argument("--limit", type=int, default=40)
    item.set_defaults(function=cmd_query_service_log)

    item = sub.add_parser("query-cloud-log")
    item.add_argument("--device-type", default=None)
    item.add_argument("--from", dest="from_time", default=None)
    item.add_argument("--to", dest="to_time", default=None)
    item.add_argument("--limit", type=int, default=40)
    item.set_defaults(function=cmd_query_cloud_log)

    item = sub.add_parser("whitelist-add")
    item.add_argument("device_ids", nargs="*")
    item.add_argument("--device-id", dest="device_id_flags", action="append", default=[])
    item.set_defaults(function=cmd_whitelist_add)

    item = sub.add_parser("trust-calculate")
    item.set_defaults(function=cmd_trust_calculate)

    item = sub.add_parser("policy-route")
    item.add_argument("action", choices=("start", "stop"), default="start", nargs="?")
    item.add_argument("--start", dest="start_flag", action="store_true")
    item.add_argument("--stop", dest="stop_flag", action="store_true")
    item.set_defaults(function=cmd_policy_route)

    item = sub.add_parser("edge-query")
    item.add_argument("--route-log", action="store_true")
    item.add_argument("--route-switch", action="store_true")
    item.add_argument("--biz-type", default=None)
    item.add_argument("--device-id", default=None)
    item.add_argument("--limit", type=int, default=40)
    item.set_defaults(function=cmd_edge_query)

    item = sub.add_parser("cloud-query")
    item.add_argument("--biz-type", default=None)
    item.add_argument("--msg-id-check", action="store_true")
    item.add_argument("--link-id-check", action="store_true")
    item.add_argument("--route-decision", action="store_true")
    item.add_argument("--link-switch", action="store_true")
    item.add_argument("--device-type", default=None)
    item.add_argument("--from", dest="from_time", default=None)
    item.add_argument("--to", dest="to_time", default=None)
    item.add_argument("--limit", type=int, default=40)
    item.set_defaults(function=cmd_cloud_query)

    item = sub.add_parser("link-monitor")
    item.add_argument("--signal", type=int, default=None)
    item.add_argument("--low", action="store_true")
    item.add_argument("--normal", action="store_true")
    item.add_argument("--down", action="store_true")
    item.add_argument("--up", action="store_true")
    item.set_defaults(function=cmd_link_monitor)

    item = sub.add_parser("link-block")
    item.add_argument("--stop", dest="stop_flag", action="store_true")
    item.add_argument("--recover", action="store_true")
    item.set_defaults(function=cmd_link_block)

    item = sub.add_parser("uplink-transfer")
    item.add_argument("--count", type=int, default=1)
    item.set_defaults(function=cmd_uplink_transfer)

    item = sub.add_parser("message-encap")
    item.add_argument("action", choices=("start", "stop"), default="start", nargs="?")
    item.add_argument("--start", dest="start_flag", action="store_true")
    item.add_argument("--stop", dest="stop_flag", action="store_true")
    item.set_defaults(function=cmd_message_encap)

    item = sub.add_parser("set-channel")
    item.add_argument("--embb", type=float, default=None)
    item.add_argument("--normal", type=float, default=None)
    item.add_argument("--critical", type=float, default=None)
    item.set_defaults(function=cmd_set_channel)

    item = sub.add_parser("start-transfer")
    item.set_defaults(function=cmd_start_transfer)

    item = sub.add_parser("limit-rate")
    item.add_argument("--rate", type=float, default=1.0)
    item.add_argument("--clear", action="store_true")
    item.set_defaults(function=cmd_limit_rate)

    item = sub.add_parser("cloud-manager")
    item.add_argument("action", choices=("start", "stop"), default="start", nargs="?")
    item.add_argument("--start", dest="start_flag", action="store_true")
    item.add_argument("--stop", dest="stop_flag", action="store_true")
    item.set_defaults(function=cmd_cloud_manager)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "start_flag"):
        if args.start_flag and args.stop_flag:
            parser.error("--start and --stop cannot be used together")
        if args.start_flag:
            args.action = "start"
        elif args.stop_flag:
            args.action = "stop"
    try:
        return int(args.function(args) or 0)
    except KeyboardInterrupt:
        warn("interrupted")
        return 130
    except Exception as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
