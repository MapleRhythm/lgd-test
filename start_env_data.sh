#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_sender_common.sh
source "$SCRIPT_DIR/_sender_common.sh"
sender_run "环境监测业务" start-env "$@"
