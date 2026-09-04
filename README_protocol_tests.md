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
2.2.3 edge:   init_link_connect, edge_forward
      device: check_link_connect, ping_link_test, start_test,
               keep_transfer, multi_link_bandwidth   （端侧终端，默认 env 身份）
      cloud:  query_link_data

2.2.4 device: start_video_stream（含每20s火情上报）, start_sensor_data,
               start_env_data（含每2分钟关键传感器上报）, start_test (illegal device)
      edge:   multi_source_access, query_service_log, trust_access_add_whitelist,
               trust_access_calculate, edge-query
      cloud:  query_cloud_log, cloud-query, query_cloud_log, cloud-query

2.2.5 edge:   start_uplink_transfer, link-monitor, edge-query, policy-route,
               msg-encap, set_channel, start_transfer, limit_rate
      other:  link_block --stop / --recover   （另一台设备下发服务器控制指令）
      cloud:  cloud-mgr, query_link_data, cloud-query
```

The individual `cloud-query.sh`, `query_cloud_log.sh` and `cloud-mgr.sh`
commands are test-control scripts backed by the shared `.protocol-test`
records. They are separate from the long-running original cloud process.
`run_test_outline.sh` keeps them in a complete local regression flow and is not
the strict two-terminal operation path.

## 2.2.3 construction test

### 生产（真机）前序操作：静态 IP 与服务拉起

网络与地址规划（网段为现网实际；`192.168.4.x` 等具体主机地址为示例，
以现场规划为准）：

| 节点 | 网卡/网段 | 地址（示例） | 用途 |
|------|-----------|--------------|------|
| 云服务器 | 公网 | 47.99.47.169 | 中转 server_v8 + 核心网关 gateway_v1，生产常驻 |
| 边缘网关·接入口 | 有线网卡 | 192.168.4.1/24 | 监听 7777（媒体）/8888（JSON），端侧全部指向它 |
| 边缘网关·宝通口 | 宝通网卡 | 192.168.2.100/24 | 到宝通工控机 192.168.2.1:9100 |
| 边缘网关·上行 | 5G/公网 | 默认路由 | 到 47.99.47.169:11500/11417/11511/11510 |
| 端侧设备板（三终端共用一块板） | 有线 | 192.168.4.10/24 | 板上三个终端进程：视频流 182D48D7（媒体 VID0→边缘:7777）、传感器 3C15DB07、环境监测 990E261B（JSON→边缘:8888，2.2.3 端侧身份）——身份靠设备 ID 区分，不靠 IP |
| 宝通工控机 | 192.168.2.0/24 | 192.168.2.1 | 短波 9100；输出侧另有 192.168.0.233/24 |

生产代码里的地址都在配置默认值与启动参数里，`original/` 源码不用改：

- `original/edge_config.py`（边缘）：`DEFAULT_CLOUD_HOST=47.99.47.169`（11500）、
  `DEFAULT_BAOTONG_HOST=192.168.2.1`（9100）、监听 `0.0.0.0:7777/8888`、
  卫星串口 `/dev/ttyUSB0@115200`；
- `original/config.py`（核心）：`DEFAULT_SERVER_HOST=127.0.0.1`（与同机中转
  互联）、`DEFAULT_B_HOST=47.99.47.169:11410`（卫星入库）、宝通监听
  192.168.2.1:9100、输出侧 192.168.0.233/24、前端 HTTP 10000-10017；
- 覆盖走启动参数：`edge_node.sh` 的 `--cloud-host/--baotong-host/
  --satellite-port` 等（`gateway_merged.py` build_parser 全表）、核心网关的
  `--server-host/--b-host`。

静态 IP 在系统层配置（示例 nmcli，Ubuntu 用 netplan；两张业务网卡不配
网关，上行走原有默认路由）：

```bash
nmcli con mod <接入网卡> ipv4.method manual ipv4.addresses 192.168.4.1/24
nmcli con mod <宝通网卡> ipv4.method manual ipv4.addresses 192.168.2.100/24
nmcli con up <接入网卡>; nmcli con up <宝通网卡>
```

端侧设备板同理只配**一个** `192.168.4.10/24`（不配网关）；端侧只需开
**一个**终端（`run_device_terminal.sh`，单终端合并形态）：三路业务的
start 命令在同一块板上各开一个进程，各自独立 TCP 长连接指向边缘
`192.168.4.1`——与 WSL 演示的单终端形态一致。

服务拉起顺序（前序操作）：

0. 云端 47.99.47.169：中转与核心网关**生产常驻**——只确认监听，不重启、
   不下发 `/stop1` 等控制指令：
   `ss -lnt | grep -E ':(11500|11502|11410|11416|11417|11420)'`
1. 边缘机直启边缘网关（真机接了 400-GM12 卫星模组就不要
   `--disable-satellite`；`PROTOCOL_TEST_STATE_DIR` 必须与大纲脚本一致——
   接入门/转发门/过滤门的标记文件是网关按这个目录读的；演示边缘默认
   身份 `gateway_1`，在中转属组1）：
```bash
cd /path/to/final
export PROTOCOL_TEST_STATE_DIR="$PWD/.protocol-test"
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
./edge_node.sh --cloud-host 47.99.47.169 --baotong-host 192.168.2.1 \
  --whitelist-filter --whitelist-interval 30 --compact-log
```
   （`run_edge_terminal.sh` 是 WSL 演示包装：会话复位、显示层 scene 改名
   与着色、固定 `--disable-satellite`、默认本地起中转——生产直启用上面
   的 `edge_node.sh`。）
2. 端侧机起端侧设备终端（单终端合并三路业务；2.2.3 只需这一台端侧）：
```bash
DEVICE_GATEWAY_HOST=192.168.4.1 PROTOCOL_TEST_RELAY_HOST=47.99.47.169 \
  ./run_device_terminal.sh
```

**跨机注意**：门命令（`init_link_connect`/`multi_source_access`/
`edge_forward`/`trust_access_*`）的标记文件写在执行机本地，必须在
**边缘机**上执行才作用到真网关；端侧机只跑发送类命令。查询云端核心
HTTP 时带 `PROTOCOL_TEST_CLOUD_HTTP_HOST=47.99.47.169`。

### 2.2.3 真机步骤（逐条）

边缘终端先落前置（`run_2_2_3.sh` 的 precondition 顺序）：

```bash
./init_link_connect.sh --reset   # 会话复位：清台账并关三扇门
```

策略路由与报文封装**不做前置**（大纲里属 2.2.5 条目3/4）：转发在
edge_forwarder 之后常开，条目2 的常态选路表（告警/控制双路 5G+短波、
关键传感器经卫星）随之生效——真网关本就没有策略路由门（受理即转发，
payload 带 fire/windspeed 即推短波专用链路），本地模型行为与之一致；
策略路由启动只是条目3 的流程步骤，5G 断开的降级切换由链路在线状态驱动。

| 步骤 | 终端 | 命令 | 预期 |
|------|------|------|------|
| 1 初始化接入链路 | 边缘 | `./init_link_connect.sh` | 落 `multi_source_access.enabled`，边缘开始受理端侧数据（`--reset` 是关门复位，只在前置用） |
| 2 检查链路连接 | 端侧 | `./check_link_connect.sh` | 三条接入链路状态表 |
| 3 链路 ping 测试 | 端侧 | `./ping_link_test.sh --real` | 接入链路 ICMP 实测边缘 192.168.4.x；回传链路 TCP 实测 11500/19100/11410 |
| 4 起始测试 | 端侧 | `./start_test.sh` | 默认 990E261B/env 身份发出首包 |
| 5 持续传输 | 端侧 | `./keep_transfer.sh --duration 600` | 边缘受理、暂不上云（转发门未开，属预期） |
| 5 多链路并发带宽 | 端侧 | `./multi_link_bandwidth.sh --duration 5` | 三条接入链路并发吞吐表 |
| 6 打开边缘转发 | 边缘 | `./edge_forward.sh --start` | 落 `edge_forward.enabled`，边缘建立到云端转发 |
| 收尾 打通端到端 | 边缘 | `./start_test.sh --device-id 990E261B --biz-type env` | 转发开启后补发一包，本机台账记 sent |
| 收尾 链路数据查询 | 边缘 | `PROTOCOL_TEST_CLOUD_HTTP_HOST=47.99.47.169 ./query_link_data.sh` | live 核对：云端最新 msg_id 与最后发送一致 |

单终端回归 `./run_2_2_3.sh` 即同一顺序的本地版。

### WSL2 本地演示

```bash
cd final
./init_link_connect.sh --reset    # 前置：会话复位（关三扇门）
./init_link_connect.sh            # 步骤1：初始化接入链路（开始受理端侧数据）
./check_link_connect.sh           # 步骤2：检查链路连接
./ping_link_test.sh               # 步骤3：链路 ping 测试
./start_test.sh                   # 步骤4：起始测试
./keep_transfer.sh --duration 600 # 步骤5：持续传输
./multi_link_bandwidth.sh         # 步骤5：多链路并发带宽
./edge_forward.sh --start         # 步骤6：打开边缘转发
./query_link_data.sh              # 收尾：链路数据查询
```

Use a short duration while checking the workflow, for example
`./keep_transfer.sh --duration 10 --interval 0.5`.

## 2.2.4 integration test

大纲 2.2.4 的三路多源数据来自**同一个端侧终端**（单终端合并形态）：
`./run_device_terminal.sh` 无需参数，视频流 182D48D7（有线，含真实媒体口
7777，每20s附带火情上报）/ 传感器 3C15DB07（Wi-Fi，名单过滤生效后自动换
无关 ID）/ 环境监测 990E261B（Wi-Fi/蓝牙/有线轮换）三路业务在同一终端
依次输入 start 命令即可（各命令独立进程、独立 TCP 长连接，可同时运行；
状态写入与 msg_id 分配均有跨进程锁）。

三个 start 命令默认**后台持续发送**：终端只显示一行 `[LAUNCH]`（biz/
持续或条数或时长/pid/日志路径）即回提示符，progress 明细写 `.protocol-test/sender-<biz>-<时间>.log`，
直到 kill `[LAUNCH]` 行打印的 pid；`--count/--duration` 到限自行结束，
`--fg` 前台直跑（一键回归脚本内部即用 `--fg`）。从 `run_device_terminal.sh`
启动的后台发送随该终端退出自动结束（终端只 kill 本会话登记的 pid）。
`./multi_source_access.sh` 执行前，边缘网关（真网关与本地模型
一致）只累计接收统计，不受理端侧数据：JSON 口报文计 `gate_drop`，媒体口
VID0 帧记 `[MEDIA][RECV][GATE]`；该命令执行后才开始受理。可信接入：起步
**不过滤名单**（全部放行），`./trust_access_add_whitelist.sh` 从服务器拉取白名单
并打印后过滤生效，传感器业务发送端逐报文自动切换为无关设备 ID
（ILLEGAL-SENSOR），被边缘拒收（`whitelist_drop`）并记阻断日志。

火情由**视频流业务**上报：`start_video_stream` 持续发送期间每 20 秒附带一条
fire 业务报文（同一设备身份、有线接入；告警/控制即短波链路的节奏，约 20s
一条），载荷为 `"fire": "false"`（无火情）；
下列命令只切换火情标志，下一条上报即变为 `"fire": "true"`（有火情）：

```bash
./fire_alarm.sh --on     # 触发火情：后续火情上报载荷变为 true
./fire_alarm.sh --off    # 解除火情：恢复 false
./fire_alarm.sh          # 只查看当前火情状态
```

光照强度模拟：`./start_light_data.sh` 以独立光照设备 DEV-001
（`PROTOCOL_TEST_DEVICE_LIGHT` 可覆盖）持续发送光照强度读数（sensor 业务，
仅有线接入边缘网关、回传走 5G，后台发送，kill [LAUNCH] 行 pid 停止）。
`./start_sensor_data.sh` 默认（后台形态）会**一并拉起** DEV-001——中断/重启后
重敲传感器命令即可，无需单独补拉；`--fg` 前台回归形态仍只跑传感器本身，
统计不混入 DEV-001 的记录。DEV-001 已内置在本地中转的白名单
（whitelist.json）；如需追加其他设备，在云端或边缘终端执行
`./whitelist_add_device.sh <设备ID>`——GET→合并→POST 改写本地中转并持久化，
边缘网关按拉取周期（默认 30s）同步新名单；**远端生产中转只读，该命令一律拒绝**。

### 2.2.4 真机步骤（逐条）

前置沿用 2.2.3 的前序操作（同一拓扑、同一服务拉起顺序）。边缘终端先落
2.2.4 前置（与 2.2.3 不同：接入门保持关闭、转发门先开——云端一致性核对
需要）：

```bash
./init_link_connect.sh --reset   # 会话复位：清台账并关三扇门
./edge_forward.sh --start        # 2.2.4 前提：转发常开（此后受理的报文即上云）
```

跨机约束同 2.2.3：门命令的标记文件写在执行机本地，`multi_source_access`/
`trust_access_*` 必须在**边缘机**上执行；端侧板只跑发送类命令。设备板起
**一个**端侧终端（同一块板；`run_device_terminal.sh` 默认即真发
`PROTOCOL_TEST_LIVE=1`，视频流业务走真实媒体口 → 边缘 7777，每 20s
附带火情上报）：

```bash
DEVICE_GATEWAY_HOST=192.168.4.1 DEVICE_MEDIA_HOST=192.168.4.1 \
PROTOCOL_TEST_RELAY_HOST=47.99.47.169 ./run_device_terminal.sh
```

| 步骤 | 终端 | 命令 | 预期 |
|------|------|------|------|
| 1 多源业务接入 | 端侧 | 依次（或同时）执行 `./start_video_stream.sh` / `./start_sensor_data.sh` / `./start_env_data.sh`（同一终端，默认后台持续发送） | 后台持续发送（默认不限时长，kill `[LAUNCH]` 行的 pid 停止）、跨越开闸时点；开闸前到达的报文边缘只计 `gate_drop` 接收统计（不受理属预期） |
| 2 多源接入门 | 边缘 | `./multi_source_access.sh` | 落 `multi_source_access.enabled`，边缘开始受理并转发上云；真网关日志出现 `[MULTI-SOURCE]` 受理公告 |
| 3 服务日志查询 | 边缘 | `./query_service_log.sh` | 接收统计 + 业务传输日志表（本地台账）；真网关日志 tail 可见受理/转发行 |
| 4 云端日志查询 | 边缘 | `PROTOCOL_TEST_CLOUD_HTTP_HOST=47.99.47.169 ./query_link_data.sh` | live 核对：云端最新 msg_id 与发送一致（真实云节点只读面） |
| 5 可信接入 | 边缘 | `PROTOCOL_TEST_WHITELIST_URL=http://47.99.47.169:11502/whitelist ./trust_access_add_whitelist.sh 182D48D7 3C15DB07 ILLEGAL-SENSOR` | 只读拉取生产中转白名单并打印、逐个在册核对；执行后过滤生效 |
| 6 非法设备发送 | 端侧 | 传感器业务继续发送（过滤生效后自动换无关 ID）；`./start_test.sh --device-id UNKNOWN-001` | 边缘拒收并计 `whitelist_drop`；真网关日志 `[WHITELIST][BLOCK] reason=not_in_whitelist` |
| 7 可信接入统计 | 边缘 | `./trust_access_calculate.sh` | 受理/拒收统计表（本地台账）；真网关以日志 `whitelist_drop`/`gate_drop` 周期计数为准 |

单终端回归 `./run_2_2_4.sh` 即同一顺序的本地版；纯真机协议（无模型台账）
用生产包 `production/run_2_2_4_production.sh`（单机联调）或
`production/device/run_2_2_4.sh`（设备板全流程），见 `production/README.md`。

### WSL2 本地演示

```bash
./start_video_stream.sh           # 视频流业务（默认后台持续发送，一行 [LAUNCH] 即回提示符）
./start_sensor_data.sh            # 传感器业务（默认后台持续发送；过滤生效后自动变为非法设备）
./start_env_data.sh               # 环境监测业务（默认后台持续发送；随终端每 2 分钟一条关键传感器上报，经卫星同步转发）
./multi_source_access.sh          # 边缘网关终端：此后边缘才开始受理端侧数据
./query_service_log.sh            # 边缘网关终端
./query_cloud_log.sh --device-type video/sensor/env   # 云端终端（默认按上行链路分 5G/卫星/短波三张表）
./trust_access_add_whitelist.sh 182D48D7 3C15DB07 ILLEGAL-SENSOR   # 拉取并打印服务器白名单；执行后过滤生效
./start_test.sh --device-id UNKNOWN-001
./trust_access_calculate.sh
./edge-query.sh --route-log --biz-type video/sensor/critical-sensor/fire
./edge-query.sh --route-switch
./cloud-query.sh --msg-id-check
./cloud-query.sh --link-id-check
./query_cloud_log.sh --device-type video/sensor/env --verify  # 收尾核对：附云端-边缘一致性核对表
```

`./run_2_2_4.sh` 单终端回归版按同样语义执行：前置 `init_link_connect --reset`
后接入门为关闭状态、名单过滤未启用，三路 start 以 `SOURCE_DURATION`（默认
3 秒）限时长发送；trust 指令后再补一段传感器发送（此时已自动换无关 ID）
产生阻断证据。

## 2.2.5 function test

2.2.5 的 5G 屏蔽演示由**另一台设备**向服务器下发 POST 控制指令
（original/server_v8.py 控制 API 11507），服务器收到后自行完成下发：

```bash
./link_block.sh --stop      # 另一台设备：POST /stop1（等价给 5G 天线加屏蔽罩）
./link_block.sh --recover   # 另一台设备：POST /recover1（摘罩恢复）
```

`/stop1` 后服务器：断开到核心网关 gateway_v1 的心跳（下发 status=0、
edge_online=False，核心随即判定边缘离线）、向边缘网关发送 5G 断开信号
（链路状态 connected=false，边缘打印 [LINK-STATUS]），并丢弃该组
（演示边缘为 gateway_1/组1）全部上行业务报文——核心与边缘双双断开。
策略路由按大纲 2.2.5 条目2/3对齐：5G 正常时视频/图片/传感器数据经 5G
转发，告警/控制信息同时经 5G 与短波转发，部分关键传感器信息经卫星
接入模块同步转发；5G 低于阈值后短波改为传输关键传感器数据与告警/
控制信息，卫星链路传输内容不变（仅承载关键传感器），关键业务持续
传输不中断。短波行为与真网关一致
（gateway_merged.py 应答选择）：5G 正常时短波只应答 fire；5G 断开后
fire/windspeed **按次轮换**应答（一条短信只装一种；"关键传感器数据"
即风速数据）——本地模型在 route.jsonl 记 `shortwave_answer/
shortwave_next` 明细，`start_uplink_transfer` 表格下方提示轮换，真网关
终端打印 [BAOTONG-V2][OFFLINE-ROTATE]。fire 报文载荷跟随 `./fire_alarm.sh`
的火情标志（false/true，不再随机）。`link-monitor.sh` 保持纯监测：
加罩后在边缘终端执行即可看到 5G BELOW THRESHOLD 与**切换信息**
（短波继续告警/控制并增加关键传感器承载、卫星继续关键传感器、
5G 承载的视频流/图片/常规传感器暂停上行；恢复时显示回切信息）
（`--low/--normal` 仍可手动置位本地模型，不再下发任何服务器指令）。
远端生产中转（47.99.47.169）只读，link_block 一律不下发，仅模型生效。
5G/链路切换状态以颜色区分：**恢复/正常绿、中断/降级红**——查询表的
Mode/Decision 单元格（normal/degraded、AVAILABLE/BELOW THRESHOLD）由
模型层着色；边缘终端的 [LINK-STATUS] 行由 run_edge_terminal.sh 的显示
管道整行着色；云端终端周期心跳照旧静默，仅在状态翻转时合成一行
[HEARTBEAT] 中断（红）/恢复（绿）提示（cloud_node.py 显示层）。全部
着色都在显示层/模型层完成，**original/ 目录内容保持原样不动**
（NO_COLOR 或管道输出时自动关闭）。

```bash
./cloud-mgr.sh --start
./start_uplink_transfer.sh
./query_link_data.sh              # 三张按链路接收表：表窗跟随链路节奏（5G 最近 10 秒 / 短波 2 分钟 / 卫星 10 分钟）
./policy-route.sh --start   # 大纲 2.2.5 条目3：策略路由启动
./link_block.sh --stop
./link-monitor.sh
./start_uplink_transfer.sh
./query_link_data.sh
./edge-query.sh --route-switch
./link_block.sh --recover
./link-monitor.sh
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
