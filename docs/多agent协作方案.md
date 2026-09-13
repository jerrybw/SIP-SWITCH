# 多 AI Agent 通信方案设计（v1.2）

> **版本**：v1.2（2026-09-13）　**修订**：WorkBuddy \<workbuddy@agent.local\>
> **本版定位**：**可据以开工的设计规格**。v1.1 的「附录 A 评审意见」已全部处置（见附录 A 对照表），本文档给出 MVP 的完整接口契约、数据结构、状态机、验收标准与环境约束。
> **实施方**：opencode \<opencode@agent.local\>（在 WSL dev 环境独立开发 + 验证）
> **验收方**：用户在宿主机浏览器验收（见 §9.3）

| 版本 | 日期 | 作者 | 变更 |
|------|------|------|------|
| v1.0 | 2026-09-12 | opencode | 初稿：轮询方案否决、WebSocket 复盘、三方案对比、Redis Stream 推荐 |
| v1.1 | 2026-09-12 | WorkBuddy | 附录 A 评审：消费组语义致命错误、4 处代码 bug、2 处产品化缺口 |
| **v1.2** | **2026-09-13** | **WorkBuddy** | **按附录 A 全面修订**：修正 A.1/A.2；新增桥接层（§5）与派发协议（§4）；裁剪范围为「先派发、后会议」；新增 Web 演示控制台（§7）、仓库结构（§8）、验收标准（§9）、环境与权限（§10）、实施步骤（§11） |

---

## 0. 本次修订摘要（v1.1 → v1.2 具体改了什么）

| # | v1.1 的问题（附录 A） | v1.2 的处置 | 落点 |
|---|----------------------|------------|------|
| 1 | 🔴 所有 Agent 共用消费组 `agents` → 组内负载均衡，消息只投给一个消费者，**会议广播失效** | 改为**每 Agent 独立消费组** `cg:{agent_id}`，各自全量消费 + 独立 ACK + 断线可重读 | §3.1 §3.5 |
| 2 | 🟠 `Message(**msg_data)` 反序列化 → XADD 的字段全是 string，`ts`/`ack_by` 类型静默损坏 | 规定**显式序列化契约**：payload 走 JSON 字符串，类型字段显式转换；附 encode/decode 规范与自测点 | §3.3 |
| 3 | 🟠 SQLite 用 `WHERE id > ?` 且 id 是 uuid4 → 非单调，漏消息 | MVP 不用 SQLite 做主线；若保留方案 B，游标必须用**自增 rowid** 或 `(ts, seq)` | §3.6 §12 |
| 4 | 🟠 `KEYS agent:alive:*` → O(N) 阻塞 | 改为 **SET + 成员级 TTL**，遍历用 `SSCAN`；禁止 `KEYS` | §3.4 |
| 5 | 🟠 `fcntl.flock(conn.fileno())` 与 WAL 锁冗余、Windows 分支语义粗糙 | 方案 B 中**删除显式文件锁**，依赖 `busy_timeout` | §12 |
| 6 | 🟡 **桥接层零覆盖**（最难、护城河） | 新增 §5：进程模型 / SessionAdapter / Executor SPI / 产物回传 / 崩溃回收 | §5 |
| 7 | 🟡 **派发协议缺失**（主诉求） | 新增 §4：`TASK_*` 六类消息 + 任务状态机 + 任务 Hash + 幂等 + 超时 | §4 |
| 8 | MVP 范围过重（会议协议先做） | **裁剪**：MVP 只做「派发回路」；会议/投票降为 P2 并保留接口 | §1.1 §6 |
| 9 | 验收不可见（用户无法体验） | 新增 **Web 演示控制台**（§7）+ 宿主机人工验收步骤（§9.3） | §7 §9 |

---

## 1. 范围与关键决策

### 1.1 MVP 边界（**这一节决定工作量，不要扩范围**）

| | 内容 |
|---|---|
| ✅ **做** | ① 总线层（可靠投递 + 在线感知 + 独立消费组）② 任务派发协议（状态机 + 幂等 + 超时）③ 桥接层（Executor SPI + 会话型 Agent 接入 + 产物回传）④ 两个可运行的 Agent（主/子）⑤ Web 演示控制台（浏览器可视）⑥ 单元测试 + 端到端测试 + 一条命令起 demo |
| ❌ **不做（P2 或更后）** | 会议/投票协议（§6 仅留接口）、跨机部署、认证鉴权/mTLS、Pub/Sub 低延迟双通道、多副本高可用、生产运维（监控/告警）、SQLite 后端（方案 B） |

> **判定标准**：若某个想法不影响「派发一个任务 → 子 Agent 执行 → 回传结果 → 主 Agent 验收 → 浏览器可见」这条主链，就**不做**。

### 1.2 关键决策（已拍板，实施时不要再纠结）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 通信语义 | **每 Agent 独立消费组** `cg:{agent_id}`（XREADGROUP 全量消费） | 修正 A.1。保留 Stream 持久化/断线重读/回放，同时实现"广播"语义 |
| 传输通道 | MVP **只用 Redis Stream**（阻塞 XREAD），不引入 Pub/Sub 双通道 | `BLOCK` 有消息立即可返回，延迟足够；减少一套一致性负担。Pub/Sub 留 P2 |
| 序列化 | **字段级显式契约**：`data` 存 JSON 字符串，`ts`/`attempt` 等显式转型 | 修正 A.2，杜绝类型静默损坏 |
| 在线感知 | Redis `SET`（成员=agent_id）+ 每成员 `String` 带 TTL 心跳；遍历用 `SSCAN` | 修正 A.2，避免 `KEYS` |
| 任务存储 | Redis `Hash` `task:{task_id}` + 全局索引 `SET task:index` | 单点可查、便于控制台列表 |
| 幂等 | 执行幂等（本地锁 `task:{id}:execlock`）+ 结果幂等（`SETNX task:{id}:result`） | 重投/重派不产生双执行、双结果 |
| Web 技术栈 | **首选 FastAPI + uvicorn + SSE**；装不上则降级标准库 `http.server` 手写 SSE | 必须有"用户可见"的体验入口（§7） |
| 交付形态 | `demo/start_demo.sh` 一条命令起全部；README 5 分钟上手 | 宿主机可验（§9.3） |
| 协议位置 | 全部下沉到 `agent_comm/` 代码，**绝不写进提示词** | v1.0 的核心原则，保留 |

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│  浏览器（宿主机 Windows）  http://localhost:18080                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  Web 控制台：Agent 在线徽章 / 任务看板 / 实时事件流 / 派发表单      │  │
│  └───────────────┬────────────────────────────┬───────────────────────┘  │
└──────────────────┼────────────────────────────┼──────────────────────────┘
                   │ HTTP + SSE                 │ 提交任务 / 验收
                   ▼                            │
        ┌────────────────────────┐              │
        │  webapp.py (port 18080)│──────────────┘
        └───────────┬────────────┘
                    │ 读写 Redis（同进程也可直接调 protocol）
┌───────────────────┼──────────────────────────────────────────────────────┐
│                   ▼                                                      │
│            ┌─────────────┐    Stream: bus:stream / event:log            │
│            │   Redis 7   │    Hash:  task:{id}                          │
│            │ (6390)      │    Set:   agent:alive / task:index            │
│            └──────┬──────┘                                               │
│                   │ XREADGROUP cg:{agent}                                │
│      ┌────────────┴─────────────┐                                        │
│      ▼                          ▼                                        │
│ ┌──────────────┐          ┌──────────────┐      ┌─────────────────────┐  │
│ │ agent-main   │          │ agent-worker │      │ runner.py（常驻）    │  │
│ │ runner.py    │          │ runner.py    │◀─────│ 心跳 / 重连 / 回收   │  │
│ │ (主：派发+验收)│        │ (子：执行+回报)│      └─────────────────────┘  │
│ └──────┬───────┘          └──────┬───────┘                               │
│        │                         │ Executor SPI                          │
│        │                         ▼                                       │
│        │                  ┌──────────────┐   ┌────────────────────────┐  │
│        │                  │ EchoExecutor │   │ ShellExecutor /        │  │
│        │                  │ (MVP 必做)   │   │ SessionAdapter(可选 CLI)│  │
│        │                  └──────────────┘   └────────────────────────┘  │
│        │                                                                 │
│        └──────── artifacts/{task_id}/…  ◀── 产物落盘，控制台可查看/下载    │
└──────────────────────────────────────────────────────────────────────────┘
```

**三层职责（严格分离）**

| 层 | 文件 | 只管什么 | 不管什么 |
|---|---|---|---|
| 总线层 | `bus.py` | 可靠投递、在线感知、消费组、重读 | 任务语义、业务规则 |
| 协议层 | `protocol_task.py` | 任务状态机、幂等、超时、验收闭环 | 具体怎么执行任务 |
| 执行层 | `executors.py` / `adapters.py` | 真正干活、产出产物 | 消息怎么传 |
| 展示层 | `webapp.py` | 可视化 + 人工派发/验收 | 直接改状态（必须走协议层 API） |

---

## 3. 总线层规格（`agent_comm/bus.py`）

### 3.1 投递语义（**修正 A.1 的核心**）

- 每个 Agent 使用**自己的消费组**：`cg:{agent_id}`，consumer name = `{agent_id}`。
- 首次运行时 `XGROUP CREATE bus:stream cg:{agent_id} $ MKSTREAM`（`$` = 只看新消息；需要回放时用 `0`）。
- 读取用 `XREADGROUP GROUP cg:{agent_id} {agent_id} COUNT 50 BLOCK 5000 STREAMS bus:stream >`。
- 处理成功后 `XACK`。**未 ACK 的消息留在 PEL**，崩溃重启后用 `XPENDING` + `XCLAIM`（或 `XREADGROUP ... 0`）重读。
- **禁止**所有 Agent 共用一个消费组——那会让消息只投给组内一个消费者（v1.0 的致命错误）。

> **自测点（必写）**：两个 Agent 同时订阅，A 发 1 条消息，**B 和 C 都必须收到**（断言各自 handler 被调用 1 次）。

### 3.2 Redis 数据结构（Key 设计表）

| Key | 类型 | 用途 | 备注 |
|---|---|---|---|
| `bus:stream` | Stream | 主消息流 | 建议 `XADD ... MAXLEN ~ 10000` 防爆 |
| `event:log` | Stream | 只读事件日志（给控制台回放） | 可由 bus 统一写，`MAXLEN ~ 2000` |
| `agent:alive` | Set | 在线 Agent 集合 | 成员 = agent_id |
| `agent:alive:{agent_id}` | String(TTL) | 心跳（TTL = 3 × interval） | 过期即离线 |
| `task:{task_id}` | Hash | 任务全量状态 | 字段见 §4.4 |
| `task:index` | Set | 全部 task_id（控制台列表用） | 可选按时间排序的 ZSet |
| `task:{task_id}:execlock` | String(TTL) | 执行幂等锁 | `SET NX EX` |
| `task:{task_id}:result` | String | 结果幂等（首次为准） | `SETNX` |

### 3.3 序列化契约（**修正 A.2**）

XADD 只能存字符串，因此**必须显式约定**，禁止 `Message(**fields)`：

```
Stream entry fields（全部为 string）:
  v      = "1"                        # 协议版本，便于未来演进
  type   = "task.assign"              # 消息类型（见 §4.1）
  from   = "agent-main"               # 发送方 agent_id
  ts     = "1757750000.123"           # 浮点文本（仅作展示，排序不用它）
  data   = '{"task_id":"...","...":...}'   # 业务负载，JSON 字符串
```

解码规则（必须显式）：
- `ts` → `float(fields["ts"])`
- `data` → `json.loads(fields["data"])`
- 缺失字段 → 抛 `MalformedMessage`，**不要让异常冒到主循环**（记录 + 跳过 + 计数）

> **自测点（必写）**：发一条含 `{"percent": 50, "ok": true}` 的 payload，收端断言 `type(payload["percent"]) is int`、`payload["ok"] is True`（不是字符串）。

### 3.4 在线感知（**修正 A.2 的 KEYS**）

```
心跳：SET agent:alive:{id} <json> EX {3*interval}   +  SADD agent:alive {id}
在线：SMEMBERS/SSCAN agent:alive，对每个成员 EXISTS agent:alive:{id} 判活，
      不存在的 SREM（惰性清理）
```
- **禁止 `KEYS`**（O(N) 阻塞）。需要遍历一律 `SSCAN`/`SMEMBERS`（集合通常只有个位数成员）。

### 3.5 API 契约（函数签名级）

```python
class Bus:
    def __init__(self, agent_id: str, redis_url: str = "redis://127.0.0.1:6390/0"): ...
    # 发送
    def publish(self, msg_type: str, payload: dict) -> str:          # 返回 msg_id
    # 订阅
    def on(self, msg_type: str, handler: Callable[[Msg], None]) -> None: ...
    def listen_forever(self, block_ms: int = 5000) -> None:          # 阻塞循环，内部捕获异常不退出
    def drain_pending(self) -> int:                                  # 处理崩溃遗留的未 ACK 消息，返回条数
    def stop(self) -> None
    # 在线
    def heartbeat_loop(self, interval: int = 5) -> None              # 独立线程
    def online_agents(self) -> list[str]
    # 任务态（协议层会用到，也可单独建 taskstore.py）
    def task_hset(self, task_id: str, mapping: dict) -> None
    def task_hgetall(self, task_id: str) -> dict
```

`Msg` 为 dataclass：`msg_id / type / from_agent / ts / data(dict) / raw(dict)`。

### 3.6 必须避免的反模式（评审结论，写进代码注释）

| ❌ 反模式 | ✅ 正确做法 |
|---|---|
| 所有 Agent 共用一个消费组 | 每 Agent `cg:{agent_id}` |
| `Message(**stream_fields)` | 显式 decode（§3.3） |
| `KEYS agent:alive:*` | `agent:alive` SET + `SSCAN` |
| 用本地时钟排序消息 | 用 Redis stream ID（`XADD` 生成）排序 |
| 用 uuid 做 SQLite 游标 | 自增 rowid / `(ts, seq)`（本版不用 SQLite 主线） |
| 协议规则写在提示词里 | 规则写进 `protocol_task.py` |
| 主循环裸 `while True` 无异常兜底 | 捕获 + 退避重连（`retry_on_timeout`、指数退避上限 5s） |

---

## 4. 任务派发协议（`agent_comm/protocol_task.py`）

> **拓扑是 hub-spoke**（主 → 子 → 主），与会议（mesh）**不是同一套协议**，不要混在一起实现。

### 4.1 消息类型（`MsgType` 枚举）

| 类型字符串 | 方向 | 含义 |
|---|---|---|
| `task.assign` | 主 → 子 | 派发任务 |
| `task.accept` | 子 → 主 | 受理（含是否接受、预计耗时） |
| `task.progress` | 子 → 主 | 进度心跳（运行期间周期发） |
| `task.result` | 子 → 主 | 执行结果（成功/失败 + 产物引用） |
| `task.verify` | 主 → 子（广播） | 验收结论（通过/驳回，驳回带原因） |
| `task.cancel` | 主 → 子 | 撤销任务（可选，MVP 可先不做） |

（会议类 `speak/propose/vote/heartbeat/join/leave` 保留枚举定义，MVP 不实现，见 §6。）

### 4.2 状态机

```
                    主:task.assign
                          │
                          ▼
   ┌───────────┐      ┌──────────┐   子:accept(ok)   ┌─────────┐
   │  PENDING  │─────▶│ ASSIGNED │──────────────────▶│ RUNNING │
   └───────────┘      └────┬─────┘                   └────┬────┘
        ▲                  │ 子:accept(no)/超时            │ progress×N
        │                  ▼                              ▼
        │             ┌─────────┐                  ┌────────────┐
        │ 重派(≤3)     │ REJECTED│                  │ SUCCEEDED  │
        └──────────────┴────┬────┘                  │  / FAILED  │
                            │                       └─────┬──────┘
                            │                             │ 主:verify
                            ▼                             ▼
                      ┌──────────┐                 ┌──────────────┐
                      │ ABANDONED│                 │ VERIFIED(终) │
                      └──────────┘                 │  / REJECTED  │──┐
                                                   └──────────────┘  │ 驳回→重派(attempt+1)
                                                                     └──▶ ASSIGNED
   任意状态：主侧 watchdog 发现 now > deadline → TIMEOUT（终态或重派）
```

**状态取值**（存 `task:{id}.status`）：`PENDING / ASSIGNED / RUNNING / SUCCEEDED / FAILED / VERIFIED / REJECTED / TIMEOUT / ABANDONED`

### 4.3 消息字段定义（逐字段，实施照抄）

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | str | 主 Agent 生成，`uuid4` 字符串。**全局唯一，重派时沿用**（便于追踪 attempt） |
| `title` | str | 任务短标题（控制台展示） |
| `spec` | dict | 任务规格（执行器自定义；如 `{"kind":"echo","text":"..."}` 或 `{"kind":"shell","cmd":"python3 -c ..."}`） |
| `from` / `to` | str | agent_id；`to` 为具体子 Agent，MVP 不做广播派发 |
| `deadline` | float(epoch) | 绝对超时时刻（主 Agent 定：`now + timeout_s`） |
| `attempt` | int | 第几次派发（从 1 起），每次重派 +1 |
| `status` | str | 见 §4.2 状态取值 |
| `result` | dict | 子 Agent 返回的结构化结果（执行器定义） |
| `artifact_ref` | str | 产物引用，相对路径 `artifacts/{task_id}/{filename}` |
| `reason` | str | 驳回/失败原因 |
| `percent` | int | progress 用，0–100 |
| `note` | str | progress 用，人类可读进展 |
| `duration_ms` | int | 子 Agent 记录的执行耗时 |

### 4.4 任务 Hash 字段（`task:{task_id}`）

```
task_id, title, status, from, to, attempt, created_at, updated_at,
deadline, spec(=JSON), result(=JSON), artifact_ref, reason, log_tail
```

### 4.5 幂等与重试规则（**必须实现，端到端测试要覆盖**）

1. **执行幂等**：子 Agent 收到 `task.assign` 先 `SET task:{id}:execlock NX EX 3600`
   - 获取失败 → 说明已在执行/已执行 → 只回一条 `task.accept{accepted:true}`，**不重复执行**。
2. **结果幂等**：回传结果前 `SETNX task:{id}:result <json>`；已存在则**不再覆盖**（首次为准），但仍回 ACK。
3. **重派**：主 Agent 收到 `REJECTED` 或 `TIMEOUT` 时可重派，`attempt += 1`，**task_id 不变**；`attempt > 3` → `ABANDONED`（终态）并记录原因。
4. **超时 watchdog**：主 Agent 每 2s `SSCAN task:index` → 取 `status ∈ {ASSIGNED,RUNNING}` 且 `now > deadline` 的任务 → 标 `TIMEOUT`（然后按策略重派或放弃）。
5. **进度心跳**：子 Agent 运行期间每 ≤5s 发 `task.progress`，主 Agent 据此刷新 `updated_at`（避免误判超时）。

### 4.6 验收闭环（主 Agent 侧）

- 收到 `task.result` → 调用 **Verifier**（可插拔）
  - MVP 内置 `AssertVerifier`：按 `spec.expect` 断言（如 `{"key":"sum","eq":5050}`）
  - 也可由人在 Web 控制台点 **VERIFY / REJECT**（人工兜底）
- 通过 → `task.verify{verdict:"verified"}` + 状态 `VERIFIED`（终态）
- 驳回 → `task.verify{verdict:"rejected", reason:...}`，并按 §4.5.3 决定是否重派

### 4.7 协议层 API 契约

```python
class TaskProtocol:
    def __init__(self, bus: Bus, role: str, agent_id: str, executor=None, verifier=None): ...
    # 主侧
    def assign(self, to: str, title: str, spec: dict, timeout_s: int = 120) -> str:  # -> task_id
    def verify(self, task_id: str) -> None         # 触发（人工/自动）验收
    def tasks(self, status: str | None = None) -> list[dict]
    # 子侧
    def start_worker(self) -> None                 # 注册 handler，开始接活
```

---

## 5. 桥接层（`agent_comm/runner.py` + `executors.py` + `adapters.py`）

> **这是护城河，也是 v1.0 完全没覆盖的部分（A.3）。** 总线用 Redis 还是别的 MQ 是可替换商品；"让异构会话型 Agent 像进程一样被寻址/唤醒/回收"才是自研价值。

### 5.1 进程模型

每个 Agent = 一个常驻 runner 进程：

```
python -m agent_comm.runner --id agent-worker --role worker
python -m agent_comm.runner --id agent-main   --role main
```

runner 内部：
- 主线程：`bus.listen_forever()`（阻塞收消息）
- 后台线程：`heartbeat_loop(5s)`
- 收到 `task.assign` → 交给 **Executor** 执行（**在线程池里跑，不要阻塞监听循环**，否则收不到新消息/心跳停摆）
- 退出：捕获 `SIGTERM` 优雅停（`bus.stop()`）

> 注意：**不是** `while True: schedule.run_pending()` 那种 daemon 假想（v1.0 示例的问题）。

### 5.2 Executor SPI（可插拔执行器）

```python
class Executor(Protocol):
    def execute(self, task: dict, ctx: dict) -> dict:
        """返回 {"status":"ok|error", "result":{...}, "log_tail":str}"""
```

MVP 必做两个：

| 执行器 | `spec.kind` | 行为 | 用途 |
|---|---|---|---|
| `EchoExecutor` | `echo` | 原样返回 `spec.text` + 时间戳 + runner 标识 | 确定性，端到端测试用 |
| `ShellExecutor` | `shell` | 在**白名单**内执行 `spec.cmd`，捕获 stdout/stderr/退出码 | "真的干了活"的观感 |

`ShellExecutor` 约束（安全，务必遵守）：
- 只允许 `python3` / `echo` / `cat` / `ls` 开头的命令（白名单前缀匹配）
- 工作目录固定为 `artifacts/{task_id}/`
- 超时 kill（默认 60s）
- 输出截断（stdout ≤ 64KB，日志尾部 ≤ 4KB）

### 5.3 会话型 Agent 适配（SessionAdapter）

抽象"把一个任务交给某个会话型 Agent 跑，并拿回结果"：

```python
class SessionAdapter(Protocol):
    def run(self, prompt: str, timeout_s: int) -> dict:   # {"status","output","exit_code"}
```

| 实现 | 说明 | MVP |
|---|---|---|
| `MockSessionAdapter` | 不调外部 CLI，按规则生成确定性输出（模拟"会话型 Agent 思考后给答案"） | ✅ 必做 |
| `CliSessionAdapter` | `subprocess.Popen([cli, "run", prompt])` + 超时 kill + 输出解析 | 可选：环境里若有可用 CLI 再接 |

> 环境里若没有可用的第三方 Agent CLI，**不要卡住**——`MockSessionAdapter` 即可满足验收；`CliSessionAdapter` 留好接口和注释说明如何接。

### 5.4 产物回传（artifact）

- 执行器把产物写入 `artifacts/{task_id}/`
- 消息里只传 **相对引用** `artifact_ref = "artifacts/{task_id}/result.txt"`（不传大内容）
- Web 控制台通过 `GET /api/artifacts/{task_id}/{name}` 提供查看/下载
- `result` 里放结构化摘要（小、可断言），大文本一律进 artifact

### 5.5 崩溃回收

- runner 崩溃 → 未 ACK 的消息在 PEL → 重启后 `drain_pending()` 重读
- 执行中崩溃 → `execlock` 的 TTL 到期后可被重派（或主 Agent watchdog 超时重派）
- 每次状态变更都 `updated_at`，控制台可显示"最后心跳距今 xx 秒"

---

## 6. 会议协议（P2 —— 本版**只保留接口**，不实现）

保留 `MsgType` 中 `speak/propose/vote/sync/join/leave` 的定义与 §3.4 的状态机设计（见 v1.1 原文），但：

- MVP 不写 `meeting_protocol.py` 的实现（或只放空壳 + `NotImplementedError`）
- 理由：会议是 mesh 拓扑、协议复杂、且**不是当前主诉求**；派发回路跑通后再作为增量协议开发
- 若时间允许，可作为 P3 加分项实现「三人会议 + 一轮投票」的 demo 页签

---

## 7. Web 演示控制台（`agent_comm/webapp.py`）—— 用户验收入口

### 7.1 页面（单页，浏览器可开，深色简洁风）

| 区域 | 内容 |
|---|---|
| 顶部栏 | 标题 + **在线 Agent 徽章**（agent-main / agent-worker，绿=在线灰=离线）+ 刷新时间 |
| 左栏 | **派发表单**：目标 Agent（下拉）、标题、任务类型（echo/shell）、内容/命令、超时秒数、`[派发]` 按钮 |
| 中栏 | **实时事件流**（SSE）：`时间 | 类型 | from → to | 摘要`，倒序滚动，可按 task_id 过滤 |
| 右栏 | **任务看板**：每任务一张卡（标题、状态色块、attempt、耗时、产物链接、`[通过]`/`[驳回]` 按钮） |
| 底部 | 最近一次执行输出（stdout 尾部）与产物预览（文本直接显示） |

状态色：`PENDING 灰 / ASSIGNED 蓝 / RUNNING 青 / SUCCEEDED 绿 / VERIFIED 深绿 / FAILED 红 / REJECTED 橙 / TIMEOUT 紫`

### 7.2 后端接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 单页 HTML（内联 CSS/JS，无构建步骤） |
| GET | `/api/agents` | 在线 Agent 列表 |
| GET | `/api/tasks` | 任务列表（可按 status 过滤） |
| GET | `/api/tasks/{id}` | 任务详情（含 result / artifact_ref / log_tail） |
| POST | `/api/tasks` | 派发任务（body: to/title/spec/timeout_s）→ 返回 task_id |
| POST | `/api/tasks/{id}/verify` | body: `{"verdict":"verified"\|"rejected","reason":...}` |
| GET | `/api/stream` | **SSE** 实时事件流（读 `event:log`，`XREAD BLOCK`） |
| GET | `/api/artifacts/{task_id}/{name}` | 产物下载/预览（**必须做路径穿越校验**：只允许 `artifacts/{task_id}/` 下的文件） |

### 7.3 实时推送实现

SSE 循环：`XREAD BLOCK 5000 STREAMS event:log {last_id}` → `data: {json}\n\n`；每 15s 发一次心跳注释 `: keepalive`。断开自动重连（浏览器 EventSource 原生支持）。

### 7.4 启动与端口

- 监听 **`0.0.0.0:18080`**（端口已确认空闲）
- 宿主机访问：`http://localhost:18080`（WSL2 端口转发）或 `http://<WSL_HOST_IP>:18080`
- 后台常驻：`nohup python3 -m agent_comm.webapp > logs/webapp.log 2>&1 &`
  （**注意**：SSH 断开后进程需存活，用 `nohup`/`setsid`；不要占用前台会话）

---

## 8. 仓库结构与交付物清单

工作根：**`/home/opencode/work/multi-agent-comm/`**（你有完整写权限）

```
multi-agent-comm/
├── DESIGN.md                 # 本文档（开工依据，可补充但不删节）
├── README.md                 # ★必做★ 5 分钟上手：怎么起、怎么看、怎么验（含截图/示例输出）
├── pyproject.toml            # package: agent-comm, deps: redis>=5
├── agent_comm/
│   ├── __init__.py           # 导出 Bus / TaskProtocol
│   ├── bus.py                # §3 总线层
│   ├── protocol_task.py      # §4 派发协议
│   ├── runner.py             # §5.1 Agent 常驻进程（CLI 入口）
│   ├── executors.py          # §5.2 Echo / Shell 执行器
│   ├── adapters.py           # §5.3 Mock / Cli 会话适配器
│   └── webapp.py             # §7 控制台（FastAPI 优先，标准库降级）
├── demo/
│   ├── start_demo.sh         # ★一条命令★ 起 redis + main + worker + webapp
│   └── stop_demo.sh          # 干净停掉全部
├── tests/
│   ├── test_bus_broadcast.py # ★关键★ 证明"两个 Agent 都收到同一条消息"（A.1 回归）
│   ├── test_serde.py         # ★关键★ 类型不被字符串化（A.2 回归）
│   ├── test_protocol_task.py # 状态机 / 幂等 / 超时 / 重派
│   └── test_e2e_dispatch.py  # ★端到端★ 派发→执行→结果→验收（Mock redis 或真 redis）
├── artifacts/                # 产物目录（.gitignore）
├── logs/                     # 运行日志（.gitignore）
└── ACCEPTANCE.md             # ★验收报告★ 你自测的结论 + 怎么复现 + 已知限制
```

**必须交付**：上面全部（`ACCEPTANCE.md` 是第一人称验收报告，列出每项验收命令与实际输出）。

---

## 9. 验收标准（Definition of Done）

### 9.1 自动化测试（自证）

| 编号 | 用例 | 通过标准 |
|---|---|---|
| T1 | 广播语义 | 两 Agent 订阅同一 stream，A 发 1 条，B、C **都**收到（各 1 次） |
| T2 | 序列化 | payload 含 int/bool/list，收端类型完全一致 |
| T3 | 在线感知 | 心跳后 `online_agents()` 含自己；TTL 过期后消失 |
| T4 | 派发主链 | assign → accept → progress → result → verify，终态 `VERIFIED` |
| T5 | 执行幂等 | 同一 `task.assign` 连发 3 次，执行器**只执行 1 次** |
| T6 | 结果幂等 | 同一 task 回传结果 2 次，`task:{id}:result` 保持首次值 |
| T7 | 超时 | 子 Agent 故意不回，主侧 watchdog 在 deadline+2s 内标 `TIMEOUT` |
| T8 | 重派 | 驳回后 `attempt` 递增且 `task_id` 不变；超过 3 次进 `ABANDONED` |
| T9 | 崩溃回收 | kill worker 后再起，PEL 中的未 ACK 消息被重新处理（不丢） |
| T10 | 路径安全 | `/api/artifacts/../../etc/passwd` 返回 400/404 |

### 9.2 端到端脚本

```bash
cd /home/opencode/work/multi-agent-comm
bash demo/start_demo.sh          # 起 redis + main + worker + webapp
python3 -m pytest tests/ -v      # 全绿（含 T1~T10）
python3 demo/run_acceptance.py   # 输出 PASS/FAIL 摘要表（自证）
bash demo/stop_demo.sh
```

### 9.3 宿主侧人工验收（**用户视角，5 步**）

> 把这份步骤写进 `README.md` 顶部，让用户照着点。

| 步 | 操作 | 预期 |
|---|---|---|
| 1 | 你在 WSL 里跑 `bash demo/start_demo.sh` | 终端打印 4 项就绪 + 一行 `Open http://localhost:18080` |
| 2 | 宿主机浏览器打开 `http://localhost:18080` | 页面加载，顶部 **agent-main / agent-worker 两个绿点** |
| 3 | 表单填：目标 `agent-worker`、类型 `echo`、内容 `hello from host`、超时 60 → 点【派发】 | 中栏**实时**依次出现 `task.assign → task.accept → task.progress → task.result`（无需刷新） |
| 4 | 右栏该任务卡变绿（`SUCCEEDED`），点【通过】 | 卡片转深绿 `VERIFIED`，事件流出现 `task.verify` |
| 5 | 再跑一次：类型选 `shell`、命令 `python3 -c "print(6*7)"` | 底部输出区显示 `42`，产物可下载 |

**只要第 3 步的事件是"实时滚动出现"而不是要手动刷新，就证明总线 + 桥接 + 推送三层都通了。**

### 9.4 反例（不通过）

- 页面打开是空列表 / 需要刷新才更新 → 不通过
- 派发后子 Agent 没反应、日志有异常 → 不通过
- 只能跑 echo、shell 一运行就报错 → 不通过
- 两个 Agent 只有一个能收到消息 → **A.1 回归，不通过**

---

## 10. 环境与权限（WSL dev —— 你的沙箱边界）

### 10.1 你能用的

| 项 | 值 |
|---|---|
| 登录 | `ssh opencode@localhost -p 22022`（**密钥登录**，私钥由用户持有并转交给你） |
| 工作根（**可写**） | `/home/opencode/work/`（项目就建在 `work/multi-agent-comm/`） |
| 只读参考 | `/home/opencode/SIP-SWITCH`（软链到源码，**仅供参考写法，禁止改动**） |
| Redis | `redis://127.0.0.1:6390/0`（**无密码**，已就绪并连通） |
| RedIS 容器控制 | `sudo opencode-stack {ps\|up\|down\|start\|stop\|restart\|logs [svc]}` |
| Python | `python3` = 3.10.12；`pip install --user <pkg>` 可用（已验证可装 redis-py 8.1.0） |
| Web 端口 | **18080**（空闲，监听 `0.0.0.0` 后宿主机可访问） |

### 10.2 你不能做的（**红线，违反会破坏他人环境**）

| ❌ 禁止 | 原因 |
|---|---|
| 写/删 `/root/**`、`/home/zcode/**`、`/root/src/SIP-SWITCH/**` | 这些是别人的工作区；你只有**只读**权限。**此前发生过误删 `/root/src` 下文件的事故**，务必只在 `/home/opencode/work` 内写文件 |
| 直接用 `docker` 命令（`docker ps/run/rm`…） | 你无 docker 组权限；只能通过 `opencode-stack` 白名单脚本 |
| 停/删公用栈 `sip-switch`、zcode 栈 `zstack` 的容器与卷 | 会打断他人工作 |
| 占用已在用的端口 | 已占：5060/5080/6060/6080/8000/8001/9000/15060/15061/6390/20000-20100/21000-21100/22000-22100(RTP)。**你只用 18080** |
| 把任何凭据（密码/密钥/token）写进代码、文档或提交 | 仓库可能公开 |
| 改 sshd、sudoers、系统配置 | 无权限，也不需要 |
| 用 `sudo` 做白名单之外的任何事 | 会被拒绝（设计如此） |

### 10.3 基础设施

```bash
sudo opencode-stack up        # 起 redis（幂等）
sudo opencode-stack ps        # 查看容器状态
sudo opencode-stack logs redis
# 连接：redis://127.0.0.1:6390
```

### 10.4 交付位置建议

- 代码：`/home/opencode/work/multi-agent-comm/`
- 运行日志：`/home/opencode/work/multi-agent-comm/logs/`
- 把 `README.md` / `ACCEPTANCE.md` 写成用户能直接照着做的样子

---

## 11. 实施步骤（分阶段 + 每阶段验证点）

| 阶段 | 内容 | 完成标志（自测） |
|---|---|---|
| **P0 总线** | `bus.py`（含 A.1/A.2 修正）+ `test_bus_broadcast.py` + `test_serde.py` | T1/T2/T3 绿 |
| **P1 派发协议** | `protocol_task.py` + `executors.py`（echo/shell）+ `runner.py` + `test_protocol_task.py` | T4/T5/T6/T7/T8 绿 |
| **P2 端到端** | 主/子两个 runner 真实跑通 + `test_e2e_dispatch.py` + 崩溃回收 | T9 绿 + `run_acceptance.py` PASS |
| **P3 控制台** | `webapp.py`（页面 + SSE + 派发/验收 + 产物下载含路径校验） | T10 绿；浏览器第 3 步"实时滚动"成立 |
| **P4 交付打磨** | `start_demo.sh`/`stop_demo.sh`、`README.md`、`ACCEPTANCE.md` | §9.3 五步人人可复现 |

**建议每阶段结束就 commit 一次**（在 `multi-agent-comm/` 里 `git init` 即可，不要卷入其它仓库）。
**不要憋到最后一次性交付**；每阶段成果都写进 `ACCEPTANCE.md`。

---

## 12. 风险与未决问题

| 风险 | 应对 |
|---|---|
| FastAPI/uvicorn 装不上（网络受限） | 降级：标准库 `http.server` + 手写 SSE（页面不变，仍是单页内联 JS） |
| 18080 被占（例如别的进程） | 可用 18081；README 里写明如何改端口 |
| 环境无可用的第三方 Agent CLI | `MockSessionAdapter` 满足验收；`CliSessionAdapter` 只留接口 |
| Stream 无限增长 | `XADD MAXLEN ~ 10000`；`event:log` 单独 `MAXLEN ~ 2000` |
| 子 Agent 执行阻塞监听循环 | 执行必须放线程池/子进程（§5.1） |
| 中文注释/输出在 WSL 终端乱码 | 文件统一 UTF-8；终端可 `export LANG=C.UTF-8` |

**待评审（不阻塞开工，先按默认做，行不通再讨论）**：
1. 是否需要把 `TASK_ASSIGN` 支持"广播派发"（任一 worker 抢单）→ 当前设计为指定 `to`，MVP 不做抢单
2. 产物是否要打包 zip 下载 → MVP 只做单文件预览/下载
3. 会议协议是否要在本期做 → 默认不做（§6）

---

## 附录 A：v1.1 评审意见处置对照表

| 评审条目 | 处置 | 落点 |
|---|---|---|
| A.1 🔴 消费组语义致命错误 | **已修**：每 Agent 独立消费组 `cg:{agent_id}`，全量消费 + 独立 ACK | §3.1，回归测试 T1 |
| A.2 🟠 反序列化类型损坏 | **已修**：显式序列化契约 + 显式转型 | §3.3，回归测试 T2 |
| A.2 🟠 SQLite uuid 游标 | **已处置**：MVP 不用 SQLite 主线；若用必须自增 rowid | §3.6 §12 |
| A.2 🟠 KEYS 阻塞 | **已修**：SET + 成员 TTL + SSCAN | §3.4 |
| A.2 🟠 flock 冗余 | **已处置**：方案 B 删除显式锁，依赖 busy_timeout | §12 |
| A.3 🟡 桥接层缺失 | **已补全**：进程模型 / Executor SPI / SessionAdapter / 产物回传 / 崩溃回收 | §5（+ T9） |
| A.4 🟡 派发协议缺失 | **已补全**：6 类消息 + 状态机 + 任务 Hash + 幂等 + 超时 + 验收闭环 | §4（+ T4~T8） |
| A.5 ✅ MVP 路径 | **已采纳**：先派发回路，会议后置；一条命令起 demo + 宿主机 5 步验收 | §1.1 §9.3 §11 |
| A.6 ✅ 与现有资产衔接 | **已落地**：直接复用 dev 的独立 Redis（6390），零新增基础设施 | §10.3 |

---

> **给实施者（opencode）的最后一句话**：本方案的价值不在"又写了一个消息队列"，而在 **§5 桥接层**——让会话型 Agent 能被总线寻址、唤醒、回收。先把 §9 的 T1/T2 两个回归测试写出来（它们分别盯着 v1.0 的两个致命错误），再往上搭。有任何设计上说不通的地方，先记录在 `ACCEPTANCE.md` 的「设计反馈」一节，不要默默改设计。
