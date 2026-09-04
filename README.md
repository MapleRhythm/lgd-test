# lgd-test

陆工大测试大纲 v49 §2.2.3–2.2.5 的三终端演示（mock）与生产部署包。

- **mock 演示**：端侧设备 / 边缘网关 / 云端管理节点三终端，`protocol_test_runtime.py`
  承载大纲命令，业务流真实打到本地边缘网关（TCP 8888 JSON / 7777 媒体）→ 本地中转
  （`original/server_v8.py`，不在任何终端显示）→ 云端节点（`original/gateway_v1.py`）。
- `original/`：现网源码参考（中转 server_v8、边缘网关 gateway_merged、核心网关 gateway_v1）。
- `production/`：真机可部署的生产版 2.2.3 流程包。

## 快速开始（mock 演示）

```bash
./run_edge_terminal.sh              # 边缘网关终端（自动拉起本地中转）
./run_cloud_terminal.sh             # 云端管理节点终端
./run_device_terminal.sh            # 端侧设备终端（单终端合并三路业务，无需参数）
```

端侧三路业务（视频流 182D48D7/有线、传感器 3C15DB07/Wi-Fi、环境监测
990E261B/轮换）合并到同一个端侧终端：依次输入三个 start 命令即可，各命令是
独立进程、独立 TCP 长连接（可同时运行，状态写入与 msg_id 分配均有跨进程锁）。
start 命令默认**后台持续发送**——终端只显示一行 `[LAUNCH]`（biz/
持续或条数或时长/pid/日志路径）
即回提示符，明细写 `.protocol-test/sender-<biz>-<时间>.log`，直到 kill
`[LAUNCH]` 行打印的 pid；`--count/--duration` 到限自行结束，`--fg` 前台
直跑（输出进度到终端，一键回归脚本内部即用 `--fg`）。从
`run_device_terminal.sh` 启动的后台发送随该终端退出自动结束（终端只 kill
本会话登记的 pid）。
`./multi_source_access.sh` 执行前
边缘网关不受理端侧数据（接入门）。可信接入：起步不过滤名单（全部放行），
`./trust_access_add_whitelist.sh` 拉取并打印服务器白名单后过滤生效，传感器
业务自动换无关设备 ID 被拒收并记阻断日志。

每次重跑自动清台账：开启边缘终端即开启新演示会话（复位转发门/接入门/
过滤门并清空全部 jsonl 台账）；`run_2_2_3.sh` / `run_2_2_4.sh` / `run_2_2_5.sh`
前置同样各自 `init_link_connect.sh --reset`，统计数据不会跨轮次残留。

## 大纲 2.2.3 运行流程（mock 演示版）

三终端就绪后按步骤执行；2.2.3 的端侧业务默认即**环境监测身份**
（终端内 `start_test` / `keep_transfer` / `multi_link_bandwidth` 默认 990E261B / env）。
一键脚本：在端侧设备终端执行 `./run_2_2_3.sh`。

| 步骤 | 命令 | 执行终端 |
|---|---|---|
| 前置 | `./init_link_connect.sh --reset` → `./policy-route.sh --start` → `./msg-encap.sh --start` | 端侧 |
| 1 初始化端侧链路 | `./init_link_connect.sh` | 端侧 |
| 2 接入链路连通性检查 | `./check_link_connect.sh` | 端侧 |
| 3 接入链路时延实测 | `./ping_link_test.sh` | 端侧 |
| 4 业务数据发送 | `./start_test.sh` | 端侧 |
| 5 网关服务日志查询 | `./query_service_log.sh` | 边缘网关 |
| 6 建立转发通道 | `./edge_forward.sh --start`（**此后业务流真实发往云端**） | 边缘网关 |
| 7 持续传输（仅有线） | `./keep_transfer.sh --duration 600 --interval 1` | 端侧 |
| 8 多模态并发传输 | `./multi_link_bandwidth.sh --duration 5` | 端侧 |
| 9 转发建立后端到端发送 | `./start_test.sh` | 端侧 |
| 10 端到端链路数据查询 | `./query_link_data.sh`（5G 信道/短波工控设备/卫星接入模块三张接收表，表窗 10s/2min/10min + 端到端核对） | 云端 |

> 步骤 6 开门前，业务报文只在边缘接入与分类，不向云端转发（大纲语义）。
> 详见 [README_protocol_tests.md](README_protocol_tests.md)。

## 大纲 2.2.3 运行流程（生产版）

生产包在 `production/`，目录即三节点部署单元，协议与 `original/` 现网实现逐字节一致：

| 目录 | 部署到 | 关键动作 |
|---|---|---|
| `production/edge/` | 边缘网关真机 | `./run_gateway.sh`（默认连生产中转 47.99.47.169:11500）；`./edge_forward.sh --start` 建立转发 |
| `production/relay/` | 中转服务器（加法部署） | `python3 radio_link_relay.py`（短波/卫星专用转发链路，11450→11550 双端口自包含单文件；server_v8 与既有端口不动） |
| `production/device/` | 端侧设备真机 | `export EDGE_HOST=<网关IP>` 后 `./run_2_2_3.sh`（大纲全流程） |
| `production/cloud/` | 任意位置 | `bash query_relay_state.sh`（生产只读验证：11502 白名单 + 11501 设备状态 + 服务器 STAT 对账） |
| 根目录 | 联调机 | `./run_2_2_3_production.sh`（单机联调，端口与演示错开） |

硬约束：远端中转为生产环境**只读**，不改动远端代码与白名单，只借用白名单内设备 ID；
本地云终端禁止直连远端业务通道。真机逐步执行手册见
[production/STEP_BY_STEP.md](production/STEP_BY_STEP.md)，完整说明见
[production/README.md](production/README.md)。
