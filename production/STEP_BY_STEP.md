# 2.2.3 / 2.2.4 逐步执行手册（正式流程）

**正式执行方式**：按本手册逐条输入命令，每步可见，可在转发激活前任意
一步停下检查。一键脚本（`run_2_2_3*.sh` / `run_2_2_4*.sh`）仅联调自检用。
两个终端：**A = 边缘网关（前台运行，日志实时可见）**，**B = 端侧设备**。

> 纯演练到步骤 7 为止**没有任何字节发往中转**（转发门未开，报文在网关队列排队，
> 上限 1000 条）。步骤 8 开门之后，真实业务流才发往生产中转 47.99.47.169。
> 另有两扇门与开发侧语义一致：**接入门**（网关启动后默认不受理端侧数据，
> 步骤 1 `init_link_connect.sh` 打开后才受理，此前报文只计接收统计 gate_drop）
> 与**名单过滤门**（默认不过滤、全部放行；2.2.4 的 trust_access_add_whitelist
> 才会启用，见下半部分 2.2.4 手册）。

## 准备

- 在 WSL 中执行（Windows 侧无 python3）。
- 生产包按生产机 **Python 3.6** 编写（不用 3.7+ 语法与标准库特性；
  网关/发送器/内嵌脚本在 3.6+ 均可直接运行）。
- 演示三终端还在跑：端口必须错开，用下述 `18888/17777/19118` 覆盖。
  演示已停：去掉端口/宝通覆盖，直接用生产默认 `8888/7777`。
- 真机三节点部署：终端 A 在边缘网关真机上只需 `./run_gateway.sh`（全部
  生产默认值，不加台架覆盖）；终端 B 在端侧设备上 `export EDGE_HOST=<边缘网关IP>`，
  其余命令相同（去掉端口覆盖）。

## 终端 A —— 启动边缘网关（保持运行）

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final/production/edge

./edge_forward.sh --stop        # 先清掉残留的转发标记

# 台架/联调参数：端口与演示错开、卫星上行关闭（2.2.3 不演示卫星）、
# 宝通指本机、中转=生产（短波默认走 5G 统一上行）
EDGE_JSON_PORT=18888 EDGE_MEDIA_PORT=17777 \
EDGE_BAOTONG_HOST=127.0.0.1 EDGE_BAOTONG_PORT=19118 \
EDGE_CLOUD_HOST=47.99.47.169 EDGE_DISABLE_SATELLITE=1 \
./run_gateway.sh
```

- 等白名单拉取成功的日志（类似 `cached_devices=4`）出现后再做后续步骤。
- 前台运行时屏幕即日志；想留档可改为
  `./run_gateway.sh 2>&1 | tee gateway.log`（Ctrl-C 停止）。

## 终端 B —— 按大纲逐步执行

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final/production/device
export EDGE_HOST=127.0.0.1 EDGE_JSON_PORT=18888 EDGE_MEDIA_PORT=17777
```

### 步骤 1 初始化接入链路（init_link_connect，在边缘网关执行）

```bash
../edge/init_link_connect.sh          # 打开多源接入门：边缘开始受理端侧数据
../edge/init_link_connect.sh --status # 可选：确认门状态
```

单机演练在终端 B 执行即可；真机三节点部署时换到终端 A（边缘网关）执行——
标记文件落在边缘机本地，门命令必须在边缘机上跑才作用到网关。

### 步骤 2 接入链路连通性检查（check_link_connect）

```bash
./check_link.sh
```

预期：`JSON接入` 与 `媒体接入` 两行均"连通"。

### 步骤 3 接入链路时延实测（ping_link_test）

```bash
./ping_link.sh            # 默认10次TCP建连；SAMPLES=20 可加样本
```

预期：`min/avg/max` 三项真实建连 RTT。

### 步骤 4 业务数据发送（start_test）

```bash
python3 send_business.py --count 5 --fg
```

预期：5 行 `[SEND] link=wired msg_id=...`，末尾 `[SUMMARY] 发送 5 条，失败 0 次`；
终端 A 出现接收/白名单拉取日志与 `[EDGE-FORWARD] 数据转发通道未建立`（排队不外发；
起步名单过滤未启用，全部放行）。若步骤 1 未执行，报文只计入 gate_drop 接收统计。

### 步骤 5 边缘网关服务日志查询（query_service_log）

直接看终端 A 屏幕（前台运行即日志）。后台/留档运行时：

```bash
tail -n 40 ../edge/gateway.log
```

### 步骤 6 持续传输（keep_transfer，仅有线）

```bash
python3 send_business.py --link wired --duration 600 --interval 1 --fg   # 演示可 --duration 60
```

### 步骤 7 多模态并发传输（multi_link_bandwidth）

```bash
python3 send_business.py --link all --duration 5 --fg
```

预期：wifi / bluetooth / wired 各自条数与 B/s，加合计一行。

### 步骤 8 建立边缘网关→云端转发通道（edge_forward）

> ⚠️ 此后真实流量发往生产中转 47.99.47.169（借用白名单设备 ID）。

```bash
../edge/edge_forward.sh --start
../edge/edge_forward.sh --status    # 可选：确认"已建立"
```

**积压提示**：开门一瞬间，步骤 4–7 积压的报文会随队列一并发往中转。
不希望积压上中转时，先在终端 A Ctrl-C 重启网关清空队列，再 `--start`。

### 步骤 9 转发通道建立后业务数据端到端发送（start_test）

```bash
python3 send_business.py --count 5 --fg
```

终端 A 此时是向 47.99.47.169:11500 的转发日志。

### 步骤 10 端到端链路数据查询（query_link_data，生产只读验证）

```bash
bash ../cloud/query_relay_state.sh
tail -n 3 .state/sent.jsonl      # 端侧发送审计，与中转 STAT 计数对账
```

输出：白名单实时内容与借用设备在册校验、云端设备状态（11501）、
短波/卫星接收表与云端解析判类与转发路径（2.2.5 条目1 生产只读版：
按业务字段判定 视频类/传感类/控制告警类 并对应处理入口）、
中转服务器上的 STAT 对账 ssh 指引（只看计数，不看 payload）。

## 收尾

```bash
../edge/edge_forward.sh --stop    # 终端 B：关转发门
# 终端 A：Ctrl-C 停止边缘网关
```

## 与一键脚本的关系

| 手册步骤 | `run_2_2_3_production.sh` 对应段 |
|---|---|
| 准备 + 终端 A | 0 环境自检 / 1 启动边缘网关 |
| 步骤 1–10 | 2–11 各节（步骤名相同） |
| 收尾 | EXIT trap 自动执行 |

---

# 2.2.4 逐步手动执行手册

`run_2_2_4_production.sh` 拆成单条命令。终端 A/B 同上；若已完成上面的
2.2.3 手册，终端 A 的边缘网关可以继续用（先做下面的会话复位）。

> 2.2.4 从"不受理"起步：**接入门先关**（开闸前发送的报文只计 gate_drop，
> 用于多源接入前后的受理对比），**转发门先开**（云端一致性核对需要）。
> 因此步骤 0 会做会话复位再单独开转发——此后受理到的真实业务流即发往
> 生产中转 47.99.47.169。名单过滤门到步骤 6 才启用，此前全部放行。

## 步骤 0 前提：会话复位 + 打开转发通道（在边缘网关执行）

> 2.2.4 的短波/卫星报文按信道时延直发核心网关（`EDGE_RADIO_OVER_5G` 默认 1，
> 若显式设过 0 请去掉重启；台架参数同终端 A 一节，见 production/README.md
> 「2.2.4 直发模式」）。真机三节点部署时网关启动命令即 `./run_gateway.sh`。

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final/production/edge

./init_link_connect.sh --reset      # 关接入/转发/过滤三扇门（2.2.4 从不受理起步）
./edge_forward.sh --start           # 单独打开转发通道（云端一致性核对需要）
```

单机演练在终端 B 执行即可；真机三节点部署时在终端 A（边缘网关）执行。

## 步骤 1 多源业务接入·开闸前（单终端依次启动三路业务）

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final/production/device
export EDGE_HOST=127.0.0.1 EDGE_JSON_PORT=18888

# 同一终端顺序输入三条业务发送指令（默认后台：一行启动日志即返回提示符）
python3 send_business.py --device-id 182D48D7 --biz-type video --link wired  --duration 5 --interval 1
python3 send_business.py --device-id 3C15DB07 --biz-type sensor --link wifi  --duration 5 --interval 1
python3 send_business.py --device-id 990E261B --biz-type env    --link rotate --duration 5 --interval 1
```

预期：每条指令只打印一行 `[LAUNCH] 业务发送启动...`（含后台 pid 与日志
路径）即回到提示符，三路业务在后台同时发送；本轮时长到后各后台进程自行
结束，`[SEND]` 明细与 `[SUMMARY] 发送 N 条，失败 0 次` 在各自日志文件
（默认 `.state/sender-*.log`）。终端 A 日志只见接收统计（`gate_drop`
累加），无 `[MULTI-SOURCE] 受理` 公告、无转发。

## 步骤 2 多源接入门打开（multi_source_access，在边缘网关执行）

```bash
../edge/multi_source_access.sh          # 打开接入门：边缘开始受理端侧数据
../edge/multi_source_access.sh --status # 可选：确认门状态
```

## 步骤 3 开闸后再发 + 视频流终端火情上报

```bash
python3 send_business.py --device-id 182D48D7 --biz-type video --link wired  --duration 12 --interval 1
python3 send_business.py --device-id 3C15DB07 --biz-type sensor --link wifi  --duration 12 --interval 1
python3 send_business.py --device-id 990E261B --biz-type env    --link rotate --duration 12 --interval 1

# 火情随视频流终端上报：同一设备身份、有线链路，每 10s 一条（默认 false）
python3 send_business.py --device-id 182D48D7 --biz-type fire --link wired --interval 10 --duration 12
```

预期：同步骤 1 的单终端形态（每条指令一行 `[LAUNCH]` 即返回）。终端 A
出现 `[MULTI-SOURCE] 多源业务接入已启动` 公告并开始转发上云；fire 报文约
10s 一条（`--duration 12` 可见两条）。直发模式下另见短波短信约 20s 一条
（`[BAOTONG-V2][SEND]`，时延 `EDGE_SW_DELAY_S`±`EDGE_SW_JITTER_S`，逐条
随机波动）。有火情演练加 `--fire true`。

## 步骤 4 边缘网关服务日志查询（query_service_log）

```bash
tail -n 40 ../edge/gateway.log       # 或直接看终端 A 屏幕
```

## 步骤 5 端到端链路数据查询（query_link_data，生产只读验证）

```bash
bash ../cloud/query_relay_state.sh
tail -n 5 .state/sent.jsonl          # 端侧发送审计，与中转 STAT 计数对账
```

## 步骤 6 可信接入·拉取服务器白名单并生效（在边缘网关执行）

```bash
../edge/trust_access_add_whitelist.sh 182D48D7 3C15DB07 990E261B
```

白名单只读拉取自中转 `11502`（与网关自身同一来源），打印在册设备并逐个
校验指定 ID（不在册给 WARN）；随后名单过滤生效，网关经标记文件感知、
无需重启。

## 步骤 7 名单外设备发送（非法终端被拒收）

```bash
python3 send_business.py --device-id ILLEGAL-SENSOR --count 5
python3 send_business.py --device-id UNKNOWN-001   --count 5
```

预期：发送本身成功（TCP 层），但边缘拒收——终端 A 日志出现
`[WHITELIST][BLOCK] device_id=... reason=not_in_whitelist`，计入 `whitelist_drop`。

## 步骤 8 可信接入统计（trust_access_calculate，在边缘网关执行）

```bash
EDGE_LOG=../edge/gateway.log ../edge/trust_access_calculate.sh
```

输出：门状态公告（最近4条）、拒收明细（最近5条）与拒收累计、
网关周期统计行的 `whitelist_drop`/`gate_drop` 计数。

## 收尾

```bash
../edge/init_link_connect.sh --reset   # 关三扇门（接入/转发/过滤）
# 终端 A：Ctrl-C 停止边缘网关
```

## 与一键脚本的关系（2.2.4）

| 手册步骤 | `run_2_2_4_production.sh` 对应段 |
|---|---|
| 步骤 0 | 1 启动边缘网关（内含会话复位）/ 2 打开转发通道 |
| 步骤 1 | 3 开闸前单终端依次启动三路业务 |
| 步骤 2 | 4 多源接入门打开 |
| 步骤 3 | 5 开闸后再发 + 火情上报 |
| 步骤 4 | 6 边缘网关服务日志 |
| 步骤 5 | 7 端到端链路数据查询 |
| 步骤 6 | 8 可信接入·白名单拉取并生效 |
| 步骤 7 | 9 名单外设备发送 |
| 步骤 8 | 10 可信接入统计 |
| 卫星上行回看（可选） | 11 卫星上行日志回看（帧立即落地，约 EDGE_SAT_DELAY_S±JITTER 一条、连续发送） |
| 收尾 | EXIT trap 自动执行 |
