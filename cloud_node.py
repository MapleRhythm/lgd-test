#!/usr/bin/env python3
"""Public cloud-node entry point for the final gateway source tree.

The files in this directory are deployment captures wrapped in a shell
heredoc.  This launcher removes only that wrapper in memory and executes the
captured source.  Console output is redacted so internal transport details do
not appear in a test-terminal recording.
"""

from __future__ import annotations

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


class RedactedStdout:
    def __init__(self, stream):
        self.stream = stream
        self._suppress_next_newline = False
        # Per-gateway last status: the relay interleaves heartbeat lines for
        # every gateway group (gateway1/2/3), so one shared value would see
        # each line as a flip against the previous gateway's status and
        # spam up/down notices.
        self._last_status = {}

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
    sys.stdout = RedactedStdout(sys.stdout)
    config_module = load_module("config", ROOT / "original" / "config.py")
    # WSL2 has no BaoTong network interface; keep this optional hardware path
    # quiet while the cloud management node and protocol data paths run.
    config_module.BAOTONG_HF_ENABLED = False
    # Demo pacing: keep /latest.json served for 5 minutes after the last
    # packet so ./query_link_data.sh still shows the live channel table once
    # a transfer finishes (deployment default in config.py is 30 s).
    config_module.JSON_MAX_AGE_SECONDS = 300.0
    # Demo pacing: the per-channel [JSON-UP][ch][DETAIL] stat lines flood the
    # cloud terminal every 5 s (config.py default); slow them to one line per
    # minute, same cadence as the throttled HTTP-SAT access log.  Set
    # CLOUD_JSON_REPORT_INTERVAL=5 to restore the fast debug cadence.
    config_module.JSON_REPORT_INTERVAL = float(
        os.environ.get("CLOUD_JSON_REPORT_INTERVAL", "60"))
    source_path = ROOT / "original" / "gateway_v1.py"
    source = unwrap(source_path)
    namespace = {"__name__": "__main__", "__file__": str(source_path)}
    exec(compile(source, str(source_path), "exec"), namespace)


if __name__ == "__main__":
    main()
