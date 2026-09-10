# SIP 软交换平台 — 路线图与进度（单一事实来源）

> **本文件是项目进度的唯一事实来源。** 判断"做到哪"一律以此文件为准，且**必须以代码验证为准**。
> 设计/需求/评审/复盘类"为什么"文档在**项目资产（tdrive）**；本文件只管"做到哪"。
> **铁律：判断进度一律以代码为准（grep 路由 / 函数 / 表），禁止凭记忆或文档推演。**

- 建立：2026-09-09（以 WSL 代码为基准完整盘点后建立）
- 代码基准：`/root/src/SIP-SWITCH`（DEV = WSL docker compose；PROD = Lighthouse 原生部署）
- 状态图例：✅ 已完成 ｜ ⚠️ 部分完成 ｜ ❌ 未开始

---

## 1. 总览

| 里程碑 | ✅ | ⚠️ | ❌ | 说明 |
|---|---|---|---|---|
| M1 核心通话（T-101~107） | 7 | 0 | 0 | 含录音；M1 尾巴（默认口令治理）已闭合 |
| M2 路由与多落地（T-201~208） | 8 | 0 | 0 | **全部完成**（tdrive 计划文档仍标未开始，已滞后） |
| M3 Web 管理端（T-301~307） | 5 | 2 | 0 | 缺口：角色校验 / 操作日志 / CDR 导出+录音下载 |
| 二期计费（P2-1~4） | 2 | 1 | 1 | 代码超前于计划；防欺诈未做 |
| 工程 P2-a/b/c | 0 | 1 | 2 | 受 Redis 未引入阻塞，见 §6 时序铁律 |
| 集群高可用（T-501~504） | 1 | 1 | 3 | #69 FS 节点健康检查已落地（探测+落库+告警）；多节点分发仍无 |
| M4 容量验证（T-401~404） | 0 | 0 | 4 | **上生产硬门槛，未开始** |
| P3 注册视图+多 FS | — | 1 | 1 | 注册目录已做，多 FS 未做 |
| P4 xml_curl+上云 | — | 1 | 1 | xml_curl 已用，上云未做 |

**核心结论**：功能面（M1/M2/M3）已基本齐活；真正未开始的是**规模化验证（M4）**与**高可用（集群）**。

---

## 2. M1 核心通话 — ✅ 全部完成

| 编号 | 任务 | 证据 |
|---|---|---|
| T-101 | FreeSWITCH 部署 | 容器 `sip-switch-freeswitch-1`，FS 1.11.2 |
| T-102 | 数据库初始化 + 核心表 DDL | 18 张表 |
| T-103 | 业务网关骨架（ESL/事件/REST） | `src/esl_client.py` + 纯 socket `src/fs_esl_socket.py` |
| T-104 | 对等接入 + IP 白名单 | `access_point` + `access_whitelist` + `register_host` 匹配 |
| T-105 | 单落地桥接 | gw-carrier-a/b provision + bridge |
| T-106 | 基础 CDR 落库 | `cdr` 表 |
| T-107 | 录音基础链路 | `record_path` / `record_status` |

- 默认口令治理 ✅（`ClueCon` 弱默认已移除，未注入则随机生成）
- FS systemd 托管：DEV 用 docker（不适用）；PROD 原生部署已有 `freeswitch.service`

## 3. M2 路由与多落地 — ✅ 全部完成

| 编号 | 任务 | 证据 |
|---|---|---|
| T-201 | 主被叫限制规则（`*`/`?`） | `build_allow_xml` / `build_deny_xml` / `build_empty_xml` |
| T-202 | 前缀↔落地 + 最长匹配 | `src/route/service.py:118` 排序 `(-len(prefix), -priority, id)` |
| T-203 | 允许/禁止落地 | `access_gateway_policy` + `_ap_gateway_allowed()` |
| T-204 | 心跳检查（OPTIONS/剔除/恢复） | `src/heartbeat.py`：UDP OPTIONS 按 `(ip,port)` 分组；连续 `FAIL_THRESHOLD` 次失败才置离线，任一次成功立即恢复 |
| T-205 | 故障切换分级 | `dialplan_xml.py` 恒定追 8 个 Q.850 cause；多候选逐腿 unrolled failover |
| T-206 | 并发上限三级 | `app.py:209-210 / 256-266` 全局 + AP + GW，超限 503 |
| T-207 | CDR 三段主被叫 + 计费时长 | `cdr` 表 + `billsec` |
| T-208 | CDR 重试 + 录音失败标记 | `main.py:26 start_cdr_reaper()` + `_spool_cdr()` + `record_status` |

> 已知限制：心跳异常**仅打日志，不接外部告警**（`heartbeat.py:13`）。

## 4. M3 Web 管理端 — 5 完成 / 2 部分

| 编号 | 任务 | 状态 | 证据 / 缺口 |
|---|---|---|---|
| T-301 | 登录鉴权 + 角色 + 操作日志 | ⚠️ | ✅ `/login` `/logout` `/me`；**❌ `sys_user.role` 建字段但无校验（只读可绕过）**；**❌ `operation_log` 建表无写入** |
| T-302 | 接入管理页 | ✅ | `/{entity}` 通用 CRUD + `/access-points/{id}/gateway-policies` |
| T-303 | 落地管理页 | ✅ | `gateway` / `carrier` CRUD |
| T-304 | 路由配置页 | ✅ | `prefix_route` |
| T-305 | 实时监控仪表盘 | ✅ | `/monitor/summary` + `/api/stats/concurrency` |
| T-306 | CDR 查询/导出/录音下载 | ⚠️ | ✅ 查询 `/cdr` `/cdr/{uuid}` `/api/cdr`；**❌ CDR 导出、录音下载/播放未做** |
| T-307 | 系统设置 | ✅ | `/sys-config` + `system_setting` |

**M3 三个真实缺口**：角色校验（安全）· 操作日志 · CDR 导出 + 录音下载

## 5. 二期计费（需求口径） — 2 完成 / 1 部分 / 1 未开始

| 编号 | 需求 | 状态 | 证据 / 缺口 |
|---|---|---|---|
| P2-1 | 费率管理 | ✅ | `rate` / `bill_unit` |
| P2-2 | 账户余额与实时扣费 | ✅ | `prepaid_enabled` + `account_ledger` + `/recharge` |
| P2-3 | 防欺诈 | ❌ | 无实现 |
| P2-4 | 报表 | ⚠️ | ✅ `/billing/summary` + `/billing/export` + `carrier_ledger`；缺完整利润分析视图 |

## 6. 落地网关不落盘（机制 A）— ✅ 完成

> 地基性改造：在 P2（并发可靠性）与生产重部署之前完成，使多 FS 节点拆分零成本。

- **机制**：FS 通过 mod_xml_curl 的 configuration 绑定，加载 sofia 配置时向网关应用请求 section=configuration&key_value=sofia.conf；网关应用按 gateway 表（单一事实来源）动态生成含 <gateways> 的完整 sofia.conf，FS 侧零落盘。新增 FS 节点只需把 xml_curl 指向同一网关端点，底层无需改动。
- **其余配置**（acl/event_socket/modules 等）回空文档，FS 按 mod_xml_curl 标准回退行为使用磁盘文件，保持原行为不变。
- **代码证据**：
  - src/fs_sofia_config.py（新增）：build_sofia_conf(db) 按 DB 生成 sofia.conf；build_config_response(key_value, db) 仅对 sofia.conf 动态下发。
  - src/api/app.py：新增 GET/POST /fs/config 端点；鉴权白名单加入 /fs/config。
  - deploy/fs-config/autoload_configs/xml_curl.conf.xml：新增 <binding name=configuration> 指向 /fs/config，cacheable=false。
  - src/fs_provision.py / src/gw_bootstrap.py：移除写盘逻辑，仅保留 sofia profile external rescan 触发（FS 重新拉取即生效）。
  - deploy/fs-config/sip_profiles/external.xml：<gateways> 置空（不再 include 磁盘网关 XML）。
  - docker-compose.yml：gateway 服务增加 EXT_SIP_IP / FS_CONFIG_RO 环境变量与 ./deploy/fs-config:/fs-config-ro:ro 只读挂载。
- **dev 验证（2026-09-09）**：清空 FS 磁盘网关 XML 后重启 FS，sofia status 仍显示 external::gw-carrier-a 指向 sipp-stub:5060（NOREG）；经该动态网关 originate 出局到 sipp-stub 成功（call ACTIVE、UAS 应答）；FS 日志无 Unable to find 回退；CDR 正常写入。证明零落盘 + 出局可达。
- **遗留清理已完成（2026-09-09）**：原 fs-profiles 共享卷已确认空挂（实测 0 文件）。
  - 已从 `docker-compose.yml` 移除 4 处无用配置：freeswitch 的 `fs-profiles` 挂载（含过时注释）、
    gateway 的 `FS_SIP_PROFILES_EXTERNAL=/fs-profiles` 环境变量、gateway 的 `fs-profiles:/fs-profiles` 挂载、卷定义。
  - `sipp-stub` 段仍提"provision 写 gateway XML"与 `id=9` 的过时注释已同步修正为机制 A 描述。
  - 空卷已 `docker volume rm sip-switch_fs-profiles`（删除前复核 0 文件）。
  - **回归实测**：重建容器后 `gw-carrier-a` 仍由 xml_curl 下发（`sofia status` 可见），
    经其 originate 到 sipp-stub 出局 1 路 ACTIVE，机制 A 完好。
- **⚠️ 已知无关项（勿误判为回归）**：`sofia status` 中的 `external::example.com`（`sip:joeuser@example.com`）
  来自镜像 `vars.xml` 的 `default_provider=example.com` + `directory/default/example.com.xml`，是 vanilla 桩网关。
  **早于不落盘改造即存在**——共享卷只遮罩 `sip_profiles/external`，从不遮罩 `directory/`。
  不影响业务：选路只走 DB 的 `prefix_route` → `gateway`，该桩永不入选。

## 7. 工程 P2（并发可靠性）与时序铁律

| 编号 | 内容 | 状态 | 证据 / 缺口 |
|---|---|---|---|
| P2-a | 状态外移 Redis + 原子预留（D7） | ❌ | **Redis 未引入**（`src/` 与 `requirements.txt` 均无） |
| P2-b | fail-close / spool 补偿 / 逃生开关 | ⚠️ | ✅ 并发预检超限 503（`app.py:256-266`）、✅ spool + reaper（`_spool_cdr` / `start_cdr_reaper`）；fail-open 逃生待确认 |
| P2-c | 并发作为**选路因子**（D8 v1.3） | ❌ | 见下方"易混淆"说明 |

> ⚠️ **时序铁律（不得颠倒）：P2-a → P2-b → P2-c**
> b/c 都依赖"准入时即时准确"的并发值，而现有计数是**事件驱动**（仅 A 腿、需等 `CHANNEL_CREATE`）。
> **未改造就启用 P2-c 会把流量引向实际已满的网关** —— 比不做更糟。

### ⚠️ 易混淆：并发「预检」≠ 并发「选路因子」

| 能力 | 状态 | 说明 |
|---|---|---|
| **并发预检（fail-close）** | ✅ 已完成 | 三档（全局/AP/GW）超限 → 直接 503 拒绝，`app.py:256-266` |
| **并发作为选路因子** | ❌ 未实现 | 需"满了自动换下一个候选"。实际是 `app.py:241 gw = candidates[0]` 只取首个候选，超限即 503 **不回退**；`route/service.py:118` 排序**无并发项**；`_enrich_candidates` 不做并发过滤 |

**当前真实生效的选路因子**：① `status==1` ② **心跳状态**（离线直接剔除候选池，`route/service.py:100`）③ 前缀最长→priority→id ④ 接入点↔落地 allow/deny 策略

## 8. 集群高可用 / M4 容量验证 / P3 / P4

| 编号 | 任务 | 状态 | 证据 / 缺口 |
|---|---|---|---|
| T-501 | FS 集群第二节点 + 健康分发 | ❌ | `fs_node` 表已建，仅 `esl_client.py:539` 传单节点 uuid，**无多节点分发** |
| T-502 | FS 配置一致性管理 | ❌ | 无（R-03 定案 git+脚本下发，未落地） |
| T-503 | DB 主从读写分离 | ❌ | 单 MySQL |
| T-504 | 在途通话中断 CDR 兜底 | ⚠️ | spool+reaper 覆盖 DB 抖动，**未覆盖节点故障** |
| **#64** | **网关-节点归属表 `gateway_node`（D11 多节点分片数据底座）** | **✅** | 已定义并落库（commit `ea5c155`）：`ensure_gateway_node_table` 幂等建表 + `01-schema.sql` + `session.py` 启动注册；dev 库已建表、gateway 重启无报错。`uk_gateway(gateway_id)` 保证注册型网关(auth_type=1)单选唯一归属节点；FK→`gateway.id`/`fs_node.node_uuid`(ON DELETE CASCADE)。点对点网关(auth_type=0)全量下发不在此表。**剩余（#64 未完）**：① `route/service.py` 按 NODE_UUID 筛选候选网关；② gateway CRUD upsert `gateway_node`（auth_type=1 必填 node_uuid，切 0 删行）+ 应用层校验同 node 下 `ip+port+注册用户名` 三要素唯一；③ 前端网关表单节点控件 |
| **#69** | **FS 节点级健康检查（DEP-6）+ Web 状态页 + Webhook 外部推送** | **✅** | `src/node_health.py`（ESL 探测）+ `src/alerting.py`（告警出口，`operation_log` 去重）；`fs_node` 扩 5 列；按 `NODE_UUID` 自注册 upsert；`online/offline/overload` 三态。**Phase1 只记录+告警、不摘除**（摘除留给 Phase2 选路按 node 过滤）。顺带闭环坑位 #18（落地网关心跳告警）+ #33（告警独立事务）。**2026-09-10 延伸**：① 前端新增「节点状态」tab（`src/static/admin.js` `renderNodes` + `src/templates/index.html`），展示各节点 UUID/地址/状态/并发/注册数/最后心跳，15s 自动刷新；② webhook 外部推送落地：`system_setting` 两项 `webhook_gateway_heartbeat_url` / `webhook_node_heartbeat_url`（落地网关 / FS 节点**分开配置**，空=不推送），`src/alerting.py::push_webhook`（urllib POST 企业微信 markdown，状态变化时才触发、异步线程、5s 超时），前端表单 + `POST /api/webhook-test` 测试；`src/api/app.py` 新增 `GET /api/nodes`。⚠️ 这两路由须注册在 `app.include_router(crud_router)` 之前，否则被 `/api/{entity}` 兜底吞掉（PITFALLS #34）。**已端到端验证**：`/api/nodes` 返回节点、`/api/webhook-test` 与企业微信 `errcode:0`、`alert_if_changed` 触发真实推送并落 `operation_log` |
| T-401~404 | SIPp 压测 / 调优 / 切换演练 / 报告 | ❌ | 仅 `deploy/sipp-stub`（功能测试桩，**非压测脚本**）。M4 是上生产硬门槛，DEV 单机跑不了真实规格 |
| P3 | 注册视图 + 多 FS | ⚠️ | ✅ `sip_phone` + `/fs/directory` 动态目录；❌ 多 FS 分发（同 T-501） |
| P4 | xml_curl configuration + 上云 | ⚠️ | ✅ 已用 xml_curl（`/fs/dialplan` + `/fs/directory`）；❌ 上云 |

---

## 9. ⚠️ 已知陷阱（判断进度前必读）

1. **"建表未实现"陷阱**：`fs_node`、`operation_log`、`sys_user.role` 三处**表/字段已建但无业务逻辑**，只看表会误判为"已完成"。判断时必须查"有没有代码用它"。
2. **计划文档滞后**：tdrive《任务拆解与测试计划 V1.0》是 2026-08-27 快照，M2/M3 全标未开始，实际已完成。**勿以该文档判断进度。**
3. **两套 P 命名**：需求规划里的 P0/P1/P2 = 需求优先级（P2=计费二期）；本文档 §6 的 P2-a/b/c = 工程阶段。勿混。
4. **DEV/PROD 分叉**：PROD 是**原生部署无 docker**，DEV 的 compose/entrypoint 改动不会自动影响生产。

---

## 10. 待拍板

| # | 问题 | 阻塞什么 |
|---|---|---|
| 1 | **Redis 高可用形态**（Sentinel / Cluster / 云托管） | 整条 P2 链；引入后 Redis 是强依赖，fail-close 下不可用 = 全站拒呼，高可用从建议变**强制** |
| 2 | 二期计费提前完成是否有意决策 | 算"已完成"还是"待评审" |
| 3 | M4 压测是否准备云上规格（FS 16C32G + 同地域压测机） | M4 能否启动 |
| 4 | P2-c 打标字段（`gw_overflowed` + 原始首选 `gw_id`）落新列 or 扩展字段 | 撤销"等价候选"限定后，打标**由建议升级为必需**（费率可追溯性唯一保障） |
| 5 | 生产策略：继续"等 dev 单机+多机成熟后按成熟方案重部署" | 当前决策=等重部署 |

---

## 11. 回填纪律（防再次漂移）

**"完成"的定义 = 以下四件事一次做完，缺一不算完成：**

1. **改代码**（功能实现）
2. **更新本文件**：把对应条目的状态改为 ✅，并写明**代码证据**（文件:行号 / 接口 / 表名）
3. **更新任务列表**：把对应 TaskList 条目标记为 completed
4. **更新项目记忆**：若涉及关键状态或新坑，同步 `.workbuddy/memory/MEMORY.md`

**禁止**：凭记忆或旧文档推演进度；只建表/建字段就标记完成（见 §8 陷阱 1）。

**验证方法**：判断某能力是否实现，看它**进没进关键函数的过滤/排序/写入**——存在配置项 ≠ 已实现。

---

## 12. 关联文档（"为什么"类，在项目资产 tdrive）

| 文档 | 用途 |
|---|---|
| `PRD-SIP软交换平台-V1.0` / `需求规划-第二版` | 需求口径 |
| `任务拆解与测试计划-V1.0` | ⚠️ 2026-08-27 快照，**进度已滞后，勿作依据** |
| `P0-pure-socket-esl-and-concurrency-failclose.md`（v1.4） | P0/P2 详细设计；D7/D8 决策（事项 `rE5ICg`） |
| `设计方案-接入点落地网关Web与限制逻辑落地` / `计费方案设计_v0.3` / `T301_鉴权设计方案` | 各模块设计 |
| `复盘纪要-*` / `部署runbook-2C2G开发机` | 复盘与部署 |
