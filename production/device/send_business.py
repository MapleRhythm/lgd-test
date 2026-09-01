#!/usr/bin/env python3
"""生产环境端侧设备业务发送器（真机部署）。

直接对接边缘网关 original/gateway_merged.py 的真机协议：
  - TCP 长连接到边缘网关 JSON 接入端口（默认 8888，监听 0.0.0.0）
  - 紧凑 JSON + '\\n' 逐行成帧（网关侧 JsonStreamExtractor 增量解析）
  - 报文字段与现网一致：event_id/device_id/biz_type/msg_id/packet_id/
    link_id/timestamp/type/packet_type/gateway/edge_gateway + 顶层业务读数

三种运行形态（对应大纲 2.2.3）：
  单条/定量   --count 5                    （步骤4 start_test）
  持续传输    --duration 600 --interval 1  （步骤7 keep_transfer，仅有线）
  多模态并发  --link all --duration 5      （步骤8 三链路并发吞吐）

风速来源：默认内置模拟（与现网 mock 传感器同分布，0.0~30.0 一位小数）；
真机接真实传感器时用 --value 指定单值，或 --values-file 逐行喂入实时读数。

审计：每条报文写入 <state>/sent.jsonl（msg_id 连续编号持久化在 counters.json），
供端到端核对与中转侧 STAT 计数对账。
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(__file__).resolve().parent / ".state"
LINKS = ("wifi", "bluetooth", "wired")


def now_bj() -> str:
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")


_COUNTERS: dict | None = None
_COUNTERS_LOCK = threading.Lock()
_SENT_LOCK = threading.Lock()


def _load_counters() -> dict:
    global _COUNTERS
    if _COUNTERS is None:
        try:
            _COUNTERS = json.loads((STATE_DIR / "counters.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _COUNTERS = {"msg": 0, "event": 0}
    return _COUNTERS


def next_ids() -> tuple[str, str]:
    """全进程共享、跨链路唯一的 msg/event 编号（并发安全）。"""
    with _COUNTERS_LOCK:
        counters = _load_counters()
        counters["msg"] = int(counters.get("msg", 0)) + 1
        counters["event"] = int(counters.get("event", 0)) + 1
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = STATE_DIR / "counters.json"
        tmp = STATE_DIR / ("counters.%d.tmp" % threading.get_ident())
        tmp.write_text(json.dumps(counters), encoding="utf-8")
        tmp.replace(path)
        return "MSG-%08d" % counters["msg"], "EVT-%08d" % counters["event"]


def append_sent(record: dict) -> None:
    with _SENT_LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "sent.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class ValueSource:
    """业务读数来源：内置模拟 / 固定值 / 文件逐行（真实传感器接入点）。"""

    def __init__(self, fixed: str | None, values_file: str | None):
        self.fixed = fixed
        self.lines: list[str] = []
        self.cursor = 0
        if values_file:
            self.lines = [
                line.strip()
                for line in Path(values_file).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def windspeed(self) -> str:
        if self.fixed is not None:
            return self.fixed
        if self.lines:
            value = self.lines[self.cursor % len(self.lines)]
            self.cursor += 1
            return value
        return "%.1f" % random.uniform(0.0, 30.0)


class Sender:
    """每条链路一个长连接（多模态并发时各自独立 TCP，贴近真实接入）。"""

    def __init__(self, host: str, port: int, device_id: str, biz_type: str,
                 link_id: str, source: ValueSource):
        self.host = host
        self.port = port
        self.device_id = device_id
        self.biz_type = biz_type
        self.link_id = link_id
        self.source = source
        self.sent = 0
        self.bytes_sent = 0
        self.last_msg_id = ""

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=5.0)
        sock.settimeout(10.0)
        return sock

    def send_one(self, sock: socket.socket | None) -> tuple[socket.socket | None, bool, int]:
        msg_id, event_id = next_ids()
        self.last_msg_id = msg_id
        message = {
            "event_id": event_id,
            "device_id": self.device_id,
            "biz_type": self.biz_type,
            "msg_id": msg_id,
            "packet_id": msg_id,
            "link_id": self.link_id,
            "timestamp": now_bj(),
            "type": self.biz_type,
            "packet_type": "alarm" if self.biz_type in ("fire", "control-alarm") else self.biz_type,
            "gateway": "gateway_1",
            "edge_gateway": "gateway_1",
            "windspeed": self.source.windspeed(),
            "data_source": "device-sender",
        }
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for attempt in (0, 1):
            try:
                if sock is None:
                    sock = self._connect()
                sock.sendall(data)
                self.sent += 1
                self.bytes_sent += len(data)
                append_sent({**message, "sent_at": datetime.now(BJ).isoformat(timespec="milliseconds")})
                return sock, True, len(data)
            except OSError:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                sock = None
                if attempt:
                    return None, False, 0
        return sock, False, 0


def run_single(args) -> int:
    source = ValueSource(args.value, args.values_file)
    sender = Sender(args.host, args.port, args.device_id, args.biz_type, args.link, source)
    sock = None
    deadline = time.monotonic() + args.duration if args.duration else None
    failures = 0
    try:
        while True:
            if args.count and sender.sent >= args.count:
                break
            if deadline and time.monotonic() >= deadline:
                break
            sock, ok, size = sender.send_one(sock)
            if not ok:
                failures += 1
                print("[SEND][FAIL] link=%s 连接边缘网关失败 (%s:%s)" % (args.link, args.host, args.port),
                      file=sys.stderr)
                if failures >= 3:
                    break
                time.sleep(1.0)
                continue
            print("[SEND] link=%s msg_id=%s bytes=%d windspeed=%s" %
                  (args.link, sender.last_msg_id, size, source.fixed or "sim"))
            if args.interval > 0 and not (args.count and sender.sent >= args.count):
                if deadline:
                    remaining = deadline - time.monotonic()
                    time.sleep(min(args.interval, max(remaining, 0.0)))
                else:
                    time.sleep(args.interval)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    print("[SUMMARY] link=%s 发送 %d 条 / %d 字节，失败 %d 次" %
          (args.link, sender.sent, sender.bytes_sent, failures))
    return 0 if failures == 0 and (args.count or 0) <= sender.sent else 1


def run_multi(args) -> int:
    """三模态并发（步骤8）：wifi/bluetooth/wired 各自独立连接同时发送。"""
    source = ValueSource(args.value, args.values_file)
    senders = {link: Sender(args.host, args.port, args.device_id, args.biz_type, link, source)
               for link in LINKS}
    started = time.monotonic()

    def worker(sender: Sender) -> None:
        sock = None
        deadline = started + args.duration
        while time.monotonic() < deadline:
            sock, _ok, _size = sender.send_one(sock)
            time.sleep(args.interval)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in senders.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = max(time.monotonic() - started, 1e-6)
    total_bytes = 0
    print("[BANDWIDTH] 并发窗口 %.1f 秒：" % elapsed)
    for link in LINKS:
        s = senders[link]
        total_bytes += s.bytes_sent
        print("  %-10s %4d 条  %8d B  %8.0f B/s" % (link, s.sent, s.bytes_sent, s.bytes_sent / elapsed))
    print("  %-10s %4d 条  %8d B  %8.0f B/s" %
          ("合计", sum(s.sent for s in senders.values()), total_bytes, total_bytes / elapsed))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生产环境端侧业务发送器（真机协议）")
    parser.add_argument("--host", default="127.0.0.1", help="边缘网关地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8888, help="边缘网关 JSON 接入端口（默认 8888）")
    parser.add_argument("--device-id", default="3C15DB07", help="设备 ID，须在中转白名单内")
    parser.add_argument("--biz-type", default="sensor", help="业务类型（默认 sensor）")
    parser.add_argument("--link", default="wired",
                        help="接入链路标记：wifi|bluetooth|wired|all（all=三链路并发）")
    parser.add_argument("--count", type=int, default=0, help="定量发送条数（0=不限，按 duration）")
    parser.add_argument("--duration", type=float, default=0.0, help="持续发送秒数（0=按 count）")
    parser.add_argument("--interval", type=float, default=1.0, help="发送间隔秒（默认 1.0）")
    parser.add_argument("--value", default=None, help="固定业务读数（真实传感器单值）")
    parser.add_argument("--values-file", default=None, help="业务读数文件，逐行喂入（真实传感器流）")
    args = parser.parse_args()

    if args.link == "all":
        if args.duration <= 0:
            print("--link all 需要 --duration", file=sys.stderr)
            return 2
        return run_multi(args)
    if args.count <= 0 and args.duration <= 0:
        args.count = 1
    return run_single(args)


if __name__ == "__main__":
    sys.exit(main())
