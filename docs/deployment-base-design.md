# SIP-SWITCH 部署底座设计（v0.2 · 已评审）

> 状态：**v0.2，六项待决已全部拍板**（2026-09-09）。本文只做设计，不含代码改动。
> 成文：2026-09-09。硬事实均来自 WSL `/root/src/SIP-SWITCH` 代码核对。
> 脱敏：文中不含真实 IP / 密码 / 实例 ID。
> 去向：**入仓 `docs/deployment-base-design.md`**（DEP-8）。
>
> **编号体系**：本文决策用 **`DEP-N`**（deployment），与项目历史决策 `D3`/`D4`/`D10`（fail-close、Redis 降级、文档放 tdrive 等，见 `docs/ROADMAP.md` 与项目记忆）**是两套编号**，勿混。

---

## 1. 已定决策

| # | 项 | 结论 |
|---|---|---|
| DEP-1 | 多 FS 模型 | **不做呼叫级路由分发**。各 FS 节点独立处理自己的 SIP 交互，仅保证「读配置 / 写数据」是多节点同一套出入口（VOS 式：按节点划分承载） |
| DEP-2 | Redis | **从部署底座起强制**，不做"可选降级" |
| DEP-3 | 录音默认形态 | **节点本地盘**；对象存储**本轮只预留接口**（DEP-7） |
| DEP-4 | 配置生成方式 | **`deploy.sh` 混合方案**（宿主机生成密钥 + 渲染配置；DB 迁移留给 gateway 启动时自做）—— 见 §6 |
| DEP-5 | 分片数据结构 | **独立关联表 `gateway_node`**（非加列），`uk_gateway` 保证单选 —— 见 §3.3 |
| DEP-6 | FS 节点级健康检查 | **纳入本轮**（不留 T-501）—— 见 §3.5 |
| DEP-7 | 对象存储录音 | **本轮只预留接口**，上传任务后置 —— 见 §5 |
| DEP-8 | 文档去向 | **入仓 `docs/`**，不沿用 D10 放 tdrive |
| DEP-9 | 落地节奏 | **分两阶段**：Phase 1「单机 + Redis」→ Phase 2「多节点」—— 见 §9 |

### 明确不兼容

**不支持用户自行预部署 FreeSWITCH、再由网关去管理。**
FS 必须是本部署体系拉起的（镜像由仓库构建），其配置由网关 xml_curl 端点下发。
理由：机制 A 下 sofia.conf / dialplan / directory 全部动态下发，预置 FS 的本地配置会与之冲突且无法保证一致。

---

## 2. 部署形态矩阵

| 形态 | FS | 网关 | MySQL | Redis | 录音 | 适用 |
|---|---|---|---|---|---|---|
| **A 单机全栈** | 1 | 1 | 容器 | 容器 | 本地盘 | dev / 原型 / 小规模 |
| **B 单机 + 云依赖** | 1 | 1 | 云数据库 | 云 Redis | 本地盘 | 生产单机 |
| **C 多节点** | N | M | 云或容器（单写） | 云或容器 | 各节点本地盘 | 扩容后 |
| **D 多节点 + 对象存储** | N | M | 云 | 云 | 对象存储 | 录音需集中 |

FS 与网关数量**解耦**：网关无状态可随意加副本；FS 按承载划分（见 §3）。

---

## 3. 多节点一致性模型（核心）

### 3.1 你的定义带来的直接推论

"各节点独立处理 SIP，但读写同一套出入口" 意味着：

- **出口统一**：所有 FS 节点经 xml_curl 从同一网关端点拉 `sofia.conf` / dialplan / directory
- **入口统一**：CDR 由网关经 ESL 落同一 DB；录音路径登记 `fs_node_uuid` 以便回溯

### 3.2 ⚠️ 必须解决：配置不能"全量下发"给每个节点

这是本模型的**首要矛盾**，不解决则多 FS 根本跑不起来：

> 若每个 FS 节点都拉到**全量**落地网关，则**每个节点都会向所有网关发起 REGISTER** —— 同一账号多点注册，运营商侧互踢、出局错乱。
> 接入点同理：话机注册到哪个节点必须是确定的，否则同一分机在 N 个节点上都在线。

**现行代码状态（实测）**：`src/fs_sofia_config.py:build_sofia_conf(db)` 拉取**全量** `gateway` 表生成 `<gateways>`，无节点维度过滤。`fs_node` 表已建（含 `node_uuid` / `host` / `esl_port` / `status`），但**仅 `esl_client.py:539` 传了一个单节点 uuid，无分发、无过滤逻辑**。

### 3.3 分片规则（2026-09-09 用户确认）

| 对象 | 规则 | 理由 |
|---|---|---|
| 接入点 `access_point` | **不分片**，全量下发所有节点 | IP 型匹配来源 IP，任意节点都要能认 |
| 话机 `sip_phone`（directory） | **不分片**，全量下发 | 话机归属由客户端指向的节点 IP 决定，非配置决定 |
| 落地网关 · **点对点**（`auth_type=0`） | **不分片**，全量下发 | 无注册状态，任何节点都能直接发 INVITE |
| 落地网关 · **注册模式**（`auth_type=1`） | **必须单选 `node_id`**，只下发给该节点 | 注册是节点行为，多点注册会互踢 |

**注册模式三要素唯一约束**：同一 `node_id` 下，`(ip/域名, port, 注册用户名)` 三元组唯一。

**数据结构：新增独立关联表**（不加列）

```sql
CREATE TABLE `gateway_node` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `gateway_id` bigint unsigned NOT NULL,
  `fs_node_id` bigint unsigned NOT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_gateway` (`gateway_id`),   -- 保证「单选」语义
  KEY `idx_node` (`fs_node_id`)
) COMMENT='落地网关 - FS 节点归属（仅注册模式需要）';
```

⚠️ `uk_gateway(gateway_id)` 是「单选」的**物理保证**：一个网关只能有一行归属。
将来若要多节点注册同一网关，删掉这个唯一键即可，表结构与代码无需改动 —— 这正是用关联表而非加列的价值。

**需要跟上的限制（四处）**：

| 处 | 限制 |
|---|---|
| DB | `gateway_node` 表 + `uk_gateway`；三要素唯一需**应用层校验**（跨表字段，非单表唯一索引能覆盖） |
| 网关 | ① 建/改 gateway 时：注册模式必须带 `node_id`；② 校验三要素在目标 node 下唯一；③ `/fs/config` 按 node 过滤（点对点全给 + 本节点注册的） |
| FS 侧 | `xml_curl.conf.xml` 的 `gateway-url` 带 `?node=<uuid>`，部署时按节点渲染（无需 FS 主动上报） |
| Web 前端 | 注册模式网关表单强制选 node；提交前提示三要素冲突；列表展示归属节点 |

**存量数据处理（已核实，2026-09-09）**：

dev 现有 `gw-carrier-a`（`id=7`，`ip=sipp-stub`，`port=5060`）实测 **`auth_type=0`（点对点）、`username=NULL`** → 属**全量下发**对象，**不需要补 `gateway_node` 记录**，多节点上线后不受影响。

⚠️ 但迁移脚本仍需写：上线时应扫描 `auth_type=1` 且无归属记录的网关 —— 这类网关在多节点下会**从所有节点消失**。当前 dev 库为 0 条，生产库需在上线前单独核查。

### 3.4 ⚠️ 由此产生的连锁问题：跨节点出局与 failover 范围

分片后必然出现：**话机注册在 FS-A，但最优落地网关只下发给了 FS-B** —— FS-A 上没有该网关定义，无法 `bridge sofia/gateway/<name>`。

| 方案 | 做法 | 代价 | failover 范围 |
|---|---|---|---|
| **A 节点间 SIP 中继**（VOS 做法） | FS-A 把呼叫以 SIP 送到 FS-B，由 FS-B 出局 | 中：需节点间互联配置 + 中继路由 | **全局**：可溢出到任意节点的网关 |
| **B 只选本节点可用网关**（当前倾向） | 选路时按 node 过滤候选 | **零**（本轮无新增组件） | **节点内**：只能用本节点的网关 |
| C 跨节点 ESL originate | FS-A 指挥 FS-B 发起出局腿 | 高：非标准、状态耦合 | 全局但不推荐 |

**已确认走 B**（用户 2026-09-09：「选取 gateway 的地方要增加对应 node_id 下能支持的 gateway 列表的筛选」）。

⚠️ **B 的代价必须知晓**：
- 注册型网关的话务**只能从其所属节点出**；该节点满或故障时，话务**不会自动溢出**到其他节点的同类网关
- 即 failover 与并发调度的粒度被限制在**节点内**（这是 VOS 式模型的固有特性，非缺陷）
- 缓解手段：同号段的注册型网关**成对部署在同一节点**，或让高价值号段同时具备点对点备份路由

**建议**：本轮按 B 实现，但选路筛选逻辑**预留 A 的接口** —— 把候选抽象为「本节点直接可达」与「需中继」两类，本轮只取前者，将来加中继时不必重构选路。

### 3.5 FS 节点级健康检查（DEP-6：纳入本轮）

现有 `src/heartbeat.py`（UDP OPTIONS 探测**落地网关**）与 `fs_node.status` 是两张皮：
心跳探的是落地网关可达性，**不是 FS 节点自身健康**。这部分当前完全空白。

**探测者 = 网关侧**（理由：ESL 本就由网关连，无需新增组件；FS 自己探自己没意义）。

| 维度 | 设计 |
|---|---|
| 探测手段 | ESL 建连 + `api status` / `show calls`（**不新增协议**） |
| 探测指标 | ① ESL 连通性 ② 当前并发数 ③ 注册分机数 ④ 最后成功时间 |
| 落库 | `fs_node` 扩列：`last_heartbeat_at`、`last_concurrency`、`last_reg_count`、`max_concurrency` |
| 判定 | 连续 N 次（默认 3）失败 → `status=offline`；并发超阈值 → `overload` |
| 告警 | 复用现有告警通道；⚠️ 需顺带闭环**坑位 #18（心跳告警未闭环）** |

**"摘除"在不同阶段含义不同，必须区分**：

| 阶段 | 节点异常时的动作 | 理由 |
|---|---|---|
| Phase 1（单机） | **只记录 + 告警，不做摘除** | 单节点摘掉等于全站停服，无意义 |
| Phase 2（多节点） | 选路跳过该节点上的**注册型网关**；接入点不摘（全量下发，且话机由客户端选节点） | 点对点网关全量下发，任何节点都能直连，不受单节点故障影响 |

**为什么 Phase 1 就要做**：把探测链路、落库字段、告警闭环先跑通，Phase 2 只需加"按节点过滤"一步；否则到了多节点时等于从零开始，且没有历史数据可比对。

**已知关联**：`esl_client.py` 现为单节点单例连接，Phase 2 需改为按节点建连的连接池（见 §7）。

#### 3.5.1 落地状态（#69 ✅，2026-09-10）

| 设计项 | 落地 |
|---|---|
| 探测者 = 网关侧 | `src/node_health.py`，`main.py` 起 `NodeHealthProber` 守护线程 |
| 探测手段 | ESL 短连接 + `api show calls`（并发）+ `api sofia status profile internal reg`（注册数） |
| 扩列 | `fs_node`: `last_heartbeat_at` / `last_concurrency` / `last_reg_count` / `max_concurrency` / `fail_count` |
| 节点从哪来 | **自注册 upsert**（按第1类 `NODE_UUID`），无需手工建；多节点各自上报 |
| 判定 | 连续 `node_health_fail_threshold`(默认 3) 次失败 → `offline(0)`；并发 ≥ 上限 → `overload(2)`；否则 `online(1)` |
| 阈值来源 | `fs_node.max_concurrency` 列优先，NULL 回落第2类 `system_setting.node_max_concurrency`（0=不限） |
| 周期/阈值热生效 | `node_health_interval`(30) / `node_health_fail_threshold`(3) / `node_max_concurrency`(0)，每轮重读 |
| 告警 | `src/alerting.py::alert_if_changed` → `operation_log`（`operator=system`），**状态变化才写**避免刷屏 |
| Phase 1 不摘除 | 仅记录+告警；Phase 2 再由选路按 node 过滤消费 `fs_node.status` |

**顺带闭环坑位 #18**：`heartbeat.py`（落地网关 UDP OPTIONS 探测）上下线翻转改为调用同一告警出口，
写 `operation_log` 的 `gateway_down` / `gateway_up`，不再是只 `print`。

⚠️ **告警必须独立事务**：`alert_if_changed` 自建 session 并 commit，不复用调用方 `db` ——
否则告警落库失败会连带 `rollback` 掉调用方的状态更新（实测踩过：状态改了却被回滚成原值）。
另：`operation_log.created_at` 是 NOT NULL 且模型无 default，必须显式赋值，否则 SQLAlchemy 显式插 NULL 报 1048。

---

## 4. 三类配置文件

### 4.1 第 1 类：部署前配置（单文件 `deploy.yaml`）

**原则**：地址留空 → 用默认值；密码留空 → 生成后回填。

| 分组 | 键 | 行为（留空时） |
|---|---|---|
| MySQL | `mysql.host` / `port` / `db` / `user` | host 留空 → `mysql`（compose 服务名，即启用容器库） |
| | `mysql.password` | 留空 → 生成强随机并回填 |
| | `mysql.root_password` | 同上（仅容器库需要） |
| Redis | `redis.host` / `port` / `password` | host 留空 → `redis`（容器）；password 留空 → 生成 |
| 外部地址 | `external.sip_ip` | 留空 → 自动探测（同 `dev-up.sh` 现有逻辑） |
| | `external.rtp_start/end` | 留空 → `20000-20100` |
| 端口 | `ports.sip` / `sip_ext` / `api` | 留空 → `5060` / `5080` / `8000` |
| ESL | `esl.password` | 留空 → 生成（须与 FS 侧一致） |
| 管理端 | `auth.admin_user` | 留空 → `admin` |
| | `auth.admin_password` | 留空 → 生成并回填（**首次安装后明文提示一次**） |
| | `auth.password_salt` / `jwt_secret` | 留空 → `secrets.token_hex(24)` |
| 录音 | `record.dir` | 留空 → `/var/lib/freeswitch/recordings` |
| | `record.backend` | 留空 → `local`；可选 `s3` |
| 部署形态 | `deploy.mode` | `single` / `split` / `multi`（决定渲染哪套 compose） |

⚠️ **回填必须是"一次性生成 + 分发"，不能让每个副本各自生成**：
网关多副本时若各自生成 `jwt_secret` / `password_salt`，副本间登录态立刻失效。
→ 生成动作只能发生在**部署器**（§6），产物落地为宿主机文件，副本只读。

### 4.2 第 2 类：热加载配置

**不收束到单一文件**，按归属分散。关键：FS 的"热加载"是**分等级**的，必须区分。

| 等级 | FS 侧 | 网关侧 |
|---|---|---|
| **真热加载** | `reloadxml`（dialplan/directory）、`sofia profile external rescan`（落地网关）、`reload mod_xxx` | 读 DB `system_setting` 的项（同步间隔等） |
| **需重启** | SIP 监听端口（`internal.xml`/`external.xml` 的 `sip-port`）、RTP 端口范围、**ESL 密码**（改 `event_socket.conf.xml` 后 ACL/密码需重启）、编解码 | DB 连接串、Redis 地址、监听端口 |

**载体建议**：
- 网关侧热加载项 → DB `system_setting` 表（现仅 2 项：`phone_sync_interval` / `ap_sync_interval`，可扩展）
- FS 侧热加载项 → 仍走 xml_curl（已在机制 A 覆盖）或 DB
- ⚠️ 改 `system_setting` 后网关需能感知：现在**没有缓存失效机制**（启动时读一次还是每次读需核实），这是改造点

### 4.3 第 3 类：启动期快照（改了要重启）

- DB 连接串、Redis 地址与密码
- ESL host/port/password
- SIP / RTP 端口范围
- 节点自身的 `node_uuid`、对外 SIP IP
- 镜像标签、资源限制

### 4.4 不在任何配置文件里：业务配置

落地网关、路由、费率、接入点、账户等**全在 DB**，天然热生效。
⚠️ 不要往配置文件里塞业务数据 —— 否则多副本/多节点立刻不一致。

### 4.5 落地状态（TaskList #68 ✅）

- 第2类统一实时访问层：`src/core/sys_setting.py`（`get_setting` / `get_int_setting`，读必查库，禁止 import 期缓存）。
- 第1/3类边界：`src/core/config.py` 模块 docstring 标注；导出 `NODE_UUID` 供 #69 FS 节点健康检查复用。
- `config.example.yaml` 增加 `node` / `record` 段；`deploy.sh` 生成并渲染 `__NODE_UUID__`（幂等：已有配置不重生成）。
- 权威说明见 `docs/config-categories.md`。

---

## 5. 各可选项的接入要点

| 可选项 | 要点 | 风险 |
|---|---|---|
| **云数据库** | 只改连接串（一行）；难点在 **migrate 的 DDL 权限** | 现 `db/migrate.py` 启动自动建表/加列。云数据库账号常无 DDL 权限 → 需区分「可建表」与「只读读写」两模式，后者需提供离线迁移脚本 |
| **docker 数据库** | 当前默认，无需改动 | `down -v` 会清库；`deploy/mysql/init/` **只有结构无种子** |
| **云 Redis** | 强制项（DEP-2） | P2-a 原子预留依赖它；Redis 不可用时策略已定（D4：只拦新增） |
| **docker Redis** | compose 加服务 + 持久化 | 需 `appendonly yes` 防重启丢计数 |
| **本地录音** | 默认；**多节点会分散** | CDR 必须存 `fs_node_uuid`，否则下载时找不到文件 |
| **对象存储录音**（DEP-7：**本轮只预留接口**） | FS 本地落盘 → 通话结束由网关任务上传 → 回写 URI | FS 原生 S3 支持弱，**不要**让 FS 直写对象存储 |

#### DEP-7 本轮预留的接口形状（不做实现）

**核心设计：CDR 存录音 URI，不存路径。**

| 存储后端 | URI 形态 |
|---|---|
| 本地盘 | `local://<node_uuid>/<uuid>.wav` |
| 对象存储 | `s3://<bucket>/<yyyy-mm-dd>/<uuid>.wav` |

本轮只做三件事，**不做上传任务**：

1. `cdr` 表增加 `recording_uri` 列（与现有 `record_file`/路径字段并存过渡），写入时统一生成 `local://...` 形态
2. 录音读取接口（M3 待补）按 URI scheme 分发：本轮只实现 `local://`（按 `node_uuid` 定位节点）→ 走网关代理下载
3. 配置层预留 `record.backend`（`local` / `s3`）与 `s3.*` 配置组，`s3` 取值时**直接报错提示未实现**，不静默降级

这样 Phase 2 要上对象存储时，只需新增一个 `s3://` 处理器 + 一个上传定时任务，CDR 与前端无需改动。

---

## 6. 配置生成方式：deploy.sh vs init 容器（回答你的第 4 问）

| 维度 | `deploy.sh`（宿主机部署器） | compose init 容器 |
|---|---|---|
| 运行时机 | 宿主机上跑，`docker compose up` **之前**，一次性 | compose 里的一个服务，`up` 时启动、做完退出 |
| 产物位置 | 直接写宿主机文件（**可 cat、可备份、可手工改**） | 写容器内；要落地宿主机必须挂 bind mount 或卷 |
| 密钥回填可见性 | ✅ 用户直接看文件 | ⚠️ 在卷里，要 `volume inspect` 才能读到 |
| 幂等要求 | 天然一次性 | **每次 `up` 都可能跑**，必须写幂等判断 |
| 编排依赖 | 无 | 需 `depends_on: condition: service_completed_successfully` |
| 跨平台 | 依赖 bash/python（Windows 需 WSL/Git Bash） | ✅ docker 原生，各平台一致 |
| 失败定位 | 终端直接报错 | 需查容器日志 |
| 适合做 | 探测 IP、**生成密钥**、渲染 compose / 配置文件 | 等待依赖就绪、**DB 迁移**、一次性数据初始化 |

### ✅ 结论：混合，各管一段（DEP-4 已确认）

> **DEP-4 已落地**：仓库根 `deploy.sh` 即此混合方案的宿主机部署器——首次运行探测 IP + 生成密钥（mysql root/user、esl、redis、jwt、salt、admin 明文+哈希）+ 渲染 `.env` 与 `config/docker/config_settings.yaml`；检测到已有真实配置时仅刷新随 IP 变化的 `EXT_SIP_IP`/`default_sip_domain`，跳过密钥生成（密钥属「一次性生成+分发」，禁止副本各自生成，见 §4.1）；`--force` 可重生成全部密钥，`--up` 可顺带 `docker compose up -d`。Phase1 仅渲染单机形态，split/multi 为 Phase2。

```
deploy.sh（宿主机，一次性）
  ├─ 探测 external.sip_ip
  ├─ 读取/创建 deploy.yaml，密码留空则生成并回填
  ├─ 渲染 config_settings.yaml、.env、各节点 xml_curl.conf.xml（含 node_uuid）
  └─ docker compose up -d
        └─ gateway 启动时自行 migrate（现状已有：db/session.py）
```

**理由**：
1. 密钥（admin 密码 / jwt_secret / DB 密码）**必须在宿主机明文可见一次**——否则用户重装或迁移时无从找回，init 容器把它埋进卷里反而是运维隐患。
2. DB 迁移天然幂等，放在 gateway 启动时做（现状已如此），不需要额外 init 容器。
3. 现有 `dev-up.sh` 已经在做"探测 IP + 注入 .env"，`deploy.sh` 是它的自然超集，不引入新概念。
4. 跨平台问题可接受：本项目的部署目标是 Linux 服务器（dev 也用 WSL）。

**什么时候才需要 init 容器**：若将来要支持"纯 docker、宿主机不许跑脚本"的场景（如某些 PaaS），再补一个 init 容器只做渲染，密钥仍由外部注入。

---

## 7. 后续改造点清单（按 DEP-9 两阶段拆分）

### Phase 1：单机 + 强制 Redis（优先落地）

| 改造点 | 现状 | 说明 |
|---|---|---|
| Redis 接入（容器 + 云） | **全仓零引用** | DEP-2 强制；compose 加服务 + `appendonly yes`；地址/密码进 `deploy.yaml` |
| `deploy.sh` 部署器 | 只有 `dev-up.sh` | 探测 IP + 生成/回填密码 + 渲染配置（§6 / DEP-4） |
| 三类配置收束 | 配置散在 yaml / env / DB | §4；`deploy.yaml` 单文件 + DB `system_setting` + 启动快照 |
| FS 节点级健康检查 | **完全空白** | DEP-6：ESL 探测 + `fs_node` 扩列 + **告警闭环（顺带修坑位 #18）**（§3.5） |
| 录音 URI 抽象 | CDR 存本地路径 | DEP-7：增 `recording_uri`，只实现 `local://`（§5） |
| `db/migrate.py` DDL 降级 | 启动自动 DDL | 云数据库无 DDL 权限时需离线迁移脚本 |
| `system_setting` 缓存失效 | 无失效机制 | 第 2 类热加载的**前置** |

### Phase 2：多节点

| 改造点 | 现状 | 说明 |
|---|---|---|
| `gateway_node` 表 | 无 | §3.3；`uk_gateway` 保证单选 |
| `fs_sofia_config.py` / `/fs/config` | 全量下发 | 按 `?node=` 过滤（点对点全给 + 本节点注册的） |
| gateway CRUD + Web 前端 | 无 node 校验/字段 | 注册模式必选节点 + 三要素唯一校验 |
| **★ 选路逻辑** | `route/service.py` 无 node 维度 | **按 node 筛选候选 gateway**（TaskList #64） |
| 存量数据迁移 | `gw-carrier-a` **已核实点对点** | 无需补记录；上线前扫 `auth_type=1` 且无归属者 |
| `esl_client.py` | 单节点单例连接 | 改为按节点建连的连接池 |
| ESL 事件订阅 | 单副本假设 | 多副本会**重复写 CDR**（坑位 #2），需幂等或选主 |
| 节点故障摘除 | 无 | Phase 1 健康检查的消费者（§3.5） |

### 文档

| 文件 | 现状 | 动作 |
|---|---|---|
| `docs/架构-拆分三机评估.md` | 2026-09-05 成文 / 2026-09-09 更新 | §4.2.1 推荐的 xml_curl 方案**已实现**（机制 A），**已于 2026-09-09 更新**（TaskList #65 ✅） |

---

## 8. 待决问题（2026-09-09 已全部拍板）

| # | 问题 | 结论 |
|---|---|---|
| 1 | §6 配置生成方式 | ✅ `deploy.sh` 混合方案（DEP-4） |
| 2 | 分片用加列还是关联表 | ✅ 独立关联表 `gateway_node`（DEP-5） |
| 3 | FS 节点级健康检查 | ✅ **纳入本轮**（DEP-6） |
| 4 | 对象存储录音 | ✅ **本轮只预留接口**，上传任务后置（DEP-7） |
| 5 | 本设计文档去向 | ✅ **入仓 `docs/`**（DEP-8） |
| 6 | 立项与推进节奏 | ✅ **分两阶段，先落地「单机 + Redis」**（DEP-9） |

### 仍开放（不阻塞 Phase 1 开工）

1. **Redis 高可用形态** —— DEP-2 强制后，Redis 挂 = 全站拒呼（D3 fail-close）。单点 / 哨兵 / 云托管主从？Phase 1 可先用单点 + 快速恢复，但**上生产前必须定**。
2. **第 2 类热加载载体** —— 全部走 DB `system_setting`，还是部分走 yaml + reload 信号。
3. **多副本 CDR 幂等方案** —— 选主 vs 幂等键去重（Phase 2 开工前定即可）。

---

## 9. 分阶段实施计划（DEP-9）

### Phase 1：单机 + 强制 Redis

**目标**：落地部署底座（三类配置、`deploy.sh`、Redis、FS 节点健康检查、录音 URI），**同时解封 P2-a**。

**为什么先做它**：P2-a（原子预留）依赖 Redis，而 Redis 的接入形态正是本设计定义的。Phase 1 完工后 P2-a→b→c 可按原铁律顺序推进，**不必等多节点**。

粗估 **3–4 dev-day**。

### Phase 2：多节点

**前置**：Phase 1 稳定 + P2 完成。
**内容**：`gateway_node` 分片、`/fs/config` 按 node 过滤、选路 node 筛选、Web 前端、ESL 多节点、CDR 幂等、故障摘除。
粗估 **3–5 dev-day**（含多节点 staging 验证）。

### 与 P2 的顺序

```
Phase 1（单机 + Redis） → P2-a → P2-b → P2-c → Phase 2（多节点）
```

**理由**：P2 的并发控制依赖"即时准确的并发值"，多副本下该值**必须来自 Redis**（Phase 1 提供）；而多节点分片不改变并发模型，可叠加在 P2 之后。

⚠️ 注意：这个顺序**不等于** P2 被阻塞到 Phase 1 全部完工 —— Redis 接入（Phase 1 的第一项）做完即可解封 P2-a，`deploy.sh` 与健康检查可与 P2 并行。
