# 2.2.3 逐步手动执行手册

`run_2_2_3_production.sh` 的一键流程拆成单条命令，逐条执行、每步可见。
两个终端：**A = 边缘网关（前台运行，日志实时可见）**，**B = 端侧设备**。

> 纯演练到步骤 7 为止**没有任何字节发往中转**（转发门未开，报文在网关队列排队，
> 上限 1000 条）。步骤 8 开门之后，真实业务流才发往生产中转 47.99.47.169。
> 另有两扇门与开发侧语义一致：**接入门**（网关启动后默认不受理端侧数据，
> 步骤 1 `init_link_connect.sh` 打开后才受理，此前报文只计接收统计 gate_drop）
> 与**名单过滤门**（默认不过滤、全部放行；2.2.4 的 trust_access_add_whitelist
> 才会启用，本流程包不涉及）。

## 准备

- 在 WSL 中执行（Windows 侧无 python3）。
- 演示三终端还在跑：端口必须错开，用下述 `18888/17777/19118` 覆盖。
  演示已停：去掉端口/宝通覆盖，直接用生产默认 `8888/7777`。
- 真机三节点部署：终端 A 在边缘网关真机上只需 `./run_gateway.sh`（全部
  生产默认值，不加台架覆盖）；终端 B 在端侧设备上 `export EDGE_HOST=<边缘网关IP>`，
  其余命令相同（去掉端口覆盖）。

## 终端 A —— 启动边缘网关（保持运行）

```bash
cd /mnt/c/Users/23369/Desktop/PythonSocketProject/final/production/edge

./edge_forward.sh --stop        # 先清掉残留的转发标记

# 台架/联调参数：端口与演示错开、卫星串口关闭、宝通指本机、中转=生产
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
python3 send_business.py --count 5
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
python3 send_business.py --link wired --duration 600 --interval 1   # 演示可 --duration 60
```

### 步骤 7 多模态并发传输（multi_link_bandwidth）

```bash
python3 send_business.py --link all --duration 5
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
python3 send_business.py --count 5
```

终端 A 此时是向 47.99.47.169:11500 的转发日志。

### 步骤 10 端到端链路数据查询（query_link_data，生产只读验证）

```bash
bash ../cloud/query_relay_state.sh
tail -n 3 .state/sent.jsonl      # 端侧发送审计，与中转 STAT 计数对账
```

输出：白名单实时内容与借用设备在册校验、云端设备状态（11501）、
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
