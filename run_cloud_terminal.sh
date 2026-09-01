#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Drop any desktop proxy: from WSL (NAT mode) it is unreachable and breaks
# the urllib-based whitelist mirror and live cloud queries in this terminal.
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="127.0.0.1,localhost"
export NO_PROXY="$no_proxy"

CLOUD_PID=""

cleanup() {
  if [[ -n "$CLOUD_PID" ]]; then
    kill "$CLOUD_PID" 2>/dev/null || true
    wait "$CLOUD_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

BAR='════════════════════════════════════════════════════════════════════════════════'
printf '\n\033[36m  %s\033[0m\n' "$BAR"
printf '\033[1;36m  %s\033[0m\n' 'CLOUD MANAGEMENT TERMINAL'
printf '\033[36m  %s\033[0m\n' "$BAR"

./cloud_node.sh \
  --server-host "${CLOUD_SERVER_HOST:-127.0.0.1}" \
  --b-host "${CLOUD_B_HOST:-127.0.0.1}" \
  --b-port "${CLOUD_B_PORT:-11410}" \
  --db "${CLOUD_DB:-/tmp/cloud-satellite.db}" \
  --reconnect-interval "${CLOUD_RECONNECT_INTERVAL:-1}" &
CLOUD_PID=$!

sleep "${CLOUD_STARTUP_WAIT:-2}"
if ! kill -0 "$CLOUD_PID" 2>/dev/null; then
  wait "$CLOUD_PID" 2>/dev/null || true
  printf '  cloud management node failed to start\n' >&2
  exit 1
fi
export PROTOCOL_TEST_LIVE=1
export PROTOCOL_TEST_CLOUD_LIVE=1
export PROTOCOL_TEST_STATE_DIR="${PROTOCOL_TEST_STATE_DIR:-$SCRIPT_DIR/.protocol-test}"
# Mirror (read-only) the whitelist the relay distributes to edge gateways so
# cloud-side query verdicts use the same device set as the real edge.
export PROTOCOL_TEST_WHITELIST_URL="${PROTOCOL_TEST_WHITELIST_URL:-http://${PROTOCOL_TEST_RELAY_HOST:-127.0.0.1}:11502/whitelist}"
printf '\n\033[32m  OK\033[0m   cloud management node started, enter the document commands in this terminal\n'
printf '  exit the prompt to stop the cloud management node\n\n'

PS1='cloud> ' bash --noprofile --norc -i
