"""Central runtime configuration for the edge gateway.

Keep fixed deployment values and protocol timers here so gateway_merged.py can
be updated without searching through the business logic.  Command-line options
may still override the matching DEFAULT_* values at startup.
"""

import os

# Local ingress services.
DEFAULT_MEDIA_LISTEN_HOST = "0.0.0.0"
DEFAULT_MEDIA_LISTEN_PORT = 7777
DEFAULT_JSON_LISTEN_HOST = "0.0.0.0"
DEFAULT_JSON_LISTEN_PORT = 8888

# Cloud services.
DEFAULT_CLOUD_HOST = "47.99.47.169"
DEFAULT_CLOUD_PORT = 11500
DEFAULT_SLICE_METRICS_PORT = 11510
DEFAULT_SLICE_METRICS_INTERVAL = 1.0
DEFAULT_EDGE_HEARTBEAT_PORT = 11511
DEFAULT_EDGE_HEARTBEAT_INTERVAL = 1.0
DEFAULT_LINK_STATUS_PORT = 11417
DEFAULT_LINK_STATUS_TIMEOUT = 15.0

# BaoTong shortwave industrial PC.
DEFAULT_BAOTONG_HOST = "192.168.2.1"
DEFAULT_BAOTONG_PORT = 9100
DEFAULT_CALLEE_ID = "025202"
BAOTONG_BUFFER_SIZE = 4096
BAOTONG_MAX_PAYLOAD_SIZE = 4096
BAOTONG_SMS_MAX_CONTENT_BYTES = 80
BAOTONG_SNR_THRESHOLD = -23.0

# Shortwave business timers.  Edge link probing uses 200s (shorter than the
# core's 300s probe window) so a lost probe reply returns the edge to idle
# before the core gives up, avoiding simultaneous probing that jams the
# channel; the edge then stays passive until the next core call.  The stale
# radio/reset watchdog uses ten minutes.
SHORTWAVE_LINK_TEST_TIMEOUT_SECONDS = 200.0
SHORTWAVE_RESPONSE_TIMEOUT_SECONDS = 300.0
SHORTWAVE_RESET_TIMEOUT_SECONDS = 600.0

# The industrial PC emits an application heartbeat every 10 seconds.  This is
# an online/offline detector rather than a radio business timeout, so retain the
# previously agreed two-missed-heartbeat (20 second) rule.
BAOTONG_HEARTBEAT_TIMEOUT = 20.0
BAOTONG_HEARTBEAT_POLL_INTERVAL = 1.0

SHORTWAVE_BUSINESS_BY_GATEWAY = {
    "gateway1": "fire",
    "gateway2": "windspeed",
    "gateway4": "fire",
}

# Gateway identity.  Registration can override these at runtime.
DEFAULT_GATEWAY_ID = "gateway_1"
DEFAULT_GATEWAY_HANDSHAKE = False

# Queue and socket limits.
DEFAULT_JSON_MAX_BUFFER_SIZE = 20 * 1024 * 1024
DEFAULT_JSON_MAX_QUEUE_SIZE = 1000
DEFAULT_MEDIA_MAX_QUEUE_PACKETS = 120
BUFFER_SIZE = 65536
MAX_PACKET_SIZE = 20 * 1024 * 1024
RECONNECT_INTERVAL = 3.0
# 周期统计行（[JSON][RECV]/[JSON][SEND]）打印间隔，可用
# EDGE_REPORT_INTERVAL 覆盖；边缘演示终端默认放慢到 30s 降噪。
REPORT_INTERVAL = float(os.environ.get("EDGE_REPORT_INTERVAL", "5.0"))
TIME_SET_INTERVAL = 2.0

# Whitelist synchronization.
DEFAULT_WHITELIST_CLOUD_PORT = 11502
DEFAULT_WHITELIST_INTERVAL = 30.0
DEFAULT_WHITELIST_FILE = "whitelist.json"
DEFAULT_WHITELIST_HTTP_TIMEOUT = 5

# 400-GM12 USB serial satellite uplink.
DEFAULT_SATELLITE_PORT = "/dev/ttyUSB0"
DEFAULT_SATELLITE_BAUD = 115200
DEFAULT_SATELLITE_INTERVAL = 300.0
DEFAULT_SATELLITE_COMMAND_TIMEOUT = 4.0
DEFAULT_SATELLITE_RECONNECT_INTERVAL = 3.0
DEFAULT_SATELLITE_DATA_TYPE = 0
SATELLITE_MAX_PAYLOAD_BYTES = 120

# BaoTong framing.
FRAME_HEAD = b"\xAA\x55"
FRAME_TAIL = b"\x55\xAA"

# Logging and JSON stream resynchronization.
DETAIL_LOG_ENABLED = True
LOG_PREVIEW_CHARS = 500
RESYNC_MARKERS = (
    '{"packet_type"',
    '{"type"',
    '{"timestamp"',
    '{"gateway"',
    '{"gateway_id"',
    '{"data_source"',
    '{"fire"',
)
