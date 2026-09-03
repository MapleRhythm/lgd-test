#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_sender_common.sh
source "$SCRIPT_DIR/_sender_common.sh"
# 默认（后台）形态：传感器业务与独立光照设备 DEV-001 一并拉起——中断/
# 重启后重敲本命令即可，DEV-001 不会再漏拉。--fg（一键回归脚本内部用）
# 保持只跑传感器本身，回归统计不混入 DEV-001 的记录。
for arg in "$@"; do
  if [[ "$arg" == "--fg" || "$arg" == "--foreground" ]]; then
    sender_run start-sensor "$@"
    exit
  fi
done
( sender_run start-sensor "$@" )
( sender_run start-light "$@" )
