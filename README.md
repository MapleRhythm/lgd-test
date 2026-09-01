# lgd-test

陆工大测试大纲 v49 §2.2.3–2.2.5 的三终端演示与生产部署包。

- **端侧设备** / **边缘网关** / **云端管理节点** 三终端演示：`protocol_test_runtime.py`
  承载大纲命令（接入检查、多模态并发、多源接入分类、可信接入、策略路由等），
  并把业务流真实打到本地边缘网关（TCP 8888 JSON / 7777 媒体）→ 本地中转 → 云端节点。
- `original/`：现网源码参考（中转 server_v8、边缘网关 gateway_merged、核心网关 gateway_v1）。
- `production/`：真机可部署的生产版 2.2.3 流程包（含逐步执行手册）。

## 快速开始（演示）

```bash
./run_edge_terminal.sh              # 边缘网关终端（顺带拉起本地中转，不在任何终端显示）
./run_cloud_terminal.sh             # 云端管理节点终端
./run_device_terminal.sh video      # 视频流终端      182D48D7  Wi-Fi
./run_device_terminal.sh sensor     # 传感器终端      3C15DB07  蓝牙
./run_device_terminal.sh env        # 环境监测模块终端 990E261B  有线
```

流程与命令对照见 [README_protocol_tests.md](README_protocol_tests.md)；
生产部署见 [production/README.md](production/README.md) 与
[production/STEP_BY_STEP.md](production/STEP_BY_STEP.md)。
