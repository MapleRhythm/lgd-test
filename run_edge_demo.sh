#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

./init_link_connect.sh --reset
./check_link_connect.sh
./edge_forward.sh --start
./policy-route.sh --start
./msg-encap.sh --start

./edge_node.sh \
  --media-listen-host 127.0.0.1 \
  --media-listen-port 7777 \
  --json-listen-host 127.0.0.1 \
  --json-listen-port 8888 \
  --cloud-host 127.0.0.1 \
  --cloud-port 11500 \
  --link-status-host 127.0.0.1 \
  --link-status-port 11417 \
  --edge-heartbeat-port 11511 \
  --slice-metrics-port 11510 \
  --baotong-host 127.0.0.1 \
  --baotong-port 19100 \
  --disable-satellite \
  --time-set-interval 0 \
  --whitelist-interval 0 \
  --compact-log &
EDGE_PID=$!

cleanup() {
  kill "$EDGE_PID" 2>/dev/null || true
  wait "$EDGE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 3
export PROTOCOL_TEST_LIVE=1
export PROTOCOL_TEST_GATEWAY_HOST=127.0.0.1
export PROTOCOL_TEST_GATEWAY_PORT=8888
export PROTOCOL_TEST_STATE_DIR="$SCRIPT_DIR/.protocol-test"
./start_sensor_data.sh --fg --duration "${SENSOR_DURATION:-30}" --interval 2 --device-id "${PROTOCOL_TEST_DEVICE_SENSOR:-3C15DB07}"
wait "$EDGE_PID"
