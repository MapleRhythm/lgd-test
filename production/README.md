# 生产环境 2.2.3 流程包

大纲 2.2.3（多模态接入与端到端传输）的生产环境版本。协议逐字节对齐
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
| `edge/` | 边缘网关真机 | `gateway_merged.py`（解包后的现网源码）、`run_gateway.sh`、`init_link_connect.sh`、`edge_forward.sh` |
| `device/` | 端侧设备真机 | `send_business.py`（真机协议发送器）、`check_link.sh`、`ping_link.sh`、`run_2_2_3.sh` |
| `cloud/` | 任意位置 | `query_relay_state.sh`（生产只读验证） |
| 根目录 | 联调机 | `run_2_2_3_production.sh`（单机联调，端口与演示环境错开） |

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
（全部放行），2.2.4 的 trust_access_add_whitelist 才启用，本流程包不涉及。

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
./run_2_2_3_production.sh
```

想逐条命令手动执行（每步可见、可在步骤 8 前停下纯演练），见 [STEP_BY_STEP.md](STEP_BY_STEP.md)。

同机拉起生产网关（卫星关闭、宝通指本机）+ 全流程；端口 18888/17777/19118
与演示三终端完全错开，占用即退出。**步骤 8 转发激活后，真实业务流将发往
生产中转**（借用白名单设备 ID）——`RELAY_HOST` 可指向测试中转。
`KEEP_DURATION` 默认 60（大纲值 600，联调可调）。

## 与演示版（final/ 根目录）的差异

| 演示版 | 生产包 |
|---|---|
| 三终端 + 本地中转 + 本地云节点 | 真实三节点 + 生产中转，云端不在本地拉起 |
| protocol_test_runtime（模型记账 + live 混合） | 纯真机协议，无模型状态 |
| 模拟时延/丢包档案（ping 表） | 实测 TCP 建连 RTT |
| query_link_data live 通道表（本地云节点 HTTP） | 中转只读面 + 服务器 STAT 对账 |
| mock 风速 | 内置模拟 / `--value` / `--values-file` 接真实传感器 |
| 转发门 marker 在 final/.protocol-test | marker 在 production/edge/.state |
