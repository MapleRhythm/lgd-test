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
./run_device_terminal.sh video      # 视频流终端      182D48D7  有线（含真实媒体口 7777）
./run_device_terminal.sh sensor     # 传感器终端      3C15DB07  Wi-Fi（名单过滤生效后自动换无关 ID）
./run_device_terminal.sh env        # 环境监测模块终端 990E261B  Wi-Fi/蓝牙/有线（轮换）
```

大纲 2.2.4 的三个端侧终端可同时运行（状态写入与 msg_id 分配均有跨进程锁）。
三个 start 命令默认持续发送（Ctrl-C 停止）；`./multi_source_access.sh` 执行前
边缘网关不受理端侧数据（接入门）。可信接入：起步不过滤名单（全部放行），
`./trust_access_add_whitelist.sh` 拉取并打印服务器白名单后过滤生效，传感器
终端自动换无关设备 ID 被拒收并记阻断日志。

每次重跑自动清台账：开启边缘终端即开启新演示会话（复位转发门/接入门/
过滤门并清空全部 jsonl 台账）；`run_2_2_3.sh` / `run_2_2_4.sh` / `run_2_2_5.sh`
前置同样各自 `init_link_connect.sh --reset`，统计数据不会跨轮次残留。

## 大纲 2.2.3 运行流程（mock 演示版）

三终端就绪后按步骤执行；2.2.3 的端侧就是**环境监测终端**
（该终端内 `start_test` / `keep_transfer` / `multi_link_bandwidth` 默认即环境监测身份
990E261B / env）。一键脚本：在环境监测终端执行 `./run_2_2_3.sh`。

| 步骤 | 命令 | 执行终端 |
|---|---|---|
| 前置 | `./init_link_connect.sh --reset` → `./policy-route.sh --start` → `./msg-encap.sh --start` | 环境监测 |
| 1 初始化端侧链路 | `./init_link_connect.sh` | 环境监测 |
| 2 接入链路连通性检查 | `./check_link_connect.sh` | 环境监测 |
| 3 接入链路时延实测 | `./ping_link_test.sh` | 环境监测 |
| 4 业务数据发送 | `./start_test.sh` | 环境监测 |
| 5 网关服务日志查询 | `./query_service_log.sh` | 边缘网关 |
| 6 建立转发通道 | `./edge_forward.sh --start`（**此后业务流真实发往云端**） | 边缘网关 |
| 7 持续传输（仅有线） | `./keep_transfer.sh --duration 600 --interval 1` | 环境监测 |
| 8 多模态并发传输 | `./multi_link_bandwidth.sh --duration 5` | 环境监测 |
| 9 转发建立后端到端发送 | `./start_test.sh` | 环境监测 |
| 10 端到端链路数据查询 | `./query_link_data.sh`（含云端实时通道表与端到端核对） | 云端 |

> 步骤 6 开门前，业务报文只在边缘接入与分类，不向云端转发（大纲语义）。
> 详见 [README_protocol_tests.md](README_protocol_tests.md)。

## 大纲 2.2.3 运行流程（生产版）

生产包在 `production/`，目录即三节点部署单元，协议与 `original/` 现网实现逐字节一致：

| 目录 | 部署到 | 关键动作 |
|---|---|---|
| `production/edge/` | 边缘网关真机 | `./run_gateway.sh`（默认连生产中转 47.99.47.169:11500）；`./edge_forward.sh --start` 建立转发 |
| `production/device/` | 端侧设备真机 | `export EDGE_HOST=<网关IP>` 后 `./run_2_2_3.sh`（大纲全流程） |
| `production/cloud/` | 任意位置 | `bash query_relay_state.sh`（生产只读验证：11502 白名单 + 11501 设备状态 + 服务器 STAT 对账） |
| 根目录 | 联调机 | `./run_2_2_3_production.sh`（单机联调，端口与演示错开） |

硬约束：远端中转为生产环境**只读**，不改动远端代码与白名单，只借用白名单内设备 ID；
本地云终端禁止直连远端业务通道。真机逐步执行手册见
[production/STEP_BY_STEP.md](production/STEP_BY_STEP.md)，完整说明见
[production/README.md](production/README.md)。
