# 生产环境流程包（2.2.3 / 2.2.4）

大纲 2.2.3（多模态接入与端到端传输）与 2.2.4（多源业务接入与可信接入）的
生产环境版本。协议逐字节对齐
`final/original/` 的现网实现（边缘网关 gateway_merged.py、中转 server_v8.py），
不依赖演示 harness（protocol_test_runtime / 三终端 / mock 面板）。

## 拓扑与硬约束

```
端侧设备 --TCP 8888(JSON)/7777(媒体)--> 边缘网关 --11500--> 生产中转 47.99.47.169 --> 核心网关/云端
```

- **远端中转是生产环境，只读**：不改动远端代码与白名单，只借用白名单内设备 ID
  （182D48D7 / 3C15DB07 / 990E261B / EA1D2801，以 `11502/whitelist` 实时返回为准）。
- **禁止本地云终端直连远端业务通道**（11401 / 11406-11410 / 11416 / 11420 / 11421），
  会与生产云节点争抢通道数据。
- 远端对外只读面仅 `11502/whitelist`（白名单）与 `11501`（云端设备状态）；
  云节点 HTTP（10008+）不对外。端到端接收核对在中转服务器上执行：
  `ssh` 后查看 server_v8 日志的 **STAT 计数**（只看计数，不看 payload）。

## 目录 = 三节点部署单元

| 目录 | 部署到 | 内容 |
|---|---|---|
| `edge/` | 边缘网关真机 | `gateway_merged.py`（解包后的现网源码）、`run_gateway.sh`、`init_link_connect.sh`、`edge_forward.sh`；2.2.4 三步：`multi_source_access.sh`、`trust_access_add_whitelist.sh`、`trust_access_calculate.sh` |
| `device/` | 端侧设备真机 | `send_business.py`（真机协议发送器）、`check_link.sh`、`ping_link.sh`、`run_2_2_3.sh`、`run_2_2_4.sh` |
| `cloud/` | 任意位置 | `query_relay_state.sh`（生产只读验证） |
| 根目录 | 联调机 | `run_2_2_3_production.sh`、`run_2_2_4_production.sh`（单机联调，端口与演示环境错开） |

## 边缘网关（真机）

```bash
cd edge
./run_gateway.sh                    # 前台运行，参数全部用 edge_config 生产默认值
```

默认即生产：中转 `47.99.47.169:11500`、白名单 `http://<cloud-host>:11502`（30s 周期）、
宝通 `192.168.2.1:9100`、卫星 `/dev/ttyUSB0@115200`、监听 `0.0.0.0:8888/7777`。
台架联调（无串口/无宝通网络）用环境变量覆盖：

```bash
EDGE_DISABLE_SATELLITE=1 EDGE_BAOTONG_HOST=127.0.0.1 EDGE_BAOTONG_PORT=19118 ./run_gateway.sh
```

**接入门（大纲步骤 1 语义，与开发侧一致）**：网关启动后默认**不受理**端侧
业务数据——JSON 报文只计接收统计（`gate_drop`）、媒体帧记 `[MEDIA][RECV][GATE]`。
在边缘网关上执行 `./init_link_connect.sh` 打开多源接入门后开始受理；
`--reset` 关闭接入/转发/过滤三扇门（会话复位），`--status` 查询。
标记文件在 `edge/.state/multi_source_access.enabled`。名单过滤门默认关闭
（全部放行），2.2.4 的 trust_access_add_whitelist 才启用（见下）。单独跑
2.2.4 时用 `./multi_source_access.sh` 打开接入门——与 init_link_connect 是
同一扇门，脚本名对齐大纲 2.2.4 步骤名。

**转发门（大纲步骤 6 语义，真机同样适用）**：接入门打开后网关仍默认不向
云端转发，在边缘网关上执行 `./edge_forward.sh --start` 建立 5G/短波/卫星
转发通道，`--stop` 断开，`--status` 查询。标记文件在
`edge/.state/edge_forward.enabled`。
注意：转发关闭期间业务报文在网关发送队列中排队（上限 1000 条，超出丢弃），
长时间大流量前先开转发。策略路由/报文封装不参与 2.2.3：转发一经建立即
持续上云（默认 5G 上行），策略路由属大纲 2.2.5 条目3，真网关无此门。

## 端侧设备（真机）

```bash
cd device
export EDGE_HOST=<边缘网关IP>        # 默认 127.0.0.1
./run_2_2_3.sh                       # 大纲 2.2.3 全流程（设备侧）
```

可选 `EDGE_SSH=user@edge-host` + `EDGE_REMOTE_DIR=<边缘上的edge目录>`：
日志查询与转发激活两步自动经 ssh 到边缘网关执行，否则打印人工指令。

单发 / 定制：

```bash
python3 send_business.py --count 5                       # 定量
python3 send_business.py --duration 600 --interval 1     # 持续（有线）
python3 send_business.py --link all --duration 5         # 三模态并发吞吐
python3 send_business.py --value 17.3 --count 1          # 真实传感器单值
python3 send_business.py --values-file ws.txt --duration 60   # 逐行喂入实时读数
```

火情上报由**视频流终端**承担（每10s一条，无火情 false / 有火情 true）：

```bash
python3 send_business.py --device-id 182D48D7 --biz-type fire --link wired \
    --interval 10 --duration 600          # 视频流终端火情上报（默认 false）
python3 send_business.py --device-id 182D48D7 --biz-type fire --link wired \
    --count 1 --fire true                 # 有火情单条（--fire 切换布尔载荷）
```

发送审计：`device/.state/sent.jsonl`（每条报文全量），`counters.json` 保证
msg_id/event_id 跨运行连续，可与中转 STAT 计数对账。

## 大纲 2.2.4 流程（多源业务接入与可信接入）

门语义与开发侧一致：**接入门**打开前，端侧报文只计接收统计（`gate_drop`）
不受理、不转发、不追溯改判；**trust_access_add_whitelist** 执行前不过滤
名单（全部放行）。真机三节点在设备侧跑全流程：

```bash
cd device
export EDGE_HOST=<边缘网关IP>          # 默认 127.0.0.1
./run_2_2_4.sh                         # 大纲 2.2.4 全流程（设备侧）
```

流程（`run_2_2_4.sh` 与单机版 `run_2_2_4_production.sh` 同构）：

1. **前提**：会话复位（`init_link_connect.sh --reset`，三扇门全关）后单独
   打开转发通道（2.2.4 需云端一致性核对）；接入门保持关闭。
2. **开闸前三终端并发发送**：视频流（`--biz-type video --link wired`，182D48D7）/
   传感器（`--biz-type sensor --link wifi`，3C15DB07）/ 环境监测
   （`--biz-type env --link rotate`，990E261B，Wi-Fi/蓝牙/有线逐条轮换）——
   本轮报文只计 `gate_drop`。
3. **`edge/multi_source_access.sh`**：打开多源接入门，边缘开始受理端侧数据。
4. **开闸后三终端再发 + 火情上报**：视频流终端随流每 10s 一条 fire 报文
   （同一设备身份、有线链路，默认无火情 false），本轮起受理并转发上云。
5. **服务日志与只读核对**：tail 网关日志（query_service_log）+
   `cloud/query_relay_state.sh` + `device/.state/sent.jsonl` 对账。
6. **`edge/trust_access_add_whitelist.sh <设备ID...>`**：从中转 `11502` 只读
   拉取白名单并打印（指定 ID 逐个在册校验，不在册给 WARN），随后名单过滤
   生效；网关经标记文件感知，无需重启。
7. **名单外设备发送**：`ILLEGAL-SENSOR` / `UNKNOWN-001`——边缘拒收，网关
   日志 `[WHITELIST][BLOCK] ... reason=not_in_whitelist`，计入 `whitelist_drop`。
8. **`edge/trust_access_calculate.sh`**：从网关日志统计门状态公告、拒收明细
   与 `whitelist_drop`/`gate_drop` 周期计数（只看计数与设备 ID，不看业务内容）。

### 2.2.4 直发模式（短波/卫星联调）

现网短波是主站轮询架构（发送由核心网关呼叫触发）、卫星走 400-GM12 串口；
联调环境没有电台呼叫与卫星串口，2.2.4 以**直发模式**运行——边缘网关把
短波/卫星报文复用统一上行通道直发核心网关，并按信道时延节奏发送：

```bash
cd edge
EDGE_RADIO_OVER_5G=1 ./run_gateway.sh      # 2.2.4 联调直发模式启动
# 时延可调：EDGE_SW_DELAY_S=20（短波）/ EDGE_SAT_DELAY_S=120（卫星）
```

- **短波**：`fire`/`windspeed` 报文受理后，约 `EDGE_SW_DELAY_S`（默认 20s）
  发一条最新业务短信（同一时刻仅一条在信道上，新数据随下一轮带出）；
  控制台打印与现网一致的 `[BAOTONG-V2][SEND] peer=<电台地址>`，
  计数进周期统计行 `shortwave=`。
- **卫星**：无串口依赖，网关周期入队身份报文（`[SATELLITE][SEND][QUEUED]`），
  经 `EDGE_SAT_DELAY_S`（默认 120s）星上时延后送达云端卫星接收页面
  （核心侧卫星页面可见；报文 timestamp 与落地时刻之差即星上时延）。
- 默认**关闭**：不设置 `EDGE_RADIO_OVER_5G` 时短波仍为主站轮询、卫星仍走
  串口（2.2.3 流程不受影响）。单机联调 `run_2_2_4_production.sh` 自动启用
  （卫星周期 `EDGE_SATELLITE_INTERVAL` 默认 150s），并在末尾等待首帧卫星
  落地后再收尾（`SAT_LAND_WAIT=0` 跳过等待）。

## 端到端验证（生产只读）

```bash
bash cloud/query_relay_state.sh        # RELAY_HOST 默认 47.99.47.169
```

输出：白名单实时内容与借用设备在册校验、云端设备状态（11501）、
以及中转服务器上的 STAT 对账指令。中转 `10008+` 不对外，
live 查询表（演示版 query_link_data 的下半张表）在生产包中没有对应物，
以中转日志 STAT 计数为准。

## 单机联调（不部署真机时）

```bash
./run_2_2_3_production.sh     # 大纲 2.2.3 全流程
./run_2_2_4_production.sh     # 大纲 2.2.4 全流程
```

想逐条命令手动执行（每步可见、可在转发激活前停下纯演练），见
[STEP_BY_STEP.md](STEP_BY_STEP.md)。

同机拉起生产网关（卫星关闭、宝通指本机）+ 全流程；端口 18888/17777/19118
与演示三终端完全错开，占用即退出。**转发通道一经激活，受理后的真实业务流
将发往生产中转**（借用白名单设备 ID）——`RELAY_HOST` 可指向测试中转。
2.2.3 的 `KEEP_DURATION` 默认 60（大纲值 600，联调可调）；2.2.4 的轮次时长
`SOURCE_DURATION`（开闸前，默认 5）/ `POST_DURATION`（开闸后，默认 12，
火情两条可见 10s 节奏）/ `ILLEGAL_COUNT`（默认 5）同理可调；2.2.4 直发
模式的时延 `EDGE_SW_DELAY_S`（短波，默认 20）/ `EDGE_SAT_DELAY_S`
（卫星，默认 120）与末尾落地等待 `SAT_LAND_WAIT`（默认 1，0 跳过）亦可覆盖。

## 与演示版（final/ 根目录）的差异

| 演示版 | 生产包 |
|---|---|
| 三终端 + 本地中转 + 本地云节点 | 真实三节点 + 生产中转，云端不在本地拉起 |
| protocol_test_runtime（模型记账 + live 混合） | 纯真机协议，无模型状态 |
| 模拟时延/丢包档案（ping 表） | 实测 TCP 建连 RTT |
| query_link_data live 通道表（本地云节点 HTTP） | 中转只读面 + 服务器 STAT 对账 |
| mock 风速 | 内置模拟 / `--value` / `--values-file` 接真实传感器 |
| 转发门 marker 在 final/.protocol-test | marker 在 production/edge/.state |
