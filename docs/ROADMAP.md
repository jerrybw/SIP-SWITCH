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
| **#64** | **网关-节点归属表 `gateway_node`（D11 多节点分片数据底座）** | **✅**  **补充(2026-09-10)**：(a) 列表页「归属节点」列由 `sec.fields` 自动生成，取值改用后端 `node_name`（`fs_node.host`，避免显示容器短 ID），点对点显示「全量」；(b) 归属节点下拉 `optt` 同步改 `host`；(c) 实测注册型网关**注册链路可用**：sipp 自定义 registrar 场景（收 REGISTER 回 200 OK）让 FS `sofia status gateway` 进入 `REGED`（此前 FAIL_WAIT 系 dev 数据 ip 误写成不存在的 `sipp-sub`，已修正为 `sipp-reg`）。 **跨节点下发同步（2026-09-10 落地，PITFALLS #44 已消除）**：`fs_provision` 改用 DB `system_setting` 做跨节点信令——`provision_seq`（每次网关增删改 +1）+ `provision_pending`（变更网关名 JSON，供精确 killgw）；各节点后台线程 `ProvisionWatcher`（周期热配 `provision_sync_interval`，默认 5s）发现版本变化即对本节点 FS 补做 killgw+rescan。选 DB 轮询而非直连其它节点 ESL 的原因：生产多节点通常只共享 DB/Redis、节点间未必互通，且离线节点回来后可自动补齐。验证：在 node1 的 Web 改点对点网关端口 -> FS2 约 5s 内自动跟上；注册型网关改归属 -> 新节点自动加载、旧节点自动卸载，均无需手动 rescan。|
| **#69** | **FS 节点级健康检查（DEP-6）+ Web 状态页 + Webhook 外部推送** | **✅** | `src/node_health.py`（ESL 探测）+ `src/alerting.py`（告警出口，`operation_log` 去重）；`fs_node` 扩 5 列；按 `NODE_UUID` 自注册 upsert；`online/offline/overload` 三态。**Phase1 只记录+告警、不摘除**（摘除留给 Phase2 选路按 node 过滤）。顺带闭环坑位 #18（落地网关心跳告警）+ #33（告警独立事务）。**2026-09-10 延伸**：① 前端新增「节点状态」tab（`src/static/admin.js` `renderNodes` + `src/templates/index.html`），展示各节点 UUID/地址/状态/并发/注册数/最后心跳，15s 自动刷新；② webhook 外部推送落地：`system_setting` 两项 `webhook_gateway_heartbeat_url` / `webhook_node_heartbeat_url`（落地网关 / FS 节点**分开配置**，空=不推送），`src/alerting.py::push_webhook`（urllib POST 企业微信 markdown，状态变化时才触发、异步线程、5s 超时），前端表单 + `POST /api/webhook-test` 测试；`src/api/app.py` 新增 `GET /api/nodes`。⚠️ 这两路由须注册在 `app.include_router(crud_router)` 之前，否则被 `/api/{entity}` 兜底吞掉（PITFALLS #34）。**已端到端验证**：`/api/nodes` 返回节点、`/api/webhook-test` 与企业微信 `errcode:0`、`alert_if_changed` 触发真实推送并落 `operation_log` |
| **#71** | **落地网关注册闭环 + 多节点重扫入口（D11 延伸）** | **✅** | ① **注册参数下发**：`gateway` 新增 `register_expire`(默认 600)/`register_retry`(默认 30)/`register_status`(默认 0 未注册)/`register_status_at`；`fs_sofia_config._gateway_xml` 对 `register=true` 的网关下发 `<param name="expire-seconds">`/`<param name="retry-seconds">`（`_int_or()` 夹取，0/NULL 回默认）。**实测生效**：下发前 `sofia status gateway` 为 `Expires 3600 / Freq 3600`（FS 自身默认），下发后 `Expires 600 / Freq 600`。② **注册状态实时回写**：新增 `src/gw_state.py`，订阅 ESL `CUSTOM sofia::gateway_state`（⚠️ 报头是 `Gateway`/`State`，非 `Gateway-Name`/`Gateway-State`），映射 REGED→1 / TRYING·REGISTER→2 / NOREG·DOWN·EXPIRED→0 / FAIL_WAIT·FAILED→3，仅对 `auth_type=1` 落库；启动时用 `sofia xmlstatus gateway` 做一次初值对齐（事件只在跃变时投递，进程重启后不会补发）。实测事件序列 `DOWN→TRYING→REGISTER(200 OK)→REGED` 全量落库。③ **多节点重扫入口**：`fs_provision.rescan_all()`/`force_all_nodes_rescan()` + `POST /api/provision/resync-all`（须注册在 crud_router 之前）+ 节点状态页顶部显眼卡片（版本号 / 最近变更网关 / 轮询周期 / **各节点同步位点** + 「立即全节点重扫」按钮）；全节点路径改为**真重建**（killgw 全部 + rescan），而非对存量无效的裸 rescan。④ 前端网关表单新增注册参数与「注册状态」徽标列、「状态更新时间」列（`noList` 机制避免列表膨胀，表头与单元格数列数已断言一致）。⑤ **各节点自报同步位点** `provision_seen_<NODE_UUID>`（判"是否真同步"的依据，见下方修复记录）。详见 PITFALLS #47/#48/#50/#51 |
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
## 2026-09-10 修复记录（dev 验证通过，已推送）

- **fix: CDR `fs_node_uuid` 落库取 `NODE_UUID`**（关联 #69 节点健康/CDR 关联）：原 `_save_cdr` 取 `ESL_CFG.get("fs_node_uuid")`，而配置 `esl.fs_node_uuid` 为空（死字段），导致所有 CDR `fs_node_uuid=null`、前端「FS节点」列无值。改为取 `core.config.NODE_UUID`（与 `node_health` 自注册同源）。`pre_insert_cdr` 半成品不填，但 HANGUP 路径 `_save_cdr` 经 `_persist_cdr` 整行 upsert 覆盖，故终态带值。验证：rebuild gateway 后新呼叫 `fs_node_uuid` 均带 NODE_UUID。
- **fix: 主被叫变换规则 `pattern` 前缀锚定**（用户需求）：原 `_translate_pattern_to_regex` 编译未加 `^`，`C?1` 会子串命中 `ccc1` 的 2-3 位；现加 `^` 前缀锚定，仅号首匹配。验证：容器内 `_apply_one` 实测 `ccc1` 不命中、`c11aa/cc143/cb12334` 前缀命中（命中后整体前缀替换为 replace_to、保留后缀）。
- **feat: 落地网关注册闭环（注册参数 + 实时注册状态）+ 多节点重扫入口**（用户需求 2026-09-10）：见 §8 `#71`。要点与实测证据：
  - **rescan 会立刻重注册（回答"rescan 之后 FS 会立刻重新发起注册吗"）**：会，而且是**亚秒级**。实测 killgw+rescan 后时间轴 `DOWN`(t+0.84s) → `TRYING`(t+1.83s) → `REGISTER 200 OK`(t+1.83s) → `REGED`(t+2.84s)，**不等 `retry-seconds`**（retry 只用于失败后重试）。前提是 **killgw + rescan**：裸 rescan 对已存在的 gateway 无效（#30），既不会重注册也不会换参数。
  - `register_expire` 下发为 `expire-seconds`：实测 FS 未收到该参数时默认 **3600**（不是 600），下发后变 600 —— 故"默认 600"必须在应用层显式下发。
  - 状态回写：`src/gw_state.py` 订阅 `CUSTOM sofia::gateway_state`（报头 `Gateway`/`State`），启动时用 `xmlstatus` 对齐初值；点对点网关不参与（不注册、无事件）。
  - 前端：节点状态页顶部新增「多节点下发同步」卡片（`provision_seq` / 待同步网关数 / `provision_sync_interval` 可改 + 「立即全节点重扫」），网关表单新增注册参数与只读「注册状态」徽标。
  - 验证：`resync-all` 后 seq 6→7、pending 清空，node2 在 ~5s 内 `full rebuild 3 gateway(s)` 且注册型网关保持 REGED；前端 16 项 DOM 冒烟断言全过（含表头/单元格列数一致）。

## 2026-09-10 修复记录（续，dev 验证通过）

- **fix: 节点同步卡片「待同步网关」措辞误导 → 改「最近变更」+ 新增各节点同步位点**（用户提问引出）：
  - **用户问题**：卡片显示「下发版本号 8 / 待同步网关 1 / 轮询周期 5」长期不变，"是展示问题还是确实没同步？"
  - **结论：确实同步了，是措辞误导。** `provision_pending` 是**只读的「最近变更网关名单」**——`ProvisionWatcher` 只读它来决定精确 `killgw` 哪些名字，**从不消费/清空**（唯一清空时机是「立即全节点重扫」）。因此它恒等于"最近一次变更涉及的网关"，与"谁还没同步"无关。
  - **修复**：① 卡片第二项改「最近变更」，并在下方把网关名以标签列出；② 新增**各节点自报同步位点** `provision_seen_<NODE_UUID>`（`fs_provision.report_seen()`，在处理完 seq / 首次上线对齐 / `force_all_nodes_rescan` 本节点分支时写入），卡片底部按节点显示「已同步 seq N」（落后=黄色「落后 N」，从未上报=灰色「未上报」）。**该位点才是"是否真同步"的判据**，节点离线时会自然停住。后端接口无需改动（`/api/sys-config` 是全部 key 直出）。
  - 验证：触发 resync-all（seq 9→10）后 10s 内两节点位点均变为 10；node1 的注册型网关重建后仍 `REGED / UP / Expires 600`（未掉注册）。
- **fix: 落地网关列表页新增「状态更新时间」列**（用户需求）：`register_status_at` 原被标 `noList: true` 不进列表，现放开（`fmt: 'time'` 按北京时间格式化）。
- **feat(dev 测试件): sipp 桩改造成单实例 Registrar + UAS**（用户需求）：`/root/sipp-reg/reg_uas.xml`（untracked）+ compose override 的 `sipp-reg.command`。
  - sipp 3.6 场景语法三坑：`<label>` 必须写 `id`（写 `name` 直接退出）；**没有** `<next label="x"/>` 元素；**不允许两条连续 optional recv**（"先等 REGISTER、收不到再等 INVITE"的双分支写法装不进去）。
  - 正解是**统一响应**：一条正则 recv 匹配 `REGISTER|INVITE|ACK|BYE|OPTIONS|...`，回同一个 `200 OK`（SDP body 让 INVITE 可接通，`Content-Length` 用 sipp 自动变量 `[len]` 动态算），加 `-rtp_echo` 回媒体。ACK/BYE/OPTIONS 必须一并匹配，否则撞上 mandatory recv 会让场景失败退出（桩死掉）。
  - 验证（FS1 / `test-register-gw`）：同一实例同时 `State REGED / Status UP / Expires 600`，且 `originate sofia/gateway/test-register-gw/9001 &echo()` → `callstate=ACTIVE / read_codec=PCMU / write_codec=PCMU`。
  - ⚠️ 这条**推翻了 PITFALLS #42** 早先"sipp 单实例无法三合一"的结论（详见 PITFALLS #50）。
