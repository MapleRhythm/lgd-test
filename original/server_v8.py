#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===========================================================
  server.py - old framed protocol + new JSON protocol router
  + HTTP Device State API
  + HTTP Whitelist API
  + Reliable Satellite Forwarding (SQLite + ACK)
  + Group Control API (stop/recover) and Heartbeat Sender
===========================================================

  Upstream:
    - Listen on 11500 (General upstream TCP).
    - Listen on 11501 (HTTP API for device state).
    - Listen on 11502 (HTTP API for device whitelist).
    - Listen on 11503 (HTTP API for satellite data, SQLite persistent).
    - Listen on 11507 (HTTP API for group control: stop/recover).

  Downstream:
    - Old framed protocol:
        gateway_1 -> 11400
        gateway_2 -> 11401
        gateway_3 -> 11402
        gateway_4 -> 11403
        gateway_5 -> 11404
        gateway_6 -> 11405
    - New JSON protocol:
        Gateway1 -> 11406
        Gateway2 -> 11407
        Gateway5 -> 11409
        WiFi     -> 11408
    - Satellite forward (TCP) -> 11410 (now managed by SQLite+ACK logic)
    - Gateway4 fire status (newline-delimited JSON) -> 11421

  Group Control:
    Group1: 11400 (framed), 11406 (JSON), heartbeat gateway=1
    Group2: 11401 (framed), 11407 (JSON), heartbeat gateway=2
    Group3: 11402,11403,11404,11405 (framed), 11409 (JSON), heartbeat gateway=3

  Heartbeat:
    Listen on 11416 and send {"gateway":1/2/3,"type":"heartbeat"} every 0.5s
    to connected clients. Heartbeat is active only when the corresponding group
    is enabled.

  Link status:
    Listen on 11417. Edge gateways register their gateway name and receive the
    corresponding group-enabled state once per second.

  JSON forwarded to the core gateway is newline-delimited UTF-8 text.
===========================================================
"""

import argparse
import codecs
import json
import os
import queue
import shutil
import socket
import struct
import threading
import time
import sqlite3
import hashlib
import select
import signal
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ======================== Defaults ========================
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_SENDER_PORT = 11500
DEFAULT_HTTP_API_PORT = 11501  # HTTP API 端口
DEFAULT_WHITELIST_PORT = 11502  # 白名单 API 端口
DEFAULT_TQ_PORT = 11503         # 卫星数据接收端口
DEFAULT_CONTROL_PORT = 11507    # 控制 API 端口
DEFAULT_WHITELIST_FILE = "whitelist.json" # 白名单数据存储文件
DEFAULT_STORAGE_DIR = "received_images"
DEFAULT_MAX_QUEUE_SIZE = 450
DEFAULT_MAX_JSON_QUEUE_SIZE = 1000
DEFAULT_MAX_FILES_PER_GATEWAY = 500
DEFAULT_JSON_MAX_BUFFER_SIZE = 1024 * 1024
MAX_PACKET_SIZE = 20 * 1024 * 1024
DEFAULT_SLICE_UPSTREAM_PORT = 11510
DEFAULT_SLICE_DOWNSTREAM_PORT = 11420
DEFAULT_SLICE_MAX_LINE_SIZE = 1024 * 1024
DEFAULT_EDGE_HEARTBEAT_PORT = 11511
DEFAULT_EDGE_HEARTBEAT_TIMEOUT = 3.0
DEFAULT_EDGE_HEARTBEAT_MAX_LINE_SIZE = 64 * 1024
DEFAULT_GATEWAY4_FIRE_DOWNSTREAM_PORT = 11421
DEFAULT_GATEWAY4_FIRE_MAX_BUFFER_SIZE = 64 * 1024

# 卫星转发专用配置
DEFAULT_SATELLITE_DB = "satellite_relay.db"
DEFAULT_MAX_RECORDS = 5000
MAX_HTTP_BODY = 1024 * 1024

# 心跳配置
DEFAULT_HEARTBEAT_HOST = DEFAULT_LISTEN_HOST
DEFAULT_HEARTBEAT_PORT = 11416
HEARTBEAT_INTERVAL = 0.5
DEFAULT_LINK_STATUS_PORT = 11417
LINK_STATUS_PUSH_INTERVAL = 1.0


# ======================== Port maps ========================
FRAMED_GATEWAY_PORT_MAP = {
    "gateway_1": 11400,
    "gateway_2": 11401,
    "gateway_3": 11402,
    "gateway_4": 11403,
    "gateway_5": 11404,
    "gateway_6": 11405,
}

# 注意：已移除 "tq_forward"，因为 11410 现在由卫星转发模块独立管理
JSON_GATEWAY_PORT_MAP = {
    "gateway1": {
        "display": "Gateway1",
        "port": 11406,
        "aliases": {"gateway1", "gateway_1", "gw1", "g1", "1"},
    },
    "gateway2": {
        "display": "Gateway2",
        "port": 11407,
        "aliases": {"gateway2", "gateway_2", "gw2", "g2", "2"},
    },
    "gateway5": {
        "display": "Gateway5",
        "port": 11409,
        "aliases": {"gateway5", "gateway_5", "gw5", "g5", "5"},
    },
    "wifi": {
        "display": "WiFi",
        "port": 11408,
        "aliases": {"wifi", "wifi_gateway", "gw_wifi"},
    },
    # tq_forward 已移除，11410 由卫星模块管理
}

JSON_GATEWAY_ID_FIELDS = (
    "gateway",
    "gateway_id",
    "gateway_name",
    "source_gateway",
    "edge_gateway",
    "device_gateway",
)


# ======================== Group Control ========================
# 映射 gateway_id -> 组号 (1,2,3)
GROUP_MAP = {
    # framed gateways
    "gateway_1": 1,
    "gateway_2": 2,
    "gateway_3": 3,
    "gateway_4": 3,
    "gateway_5": 3,
    "gateway_6": 3,
    # json gateways
    "gateway1": 1,
    "gateway2": 2,
    "gateway5": 3,
    # wifi not in any group (ignored)
}

group_enabled = {1: True, 2: True, 3: True}
group_lock = threading.Lock()

EDGE_HEARTBEAT_GATEWAYS = {
    "gateway1": {"group": 1, "display": "gateway_1"},
    "gateway2": {"group": 2, "display": "gateway_2"},
    "gateway4": {"group": 3, "display": "gateway_4"},
}


def normalize_edge_heartbeat_gateway(value):
    normalized = "".join(
        char for char in str(value).strip().lower() if char.isalnum()
    )
    return normalized if normalized in EDGE_HEARTBEAT_GATEWAYS else None


def heartbeat_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "connected", "online"
    }


class EdgeHeartbeatRegistry:
    """Keep the latest real heartbeat for gateway_1, gateway_2 and gateway_4."""

    def __init__(self, timeout_seconds):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._lock = threading.Lock()
        self._states = {}
        self._active_connections = {}

    def claim(self, gateway_key, connection_token, peer):
        with self._lock:
            previous = self._active_connections.get(gateway_key)
            self._active_connections[gateway_key] = connection_token
            if previous is not connection_token:
                print("[EDGE HEARTBEAT] %s active connection -> %s" % (
                    EDGE_HEARTBEAT_GATEWAYS[gateway_key]["display"], peer
                ))

    def update(self, gateway_key, connection_token, payload, peer):
        with self._lock:
            if self._active_connections.get(gateway_key) is not connection_token:
                return False
            self._states[gateway_key] = {
                "last_seen": time.monotonic(),
                "sequence": payload.get("sequence"),
                "edge_timestamp": payload.get("timestamp"),
                "industrial_pc_connected": bool(
                    heartbeat_bool(payload.get("industrial_pc_connected", False))
                ),
                "peer": peer,
            }
        return True

    def snapshot_for_group(self, group_id):
        gateway_key = next(
            key for key, config in EDGE_HEARTBEAT_GATEWAYS.items()
            if config["group"] == group_id
        )
        config = EDGE_HEARTBEAT_GATEWAYS[gateway_key]
        with self._lock:
            state = dict(self._states.get(gateway_key) or {})
        last_seen = state.get("last_seen")
        age_seconds = (
            time.monotonic() - last_seen if last_seen is not None else None
        )
        online = bool(
            age_seconds is not None and age_seconds <= self.timeout_seconds
        )
        return {
            "gateway": group_id,
            "source_gateway": config["display"],
            "edge_online": online,
            "status": 1 if online else 0,
            "industrial_pc_connected": bool(
                online and state.get("industrial_pc_connected", False)
            ),
            "heartbeat_age_ms": (
                max(0, int(round(age_seconds * 1000.0)))
                if age_seconds is not None else None
            ),
            "sequence": state.get("sequence"),
            "edge_timestamp": state.get("edge_timestamp"),
            "edge_peer": state.get("peer"),
        }

def get_group_for_gateway(gateway_id):
    return GROUP_MAP.get(gateway_id)

def clear_group_queues(group):
    """清空属于该组的所有队列"""
    for gw, g in GROUP_MAP.items():
        if g == group:
            if gw in framed_queues:
                framed_queues[gw].clear()
            if gw in json_queues:
                json_queues[gw].clear()


# ======================== Heartbeat Server ========================
class HeartbeatServer:
    def __init__(self, registry, host=DEFAULT_HEARTBEAT_HOST, port=DEFAULT_HEARTBEAT_PORT):
        self.registry = registry
        self.host = host
        self.port = port
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(20)
        server_sock.settimeout(1.0)
        print("[HEARTBEAT] listening on %s:%s" % (self.host, self.port))

        try:
            while not self.stop_event.is_set():
                try:
                    conn, addr = server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise

                threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    daemon=True,
                ).start()
        finally:
            server_sock.close()

    def _handle_client(self, conn, addr):
        print("[+HEARTBEAT] client connected: %s:%s" % (addr[0], addr[1]))
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        try:
            while not self.stop_event.is_set():
                with group_lock:
                    enabled_snapshot = dict(group_enabled)

                for group_id in (1, 2, 3):
                    payload = self.registry.snapshot_for_group(group_id)
                    enabled = bool(enabled_snapshot.get(group_id, False))
                    if not enabled:
                        payload["status"] = 0
                        payload["edge_online"] = False
                        payload["industrial_pc_connected"] = False
                    payload["type"] = "heartbeat"
                    payload["group_enabled"] = enabled
                    payload["timestamp"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    msg = json.dumps(payload, separators=(",", ":")) + "\n"
                    conn.sendall(msg.encode("utf-8"))

                time.sleep(HEARTBEAT_INTERVAL)
        except Exception as e:
            print("[-HEARTBEAT] client %s:%s disconnected: %s" % (addr[0], addr[1], e))
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ======================== Link Status Server ========================
class LinkStatusServer:
    """Push the group-enabled link state to registered edge gateways."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="link-status-%s" % self.port,
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(20)
        server_sock.settimeout(1.0)
        print("[LINK STATUS] listening on %s:%s" % (self.host, self.port))

        try:
            while not self.stop_event.is_set():
                try:
                    conn, addr = server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    daemon=True,
                ).start()
        finally:
            server_sock.close()

    def _handle_client(self, conn, addr):
        print("[+LINK STATUS] client connected: %s:%s" % (addr[0], addr[1]))
        gateway_name = None
        buffer = b""
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while not self.stop_event.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    raise ConnectionError("client closed before registration")
                buffer += chunk
                if len(buffer) > DEFAULT_EDGE_HEARTBEAT_MAX_LINE_SIZE:
                    raise ValueError("registration exceeds maximum size")
                if b"\n" not in buffer:
                    continue
                line, buffer = buffer.split(b"\n", 1)
                reg = json.loads(line.decode("utf-8"))
                if (
                    not isinstance(reg, dict)
                    or reg.get("type") != "link_status_subscribe"
                    or "gateway" not in reg
                ):
                    raise ValueError("invalid registration")
                gateway_name = str(reg["gateway"]).strip()
                break

            if not gateway_name:
                return
            print(
                "[+LINK STATUS] gateway '%s' registered from %s:%s"
                % (gateway_name, addr[0], addr[1])
            )

            while not self.stop_event.is_set():
                group = get_group_for_gateway(gateway_name)
                with group_lock:
                    enabled = bool(
                        group is not None and group_enabled.get(group, False)
                    )
                status = {
                    "type": "link_status",
                    "gateway": gateway_name,
                    "connected": enabled,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                conn.sendall(
                    (json.dumps(status, separators=(",", ":")) + "\n").encode("utf-8")
                )
                self.stop_event.wait(LINK_STATUS_PUSH_INTERVAL)
        except (ConnectionError, OSError, ValueError, UnicodeDecodeError) as exc:
            print(
                "[-LINK STATUS] client %s:%s disconnected: %s"
                % (addr[0], addr[1], exc)
            )
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ======================== Common queue ========================
class DroppingQueue:
    def __init__(self, maxsize):
        self.maxsize = maxsize
        self._items = deque()
        self._condition = threading.Condition()

    def put(self, item):
        with self._condition:
            if len(self._items) >= self.maxsize:
                self._items.popleft()
            self._items.append(item)
            self._condition.notify()

    def put_if_empty(self, item):
        """Requeue a failed delivery without replacing a newer queued state."""
        with self._condition:
            if self._items:
                return False
            self._items.append(item)
            self._condition.notify()
            return True

    def get(self, timeout=None):
        with self._condition:
            if timeout is None:
                while not self._items:
                    self._condition.wait()
            else:
                deadline = time.time() + timeout
                while not self._items:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise queue.Empty()
                    self._condition.wait(timeout=remaining)
            return self._items.popleft()

    def clear(self):
        with self._condition:
            removed = len(self._items)
            self._items.clear()
            return removed

    def qsize(self):
        with self._condition:
            return len(self._items)


framed_queues = {}
json_queues = {}
gateway4_fire_queue = None

# ========== 队列统计相关全局变量 ==========
enqueue_stats = {}
enqueue_stats_lock = threading.Lock()


# ======================== HTTP Device State API ========================
DEVICE_STATE = {
    "baojingdeng": {"status": 0},
    "shuifa": {"status": 0}
}
state_lock = threading.Lock()

class DeviceStateHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        
        with state_lock:
            response_data = json.dumps(DEVICE_STATE).encode("utf-8")
        
        self.wfile.write(response_data)

    def do_POST(self):
        if self.path == "/alarm":
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length <= 0 or content_length > 4096:
                    self.send_response(400)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status":"error", "message":"Invalid content length"}')
                    return
                    
                post_data = self.rfile.read(content_length)
                payload = json.loads(post_data.decode('utf-8'))
                
                if not isinstance(payload, dict):
                    self.send_response(400)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status":"error", "message":"Not a JSON object"}')
                    return
                
                if "gateway" not in payload:
                    payload["gateway"] = "wifi"
                
                gateway_id = find_json_gateway_id(payload)
                if gateway_id is None:
                    gateway_id = "wifi"
                
                # 检查组状态
                group = get_group_for_gateway(gateway_id)
                if group is not None:
                    with group_lock:
                        if not group_enabled[group]:
                            # 组被禁用，丢弃消息但不报错（或可返回警告）
                            self.send_response(200)
                            self._send_cors_headers()
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(b'{"status":"dropped", "reason":"group disabled"}')
                            return
                
                if gateway_id in json_queues:
                    json_queues[gateway_id].put(json.dumps(payload, ensure_ascii=False))
                    
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok", "gateway":"%s"}' % gateway_id.encode())
                    return
                else:
                    self.send_response(400)
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(b'{"status":"error", "message":"Gateway queue not found"}')
                    return
                    
            except json.JSONDecodeError as e:
                print("[HTTP API] JSON decode error:", e)
                self.send_response(400)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"status":"error", "message":"Invalid JSON"}')
                return
            except Exception as e:
                print("[HTTP API ERROR]", e)
                self.send_response(500)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"status":"error", "message":"Internal server error"}')
                return
        
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            new_state = json.loads(post_data.decode('utf-8'))
            
            with state_lock:
                if "baojingdeng" in new_state:
                    DEVICE_STATE["baojingdeng"] = new_state["baojingdeng"]
                if "shuifa" in new_state:
                    DEVICE_STATE["shuifa"] = new_state["shuifa"]
            
            print("[HTTP API] Device state updated:", new_state)
            
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            
        except json.JSONDecodeError as e:
            print("[HTTP API] JSON decode error:", e)
            self.send_response(400)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"error", "message":"Invalid JSON"}')
        except Exception as e:
            print("[HTTP API ERROR]", e)
            self.send_response(500)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"error", "message":"Internal server error"}')

    def log_message(self, format, *args):
        pass


def start_http_api_server(host, port):
    server = ThreadingHTTPServer((host, port), DeviceStateHandler)
    print(" -> HTTP API server ready on %s (for device control)" % port)
    server.serve_forever()


# ======================== HTTP Whitelist API ========================
WHITELIST_STATE = {"devices": []}
whitelist_lock = threading.Lock()
whitelist_file_path = DEFAULT_WHITELIST_FILE

def load_whitelist():
    global WHITELIST_STATE
    if os.path.exists(whitelist_file_path):
        try:
            with open(whitelist_file_path, "r", encoding="utf-8") as f:
                WHITELIST_STATE = json.load(f)
                if "devices" not in WHITELIST_STATE:
                    WHITELIST_STATE = {"devices": []}
        except Exception as e:
            print("[WHITELIST API] Failed to load %s: %s" % (whitelist_file_path, e))
    else:
        save_whitelist()

def save_whitelist():
    try:
        with open(whitelist_file_path, "w", encoding="utf-8") as f:
            json.dump(WHITELIST_STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[WHITELIST API] Failed to save %s: %s" % (whitelist_file_path, e))

class WhitelistHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        
        with whitelist_lock:
            response_data = json.dumps(WHITELIST_STATE).encode("utf-8")
        self.wfile.write(response_data)

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length <= 0 or content_length > 40960:
                self.send_response(400)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"status":"error", "message":"Invalid content length"}')
                return

            post_data = self.rfile.read(content_length)
            new_state = json.loads(post_data.decode('utf-8'))
            
            with whitelist_lock:
                if "devices" in new_state and isinstance(new_state["devices"], list):
                    WHITELIST_STATE["devices"] = new_state["devices"]
                    save_whitelist()
                    print("[WHITELIST API] Whitelist updated with %d devices" % len(WHITELIST_STATE["devices"]))
                    
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    self.send_response(400)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"error", "message":"Expected format: {\\"devices\\": [\\"id1\\", \\"id2\\"]}"}')
            
        except json.JSONDecodeError as e:
            print("[WHITELIST API] JSON decode error:", e)
            self.send_response(400)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"error", "message":"Invalid JSON"}')
        except Exception as e:
            print("[WHITELIST API ERROR]", e)
            self.send_response(500)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"error", "message":"Internal server error"}')

    def log_message(self, format, *args):
        pass


def start_whitelist_api_server(host, port, file_path):
    global whitelist_file_path
    whitelist_file_path = file_path
    load_whitelist()
    server = ThreadingHTTPServer((host, port), WhitelistHandler)
    print(" -> HTTP Whitelist API server ready on %s" % port)
    server.serve_forever()


# ======================== Control HTTP API (port 11507) ========================
class ControlHandler(BaseHTTPRequestHandler):
    def _send_response(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/stop'):
            try:
                group = int(path[5:])
                if group not in (1,2,3):
                    raise ValueError
                with group_lock:
                    group_enabled[group] = False
                clear_group_queues(group)
                # 停止心跳
                self._send_response(200, {"status": "ok", "group": group, "action": "stop"})
                print("[CONTROL] Group %d stopped" % group)
            except Exception:
                self._send_response(400, {"status": "error", "message": "invalid group"})
        elif path.startswith('/recover'):
            try:
                group = int(path[8:])
                if group not in (1,2,3):
                    raise ValueError
                with group_lock:
                    group_enabled[group] = True
                # 恢复心跳
                self._send_response(200, {"status": "ok", "group": group, "action": "recover"})
                print("[CONTROL] Group %d recovered" % group)
            except Exception:
                self._send_response(400, {"status": "error", "message": "invalid group"})
        else:
            self._send_response(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        # 支持 POST 方式，行为与 GET 相同
        self.do_GET()

    def log_message(self, format, *args):
        pass


# ======================== Satellite Relay Module ========================
# 替代原 TQHandler + 内存队列，使用 SQLite 持久化 + ACK 可靠转发

class QueueFullError(RuntimeError):
    pass


class RelayDatabase:
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
                CREATE TABLE IF NOT EXISTS relay_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    sent_at TEXT,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_relay_messages_state_id
                ON relay_messages(state, id)
                """
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM relay_messages"
            ).fetchone()[0]
            if total > self.max_records:
                conn.execute(
                    """
                    DELETE FROM relay_messages
                    WHERE id IN (
                        SELECT id FROM relay_messages
                        WHERE state = 'sent'
                        ORDER BY id
                        LIMIT ?
                    )
                    """,
                    (total - self.max_records,),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def canonical_json(payload):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def enqueue(self, payload):
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
            existing = conn.execute(
                "SELECT id, state FROM relay_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if existing:
                conn.commit()
                return existing["id"], message_id, False, existing["state"]

            total = conn.execute(
                "SELECT COUNT(*) FROM relay_messages"
            ).fetchone()[0]
            if total >= self.max_records:
                remove_count = total - self.max_records + 1
                conn.execute(
                    """
                    DELETE FROM relay_messages
                    WHERE id IN (
                        SELECT id FROM relay_messages
                        WHERE state = 'sent'
                        ORDER BY id
                        LIMIT ?
                    )
                    """,
                    (remove_count,),
                )
                total = conn.execute(
                    "SELECT COUNT(*) FROM relay_messages"
                ).fetchone()[0]
                if total >= self.max_records:
                    conn.rollback()
                    raise QueueFullError(
                        "SQLite queue contains %s unconfirmed records" % total
                    )

            cursor = conn.execute(
                """
                INSERT INTO relay_messages (
                    message_id, payload_json, received_at, state
                ) VALUES (?, ?, ?, 'pending')
                """,
                (message_id, payload_json, datetime.now().astimezone().isoformat(timespec="milliseconds")),
            )
            row_id = cursor.lastrowid
            conn.commit()
            return row_id, message_id, True, "pending"
        finally:
            conn.close()

    def next_pending(self):
        conn = self.connect()
        try:
            row = conn.execute(
                """
                SELECT id, message_id, payload_json, attempt_count
                FROM relay_messages
                WHERE state = 'pending'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def mark_attempt(self, row_id):
        conn = self.connect()
        try:
            conn.execute(
                """
                UPDATE relay_messages
                SET attempt_count = attempt_count + 1,
                    last_attempt_at = ?, last_error = NULL
                WHERE id = ? AND state = 'pending'
                """,
                (datetime.now().astimezone().isoformat(timespec="milliseconds"), row_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_sent(self, row_id):
        conn = self.connect()
        try:
            conn.execute(
                """
                UPDATE relay_messages
                SET state = 'sent', sent_at = ?, last_error = NULL
                WHERE id = ? AND state = 'pending'
                """,
                (datetime.now().astimezone().isoformat(timespec="milliseconds"), row_id),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_error(self, row_id, error):
        conn = self.connect()
        try:
            conn.execute(
                """
                UPDATE relay_messages SET last_error = ?
                WHERE id = ? AND state = 'pending'
                """,
                (str(error)[:2000], row_id),
            )
            conn.commit()
        finally:
            conn.close()

    def latest_payload(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM relay_messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
        finally:
            conn.close()

    def statistics(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM relay_messages GROUP BY state"
            ).fetchall()
            result = {"pending": 0, "sent": 0}
            for row in rows:
                result[row["state"]] = row["count"]
            result["total"] = result.get("pending", 0) + result.get("sent", 0)
            result["max_records"] = self.max_records
            return result
        finally:
            conn.close()


class RuntimeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.core_connected = False
        self.core_peer = None

    def set_core(self, connected, peer=None):
        with self.lock:
            self.core_connected = bool(connected)
            self.core_peer = peer if connected else None

    def snapshot(self):
        with self.lock:
            return {
                "core_connected": self.core_connected,
                "core_peer": self.core_peer,
            }


def configure_client_socket(client):
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (
        ("TCP_KEEPIDLE", 30),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
    ):
        option = getattr(socket, name, None)
        if option is not None:
            try:
                client.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass


def wait_for_ack(client, receive_buffer, expected_message_id, timeout, stop_event):
    deadline = time.monotonic() + timeout
    while not stop_event.is_set():
        while b"\n" in receive_buffer:
            raw_line, receive_buffer = receive_buffer.split(b"\n", 1)
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ack = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                print("[SATELLITE] ignored invalid ACK: %r" % raw_line[:200])
                continue
            if (
                isinstance(ack, dict)
                and ack.get("type") == "ack"
                and str(ack.get("message_id") or "") == expected_message_id
            ):
                return receive_buffer
            print("[SATELLITE] ignored unmatched ACK: %r" % ack)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ACK timeout for %s" % expected_message_id)
        readable, _, _ = select.select([client], [], [], min(1.0, remaining))
        if not readable:
            continue
        incoming = client.recv(4096)
        if not incoming:
            raise ConnectionError("core closed connection before ACK")
        receive_buffer += incoming
        if len(receive_buffer) > 1024 * 1024:
            raise ValueError("ACK receive buffer too large")
    raise ConnectionError("service stopping")


def serve_core_clients(database, runtime, host, port, send_interval, ack_timeout, stop_event):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)
    server.settimeout(1.0)
    print("[SATELLITE] core TCP listening on %s:%s" % (host, port))

    try:
        while not stop_event.is_set():
            try:
                client, peer = server.accept()
            except socket.timeout:
                continue

            configure_client_socket(client)
            peer_text = "%s:%s" % peer
            runtime.set_core(True, peer_text)
            print("[SATELLITE] core connected: %s" % peer_text)
            current_row_id = None
            receive_buffer = b""

            try:
                while not stop_event.is_set():
                    row = database.next_pending()
                    if row is not None:
                        current_row_id = row["id"]
                        database.mark_attempt(current_row_id)
                        wire_data = row["payload_json"].encode("utf-8") + b"\n"
                        client.sendall(wire_data)
                        receive_buffer = wait_for_ack(
                            client,
                            receive_buffer,
                            row["message_id"],
                            ack_timeout,
                            stop_event,
                        )
                        database.mark_sent(current_row_id)
                        print(
                            "[SATELLITE] forwarded+acked id=%s message_id=%s bytes=%s core=%s"
                            % (
                                current_row_id,
                                row["message_id"],
                                len(wire_data),
                                peer_text,
                            )
                        )
                        current_row_id = None
                        if send_interval > 0:
                            stop_event.wait(send_interval)
                        continue

                    # 没有待发记录时检测对端是否已经关闭连接
                    readable, _, _ = select.select([client], [], [], 1.0)
                    if readable:
                        incoming = client.recv(4096)
                        if not incoming:
                            raise ConnectionError("core closed connection")
                        print("[SATELLITE] ignored %s bytes received from core" % len(incoming))
            except (ConnectionError, OSError, ValueError) as exc:
                if current_row_id is not None:
                    database.mark_error(current_row_id, exc)
                print("[SATELLITE] core disconnected: %s (%s)" % (peer_text, exc))
            finally:
                runtime.set_core(False)
                try:
                    client.close()
                except OSError:
                    pass
    finally:
        server.close()


def make_satellite_http_handler(database, runtime):
    class SatelliteHttpHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print("[SATELLITE HTTP] %s - %s" % (self.client_address[0], fmt % args))

        def send_json(self, status, payload):
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):
            if self.path.split("?", 1)[0] not in ("/", "/ingest"):
                self.send_json(404, {"status": "error", "error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_HTTP_BODY:
                self.send_json(
                    400,
                    {"status": "error", "error": "invalid Content-Length"},
                )
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON root must be an object")
                row_id, message_id, inserted, state = database.enqueue(payload)
            except QueueFullError as exc:
                self.send_json(
                    503,
                    {"status": "retry", "error": str(exc)},
                )
                return
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"status": "error", "error": str(exc)})
                return
            except Exception as exc:
                print("[SATELLITE] SQLite enqueue failed: %s" % exc)
                self.send_json(500, {"status": "error", "error": "database error"})
                return

            print(
                "[SATELLITE] stored id=%s message_id=%s inserted=%s state=%s"
                % (row_id, message_id, inserted, state)
            )
            self.send_json(
                200,
                {
                    "status": "ok",
                    "stored": True,
                    "duplicate": not inserted,
                    "queue_id": row_id,
                    "message_id": message_id,
                    "state": state,
                },
            )

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/latest.json"):
                payload = database.latest_payload()
                if payload is None:
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_json(200, payload)
                return

            if path == "/health":
                result = {
                    "status": "ok",
                    "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "database": database.path,
                }
                result.update(database.statistics())
                result.update(runtime.snapshot())
                self.send_json(200, result)
                return

            self.send_json(404, {"status": "error", "error": "not found"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return SatelliteHttpHandler


def start_satellite_http_server(host, port, database, runtime):
    handler = make_satellite_http_handler(database, runtime)
    server = ThreadingHTTPServer((host, port), handler)
    print("[SATELLITE] HTTP listening on %s:%s (SQLite: %s)" % (host, port, database.path))
    server.serve_forever()


# ======================== Old framed protocol ========================
def recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def recv_packet(sock, prefix=b""):
    if prefix:
        header = prefix + recv_exactly(sock, 12 - len(prefix))
    else:
        header = recv_exactly(sock, 12)

    packet_type_bytes, body_size = struct.unpack("!4sQ", header)
    packet_type = packet_type_bytes.decode("ascii", errors="replace")

    if body_size > MAX_PACKET_SIZE:
        raise ValueError("packet body too large: %s bytes" % body_size)

    body = recv_exactly(sock, body_size)
    return header, packet_type, body


def extract_jpeg_from_body(body):
    if len(body) < 4:
        return b""
    meta_len = struct.unpack("!I", body[:4])[0]
    if 4 + meta_len > len(body):
        return b""
    return body[4 + meta_len :]


def send_forward_packet(sock, gateway_id, timestamp, frame_seq, original_header, original_body):
    id_bytes = gateway_id.encode("utf-8")
    payload = struct.pack("!B", len(id_bytes))
    payload += id_bytes
    payload += struct.pack("!d", timestamp)
    payload += struct.pack("!I", frame_seq)
    payload += original_header
    payload += original_body
    sock.sendall(payload)


class ImageStorage:
    def __init__(self, base_dir, max_files=500):
        self.base_dir = base_dir
        self.max_files = max_files
        self._counters = defaultdict(int)
        self._write_queue = queue.Queue(maxsize=2000)
        self._pending_writes = defaultdict(int)
        self._lock = threading.Lock()
        self._all_done = threading.Condition(self._lock)
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        os.makedirs(base_dir, exist_ok=True)

    def save_async(self, gateway_id, packet_type, jpeg_data):
        if not jpeg_data:
            return
        try:
            self._write_queue.put_nowait((gateway_id, packet_type, jpeg_data))
            with self._lock:
                self._pending_writes[gateway_id] += 1
        except queue.Full:
            pass

    def _writer_loop(self):
        while True:
            gateway_id, packet_type, jpeg_data = self._write_queue.get()
            try:
                self._save_to_disk(gateway_id, packet_type, jpeg_data)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._pending_writes[gateway_id] -= 1
                    if self._pending_writes[gateway_id] <= 0:
                        self._all_done.notify_all()

    def _save_to_disk(self, gateway_id, packet_type, jpeg_data):
        gateway_dir = os.path.join(self.base_dir, gateway_id, packet_type)
        os.makedirs(gateway_dir, exist_ok=True)
        counter_key = "%s_%s" % (gateway_id, packet_type)
        self._counters[counter_key] += 1
        seq = self._counters[counter_key]
        now = datetime.now()
        filename = "%s_%s_%06d.jpg" % (
            packet_type,
            now.strftime("%Y%m%d_%H%M%S"),
            seq,
        )
        with open(os.path.join(gateway_dir, filename), "wb") as writer:
            writer.write(jpeg_data)
        if seq % 100 == 0:
            self._cleanup(gateway_dir)

    def _cleanup(self, directory):
        try:
            files = sorted(os.listdir(directory))
            if len(files) > self.max_files:
                for filename in files[: len(files) - self.max_files]:
                    os.remove(os.path.join(directory, filename))
        except Exception:
            pass

    def clear_gateway(self, gateway_id):
        deadline = time.time() + 5.0
        with self._lock:
            while self._pending_writes[gateway_id] > 0 and deadline > time.time():
                self._all_done.wait(timeout=0.5)

        gateway_dir = os.path.join(self.base_dir, gateway_id)
        if os.path.exists(gateway_dir):
            try:
                shutil.rmtree(gateway_dir)
            except Exception:
                pass


def handle_framed_sender(conn, addr, storage, initial_prefix, custom_id):
    gateway_id = custom_id if custom_id else addr[0]
    if gateway_id not in framed_queues:
        framed_queues[gateway_id] = DroppingQueue(maxsize=DEFAULT_MAX_QUEUE_SIZE)

    frame_queue = framed_queues[gateway_id]
    print(
        "[+OLD UP] framed sender %s connected (%s:%s)"
        % (gateway_id, addr[0], addr[1])
    )

    try:
        frame_count = 0
        last_frame_count = 0
        last_report = time.time()
        prefix = initial_prefix

        while True:
            header, packet_type, body = recv_packet(conn, prefix)
            prefix = b""

            frame_count += 1
            timestamp = time.time()

            jpeg_data = extract_jpeg_from_body(body)
            storage.save_async(gateway_id, packet_type, jpeg_data)

            # 检查该 gateway 所属组是否启用
            group = get_group_for_gateway(gateway_id)
            if group is not None:
                with group_lock:
                    if not group_enabled[group]:
                        # 丢弃该帧
                        continue

            frame_queue.put((gateway_id, timestamp, frame_count, header, body))

            now = time.time()
            if now - last_report >= 5.0:
                fps = (frame_count - last_frame_count) / (now - last_report)
                print(
                    "[OLD UP STAT] %s total=%s rate=%.1f FPS queue=%s/%s"
                    % (
                        gateway_id,
                        frame_count,
                        fps,
                        frame_queue.qsize(),
                        frame_queue.maxsize,
                    )
                )
                last_report = now
                last_frame_count = frame_count

    except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
        print("[-OLD UP] framed sender %s disconnected" % gateway_id)
    except Exception as exc:
        print("[OLD ERROR] framed sender %s failed: %s" % (gateway_id, exc))
    finally:
        removed = frame_queue.clear()
        if removed:
            print("[OLD CLEAN] cleared %s queued frames for %s" % (removed, gateway_id))
        storage.clear_gateway(gateway_id)
        try:
            conn.close()
        except OSError:
            pass


def enable_tcp_keepalive(conn, idle=30, interval=10, count=3):
    """下行消费者线程只发不收（get→sendall，从不 recv），对端断开时收不到
    EOF；5G 运营商 NAT 还会静默回收空闲 TCP 映射，核心侧的 FIN 都送不进
    服务器——旧消费者线程就此变成僵尸，与重连后的新消费者轮流抢共享队列
    里的消息（业务表现：一次发 5 条核心只见到第 1/3/5 条）。开 keepalive：
    探测包既保住 NAT 映射，又让死连接在 idle+interval*count 秒后出错，僵尸
    线程偷到下一条消息时 sendall 抛异常自行退出。"""
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt_name, value in (
        ("TCP_KEEPIDLE", idle),
        ("TCP_KEEPINTVL", interval),
        ("TCP_KEEPCNT", count),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is not None:
            try:
                conn.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass


def handle_framed_receiver(conn, addr, gateway_id, pre_buffer_count):
    frame_queue = framed_queues[gateway_id]
    print(
        "[+OLD DOWN] core connected for %s on old channel (%s:%s)"
        % (gateway_id, addr[0], addr[1])
    )
    enable_tcp_keepalive(conn)

    try:
        buffer = []
        deadline = time.time() + 5.0
        while len(buffer) < pre_buffer_count and time.time() < deadline:
            try:
                buffer.append(frame_queue.get(timeout=0.2))
            except queue.Empty:
                if buffer:
                    break

        for queued in buffer:
            gw_id, timestamp, seq, header, body = queued
            send_forward_packet(conn, gw_id, timestamp, seq, header, body)

        total_sent = len(buffer)
        last_total_sent = total_sent
        last_report = time.time()

        while True:
            try:
                gw_id, timestamp, seq, header, body = frame_queue.get(timeout=10.0)
            except queue.Empty:
                print("[OLD DOWN] %s no new frame, waiting..." % gateway_id)
                continue

            send_forward_packet(conn, gw_id, timestamp, seq, header, body)
            total_sent += 1

            now = time.time()
            if now - last_report >= 5.0:
                fps = (total_sent - last_total_sent) / (now - last_report)
                print(
                    "[OLD DOWN STAT] %s total=%s rate=%.1f FPS"
                    % (gateway_id, total_sent, fps)
                )
                last_report = now
                last_total_sent = total_sent

    except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
        print("[-OLD DOWN] core disconnected for %s" % gateway_id)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def start_framed_receiver_port(host, gateway_id, port, pre_buffer_count):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(5)
    print(" -> old framed channel %s for %s" % (port, gateway_id))

    while True:
        try:
            conn, addr = sock.accept()
            threading.Thread(
                target=handle_framed_receiver,
                args=(conn, addr, gateway_id, pre_buffer_count),
                daemon=True,
            ).start()
        except Exception as exc:
            print("[OLD PORT ERROR] %s: %s" % (port, exc))


# ======================== New JSON protocol ========================
def normalize_gateway_id(value):
    text = str(value).strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


GATEWAY4_REGISTERED_ID = "gateway_4"


def is_gateway4_registration(value):
    return str(value).strip() == GATEWAY4_REGISTERED_ID


def canonical_registered_gateway_id(value):
    """Return the canonical ID for the strictly registered gateway_4."""
    return "gateway_4" if is_gateway4_registration(value) else value


def find_json_gateway_id(payload):
    if not isinstance(payload, dict):
        return None

    alias_to_gateway = {}
    for gateway_id, config in JSON_GATEWAY_PORT_MAP.items():
        for alias in config["aliases"]:
            alias_to_gateway[normalize_gateway_id(alias)] = gateway_id

    for field in JSON_GATEWAY_ID_FIELDS:
        if field not in payload:
            continue
        normalized = normalize_gateway_id(payload[field])
        if normalized in alias_to_gateway:
            return alias_to_gateway[normalized]

    for value in payload.values():
        normalized = normalize_gateway_id(value)
        if normalized in alias_to_gateway:
            return alias_to_gateway[normalized]

    return None


class JsonObjectExtractor:
    def __init__(self, max_buffer_size):
        self.max_buffer_size = max_buffer_size
        self.buffer = ""
        self.start = None
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.scan_position = 0

    @property
    def remainder(self):
        return self.buffer

    def feed(self, text):
        self.buffer += text
        if len(self.buffer.encode("utf-8")) > self.max_buffer_size:
            self.reset()
            raise BufferError("JSON receive buffer exceeded %s bytes" % self.max_buffer_size)

        objects = []
        index = self.scan_position

        while index < len(self.buffer):
            ch = self.buffer[index]

            if self.start is None:
                if ch == "{":
                    if index > 0:
                        self.buffer = self.buffer[index:]
                        index = 0
                    self.start = 0
                    self.depth = 1
                    self.in_string = False
                    self.escaped = False
                    index += 1
                else:
                    index += 1
                continue

            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif ch == "\\":
                    self.escaped = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"':
                self.in_string = True
            elif ch == "{":
                self.depth += 1
            elif ch == "}":
                self.depth -= 1
                if self.depth == 0:
                    end = index + 1
                    objects.append(self.buffer[self.start : end])
                    self.buffer = self.buffer[end:]
                    self.start = None
                    self.scan_position = 0
                    index = 0
                    continue

            index += 1

        self.scan_position = index
        return objects

    def reset(self):
        self.buffer = ""
        self.start = None
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.scan_position = 0


def enqueue_json(json_data, addr):
    try:
        payload = json.loads(json_data)
    except json.JSONDecodeError as exc:
        print("[JSON WARN] invalid JSON from %s:%s: %s" % (addr[0], addr[1], exc))
        return False

    gateway_id = find_json_gateway_id(payload)
    if gateway_id is None:
        print(
            "[JSON WARN] cannot route JSON from %s:%s, missing Gateway1/Gateway2 field: %s"
            % (addr[0], addr[1], json_data)
        )
        return False

    # 检查组状态
    group = get_group_for_gateway(gateway_id)
    if group is not None:
        with group_lock:
            if not group_enabled[group]:
                # 组被禁用，丢弃该 JSON
                return False

    json_queues[gateway_id].put(json_data)
    config = JSON_GATEWAY_PORT_MAP[gateway_id]
    
    with enqueue_stats_lock:
        if gateway_id not in enqueue_stats:
            enqueue_stats[gateway_id] = {"count": 0, "last_time": time.time()}
        
        stats = enqueue_stats[gateway_id]
        stats["count"] += 1
        now = time.time()
        
        if now - stats["last_time"] >= 5.0:
            rate = stats["count"] / (now - stats["last_time"])
            print(
                "[JSON QUEUE STAT] %s -> port %s | queue=%s/%s | rate=%.1f msg/s"
                % (
                    config["display"],
                    config["port"],
                    json_queues[gateway_id].qsize(),
                    json_queues[gateway_id].maxsize,
                    rate
                )
            )
            stats["count"] = 0
            stats["last_time"] = now

    return True


def handle_json_sender(conn, addr, initial_bytes, max_buffer_size):
    print("[+JSON UP] socketproject gateway connected (%s:%s)" % (addr[0], addr[1]))
    extractor = JsonObjectExtractor(max_buffer_size)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    total_received = 0
    last_total = 0
    last_report = time.time()

    try:
        with conn:
            if initial_bytes:
                text = decoder.decode(initial_bytes)
                for json_data in extractor.feed(text):
                    if enqueue_json(json_data, addr):
                        total_received += 1

            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break

                text = decoder.decode(chunk)
                try:
                    json_objects = extractor.feed(text)
                except BufferError as exc:
                    print("[JSON WARN] %s:%s buffer reset: %s" % (addr[0], addr[1], exc))
                    continue

                for json_data in json_objects:
                    if enqueue_json(json_data, addr):
                        total_received += 1

                now = time.time()
                if now - last_report >= 5.0:
                    rate = (total_received - last_total) / (now - last_report)
                    print(
                        "[JSON UP STAT] %s:%s total=%s rate=%.1f json/s"
                        % (addr[0], addr[1], total_received, rate)
                    )
                    last_report = now
                    last_total = total_received

            final_text = decoder.decode(b"", final=True)
            if final_text:
                for json_data in extractor.feed(final_text):
                    if enqueue_json(json_data, addr):
                        total_received += 1
    except (ConnectionResetError, UnicodeDecodeError, OSError) as exc:
        print("[JSON ERROR] sender %s:%s failed: %s" % (addr[0], addr[1], exc))
    finally:
        if extractor.remainder.strip():
            print(
                "[JSON WARN] sender %s:%s closed with incomplete data: %s"
                % (addr[0], addr[1], extractor.remainder)
            )
        print("[-JSON UP] socketproject gateway disconnected (%s:%s)" % (addr[0], addr[1]))


def enqueue_gateway4_fire(json_data, addr, registered_gateway_id):
    """Validate one registered gateway4 fire status and retain the latest state."""
    try:
        payload = json.loads(json_data)
    except json.JSONDecodeError as exc:
        print(
            "[GW4 FIRE WARN] invalid JSON from %s:%s: %s"
            % (addr[0], addr[1], exc)
        )
        return False

    if not isinstance(payload, dict) or "fire" not in payload:
        print(
            "[GW4 FIRE WARN] registered sender %s sent non-fire JSON from %s:%s: %s"
            % (registered_gateway_id, addr[0], addr[1], json_data)
        )
        return False

    # The dedicated 11421 channel identifies the destination. Preserve all
    # business fields (especially fire/timestamp/scene) without injecting a
    # routing field into the shortwave payload.
    wire_data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    gateway4_fire_queue.put(wire_data)
    print(
        "[GW4 FIRE UP] %s:%s gateway=%s fire=%s timestamp=%s scene=%s queue=%s/%s"
        % (
            addr[0],
            addr[1],
            registered_gateway_id,
            payload.get("fire"),
            payload.get("timestamp"),
            payload.get("scene"),
            gateway4_fire_queue.qsize(),
            gateway4_fire_queue.maxsize,
        )
    )
    return True


def handle_gateway4_fire_sender(
    conn,
    addr,
    initial_bytes,
    registered_gateway_id,
    max_buffer_size,
):
    """Receive newline-delimited or concatenated JSON after a gateway4 handshake."""
    print(
        "[+GW4 FIRE UP] registered sender %s connected (%s:%s)"
        % (registered_gateway_id, addr[0], addr[1])
    )
    extractor = JsonObjectExtractor(max_buffer_size)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

    def process_bytes(data):
        text = decoder.decode(data)
        for json_data in extractor.feed(text):
            enqueue_gateway4_fire(json_data, addr, registered_gateway_id)

    try:
        with conn:
            if initial_bytes:
                process_bytes(initial_bytes)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                process_bytes(chunk)
            final_text = decoder.decode(b"", final=True)
            if final_text:
                for json_data in extractor.feed(final_text):
                    enqueue_gateway4_fire(json_data, addr, registered_gateway_id)
    except (BufferError, ConnectionResetError, UnicodeDecodeError, OSError) as exc:
        print(
            "[GW4 FIRE ERROR] sender %s at %s:%s failed: %s"
            % (registered_gateway_id, addr[0], addr[1], exc)
        )
    finally:
        if extractor.remainder.strip():
            print(
                "[GW4 FIRE WARN] sender %s:%s closed with incomplete data: %s"
                % (addr[0], addr[1], extractor.remainder)
            )
        print(
            "[-GW4 FIRE UP] registered sender %s disconnected (%s:%s)"
            % (registered_gateway_id, addr[0], addr[1])
        )


def handle_gateway4_fire_receiver(conn, addr, port):
    """Deliver gateway4 fire JSON to the gateway4 edge board on port 11421."""
    configure_client_socket(conn)
    print(
        "[+GW4 FIRE DOWN] gateway4 edge connected on %s (%s:%s)"
        % (port, addr[0], addr[1])
    )
    try:
        with conn:
            while True:
                json_data = gateway4_fire_queue.get(timeout=None)
                try:
                    conn.sendall(json_data.encode("utf-8") + b"\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # Preserve the latest state for the reconnecting edge board.
                    gateway4_fire_queue.put_if_empty(json_data)
                    raise
                print(
                    "[GW4 FIRE DOWN] port=%s bytes=%s payload=%s"
                    % (port, len(json_data.encode("utf-8")), json_data)
                )
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        print(
            "[GW4 FIRE DOWN WARN] edge disconnected from %s: %s" % (port, exc)
        )
    finally:
        print("[-GW4 FIRE DOWN] gateway4 edge closed (%s:%s)" % addr)


def start_gateway4_fire_downstream(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(5)
    print(" -> gateway4 fire JSON downstream %s" % port)
    while True:
        try:
            conn, addr = sock.accept()
            threading.Thread(
                target=handle_gateway4_fire_receiver,
                args=(conn, addr, port),
                daemon=True,
            ).start()
        except Exception as exc:
            print("[GW4 FIRE PORT ERROR] %s: %s" % (port, exc))


def handle_json_receiver(conn, addr, gateway_id):
    config = JSON_GATEWAY_PORT_MAP[gateway_id]
    json_queue = json_queues[gateway_id]
    print(
        "[+JSON DOWN] core connected on %s for %s (%s:%s)"
        % (config["port"], config["display"], addr[0], addr[1])
    )
    enable_tcp_keepalive(conn)

    total_sent = 0
    last_total = 0
    last_report = time.time()

    try:
        with conn:
            while True:
                try:
                    json_data = json_queue.get(timeout=10.0)
                except queue.Empty:
                    print(
                        "[JSON DOWN] %s port %s has no new JSON, waiting..."
                        % (config["display"], config["port"])
                    )
                    continue

                conn.sendall(json_data.encode("utf-8") + b"\n")
                total_sent += 1

                now = time.time()
                if now - last_report >= 5.0:
                    rate = (total_sent - last_total) / (now - last_report)
                    print(
                        "[JSON DOWN STAT] %s port=%s total=%s rate=%.1f json/s"
                        % (config["display"], config["port"], total_sent, rate)
                    )
                    last_report = now
                    last_total = total_sent

    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        print(
            "[JSON WARN] core disconnected from %s port %s: %s"
            % (config["display"], config["port"], exc)
        )
    finally:
        print(
            "[-JSON DOWN] core closed for %s (%s:%s)"
            % (config["display"], addr[0], addr[1])
        )


def start_json_receiver_port(host, gateway_id):
    config = JSON_GATEWAY_PORT_MAP[gateway_id]
    port = config["port"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(5)
    print(" -> JSON channel %s for %s" % (port, config["display"]))

    while True:
        try:
            conn, addr = sock.accept()
            threading.Thread(
                target=handle_json_receiver,
                args=(conn, addr, gateway_id),
                daemon=True,
            ).start()
        except Exception as exc:
            print("[JSON PORT ERROR] %s: %s" % (port, exc))


# ======================== Upstream protocol dispatcher ========================
def handle_incoming_connection(conn, addr, storage, json_max_buffer_size):
    try:
        first_byte = recv_exactly(conn, 1)
        if first_byte == b"V":
            handle_framed_sender(conn, addr, storage, b"V", None)
        elif first_byte == b"S":
            next_byte = recv_exactly(conn, 1)
            if next_byte == b"N":
                handle_framed_sender(conn, addr, storage, b"SN", None)
            else:
                id_len = struct.unpack("!B", next_byte)[0]
                custom_id = recv_exactly(conn, id_len).decode("utf-8", errors="replace")
                # Both the media sender and the fire-status sender use the same
                # S + uint8 length + gateway-id registration. Inspect the first
                # business byte after registration so gateway4 JSON is not
                # misparsed as a VID0/SNAP frame.
                first_payload_byte = recv_exactly(conn, 1)
                if (
                    is_gateway4_registration(custom_id)
                    and first_payload_byte in b"{ \t\r\n"
                ):
                    handle_gateway4_fire_sender(
                        conn,
                        addr,
                        first_payload_byte,
                        canonical_registered_gateway_id(custom_id),
                        min(
                            json_max_buffer_size,
                            DEFAULT_GATEWAY4_FIRE_MAX_BUFFER_SIZE,
                        ),
                    )
                else:
                    handle_framed_sender(
                        conn,
                        addr,
                        storage,
                        first_payload_byte,
                        canonical_registered_gateway_id(custom_id),
                    )
        else:
            handle_json_sender(conn, addr, first_byte, json_max_buffer_size)
    except ConnectionError:
        try:
            conn.close()
        except OSError:
            pass
    except Exception as exc:
        print("[DISPATCH ERROR] %s:%s failed: %s" % (addr[0], addr[1], exc))
        try:
            conn.close()
        except OSError:
            pass


def start_upstream_port(host, port, storage, json_max_buffer_size):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(50)
    print(" -> upstream port %s ready for old framed and new JSON senders" % port)

    try:
        while True:
            conn, addr = server_sock.accept()
            threading.Thread(
                target=handle_incoming_connection,
                args=(conn, addr, storage, json_max_buffer_size),
                daemon=True,
            ).start()
    finally:
        server_sock.close()


# ======================== Slice metrics relay ========================
SLICE_GATEWAY_ALIASES = {
    "gateway1": {"gateway1", "gateway_1", "gw1", "g1", "1"},
    "gateway2": {"gateway2", "gateway_2", "gw2", "g2", "2"},
    "gateway4": {"gateway4", "gateway_4", "gw4", "g4", "4"},
}


def find_slice_gateway_id(payload):
    """Resolve routing identity while leaving the payload gateway unchanged."""
    if not isinstance(payload, dict):
        return None
    alias_map = {}
    for gateway_id, aliases in SLICE_GATEWAY_ALIASES.items():
        for alias in aliases:
            alias_map[normalize_gateway_id(alias)] = gateway_id
    for field in JSON_GATEWAY_ID_FIELDS:
        if field in payload:
            gateway_id = alias_map.get(normalize_gateway_id(payload[field]))
            if gateway_id:
                return gateway_id
    return None


class LatestSliceStateHub:
    """Fan out updates while retaining only the newest state per gateway."""

    def __init__(self, history_size=256):
        self._condition = threading.Condition()
        self._latest = {}
        self._history = deque(maxlen=max(16, int(history_size)))
        self._version = 0

    def update(self, gateway_id, payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._condition:
            self._version += 1
            version = self._version
            self._latest[gateway_id] = encoded
            self._history.append((version, encoded))
            self._condition.notify_all()
            return version

    def initial(self):
        with self._condition:
            ordered = [
                self._latest[gateway_id]
                for gateway_id in ("gateway1", "gateway2", "gateway4")
                if gateway_id in self._latest
            ]
            return self._version, ordered

    def wait_after(self, cursor, timeout=15.0):
        with self._condition:
            if self._version <= cursor:
                self._condition.wait(timeout=timeout)
            if self._version <= cursor:
                return self._version, []
            updates = [encoded for version, encoded in self._history if version > cursor]
            if not updates:
                # Cursor fell behind the bounded history: resynchronize from latest.
                updates = [self._latest[key] for key in sorted(self._latest)]
            return self._version, updates


def handle_slice_upstream(conn, addr, hub, max_line_size):
    print("[+SLICE UP] edge connected: %s:%s" % (addr[0], addr[1]))
    buffer = b""
    accepted = 0
    try:
        with conn:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > max_line_size and b"\n" not in buffer:
                    raise ValueError("slice JSON line exceeds %s bytes" % max_line_size)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if len(line) > max_line_size:
                        print("[SLICE UP WARN] oversized line from %s:%s" % addr)
                        continue
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        print("[SLICE UP WARN] invalid JSON from %s:%s: %s" % (
                            addr[0], addr[1], exc
                        ))
                        continue
                    if not isinstance(payload, dict) or payload.get("type") != "slice_metrics":
                        print("[SLICE UP WARN] ignored non-slice payload from %s:%s" % addr)
                        continue
                    gateway_id = find_slice_gateway_id(payload)
                    if gateway_id is None:
                        print("[SLICE UP WARN] unknown gateway declaration from %s:%s: %r" % (
                            addr[0], addr[1], payload.get("gateway")
                        ))
                        continue
                    # Keep the exact top-level gateway value supplied by --gateway.
                    hub.update(gateway_id, payload)
                    accepted += 1
    except (ConnectionResetError, OSError, ValueError) as exc:
        print("[SLICE UP WARN] %s:%s disconnected: %s" % (addr[0], addr[1], exc))
    finally:
        print("[-SLICE UP] edge closed %s:%s accepted=%s" % (
            addr[0], addr[1], accepted
        ))


def start_slice_upstream(host, port, hub, max_line_size):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(20)
    print(" -> slice metrics upstream %s:%s" % (host, port))
    while True:
        conn, addr = server_sock.accept()
        threading.Thread(
            target=handle_slice_upstream,
            args=(conn, addr, hub, max_line_size),
            daemon=True,
        ).start()


def handle_slice_downstream(conn, addr, hub):
    print("[+SLICE DOWN] core connected on 11420 (%s:%s)" % (addr[0], addr[1]))
    cursor, initial = hub.initial()
    try:
        with conn:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for encoded in initial:
                conn.sendall(encoded.encode("utf-8") + b"\n")
            while True:
                new_cursor, updates = hub.wait_after(cursor)
                for encoded in updates:
                    conn.sendall(encoded.encode("utf-8") + b"\n")
                cursor = new_cursor
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        print("[-SLICE DOWN] core %s:%s disconnected: %s" % (
            addr[0], addr[1], exc
        ))


def start_slice_downstream(host, port, hub):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(10)
    print(" -> slice metrics downstream %s:%s" % (host, port))
    while True:
        conn, addr = server_sock.accept()
        threading.Thread(
            target=handle_slice_downstream,
            args=(conn, addr, hub),
            daemon=True,
        ).start()


# ======================== Real edge heartbeat input ========================

def handle_edge_heartbeat_client(conn, addr, registry, max_line_size):
    peer = "{}:{}".format(addr[0], addr[1])
    connection_token = object()
    claimed_gateway = None
    accepted = 0
    buffer = b""
    print("[+EDGE HEARTBEAT] connected %s" % peer)
    try:
        with conn:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > max_line_size and b"\n" not in buffer:
                    raise ValueError("heartbeat line exceeds %s bytes" % max_line_size)
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    if not raw_line.strip():
                        continue
                    if len(raw_line) > max_line_size:
                        print("[EDGE HEARTBEAT WARN] oversized line from %s" % peer)
                        continue
                    try:
                        payload = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        print("[EDGE HEARTBEAT WARN] invalid JSON from %s: %s" % (
                            peer, exc
                        ))
                        continue
                    if (
                        not isinstance(payload, dict)
                        or payload.get("type") != "edge_heartbeat"
                    ):
                        print("[EDGE HEARTBEAT WARN] invalid heartbeat from %s" % peer)
                        continue
                    gateway_key = normalize_edge_heartbeat_gateway(
                        payload.get("gateway")
                    )
                    if gateway_key is None:
                        print("[EDGE HEARTBEAT WARN] unknown gateway from %s" % peer)
                        continue
                    if claimed_gateway is None:
                        claimed_gateway = gateway_key
                        registry.claim(gateway_key, connection_token, peer)
                    elif gateway_key != claimed_gateway:
                        print("[EDGE HEARTBEAT WARN] gateway changed on one connection %s" % peer)
                        continue
                    if registry.update(
                        gateway_key, connection_token, payload, peer
                    ):
                        accepted += 1
    except (ConnectionResetError, OSError, ValueError) as exc:
        print("[EDGE HEARTBEAT WARN] %s disconnected: %s" % (peer, exc))
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print("[-EDGE HEARTBEAT] %s closed accepted=%s" % (peer, accepted))


def start_edge_heartbeat_server(host, port, registry, max_line_size):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host, port))
    server_sock.listen(20)
    print(" -> edge heartbeat upstream %s:%s" % (host, port))
    while True:
        conn, addr = server_sock.accept()
        threading.Thread(
            target=handle_edge_heartbeat_client,
            args=(conn, addr, registry, max_line_size),
            daemon=True,
        ).start()


# ======================== Main ========================
def main():
    global gateway4_fire_queue

    parser = argparse.ArgumentParser(
        description="Server router compatible with old framed protocol and new JSON protocol."
    )
    parser.add_argument("--host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_SENDER_PORT)
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_API_PORT, help="Port for HTTP Device API (11501)")
    parser.add_argument("--whitelist-port", type=int, default=DEFAULT_WHITELIST_PORT, help="Port for HTTP Whitelist API (11502)")
    parser.add_argument("--whitelist-file", default=DEFAULT_WHITELIST_FILE, help="File to store whitelist JSON")
    parser.add_argument("--storage", default=DEFAULT_STORAGE_DIR)
    parser.add_argument("--max-queue", type=int, default=DEFAULT_MAX_QUEUE_SIZE)
    parser.add_argument("--max-json-queue", type=int, default=DEFAULT_MAX_JSON_QUEUE_SIZE)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES_PER_GATEWAY)
    parser.add_argument("--pre-buffer", type=int, default=15)
    parser.add_argument("--json-max-buffer", type=int, default=DEFAULT_JSON_MAX_BUFFER_SIZE)
    # 卫星转发专用参数
    parser.add_argument("--satellite-db", default=DEFAULT_SATELLITE_DB, help="SQLite database for satellite relay")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS, help="Max records in SQLite")
    parser.add_argument("--send-interval", type=float, default=0.1, help="Interval between sending messages (seconds)")
    parser.add_argument("--ack-timeout", type=float, default=10.0, help="Timeout for ACK from core gateway (seconds)")
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT, help="Port for Control API (11507)")
    parser.add_argument("--heartbeat-host", default=DEFAULT_HEARTBEAT_HOST, help="Listen host for heartbeat")
    parser.add_argument("--heartbeat-port", type=int, default=DEFAULT_HEARTBEAT_PORT, help="Listen port for heartbeat")
    parser.add_argument("--link-status-port", type=int, default=DEFAULT_LINK_STATUS_PORT, help="Listen port for link status service (11417)")
    parser.add_argument("--slice-upstream-port", type=int, default=DEFAULT_SLICE_UPSTREAM_PORT, help="Edge slice metrics input port (11510)")
    parser.add_argument("--slice-downstream-port", type=int, default=DEFAULT_SLICE_DOWNSTREAM_PORT, help="Core slice metrics output port (11420)")
    parser.add_argument("--edge-heartbeat-port", type=int, default=DEFAULT_EDGE_HEARTBEAT_PORT, help="Real edge heartbeat input port (11511)")
    parser.add_argument("--edge-heartbeat-timeout", type=float, default=DEFAULT_EDGE_HEARTBEAT_TIMEOUT, help="Seconds before an edge gateway is offline")
    parser.add_argument(
        "--gateway4-fire-downstream-port",
        type=int,
        default=DEFAULT_GATEWAY4_FIRE_DOWNSTREAM_PORT,
        help="Gateway4 fire JSON output port (11421)",
    )
    args = parser.parse_args()

    # 初始化队列
    for gateway_id in FRAMED_GATEWAY_PORT_MAP:
        framed_queues[gateway_id] = DroppingQueue(args.max_queue)
    for gateway_id in JSON_GATEWAY_PORT_MAP:
        json_queues[gateway_id] = DroppingQueue(args.max_json_queue)
    # Fire is a current-state service. When the edge board is offline, retain
    # only the newest state so reconnecting does not replay stale alarms.
    gateway4_fire_queue = DroppingQueue(1)

    edge_heartbeat_registry = EdgeHeartbeatRegistry(args.edge_heartbeat_timeout)
    heartbeat_server = HeartbeatServer(
        edge_heartbeat_registry,
        args.heartbeat_host,
        args.heartbeat_port,
    )
    link_status_server = LinkStatusServer(
        args.host,
        args.link_status_port,
    )
    slice_hub = LatestSliceStateHub()

    storage = ImageStorage(args.storage, args.max_files)

    # ===== 初始化卫星转发模块 =====
    satellite_db = RelayDatabase(args.satellite_db, args.max_records)
    satellite_db.initialize()
    runtime = RuntimeState()
    stop_event = threading.Event()

    # 启动时清空卫星消息表（relay_messages）
    conn = satellite_db.connect()
    try:
        conn.execute("DELETE FROM relay_messages")
        conn.commit()
        print("[SATELLITE] Cleared relay_messages table on startup")
    except Exception as e:
        print("[SATELLITE] WARNING: Failed to clear relay_messages table: %s" % e)
    finally:
        conn.close()

    print("")
    print("=" * 70)
    print(" Server router: old framed protocol + new JSON protocol + HTTP API")
    print(" upstream listen (general TCP): %s:%s" % (args.host, args.port))
    print(" upstream listen (HTTP Device API): %s:%s" % (args.host, args.http_port))
    print(" upstream listen (HTTP Whitelist API): %s:%s" % (args.host, args.whitelist_port))
    print(" upstream listen (HTTP Satellite API): %s:%s (SQLite: %s)" % 
          (args.host, DEFAULT_TQ_PORT, satellite_db.path))
    print(" upstream listen (Control API): %s:%s" % (args.host, args.control_port))
    print(" upstream listen (Slice metrics): %s:%s" % (args.host, args.slice_upstream_port))
    print(" upstream listen (Edge heartbeat): %s:%s timeout=%.1fs" % (
        args.host, args.edge_heartbeat_port, args.edge_heartbeat_timeout
    ))
    print("")
    print(" old framed downstream:")
    for gateway_id, port in FRAMED_GATEWAY_PORT_MAP.items():
        print("   %s -> %s" % (gateway_id, port))
    print("")
    print(" new JSON downstream:")
    for config in JSON_GATEWAY_PORT_MAP.values():
        print("   %s -> %s" % (config["display"], config["port"]))
    print(" satellite downstream: 11410 (managed by SQLite + ACK)")
    print(" slice metrics downstream: %s" % args.slice_downstream_port)
    print(" gateway4 fire downstream: %s (JSON lines)" % args.gateway4_fire_downstream_port)
    print("")
    print(" Group Control:")
    print("   Group1: 11400(framed), 11406(JSON), heartbeat gateway=1")
    print("   Group2: 11401(framed), 11407(JSON), heartbeat gateway=2")
    print("   Group3: 11402,11403,11404,11405(framed), 11409(JSON), heartbeat gateway=3")
    print("")
    print(" Heartbeat listens on %s:%s, sends every %.1fs to connected clients" % (args.heartbeat_host, args.heartbeat_port, HEARTBEAT_INTERVAL))
    print(" Link Status listens on %s:%s, pushes every %.1fs" % (args.host, args.link_status_port, LINK_STATUS_PUSH_INTERVAL))
    print("=" * 70)
    print("")

    heartbeat_server.start()
    link_status_server.start()

    threading.Thread(
        target=start_edge_heartbeat_server,
        args=(
            args.host,
            args.edge_heartbeat_port,
            edge_heartbeat_registry,
            DEFAULT_EDGE_HEARTBEAT_MAX_LINE_SIZE,
        ),
        daemon=True,
        name="edge-heartbeat-upstream",
    ).start()

    threading.Thread(
        target=start_slice_upstream,
        args=(args.host, args.slice_upstream_port, slice_hub, DEFAULT_SLICE_MAX_LINE_SIZE),
        daemon=True,
        name="slice-upstream",
    ).start()
    threading.Thread(
        target=start_slice_downstream,
        args=(args.host, args.slice_downstream_port, slice_hub),
        daemon=True,
        name="slice-downstream",
    ).start()

    threading.Thread(
        target=start_gateway4_fire_downstream,
        args=(args.host, args.gateway4_fire_downstream_port),
        daemon=True,
        name="gateway4-fire-downstream",
    ).start()

    # 启动所有旧协议下发监听
    for gateway_id, port in FRAMED_GATEWAY_PORT_MAP.items():
        threading.Thread(
            target=start_framed_receiver_port,
            args=(args.host, gateway_id, port, args.pre_buffer),
            daemon=True,
        ).start()

    # 启动所有 JSON 协议下发监听（跳过卫星端口，因为由卫星模块管理）
    for gateway_id in JSON_GATEWAY_PORT_MAP:
        threading.Thread(
            target=start_json_receiver_port,
            args=(args.host, gateway_id),
            daemon=True,
        ).start()

    # 启动 HTTP 状态 API 监听 (11501)
    threading.Thread(
        target=start_http_api_server,
        args=(args.host, args.http_port),
        daemon=True,
    ).start()

    # 启动 HTTP 白名单 API 监听 (11502)
    threading.Thread(
        target=start_whitelist_api_server,
        args=(args.host, args.whitelist_port, args.whitelist_file),
        daemon=True,
    ).start()

    # ===== 启动控制 HTTP 服务 (11507) =====
    control_server = ThreadingHTTPServer((args.host, args.control_port), ControlHandler)
    threading.Thread(target=control_server.serve_forever, daemon=True).start()
    print(" -> Control HTTP API on %s:%s" % (args.host, args.control_port))

    # ===== 启动卫星转发服务（HTTP 11503 + TCP 11410） =====
    # HTTP 服务
    threading.Thread(
        target=start_satellite_http_server,
        args=(args.host, DEFAULT_TQ_PORT, satellite_db, runtime),
        daemon=True,
    ).start()
    # TCP 服务
    threading.Thread(
        target=serve_core_clients,
        args=(satellite_db, runtime, args.host, 11410, args.send_interval, args.ack_timeout, stop_event),
        daemon=True,
    ).start()

    # 启动通用的 11500 监听
    try:
        start_upstream_port(args.host, args.port, storage, args.json_max_buffer)
    except KeyboardInterrupt:
        print("\n[STOP] server stopped")
        heartbeat_server.stop()
        link_status_server.stop()
        stop_event.set()
        control_server.shutdown()


if __name__ == "__main__":
    main()
