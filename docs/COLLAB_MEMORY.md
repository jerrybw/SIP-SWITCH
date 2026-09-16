# COLLAB_MEMORY — 协作记忆与资产密级约定

> **版本**：v1.2（2026-09-16，作者 @WorkBuddy；v1.2 补齐 §4 模块 Owner 责任表 + 关键路径冻结表）
> **定位**：仓内协作记忆的唯一入口。配合 `docs/ROADMAP.md`（进度唯一事实源）与 `docs/PITFALLS.md`（避坑合集，append-only）使用。多 Agent 协作规范全文见 **`docs/多agent协作方案.md`**（v1.1，2026-09-13 入仓；此前置于仓外 `/root/src/` 时曾被误删，入仓后受 git 历史保护）。

## 1. 资产密级三级约定

| 密级 | 内容 | 位置 | 规则 |
|------|------|------|------|
| **公开** | 机制/坑位/工作流/场景桩 | 仓内（本文档、PITFALLS、skills/、docs/） | 可自由 clone；PR 更新；PITFALLS 类文件 append-only |
| **脱敏** | 配置模板、交接文档 | 仓内（占位符形式，如 `<DEV_HOST>`、`<ADMIN_PASSWORD>`） | 真实值只存在于部署时生成的 `.env` / `config_settings.yaml`（不入仓）；改动需保持占位符形式 |
| **禁入** | 凭据、`docker-compose.override.yml`、`config/node2/`、`deploy/fs-config/node2/`、录音运行时数据、各 Agent 本地记忆 | 仓外 | 严禁入仓；push 前 secret-scan 兜底 |

## 2. 仓内共享资产清单（2026-09-12 批次）

| 资产 | 路径 | 说明 |
|------|------|------|
| 避坑合集 | `docs/PITFALLS.md` | 65+ 条，按 #编号引用；**append-only** |
| 交接文档 | `docs/SIP-SWITCH-交接文档.md` | 项目全景速览（新 Agent 必读 #1） |
| 环境上手 | `docs/SIP-SWITCH-WSL环境与上手.md` | DEV 环境搭建与起栈（必读 #2） |
| sipp UAS 桩 skill | `skills/sipp-uas-stub/`（含 `assets/` 场景 XML+探测脚本） | 落地网关桩/注册桩/OPTIONS 探测；**场景文件已归档验证过，禁手写场景 XML**（中文注释/`--`/`response=".*"` 三大 parse 坑见 PITFALLS #54/#55） |
| WSL 运维 skill | `skills/sip-wsl-devops/SKILL.md` | DEV 全栈运维：改源码重建、ESL/CDR 排障、多栈隔离 |

**说明**：`skills/` 目录源自 WorkBuddy 用户级 skill 的脱敏镜像。对 WorkBuddy Agent：直接用本地 skill（更快、含凭据引用）；对其他 Agent（opencode 等）：读本目录。

## 3. 提交归因与 token 纪律（v1.1 增补）

### 3.1 commit 归因规范（本地同环境，即时生效）

本地同环境的 Agent（WorkBuddy / opencode 等，能直接操作 WSL 仓）统一在 commit 尾注声明身份：

```
Co-authored-by: <agent-name> <agent-name>@agent.local
```

- 示例：`Co-authored-by: workbuddy <workbuddy@agent.local>`、`Co-authored-by: opencode <opencode@agent.local>`
- 分支仍按 trunk-based 规范：`feature/{agent-name}/{task-id}-{desc}`，生命周期 ≤3 天
- 性质说明：尾注归因是**约定而非强制**（git 不校验署名）；单人 + 可信 Agent 场景够用，强制力随 §3.2 的触发条件一并引入

### 3.2 token 纪律与升级触发条件

**现状（维持）**：Agent **不持有**任何 token——push 由用户触发，PAT 当面提供、一次性 URL 使用、不落盘（`.git/config` 0 残留），push 前 `git-push-secret-scan` 卡点。这是当前最强且最简的权限模型。

**升级触发条件（满足任一才升级）**：
1. CI/PR 临时环境需要**自主 push**（无人值守流程）→ 引入 **fine-grained PAT**（仅 `Contents: write`，绑定单仓）+ **main 分支保护**（禁直推、只许 PR）
2. 出现**非本地同环境**的常驻 Agent（云端/他人机器）→ 同上，外加评估 MCP 权限网关类方案
3. 多人类协作启动 → branch protection + 强制 PR review

**明确不采纳**（2026-09-12 评审，@WorkBuddy）：MCP 权限网关（GitBlinder/service-gator，当前 token 纪律已等效覆盖且更严）、Cursor Origin（Beta/规模不匹配）、自托管 GitLab/Gitea（运维负担）、平台迁移（成本纯损失）。完整论证见《多 Agent 协作开发方案》v1.1 §2。

## 4. 模块 Owner（v1.2 —— 2026-09-16 由 @WorkBuddy 补齐，取代 v1.0 空骨架）

> **Owner 的含义（务必先读）**：Owner ≠ 产权，而是**变更责任 + 冲突仲裁人**。
> 具体三条：(1) 该模块的改动**先与 Owner 对齐**再动手；(2) 模块位于关键路径（§4.2）时，
> **同一时刻只允许一个 Agent 写**，其他人改走「提方案 → Owner 实施」；
> (3) 出现跨模块接口分歧时，Owner 的意见优先，解决不了升级给用户。
> **Owner 与「谁在做」是两回事** —— 当前实际开发者可能是任意 Agent（见 §7 快照），
> 但**责任与仲裁固定在人**，否则并行时无人对回归负责。

### 4.1 分模块责任表

| # | 模块（文件） | Owner | 职责范围 | 关键路径 |
|---|---|---|---|---|
| M1 | `src/fs_sofia_config.py`、`src/fs_provision.py`、`src/gw_state.py`、`src/phone_sync.py` | **@WorkBuddy** | FS 对接层：**机制 A（不落盘）** 的 XML 生成与下发、网关增删改的跨节点信令（`provision_seq`/`provision_pending`）、`sofia::gateway_state` 事件消费与注册状态回写、话机同步 | **是**（§4.2） |
| M2 | `src/esl_client.py`、`src/cdr_truth.py`、`src/api/billing.py` | **@WorkBuddy** | ESL 连接与事件循环、CDR 落库与 **mod_xml_cdr 真源补算**、计费扣款 | **是**（§4.2） |
| M3 | `src/api/app.py`（**冻结**）、`src/route/service.py`、`src/rules/`、`src/concurrency.py` | **@WorkBuddy** | **`app.py` 冻结，改动权归维护者**（`ROADMAP` 2026-09-11 约定）；并发**预检**（P2-a Redis Lua）与并发**选路因子**（P2-c 打标）；主被叫规则过滤（`_filter_candidates_by_gw_rules`）与候选池排序 | **是**（§4.2） |
| M4 | `src/api/dialplan_xml.py`、`src/api/directory_xml.py`、`src/api/fs_auth.py` | **@WorkBuddy** | xml_curl 出口：拨号计划、动态目录、`/fs/*` 的 HTTP Basic 认证（fail-closed） | **是**（§4.2） |
| M5 | `src/api/auth.py`、`authz.py`、`users.py`、`roles.py`、`csrf.py`、`oplog.py` | **@WorkBuddy**（M3P2 实施：@zcode） | 登录/会话、角色三档与权限矩阵、用户 CRUD、CSRF 同源校验、操作日志埋点 | **是**（§4.2，涉权限判定） |
| M6 | `src/db/migrate.py`、`src/db/models.py`、`src/db/session.py` | **@WorkBuddy** | schema 演进。**改动先报备**：两边都往尾部追加，**串行合入**，后合方 rebase | **是**（§4.2） |
| M7 | `src/core/`（`config.py`/`redis_client.py`/`sys_setting.py`/`pw_hash.py`/`lru_cache.py`） | **@WorkBuddy** | 配置加载与热更、Redis 客户端、`system_setting` 读写、密码哈希 | 是（配置变更影响全站） |
| M8 | `src/static/`（`admin.js`/`admin.css`）、`src/templates/index.html` | **@WorkBuddy** | 管理端前端。**M3 独占**：同一时刻单一写入者；`index.html` 的 `?v=` 缓存号由**最后合入 main 的一方** bump 一次 | 否，但**冲突面最大** |
| M9 | `src/node_health.py`、`src/heartbeat.py`、`src/alerting.py` | **@WorkBuddy** | FS 节点健康探测与僵尸治理、落地网关心跳、告警出口（`operation_log` 去重 + webhook 外推） | 否 |
| M10 | `src/recordings.py`、`src/api/live_calls.py`、`src/api/cdr_export.py` | **@WorkBuddy** | 录音 URI 抽象（`local://`/`cos://`）、在途通话视图、话单/账单导出 | 否 |
| M11 | `src/api/sys_config.py`、`src/api/accounts.py`、`src/api/crud.py` | **@WorkBuddy** | 系统设置（含 schema 校验）、账户与网关 CRUD、通用实体兜底路由 | 否 |
| M12 | `tests/`、`deploy/`、`docker-compose*.yml`、`dev-up.sh`、`deploy.sh` | **@WorkBuddy** | 测试夹具与用例、部署脚本与 compose 基线、密钥生成（密钥仅部署时生成，不入仓） | 否 |

### 4.2 关键路径冻结表（比 Owner 更硬的一层约束）

以下文件属于**多人并行时最易互相踩踏**的面。规则：**同一时刻只允许一个 Agent 持有写权**，
其余 Agent 一律「提出方案 → 交由当前持有者实施」；持有者变更须在群内周知。

| 文件 | 为什么关键 | 约束来源 |
|---|---|---|
| `src/api/app.py` | 全站路由注册点，所有 router/中间件挂载处；改动易致路由被兜底吞掉（PITFALLS #34） | `ROADMAP` 2026-09-11「app.py 冻结」 |
| `src/db/migrate.py` | schema 演进的唯一入口，多人同时追加必冲突 | `ROADMAP` 2026-09-12「migrate 报备」 |
| `src/static/admin.js` | 前端冲突面最大（M3 用户管理、P2 并发打标都曾想动它） | `ROADMAP` 2026-09-12「M3 独占」 |
| `src/templates/index.html` | 含 `?v=` 缓存版本号，多人同 bump 会互相覆盖 | 同上，「最后合入方 bump」 |
| M1 的 FS 对接层 | 机制 A 的**单一下发出口**，多写者会造出重复/冲突的 XML | 本文档 v1.0 起即定「此层单一 Owner」 |

### 4.3 未分派 / 待定

| 项 | 说明 |
|---|---|
| **`skills/` 与 `docs/`（除 ROADMAP/PITFALLS）** | 文档类无 Owner 也有事实仲裁人：`ROADMAP.md` 进度口径 = @WorkBuddy；`PITFALLS.md` **append-only**，任何人可追加、**不可改写历史条目**（删除仅限含敏感信息者，须记录原因） |
| **M4 压测脚本（T-401~404）** | ❌ 未实现，故无 Owner；一旦开工须先定 Owner（`ROADMAP` §10 待拍板 #3 未决，DEV 单机跑不了真实规格） |
| **T-501 FS 集群分发** | **已拍板不做**（2026-09-15，走 VOS500 类架构，只保多节点数据同步）——**不设 Owner**，避免复活 |

## 5. 新 Agent 入职清单

1. `docs/SIP-SWITCH-交接文档.md`
2. `docs/ROADMAP.md`（待办唯一事实源；**以代码验证，禁凭记忆推演**）
3. `docs/PITFALLS.md`（先查索引再按编号读；重点 #8 #24 #26 #30 #53 #60 #62-65）
4. 本文档（**提交前读 §3 归因规范**）
5. 按 `docs/SIP-SWITCH-WSL环境与上手.md` 起栈 → `./dev-up.sh` 验证

## 6. dev 共享环境使用约定（2026-09-12 环境重构后生效，全体 Agent 铁律）

**公用栈（sip-switch project，`/root/src/SIP-SWITCH`，8 容器常备）**：
- mysql + redis（单套共享）；freeswitch+gateway ×2（node1 Web 8000 / node2 Web 8001）
- **sipp-reg 公用注册桩**：单实例三合一（REGISTER/INVITE/OPTIONS 全 200，`/root/sipp-reg/reg_uas.xml` 最新版，带头部声明），node1/node2 注册型网关共用（gw9 `test-register-gw` 应 REGED）
- sipp-stub（OPTIONS+INVITE 桩）；种子备份 `/root/sip-switch-seed-backup-20260912.sql`（down -v 后 `docker exec -i mysql < 备份` 恢复）

**使用铁律**：
1. 公用栈供 **WorkBuddy 主会话 + 用户日常测试**使用，保持常驻
2. **其他 Agent 按需自起独立栈**（`docker compose -p sip-switch-{name} ...` 或复制 dev-up.sh 改项目名），**用完立即 stop/down**，**禁止常驻**
3. 对公用栈做破坏性操作（down -v、改卷、schema 迁移）前**必须先 mysqldump 备份并周知**
4. `/fs/*` 已启用 HTTP Basic 认证（fail-closed）：凭据在 `.env` XMLCURL_* 与两个 config_settings.yaml `[xml_curl]` 段（node1/node2）**三处同值**，改配置勿删
5. `/fs/*` 外部 curl 无凭据 → 401（正常，不是故障）；管理端裸 curl cookie 写 `/api/*` → 403（CSRF 同源校验，脚本请带 Authorization/Origin 头）

6. **改了 `src/` 必须重建镜像才生效；且「验收通过」≠「运行环境已更新」（2026-09-15 立）**：
   - 网关 `src/` 是 **COPY 进镜像**（非挂载）。改代码后**必须**
     `docker compose build gateway gateway2 && docker compose up -d --no-deps gateway gateway2`。
     ⚠️ **裸跑 `docker compose build` 会连带重建 FS（30–60 min）** —— 永远显式指定服务名。
   - ⚠️ **验收方式 ≠ 交付方式**：验收通常用「**挂载代码的一次性容器 + 独立库**」
     （`docker run -v <代码>:/app ...`、隔离库 `sip_e2e`），它证明**代码正确**，
     但**不会更新任何常驻容器**。凡「合入 / 交付 / 验收通过」后，**必须显式让运行容器与 HEAD 对齐**，
     并核验两项：① `md5sum` 比对（仓库 vs 容器内 `/app/src/...`）；② 启动日志迁移行
     （如 `[migrate] roles / role_perm tables ensured`）。
   - **反例（本次）**：2026-09-15 M3P2 已合入并推送 `cd3caf4`，但 dev 容器仍是 12h 前镜像
     （`src/api/roles.py` 不存在、`app.py`/`admin.js` 的 md5 与仓库不符）→ **用户测不到**才发现此缺口。

## 7. 三方协作现状与进度快照（2026-09-13 傍晚 —— ⚠️ 已过期，见 §7.1 更正）

> 本节是**快照**，会过时；**待办事实源仍以 `docs/ROADMAP.md` 为准（以代码验证，禁凭记忆推演）**。

**§7.1 快照更正（2026-09-16 @WorkBuddy 实测，读下表前先看这里）**：下表已落后 3 天，实测偏差如下 ——

- 仓 HEAD = **`7d23898`**（不再是 §7 提到的 `16b6ca7`），`origin/main` **同点 0/0**，工作区干净；
  **当前待推 = 0**（§7 写的「18 commit 待推」已全部推完）。
- **M3 Phase 2**（自定义角色 + 权限矩阵）**已交付并推送**（2026-09-14）；§7 表里 zcode「正在写 P2」的描述已作废。
- **P1 CDR 真源 / P2 并发预检** 均已合入 main（见 `ROADMAP` 2026-09-13 两节）。
- §7 写的「未拍板：多 Agent 互相派发」——已演进为独立项目，见 `docs/多agent协作方案.md` 与 `multi-agent-task-chain-orchestration` skill。
- **Module Owner 已于 2026-09-16 补齐**（见本文档 **§4**），不再有「（待派）」空项。

| 方 | 沙箱账户 | 工作区 | 栈权限 | 当前状态 |
|---|---|---|---|---|
| **WorkBuddy** | 宿主直接 ssh root（**维护者**） | `/root/src/SIP-SWITCH`（真源） | 全权 | **P1 已交付**（`25902a7`→`16b6ca7`）；下一批 = P2 的 `app.py` patch |
| **zcode** | `zcode@localhost:22022`（仅密钥；sudo 仅 `zcode-stack`） | `/home/zcode/work/SIP-SWITCH`（自己的 clone） | 独立栈 **zstack**（主仓代码）+ **zdev**（**它自己的 clone**），端口独立 | 正在写 **P2**（并发预检）+ 修前端 3 处；须 rebase 到 `16b6ca7`；**无 push 权限** |
| **opencode** | `opencode@localhost:22022`（仅密钥；sudo 仅 `opencode-stack`） | `/home/opencode/work/multi-agent-comm/` | 独立栈 **ostack**（**仅 redis** `127.0.0.1:6390`） | 「多 Agent 通信方案」MVP 已交付验收；SSE 阻塞缺陷修复**已实测通过**；新派 4 件 |

- **评审与设计稿**（不在仓内）：`/home/zcode/work/reviews/` —— `设计稿-P1-CDR真源.md`（含 **§14 实施发现与定稿偏离**）、
  `设计稿-P2-并发预检实时查询.md`、`评审-P1-by-zcode.md`、`评审-P2-by-workbuddy.md`
- **多 Agent 通信方案 v1.2**：本仓 `docs/多agent协作方案.md`（自 v1.1 升级）；opencode 侧开工依据 = 其项目内 `DESIGN.md`
- **通知约定（2026-09-13 拍板）**：给其他 Agent 的通知**直接输出纯文本**（缩进 + 分段排版），**不生成 `.md` 文件**
- **push 纪律**：zcode / opencode **均无 push 权限**；统一由 WorkBuddy **核验合入后**经**人工一次性 PAT** push（PAT 不落盘）
- **当前待推**：18 commit（P1 的 5 个 + 此前 13 个）；策略 = 等 zcode 交付 → 核验合入 → **一起 push**
- **环境新增**：zstack / zdev 的 freeswitch 段已加 `xml_cdr` 落盘卷（P1 随基线进入 Agent 栈；compose 有 `.bak` 备份）
- **未拍板**：多 Agent「互相派发 / 可被调度的执行面」（A 常驻 runner + headless CLI / B 只接可脚本化任务 / C 文件桥）
  —— **opencode 已被明确按住不做**，等用户决策

