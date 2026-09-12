# SIP-SWITCH 多 Agent 协作开发方案

> **版本**：v1.1（2026-09-12 修订）　**修订人**：WorkBuddy　**原稿**：v1.0
> **背景**：SIP-SWITCH 是一个面向中小企 VoIP 的 FreeSWITCH 业务网关，将路由决策、主被叫限制、故障切换、并发控制、计费、CDR 与运营后台从 FreeSWITCH 内部剥离到独立 Python 服务。当前项目已完成 M1/M2/M3 核心功能，处于「功能基本齐整、规模化验证与高可用待建设」阶段。随着团队扩展，需要建立多 Agent 并行开发的协作体系。
>
> **🔧 v1.1 修订说明（2026-09-12，@WorkBuddy）**：经评审，本方案按「**方案 A：多 AI Agent 并行**（WorkBuddy / opencode 等多会话协作，非多人类团队）」裁剪落地。三处实质修订：① §3 记忆体系——仓内协作记忆与 WorkBuddy 会话记忆**分立不合并**，记忆类文件改 append-only；② §4.2 环境——删除「本地轻量连远程共享 FS」层（**技术不可行**，理由见该节），环境隔离主体改为**独立 Docker Compose project**（已在实战验证）；③ §7 路线图——K8s/契约测试/性能基线后置，第 1 周收窄为最小闭环。另：原文件编码已损坏（UTF-8→GBK mojibake），本次全文重写修复。正文中所有改动处均以「🔧 v1.1」标注。

---

## 1. 项目现状速览

| 维度 | 现状 |
|------|------|
| **核心技术栈** | FreeSWITCH 1.11.2 + Python 3.12 (FastAPI/SQLAlchemy) + MySQL 8 + Redis 7 |
| **部署形态** | Docker Compose 多节点（FS1/FS2 + Gateway1/Gateway2 + MySQL + Redis + sipp-stub） |
| **关键架构** | 机制 A（mod_xml_curl 动态下发，FS 零落盘）、ESL 纯 Socket 客户端、事件驱动并发控制 |
| **进度事实源** | `docs/ROADMAP.md`（唯一），`PITFALLS.md`（65 条避坑），交接文档 |
| **当前痛点** | 单人开发、记忆不共享、环境独占、无契约测试、Secret 手工管理 |

> 🔧 **v1.1 注（@WorkBuddy）**：「团队扩展」在可见周期的实际形态 = 用户 + 多个 AI Agent 会话并行。人类团队协作相关项（On-call、Secret 人工轮换、强制人类 Code Review）**保留在 §6/§8 但标注「待触发」**，不进入第 1-2 周落地范围。

---

## 2. 代码版本管理策略

### 2.1 分支模型：Trunk-based + 短生命周期 Feature 分支
```
main (受保护分支，仅允许 PR 合并，CI 必须通过)
├── feature/{agent-name}/{task-id}-{short-desc}  # 每 Agent 一个分支，生命周期 ≤ 3 天
├── release/v0.x                                 # 发布分支
└── hotfix/{issue-id}                            # 紧急修复
```

**核心约束**：
- `ROADMAP.md` 是**唯一进度事实源**，所有 Agent 必须遵守「改代码 → 更新 ROADMAP（✅+证据：文件/行号/接口/表名）→ 关 TaskList → 同步记忆」四件套
- 禁止长期分支：feature 分支超过 3 天必须拆小或 rebase
- 代码所有权按模块划分（如 `api/app.py` 路由层归 Agent-A，`esl_client.py` 归 Agent-B），但允许跨模块 PR

> ✅ v1.1 评审结论（@WorkBuddy）：保留，与项目现有铁律完全一致，无需改动。

### 2.2 Git Hooks 强制门禁
```bash
# .githooks/pre-commit
secret-scan (git-secrets/trufflehog)  # 禁止密钥入库
black + ruff + mypy                   # 代码风格/类型检查
# .githooks/pre-push
pytest -x                             # 单测全绿
secret-scan                           # 再次扫描

# .githooks/commit-msg
conventional commits: feat|fix|chore|refactor(#issue): <desc>
```

> 🔧 **v1.1 补充（@WorkBuddy）**：当前项目 push 前 secret-scan 是以 WorkBuddy skill（`git-push-secret-scan`）人工触发的；落地 hook 时应把该 skill 的扫描规则固化为本仓 `.githooks/pre-push` 真实脚本（扫描真实 IP/`github_pat_`/`.env` 等），并 `git config core.hooksPath .githooks` 激活。hook 是兜底，不取代 skill 流程。

---

## 3. 记忆/上下文共享体系（最关键）

> 🔧 **v1.1 本节整体修订（@WorkBuddy）**：原方案把 `.workbuddy/memory/MEMORY.md` 直接当「仓内共享记忆」，且冲突策略为「最近提交者为准」——两处都有问题：① `.workbuddy/` 是 WorkBuddy 客户端的**会话记忆目录**（位于 Windows 工作区、自动注入会话、客户端管理），**不属于仓库、不应入仓**，硬入仓会与客户端机制冲突；② 「最近提交者为准」对坑位记录是灾难——并发追加的坑位会被静默覆盖丢失。修订为「**两套记忆分立**」：

| 层级 | 存储位置 | 生命周期 | 读写权限 | 更新触发 | v1.1 变化 |
|------|----------|----------|----------|----------|----------|
| **仓内协作记忆** | `PITFALLS.md` + `docs/ROADMAP.md` + `docs/COLLAB_MEMORY.md`（新增，存架构决策/模块 Owner/环境约定） | 永久 | 全员可读，写走 PR | 重大决策、架构变更、踩坑复盘 | **唯一入仓记忆源**；PITFALLS 严格 **append-only**（按编号追加，禁改写历史条目，废弃条目标 `[已修复]` 不删除） |
| **WorkBuddy 会话记忆** | `.workbuddy/memory/`（Windows 工作区，**不入仓**） | 客户端管理 | 单用户 | 会话自动注入 + 用户显式记忆 | **与仓内记忆分立**：仓内记忆是事实源，WorkBuddy 记忆是索引/偏好；开工时 Agent 按现有惯例读 WorkBuddy 记忆 → 再读仓内文档对齐 |
| **任务级上下文** | `docs/ROADMAP.md` + commit message + PR description | 任务周期 | 执行 Agent 写，其他 Agent 读 | 每个 Task 完成时 | 不变 |
| **会话级短期** | 各 Agent 自身会话上下文 | 单次会话 | 仅当前 Agent | 开发过程中 | 不变（不限定 opencode，任何 AI 会话工具等价） |

### 3.2 同步机制
> 🔧 **v1.1 修订（@WorkBuddy）**：删除「每日 GitHub Actions 提取 MEMORY.md 变更 + Webhook/邮件/飞书广播」——这是人类团队语义的过度设计；AI Agent 每次开工自读仓内文档即完成同步（现状已如此）。冲突解决修订为：

- **记忆/坑位类文件（PITFALLS、COLLAB_MEMORY）**：**append-only**，并发追加无冲突；只有编号索引区允许小改
- **ROADMAP**：同一 Task 条目并发改 → 以「✅ + 证据链（commit hash / 文件:行号）」完整者为准
- **代码文件**：一律走 PR，不存在并发直改 main

### 3.3 新 Agent 入职清单
```bash
git clone https://github.com/jerrybw/SIP-SWITCH.git
# 依次阅读（按重要序）：
# 1. SIP-SWITCH-交接文档.md
# 2. docs/ROADMAP.md          ← 待办唯一事实源，以代码验证，禁凭记忆推演
# 3. PITFALLS.md              ← 先查顶部「全量索引」定位编号再读条目，勿通读
#    （重点 #8 #24 #26 #30 #53 #60 #62-65）
# 4. docs/COLLAB_MEMORY.md    ← v1.1 新增：协作约定/模块 Owner/环境现状
# 5. 运行 ./dev-up.sh 验证环境
```

---

## 4. 运行环境管理方案

### 4.1 现状分析
- 当前：单套 WSL 环境，`docker-compose.override.yml` / `config/node2/` / `deploy/fs-config/node2/` 为 untracked 文件，含 Dev 内部 IP，**严禁入仓**
- 痛点：端口冲突、数据卷共享导致录音/CDR 串扰、密钥手工分发、无法并行跑多套环境

### 4.2 环境架构（v1.1 修订：两层，独立 compose project 为主体）

> 🔧 **v1.1 实质修订（@WorkBuddy）**：原方案「本地轻量环境 = 单 gateway 进程 (`python -m src.main`) 连远程共享 MySQL/Redis/FS」**技术不可行，整层删除**。理由：gateway 是 ESL 长连接事件驱动模型，多个 gateway 实例连同一个 FS 会**抢事件**——事件重复消费、CDR 重复落库、reconcile 对账互踩（#75 事件异步化 + 对账自愈改造的前提就是「一个 FS 对一个 gateway」）；`NODE_UUID` 只隔离机制 A 的配置下发（xml_curl 按节点过滤），**隔离不了 ESL 事件广播**。要真隔离就得每 Agent 独立 FS → 退化为完整栈，「轻量层」失去意义。
>
> **实证**：当前 WSL 上已稳定并存两套独立 compose 栈（`sip-switch-*` 与 `sipswitch-pr1-*`，`COMPOSE_PROJECT_NAME` 隔离）——**该模式已被实战验证**，直接升为主体。

| 层级 | 用途 | 资源 | 隔离策略 | 适用场景 |
|------|------|------|----------|----------|
| **独立完整栈（主体）** | 每 Agent 一套，日常开发 + E2E | 完整栈（FS + MySQL + Redis + gateway + sipp-stub） | `COMPOSE_PROJECT_NAME=sip-switch-{agent}`，独立端口段/数据卷/密钥（`deploy.sh --agent {name}` 生成） | 业务逻辑、单测、真实 FS 行为验证、录音/CDR 全链路 |
| **PR 临时环境（后置）** | PR 级自动化测试 | 独立 Docker Compose project | `COMPOSE_PROJECT_NAME=sip-switch-pr-{num}`，随机密钥、端口/DB 前缀，跑完自动销毁 | CI 建成后再启用（见 §7，第 3 周）；**Kubernetes namespace 方案删除**——项目无 K8s 基础设施，属过度设计 |

- ~~原「共享集成环境（单套全栈按 NODE_UUID 隔离网关实例）」~~ → 删除：多 Agent 共享一套栈时 ESL 事件/数据卷串扰问题与「轻量层」同源，共享不如独占（单栈全量资源开销在本机 WSL 可承受，实测已跑两套）。

### 4.3 关键隔离实现细节
```yaml
# docker-compose.override.yml.{agent}（gitignore，各 Agent 自备）
# v1.1 注：作用域从「单套栈内隔离网关」改为「整套栈独立」——
# 项目名隔离后端口/卷/网络天然分离，override 只承载各栈差异项
services:
  gateway:
    environment:
      NODE_UUID: "agent-{name}-node1"
      GATEWAY_CONFIG: /app/config/config_settings.{name}.yaml
    ports:
      - "800{id}:8000"   # 宿主端口错开（同机多栈时用栈级端口段更稳）

# deploy.sh --agent {name} 自动生成：
#   .env.{name} + config_settings.{name}.yaml（含独立 JWT/ESL/MySQL/Redis 密码）
```

> 🔧 **v1.1 补充（@WorkBuddy）**：录音数据卷每栈独立（`data/{agent}/recordings/`），沿用 #70 录音 URI 的 `local://` 归属判定；**改 `src/` 后必须 `docker compose build gateway`（带 `-p {project}`），无参 build 会连带重建 FS 30-60min**（PITFALLS #10），这是多栈并行的最大时间成本，任务排期需预留。

---

## 5. 任务拆分与协作流程

### 5.1 任务粒度标准
| 级别 | 示例 | 预估工时 | 验收标准 |
|------|------|----------|----------|
| **Epic** | "多租户计费 v0.4" | 2-4 周 | ROADMAP 里程碑完成 |
| **Story** | "网关主被叫规则参与降级(#74)" | 1-3 天 | 单个 PR + 单测 + E2E 实测 |
| **Task** | "修复 _filter_candidates_by_gw_rules 空指针" | < 4h | 单次 commit + 单测通过 |

### 5.2 标准协作流（四件套强制）
```
Agent 接任务 → 本地建 feature 分支 (feature/{name}/{task-id}-{desc})
  → 写代码 + 补单测（tests/ 目录对应文件）
  → 本地验证：pytest + E2E（真实软电话/sipp 桩，禁手搓 UAC）
  → 更新 ROADMAP.md（✅+证据：文件/行号/接口/表名）
  → 关联 TaskList
  → 同步关键坑位到 PITFALLS.md（append-only）
  → 提 PR（模板必须含：变更点、验证步骤、回滚方案、影响范围）
  → CI 自动跑：pytest + secret-scan（CI 建成前：pre-push hook 兜底）
  → Review：Owner Agent 复核 + 用户对高风险/超范围项拍板
  → merge 到 main
  → 部署到独立栈冒烟
  → 更新部署文档（如有配置变更）
```

> 🔧 **v1.1 修订（@WorkBuddy）**：「Code Review 至少 1 人」改为「Owner Agent 复核 + 用户拍板」——AI Agent 并行场景下无固定人类 reviewers；用户保留对架构决策与高风险操作的终审权（与现有协作约定一致：超范围先提醒、不可逆操作二次确认）。

---

## 6. 关键风险点（补全清单）

> ✅ v1.1 评审结论（@WorkBuddy）：本清单质量最高，**全部保留**。标注「待触发」的项在多 AI Agent 阶段不投入。

| 维度 | 遗漏项 | 影响 | 对策建议 | v1.1 标注 |
|------|--------|------|----------|----------|
| **DB 迁移冲突** | 多 Agent 并发加列/改表 → `migrate.py` 幂等但顺序敏感 | 启动报错、数据不一致 | 引入「迁移锁」（Redis SETNX）或按模块分表前缀，或指定单一 Agent 管 DDL | 保留，多栈各自独立 DB 后冲突已大幅缓解，锁仍建议 |
| **FS 配置下发一致性** | 机制 A 下，多 Agent 改 `fs_sofia_config.py` 可能导致下发 XML 冲突 | FS 拿到错误配置、呼叫失败 | 指定单一 Agent 管 FS 对接层（`fs_sofia_config.py`/`fs_provision.py`） | 保留；「版本号+灰度下发」延后到真需求出现 |
| **录音文件隔离** | 共享 `./data/recordings/` 并发写会乱 | 录音丢失/错乱 | 已按 `NODE_UUID` 分片，需约定 `agent-{name}-{node}` 命名规范 | 保留，v1.1 已并入 §4.3（每栈独立卷） |
| **CDR 幂等键设计** | 多副本写 CDR 无主键冲突策略（ROADMAP §10 #2） | 重复话单/丢话单 | 必须先拍板：`uuid` 唯一键 + `INSERT IGNORE` 还是 `ON DUPLICATE KEY UPDATE` | 保留，本来就是 ROADMAP §10 未拍板项 |
| **Secret 轮换自动化** | 多人共享 `.env`/密钥，离职泄露风险高 | 安全事件 | Vault/Sealed Secrets，或 `deploy.sh --rotate` 自动轮换 | **待触发**（人类团队语义；`--rotate` 可先做，Vault 缓） |
| **API 契约测试** | Agent A 改 `/fs/dialplan` 响应格式，Agent B 前端/测试挂 | 无声破坏兼容性 | OpenAPI schema + `schemathesis` 契约测试，CI 强制跑 | 保留，后置到第 3 周（CI 建成后） |
| **性能基线** | 并发/延迟指标无基线，重构是否退化无感 | 性能倒退无感知 | 建 `benchmarks/`，PR 跑 sipp 压测对比 baseline | **依赖 M4 压测先行**——ROADMAP M4 未完成前无 baseline 可比，整体后置 |
| **On-call 轮值** | 生产故障谁响应 | 故障响应延迟 | `ONCALL.md` 按模块指定 Owner，周轮换 | **待触发**（人类团队语义） |
| **依赖升级策略** | `requirements.txt` 钉版本，安全漏洞处理 | 供应链风险 | `dependabot` + 定期统一升级窗口，破坏性升级走 Epic | 保留，成本极低可直接开 |
| **文档即代码** | README/交接文档/架构图与代码不同步 | 新人入坑 | 文档放仓库，PR 必须同步更新相关文档，CI 检查链接有效性 | 保留 |

---

## 7. 落地实施路线图（v1.1 修订：按方案 A 裁剪）

> 🔧 **v1.1 实质修订（@WorkBuddy）**：原第 1 周清单（CI+hooks+secret-scan+模板+PR 环境全上）过重；K8s/契约测试/性能基线整体后置或删除。修订后第 1 周只做**最小闭环**：跑通「两个 AI Agent 会话在各自独立 compose 栈上并行开发且互不干扰」。

| 阶段 | 时间 | 目标 | 关键交付物 |
|------|------|------|------------|
| **第 1 周** | 最小闭环 | 双 Agent 并行开发不互扰 | ① `deploy.sh --agent` 多栈生成（复用 pr1 已验证模式）② `.githooks/` 三件套 + `core.hooksPath` 激活 ③ PITFALLS append-only 纪律写入文档 ④ `docs/COLLAB_MEMORY.md` 骨架 + 模块 Owner 表 |
| **第 2 周** | 双 Agent 并行验收 | 两个真实任务各自独立栈完成四件套全流程 | 模块 Owner 表定稿、并行验收记录、踩坑回填 PITFALLS |
| **第 3 周** | CI 支撑 | GitHub Actions 基线 | `ci.yml`（pytest + secret-scan + 类型检查）；`schemathesis` 契约测试；PR 临时环境（compose project 版） |
| **后置** | 待触发/待依赖 | 按需启用 | M4 压测完成 → `benchmarks/` 基线；人类团队扩张 → On-call/Secret 轮换/Vault |
| **持续** | 治理优化 | 度量驱动改进 | 月度回顾：冲突率、回滚率、并行任务吞吐、坑位复发率、PR 周期 |

---

## 8. 附录：核心文件清单（v1.1 裁剪后）

```
SIP-SWITCH/
├── .github/
│   └── workflows/
│       └── ci.yml              # pytest + secret-scan + 类型检查（第 3 周）
├── .githooks/                  # 第 1 周
│   ├── pre-commit              # secret-scan + black/ruff/mypy
│   ├── pre-push                # pytest -x + secret-scan（固化 git-push-secret-scan 规则）
│   └── commit-msg              # conventional commits
├── PITFALLS.md                 # 避坑合集（按 #编号维护，append-only，关联 ROADMAP 任务）
├── docs/
│   ├── ROADMAP.md              # 进度唯一事实源（四件套更新）
│   ├── COLLAB_MEMORY.md        # v1.1 新增：协作约定/模块 Owner/环境现状（仓内记忆唯一源）
│   ├── ONCALL.md               # 待触发（人类团队）
│   └── CONTRACT_TESTS.md       # 契约测试用例索引（第 3 周）
├── benchmarks/                 # 后置：依赖 M4 压测
│   ├── baseline.json
│   └── run_bench.sh
├── deploy.sh                   # v1.1 增加 --agent 参数（--ephemeral/--rotate 后置）
└── ONBOARDING.md               # 新 Agent 入职清单（含环境搭建/必读文档/第一个 Task）
```

> 🔧 **v1.1 删除项（@WorkBuddy）**：`pr-env.yml`（K8s 版）、`daily-memory-sync.yml`（广播）、`.workbuddy/` 入仓条目、`docker-compose.pr.yml`（改为 `--agent` 生成）。被删项如需恢复，走文末变更流程。

---

## 9. 决策记录（供后续复盘）

| 日期 | 决策项 | 方案 | 备选方案 | 决策依据 |
|------|--------|------|----------|----------|
| 2026-09-12 | 记忆共享方案 | ~~三层架构 + GitHub Actions 同步~~ → **v1.1：仓内协作记忆（PITFALLS/ROADMAP/COLLAB_MEMORY，append-only）与 WorkBuddy 会话记忆分立** | Notion/Confluence 外部 Wiki | 代码即文档、版本可追溯、无额外工具依赖；WorkBuddy 记忆由客户端管理不可入仓 |
| 2026-09-12 | 环境隔离方案 | ~~本地轻量 + 共享集成 + PR 临时三层~~ → **v1.1：独立完整栈（compose project 为主体）+ PR 临时环境（后置）** | 全员本地全栈 / 全云端 | 「单 gateway 连共享 FS」被 ESL 事件模型否决；`sip-switch-*`+`sipswitch-pr1-*` 并存已实证 compose project 隔离可行 |
| 2026-09-12 | 分支策略 | Trunk-based + 短 feature 分支 | GitFlow / GitHub Flow | 适配「小步快跑、ROADMAP 为准」的节奏 |
| 2026-09-12 | 代码所有权 | 模块级 Owner + 跨模块 PR | 完全集体所有制 / 严格模块独占 | 平衡专业化与协作灵活性 |
| 2026-09-12 | **协作形态（v1.1 新增）** | **方案 A：多 AI Agent 会话并行先行**，人类团队项（On-call/Secret 人工轮换/强制人类 Review）标注待触发 | B：真多人协作优先 / C：混合 | 用户拍板；当前扩展主体是 AI Agent 会话，人类团队语义项延后避免过度工程 |

---

> **维护提醒**：本文档作为协作规范的「宪法」，任何流程变更需走「提 Issue → 讨论 → 更新本文档 → 同步全员」流程。**变更记录见文首修订说明；下次评审点：第 2 周末（双 Agent 并行验收后）。**
