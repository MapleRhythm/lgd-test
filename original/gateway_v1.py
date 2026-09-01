cat > gateway_v1.py << 'EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
===========================================================
  gateway.py - Core gateway: server.py -> frontend HTTP
===========================================================

Upstream framed channels:
  Connect to server.py ports 11400-11405.

Framed packet from server.py:
  [1B gateway_id_len]
  [gateway_id]
  [8B timestamp, double]
  [4B frame_seq, uint32]
  [4B packet_type: VID0/SNAP]
  [8B body_size]
  [4B metadata_len]
  [metadata JSON]
  [JPEG payload]

Upstream JSON channels:
  Connect to server.py ports 11406, 11407, 11408, 11409.
  Each JSON object is newline-delimited UTF-8 text:
    {...}\n

Frontend outputs:

  VID0 video:
    10000 /stream -> 11400 VID0
    10001 /stream -> 11401 VID0
    10002 /stream -> 11402 VID0
    10003 /stream -> 11403 VID0
    10004 /stream -> 11404 VID0

  SNAP image:
    10005 /latest.jpg -> 11400 SNAP
    10006 /latest.jpg -> 11402 SNAP
    10007 /latest.jpg -> 11405 SNAP

  JSON:
    10008 /events or /latest.json -> 11406 JSON
    10009 /events or /latest.json -> 11407 JSON
    10011 /events or /latest.json -> 11408 JSON
    10012 /events or /latest.json -> 11409 JSON

  Satellite (SQLite):
    10014 /events or /latest.json -> 11410 (managed by SQLite + ACK)

JSON file output:
  11408 latest JSON -> /root/newjson/newjs3_sensor.json
  11409 latest JSON -> /root/newjson/newjs4_inf.json

Notes:
  11401, 11403, 11404 SNAP are ignored.
  11405 is SNAP-only; no /stream is created for 11405.
  gateway.py does not forward raw server packet headers to frontend.
  It parses packets and exports HTTP MJPEG / JPEG / JSON / SSE.
"""

import argparse
import copy
import json
import os
import socket
import struct
import threading
import time
import sqlite3
import hashlib
import signal
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, urlsplit

from config import *


# Configuration values are maintained in config.py.

# ======================== Logging ========================

def bj_time_str():
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


_NO_COLOUR = bool(os.environ.get("NO_COLOR"))
_LOG_COLOURS = {
    "green": "" if _NO_COLOUR else "\033[32m",
    "red": "" if _NO_COLOUR else "\033[31m",
    "reset": "" if _NO_COLOUR else "\033[0m",
}


def log(msg, colour=None):
    line = "[{}] {}".format(bj_time_str(), msg)
    if colour:
        # 5G/链路切换状态着色：恢复绿、中断红（NO_COLOR 关闭）。
        line = _LOG_COLOURS.get(colour, "") + line + _LOG_COLOURS["reset"]
    print(line, flush=True)


def format_snr_for_frontend(value):
    """Render the BaoTong -255 dB disconnected sentinel as '--'."""
    if value is None:
        return None

    try:
        text = str(value).strip()
        if text.lower().endswith("db"):
            text = text[:-2].strip()
        if float(text) == -255.0:
            return "--"
    except (TypeError, ValueError):
        pass
    return value


def sanitize_snr_for_frontend(value):
    """Copy a frontend payload while replacing only SNR sentinel values."""
    if isinstance(value, dict):
        return {
            key: (
                format_snr_for_frontend(item)
                if key in ("tx_snr", "rx_snr")
                else sanitize_snr_for_frontend(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_snr_for_frontend(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_snr_for_frontend(item) for item in value)
    return value


# ======================== TCP helpers ========================

def recv_exact(sock, size):
    chunks = []
    received = 0

    while received < size:
        data = sock.recv(min(BUFFER_SIZE, size - received))
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


def set_socket_options(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


# ======================== Framed packet parsing ========================

def read_server_forward_packet(sock):
    id_len_bytes = recv_exact(sock, 1)
    if id_len_bytes is None:
        return None

    id_len = struct.unpack("!B", id_len_bytes)[0]
    if id_len <= 0 or id_len > MAX_GATEWAY_ID_SIZE:
        raise ValueError("invalid gateway_id length: {}".format(id_len))

    gateway_id_bytes = recv_exact(sock, id_len)
    if gateway_id_bytes is None:
        raise ConnectionError("connection closed while reading gateway_id")

    gateway_id = gateway_id_bytes.decode("utf-8", errors="replace")

    timestamp_bytes = recv_exact(sock, TIMESTAMP_STRUCT.size)
    if timestamp_bytes is None:
        raise ConnectionError("connection closed while reading timestamp")
    timestamp = TIMESTAMP_STRUCT.unpack(timestamp_bytes)[0]

    seq_bytes = recv_exact(sock, FRAME_SEQ_STRUCT.size)
    if seq_bytes is None:
        raise ConnectionError("connection closed while reading frame_seq")
    frame_seq = FRAME_SEQ_STRUCT.unpack(seq_bytes)[0]

    original_header = recv_exact(sock, PACKET_HEADER.size)
    if original_header is None:
        raise ConnectionError("connection closed while reading original header")

    packet_type_bytes, body_size = PACKET_HEADER.unpack(original_header)
    packet_type = packet_type_bytes.decode("ascii", errors="replace")

    if packet_type not in VALID_PACKET_TYPES:
        raise ValueError(
            "unknown packet_type={!r}, gateway_id={}, seq={}".format(
                packet_type, gateway_id, frame_seq
            )
        )

    if body_size < META_LENGTH.size:
        raise ValueError("invalid body_size too small: {}".format(body_size))

    if body_size > MAX_BODY_SIZE:
        raise ValueError("body_size too large: {}".format(body_size))

    body = recv_exact(sock, body_size)
    if body is None:
        raise ConnectionError("connection closed while reading original body")

    metadata, jpeg = parse_body(body)

    return {
        "gateway_id": gateway_id,
        "timestamp": timestamp,
        "frame_seq": frame_seq,
        "packet_type": packet_type,
        "body_size": body_size,
        "body": body,
        "metadata": metadata,
        "jpeg": jpeg,
    }


def parse_body(body):
    if len(body) < META_LENGTH.size:
        return {}, b""

    meta_size = META_LENGTH.unpack(body[:META_LENGTH.size])[0]

    if meta_size > MAX_META_SIZE:
        return {}, b""

    image_offset = META_LENGTH.size + meta_size
    if image_offset > len(body):
        return {}, b""

    meta_bytes = body[META_LENGTH.size:image_offset]
    jpeg = body[image_offset:]

    metadata = {}
    if meta_bytes:
        try:
            metadata = json.loads(meta_bytes.decode("utf-8"))
        except Exception:
            metadata = {}

    return metadata, jpeg


def is_jpeg(data):
    return data.startswith(b"\xff\xd8")


# ======================== Freshness helpers ========================

def parse_since_ts(raw_path):
    """
    Supported:
      ?since=1710000000000  milliseconds
      ?since=1710000000     seconds
    """
    try:
        parsed = urlparse(raw_path)
        qs = parse_qs(parsed.query)
        values = qs.get("since") or qs.get("start") or qs.get("frontend_start")
        if not values:
            return None

        value = float(values[0])
        if value > 1000000000000:
            return value / 1000.0
        return value
    except Exception:
        return None


def is_fresh_for_request(item, raw_path, max_age_seconds):
    if item is None:
        return False

    update_time = float(item.get("update_time", 0.0) or 0.0)
    if update_time <= 0:
        return False

    since_ts = parse_since_ts(raw_path)
    if since_ts is not None:
        return update_time > since_ts

    if max_age_seconds is not None and max_age_seconds > 0:
        return (time.time() - update_time) <= max_age_seconds

    return True


def send_no_content(handler):
    handler.send_response(204)
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    send_cors_headers(handler)
    handler.end_headers()


# ======================== VideoHub ========================

class VideoHub:
    def __init__(self, name):
        self.name = name
        self.cond = threading.Condition()
        self.seq = 0
        self.avg_fps = 0.0
        self.last_update_time = 0.0
        self.fps = 0.0
        self.jpeg = None
        self.metadata = {}
        self.gateway_id = ""
        self.server_seq = 0
        self.last_update = 0.0

    def update(self, jpeg, metadata, gateway_id, server_seq):
        if not jpeg:
            return

        with self.cond:
            self.seq += 1
            self.jpeg = jpeg
            self.metadata = metadata or {}
            self.gateway_id = gateway_id
            self.server_seq = server_seq
            self.last_update = time.time()
            self.cond.notify_all()

            # 更新 FPS 和最后更新时间
            now = time.time()
            if self.last_update_time > 0:
                delta = now - self.last_update_time
                if delta > 0:
                    instant_fps = 1.0 / delta
                    if self.fps == 0.0:
                        self.fps = instant_fps
                    else:
                        self.fps = 0.9 * self.fps + 0.1 * instant_fps
            self.last_update_time = now

    def current_seq(self):
        with self.cond:
            return self.seq

    def wait_next(self, last_seq, timeout=10.0):
        with self.cond:
            if self.seq <= last_seq:
                self.cond.wait(timeout=timeout)

            if self.seq <= last_seq or self.jpeg is None:
                return None

            return {
                "seq": self.seq,
                "jpeg": self.jpeg,
                "metadata": self.metadata,
                "gateway_id": self.gateway_id,
                "server_seq": self.server_seq,
                "update_time": self.last_update,
            }


# ======================== SnapshotStore ========================

class SnapshotStore:
    def __init__(self, name):
        self.name = name
        self.lock = threading.Lock()
        self.seq = 0
        self.avg_fps = 0.0
        self.jpeg = None
        self.filename = ""
        self.metadata = {}
        self.gateway_id = ""
        self.channel = ""
        self.source_port = 0
        self.update_time = 0.0

    def update(self, jpeg, metadata, gateway_id, channel, source_port, server_seq):
        if not jpeg:
            return

        filename = make_snapshot_filename(metadata, gateway_id, channel, server_seq)

        with self.lock:
            self.seq += 1
            self.jpeg = jpeg
            self.filename = filename
            self.metadata = metadata or {}
            self.gateway_id = gateway_id
            self.channel = channel
            self.source_port = source_port
            self.update_time = time.time()

    def get_latest(self):
        with self.lock:
            if self.jpeg is None:
                return None

            return {
                "seq": self.seq,
                "jpeg": self.jpeg,
                "filename": self.filename,
                "metadata": self.metadata,
                "gateway_id": self.gateway_id,
                "channel": self.channel,
                "source_port": self.source_port,
                "update_time": self.update_time,
            }


def sanitize_filename(name):
    name = str(name or "").strip()
    if not name:
        return ""

    name = os.path.basename(name)
    name = name.replace("\r", "_").replace("\n", "_").replace('"', "_")
    name = name.replace("/", "_").replace("\\", "_")

    if not name.lower().endswith((".jpg", ".jpeg")):
        name += ".jpg"

    return name


def make_snapshot_filename(metadata, gateway_id, channel, server_seq):
    filename = sanitize_filename((metadata or {}).get("filename", ""))

    if filename:
        return filename

    ts = datetime.now(BJ_TZ).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return "SNAP_{}_{}_{}_{}.jpg".format(channel, gateway_id, ts, server_seq)


# ======================== JsonHub (for non-satellite JSON) ========================

class JsonHub:
    def __init__(self, name, source_port, file_path=None):
        self.name = name
        self.source_port = source_port
        self.file_path = file_path
        self.cond = threading.Condition()
        self.seq = 0
        self.avg_fps = 0.0
        self.text = ""
        self.obj = None
        self.update_time = 0.0
        self.history = deque(maxlen=1024)
        self._last_write_log = 0.0

    def update(self, text, obj):
        with self.cond:
            self.seq += 1
            self.text = text
            self.obj = obj
            self.update_time = time.time()
            self.history.append({
                "seq": self.seq,
                "text": text,
                "obj": obj,
                "update_time": self.update_time,
                "source_port": self.source_port,
                "name": self.name,
            })
            self.cond.notify_all()

        if self.file_path:
            self.write_latest_to_file(text)

    def write_latest_to_file(self, text):
        """
        Write latest JSON to file.

        Use tmp + os.replace to avoid half-written files.
        Print success log at most once per second.
        Print explicit ERROR log on failure.
        """
        try:
            directory = os.path.dirname(self.file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            tmp_path = self.file_path + ".tmp"

            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n")

            os.replace(tmp_path, self.file_path)

            now = time.time()
            if now - self._last_write_log >= 1.0:
                log("[JSON-FILE][{}] written {} bytes -> {}".format(
                    self.name,
                    len(text.encode("utf-8")),
                    self.file_path,
                ))
                self._last_write_log = now

        except Exception as exc:
            log("[JSON-FILE][{}][ERROR] write failed! path={!r} err={}".format(
                self.name,
                self.file_path,
                exc,
            ))

    def current_seq(self):
        with self.cond:
            return self.seq

    def get_latest(self):
        with self.cond:
            if not self.text:
                return None
            return {
                "seq": self.seq,
                "text": self.text,
                "obj": self.obj,
                "update_time": self.update_time,
                "source_port": self.source_port,
                "name": self.name,
            }

    def wait_next(self, last_seq, timeout=15.0):
        with self.cond:
            if self.seq <= last_seq:
                self.cond.wait(timeout=timeout)

            if self.seq <= last_seq:
                return None

            for item in self.history:
                if item["seq"] > last_seq:
                    return dict(item)
            return None


# ======================== HTTP Server base ========================

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def send_cors_headers(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Cache-Control, X-Requested-With, Last-Event-ID"
    )
    handler.send_header(
        "Access-Control-Expose-Headers",
        "Content-Disposition, X-Image-Name, X-Gateway-ID, X-Channel, X-Source-Port, X-Frame-Seq, X-Json-Seq"
    )


# ======================== MJPEG HTTP Handler ========================

def make_mjpeg_handler(video_hub):
    class MjpegHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_OPTIONS(self):
            self.send_response(204)
            send_cors_headers(self)
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path

            if path != "/stream":
                self.send_response(404)
                send_cors_headers(self)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            try:
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary={}".format(MJPEG_BOUNDARY)
                )
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "close")
                send_cors_headers(self)
                self.end_headers()

                log("[HTTP][{}] MJPEG client connected: {}".format(
                    video_hub.name,
                    self.client_address
                ))

                # Do not push old frame when frontend reconnects.
                last_seq = video_hub.current_seq()

                while True:
                    item = video_hub.wait_next(last_seq, timeout=15.0)
                    if item is None:
                        continue

                    last_seq = item["seq"]
                    jpeg = item["jpeg"]
                    if not jpeg:
                        continue

                    part_header = (
                        "--{}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        "Content-Length: {}\r\n"
                        "X-Gateway-ID: {}\r\n"
                        "X-Channel: {}\r\n"
                        "X-Frame-Seq: {}\r\n"
                        "\r\n"
                    ).format(
                        MJPEG_BOUNDARY,
                        len(jpeg),
                        item.get("gateway_id", ""),
                        video_hub.name,
                        item.get("server_seq", 0),
                    ).encode("utf-8")

                    self.wfile.write(part_header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError, OSError):
                log("[HTTP][{}] MJPEG client disconnected: {}".format(
                    video_hub.name,
                    self.client_address
                ))

    return MjpegHandler


# ======================== SNAP HTTP Handler ========================

def make_snapshot_handler(snapshot_store):
    class SnapshotHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_OPTIONS(self):
            self.send_response(204)
            send_cors_headers(self)
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path

            if path not in ("/latest.jpg", "/latest.jpeg", "/"):
                self.send_response(404)
                send_cors_headers(self)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            item = snapshot_store.get_latest()

            if not is_fresh_for_request(item, self.path, SNAPSHOT_MAX_AGE_SECONDS):
                send_no_content(self)
                return

            jpeg = item["jpeg"]
            filename = sanitize_filename(item.get("filename", "")) or "latest.jpg"

            try:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Content-Disposition", 'inline; filename="{}"'.format(filename))
                self.send_header("X-Image-Name", filename)
                self.send_header("X-Gateway-ID", item.get("gateway_id", ""))
                self.send_header("X-Channel", item.get("channel", ""))
                self.send_header("X-Source-Port", str(item.get("source_port", "")))
                send_cors_headers(self)
                self.end_headers()
                self.wfile.write(jpeg)
                self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    return SnapshotHandler


# ======================== JSON HTTP Handler (for non-satellite JSON) ========================

def make_json_handler(json_hub):
    class JsonHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_OPTIONS(self):
            self.send_response(204)
            send_cors_headers(self)
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path

            if path in ("/", "/latest.json"):
                self.handle_latest_json()
                return

            if path == "/events":
                self.handle_sse()
                return

            self.send_response(404)
            send_cors_headers(self)
            self.end_headers()
            self.wfile.write(b"Not Found")

        def handle_latest_json(self):
            item = json_hub.get_latest()

            if not is_fresh_for_request(item, self.path, JSON_MAX_AGE_SECONDS):
                send_no_content(self)
                return

            body = item["text"].encode("utf-8")

            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("X-Json-Seq", str(item["seq"]))
                self.send_header("X-Source-Port", str(item["source_port"]))
                self.send_header("X-Channel", item["name"])
                send_cors_headers(self)
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def handle_sse(self):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                send_cors_headers(self)
                self.end_headers()

                log("[HTTP][{}] JSON SSE client connected: {}".format(
                    json_hub.name,
                    self.client_address
                ))

                self.wfile.write(b": connected\n\n")
                self.wfile.flush()

                # Do not push old JSON when frontend reconnects.
                last_seq = json_hub.current_seq()

                while True:
                    item = json_hub.wait_next(last_seq, timeout=15.0)

                    if item is None:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue

                    last_seq = item["seq"]
                    text = item["text"]

                    msg = "id: {}\n".format(last_seq)
                    msg += "event: json\n"

                    for line in text.splitlines() or [""]:
                        msg += "data: {}\n".format(line)

                    msg += "\n"

                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError, OSError):
                log("[HTTP][{}] JSON SSE client disconnected: {}".format(
                    json_hub.name,
                    self.client_address
                ))

    return JsonHandler


def start_http_server(port, handler_cls, name):
    server = ThreadingHTTPServer((FRONTEND_LISTEN_HOST, port), handler_cls)
    log("[HTTP][{}] listening on {}:{}".format(name, FRONTEND_LISTEN_HOST, port))
    server.serve_forever()


# ======================== Framed upstream pull ========================

def server_framed_pull_loop(server_host, server_port, video_hub, snapshot_store_map):
    ch = CHANNEL_NAME.get(server_port, str(server_port))
    snap_output_port = SNAP_FORWARD_MAP.get(server_port)
    snapshot_store = snapshot_store_map.get(snap_output_port)

    recv_video = 0
    recv_snap = 0
    ignored_snap = 0
    ignored_video = 0

    last_video_report_count = 0
    last_report_time = time.time()

    while True:
        sock = None

        try:
            log("[UPSTREAM][{}] connecting to {}:{} ...".format(ch, server_host, server_port))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            set_socket_options(sock)
            sock.settimeout(UPSTREAM_CONNECT_TIMEOUT_SECONDS)
            sock.connect((server_host, server_port))
            sock.settimeout(None)

            log("[UPSTREAM][{}] connected to {}:{} | VID0 -> {} | SNAP -> {}".format(
                ch,
                server_host,
                server_port,
                VIDEO_FORWARD_MAP.get(server_port, "ignored"),
                snap_output_port if snap_output_port is not None else "ignored",
            ))

            while True:
                packet = read_server_forward_packet(sock)

                if packet is None:
                    raise ConnectionError("server closed connection")

                gateway_id = packet["gateway_id"]
                frame_seq = packet["frame_seq"]
                packet_type = packet["packet_type"]
                metadata = packet["metadata"]
                jpeg = packet["jpeg"]

                if not jpeg:
                    continue

                if not is_jpeg(jpeg):
                    log("[UPSTREAM][{}][WARN] payload is not JPEG | type={} | seq={}".format(
                        ch, packet_type, frame_seq
                    ))
                    continue

                if packet_type == "VID0":
                    recv_video += 1

                    if video_hub is None:
                        ignored_video += 1
                        if ignored_video % LOG_EVERY_VIDEO == 1:
                            log("[UPSTREAM][{}][VID0][WARN] received VID0 on non-video port {} | ignored".format(
                                ch, server_port
                            ))
                        continue

                    video_hub.update(jpeg, metadata, gateway_id, frame_seq)

                    if recv_video % LOG_EVERY_VIDEO == 0:
                        log(
                            "[UPSTREAM][{}][VID0] recv={} | gateway={} | seq={} | "
                            "frame_id={} | jpeg={} bytes | video_http={}".format(
                                ch,
                                recv_video,
                                gateway_id,
                                frame_seq,
                                metadata.get("frame_id", ""),
                                len(jpeg),
                                VIDEO_FORWARD_MAP.get(server_port, ""),
                            )
                        )

                elif packet_type == "SNAP":
                    recv_snap += 1

                    if snapshot_store is None:
                        ignored_snap += 1
                        if ignored_snap % 50 == 1:
                            log("[UPSTREAM][{}][SNAP] ignored | source_port={} | ignored_count={}".format(
                                ch, server_port, ignored_snap
                            ))
                        continue

                    snapshot_store.update(
                        jpeg=jpeg,
                        metadata=metadata,
                        gateway_id=gateway_id,
                        channel=ch,
                        source_port=server_port,
                        server_seq=frame_seq,
                    )

                    log(
                        "[UPSTREAM][{}][SNAP] recv={} | gateway={} | seq={} | "
                        "filename={} | event={} | event_id={} | jpeg={} bytes | snap_http={}".format(
                            ch,
                            recv_snap,
                            gateway_id,
                            frame_seq,
                            metadata.get("filename", ""),
                            metadata.get("event", ""),
                            metadata.get("event_id", ""),
                            len(jpeg),
                            snap_output_port,
                        )
                    )

                now = time.time()
                dt = now - last_report_time
                if dt >= FPS_REPORT_INTERVAL:
                    fps = (recv_video - last_video_report_count) / dt
                    if video_hub is not None:
                        video_hub.avg_fps = fps
                    log("[UPSTREAM][{}][FPS] video_fps={:.1f} | total_video={} | total_snap={} | ignored_snap={}".format(
                        ch, fps, recv_video, recv_snap, ignored_snap
                    ))
                    last_report_time = now
                    last_video_report_count = recv_video

        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
            log("[UPSTREAM][{}][WARN] disconnected/error: {}".format(ch, exc))

        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

            log("[UPSTREAM][{}] reconnect after {:.1f}s".format(ch, RECONNECT_INTERVAL))
            time.sleep(RECONNECT_INTERVAL)


# ======================== JSON upstream pull (non-satellite) ========================

def server_json_pull_loop(server_host, server_port, json_hub):
    ch = CHANNEL_NAME.get(server_port, str(server_port))
    frontend_port = JSON_FORWARD_MAP[server_port]

    total_json = 0
    last_total_json = 0
    last_report_time = time.time()

    last_json_text = ""
    last_json_bytes = 0
    last_json_time = 0.0

    while True:
        sock = None

        try:
            log("[JSON-UP][{}] connecting to {}:{} ...".format(ch, server_host, server_port))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            set_socket_options(sock)
            sock.settimeout(UPSTREAM_CONNECT_TIMEOUT_SECONDS)
            sock.connect((server_host, server_port))
            sock.settimeout(None)

            log("[JSON-UP][{}] connected to {}:{} | JSON -> http://{}:{}/events".format(
                ch,
                server_host,
                server_port,
                FRONTEND_LISTEN_HOST,
                frontend_port,
            ))

            f = sock.makefile("rb")

            while True:
                # 修复缩进：添加正确缩进（8个空格）
                line = f.readline(MAX_JSON_LINE_SIZE + 1)

                if not line:
                    raise ConnectionError("server JSON channel closed")

                if len(line) > MAX_JSON_LINE_SIZE:
                    log("[JSON-UP][{}][WARN] JSON line too large | bytes={}".format(
                        ch, len(line)
                    ))
                    continue

                text = line.decode("utf-8", errors="replace").strip()

                if not text:
                    continue

                try:
                    obj = json.loads(text)
                except Exception as exc:
                    log("[JSON-UP][{}][WARN] invalid JSON | raw={!r} | error={}".format(
                        ch, text, exc
                    ))
                    continue

                total_json += 1
                last_json_text = text
                last_json_bytes = len(text.encode("utf-8"))
                last_json_time = time.time()

                json_hub.update(text, obj)

                # 11410 已移出此循环，故删除原来的特殊日志

                now = time.time()
                dt = now - last_report_time

                if dt >= JSON_REPORT_INTERVAL:
                    rate = (total_json - last_total_json) / dt

                    # 日志长度保护，避免单条 JSON 太长刷屏。
                    display_json = last_json_text
                    if len(display_json) > 500:
                        display_json = display_json[:500] + "...<truncated>"

                    log(
                        "[JSON-UP][{}][DETAIL] rate={:.2f} json/s | total={} | "
                        "bytes={} | last_age={:.2f}s | http_port={} | last_json={}".format(
                            ch,
                            rate,
                            total_json,
                            last_json_bytes,
                            now - last_json_time,
                            frontend_port,
                            display_json,
                        )
                    )

                    last_report_time = now
                    last_total_json = total_json

        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError, ValueError) as exc:
            log("[JSON-UP][{}][WARN] disconnected/error: {}".format(ch, exc))

        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

            log("[JSON-UP][{}] reconnect after {:.1f}s".format(ch, RECONNECT_INTERVAL))
            time.sleep(RECONNECT_INTERVAL)


# ======================== BaoTong HF receive ========================

def setup_baotong_output_interface():
    cmds = [
        ["ip", "link", "set", BAOTONG_OUTPUT_IFACE, "up"],
        ["ip", "addr", "flush", "dev", BAOTONG_OUTPUT_IFACE],
        ["ip", "addr", "add", BAOTONG_OUTPUT_ADDR, "dev", BAOTONG_OUTPUT_IFACE],
    ]
    for cmd in cmds:
        rc = os.system(" ".join(cmd) + " >/dev/null 2>&1")
        if rc != 0:
            log("[BAOTONG-RX][WARN] interface setup command failed: {}".format(" ".join(cmd)))
    log("[BAOTONG-RX] output interface {} configured as {}".format(
        BAOTONG_OUTPUT_IFACE, BAOTONG_OUTPUT_ADDR
    ))


def baotong_extract_one_payload(buffer):
    head_index = buffer.find(BAOTONG_FRAME_HEAD)
    if head_index < 0:
        return None, b""
    if head_index > 0:
        log("[BAOTONG-RX][WARN] discard {} dirty bytes before frame head".format(head_index))
        buffer = buffer[head_index:]
    if len(buffer) < 8:
        return None, buffer

    length = struct.unpack(">I", buffer[2:6])[0]
    if length <= 0 or length > BAOTONG_MAX_PAYLOAD_SIZE:
        log("[BAOTONG-RX][WARN] invalid payload length {}, drop one byte".format(length))
        return None, buffer[1:]

    total = 2 + 4 + length + 2
    if len(buffer) < total:
        return None, buffer
    if buffer[total - 2:total] != BAOTONG_FRAME_TAIL:
        log("[BAOTONG-RX][WARN] bad frame tail, drop one byte")
        return None, buffer[1:]
    return buffer[6:6 + length], buffer[total:]


class BaoTongReceiveServer:
    def __init__(self, host, port, json_hub):
        self.host = host
        self.port = int(port)
        self.json_hub = json_hub
        self.tx_snr = None
        self.rx_snr = None
        self.hf_status = None
        self.detect_callback = None
        self.linkstatus_callback = None
        self.response_callback = None
        self.response_forward_filter = None
        self._connection = None
        self._connection_addr = None
        self._connection_lock = threading.Lock()
        self._send_lock = threading.Lock()

    def set_linkstatus_callback(self, callback):
        self.linkstatus_callback = callback

    def set_detect_callback(self, callback):
        self.detect_callback = callback

    def set_response_callback(self, callback):
        self.response_callback = callback

    def set_response_forward_filter(self, callback):
        """Set the business-response filter used only for frontend output."""
        self.response_forward_filter = callback

    def _notify_response(self, message):
        """Always deliver a radio response to the call state machine."""
        if self.response_callback is None:
            return
        try:
            self.response_callback(dict(message))
        except Exception as exc:
            log("[BAOTONG-RX][WARN] response callback failed: {}".format(exc))

    def _should_forward_response(self, message):
        """Return whether a business response may enter the frontend stream."""
        if self.response_forward_filter is None:
            return True
        try:
            return bool(self.response_forward_filter(dict(message)))
        except Exception as exc:
            log("[BAOTONG-RX][WARN] response forward filter failed; packet dropped: {}".format(
                exc
            ))
            return False

    def is_connected(self):
        with self._connection_lock:
            return self._connection is not None

    def connection_info(self):
        with self._connection_lock:
            return {
                "connected": self._connection is not None,
                "peer": self._connection_addr,
                "hf_status": self.hf_status,
            }

    def _register_connection(self, conn, addr):
        with self._connection_lock:
            previous = self._connection
            self._connection = conn
            self._connection_addr = "{}:{}".format(addr[0], addr[1])
        if previous is not None and previous is not conn:
            try:
                previous.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                previous.close()
            except OSError:
                pass

    def _unregister_connection(self, conn):
        with self._connection_lock:
            if self._connection is conn:
                self._connection = None
                self._connection_addr = None

    def send_message(self, message):
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > BAOTONG_MAX_PAYLOAD_SIZE:
            raise ValueError("BaoTong payload too large: {} bytes".format(len(payload)))

        frame = (
            BAOTONG_FRAME_HEAD
            + struct.pack(">I", len(payload))
            + payload
            + BAOTONG_FRAME_TAIL
        )
        with self._send_lock:
            with self._connection_lock:
                conn = self._connection
            if conn is None:
                raise ConnectionError("BaoTong industrial PC is not connected")
            try:
                conn.sendall(frame)
            except OSError:
                self._unregister_connection(conn)
                raise

        log("[BAOTONG-TX] {}".format(payload.decode("utf-8")))

    def serve_forever(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(4)
        log("[BAOTONG-RX] listening on {}:{}".format(self.host, self.port))
        while True:
            conn, addr = server.accept()
            log("[BAOTONG-RX] client connected: {}:{}".format(addr[0], addr[1]))
            t = threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True)
            t.start()

    def handle_client(self, conn, addr):
        buffer = b""
        self._register_connection(conn, addr)
        try:
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buffer += data
                    while True:
                        payload, buffer = baotong_extract_one_payload(buffer)
                        if payload is None:
                            break
                        self.handle_payload(payload, addr)
        except Exception as exc:
            log("[BAOTONG-RX][ERROR] client {}:{} exception: {}".format(addr[0], addr[1], exc))
        finally:
            self._unregister_connection(conn)
            log("[BAOTONG-RX] client disconnected: {}:{}".format(addr[0], addr[1]))

    def handle_payload(self, payload, addr):
        text = payload.decode("utf-8", errors="ignore")
        try:
            message = json.loads(text)
        except ValueError:
            log("[BAOTONG-RX][WARN] invalid JSON dropped: {}".format(text))
            return

        if str(message.get("detect", "")).strip() == "0":
            log("[BAOTONG-RX] detect=0 received")
            if self.detect_callback is not None:
                try:
                    self.detect_callback(dict(message))
                except Exception as exc:
                    log("[BAOTONG-RX][WARN] detect callback failed: {}".format(exc))
            return

        msg_type = str(message.get("type", "")).lower()
        if msg_type == "heartbeat":
            self.hf_status = message.get("status")
            log("[BAOTONG-RX] heartbeat status={}".format(message.get("status")))
            return

        if msg_type == "connect":
            log("[BAOTONG-RX] connect status={}".format(message.get("status")))
            return

        if msg_type == "linkstatus":
            self.tx_snr = message.get("tx_snr")
            self.rx_snr = message.get("rx_snr")
            self.hf_status = message.get("status")
            log("[BAOTONG-RX] linkstatus tx_snr={} rx_snr={}".format(self.tx_snr, self.rx_snr))

            if self.linkstatus_callback is not None:
                try:
                    self.linkstatus_callback(dict(message))
                except Exception as exc:
                    log("[BAOTONG-RX][WARN] linkstatus callback failed: {}".format(exc))

            out = {
                "gateway": BAOTONG_OUTPUT_GATEWAY,
                "type": "linkstatus",
                "tx_snr": self.tx_snr,
                "rx_snr": self.rx_snr,
                "timestamp": str(message.get("timestamp") or bj_time_str()[:19]),
            }
            if "status" in message:
                out["status"] = message.get("status")
            if "probe_version" in message:
                out["probe_version"] = message.get("probe_version")
            out = sanitize_snr_for_frontend(out)
            out_text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            self.json_hub.update(out_text, out)
            log("[BAOTONG-RX] linkstatus forwarded to HTTP {} latest/events: {}".format(
                BAOTONG_OUTPUT_HTTP_PORT, out_text
            ))
            return

        if "caller_id" in message and "fire" in message:
            fire = str(message.get("fire", "")).strip().lower()
            if fire not in ("true", "false"):
                log("[BAOTONG-RX] empty/invalid fire packet dropped: {}".format(text))
                return

            # The response must always reach the internal call state machine so
            # an in-flight call can finish safely.  Frontend publication is a
            # separate decision based on the latest requested fixed-radio mode.
            self._notify_response(message)
            if not self._should_forward_response(message):
                return

            out = {
                "gateway": BAOTONG_OUTPUT_GATEWAY,
                "caller_id": str(message.get("caller_id", "")).strip(),
                "fire": fire,
                "timestamp": str(message.get("timestamp") or bj_time_str()[:19]),
            }
            if "scene" in message:
                out["scene"] = str(message.get("scene"))
            out_text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            self.json_hub.update(out_text, out)
            log("[BAOTONG-RX] forwarded to HTTP {} latest/events: {}".format(
                BAOTONG_OUTPUT_HTTP_PORT, out_text
            ))
            return

        if "caller_id" in message and "windspeed" in message:
            self._notify_response(message)
            if not self._should_forward_response(message):
                return

            out = {
                "gateway": BAOTONG_OUTPUT_GATEWAY,
                "caller_id": str(message.get("caller_id", "")).strip(),
                "windspeed": str(message.get("windspeed")),
                "timestamp": str(message.get("timestamp") or bj_time_str()[:19]),
            }
            out_text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
            self.json_hub.update(out_text, out)
            log("[BAOTONG-RX] windspeed forwarded to HTTP {} latest/events: {}".format(
                BAOTONG_OUTPUT_HTTP_PORT, out_text
            ))
            return

        log("[BAOTONG-RX] unknown packet dropped: {}".format(text))


def start_baotong_receive_server(server):
    setup_baotong_output_interface()
    server.serve_forever()


class ShortwaveCallController:
    def __init__(self, transport):
        self.transport = transport
        self._condition = threading.Condition()
        self._mode_condition = threading.Condition()
        self._detect_version = 0
        self._detect_state = 0
        self._detect_set_at = 0.0
        self._detect_reset_requested = False
        self._last_detect = None
        self._linkstatus_version = 0
        self._last_linkstatus = None
        self._linkstatus_events = deque(maxlen=100)
        self._response_version = 0
        self._responses = deque(maxlen=100)
        self._state_lock = threading.Lock()
        self._next_command_id = 1
        self._desired_db = "-1"
        self._active_db = "-1"
        self._mode_version = 0
        self._poll_station_index = 0
        self._poll_attempt_index = 0
        self._fixed_cycle_count = 0
        # 方案A死台跳过：当前电台的连续探测失败计数；探测成功即清零。
        self._poll_probe_failures = 0
        self._state = {
            "state": "silent",
            "command_id": 0,
            "db": "-1",
            "desired_db": "-1",
            "active_db": "-1",
            "switching_pending": False,
            "callee_id": None,
            "attempt": 0,
            "total_attempts": 0,
            "last_result": None,
        }

    def start(self):
        threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="shortwave-call-worker",
        ).start()
        threading.Thread(
            target=self._detect_reset_watchdog,
            daemon=True,
            name="shortwave-detect-reset-watchdog",
        ).start()

    def submit(self, db_value):
        db = str(db_value).strip()
        if db not in ("-1", "0", "1", "2", "3", "4", "5"):
            raise ValueError("db must be one of -1,0,1,2,3,4,5")

        with self._mode_condition:
            command_id = self._next_command_id
            self._next_command_id += 1
            previous_db = self._desired_db
            active_db = self._active_db
            self._desired_db = db
            self._mode_version += 1
            mode_version = self._mode_version
            switching_pending = active_db != "-1" and active_db != db
            self._mode_condition.notify_all()

        # wake up _wait_for_response so it can detect db change and abort
        with self._condition:
            self._condition.notify_all()

        command = {
            "command_id": command_id,
            "db": db,
            "previous_db": previous_db,
            "active_db": active_db,
            "switching_pending": switching_pending,
            "mode_version": mode_version,
        }
        self._set_state(
            command_id=command_id,
            desired_db=db,
            switching_pending=switching_pending,
        )
        log("[SHORTWAVE] mode requested command_id={} db={} previous_db={} "
            "active_db={} switching_pending={}".format(
                command_id,
                db,
                previous_db,
                active_db,
                switching_pending,
            )
        )
        return command

    def snapshot(self):
        with self._state_lock:
            state = dict(self._state)
        with self._mode_condition:
            state["desired_db"] = self._desired_db
            state["active_db"] = self._active_db
            state["mode_version"] = self._mode_version
            state["switching_pending"] = (
                self._active_db != "-1"
                and self._desired_db != self._active_db
            )
        state["queue_depth"] = 0
        state["baotong"] = self.transport.connection_info()
        with self._condition:
            state["detect"] = self._detect_state
        return sanitize_snr_for_frontend(state)

    def should_forward_response(self, message):
        """Filter frontend business data by the latest requested fixed db.

        db=0 is polling mode and deliberately bypasses this filter.  For a
        fixed db, desired_db is used instead of active_db so a late response
        from the previous radio cannot appear on the newly selected page.
        Silent mode has no selected radio, therefore business data is hidden.
        This method controls frontend publication only; notify_response still
        receives every valid packet to complete any in-flight call.
        """
        caller_id = str(message.get("caller_id", "")).strip()
        with self._mode_condition:
            desired_db = self._desired_db

        if desired_db == "0":
            return True

        expected_caller_id = SHORTWAVE_CALLEE_MAP.get(desired_db)
        allowed = bool(
            expected_caller_id
            and caller_id == expected_caller_id
        )
        if not allowed:
            log(
                "[SHORTWAVE-FILTER] frontend packet dropped: "
                "desired_db={} expected_caller_id={} caller_id={}".format(
                    desired_db,
                    expected_caller_id or "none",
                    caller_id or "missing",
                )
            )
        return allowed

    def notify_detect(self, message):
        with self._condition:
            self._detect_state = 0
            self._detect_set_at = 0.0
            self._detect_reset_requested = False
            self._detect_version += 1
            self._last_detect = dict(message)
            self._condition.notify_all()

    def _detect_reset_watchdog(self):
        while True:
            with self._condition:
                while (
                    self._detect_state != 1
                    or not self._detect_set_at
                    or self._detect_reset_requested
                ):
                    self._condition.wait()

                remaining = (
                    self._detect_set_at
                    + DETECT_STALE_RESET_SECONDS
                    - time.time()
                )
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue

                self._detect_reset_requested = True

            try:
                self.transport.send_message({"reset": 1})
                log(
                    "[SHORTWAVE] radio occupation exceeded {:.0f}s; "
                    "reset requested from BaoTong industrial PC".format(
                        DETECT_STALE_RESET_SECONDS
                    )
                )
            except Exception as exc:
                log(
                    "[SHORTWAVE][WARN] failed to request radio reset: {}".format(
                        exc
                    )
                )
                with self._condition:
                    if self._detect_state == 1:
                        self._detect_reset_requested = False
                        self._condition.wait(
                            timeout=DETECT_RESET_RETRY_SECONDS
                        )

    def notify_linkstatus(self, message):
        with self._condition:
            self._linkstatus_version += 1
            self._last_linkstatus = dict(message)
            self._linkstatus_events.append((
                self._linkstatus_version,
                dict(message),
            ))
            self._condition.notify_all()

    def notify_response(self, message):
        caller_id = str(message.get("caller_id", "")).strip()
        if not caller_id:
            return
        with self._condition:
            self._response_version += 1
            self._responses.append((
                self._response_version,
                caller_id,
                dict(message),
            ))
            self._condition.notify_all()

    def _wait_until_detect_idle(self):
        """Wait indefinitely for a real detect=0 report from the controller."""
        with self._condition:
            while self._detect_state != 0:
                if self._desired_db != self._active_db:
                    return None
                self._condition.wait()
            self._detect_state = 1
            self._detect_set_at = time.time()
            self._detect_reset_requested = False
            self._condition.notify_all()
            return {
                "state": self._detect_state,
                "version": self._detect_version,
                "last": dict(self._last_detect or {}),
            }

    def _linkstatus_receive_version(self):
        with self._condition:
            return self._linkstatus_version

    def _wait_for_linkstatus(self, after_receive_version, timeout):
        """Accept the first new linkstatus received after this probe starts."""
        deadline = time.time() + timeout
        with self._condition:
            while True:
                for receive_version, message in self._linkstatus_events:
                    if receive_version > after_receive_version:
                        return dict(message)
                if self._desired_db != self._active_db:
                    return None
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _wait_for_response(self, previous_version, callee_id, timeout):
        expected_caller_id = str(callee_id).strip()
        deadline = time.time() + timeout
        with self._condition:
            while True:
                for version, caller_id, message in self._responses:
                    if version > previous_version and caller_id == expected_caller_id:
                        return dict(message)
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    @staticmethod
    def _link_is_usable(status):
        if not status:
            return False, "link_test_timeout"
        if str(status.get("status", "")).lower() != "hf_active":
            return False, "hf_inactive"
        try:
            tx_snr = float(status.get("tx_snr"))
            rx_snr = float(status.get("rx_snr"))
        except (TypeError, ValueError):
            return False, "invalid_snr"
        if tx_snr <= SHORTWAVE_SNR_THRESHOLD or rx_snr <= SHORTWAVE_SNR_THRESHOLD:
            return False, "snr_too_low"
        return True, "ok"

    def _call_once(self, callee_id):
        detect = self._wait_until_detect_idle()
        if detect is None:
            return {
                "success": False,
                "reason": "mode_changed",
                "completed_call": False,
            }
        previous_receive_version = self._linkstatus_receive_version()

        self.transport.send_message({
            "callee_id": callee_id,
            "link_test": "test",
            "timestamp": bj_time_str()[:19],
        })

        linkstatus = self._wait_for_linkstatus(
            previous_receive_version,
            SHORTWAVE_LINK_TEST_TIMEOUT,
        )
        usable, reason = self._link_is_usable(linkstatus)
        if not usable:
            return {
                "success": False,
                "reason": reason,
                "linkstatus": linkstatus,
                "completed_call": False,
            }

        with self._condition:
            previous_response_version = self._response_version
        self.transport.send_message({
            "callee_id": callee_id,
            "fire": SHORTWAVE_CALL_FIRE,
            "timestamp": bj_time_str()[11:16],
        })
        # The response window starts when the fire template packet is sent,
        # not when the probe started: the probe round trip can consume most
        # of the old window and doom the SMS exchange before it begins.
        response = self._wait_for_response(
            previous_response_version,
            callee_id,
            SHORTWAVE_RESPONSE_TIMEOUT,
        )
        if response is None:
            return {
                "success": False,
                "reason": "response_timeout",
                "linkstatus": linkstatus,
                "response": None,
                "completed_call": True,
            }
        return {
            "success": True,
            "reason": "response_received",
            "linkstatus": linkstatus,
            "response": response,
            "completed_call": True,
        }

    def _set_state(self, **updates):
        with self._state_lock:
            self._state.update(updates)

    def _wait_for_non_silent_mode(self):
        with self._mode_condition:
            while self._desired_db == "-1":
                self._active_db = "-1"
                self._mode_condition.wait()
            self._active_db = self._desired_db
            return self._active_db

    def _activate_mode(self, db):
        self._poll_station_index = 0
        self._poll_attempt_index = 0
        self._fixed_cycle_count = 0
        self._poll_probe_failures = 0
        self._set_state(
            state="communicating",
            db=db,
            active_db=db,
            desired_db=db,
            switching_pending=False,
            callee_id=None,
            attempt=0,
            total_attempts=(
                SHORTWAVE_POLL_REPEATS if db == "0" else None
            ),
            last_result=None,
        )
        log("[SHORTWAVE] mode activated db={}".format(db))

    def _synchronize_mode_before_cycle(self, active_db):
        with self._mode_condition:
            desired_db = self._desired_db
            if desired_db == active_db:
                return active_db, False
            self._active_db = desired_db

        log("[SHORTWAVE] pre-cycle mode update db={} -> db={}".format(
            active_db,
            desired_db,
        ))
        if desired_db == "-1":
            self._set_state(
                state="silent",
                db="-1",
                active_db="-1",
                desired_db="-1",
                switching_pending=False,
                callee_id=None,
                attempt=0,
                total_attempts=0,
            )
        else:
            self._activate_mode(desired_db)
        return desired_db, True

    def _current_target(self, db):
        if db == "0":
            callee_ids = list(SHORTWAVE_CALLEE_MAP.values())
            return (
                callee_ids[self._poll_station_index],
                self._poll_attempt_index + 1,
                SHORTWAVE_POLL_REPEATS,
            )
        return SHORTWAVE_CALLEE_MAP[db], self._fixed_cycle_count + 1, None

    def _advance_poll_target(self):
        self._poll_attempt_index += 1
        if self._poll_attempt_index >= SHORTWAVE_POLL_REPEATS:
            self._poll_attempt_index = 0
            self._poll_station_index = (
                self._poll_station_index + 1
            ) % len(SHORTWAVE_CALLEE_MAP)

    def _probe_failure_skip_needed(self, result):
        """方案A：根据本轮结果维护连续探测失败计数。

        探测成功（completed_call=True 说明模板报文已发出，探测必然
        通过）清零计数；探测阶段失败（link_test_timeout / hf_inactive /
        snr_too_low / invalid_snr）累加。连续失败达到
        SHORTWAVE_DEAD_STATION_THRESHOLD 时判定该台本轮不可达，
        跳到下一个电台并把 attempt、失败计数一并复位。
        """
        if result.get("completed_call"):
            self._poll_probe_failures = 0
            return False
        if result.get("reason") == "mode_changed":
            # 模式切换打断，不算电台的账
            return False

        self._poll_probe_failures += 1
        if self._poll_probe_failures < SHORTWAVE_DEAD_STATION_THRESHOLD:
            return False

        callee_ids = list(SHORTWAVE_CALLEE_MAP.values())
        dead_callee = callee_ids[self._poll_station_index]
        self._poll_probe_failures = 0
        self._poll_attempt_index = 0
        self._poll_station_index = (
            self._poll_station_index + 1
        ) % len(callee_ids)
        log(
            "[SHORTWAVE] station {} unreachable after {} consecutive "
            "probe failures; skip to next station".format(
                dead_callee,
                SHORTWAVE_DEAD_STATION_THRESHOLD,
            )
        )
        return True

    def _finish_cycle_and_choose_mode(self, active_db, result):
        with self._mode_condition:
            desired_db = self._desired_db
            if desired_db != active_db:
                old_db = active_db
                self._active_db = desired_db
                switching = True
            else:
                old_db = active_db
                switching = False

        if switching:
            log("[SHORTWAVE] cycle finished; switch db={} -> db={} result={}".format(
                old_db,
                desired_db,
                result,
            ))
            if desired_db == "-1":
                self._set_state(
                    state="silent",
                    db="-1",
                    active_db="-1",
                    desired_db="-1",
                    switching_pending=False,
                    callee_id=None,
                    attempt=0,
                    total_attempts=0,
                    last_result=result,
                )
            return desired_db, True

        if result.get("completed_call"):
            if active_db == "0":
                self._advance_poll_target()
            else:
                self._fixed_cycle_count += 1
            return active_db, False

        # completed_call=False：探测失败路径。轮询模式下先过死台判定
        # （连续 K 次失败强制换台），其余情况留在原台由调用方延迟重试。
        if active_db == "0" and self._probe_failure_skip_needed(result):
            return active_db, False
        return active_db, False

    def _wait_before_retry_if_needed(self, active_db, result):
        if result.get("success"):
            return
        if result.get("reason") == "response_timeout":
            return
        with self._mode_condition:
            if self._desired_db == active_db:
                self._mode_condition.wait(timeout=SHORTWAVE_RETRY_DELAY)

    def _worker_loop(self):
        active_db = "-1"
        while True:
            if active_db == "-1":
                self._set_state(
                    state="silent",
                    db="-1",
                    active_db="-1",
                    callee_id=None,
                    attempt=0,
                    total_attempts=0,
                    switching_pending=False,
                )
                active_db = self._wait_for_non_silent_mode()
                self._activate_mode(active_db)

            active_db, synchronized = self._synchronize_mode_before_cycle(
                active_db
            )
            if synchronized and active_db == "-1":
                continue

            callee_id, attempt, total_attempts = self._current_target(active_db)
            with self._mode_condition:
                desired_db = self._desired_db
            self._set_state(
                state="communicating",
                db=active_db,
                active_db=active_db,
                desired_db=desired_db,
                switching_pending=(desired_db != active_db),
                callee_id=callee_id,
                attempt=attempt,
                total_attempts=total_attempts,
            )

            try:
                result = self._call_once(callee_id)
            except Exception as exc:
                result = {
                    "success": False,
                    "reason": "send_error",
                    "error": str(exc),
                    "completed_call": False,
                }

            self._set_state(last_result=result)
            log("[SHORTWAVE] db={} callee_id={} attempt={} result={}".format(
                active_db,
                callee_id,
                attempt,
                result,
            ))

            active_db, switched = self._finish_cycle_and_choose_mode(
                active_db,
                result,
            )
            if switched:
                if active_db != "-1":
                    self._activate_mode(active_db)
                continue
            self._wait_before_retry_if_needed(active_db, result)


def make_shortwave_command_handler(controller):
    class ShortwaveCommandHandler(BaseHTTPRequestHandler):
        server_version = "CoreGatewayShortwave/1.0"

        def log_message(self, fmt, *args):
            log("[SHORTWAVE-HTTP] {} - {}".format(
                self.client_address[0], fmt % args
            ))

        def _send_json(self, status_code, payload):
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            send_cors_headers(self)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            send_cors_headers(self)
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path not in ("/", "/status"):
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, controller.snapshot())

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/", "/call"):
                self._send_json(404, {"error": "not found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if content_length <= 0 or content_length > 4096:
                self._send_json(400, {"error": "JSON body is required"})
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                self._send_json(400, {"error": "invalid JSON", "detail": str(exc)})
                return
            if not isinstance(payload, dict) or "db" not in payload:
                self._send_json(400, {"error": "request must contain db"})
                return
            try:
                command = controller.submit(payload["db"])
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(202, {
                "accepted": True,
                "command_id": command["command_id"],
                "db": command["db"],
                "previous_db": command["previous_db"],
                "active_db": command["active_db"],
                "switching_pending": command["switching_pending"],
                "mode_version": command["mode_version"],
            })

    return ShortwaveCommandHandler


# ======================== Satellite Relay Module (SQLite + ACK) ========================

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class GatewayDatabase:
    def __init__(self, path, max_records=DEFAULT_MAX_RECORDS):
        self.path = os.path.abspath(path)
        self.max_records = int(max_records)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    b_peer TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_messages_received_at
                ON gateway_messages(received_at)
                """
            )
            conn.execute(
                """
                DELETE FROM gateway_messages
                WHERE id NOT IN (
                    SELECT id FROM gateway_messages
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (self.max_records,),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def canonical_json(payload):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def store(self, payload, b_peer=None):
        payload_json = self.canonical_json(payload)
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            message_id = "auto-" + digest
            payload = dict(payload)
            payload["message_id"] = message_id
            payload_json = self.canonical_json(payload)

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO gateway_messages (
                    message_id, payload_json, received_at, b_peer
                ) VALUES (?, ?, ?, ?)
                """,
                (message_id, payload_json, now_iso(), b_peer),
            )
            inserted = cursor.rowcount > 0
            row = conn.execute(
                "SELECT id FROM gateway_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            conn.execute(
                """
                DELETE FROM gateway_messages
                WHERE id NOT IN (
                    SELECT id FROM gateway_messages
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (self.max_records,),
            )
            conn.commit()
            return message_id, inserted, row["id"] if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def latest_payload(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM gateway_messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
        finally:
            conn.close()

    def rows_after(self, after_id, limit=200):
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT id, message_id, payload_json, received_at
                FROM gateway_messages
                WHERE id > ? ORDER BY id LIMIT ?
                """,
                (int(after_id), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def statistics(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS count, MIN(id) AS min_id, MAX(id) AS max_id "
                "FROM gateway_messages"
            ).fetchone()
            return {
                "total": row["count"],
                "min_id": row["min_id"],
                "max_id": row["max_id"],
                "max_records": self.max_records,
            }
        finally:
            conn.close()


class RuntimeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.b_connected = False
        self.b_peer = None
        self.last_message_id = None
        self.last_received_at = None

    def set_connection(self, connected, peer=None):
        with self.lock:
            self.b_connected = bool(connected)
            self.b_peer = peer if connected else None

    def record_message(self, message_id):
        with self.lock:
            self.last_message_id = message_id
            self.last_received_at = now_iso()

    def snapshot(self):
        with self.lock:
            return {
                "b_connected": self.b_connected,
                "b_peer": self.b_peer,
                "last_message_id": self.last_message_id,
                "last_received_at": self.last_received_at,
            }


def parse_nonnegative_int(value, default=0, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if result < 0:
        return default
    if maximum is not None:
        result = min(result, maximum)
    return result


def make_satellite_http_handler(database, runtime, stop_event):
    class SatelliteHttpHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log("HTTP-SAT %s - %s" % (self.client_address[0], fmt % args))

        def common_headers(self):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")

        def send_json(self, status, payload):
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.common_headers()
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path in ("/", "/latest.json"):
                payload = database.latest_payload()
                if payload is None:
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.common_headers()
                    self.end_headers()
                else:
                    self.send_json(200, payload)
                return

            if path == "/health":
                result = {
                    "status": "ok",
                    "time": now_iso(),
                    "database": database.path,
                }
                result.update(database.statistics())
                result.update(runtime.snapshot())
                self.send_json(200, result)
                return

            if path == "/records":
                after_id = parse_nonnegative_int(
                    (query.get("after_id") or ["0"])[0], 0
                )
                limit = parse_nonnegative_int(
                    (query.get("limit") or ["200"])[0], 200, 1000
                )
                rows = database.rows_after(after_id, max(1, limit))
                self.send_json(
                    200,
                    {
                        "records": [json.loads(row["payload_json"]) for row in rows],
                        "last_id": rows[-1]["id"] if rows else after_id,
                    },
                )
                return

            if path == "/events":
                self.serve_events(query)
                return

            self.send_json(404, {"status": "error", "error": "not found"})

        def serve_events(self, query):
            header_cursor = self.headers.get("Last-Event-ID")
            query_cursor = (query.get("after_id") or [None])[0]
            cursor = parse_nonnegative_int(
                query_cursor if query_cursor is not None else header_cursor,
                0,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.common_headers()
            self.end_headers()

            last_heartbeat = time.monotonic()
            try:
                while not stop_event.is_set():
                    rows = database.rows_after(cursor, 200)
                    if rows:
                        for row in rows:
                            event = (
                                "id: %s\n" % row["id"]
                                + "data: " + row["payload_json"] + "\n\n"
                            ).encode("utf-8")
                            self.wfile.write(event)
                            cursor = row["id"]
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
                        continue

                    if time.monotonic() - last_heartbeat >= 15.0:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
                    stop_event.wait(0.25)
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                return

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Last-Event-ID")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return SatelliteHttpHandler


def configure_satellite_socket(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (
        ("TCP_KEEPIDLE", 30),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
    ):
        option = getattr(socket, name, None)
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass


def send_ack(sock, message_id):
    payload = json.dumps(
        {"type": "ack", "message_id": message_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    sock.sendall(payload)


def receive_from_b(database, runtime, host, port, reconnect_interval, stop_event):
    target = "%s:%s" % (host, port)
    while not stop_event.is_set():
        sock = None
        try:
            log("[SAT-RX] connecting to B %s" % target)
            sock = socket.create_connection((host, port), timeout=10.0)
            configure_satellite_socket(sock)
            sock.settimeout(1.0)
            peer = "%s:%s" % sock.getpeername()
            runtime.set_connection(True, peer)
            log("[SAT-RX] connected to B %s" % peer)
            buffer = b""

            while not stop_event.is_set():
                try:
                    chunk = sock.recv(64 * 1024)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("B closed connection")
                buffer += chunk
                if len(buffer) > MAX_LINE_BYTES and b"\n" not in buffer:
                    raise ValueError("JSON line exceeds %s bytes" % MAX_LINE_BYTES)

                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line.decode("utf-8"))
                        if not isinstance(payload, dict):
                            raise ValueError("JSON root must be an object")
                        message_id, inserted, row_id = database.store(payload, peer)
                    except Exception as exc:
                        log("[SAT-RX] message rejected without ACK: %s" % exc)
                        continue

                    # 只有 SQLite 提交成功后才确认，重复 message_id 也确认。
                    send_ack(sock, message_id)
                    runtime.record_message(message_id)
                    log(
                        "[SAT-RX] stored+acked id=%s message_id=%s inserted=%s"
                        % (row_id, message_id, inserted)
                    )
        except (ConnectionError, OSError, ValueError) as exc:
            if not stop_event.is_set():
                log("[SAT-RX] B connection unavailable: %s" % exc)
        finally:
            runtime.set_connection(False)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        stop_event.wait(reconnect_interval)



# ======================== Gateway Heartbeat Monitor ========================

class HeartbeatStatus:
    def __init__(self, timeout_seconds):
        self.timeout_seconds = float(timeout_seconds)
        self.lock = threading.Lock()
        self.last_seen = {}
        self.details = {}

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {
            "1", "true", "yes", "on", "online", "connected"
        }

    def mark(self, gateway_id, payload=None):
        gateway_id = int(gateway_id)
        if gateway_id not in (1, 2, 3):
            return False
        payload = payload if isinstance(payload, dict) else {}
        if "edge_online" in payload:
            edge_online = self._as_bool(payload.get("edge_online"))
        elif "status" in payload:
            edge_online = self._as_bool(payload.get("status"))
        else:
            # Backward compatibility with the old {gateway,type} heartbeat.
            edge_online = True
        with self.lock:
            self.last_seen[gateway_id] = time.monotonic()
            self.details[gateway_id] = {
                "edge_online": edge_online,
                "industrial_pc_connected": self._as_bool(
                    payload.get("industrial_pc_connected", False)
                ),
                "source_gateway": payload.get("source_gateway"),
                "group_enabled": self._as_bool(
                    payload.get("group_enabled", True)
                ),
                "sequence": payload.get("sequence"),
                "edge_timestamp": payload.get("edge_timestamp"),
                "server_timestamp": payload.get("timestamp"),
                "edge_peer": payload.get("edge_peer"),
                "edge_heartbeat_age_ms": payload.get("heartbeat_age_ms"),
            }
        return True

    def is_online(self, gateway_id):
        gateway_id = int(gateway_id)
        with self.lock:
            last = self.last_seen.get(gateway_id)
            detail = dict(self.details.get(gateway_id) or {})
        if last is None:
            return False
        return (
            (time.monotonic() - last) <= self.timeout_seconds
            and bool(detail.get("edge_online", False))
        )

    def age_ms(self, gateway_id):
        gateway_id = int(gateway_id)
        with self.lock:
            last = self.last_seen.get(gateway_id)
        if last is None:
            return None
        return max(0, int(round((time.monotonic() - last) * 1000.0)))

    def snapshot(self, gateway_id):
        gateway_id = int(gateway_id)
        with self.lock:
            detail = dict(self.details.get(gateway_id) or {})
        online = self.is_online(gateway_id)
        source_gateway = detail.get("source_gateway") or {
            1: "gateway_1", 2: "gateway_2", 3: "gateway_4"
        }.get(gateway_id)
        return {
            "status": 1 if online else 0,
            "edge_online": online,
            "industrial_pc_connected": bool(
                online and detail.get("industrial_pc_connected", False)
            ),
            "gateway": source_gateway,
            "gateway_group": gateway_id,
            "heartbeat_age_ms": detail.get("edge_heartbeat_age_ms"),
            "server_heartbeat_age_ms": self.age_ms(gateway_id),
            "group_enabled": bool(detail.get("group_enabled", False)),
            "sequence": detail.get("sequence"),
            "edge_timestamp": detail.get("edge_timestamp"),
            "server_timestamp": detail.get("server_timestamp"),
        }

def heartbeat_pull_loop(server_host, server_port, heartbeat_status):
    target = '{}:{}'.format(server_host, server_port)
    while True:
        sock = None
        try:
            log('[HEARTBEAT-UP] connecting to {} ...'.format(target))
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            set_socket_options(sock)
            sock.settimeout(UPSTREAM_CONNECT_TIMEOUT_SECONDS)
            sock.connect((server_host, int(server_port)))
            sock.settimeout(None)
            log('[HEARTBEAT-UP] connected to {}'.format(target))
            f = sock.makefile('rb')
            while True:
                line = f.readline(MAX_JSON_LINE_SIZE + 1)
                if not line:
                    raise ConnectionError('heartbeat channel closed')
                if len(line) > MAX_JSON_LINE_SIZE:
                    log('[HEARTBEAT-UP][WARN] line too large | bytes={}'.format(len(line)))
                    continue
                text = line.decode('utf-8', errors='replace').strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except Exception as exc:
                    log('[HEARTBEAT-UP][WARN] invalid JSON | raw={!r} | error={}'.format(text, exc))
                    continue
                if str(payload.get('type', '')).lower() != 'heartbeat':
                    continue
                try:
                    gateway_id = int(payload.get('gateway'))
                except (TypeError, ValueError):
                    log('[HEARTBEAT-UP][WARN] invalid gateway in payload: {}'.format(text))
                    continue
                if heartbeat_status.mark(gateway_id, payload):
                    online = heartbeat_status.is_online(gateway_id)
                    log('[HEARTBEAT-UP] gateway{} heartbeat status={} industrial_pc_connected={}'.format(
                        gateway_id,
                        1 if online else 0,
                        heartbeat_status.snapshot(gateway_id).get('industrial_pc_connected'),
                    ), colour="green" if online else "red")
        except (ConnectionError, OSError, ValueError) as exc:
            log('[HEARTBEAT-UP][WARN] disconnected/error: {}'.format(exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        log('[HEARTBEAT-UP] reconnect after {:.1f}s'.format(RECONNECT_INTERVAL))
        time.sleep(RECONNECT_INTERVAL)


# ======================== Network slice telemetry ========================

def normalize_slice_gateway(value):
    text = str(value).strip().lower()
    normalized = "".join(ch for ch in text if ch.isalnum())
    for canonical, aliases in SLICE_GATEWAY_ALIASES.items():
        if normalized in {
            "".join(ch for ch in str(alias).strip().lower() if ch.isalnum())
            for alias in aliases
        }:
            return canonical
    return None


class SliceMetricsRegistry:
    """Hold newest edge reports and enrich them with core-side link state."""

    def __init__(self, heartbeat_status, shortwave_controller=None):
        self.heartbeat_status = heartbeat_status
        self.shortwave_controller = shortwave_controller
        self._lock = threading.Lock()
        self._latest = {}
        self._sequence = 0

    def update(self, payload):
        if not isinstance(payload, dict) or payload.get("type") != "slice_metrics":
            return False
        canonical = normalize_slice_gateway(payload.get("gateway"))
        if canonical is None or not isinstance(payload.get("slices"), dict):
            return False
        with self._lock:
            self._latest[canonical] = {
                "payload": copy.deepcopy(payload),
                "received_monotonic": time.monotonic(),
            }
        return True

    def _shortwave_snapshot(self):
        if self.shortwave_controller is None:
            return {
                "connected": False,
                "hf_status": None,
                "detect": None,
                "active_db": "-1",
                "desired_db": "-1",
                "callee_id": None,
            }
        try:
            state = self.shortwave_controller.snapshot()
        except Exception as exc:
            log("[SLICE][WARN] shortwave snapshot failed: {}".format(exc))
            return {"connected": False, "error": str(exc)}
        baotong = state.get("baotong") or {}
        return {
            "connected": bool(baotong.get("connected")),
            "peer": baotong.get("peer"),
            "hf_status": baotong.get("hf_status"),
            "detect": state.get("detect"),
            "active_db": state.get("active_db"),
            "desired_db": state.get("desired_db"),
            "callee_id": state.get("callee_id"),
        }

    @staticmethod
    def _edge_status(age_seconds, exists):
        if not exists or age_seconds is None:
            return "offline"
        if age_seconds > SLICE_METRICS_OFFLINE_SECONDS:
            return "offline"
        if age_seconds > SLICE_METRICS_STALE_SECONDS:
            return "stale"
        return "online"

    def snapshot(self):
        with self._lock:
            latest = copy.deepcopy(self._latest)
            self._sequence += 1
            sequence = self._sequence

        now_mono = time.monotonic()
        shortwave = self._shortwave_snapshot()
        gateways = []

        for canonical in ("Gateway1", "Gateway2", "Gateway4"):
            entry = latest.get(canonical)
            payload = entry["payload"] if entry else {}
            age_seconds = (
                now_mono - entry["received_monotonic"] if entry else None
            )
            edge_status = self._edge_status(age_seconds, entry is not None)
            heartbeat_group = SLICE_GATEWAY_HEARTBEAT_GROUP[canonical]
            mobile_online = self.heartbeat_status.is_online(heartbeat_group)
            heartbeat_detail = self.heartbeat_status.snapshot(heartbeat_group)
            heartbeat_age_ms = heartbeat_detail.get("heartbeat_age_ms")
            expected_callee = SLICE_GATEWAY_CALLEE_MAP[canonical]
            active_for_gateway = (
                str(shortwave.get("callee_id") or "").strip() == expected_callee
            )

            slices = copy.deepcopy(payload.get("slices") or {})
            shortwave_available = bool(shortwave.get("connected"))
            for slice_id, item in slices.items():
                if not isinstance(item, dict):
                    continue
                allowed = item.get("allowed_links") or []
                if "5g" in allowed and mobile_online:
                    effective_link = "5g"
                elif "shortwave" in allowed and shortwave_available:
                    effective_link = "shortwave"
                else:
                    effective_link = "unavailable"
                item["effective_selected_link"] = effective_link
                if slice_id == "urllc":
                    item["effective_policy"] = (
                        "5g_priority" if effective_link == "5g"
                        else "shortwave_backup" if effective_link == "shortwave"
                        else "link_unavailable"
                    )
                elif slice_id == "mmtc":
                    item["shortwave_redundancy_available"] = shortwave_available
                item["link_state"] = (
                    "available" if effective_link != "unavailable" else "unavailable"
                )

            source_gateway = payload.get("gateway", canonical)
            gateways.append({
                # gateway keeps the declaration originating from edge --gateway.
                "gateway": source_gateway,
                "gateway_id": canonical,
                "scene": canonical.replace("Gateway", ""),
                "edge": {
                    "status": edge_status,
                    "age_ms": (
                        int(round(age_seconds * 1000.0))
                        if age_seconds is not None else None
                    ),
                    "reported_at": payload.get("timestamp"),
                    "sequence": payload.get("sequence"),
                    "industrial_pc_connected": heartbeat_detail.get(
                        "industrial_pc_connected", False
                    ),
                },
                "links": {
                    "5g": {
                        "status": "online" if mobile_online else "offline",
                        "heartbeat_group": heartbeat_group,
                        "heartbeat_age_ms": heartbeat_age_ms,
                    },
                    "shortwave": {
                        "status": "online" if shortwave_available else "offline",
                        "expected_callee_id": expected_callee,
                        "active_for_gateway": active_for_gateway,
                        "detect": shortwave.get("detect"),
                        "hf_status": shortwave.get("hf_status"),
                    },
                },
                "slices": slices,
            })

        return {
            "type": "multi_gateway_slice_status",
            "schema_version": "1.0",
            "sequence": sequence,
            "timestamp": datetime.now(BJ_TZ).isoformat(timespec="milliseconds"),
            "gateways": gateways,
        }


def slice_metrics_pull_loop(server_host, server_port, registry):
    target = "{}:{}".format(server_host, server_port)
    while True:
        sock = None
        try:
            log("[SLICE-UP] connecting to {} ...".format(target))
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            set_socket_options(sock)
            sock.settimeout(UPSTREAM_CONNECT_TIMEOUT_SECONDS)
            sock.connect((server_host, int(server_port)))
            sock.settimeout(None)
            log("[SLICE-UP] connected to {}".format(target))
            file_obj = sock.makefile("rb")
            while True:
                line = file_obj.readline(MAX_JSON_LINE_SIZE + 1)
                if not line:
                    raise ConnectionError("slice metrics channel closed")
                if len(line) > MAX_JSON_LINE_SIZE:
                    log("[SLICE-UP][WARN] oversized JSON line: {} bytes".format(len(line)))
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    log("[SLICE-UP][WARN] invalid JSON: {}".format(exc))
                    continue
                if not registry.update(payload):
                    log("[SLICE-UP][WARN] invalid slice payload ignored")
        except (ConnectionError, OSError, ValueError) as exc:
            log("[SLICE-UP][WARN] disconnected/error: {}".format(exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(RECONNECT_INTERVAL)


def slice_metrics_publish_loop(registry, json_hub):
    while True:
        started = time.monotonic()
        payload = registry.snapshot()
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        json_hub.update(text, payload)
        delay = SLICE_METRICS_PUBLISH_INTERVAL_SECONDS - (time.monotonic() - started)
        if delay > 0:
            time.sleep(delay)

# ======================== Status HTTP Handler (port 10015) ========================

class StatusHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, hubs=None, heartbeat_status=None, **kwargs):
        self.hubs = hubs
        self.heartbeat_status = heartbeat_status
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/link1' or path == '/link2' or path == '/link3':
            idx = int(path[-1]) - 1
            gateway_id = idx + 1
            if self.heartbeat_status is not None and 1 <= gateway_id <= 3:
                self.send_json(200, self.heartbeat_status.snapshot(gateway_id))
            elif 0 <= idx < len(self.hubs):
                hub = self.hubs[idx]
                online = (time.time() - hub.last_update_time) <= 1.0
                self.send_json(200, {'status': 1 if online else 0})
            else:
                self.send_json(404, {'error': 'not found'})
        elif path == '/fps1' or path == '/fps2' or path == '/fps3':
            idx = int(path[-1]) - 1
            if 0 <= idx < len(self.hubs):
                hub = self.hubs[idx]
                self.send_json(200, {'fps': round(hub.avg_fps, 2)})
            else:
                self.send_json(404, {'error': 'not found'})
        else:
            self.send_json(404, {'error': 'not found'})


def make_status_handler(hubs, heartbeat_status=None):
    class Handler(StatusHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, hubs=hubs, heartbeat_status=heartbeat_status, **kwargs)
    return Handler


# ======================== Main ========================

def main():
    parser = argparse.ArgumentParser(
        description="Core Gateway: framed + JSON router from server.py to frontend HTTP services"
    )

    parser.add_argument(
        "--server-host",
        default=DEFAULT_SERVER_HOST,
        help="server.py host, default: {}".format(DEFAULT_SERVER_HOST)
    )
    # 新增卫星模块参数
    parser.add_argument("--b-host", default=DEFAULT_B_HOST,
                        help="B server host for satellite data (default: {})".format(DEFAULT_B_HOST))
    parser.add_argument("--b-port", type=int, default=DEFAULT_B_PORT,
                        help="B server port for satellite data (default: {})".format(DEFAULT_B_PORT))
    parser.add_argument("--db", default=DEFAULT_SATELLITE_DB,
                        help="SQLite database path for satellite data")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS,
                        help="Max records in satellite SQLite")
    parser.add_argument("--reconnect-interval", type=float,
                        default=SATELLITE_RECONNECT_INTERVAL_SECONDS,
                        help="Reconnect interval to B server")

    args = parser.parse_args()

    log("=" * 90)
    log("[MAIN] gateway.py started")
    log("[MAIN] upstream server host: {}".format(args.server_host))

    log("[MAIN] VID0 route:")
    for upstream_port, frontend_port in VIDEO_FORWARD_MAP.items():
        log("[MAIN]   server:{} VID0 -> http://{}:{}/stream".format(
            upstream_port,
            FRONTEND_LISTEN_HOST,
            frontend_port,
        ))

    log("[MAIN] SNAP route:")
    for upstream_port, frontend_port in SNAP_FORWARD_MAP.items():
        log("[MAIN]   server:{} SNAP -> http://{}:{}/latest.jpg".format(
            upstream_port,
            FRONTEND_LISTEN_HOST,
            frontend_port,
        ))

    log("[MAIN] JSON route:")
    for upstream_port, frontend_port in JSON_FORWARD_MAP.items():
        log("[MAIN]   server:{} JSON -> http://{}:{}/events and /latest.json".format(
            upstream_port,
            FRONTEND_LISTEN_HOST,
            frontend_port,
        ))

    # 卫星通道
    log("[MAIN] Satellite route:")
    log("[MAIN]   B:{}:{} -> http://{}:{}/events and /latest.json (SQLite)".format(
        args.b_host, args.b_port, FRONTEND_LISTEN_HOST, SATELLITE_HTTP_PORT
    ))

    log("[MAIN] JSON file output:")
    for upstream_port, file_path in JSON_FILE_ROUTE_MAP.items():
        log("[MAIN]   server:{} latest JSON -> {}".format(upstream_port, file_path))
    log("[MAIN] Slice metrics: server:{} -> http://{}:{}/events and /latest.json".format(
        SLICE_METRICS_SERVER_PORT,
        FRONTEND_LISTEN_HOST,
        SLICE_METRICS_FRONTEND_PORT,
    ))

    if BAOTONG_HF_ENABLED:
        log("[MAIN] BaoTong RX: {}:{} -> http://{}:{}/latest.json and /events".format(
            BAOTONG_HF_LISTEN_HOST,
            BAOTONG_HF_LISTEN_PORT,
            FRONTEND_LISTEN_HOST,
            BAOTONG_OUTPUT_HTTP_PORT,
        ))
        log("[MAIN] BaoTong output JSON gateway={}; interface {}={}".format(
            BAOTONG_OUTPUT_GATEWAY,
            BAOTONG_OUTPUT_IFACE,
            BAOTONG_OUTPUT_ADDR,
        ))
        log("[MAIN] Shortwave command API: http://{}:{}/call".format(
            FRONTEND_LISTEN_HOST,
            SHORTWAVE_COMMAND_HTTP_PORT,
        ))

    log("[MAIN] ignored SNAP ports: {}".format(sorted(IGNORED_SNAP_PORTS)))
    log("[MAIN] 11405 is SNAP-only; no video /stream is created for 11405")
    log("[MAIN] latest TTL: snapshot={}s, json={}s".format(
        SNAPSHOT_MAX_AGE_SECONDS,
        JSON_MAX_AGE_SECONDS,
    ))
    log("[MAIN] local image storage: disabled")
    log("=" * 90)

    video_hubs = {}
    for server_port in VIDEO_FORWARD_MAP.keys():
        ch = CHANNEL_NAME[server_port]
        video_hubs[server_port] = VideoHub(name=ch)

    snapshot_store_map = {}
    for snap_port in sorted(set(SNAP_FORWARD_MAP.values())):
        snapshot_store_map[snap_port] = SnapshotStore(name="snap_{}".format(snap_port))

    json_hubs = {}
    for server_port, frontend_port in JSON_FORWARD_MAP.items():
        ch = CHANNEL_NAME[server_port]
        json_hubs[server_port] = JsonHub(
            name=ch,
            source_port=server_port,
            file_path=JSON_FILE_ROUTE_MAP.get(server_port),
        )

    # ----- 启动前端 HTTP 服务（视频、快照、非卫星JSON） -----
    for server_port, frontend_port in VIDEO_FORWARD_MAP.items():
        handler_cls = make_mjpeg_handler(video_hubs[server_port])
        t = threading.Thread(
            target=start_http_server,
            args=(frontend_port, handler_cls, CHANNEL_NAME[server_port]),
            daemon=True,
        )
        t.start()

    for snap_port, snap_store in snapshot_store_map.items():
        handler_cls = make_snapshot_handler(snap_store)
        t = threading.Thread(
            target=start_http_server,
            args=(snap_port, handler_cls, "snapshot_{}".format(snap_port)),
            daemon=True,
        )
        t.start()

    for server_port, frontend_port in JSON_FORWARD_MAP.items():
        handler_cls = make_json_handler(json_hubs[server_port])
        t = threading.Thread(
            target=start_http_server,
            args=(frontend_port, handler_cls, "json_{}".format(frontend_port)),
            daemon=True,
        )
        t.start()

    # ----- BaoTong HF -----
    shortwave_controller = None
    if BAOTONG_HF_ENABLED:
        baotong_hub = JsonHub(
            name="baotong_rx",
            source_port=BAOTONG_HF_LISTEN_PORT,
            file_path=None,
        )
        handler_cls = make_json_handler(baotong_hub)
        t = threading.Thread(
            target=start_http_server,
            args=(BAOTONG_OUTPUT_HTTP_PORT, handler_cls, "baotong_{}".format(BAOTONG_OUTPUT_HTTP_PORT)),
            daemon=True,
        )
        t.start()
        baotong_server = BaoTongReceiveServer(
            BAOTONG_HF_LISTEN_HOST,
            BAOTONG_HF_LISTEN_PORT,
            baotong_hub,
        )
        t = threading.Thread(
            target=start_baotong_receive_server,
            args=(baotong_server,),
            daemon=True,
        )
        t.start()

        shortwave_controller = ShortwaveCallController(baotong_server)
        baotong_server.set_detect_callback(
            shortwave_controller.notify_detect
        )
        baotong_server.set_linkstatus_callback(
            shortwave_controller.notify_linkstatus
        )
        baotong_server.set_response_callback(
            shortwave_controller.notify_response
        )
        baotong_server.set_response_forward_filter(
            shortwave_controller.should_forward_response
        )
        shortwave_controller.start()
        shortwave_handler = make_shortwave_command_handler(shortwave_controller)
        shortwave_http_server = ThreadingHTTPServer(
            (FRONTEND_LISTEN_HOST, SHORTWAVE_COMMAND_HTTP_PORT),
            shortwave_handler,
        )
        t = threading.Thread(
            target=shortwave_http_server.serve_forever,
            daemon=True,
            name="shortwave-http-{}".format(SHORTWAVE_COMMAND_HTTP_PORT),
        )
        t.start()
        log("[MAIN] Shortwave command HTTP listening on {}:{}".format(
            FRONTEND_LISTEN_HOST,
            SHORTWAVE_COMMAND_HTTP_PORT,
        ))

    # ----- 上游拉取线程（旧协议 + 非卫星JSON） -----
    for server_port in FRAMED_SERVER_INPUT_PORTS:
        t = threading.Thread(
            target=server_framed_pull_loop,
            args=(
                args.server_host,
                server_port,
                video_hubs.get(server_port),
                snapshot_store_map,
            ),
            daemon=True,
        )
        t.start()

    for server_port in JSON_SERVER_INPUT_PORTS:  # 注意：11410 已不在列表中
        t = threading.Thread(
            target=server_json_pull_loop,
            args=(
                args.server_host,
                server_port,
                json_hubs[server_port],
            ),
            daemon=True,
        )
        t.start()

    # ----- 卫星接收模块（SQLite + HTTP 10014） -----
    satellite_db = GatewayDatabase(args.db, args.max_records)
    satellite_db.initialize()

    # 启动时清空卫星消息表
    conn = satellite_db.connect()
    try:
        conn.execute("DELETE FROM gateway_messages")
        conn.commit()
        log("[MAIN] Cleared satellite messages table (gateway_messages)")
    except Exception as e:
        log("[MAIN] WARNING: Failed to clear satellite messages table: %s" % e)
    finally:
        conn.close()

    runtime = RuntimeState()
    stop_event = threading.Event()

    # 启动接收线程
    receiver_thread = threading.Thread(
        target=receive_from_b,
        args=(satellite_db, runtime, args.b_host, args.b_port, args.reconnect_interval, stop_event),
        daemon=True,
    )
    receiver_thread.start()

    # 启动 HTTP 服务（端口 10014）
    satellite_handler = make_satellite_http_handler(satellite_db, runtime, stop_event)
    satellite_http_server = ThreadingHTTPServer(
        (FRONTEND_LISTEN_HOST, SATELLITE_HTTP_PORT),
        satellite_handler,
    )
    http_thread = threading.Thread(
        target=satellite_http_server.serve_forever,
        name="satellite-http-{}".format(SATELLITE_HTTP_PORT),
        daemon=True,
    )
    http_thread.start()
    log("[MAIN] Satellite HTTP listening on {}:{} (SQLite: {})".format(
        FRONTEND_LISTEN_HOST, SATELLITE_HTTP_PORT, satellite_db.path
    ))

    # 启动状态监控服务（端口 10015）
    monitored_ports = STATUS_MONITORED_PORTS
    monitored_hubs = [video_hubs[port] for port in monitored_ports]
    heartbeat_status = HeartbeatStatus(HEARTBEAT_LINK_TIMEOUT_SECONDS)
    heartbeat_thread = threading.Thread(
        target=heartbeat_pull_loop,
        args=(args.server_host, HEARTBEAT_SERVER_INPUT_PORT, heartbeat_status),
        daemon=True,
        name="heartbeat-up-{}".format(HEARTBEAT_SERVER_INPUT_PORT),
    )
    heartbeat_thread.start()
    log("[MAIN] Heartbeat route: server:{} -> http://{}:{}/link1,/link2,/link3 timeout={}s".format(
        HEARTBEAT_SERVER_INPUT_PORT, FRONTEND_LISTEN_HOST, STATUS_HTTP_PORT,
        HEARTBEAT_LINK_TIMEOUT_SECONDS
    ))
    status_handler_cls = make_status_handler(monitored_hubs, heartbeat_status=heartbeat_status)
    status_server = ThreadingHTTPServer(
        (FRONTEND_LISTEN_HOST, STATUS_HTTP_PORT),
        status_handler_cls,
    )
    t = threading.Thread(
        target=status_server.serve_forever,
        daemon=True,
        name="status-http-{}".format(STATUS_HTTP_PORT),
    )
    t.start()
    log("[MAIN] Status HTTP listening on {}:{}".format(
        FRONTEND_LISTEN_HOST,
        STATUS_HTTP_PORT,
    ))

    # ----- Slice metrics: server 11420 -> enriched frontend 10017 -----
    slice_registry = SliceMetricsRegistry(
        heartbeat_status,
        shortwave_controller=shortwave_controller,
    )
    slice_hub = JsonHub(
        name="slice_metrics",
        source_port=SLICE_METRICS_SERVER_PORT,
        file_path=None,
    )
    slice_handler = make_json_handler(slice_hub)
    t = threading.Thread(
        target=start_http_server,
        args=(SLICE_METRICS_FRONTEND_PORT, slice_handler, "slice_metrics"),
        daemon=True,
        name="slice-http-{}".format(SLICE_METRICS_FRONTEND_PORT),
    )
    t.start()
    t = threading.Thread(
        target=slice_metrics_pull_loop,
        args=(args.server_host, SLICE_METRICS_SERVER_PORT, slice_registry),
        daemon=True,
        name="slice-up-{}".format(SLICE_METRICS_SERVER_PORT),
    )
    t.start()
    t = threading.Thread(
        target=slice_metrics_publish_loop,
        args=(slice_registry, slice_hub),
        daemon=True,
        name="slice-publisher",
    )
    t.start()
    log("[MAIN] Slice frontend HTTP listening on {}:{}; publish interval={}s".format(
        FRONTEND_LISTEN_HOST,
        SLICE_METRICS_FRONTEND_PORT,
        SLICE_METRICS_PUBLISH_INTERVAL_SECONDS,
    ))

    # 主循环
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("[MAIN] Shutting down...")
        stop_event.set()
        satellite_http_server.shutdown()
        satellite_http_server.server_close()
        receiver_thread.join(timeout=3.0)


if __name__ == "__main__":
    main()
EOF
