cat > mock_sensor.py << 'EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟传感器数据发送器
向边缘网关 (TCP 8888) 发送包含 fire 或 windspeed 的 JSON 数据。
支持两个独立客户端，分别模拟 gateway_1 和 gateway_2。
"""

import argparse
import json
import random
import socket
import threading
import time
from datetime import datetime, timezone, timedelta

# 默认配置
DEFAULT_TARGET_HOST = "127.0.0.1"   # 边缘网关 IP，修改为实际地址
DEFAULT_TARGET_PORT = 8888
DEFAULT_INTERVAL = 2.0  # 发送间隔（秒）
DEFAULT_DATA_TYPE = "fire"  # 或 "windspeed"
DEFAULT_GATEWAY = "gateway_1"

# 时区（北京时间）
BJ_TZ = timezone(timedelta(hours=8))


def now_str():
    """返回北京时间字符串"""
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


def generate_fire_payload(gateway_id, device_id="DEV_FIRE_01"):
    """生成 fire 数据 JSON"""
    return {
        "gateway": gateway_id,
        "device_id": device_id,
        "packet_type": "alarm",
        "timestamp": now_str(),
        "fire": random.choice(["true", "false"]),
        "scene": str(random.randint(1, 5)),
        "data_source": "mock_fire_sensor"
    }


def generate_windspeed_payload(gateway_id, device_id="DEV_WIND_01", fixed_value=None):
    """生成 windspeed 数据 JSON；指定 fixed_value 时发送固定风速值"""
    if fixed_value is not None:
        windspeed = f"{float(fixed_value):.1f}"
    else:
        windspeed = f"{random.uniform(0.0, 30.0):.1f}"
    return {
        "gateway": gateway_id,
        "device_id": device_id,
        "packet_type": "sensor",
        "timestamp": now_str(),
        "windspeed": windspeed,
        "data_source": "mock_wind_sensor"
    }


def send_loop(target_host, target_port, data_type, gateway_id, interval, device_id=None, fixed_windspeed=None):
    """
    持续发送数据的循环；fixed_windspeed 不为 None 时发送固定风速值
    """
    if data_type == "fire":
        payload_func = generate_fire_payload
        if not device_id:
            device_id = "DEV_FIRE_01"
    elif data_type == "windspeed":
        payload_func = generate_windspeed_payload
        if not device_id:
            device_id = "DEV_WIND_01"
    else:
        raise ValueError("data_type must be 'fire' or 'windspeed'")

    # 连接
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((target_host, target_port))
            print(f"[{gateway_id}] Connected to {target_host}:{target_port}")
            break
        except Exception as e:
            print(f"[{gateway_id}] Connection failed: {e}, retry in 5s...")
            time.sleep(5)

    count = 0
    try:
        while True:
            if data_type == "windspeed":
                payload = payload_func(gateway_id, device_id, fixed_value=fixed_windspeed)
            else:
                payload = payload_func(gateway_id, device_id)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            sock.sendall(line.encode("utf-8"))
            count += 1
            print(f"[{gateway_id}] Sent #{count}: {payload}")
            time.sleep(interval)
    except (BrokenPipeError, ConnectionResetError) as e:
        print(f"[{gateway_id}] Connection lost: {e}, reconnecting...")
        sock.close()
        # 递归重连（重新调用自身）
        send_loop(target_host, target_port, data_type, gateway_id, interval, device_id, fixed_windspeed)
    except KeyboardInterrupt:
        print(f"\n[{gateway_id}] Stopped by user.")
        sock.close()
    except Exception as e:
        print(f"[{gateway_id}] Error: {e}, reconnecting in 5s...")
        sock.close()
        time.sleep(5)
        send_loop(target_host, target_port, data_type, gateway_id, interval, device_id, fixed_windspeed)


def main():
    parser = argparse.ArgumentParser(description="模拟传感器数据发送器")
    parser.add_argument("--host", default=DEFAULT_TARGET_HOST,
                        help="边缘网关 IP 地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_TARGET_PORT,
                        help="边缘网关 JSON 端口，默认 8888")
    parser.add_argument("--type", choices=["fire", "windspeed"], default=DEFAULT_DATA_TYPE,
                        help="数据类型：fire 或 windspeed")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY,
                        help="网关标识，如 gateway_1, gateway_2")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="发送间隔（秒），默认 2")
    parser.add_argument("--device-id", default=None,
                        help="设备 ID，若不指定则自动生成")
    parser.add_argument("--windspeed-value", type=float, default=None,
                        help="发送固定风速值（如 8.6），不指定则随机 0.0-30.0")
    parser.add_argument("--dual", action="store_true",
                        help="同时启动两个客户端：一个 fire (gateway_1)，一个 windspeed (gateway_2)")
    args = parser.parse_args()

    if args.windspeed_value is not None and args.type != "windspeed":
        parser.error("--windspeed-value 只能搭配 --type windspeed 使用")

    if args.dual:
        # 启动两个线程
        t1 = threading.Thread(
            target=send_loop,
            args=(args.host, args.port, "fire", "gateway_1", args.interval, "DEV_FIRE_01"),
            daemon=True,
            name="FireSender"
        )
        t2 = threading.Thread(
            target=send_loop,
            args=(args.host, args.port, "windspeed", "gateway_2", args.interval, "DEV_WIND_01", args.windspeed_value),
            daemon=True,
            name="WindSender"
        )
        t1.start()
        t2.start()
        print("Both fire and windspeed senders started. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
    else:
        send_loop(args.host, args.port, args.type, args.gateway, args.interval, args.device_id, args.windspeed_value)


if __name__ == "__main__":
    main()
EOF
