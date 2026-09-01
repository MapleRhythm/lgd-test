#!/usr/bin/env python3
"""Public edge-node entry point for the final gateway source tree."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent
_IP_PORT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")


class RedactedStdout:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(_IP_PORT.sub("[REDACTED ENDPOINT]", text))

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
    load_module("edge_config", ROOT / "original" / "edge_config.py")
    source_path = ROOT / "original" / "gateway_merged.py"
    source = unwrap(source_path)
    namespace = {"__name__": "__main__", "__file__": str(source_path)}
    exec(compile(source, str(source_path), "exec"), namespace)


if __name__ == "__main__":
    main()
