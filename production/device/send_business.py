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

大纲 2.2.4 多源业务接入的三终端（接入链路与现网一致）：
  视频流终端   --biz-type video --link wired    （有线）
  传感器终端   --biz-type sensor --link wifi    （Wi-Fi）
  环境监测终端 --biz-type env --link rotate     （Wi-Fi/蓝牙/有线逐条轮换）

单终端合并形态（默认，2.2.4 生产流程）：同一终端依次输入三条业务发送
指令，每条只打印一行 [LAUNCH] 业务发送启动日志即返回提示符，实际发送在
后台进行（[SEND]/[SUMMARY] 写 --bg-log 日志文件，默认
<state>/sender-<biz>-<时间>.log）。未给 --count/--duration 时持续发送，
直到 kill [LAUNCH] 行打印的 pid；--fg/--foreground 回前台直跑：
  python3 send_business.py --device-id 182D48D7 --biz-type video --link wired \
      --duration 12 --interval 1

火情上报由视频流终端承担（每10s一条，无火情 false / 有火情 true）：
  ./send_business.py --device-id 182D48D7 --biz-type fire --link wired \
      --interval 10 --duration 600
  --fire true 为有火情（默认 false 无火情）；报文载荷与现网一致，携带
  fire/scene 顶层字段（不再用 windspeed）。

风速来源：默认内置模拟（与现网 mock 传感器同分布，0.0~30.0 一位小数）；
真机接真实传感器时用 --value 指定单值，或 --values-file 逐行喂入实时读数。

审计：每条报文写入 <state>/sent.jsonl（msg_id 连续编号持久化在 counters.json），
供端到端核对与中转侧 STAT 计数对账。
"""

from __future__ import annotations

import argparse
import json
import os
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
                 link_id: str, source: ValueSource, fire: str = "false"):
        self.host = host
        self.port = port
        self.device_id = device_id
        self.biz_type = biz_type
        self.link_id = link_id
        self.source = source
        self.fire = fire
        self.sent = 0
        self.bytes_sent = 0
        self.last_msg_id = ""
        self.last_detail = ""

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
        }
        if self.biz_type in ("fire", "control-alarm"):
            # 火情布尔载荷（与现网 mock_fire_sensor / 边缘短波分支一致）：
            # --fire true 为有火情，默认 false 无火情。
            message["fire"] = self.fire
            message["scene"] = str(random.randint(1, 5))
        elif self.biz_type == "env":
            # 环境监测读数（与现网 mock_env_monitor 同构，大纲 2.2.4 环境终端）。
            message["temperature"] = round(random.uniform(23.5, 29.5), 1)
            message["humidity"] = round(random.uniform(45.0, 70.0), 1)
            message["pm25"] = random.randint(18, 65)
            message["co2"] = random.randint(450, 820)
            message["noise"] = round(random.uniform(41.0, 55.5), 1)
        elif self.biz_type == "video":
            # 视频流业务元数据（真实媒体帧走 7777 VID0，JSON 口上报流描述）。
            message["codec"] = "H.264"
            message["resolution"] = "1920x1080"
            message["fps"] = 25
            message["frames"] = random.randint(240, 260)
        else:
            message["windspeed"] = self.source.windspeed()
        message["data_source"] = "device-sender"
        # [SEND] 明细取本条报文实际载荷字段（只展示真实读数口径）。
        if self.biz_type in ("fire", "control-alarm"):
            self.last_detail = "fire=%s scene=%s" % (message["fire"], message["scene"])
        elif self.biz_type == "env":
            self.last_detail = "temp=%sC humidity=%s%% pm25=%s" % (
                message["temperature"], message["humidity"], message["pm25"])
        elif self.biz_type == "video":
            self.last_detail = "codec=%s %s@%sfps frames=%s" % (
                message["codec"], message["resolution"], message["fps"], message["frames"])
        else:
            self.last_detail = "windspeed=%s" % message["windspeed"]
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
    sender = Sender(args.host, args.port, args.device_id, args.biz_type, args.link, source,
                    fire=args.fire)
    sock = None
    deadline = time.monotonic() + args.duration if args.duration else None
    failures = 0
    rotate = args.link == "rotate"
    try:
        while True:
            if args.count and sender.sent >= args.count:
                break
            if deadline and time.monotonic() >= deadline:
                break
            if rotate:
                # 环境监测终端：每条报文轮换一条接入链路（wifi/蓝牙/有线），
                # 与现网 env 终端的轮流分担语义一致。
                sender.link_id = LINKS[sender.sent % len(LINKS)]
            sock, ok, size = sender.send_one(sock)
            if not ok:
                failures += 1
                print("[SEND][FAIL] link=%s 连接边缘网关失败 (%s:%s)" % (args.link, args.host, args.port),
                      file=sys.stderr)
                if failures >= 3:
                    break
                time.sleep(1.0)
                continue
            detail = sender.last_detail
            print("[SEND] link=%s msg_id=%s bytes=%d %s" %
                  (args.link, sender.last_msg_id, size, detail))
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
    senders = {link: Sender(args.host, args.port, args.device_id, args.biz_type, link, source,
                             fire=args.fire)
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


def _launch_line(args, log_path: str, pid: int | None = None) -> str:
    if args.count:
        amount = "count=%d" % args.count
    elif args.duration:
        amount = "duration=%gs" % args.duration
    else:
        amount = "持续"
    pid_part = " pid=%d" % pid if pid is not None else ""
    return ("[LAUNCH] 业务发送启动：biz=%s device=%s link=%s %s interval=%gs%s 日志=%s"
            % (args.biz_type, args.device_id, args.link, amount, args.interval,
               pid_part, log_path))


def _enter_background(args) -> int | None:
    """单终端合并形态：打印一行业务发送启动日志后转入后台。

    父进程输出 [LAUNCH]（含后台进程 pid 与日志路径）后立即退出，终端
    马上可以输入下一条指令；子进程脱离会话，把 [SEND]/[SUMMARY] 全部
    写入 --bg-log 日志文件，按 --duration/--count 自行结束（未限时则
    持续发送，直到 kill [LAUNCH] 行打印的 pid）。
    返回 None 表示当前是子进程，继续正常发送流程。
    """
    if not hasattr(os, "fork"):
        print("[LAUNCH][ERROR] --background 仅支持 Linux（真机/WSL 部署）", file=sys.stderr)
        return 2
    if args.bg_log:
        log_path = args.bg_log
    else:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = str(STATE_DIR / ("sender-%s-%s.log" % (
            args.biz_type, datetime.now(BJ).strftime("%Y%m%d-%H%M%S"))))
    log_file = Path(log_path)
    if str(log_file.parent) not in ("", "."):
        log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = os.fork()
    except OSError as exc:
        print("[LAUNCH][ERROR] fork 失败：%s" % exc, file=sys.stderr)
        return 2
    if pid > 0:
        print(_launch_line(args, log_path, pid), flush=True)
        return 0
    # 子进程：脱离终端、输出重定向到日志文件，回到正常发送流程。
    os.setsid()
    sys.stdout.flush()
    sys.stderr.flush()
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
    os.dup2(log_fh.fileno(), 1)
    os.dup2(log_fh.fileno(), 2)
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:  # py<3.7 无 reconfigure，行缓冲兜底
        pass
    print(_launch_line(args, log_path, os.getpid()))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="生产环境端侧业务发送器（真机协议）")
    parser.add_argument("--host", default="127.0.0.1", help="边缘网关地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8888, help="边缘网关 JSON 接入端口（默认 8888）")
    parser.add_argument("--device-id", default="3C15DB07", help="设备 ID，须在中转白名单内")
    parser.add_argument("--biz-type", default="sensor", help="业务类型（默认 sensor）")
    parser.add_argument("--link", default="wired",
                        help="接入链路标记：wifi|bluetooth|wired|all|rotate"
                             "（all=三链路并发，rotate=逐条轮换）")
    parser.add_argument("--count", type=int, default=0, help="定量发送条数（与 --duration 都不给则持续发送）")
    parser.add_argument("--duration", type=float, default=0.0, help="持续发送秒数（与 --count 都不给则持续发送）")
    parser.add_argument("--interval", type=float, default=1.0, help="发送间隔秒（默认 1.0）")
    parser.add_argument("--value", default=None, help="固定业务读数（真实传感器单值）")
    parser.add_argument("--values-file", default=None, help="业务读数文件，逐行喂入（真实传感器流）")
    parser.add_argument("--fire", choices=("true", "false"), default="false",
                        help="火情布尔载荷（fire 业务；默认 false 无火情，true=有火情）")
    parser.add_argument("--background", action="store_true",
                        help="后台发送（默认即后台；保留以兼容显式写法）：打印"
                             "业务发送启动日志后转后台，明细/汇总写 --bg-log")
    parser.add_argument("--fg", "--foreground", dest="background", action="store_false",
                        help="前台直跑：[SEND]/[SUMMARY] 直接打印到终端")
    parser.set_defaults(background=True)
    parser.add_argument("--bg-log", default=None, metavar="PATH",
                        help="后台模式日志文件（默认 <state>/sender-<biz>-<时间>.log）")
    args = parser.parse_args()

    if args.link == "all":
        if args.duration <= 0:
            print("--link all 需要 --duration", file=sys.stderr)
            return 2
    if args.background:
        parent_rc = _enter_background(args)
        if parent_rc is not None:
            return parent_rc
    if args.link == "all":
        return run_multi(args)
    return run_single(args)


if __name__ == "__main__":
    sys.exit(main())
