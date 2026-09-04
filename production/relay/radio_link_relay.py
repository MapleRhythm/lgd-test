#!/usr/bin/env python3
"""短波/卫星专用转发链路（radio relay，与 server_v8 同机部署）。

背景：server_v8 现网的业务下发口（11400-11409）是长连接消费模型，
消费端过 5G NAT 闲置即被静默断连，死连接留在共享队列里抢报文
（见 DEPLOY_3BOARDS.md 坑17）。本转发器对远端只做"加法"：新开两个
HTTP 口（默认 11450→11550），短波/卫星帧经入口 POST /push 入库，
云端管理节点从出口 GET /records 短连接拉取（seq 游标、可回放历史）
——一来一回即断，不存在可僵尸化的长连接；server_v8 本体与既有
114xx/115xx 端口不受影响。

自包含单文件：默认值全部内置，服务器上无需任何 sh 启动脚本，直接
    nohup python3 -u radio_link_relay.py >/dev/null 2>&1 &
即可（如需改端口/状态目录再带 --ingress-port 等参数）。

链路时延不在本转发器：短波的信道时延/占道语义（默认 20±3s、同一
时刻仅一条在信道上、新数据顶掉旧的）与卫星的"立即落地 + 约 2 分钟
一条"节奏都在边缘网关（gateway_merged.py 联调模式，EDGE_SW_DELAY_S
/ EDGE_SAT_DELAY_S）实现，与既有口径一致。本转发器只忠实转发并
记录 sent_at/received_at（时延列含两端时钟偏差，仅供参考）。

记录落地为追加式 jsonl（默认 <脚本目录>/radio-relay-state/
radio-relay.jsonl），重启不丢历史、seq 连续。
"""

import argparse
import datetime
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# 单条 /push 报文上限（字节）：短波短信与卫星身份帧都远小于此。
MAX_BODY_BYTES = 256 * 1024
# /records 单次返回上限。
MAX_LIMIT = 500
DEFAULT_LIMIT = 50

# 双端口拓扑：入口收边缘推送，出口供云端拉取（两端口都不与 server_v8
# 既有 11400-11421 / 11500-11511 冲突）。
DEFAULT_INGRESS_PORT = 11450
DEFAULT_EGRESS_PORT = 11550


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(
        timespec="milliseconds"
    )


class RecordStore(object):
    """seq 连续递增的追加式记录库（内存索引 + jsonl 落盘）。"""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "radio-relay.jsonl")
        self._lock = threading.Lock()
        self._records = []
        self._seq = 0
        os.makedirs(state_dir, exist_ok=True)
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        loaded = 0
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    print("[RADIO-RELAY][WARN] skip bad state line: {}".format(
                        line[:80]
                    ))
                    continue
                self._records.append(record)
                self._seq = max(self._seq, int(record.get("seq", 0)))
                loaded += 1
        print("[RADIO-RELAY] state loaded: {} records, next_seq={}".format(
            loaded, self._seq + 1
        ))

    def append(self, link, payload, gateway, sent_at):
        received_at = now_iso()
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "link": link,
                "gateway": gateway,
                "sent_at": sent_at or received_at,
                "received_at": received_at,
                "payload": payload,
            }
            self._records.append(record)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    record, ensure_ascii=False, separators=(",", ":")
                ) + "\n")
        return record

    def query(self, link, after_seq, limit):
        with self._lock:
            snapshot = list(self._records)
        matched = []
        for record in snapshot:
            if record["seq"] <= after_seq:
                continue
            if link not in (None, "", "all") and record.get("link") != link:
                continue
            matched.append(record)
            if len(matched) >= limit:
                break
        return matched

    def statistics(self):
        with self._lock:
            total = len(self._records)
            per_link = {}
            for record in self._records:
                key = str(record.get("link", "?"))
                per_link[key] = per_link.get(key, 0) + 1
        return {"total": total, "per_link": per_link}


def make_handler(store, started_at, role):
    """role="ingress"（收边缘推送）或 "egress"（供云端拉取）。"""

    class RadioRelayHandler(BaseHTTPRequestHandler):
        server_version = "RadioRelay/1.0"

        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if role != "ingress":
                self._send_json(404, {
                    "status": "error",
                    "error": "POST /push is on the ingress port "
                             "(default {})".format(DEFAULT_INGRESS_PORT),
                })
                return
            if self.path.split("?", 1)[0] != "/push":
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(
                    400, {"status": "error", "error": "invalid Content-Length"}
                )
                return
            raw = self.rfile.read(length)
            try:
                envelope = json.loads(raw.decode("utf-8"))
                if not isinstance(envelope, dict):
                    raise ValueError("JSON root must be an object")
                link = str(envelope.get("link", "")).strip().lower()
                if link not in ("shortwave", "satellite"):
                    raise ValueError(
                        "link must be shortwave or satellite"
                    )
                payload = envelope.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
            except (UnicodeDecodeError, ValueError) as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
                return

            record = store.append(
                link=link,
                payload=payload,
                gateway=envelope.get("gateway", ""),
                sent_at=str(envelope.get("sent_at", "")),
            )
            print("[RADIO-RELAY] stored seq={} link={} gateway={} bytes={}".format(
                record["seq"], link, record.get("gateway", ""), length
            ))
            self._send_json(200, {
                "status": "ok",
                "seq": record["seq"],
                "received_at": record["received_at"],
            })

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                result = {
                    "status": "ok",
                    "role": role,
                    "time": now_iso(),
                    "uptime_s": round(time.time() - started_at, 1),
                    "state_file": store.path,
                }
                result.update(store.statistics())
                self._send_json(200, result)
                return

            if path == "/records":
                if role != "egress":
                    self._send_json(404, {
                        "status": "error",
                        "error": "GET /records is on the egress port "
                                 "(default {})".format(DEFAULT_EGRESS_PORT),
                    })
                    return
                params = self._query_params()
                link = params.get("link", "all")
                try:
                    after = int(params.get("after", "0"))
                    limit = int(params.get("limit", str(DEFAULT_LIMIT)))
                except ValueError:
                    self._send_json(
                        400,
                        {"status": "error", "error": "after/limit must be int"},
                    )
                    return
                limit = max(1, min(limit, MAX_LIMIT))
                records = store.query(link, after, limit)
                next_after = records[-1]["seq"] if records else after
                self._send_json(200, {
                    "status": "ok",
                    "count": len(records),
                    "next_after": next_after,
                    "records": records,
                })
                return

            self._send_json(404, {"status": "error", "error": "not found"})

        def _query_params(self):
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = {}
            for chunk in query.split("&"):
                if not chunk or "=" not in chunk:
                    continue
                key, _, value = chunk.partition("=")
                params[key] = value
            return params

        def log_message(self, fmt, *args):
            # 访问日志走 /health 与存储行，避免逐请求刷屏。
            return

    return RadioRelayHandler


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(
        description="短波/卫星专用转发链路（入口 POST /push → 出口 GET /records）"
    )
    parser.add_argument(
        "--ingress-host", default="0.0.0.0",
        help="ingress listen host (edge pushes here)",
    )
    parser.add_argument(
        "--ingress-port", type=int, default=DEFAULT_INGRESS_PORT,
        help="ingress port, edge POST /push (default {})".format(
            DEFAULT_INGRESS_PORT
        ),
    )
    parser.add_argument(
        "--egress-host", default="0.0.0.0",
        help="egress listen host (cloud pulls here)",
    )
    parser.add_argument(
        "--egress-port", type=int, default=DEFAULT_EGRESS_PORT,
        help="egress port, cloud GET /records (default {})".format(
            DEFAULT_EGRESS_PORT
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "radio-relay-state"
        ),
        help="state directory for the append-only jsonl history",
    )
    args = parser.parse_args()

    store = RecordStore(args.state_dir)
    started_at = time.time()
    ingress = ThreadingHTTPServer(
        (args.ingress_host, args.ingress_port),
        make_handler(store, started_at, "ingress"),
    )
    egress = ThreadingHTTPServer(
        (args.egress_host, args.egress_port),
        make_handler(store, started_at, "egress"),
    )
    print("[RADIO-RELAY] 转发拓扑: 入口(边缘推送) {}:{} -> 出口(云端拉取) {}:{}".format(
        args.ingress_host, args.ingress_port,
        args.egress_host, args.egress_port,
    ))
    print("[RADIO-RELAY] 入口: POST /push + GET /health | "
          "出口: GET /records?link=&after=&limit= + GET /health")
    print("[RADIO-RELAY] state: {}".format(store.path))
    egress_thread = threading.Thread(
        target=egress.serve_forever, daemon=True, name="egress-server"
    )
    egress_thread.start()
    try:
        ingress.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        egress.shutdown()
        egress.server_close()
        ingress.server_close()
        print("[RADIO-RELAY] stopped")


if __name__ == "__main__":
    main()
