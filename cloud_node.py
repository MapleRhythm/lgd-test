#!/usr/bin/env python3
"""Public cloud-node entry point for the final gateway source tree.

The files in this directory are deployment captures wrapped in a shell
heredoc.  This launcher removes only that wrapper in memory and executes the
captured source.  Console output is redacted so internal transport details do
not appear in a test-terminal recording.
"""

import os
import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent
_IP_PORT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")
_PORT = re.compile(r"(?<!\d)(?:10\d{3}|11\d{3}|7\d{3}|8\d{3})(?!\d)")
# Heartbeat status line from the captured gateway source: the display layer
# only watches it for state changes (link up/down colour, outline 2.2.5).
_HEARTBEAT_STATUS = re.compile(r"\[HEARTBEAT-UP\] gateway(\d+) heartbeat status=(\d+)")
# Per-channel uplink stat lines ([JSON-UP][json_chN][DETAIL] rate=... |
# last_json={full business payload}) are transport bookkeeping with a whole
# message attached: console noise for the demo, dropped by default below.
_JSON_UP_DETAIL = re.compile(r"\[JSON-UP\]\[[^\]]+\]\[DETAIL\]")


class RedactedStdout:
    def __init__(self, stream):
        self.stream = stream
        self._suppress_next_newline = False
        # Per-gateway last status: the relay interleaves heartbeat lines for
        # every gateway group (gateway1/2/3), so one shared value would see
        # each line as a flip against the previous gateway's status and
        # spam up/down notices.
        self._last_status = {}
        # CLOUD_JSON_DETAIL=1 re-enables the dropped [JSON-UP][DETAIL] stat
        # lines (debug path; pair with CLOUD_JSON_REPORT_INTERVAL for the
        # cadence).
        self._drop_json_detail = not os.environ.get("CLOUD_JSON_DETAIL")

    def _colour(self, code, text):
        # Same rule as the model layer: plain when piped or NO_COLOR is set.
        if os.environ.get("NO_COLOR") or not self.stream.isatty():
            return text
        return "\033[{}m{}\033[0m".format(code, text)

    def _heartbeat_notice(self, text):
        """Replace suppressed heartbeat chatter with a coloured state change.

        Periodic same-status pings stay invisible.  When the status flips the
        terminal gets exactly one line: red for link down (edge offline),
        green for recovery -- 5G 链路切换显示, display layer only.
        """
        match = _HEARTBEAT_STATUS.search(text)
        if not match:
            return
        gateway, status = match.group(1), match.group(2)
        if status == self._last_status.get(gateway):
            return
        first_observation = gateway not in self._last_status
        self._last_status[gateway] = status
        if first_observation:
            return
        if status == "0":
            line = "[HEARTBEAT] gateway{} 心跳中断：边缘离线（status=0）".format(gateway)
            self.stream.write(self._colour(31, line) + "\n")
        else:
            line = "[HEARTBEAT] gateway{} 心跳恢复：边缘在线（status={}）".format(gateway, status)
            self.stream.write(self._colour(32, line) + "\n")

    def write(self, text):
        if self._suppress_next_newline and text in {"\n", "\r\n"}:
            self._suppress_next_newline = False
            return len(text)
        self._suppress_next_newline = False
        # Core-gateway heartbeat chatter (periodic status pings plus rare
        # connect/reconnect transitions) is transport bookkeeping, not demo
        # output: drop it from the terminal, surfacing only the coloured
        # up/down transitions above.
        if "[HEARTBEAT-UP]" in text:
            self._heartbeat_notice(text)
            self._suppress_next_newline = True
            return len(text)
        # [JSON-UP][ch][DETAIL] stat lines (rate/total + last_json payload)
        # never reach the terminal; WARN/reconnect lines still do.  Same
        # split-write dance as heartbeat: eat the bare newline when print()
        # delivers it separately.
        if self._drop_json_detail and _JSON_UP_DETAIL.search(text):
            self._suppress_next_newline = not text.endswith("\n")
            return len(text)
        text = _IP_PORT.sub("[REDACTED ENDPOINT]", text)
        text = _PORT.sub("[REDACTED PORT]", text)
        text = text.replace("old framed downstream", "[REDACTED CHANNEL]")
        text = text.replace("new JSON downstream", "[REDACTED CHANNEL]")
        text = text.replace("upstream", "[REDACTED]")
        text = text.replace("UPSTREAM", "[REDACTED]")
        text = text.replace("server", "[REDACTED]")
        text = text.replace("Server", "[REDACTED]")
        self.stream.write(text)

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return self.stream.isatty()


def unwrap(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    if lines and lines[0].lstrip().startswith("cat >"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "EOF":
            lines = lines[:-1]
    return "".join(lines)


def load_module(name: str, path: Path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(unwrap(path), str(path), "exec"), module.__dict__)
    return module


def main() -> None:
    # Redaction layer is for demo-terminal recordings.  Real-board debugging
    # sets CLOUD_REDACT=0 to print raw endpoints/ports and unsuppressed
    # heartbeat lines (about one heartbeat line per second per gateway).
    if os.environ.get("CLOUD_REDACT", "1") != "0":
        sys.stdout = RedactedStdout(sys.stdout)
    config_module = load_module("config", ROOT / "original" / "config.py")
    # WSL2 has no BaoTong network interface; keep this optional hardware path
    # quiet while the cloud management node and protocol data paths run.
    config_module.BAOTONG_HF_ENABLED = False
    # Demo pacing: keep /latest.json served for 5 minutes after the last
    # packet so ./query_link_data.sh can still run its live end-to-end msg_id
    # check once a transfer finishes (deployment default in config.py is 30 s).
    config_module.JSON_MAX_AGE_SECONDS = 300.0
    # Real-time receive logging: 0 makes gateway_v1 print one
    # [JSON-UP][ch][DETAIL] line for EVERY received business JSON line
    # (rate/total are then per-message).  The demo display layer drops those
    # lines unless CLOUD_JSON_DETAIL=1 (real-board debugging sets that, or
    # CLOUD_REDACT=0); CLOUD_JSON_REPORT_INTERVAL=60 restores the
    # one-line-per-minute summary for quiet demo terminals.
    config_module.JSON_REPORT_INTERVAL = float(
        os.environ.get("CLOUD_JSON_REPORT_INTERVAL", "0"))
    source_path = ROOT / "original" / "gateway_v1.py"
    source = unwrap(source_path)
    namespace = {"__name__": "__main__", "__file__": str(source_path)}
    exec(compile(source, str(source_path), "exec"), namespace)


if __name__ == "__main__":
    main()
