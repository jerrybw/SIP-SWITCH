# COLLAB_MEMORY — 协作记忆与资产密级约定

> **版本**：v1.1（2026-09-12，作者 @WorkBuddy；v1.1 增补提交归因与 token 纪律）
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

## 4. 模块 Owner（v1.0 骨架，随并行开发补齐）

| 模块 | Owner | 说明 |
|------|-------|------|
| `api/app.py` 路由/选路 | （待派） | dialplan 预检/预留、P2-c 重排 |
| `esl_client.py` ESL/CDR/计费 | （待派） | 事件异步化、reconcile |
| `fs_sofia_config.py` / `fs_provision.py` FS 对接层 | （待派） | 机制 A 下发；**此层单一 Owner**，见协作方案 §6 |
| `concurrency.py` 并发预留 | （待派） | P2-a Redis Lua |
| 前端 `static/` | （待派） | SECTIONS 驱动 |

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

