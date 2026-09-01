# Protocol test scripts

These WSL2 scripts implement the command names in test outline items
2.2.3, 2.2.4 and 2.2.5.  They share `protocol_test_runtime.py`, which records
JSONL audit files under `.protocol-test/` and prints fixed-width console tables.

The default mode is a deterministic local test model.  It is useful when the
Wi-Fi, radio, satellite or cloud hardware is not attached to WSL2.  The test
records use the same message fields consumed by the final gateway code:
`device_id`, `biz_type`, `msg_id`, `link_id`, `timestamp`, `data_content`,
`gateway` and `edge_gateway`.

Each command below is an executable WSL2 script. The section runners preserve
the order in the test outline and print a separate precondition block before
the test steps:

```bash
./run_2_2_3.sh
./run_2_2_4.sh
./run_2_2_5.sh
```

To execute all three sections in one WSL2 terminal, use:

```bash
KEEP_DURATION=10 BANDWIDTH_DURATION=2 ./run_test_outline.sh
```

Omit those variables for the outline defaults. The default continuous-link
duration is 600 seconds, matching the requirement of at least 10 minutes.

## Two-terminal deployment

`run_test_outline.sh` is a local test-flow runner. It does not start the cloud
management node or the edge gateway. For the two-node WSL2 setup, use two
terminals:

Terminal 1, cloud management node and cloud-side commands:

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final
./run_cloud_terminal.sh
```

The wrapper starts the original cloud node in the background while keeping the
same terminal available for `cloud-query.sh`, `query_cloud_log.sh` and
`cloud-mgr.sh`. Terminal 2, edge gateway and edge-side commands:

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final
./run_edge_terminal.sh
```

The wrapper starts the original edge node in the background while keeping the
same terminal available for the edge-side test scripts. Each edge-terminal
start also opens a fresh demo session: the forward/access/filter gates are
reset and every jsonl ledger is cleared (same list as `init --reset`), so
per-run statistics such as `trust_access_calculate.sh` never mix runs. The default
`KEEP_DURATION=600` matches the document requirement. For a short manual check,
use `./keep_transfer.sh --duration 10 --interval 0.5` in the edge terminal.

Run the commands in the document order, switching terminals when the document
changes node ownership:

```text
2.2.3 edge:  init_link_connect, check_link_connect, ping_link_test,
             start_test, keep_transfer, multi_link_bandwidth, edge_forward
      cloud: query_link_data

2.2.4 edge:  start_video_stream, start_sensor_data, start_env_data,
             multi_source_access, query_service_log, trust_access_add_whitelist,
             start_test (illegal device), trust_access_calculate, edge-query
      cloud: query_cloud_log, cloud-query, query_cloud_log, cloud-query

2.2.5 edge:  start_uplink_transfer, link-monitor, edge-query, start_uplink_transfer,
             link-monitor, start_uplink_transfer, msg-encap, set_channel,
             start_transfer, limit_rate
      cloud: cloud-mgr, query_link_data, cloud-query
```

The individual `cloud-query.sh`, `query_cloud_log.sh` and `cloud-mgr.sh`
commands are test-control scripts backed by the shared `.protocol-test`
records. They are separate from the long-running original cloud process.
`run_test_outline.sh` keeps them in a complete local regression flow and is not
the strict two-terminal operation path.

## 2.2.3 construction test

```bash
cd final
./init_link_connect.sh --reset
./check_link_connect.sh
./ping_link_test.sh
./start_test.sh
./keep_transfer.sh --duration 600
./multi_link_bandwidth.sh
./edge_forward.sh --start
./query_link_data.sh
```

The policy route and message encapsulation programs are started as
preconditions by `run_2_2_3.sh`; they are not additional 2.2.3 test steps.

Use a short duration while checking the workflow, for example
`./keep_transfer.sh --duration 10 --interval 0.5`.

## 2.2.4 integration test

大纲 2.2.4 的三路多源数据来自三个端侧终端（同一目录可同时开三个 WSL 终端，
各自独立 TCP 长连接；状态写入与 msg_id 分配均有跨进程锁）：

```bash
./run_device_terminal.sh video    # 视频流终端 182D48D7（有线，含真实媒体口 7777）
./run_device_terminal.sh sensor   # 传感器终端 3C15DB07（Wi-Fi；名单过滤生效后自动换无关 ID）
./run_device_terminal.sh env      # 环境监测模块终端 990E261B（Wi-Fi/蓝牙/有线轮换）
```

三个 start 命令默认**持续发送**（每秒一条，`--count`/`--duration` 可覆盖，
Ctrl-C 停止）。`./multi_source_access.sh` 执行前，边缘网关（真网关与本地模型
一致）只累计接收统计，不受理端侧数据：JSON 口报文计 `gate_drop`，媒体口
VID0 帧记 `[MEDIA][RECV][GATE]`；该命令执行后才开始受理。可信接入：起步
**不过滤名单**（全部放行），`./trust_access_add_whitelist.sh` 从服务器拉取白名单
并打印后过滤生效，传感器终端发送端逐报文自动切换为无关设备 ID
（ILLEGAL-SENSOR），被边缘拒收（`whitelist_drop`）并记阻断日志。

```bash
./start_video_stream.sh           # 视频流终端（持续发送，Ctrl-C 停止）
./start_sensor_data.sh            # 传感器终端（持续发送；过滤生效后自动变为非法设备）
./start_env_data.sh               # 环境监测模块终端（持续发送）
./multi_source_access.sh          # 边缘网关终端：此后边缘才开始受理端侧数据
./query_service_log.sh            # 边缘网关终端
./query_cloud_log.sh --device-type video/sensor/env   # 云端终端（末尾输出云端与边缘记录一致性核对）
./trust_access_add_whitelist.sh 182D48D7 3C15DB07 ILLEGAL-SENSOR   # 拉取并打印服务器白名单；执行后过滤生效
./start_test.sh --device-id UNKNOWN-001
./trust_access_calculate.sh
./edge-query.sh --route-log --biz-type video/sensor/critical-sensor/fire
./edge-query.sh --route-switch
./cloud-query.sh --msg-id-check
./cloud-query.sh --link-id-check
```

`./run_2_2_4.sh` 单终端回归版按同样语义执行：前置 `init_link_connect --reset`
后接入门为关闭状态、名单过滤未启用，三路 start 以 `SOURCE_DURATION`（默认
3 秒）限时长发送；trust 指令后再补一段传感器发送（此时已自动换无关 ID）
产生阻断证据。

## 2.2.5 function test

```bash
./cloud-mgr.sh --start
./start_uplink_transfer.sh
./query_link_data.sh
./link-monitor.sh --low
./start_uplink_transfer.sh
./query_link_data.sh
./edge-query.sh --route-switch
./link-monitor.sh --normal
./start_uplink_transfer.sh
./query_link_data.sh
./msg-encap.sh --start
./set_channel.sh
./start_transfer.sh
./limit_rate.sh --rate 1
./cloud-query.sh --biz-type video/sensor/control-alarm
./cloud-query.sh --route-decision
./cloud-query.sh --link-switch
```

To send the JSON messages to a running final gateway listener, set
`PROTOCOL_TEST_LIVE=1`.  The default destination is `127.0.0.1:8888`; it can
be changed with `PROTOCOL_TEST_GATEWAY_HOST` and `PROTOCOL_TEST_GATEWAY_PORT`.
Set `PROTOCOL_TEST_LIVE_MEDIA=1` as well to exercise the final gateway's
binary media listener on `127.0.0.1:7777` for video source records.
