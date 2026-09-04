# 三开发板 + 云服务器部署手册(最终架构)

复用现网逻辑:**云服务器跑 server_v8(已在跑),核心板跑 gateway_v1 连云,
边缘板经 5G 拨号上云,端侧板网线接边缘**。三块板凑齐现网缺失的端侧环节:

```
端侧板 ──网线──> 边缘板 ──5G拨号(quectel)──> 云服务器 47.99.47.169
192.168.4.10     192.168.4.1                    server_v8(中转/白名单/11500-11502)
                                                      ↑
                                  核心板 gateway_v1 ────┘(自身网络/拨号连云,
                                  消费业务通道,前端 10000-10017 输出)
```

服务器是自己的:**白名单可改**(ssh 上去操作)、**link_block 可下发**
(注意 `/stop1` 作用于服务器上组1的全部边缘,下发前确认没有别的组1边缘在跑)。

## 0. 前提确认(动板子之前)

```bash
# 任一台能上网的机器(或 Windows):
curl http://47.99.47.169:11502/whitelist     # server_v8 活着,白名单内容可见
```

- 白名单里应有要用的设备 ID(182D48D7 / 3C15DB07 / 990E261B)。
- 缺设备时的加法:把 lgd-test 传到云服务器解包,在服务器上执行
  `./whitelist_add_device.sh <设备ID>`(GET→合并→POST,server_v8 持久化,
  边缘按拉取周期自动同步,无需重启);或直接改 server_v8 工作目录下的
  whitelist.json(自己的服务器,可改)。

## 1. Windows 上打包(避开 CRLF 坑)

仓库是 Windows 上 `autocrlf=true` 检出的,直接拷文件夹到板子会让所有 `.sh` 报
`/usr/bin/env: 'bash\r'`。**用 `git archive` 打包**——出来的就是 LF 行尾:

```powershell
cd "D:\SEU\课题组\陆工大项目\陆工大项目\陆工大项目\code\lgd-test"
git archive --format=tar -o lgd-test.tar HEAD
```

(板子尚无 IP 时用 U 盘拷,完整步骤见 §1.2。)

若已经直接拷过文件夹,在板上补救:

```bash
find . -name '*.sh' -not -path './.git/*' -exec sed -i 's/\r$//' {} +
```

板上只需 python3(Ubuntu 18.04 自带 3.6,代码已确认兼容),标准库即可,无需 pip。

## 1.1 代码更新怎么最快传到板子

全量 `git archive` tar(§1)任何时候可用;日常小改动更快的是**只传改动文件**
或 **Windows 直连交换机**:

```bash
# 只传改动文件(目录结构对上直接覆盖)。单拷 .sh 记得 chmod +x。
scp send_business.py link_block.sh root@192.168.4.10:/root/lgd-test/production/device/
# 核心板不在局域网内,改动文件走 U 盘,见 §1.2
```

```powershell
# Windows 直连交换机(推荐,一次配好):PC 接同一交换机,本地网卡配静态
# IP 192.168.4.100/24(无网关),之后 PowerShell 直接对端侧/边缘板 scp:
scp .\lgd-test.tar root@192.168.4.10:/root/
scp .\send_business.py root@192.168.4.10:/root/lgd-test/production/device/
```

提交记录里 .sh 已带执行位、tar 内行尾为 LF:全量解包后**不需要**再
chmod/sed。边缘/端侧板之间可互相 scp;核心板只能 U 盘(§1.2)或接进交换机。

## 1.2 用 U 盘更新代码(核心板必走,三块板通用)

核心板不在局域网(只有 5G 出网),代码更新只能靠 U 盘;端侧/边缘板平时
走 §1.1 的 scp,没 scp 路径时同法。分全量和单文件两种。

### 全量更新(git archive tar)

Windows 上按 §1 打包 `lgd-test.tar` 拷进 U 盘,板上:

```bash
lsblk -f                                     # 找 U 盘分区(常见 /dev/sda1)
sudo mkdir -p /mnt/usb
sudo mount /dev/sda1 /mnt/usb
mkdir -p ~/lgd-test
tar xf /mnt/usb/lgd-test.tar -C ~/lgd-test  # tar 无顶层目录,必须先建目录再 -C,否则散在当前目录
sudo umount /mnt/usb                         # 拔盘前必做,否则下次卷脏挂不上
```

- 原地覆盖不影响 `~/lgd-test/production/*/.state`(门标记/发送审计保留);
  要彻底干净先 `rm -rf ~/lgd-test` 再解包,但三扇门回到全关。
- tar 内 .sh 已带执行位、行尾 LF(§1),解包即用,无需 chmod/sed。
- 覆盖不热生效:核心板按 §2 重启 `cloud_node.sh`,边缘板重启
  `run_gateway.sh`(正在跑的进程内存里还是旧代码)。

### 只更新个别文件(日常小改动)

U 盘拷改动文件,板上按原路径覆盖。**从 Windows 工作区拷出来的文件是
CRLF**:`.sh` 必须清行尾否则报 `bash\r`;`.py` 无影响(Python 容忍 CRLF):

```bash
sudo mount /dev/sda1 /mnt/usb
cp /mnt/usb/cloud_node.sh ~/lgd-test/                       # 举例:根目录脚本
cp /mnt/usb/gateway_merged.py ~/lgd-test/production/edge/   # 边缘网关
cp /mnt/usb/send_business.py ~/lgd-test/production/device/  # 端侧发送
sed -i 's/\r$//' ~/lgd-test/cloud_node.sh   # 只 .sh 需要;.py 不用
chmod +x ~/lgd-test/cloud_node.sh           # 只 .sh 需要(执行位不在文件里)
sudo umount /mnt/usb
grep -rl $'\r' ~/lgd-test --include='*.sh' || echo OK   # 自检:输出 OK=无残留 CRLF
```

### U 盘格式与拔盘

exFAT 走 exfat-fuse,拔盘前忘 `umount` 会"卷脏",下次挂载报
`Transport endpoint is not connected`(处置见坑 11)。一劳永逸:Windows
上把 U 盘格成 FAT32(内核原生 vfat 驱动,不走 FUSE;tar 体积远小于
4G 单文件限制)。

### 认不到 U 盘(lsblk 里没有 sd 设备)

`mount: special device /dev/sda1 does not exist` 多半不是命令错,是
板子没认到盘——`lsblk -f` 里只有 mmcblk(内部存储)没有 sd 开头的
设备即坐实。逐条排查(插稳/换 USB 口后等 3~5s 再 `lsblk -f`;
`dmesg | tail -20` 看有无 `usb-storage`/`[sda] sda1` 字样,设备名以
dmesg 报的为准);`device descriptor read/64 error` = 供电/兼容性,
换小容量盘或换口。

### 方案 B:核心板经 5G 从云服务器拉包(U 盘不行时)

核心板有 5G 默认路由,**主动出网**不被 NAT 拦,可绕开 U 盘:

```powershell
# Windows:先把新 tar 传到自己服务器
scp .\lgd-test.tar root@47.99.47.169:/root/
```

```bash
# 核心板:经 5G 拉下来解包,按 §2 重启 cloud_node.sh
scp root@47.99.47.169:/root/lgd-test.tar /root/
mkdir -p ~/lgd-test
tar xf /root/lgd-test.tar -C ~/lgd-test
```

tar 才几 MB,5G 流量开销可忽略。端侧/边缘板不需要此法(§1.1 直接 scp)。

## 2. 核心板(gateway_v1 连云)

需要能到 47.99.47.169(拉起移远拨号):

```bash
systemctl start quectel-cm           # 若已停;等 usb0 拿到 IP
```

跑新代码(推荐,~/lgd-test 是最新版):

```bash
cd ~/lgd-test
CLOUD_REDACT=0 ./cloud_node.sh --server-host 47.99.47.169
```

`CLOUD_REDACT=0` 是真机调试口径:IP/端口号原样打印、心跳逐条打印;
不设则保留录屏脱敏层(见下方说明)。

- `cloud_node.sh` 的参数原样传给 gateway_v1 的 argparse;
- 它会关闭宝通口监听(板上无宝通硬件,不影响);
- 卫星入库走默认 `--b-host 47.99.47.169:11410`(云端 B 服务,与原现场一致)。

或直接还原旧服务(旧版本代码,原现场):

```bash
systemctl enable --now gateway-v1
```

验证:日志出现到 47.99.47.169 各通道的连接;`ss -lnt | grep -E ':100[0-1][0-9]'`
前端口在监听(浏览器接核心板输出侧网卡可看通道画面)。

收消息日志节奏:JSON 通道**逐条实时打印**——每收到一条业务 JSON 立即打一行
`[JSON-UP][通道][DETAIL] rate=x json/s | total=N | last_json=报文预览(超500字符截断)`
(cloud_node.py 默认 `JSON_REPORT_INTERVAL=0`,逐条;演示终端嫌刷屏用
`CLOUD_JSON_REPORT_INTERVAL=60 ./cloud_node.sh ...` 恢复每分钟一条汇总);
视频通道仍每 30 帧一条 `[UPSTREAM][ch][VID0]`(逐帧打印会被 25fps 淹掉)。
真机调试加 `CLOUD_REDACT=0`:IP/端口号原样、心跳逐条打印(约 1 条/s/网关);
不加则走录屏脱敏层——日志里 `[REDACTED ENDPOINT]/[REDACTED PORT]` 为
占位符,心跳只打状态翻转(红=中断/绿=恢复)。log() 带 flush=True,
tee/重定向到文件也逐行实时落盘。

## 3. 边缘板(新网关 + 5G 上云 + 局域网接入)

```bash
# 1. 拨号(旧 gateway-startup 停了,拨号手动拉)
/root/Net_Tools/quectel-CM > /run/quectel-CM.log 2>&1 &
# 等 usb0 拿到 IP,确认默认路由走上 usb0:
ip route show default           # 应见 dev usb0

# 2. 局域网口给端侧板接入(接交换机或直连端侧板)
#    注意:运行时配置,板子重启即失——要持久化见 §8.5
ip link set fm1-mac5 up
ip addr flush dev fm1-mac5
ip addr add 192.168.4.1/24 dev fm1-mac5     # 5=挑的口 选一个空的

# 3. 起网关:上行走 5G 到云,白名单自动取自云 11502
cd ~/lgd-test/production/edge
EDGE_CLOUD_HOST=47.99.47.169 EDGE_DISABLE_SATELLITE=1 ./run_gateway.sh
```

- 网关日志同屏并**自动落盘 `gateway.log`**(大纲 query_service_log 步骤
  tail 这个文件;追加不覆盖,要干净日志先 rm);
- 板上接了 400-GM12 卫星模组就去掉 `EDGE_DISABLE_SATELLITE=1`(串口
  /dev/ttyUSB0@115200);
- 宝通口默认指向 192.168.2.1:9100,接了宝通工控机就把它放到那个网段;
- **等日志出现白名单拉取成功(类似 `cached_devices=N`)再继续**;
- 三扇门(受理/转发/过滤)默认全关,都在本板用 `init_link_connect.sh` /
  `multi_source_access.sh` / `edge_forward.sh` / `trust_access_add_whitelist.sh` 控制。

## 4. 端侧板

```bash
# 运行时配置,重启即失——要持久化见 §8.5
ip link set fm1-mac5 up
ip addr flush dev fm1-mac5
ip addr add 192.168.4.10/24 dev fm1-mac5
cd ~/lgd-test/production/device
export EDGE_HOST=192.168.4.1          # 边缘网关(局域网)
export RELAY_HOST=47.99.47.169        # 云服务器(查询用;需板子自己有路由,
                                      # 没有就在边缘板上跑云侧查询)

./check_link.sh                       # 预期:JSON接入/媒体接入 两行"连通"
```

**必配:免密 ssh 到边缘板**——一键脚本的门命令经**非交互 ssh** 在边缘板执行,
没配会卡在主机指纹 yes/no 或密码提示(脚本是裸 `ssh`,不带任何 -o 参数):

```bash
ssh root@192.168.4.1          # 第一次:输 yes 接受指纹 → 输边缘板 root 密码
                              # (与串口登录同密码)→ 进去后 exit
ssh-keygen -t ed25519         # 一路回车(已有 key 就别覆盖)
ssh-copy-id root@192.168.4.1  # 再输一次密码,装上公钥
ssh root@192.168.4.1 hostname # 不再要密码 = 成功
export EDGE_SSH=root@192.168.4.1
```

- `ssh: /usr/local/lib/libcrypto.so.1.0.0: no version information available`
  是旧板自定义 openssl 的**无害告警**,每条 ssh 都会打,不影响登录与执行;
- 两块板代码都在 `/root/lgd-test`,`EDGE_REMOTE_DIR` 用脚本默认值
  (`../edge` 展开正好是 `/root/lgd-test/production/edge`),**不用导出**。

**端侧板怎么查云**:query_relay_state 要访问 RELAY_HOST,端侧板默认没有
去云的路由(只有到边缘的局域网),二选一:

```bash
# 方案 A(推荐):边缘板开 NAT,端侧经 5G 出网——在边缘板上:
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -o usb0 -j MASQUERADE
# 在端侧板上:
ip route add default via 192.168.4.1
curl -m 5 http://47.99.47.169:11502/whitelist    # 通了即可在本板跑云侧查询

# 方案 B:云侧查询挪到有 5G 的边缘板上跑:
RELAY_HOST=47.99.47.169 bash ~/lgd-test/production/cloud/query_relay_state.sh
```

## 5. 跑大纲流程

### 2.2.3 多模态接入与端到端传输(端侧板)

```bash
export EDGE_HOST=192.168.4.1 RELAY_HOST=47.99.47.169 EDGE_SSH=root@192.168.4.1
KEEP_DURATION=60 ./run_2_2_3.sh       # 快验 60s;大纲值 600
```

### 2.2.3 逐步手动测试(逐步版,全部在端侧板执行)

先在端侧板定义快捷函数(追加到 ~/.bashrc 可长期用):

```bash
edge() { ssh root@192.168.4.1 "cd /root/lgd-test/production/edge && $*"; }
```

随时盯边缘实时日志(Ctrl-C 退出):

```bash
ssh root@192.168.4.1 'tail -f /root/lgd-test/production/edge/gateway.log'
```

十步全在端侧板(`cd ~/lgd-test/production/device` 后)。
**注意**:send_business.py 现默认后台形态(一条命令只回一行 `[LAUNCH]`
即返回提示符,明细写 `.state/sender-*.log`);下表都带 `--fg` 前台直跑
便于当场看 `[SEND]/[SUMMARY]`,要后台就去掉 `--fg`:

| 步 | 命令 | 看什么 |
|---|---|---|
| ① | `edge ./init_link_connect.sh` | 多源接入门打开 |
| ② | `./check_link.sh` | 两行"连通" |
| ③ | `./ping_link.sh` | min/avg/max |
| ④ | `python3 send_business.py --host 192.168.4.1 --device-id 3C15DB07 --count 5 --fg` | 边缘日志 `message=1..5`;转发门未开 → 排队 |
| ⑤ | `edge 'tail -n 40 gateway.log'` | `received=5 bytes=1553` 对账 |
| ⑥ | `python3 send_business.py --host 192.168.4.1 --device-id 3C15DB07 --link wired --duration 60 --interval 1 --fg` | 逐秒收包(大纲值 600) |
| ⑦ | `python3 send_business.py --host 192.168.4.1 --device-id 3C15DB07 --link all --duration 5 --interval 1 --fg` | 三路独立连接 + [BANDWIDTH] |
| ⑧ | `edge ./edge_forward.sh --start` | [EDGE-FORWARD] 开启连云 |
| ⑨ | 同④ | 收包后转发上云,不再排队 |
| ⑩ | `RELAY_HOST=47.99.47.169 bash ~/lgd-test/production/cloud/query_relay_state.sh` | 白名单+DEVSTATE;三级对账 |

对照实验(逐步模式的价值):

```bash
edge ./edge_forward.sh --stop      # 再发 → 排队/gate_drop(队列上限 1000)
edge ./init_link_connect.sh --reset # 再发 → 不受理
# 换业务载荷:--biz-type env / video / fire(--fire true 有火情)
```

"排队/不排队"怎么判定——看三个计数字段(网关默认 `--compact-log`,
逐条出队 DEBUG 行不打,靠计数判定):

| 字段(所在行) | 含义 |
|---|---|
| `queued=N`(`client closed` 行) | 收下并放进转发队列的条数(排队证据) |
| `queue=N/1000`(`[JSON][SEND] total=` 行 / RECV 周期行) | 当前积压深度;1000 是队列上限 |
| `gate_drop=N`(`client closed` 行) | 接入门关着被直接丢弃的条数(根本不排队) |

三种状态一眼区分:

| 状态 | gateway.log 特征 |
|---|---|
| 排队(转发门关) | `数据转发通道未建立，等待 ./edge_forward.sh --start`;发完 `client closed ... received=5 ... queued=5 ... gate_drop=0`;**全程没有 `[JSON][SEND]` 行**(没连云没出队) |
| 转发(转发门开) | `数据转发通道已建立` → `[JSON][SEND] connected cloud 47.99.47.169:11500`;统计行每 5s 一条 `[JSON][SEND] total=增长 \| x.x msg/s \| queue=积压→0/1000 \| drop=0`;开门瞬间先放积压(先进先出),total 从 0 跳到积压数即出队证据 |
| 不受理(接入门关) | 发完 `client closed ... received=N ... queued=0 ... gate_drop=N`;queue 始终 0/1000 |

观测窗口另开一个终端(只过滤关键行,Ctrl-C 退出):

```bash
ssh root@192.168.4.1 'tail -f -n 0 /root/lgd-test/production/edge/gateway.log' \
  | grep --line-buffered -E 'EDGE-FORWARD|JSON.\[SEND\]|client closed'
```

想看逐条出队明细(`[JSON][SEND] dequeued bytes=... \| preview=` / `success seq=`)
就关掉紧凑日志再重启网关(量大,看完记得还原):

```bash
ssh root@192.168.4.1 'cd /root/lgd-test/production/edge && cp run_gateway.sh run_gateway.sh.bak && sed -i "/^  --compact-log$/d" run_gateway.sh'
# 重启网关生效;还原:mv run_gateway.sh.bak run_gateway.sh 再重启
```

2.2.5 逐步可用(端侧板);2.2.4 的逐步版见下节:

```bash
./link_block.sh --stop      # / --recover
```

### 2.2.3 云侧只读验证

(一键脚本最后一步自动跑;若端侧板没按第 4 节方案 A 出网,
该步会超时——挪到边缘板跑同一条命令即可):

```bash
RELAY_HOST=47.99.47.169 bash ~/lgd-test/production/cloud/query_relay_state.sh
```

### 短波/卫星接收记录查询(专用转发链路,可选·加法部署)

现网业务下发口(11400-11409)是长连接消费,过 5G NAT 闲置即静默断连,
死连接留在共享队列抢报文(坑17)。`production/relay/` 是加法方案:中转
服务器新开 11450(入口,收边缘推送)与 11550(出口,云端拉取)两个端口跑
伴生转发器(server_v8 不动)。转发器是自包含单文件 py,服务器上不需要
任何 sh 脚本,python3 直接跑。

```bash
# ① 云服务器(与 server_v8 同机;只加端口,放行 11450/tcp 与 11550/tcp)
scp -r production/relay/ <中转机>:~/radio-relay/
ssh <中转机> "cd ~/radio-relay && nohup python3 -u radio_link_relay.py >/dev/null 2>&1 &"
# 验证: ssh <中转机> "curl -s http://127.0.0.1:11550/health"

# ② 边缘板起网关前加一个环境变量(指向入口 11450;时延/节奏口径不变,失败自动回退统一上行)
export EDGE_RADIO_RELAY_URL=http://47.99.47.169:11450

# ③ 云侧查询(2.2.4/2.2.5 接收记录,走出口 11550;调整前/后用 --after 游标增量)
RELAY_HOST=47.99.47.169 bash ~/lgd-test/production/cloud/query_radio_records.sh
```

不设 `EDGE_RADIO_RELAY_URL` 时一切走既有路径(统一上行+11503),该部署
完全可逆:中转机上 kill 掉 radio-relay 进程即恢复原状。

### 2.2.4 多源业务接入与可信接入(端侧板)

```bash
./run_2_2_4.sh
```

trust 步骤等价于在边缘板执行:

```bash
cd ~/lgd-test/production/edge
RELAY_HOST=47.99.47.169 ./trust_access_add_whitelist.sh 182D48D7 3C15DB07 990E261B
```

非法设备(ILLEGAL-SENSOR / UNKNOWN-001)不在云白名单 → 边缘拒收演示照常。
**加白名单演示**:在云服务器上跑 `./whitelist_add_device.sh <设备ID>`(第 0 节)。

### 2.2.4 逐步手动测试(逐步版,全部在端侧板执行)

前提:边缘网关在跑(§3;直发模式已是默认,不用加参数)、`edge()`
快捷函数和观测窗口同 2.2.3 逐步节。语义:**接入门先关**(开闸前发送
只计 gate_drop,做受理前后对比)、**转发门先开**(云侧一致性核对需要,
此后受理的业务流即发往 47.99.47.169,注意 5G 计费)、**名单过滤门到
步骤⑥才启用**。步骤①③刻意用**后台形态**(一条命令一行 `[LAUNCH]`
即返回,模拟现网"单终端依次输入、三终端并发"),明细在各
`.state/sender-*.log`。

| 步 | 命令 | 看什么 |
|---|---|---|
| ⓪ | `edge './init_link_connect.sh --reset && ./edge_forward.sh --start'` | 复位关三扇门,再单开转发;此后受理即上云 |
| ① | `python3 send_business.py --host 192.168.4.1 --device-id 182D48D7 --biz-type video --link wired --duration 5 --interval 1` | 一行 `[LAUNCH]`(含 pid 与日志路径)即返回,后台发送 |
| ① | `python3 send_business.py --host 192.168.4.1 --device-id 3C15DB07 --biz-type sensor --link wifi --duration 5 --interval 1` | 同上;5s 后各进程自行结束,`tail .state/sender-*.log` 看 [SUMMARY] |
| ① | `python3 send_business.py --host 192.168.4.1 --device-id 990E261B --biz-type env --link rotate --duration 5 --interval 1` | 边缘日志:接入门未开 → `client closed ... queued=0 ... gate_drop=N`(只计收,不受理) |
| ② | `edge ./multi_source_access.sh` | 多源接入门打开,边缘开始受理 |
| ③ | `python3 send_business.py --host 192.168.4.1 --device-id 182D48D7 --biz-type video --link wired --duration 12 --interval 1` | 边缘日志 `[MULTI-SOURCE]` 公告;`queued=` 增长(受理) |
| ③ | `python3 send_business.py --host 192.168.4.1 --device-id 3C15DB07 --biz-type sensor --link wifi --duration 12 --interval 1` | `[JSON][SEND] total=` 增长(在转发上云,不排队) |
| ③ | `python3 send_business.py --host 192.168.4.1 --device-id 990E261B --biz-type env --link rotate --duration 12 --interval 1` | 环境监测三链路逐条轮换(风速模拟,DEV-001 在册) |
| ③ | `python3 send_business.py --host 192.168.4.1 --device-id 182D48D7 --biz-type fire --link wired --interval 10 --duration 12` | fire 报文约 10s 一条(默认无火情,演练加 `--fire true`);直发模式另见 `[BAOTONG-V2][SEND]` 短波约 20s 一条 |
| ④ | `edge 'tail -n 40 gateway.log'` | query_service_log:受理/转发行对账 |
| ⑤ | `RELAY_HOST=47.99.47.169 bash ~/lgd-test/production/cloud/query_relay_state.sh` | 白名单在册 + DEVSTATE(端侧没出网则挪边缘板跑,§4 方案) |
| ⑤ | `tail -n 5 ~/lgd-test/production/device/.state/sent.jsonl` | 端侧发送审计,与云侧 STAT 对账 |
| ⑥ | `edge 'RELAY_HOST=47.99.47.169 ./trust_access_add_whitelist.sh 182D48D7 3C15DB07 990E261B'` | 只读拉取云 11502 白名单,三设备逐个在册 OK;名单过滤门生效(网关免重启) |
| ⑦ | `python3 send_business.py --host 192.168.4.1 --device-id ILLEGAL-SENSOR --count 5 --fg` | 发送端 [SUMMARY] 成功(TCP 层通),但边缘日志 `[WHITELIST][BLOCK] device_id=... rejected_message={...not_in_whitelist...}` |
| ⑦ | `python3 send_business.py --host 192.168.4.1 --device-id UNKNOWN-001 --count 5 --fg` | 同上;`whitelist_drop` 计数累计 |
| ⑧ | `edge 'EDGE_LOG=gateway.log ./trust_access_calculate.sh'` | 门状态公告(最近4条)+拒收明细(最近5条)+whitelist_drop/gate_drop 累计 |

收尾(可选):`edge './init_link_connect.sh --reset'` 关三扇门;
想前台盯①③的逐条明细,给对应命令加 `--fg` 即可。

### 2.2.5 链路屏蔽(production/device/link_block.sh,直连中转控制口)

```bash
./link_block.sh --stop      # POST /stop1 到云 11507:断组1上行的 5G
./link_block.sh --recover   # POST /recover1 恢复
```

生产版脚本独立直连中转控制 HTTP,不依赖根目录演示层(那个要 Python 3.8+,
只能在 Windows/WSL 本机跑)。下发后观察边缘板日志
`[LINK-STATUS] connected=False/True`。

⚠️ `/stop1` 作用于服务器上**组1的全部边缘**——确认没有别的组1边缘在用再下发。

## 6. 预期现象速查

| 动作 | 看哪里 | 预期 |
|---|---|---|
| `send_business.py --count 5` | 边缘板日志 | 接收行;转发门前 `[EDGE-FORWARD] 通道未建立`(排队,预期) |
| `edge_forward.sh --start` 后再发 | 云服务器 server_v8 日志 / 核心板 gateway_v1 日志 | 数据到达、通道转发、STAT 计数增长 |
| 开转发后 | `curl :11501` / query_relay_state | 设备状态在线 |
| 2.2.4 非法设备 | 边缘板日志 | `[WHITELIST][BLOCK] reason=not_in_whitelist` |
| 2.2.5 `link_block --stop` | 边缘板日志 | `[LINK-STATUS] connected=False`,路由降级 |
| 探测/短连接关闭 | 边缘板日志 | `[TIME_SET] stopped ... Bad file descriptor` 为对时下行线程随短连接退出的正常噪音,无业务影响;对账看 `[JSON][RECV]` 计数与 `[JSON][SHORTWAVE] message=N` 行 |

## 7. 常见坑

1. **`bash\r` 报错** = CRLF:用 git archive 打包,或第 1 节 sed 补救。
2. **边缘上不了云**:先查拨号(`ip route show default` 是否 dev usb0)、
   再查信号(`tail /run/quectel-CM.log`)。
3. **白名单拉取失败**:边缘板若设了 `http_proxy`,先 `unset`;确认
   `curl http://47.99.47.169:11502/whitelist` 通。
4. **门命令无效**:标记文件(`edge/.state/*.enabled`)在执行机本地,
   门命令必须在**边缘板**执行(配了 EDGE_SSH 则自动)。
5. **长流量前先开转发门**:关闭期间网关队列上限 1000 条,超出丢弃。
6. **蜂窝流量**:持续传输走 5G 计费,演示用短时长参数先过一遍。
7. **run_2_2_3 卡在 ssh 的 yes/no 或密码**:一键脚本的 ssh 是非交互裸调用,
   先按第 4 节配好免密再跑;`libcrypto.so.1.0.0: no version information
   available` 是无害告警,忽略。
8. **端侧板跑 query_relay_state 超时**:端侧没有去云的路由——第 4 节
   方案 A 在边缘开 NAT,或把查询挪到边缘板跑。
9. **ssh 密钥配到了错误的板**:在边缘板的 ssh 会话里执行了 ssh-keygen
   (配成了边缘→自己,端侧板依旧要密码)。配之前先
   `ip addr show fm1-mac5` 确认本机地址(.10=端侧板)再操作。
10. **板上 Python 是 3.6.9**:上板的 .py 禁用 `from __future__ import
    annotations`、`X | None`、`list[str]`、海象运算符等 3.7+ 写法(仓库
    已全量改写兼容;根目录演示层 protocol_test_runtime.py 是例外,只在
    本机 3.8+ 跑,不上板)。
11. **U 盘 "Transport endpoint is not connected"**:exfat-fuse 挂载失效
    (多半上次拔盘前没 umount,卷脏了)。`sudo umount /mnt/usb ||
    sudo fusermount -u /mnt/usb` 清掉再重新 mount;每次拔盘前先
    `sudo umount /mnt/usb`。反复发作就在 Windows 上把 U 盘格成 FAT32
    (内核原生 vfat 驱动,不走 FUSE,tar 体积远小于 4G 限制)。
12. **重启后 ssh "Network is unreachable"**:`ip addr add` 重启即失,
    fm1-mac5 没有 IP——按 §3/§4 重配,或按 §8.5 做开机持久化。
13. **端侧/边缘跑在了同一块板上**:边缘网关日志里客户端地址全是
    `192.168.4.1:xxxx` 而不是 `192.168.4.10:xxxx`——说明发数据的板子
    自己持有 .1,ssh/发送全在本机回环,网线那段没走到。验收标准:
    **边缘日志客户端必须是 192.168.4.10**。两块板各 `ip -4 addr |
    grep 192.168.4` 核对;认板看 `ip addr show usb0` 有运营商 IP 的
    那块是边缘板。
14. **串口控制台极易敲错板**:三块板提示符完全相同(root@localhost),
    IP/网关/ssh 配置命令敲错板就是坑 9/13 的根源。第一天就执行
    `cat /sys/class/net/fm1-mac5/address` 记下每块板 MAC 并贴物理标签;
    配 IP 后用 `ssh root@192.168.4.1 'cat /sys/class/net/fm1-mac5/address'`
    核对返回的不是本板 MAC,证明命令真的打在了另一块板上。
15. **串口控制台不要整块粘贴多行命令**:串口没有粘贴缓冲,多行与
    回显/输出交错错乱,命令会乱序、半截执行(现象:粘贴文本和 lsblk
    之类的输出混排在一起)。**一次敲一条,等出结果再敲下一条**;
    复杂流程写成一行的 `&&` 链或先 scp 上去跑脚本。
16. **核心板收到的条数比端侧发的少、固定隔条丢(如只见第 1/3/5 条)**:
    中转 server_v8 对每组通道是**共享队列、每条只发给一个消费者**
    (`json_queues[gateway_id]`,handle_json_receiver/framed 同理)——
    第二个消费者连着同一通道就会对半轮流分走,不是丢包。查:服务器
    `ss -tn | grep -E ':11406|:11400' | grep ESTAB` 应各只有一条
    (但单条也可能是 5G NAT 留下的僵尸,见坑17),
    server_v8 日志 `[+JSON DOWN] core connected` 次数即消费者数;核心板
    `ps aux | grep -E 'cloud_node|gateway_v1' | grep -v grep` 清到只剩
    一个(注意回收板旧 gateway-v1.service 连的是同一组端口,§8.2),
    Windows/WSL 演示云节点连着生产中转也会分走。
17. **坑16的姊妹坑:服务器 ss 里"只有一条 ESTAB"也可能整条都是僵尸
    (5G 运营商 NAT 静默回收空闲 TCP 映射,服务器永远收不到 FIN)**:
    核心板 `ss -tn | grep 47.99.47.169` 全是 `FIN-WAIT-1/2`(Send-Q=1
    就是那条从没被确认的 FIN)= 核心早已 close 这些连接,但 5G NAT 把
    空闲映射回收了,FIN 根本送不到服务器。server_v8 的下行消费者线程
    (handle_json_receiver 11406-11409 / handle_framed_receiver
    11400-11405)**只 get→sendall、从不 recv**:收不到对端 EOF,
    sendall 把数据写进死连接的内核缓冲也不报错——旧线程变僵尸,继续
    和重连后的新消费者轮流抢同一队列(表象与坑16一模一样:发5条只见
    1/3/5;队列安静时僵尸永不退出,每重启一次核心网关就多漏一个)。
    判别:核心板没有 ESTAB 时,服务器 `ss -tn | grep ':11406' |
    grep -c ESTAB` 仍 >0 即僵尸;或 server_v8 日志
    `[+JSON DOWN] core connected on 11406` 次数减 `[-JSON DOWN]
    core closed` 次数 > 1。处置:重启 server_v8 清光僵尸(边缘/核心
    都会自动重连),再重启核心网关,发5条应见5条。治本(代码已改):
    server_v8 两个消费者入口已开 TCP keepalive(30s 空闲探测,
    enable_tcp_keepalive)——探测包既保住 NAT 映射,又让死连接报错、
    僵尸线程偷下一条消息时 sendall 抛异常自行退出;**改完需把
    original/server_v8.py 重新 scp 到云服务器原路径并重启**。

## 8. 旧自启服务(回收板残留,已处理,留档)

三块板是现网回收的,原自启已停用;**只停用、不删除、全可逆**。

### 8.1 通用检查(每块板)

```bash
systemctl list-unit-files | grep -iE 'gateway|lgd'
ps aux | grep -E 'gateway|server_v8|cloud_node' | grep -v grep
ss -lnt | grep -E ':(8888|7777|11500|11501|11502|9100|100[0-1][0-9])'
crontab -l 2>/dev/null; grep -r gateway /etc/rc.local 2>/dev/null
```

### 8.2 核心板:gateway-v1.service

- 旧现场:`gateway-v1.service` 自启 → `/root/lgd/gateway_v1.py
  --server-host 47.99.47.169`,经 quectel-cm 拨号连云。
- **本架构下的两种用法**:①还原旧服务 `systemctl enable --now gateway-v1`
  (旧代码原现场);②保持停用,用第 2 节的 `cloud_node.sh --server-host
  47.99.47.169` 跑新代码(推荐)。
- 拨号 quectel-cm 独立于 gateway-v1,要网就 `systemctl start quectel-cm`。

### 8.3 边缘板:gateway-startup.service

- 旧现场:自启 → `/root/start_gateway_all.sh`(配生产地址 fm1-mac2=
  192.168.0.233 / fm1-mac4=192.168.1.106 / fm1-mac6=192.168.2.1 → 循环拨 5G
  → 拨通起旧网关 `/root/lgd/run_edge_GW1_v2.sh` → 监控重拨)。
- **本架构下保持停用**:拨号改手动(第 3 节),网关用新的 `run_gateway.sh`。
- 还原现场:`systemctl enable --now gateway-startup`。
- 旧脚本配的三个生产地址停服后仍在网卡上(重启后才不再配置),注意
  fm1-mac2/fm1-mac6 与核心板旧地址相同,同接一个交换机会 IP 冲突——
  测试只用挑好的口,或 `ip addr flush dev fm1-mac2; ip addr flush dev fm1-mac6`。

### 8.4 本测试环境要不要配自启?

不配。演示流程前台跑、日志实时可见、Ctrl-C 即停;`run_gateway.sh` 依赖
`EDGE_*` 环境变量、`cloud_node.sh` 依赖参数,交给 systemd 反而容易错。

### 8.5 板子 IP 持久化(建议做,重启不丢)

`ip addr add` 是运行时配置,板子一重启就没(表现为 ssh 报
"Network is unreachable")。`crontab -e` 各加一行,开机自动配:

```bash
# 端侧板:
@reboot sleep 5 && ip link set fm1-mac5 up && ip addr add 192.168.4.10/24 dev fm1-mac5
# 边缘板:
@reboot sleep 5 && ip link set fm1-mac5 up && ip addr add 192.168.4.1/24 dev fm1-mac5
```

重启后仍需手动做的:边缘板拨号(§3 第 1 步)与起网关(§3 第 3 步);
端侧板若用过 NAT 出网,再 `ip route add default via 192.168.4.1`。
