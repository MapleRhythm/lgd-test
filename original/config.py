cat > config.py << 'EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fixed configuration for the core gateway service.

Edit this file and restart ``gateway-v1.service`` for changes to take effect.
Runtime state and protocol-processing logic remain in ``gateway_v1.py``.
"""

import os
import struct
from datetime import timezone, timedelta


# Upstream servers
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_B_HOST = "47.99.47.169"
DEFAULT_B_PORT = 11410
FRAMED_SERVER_INPUT_PORTS = [11400, 11401, 11402, 11403, 11404, 11405]
JSON_SERVER_INPUT_PORTS = [11406, 11407, 11408, 11409]
HEARTBEAT_SERVER_INPUT_PORT = 11416
HEARTBEAT_LINK_TIMEOUT_SECONDS = 5.0

# Network-slice telemetry: server 11420 -> core -> frontend 10017.
SLICE_METRICS_SERVER_PORT = 11420
SLICE_METRICS_FRONTEND_PORT = 10017
SLICE_METRICS_PUBLISH_INTERVAL_SECONDS = 1.0
SLICE_METRICS_STALE_SECONDS = 3.0
SLICE_METRICS_OFFLINE_SECONDS = 5.0
SLICE_GATEWAY_ALIASES = {
    "Gateway1": {"gateway1", "gateway_1", "gw1", "g1", "1"},
    "Gateway2": {"gateway2", "gateway_2", "gw2", "g2", "2"},
    "Gateway4": {"gateway4", "gateway_4", "gw4", "g4", "4"},
}
SLICE_GATEWAY_HEARTBEAT_GROUP = {
    "Gateway1": 1,
    "Gateway2": 2,
    "Gateway4": 3,
}
SLICE_GATEWAY_CALLEE_MAP = {
    "Gateway1": "025207",
    "Gateway2": "025203",
    "Gateway4": "025204",
}


# Frontend forwarding
VIDEO_FORWARD_MAP = {
    11400: 10000,
    11401: 10001,
    11402: 10002,
    11403: 10003,
    11404: 10004,
}
SNAP_FORWARD_MAP = {11400: 10005, 11402: 10006, 11405: 10007}
IGNORED_SNAP_PORTS = {11401, 11403, 11404}
JSON_FORWARD_MAP = {
    11406: 10008,
    11407: 10009,
    11408: 10011,
    11409: 10012,
}
CHANNEL_NAME = {
    11400: "ch0",
    11401: "ch1",
    11402: "ch2",
    11403: "ch3",
    11404: "ch4",
    11405: "ch5_snap",
    11406: "json_ch6",
    11407: "json_ch7",
    11408: "json_ch8",
    11409: "json_ch9",
}
FRONTEND_LISTEN_HOST = "0.0.0.0"


# Limits, wire formats and reporting
BUFFER_SIZE = 65536
MAX_BODY_SIZE = 20 * 1024 * 1024
MAX_META_SIZE = 1024 * 1024
MAX_GATEWAY_ID_SIZE = 255
MAX_JSON_LINE_SIZE = 1024 * 1024
RECONNECT_INTERVAL = 3.0
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
STREAM_WAIT_TIMEOUT_SECONDS = 15.0
PACKET_HEADER = struct.Struct("!4sQ")
META_LENGTH = struct.Struct("!I")
TIMESTAMP_STRUCT = struct.Struct("!d")
FRAME_SEQ_STRUCT = struct.Struct("!I")
VALID_PACKET_TYPES = {"VID0", "SNAP"}
MJPEG_BOUNDARY = "myboundary"
LOG_EVERY_VIDEO = 30
FPS_REPORT_INTERVAL = 5.0
JSON_REPORT_INTERVAL = 5.0
SNAPSHOT_MAX_AGE_SECONDS = 8.0
JSON_MAX_AGE_SECONDS = 30.0
BJ_TZ = timezone(timedelta(hours=8))


# JSON file output
JSON_FILE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "newjson")
)
JSON_FILE_ROUTE_MAP = {
    11408: os.path.join(JSON_FILE_DIR, "newjs3_sensor.json"),
    11409: os.path.join(JSON_FILE_DIR, "newjs4_inf.json"),
}


# BaoTong HF integration
BAOTONG_HF_ENABLED = True
BAOTONG_HF_LISTEN_HOST = "192.168.2.1"
BAOTONG_HF_LISTEN_PORT = 9100
BAOTONG_OUTPUT_GATEWAY = "Gateway1"
BAOTONG_OUTPUT_HTTP_PORT = 10010
BAOTONG_OUTPUT_IFACE = "fm1-mac2"
BAOTONG_OUTPUT_ADDR = "192.168.0.233/24"
BAOTONG_FRAME_HEAD = b"\xAA\x55"
BAOTONG_FRAME_TAIL = b"\x55\xAA"
BAOTONG_MAX_PAYLOAD_SIZE = 4096


# Shortwave call control
# HTTP 10016 accepts {"db":"-1".."5"}.
# db=-1: silent; db=0: poll; db=1..5: continuously call one radio.
SHORTWAVE_COMMAND_HTTP_PORT = 10016
# 2026-08-26 测试期精简：只轮询在线的两台边缘网关 201(gw1)/203(gw2)。
# 恢复全部站台时把 204/205/206 三行加回来即可。
# 轮询顺序 = dict 值序：025207 -> 025203。
SHORTWAVE_CALLEE_MAP = {
    "1": "025207",
    "2": "025203",
    # "3": "025204",
    # "4": "025205",
    # "5": "025206",
}
SHORTWAVE_POLL_REPEATS = 3
# 方案A：同一电台连续 SHORTWAVE_DEAD_STATION_THRESHOLD 次探测失败
# （link_test_timeout / hf_inactive / snr 不达标）即判定该台本轮不可达，
# 跳到轮询目录的下一个电台。任何一次探测成功都会把连续计数清零，
# 因此只惩罚"彻底叫不应"的台，不影响时好时坏的慢台。
SHORTWAVE_DEAD_STATION_THRESHOLD = 2
# Accept the first new linkstatus received after the current link_test starts.
# Shortwave link probing uses the agreed five-minute business timeout.  The
# response window starts when the fire template packet is sent (NOT when the
# probe starts), so a slow probe round trip no longer eats the SMS window;
# both windows below are therefore independent and each gets the full value.
# The independent stale-detect reset watchdog below uses ten minutes.
SHORTWAVE_LINK_TEST_TIMEOUT = 300.0
SHORTWAVE_RESPONSE_TIMEOUT = 300.0
SHORTWAVE_RETRY_DELAY = 1.0
SHORTWAVE_SNR_THRESHOLD = -23.0
DETECT_STALE_RESET_SECONDS = 600.0
DETECT_RESET_RETRY_SECONDS = 10.0
SHORTWAVE_CALL_FIRE = "false"


# Satellite relay
DEFAULT_SATELLITE_DB = "core_satellite_gateway.db"
DEFAULT_MAX_RECORDS = 5000
MAX_LINE_BYTES = 1024 * 1024
SATELLITE_HTTP_PORT = 10014
STATUS_HTTP_PORT = 10015
STATUS_MONITORED_PORTS = [11400, 11401, 11403]
SATELLITE_RECONNECT_INTERVAL_SECONDS = 3.0
EOF