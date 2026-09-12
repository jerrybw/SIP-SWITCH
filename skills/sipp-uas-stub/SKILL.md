---
name: sipp-uas-stub
description: 用 sipp 当 SIP UAS / Registrar 桩（落地网关、被叫模拟）的完整避坑指南。覆盖 `-aa` 管 OPTIONS、用「统一响应」让**单实例同时当 Registrar + UAS + OPTIONS 应答器**（不需要分支）、sipp 3.6 场景语法三坑（label 必须 id / 无 next 元素 / 连续 optional recv 被拒）、`-oocsf`/`optional=global`/`-sf+-aa` 等死路、以及 `-i` 绑定地址导致的"打不通"假象。适用于 FreeSWITCH/Kamailio/OpenSIPS 等出局到 sipp 桩的 dev 环境联调。附带 5 个开箱即用的工具（`assets/`：统一响应 Registrar+UAS 场景 / 纯 Registrar 场景 / OPTIONS 探针 UAC / 裸 UDP 探针 / 场景 XML 合规校验器）。
agent_created: true
---

# sipp UAS / Registrar 桩避坑指南

## 随本 skill 附带的场景文件（直接用，别手敲）

文件都在 `assets/` 下，**开箱即用**。XML 注释**刻意只用 ASCII**，且**注释内不含 `--`**（XML 规范禁止）—— sipp 3.6 在「声明编码 ≠ 实际字节」或「注释含 `--`」时都会直接报 `Unable to load or parse` 且不给行号，这两点都规避掉才安全。

| 文件 | 用途 |
|---|---|
| `assets/reg_uas.xml` | **推荐**：单实例同时当 Registrar + UAS（REGISTER/INVITE/OPTIONS 统一回 200 OK） |
| `assets/reg-simple.xml` | 只当 Registrar（让注册型网关变 `REGED`，不接呼叫） |
| `assets/uac-options-probe.xml` | 自定义 UAC，探桩是否答 OPTIONS（sipp 没有内建 OPTIONS UAC） |
| `assets/probe_options.py` | 裸 UDP OPTIONS 探针，不依赖任何 SIP 库：`python3 probe_options.py <host> [port] [timeout]` |
| `assets/check-scenario.py` | 场景 XML 合规校验器（下面详述），**改完 XML 先跑它再喂 sipp** |

拷进容器的常规做法（`/sc` 是场景目录挂载点）：

```bash
docker cp <skill>/assets/reg_uas.xml sip-switch-sipp-reg-1:/sc/reg_uas.xml
```

**改完场景 XML 的自查**（sipp 的报错不给行号，这个能给）：

```bash
python3 <skill>/assets/check-scenario.py *.xml
#   reg_uas.xml              OK
#   bad.xml                  FAIL
#       - '--' inside 1 XML comment(s) at byte offsets [40] (XML forbids it; use ';' or a single '-')
#       - not well-formed XML -> not well-formed (invalid token): line 2, column 11
```
它查四件事：**非 ASCII 字节 / UTF-8 BOM / 注释内 `--` / XML 良构性**，后者会给出**精确行列号**。任一不过 → 退出码 1。

> ⚠️ 别把带真实密码的验证脚本一起归档进 skill。本项目 dev 侧 `_work/lab/*.sh` 里硬编码过
> ESL 与 MySQL root 密码，属**未入库调试件**，只作本地临时用。

## 两条路线先选清楚

| 需求 | 路线 |
|---|---|
| 只要 INVITE 自动应答 + OPTIONS 存活探测（不接 REGISTER） | `-sn uas -aa`，见下节 |
| **要 REGISTER（当 Registrar）**，或想**单实例同时干全部**（REGISTER+INVITE+OPTIONS） | 「统一响应」自定义场景，见文末【单实例 Registrar + UAS】 |

## 路线一：内建 UAS

```bash
sipp -sn uas -aa -i <容器真实IP> -p 5060 -t u1 -m 999999 -l 999 -d 1000 -rtp_echo -nostdin -trace_err
```

- `-sn uas`：内建 UAS 场景，INVITE → 180 → 200(+SDP) → ACK → pause(`-d`) → BYE → 200。
- **`-aa` 是 OPTIONS 的开关**：sipp 的 auto-answer 会对 INFO / UPDATE / NOTIFY / **OPTIONS** / REFER 直接回 200 OK。
  没有 `-aa` 时，server 模式收到 OPTIONS **直接丢弃，不发任何响应**（UAC 侧表现为 `200 <---------- 0`，Timeout=1）。
- ⚠️ `-sn uas -aa` **不答 REGISTER**（REGISTER 不在 auto-answer 列表里），FS 侧表现为 REGISTER 发出后对端零响应。

## 四条死路（sipp 3.6.1 实测，别再绕）

| 尝试 | 结果 |
|---|---|
| `-oocsf` / `-oocsn`（out-of-call 场景） | 拒绝启动：`SIPp cannot use out-of-call scenarios when running in server mode` |
| 场景里写 `<recv request="OPTIONS">` + `<recv request="INVITE">` 双分支 | 场景**首条必须是 mandatory recv**，新 call 只能由首条消息创建，第二条永远匹配不上 |
| `<recv request="OPTIONS" optional="global">` | 无活动 call 时 OPTIONS 仍被丢弃（global 消息要挂在已有 call 上）；连续两个 optional recv 还会报 `<recv> before <send> sequence without a mandatory message` |
| `-sf` 自定义场景 **+** `-aa` | sipp 启动后立刻 `Test Terminated`（0 calls），进程秒退。`-aa` 只与内建 `-sn uas` 搭配可用 |
| 想用「先 optional 收 REGISTER，收不到再收 INVITE」实现双协议 | 装不进去：`<recv> before <nop> sequence without a mandatory message. Please remove one 'optional=true'`；且 optional recv 遇到不匹配的消息会**丢弃**它。→ 改用**统一响应**（文末），不要走分支 |

## 两个必踩的坑

1. **`-i` 别填 `0.0.0.0`**：会让 SDP 变成 `c=IN IP4 0.0.0.0`，对端媒体地址不可达。填 `hostname -i` 取到的容器真实 IP。
2. **`-i` 同时也是 bind 地址**：填了容器 IP 后，**loopback（`127.0.0.1:5060`）收不到包**。
   容器内自测要么用 `-i 127.0.0.1`，要么直接打容器 IP。这个坑极易被误判成"sipp 没起来"或"场景写错"。

## 验证方式（跨容器，从一个容器打 sipp 桩）

探测 OPTIONS（自定义 UAC 场景，因为 sipp 没有内建 OPTIONS UAC）：

```xml
<!-- uac-options-probe.xml -->
<scenario name="uac-options-probe">
  <send><![CDATA[
OPTIONS sip:stub@[remote_ip]:[remote_port] SIP/2.0
Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch]
From: sipp <sip:sipp@[local_ip]>;tag=[call_number]
To: stub <sip:stub@[remote_ip]>
Call-ID: [call_id]
CSeq: 1 OPTIONS
Contact: <sip:sipp@[local_ip]:[local_port]>
Max-Forwards: 70
Content-Length: 0

]]></send>
  <recv response="200" timeout="6000" optional="true" />
</scenario>
```

```bash
# OPTIONS：看统计屏 "200 <----------" 是否为 1
sipp -sf uac-options-probe.xml -i <本机IP> -p 5070 -m 1 -nostdin <stub_ip>:5060
# INVITE：看 "Successful call" 是否为 1（对端是 sipp 桩，不涉及鉴权，可用内建 uac）
sipp -sn uac -i <本机IP> -p 5071 -m 1 -d 200 -nostdin <stub_ip>:5060
```

判据：OPTIONS `200 <--- 1 / Timeout 0`；INVITE `180 + 200 + BYE + 200`、`Successful call = 1`。

## 延伸：用 sipp 当 Registrar（让 FS 注册型网关真的 REGED）

> ⚠️ 只想让网关"注册上"用本节的精简场景即可；**若还希望同一实例能接呼叫**（模拟真实落地网关），
> 直接用文末【单实例 Registrar + UAS（统一响应）】——那个场景把 REGISTER 也覆盖了，无需两个实例。

**可行**。FS 的注册型 gateway（`register=true`）会外发 REGISTER，sipp 回 200 OK 即可（**不需要 401 挑战**——FS 只有收到 401/407 才用账号密码重发，直接 200 就认成功）。

```xml
<!-- reg-simple.xml（完整文件见 assets/reg-simple.xml）：收 REGISTER 回 200 OK，并循环等待重注册 -->
<scenario name="SIP registrar stub">
  <label id="1"/>
  <recv request="REGISTER" timeout="0"/>
  <send next="1"><![CDATA[
SIP/2.0 200 OK
[last_Via:]
[last_From:]
[last_To:];tag=[pid]SIPpTag[call_number]
[last_Call-ID:]
[last_CSeq:]
Contact: <sip:[local_ip]:[local_port];transport=[transport]>;expires=3600
Expires: 3600
Content-Length: 0

  ]]></send>
</scenario>
```

```bash
sipp -sf reg.xml -i <容器真实IP> -p 5060 -t u1 -l 100 -nostdin -trace_err
```

**两个硬约束**：

1. **必须覆盖容器 entrypoint**。若镜像的 `ENTRYPOINT` 是固定脚本（例如内部写死 `sipp -sn uas -aa ...`），它会**忽略 docker `command`**，自建场景根本跑不起来。compose 里要显式覆盖：
   ```yaml
   entrypoint: ["sh", "-c"]
   command:
     - exec sipp -sf /sc/reg-simple.xml -i "$$(hostname -i | awk '{print $$1}')" -p 5060 -t u1 -l 100 -nostdin -trace_err
   ```
2. **`-sf` 不能配 `-aa`**（见上文死路表），否则 sipp 秒退。registrar 场景靠 `next="1"` 跳回 `<label id="1"/>` 实现循环，靠 `-l` 放开并发（每次新 Call-ID 的 REGISTER 会开新 call）。

**判据**：`sofia status gateway` 里该 gateway 状态从 `FAIL_WAIT`/`NOREG` 变为 **`REGED`**。

**配套坑**：改已存在 gateway 的目标地址后，`rescan` 不够——FS 内存里的 gateway 是旧快照，要
`sofia profile external killgw <name>` 再 `sofia profile external rescan`，否则一直打旧地址。

## 排查清单

- sipp 进程活着但不响应 → 先核对 `-i` 绑定地址与发包目标地址是否一致。
- 场景加载报错 `<recv> before <send> sequence without a mandatory message` → 有连续 optional recv，或首条 recv 是 optional/global。
- 想让 FS 真的发 OPTIONS ping：注意 FS 对**点对点网关**（auth_type=0）默认 `Ping 0 / PingFreq 0`，要在 gateway 配置里显式开 ping；`sofia status gateway <name>` 可看到 `Ping/PingFreq/PingState`。
- FS 里 gateway 的 Proxy/Realm 可能是**旧快照**：改完数据后要 `sofia profile external rescan` 才刷新；`sofia profile external restart` 会让网关短暂消失（需再 rescan）。

## 单实例 Registrar + UAS（统一响应）—— 推荐做法

目标：**同一个 sipp 进程**既让 FS 的注册型网关保持 `REGED`，又能在被叫时自动应答（含媒体）。
之前认为"做不到、需要两个实例"是**错的**——不需要任何分支，用**统一响应**即可。

### 为什么不能用分支

sipp 3.6.1 场景语法有三条硬约束（都实测踩过）：

1. `<label>` **必须用 `id`**，写成 `name="1"` 会报 `label is missing the required 'id' parameter` 并退出。
2. **不存在 `<next label="x"/>` 元素**（报 `Unknown element 'next'`）。跳转只能靠元素属性：
   `<recv ... next="2"/>`、`<send next="1">`、`<nop next="1"/>`。
3. **不允许两条连续 optional recv**：
   `<recv> before <nop> sequence without a mandatory message. Please remove one 'optional=true'`。
   而且 optional recv 碰到不匹配的消息会把它**丢弃**。→ "先等 REGISTER、收不到再等 INVITE" 根本装不进去。

### 正解：一条正则 recv + 同一个 200 OK

REGISTER 与 INVITE 的 200 OK **头部结构完全相同**（Via/From/To/Call-ID/CSeq/Contact），
差别只有 SDP body。所以只要 body 长度能动态算，一份模板两边通用 —— sipp 的自动变量
**`[len]`** 正是"本次发送消息的 body 长度"，用它填 `Content-Length` 即可。

```xml
<?xml version="1.0" encoding="UTF-8" ?>
<scenario name="reg-uas-unified">

  <label id="1"/>

  <!-- 关键：把所有可能出现的方法一次匹配完（ACK/BYE/OPTIONS 必须在内，
       否则它们撞上这条 mandatory recv 会让场景失败退出 = 桩直接死） -->
  <recv request="(REGISTER|INVITE|ACK|BYE|OPTIONS|CANCEL|INFO|UPDATE|PRACK|SUBSCRIBE|NOTIFY)"
        regexp_match="true" timeout="0"/>

  <send next="1"><![CDATA[
SIP/2.0 200 OK
[last_Via:]
[last_From:]
[last_To:];tag=[pid]SIPpTag[call_number]
[last_Call-ID:]
[last_CSeq:]
Contact: <sip:[local_ip]:[local_port];transport=[transport]>;expires=3600
Expires: 3600
Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, REGISTER, INFO, UPDATE
Content-Type: application/sdp
Content-Length: [len]

v=0
o=sipp 1 1 IN IP4 [local_ip]
s=sipp
c=IN IP4 [local_ip]
t=0 0
m=audio [media_port] RTP/AVP 0 8
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=sendrecv
  ]]></send>

</scenario>
```

```bash
sipp -sf /sc/reg_uas.xml -i "$(hostname -i | awk '{print $1}')" -p 5060 -t u1 \
     -m 999999 -l 999 -rtp_echo -trace_err -nostdin
# /sc/reg_uas.xml = 把 assets/reg_uas.xml 拷进容器后的路径；两者内容一致，直接用 assets/ 那份即可
```

要点：
- **`-rtp_echo` 必须加**，否则只有信令通、没有双向媒体（FS 侧看不到 codec 起来）。
- **不要加 `-aa`**（`-sf` 配 `-aa` 会秒退）；OPTIONS 已由正则覆盖。
- 对 ACK 回 200 OK 协议上多余，但 FS 会当"无匹配事务"丢弃，**实测无害**，且不加会因 ACK 不匹配而让场景退出。
- `[last_To:]` 后的 `;tag=...` 同时满足 REGISTER 与 INVITE 的 200 OK；FS 侧两者都接受。
- 若镜像 `ENTRYPOINT` 写死了别的命令（如 `start-stub.sh`），必须 `--entrypoint sipp`（或 compose 覆盖 entrypoint）才能让自己的参数生效——否则你以为在跑场景，其实跑的是镜像内置场景。

### 验证（判据）

```bash
# 1) 注册是否真的成立
fs_cli -x "sofia status gateway <gw-name>"      # State=REGED / Status=UP / Expires=<你下发的值>
# 2) 同一个实例能否接呼叫（不必走 dialplan，直接 originate 到该网关）
fs_cli -x "originate {origination_uuid=t1,ignore_early_media=true}sofia/gateway/<gw-name>/9001 &echo()"
fs_cli -x "show channels"                        # callstate=ACTIVE / read_codec=PCMU / write_codec=PCMU
```

实测（sipp 3.6.1 + FS 1.11.2）：`State REGED / Status UP / Expires 600` 与
`callstate=ACTIVE / PCMU 双向` 在**同一进程上同时成立**。

## 怎么验证桩真的会答 OPTIONS（别等 FS 来 ping）

**先认清一件事：FS 默认不会给网关发 OPTIONS。** `sofia status gateway <name>` 里
`Ping 0 / PingFreq 0 / PingState 0/0/0` = **ping 处于关闭状态**（要下发 `<param name="ping" value="30"/>`
或开 profile 的 `all-reg-options-ping` 才会发）。所以「FS 侧看不到 OPTIONS 交互」通常是没开 ping，不是桩不答。

想直接证明桩会答，用**裸 UDP 探针**最快（不依赖任何 SIP 库，也排除"探针自身有 bug"的误判）：

```python
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(6)
s.sendto(("OPTIONS sip:<stub-host>:5060 SIP/2.0\r\n"
          "Via: SIP/2.0/UDP 0.0.0.0:59931;branch=z9hG4bKprobe1;rport\r\n"
          "Max-Forwards: 70\r\n"
          "From: <sip:probe@probe>;tag=probe1\r\n"
          "To: <sip:<stub-host>:5060>\r\n"
          "Call-ID: probe1@probe\r\nCSeq: 1 OPTIONS\r\n"
          "Contact: <sip:probe@0.0.0.0:59931>\r\nContent-Length: 0\r\n\r\n").encode(),
         ("<stub-host>", 5060))
print(s.recvfrom(65535)[0].decode())   # 期望首行 SIP/2.0 200 OK
```

在容器化环境里跑探针要挂到**同一个 docker 网络**：
`docker run --rm --network <compose>_sipnet --entrypoint python3 <一个带 python 的镜像> /p/probe.py`。

另一条线索：应用层的网关 OPTIONS 心跳（自发包探测）留下的 `heartbeat_fail_count` 很能说明历史——
若它是几十上百的累计失败数、而后来桩改好了，说明**那段时间桩确实丢弃了 OPTIONS**；把心跳打开一次即可验证现在的行为。
