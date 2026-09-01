#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

KEEP_DURATION="${KEEP_DURATION:-600}"
BANDWIDTH_DURATION="${BANDWIDTH_DURATION:-5}"

section() {
  local bar='════════════════════════════════════════════════════════════════════════════════'
  printf '\n\033[36m  %s\033[0m\n' "$bar"
  printf '\033[1;36m  %s\033[0m\n' "$1"
  printf '\033[36m  %s\033[0m\n' "$bar"
}

section '2.2.3 PRECONDITIONS'
./init_link_connect.sh --reset
./policy-route.sh --start
./msg-encap.sh --start

section '2.2.3 TEST STEPS'
./init_link_connect.sh
./check_link_connect.sh
./ping_link_test.sh
./start_test.sh
./query_service_log.sh
./keep_transfer.sh --duration "$KEEP_DURATION"
./multi_link_bandwidth.sh --duration "$BANDWIDTH_DURATION"
./edge_forward.sh --start
./start_test.sh
./query_link_data.sh
