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
| M3 Web 管理端（T-301~307） | 5 | 2 | 0 | 缺口：角色校验 / 操作日志 / CDR 导出（录音下载/播放已闭环 #70） |
| 二期计费（P2-1~4） | 2 | 1 | 1 | 代码超前于计划；防欺诈未做 |
| 工程 P2-a/b/c | 0 | 1 | 2 | 受 Redis 未引入阻塞；**✅ Redis 形态已拍板=云托管（2026-09-11），P2-a 可启动**，见 §6 时序铁律 |
| 集群高可用（T-501~504） | 1 | 1 | 3 | #69 FS 节点健康检查已落地（探测+落库+告警）；**心跳超时判定 B1+B2 已修（#73，僵尸在线闭环）**；多节点分发仍无 |
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
| T-204 | 心跳检查（OPTIONS/剔除/恢复） | `src/heartbeat.py`：UDP OPTIONS 按 `(ip,port)` 分组；连续 `FAIL_THRESHOLD` 次失败才置离线，任一次成功立即恢复。**探测周期**取所有启用心跳网关的 `MIN(heartbeat_interval)`（每轮现读 DB，`heartbeat.py::_plan_interval`；2026-09-10 修死配置） |
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
| T-306 | CDR 查询/导出/录音下载 | ⚠️ | ✅ 查询 `/cdr` `/cdr/{uuid}` `/api/cdr`；**✅ 录音播放/下载（#70，2026-09-11）**：`GET /api/cdr/{uuid}/recording`（文件流+Range 206，支持 `?download=1`）+ `/recording/meta`；录音改 URI 抽象 `local://<node_uuid>/<file>`，共享卷 `./data/recordings`；**❌ CDR 导出未做** |
| T-307 | 系统设置 | ✅ | `/sys-config` + `system_setting` |

**M3 真实缺口**：角色校验（安全）· 操作日志 · CDR 导出 ｜ **录音下载/播放已闭环（#70，2026-09-11）**

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
| P2-c | 并发作为**选路因子**（D8 v1.3） | ⚠️ **部分完成**（打标 ✅ / 选路 ✅ 基础版 / 原子预留 ❌ 依赖 P2-a） | 见下方"易混淆"说明 |

> ⚠️ **时序铁律（不得颠倒）：P2-a → P2-b → P2-c**
> b/c 都依赖"准入时即时准确"的并发值，而现有计数是**事件驱动**（仅 A 腿、需等 `CHANNEL_CREATE`）。
> **未改造就启用 P2-c 会把流量引向实际已满的网关** —— 比不做更糟。

### ⚠️ 易混淆：并发「预检」≠ 并发「选路因子」

| 能力 | 状态 | 说明 |
|---|---|---|
| **并发预检（fail-close）** | ✅ 已完成 | 三档（全局/AP/GW）超限 → 直接 503 拒绝，`app.py` `_route_via_ap` ⑤ 段 |
| **并发打标（可追溯性）** | ✅ **已完成（2026-09-10）** | 复用 `switch_detail` 扩展字段（**未加新列**）：元素由 `gid:callee_out:cause` 扩为 `gid:callee_out[:conc_gw[:conc_limit]]:cause`，记录该腿**进入时**的并发快照；单候选路径同样打标（`_single_leg_detail`）；解析端 `esl_client._parse_switch_detail` 改**从右往左**解析以兼容老 3 段记录；前端 `fmtSwitchDetail` 追加 `[并发 x/上限y]` 标注。并发 503 的 `reject_reason` 亦改为机器可读串 `busy_limit_gw;gw=..;gw_conc=..;gw_limit=..;g_conc=..;g_limit=..`（`app._conc_detail`） |
| **并发作为选路因子（回退）** | ⚠️ **基础版已完成（2026-09-10）** | `app._order_by_concurrency`：候选池按「是否已打满」**稳定重排**（未满的排前），首选满则自动降级到同池有余量的网关，**全满才 503**；`_enrich_candidates` 附带 `conc_gw/conc_limit/at_capacity`。⚠️ 仍**依赖事件驱动的近似计数**，未接 P2-a 原子预留 → 并发瞬时误差下仍可能超发（P2-a 才是根治） |

**当前真实生效的选路因子**：① `status==1` ② **心跳状态**（离线直接剔除候选池，`route/service.py:104`）③ 前缀最长→priority→id ④ 接入点↔落地 allow/deny 策略 ⑤ **并发未打满优先**（`_order_by_concurrency`，P2-c 2026-09-10）⑥ **网关主被叫规则**（候选池过滤，不通过即剔除该网关、回退同池下一候选，#74 2026-09-11）

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

1. **"建表未实现"陷阱**：`sys_user.role` 仍是**字段已建但无业务逻辑**，只看表会误判为"已完成"。（`fs_node` / `operation_log` 已分别在 #69 与 #69/#73 接上业务逻辑，不再是空壳。）判断时必须查"有没有代码用它"。
2. **计划文档滞后**：tdrive《任务拆解与测试计划 V1.0》是 2026-08-27 快照，M2/M3 全标未开始，实际已完成。**勿以该文档判断进度。**
3. **两套 P 命名**：需求规划里的 P0/P1/P2 = 需求优先级（P2=计费二期）；本文档 §6 的 P2-a/b/c = 工程阶段。勿混。
4. **DEV/PROD 分叉**：PROD 是**原生部署无 docker**，DEV 的 compose/entrypoint 改动不会自动影响生产。

---

## 10. 待拍板

| # | 问题 | 阻塞什么 |
|---|---|---|
| 1 | ~~**Redis 高可用形态**（Sentinel / Cluster / 云托管）~~ **已拍板 2026-09-11：云托管** | ✅ 形态已定，P2-a 解除阻塞。注意：引入后 Redis 是强依赖，fail-close 下不可用 = 全站拒呼，云托管高可用为**强制**前提 |
| 2 | ~~二期计费提前完成是否有意决策~~ **已拍板 2026-09-11：算「待评审」**（代码超前于计划，评审通过后才记「已完成」） | 进度口径=待评审 |
| 3 | M4 压测是否准备云上规格（FS 16C32G + 同地域压测机） | M4 能否启动 |
| 4 | ~~P2-c 打标字段（`gw_overflowed` + 原始首选 `gw_id`）落新列 or 扩展字段~~ **已拍板 2026-09-10：走扩展字段，复用 `switch_detail`**（不加列） | ✅ 已落地 |
| 5 | 生产策略：继续"等 dev 单机+多机成熟后按成熟方案重部署" | 当前决策=等重部署 |
| 6 | `concurrent_limit_global` 无配置入口（全局并发限制恒 0） | 全局并发限制实际不生效，需补配置入口 |
| 7 | 多副本 CDR 幂等（选主 vs 幂等键） | 网关多副本部署时 CDR 去重 / 防重复计费 |

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

## 2026-09-10 修复记录（三，dev 验证通过）

本轮用户拍板四项，全部落地并 dev 实测。

### 1. fix: `heartbeat_interval` 死配置 → 真正生效（PITFALLS #53 闭环）

- **问题**：`gateway.heartbeat_interval` 在 DB 列 / schema DDL / 前端表单 / CRUD 写白名单**四处都有**，但**全仓无读点**——`heartbeat.py` 从不引用它，实际周期硬编码在 `main.py:31` 的 `HeartbeatProber(interval=30)`。改多少都还是 30s。三处默认值还互相打架（DB/schema=10、前端=10、运行=30）。
- **修复**（`src/heartbeat.py`）：新增 `_plan_interval(db)` —— 每轮探测完**现读 DB**，取所有 `status=1 AND heartbeat_enabled=1` 网关的 **`MIN(heartbeat_interval)`** 作为下一轮睡眠时长；`_clamp()` 夹取到 `[MIN_INTERVAL=5, MAX_INTERVAL=3600]`（防 0 打满 CPU / 配太大导致假死无感知）；DB 读不到时回落 `DEFAULT_INTERVAL=30`。`HeartbeatProber.interval` 构造参数**退化为兜底值**，`main.py` 改为 `HeartbeatProber()`（不再硬编码 30）。
- **粒度取舍**：现有实现是「单线程、一轮探所有网关、按 (ip,port) 分组」；而字段是 **per-gateway**。取 `min` 的意义是**粒度最细者的周期被严格遵守，其余只会被更频繁地探测**（偏保守、不会漏探测），且不引入 per-gateway 定时器重构。这是本轮明确的权衡，非疏漏。
- **三处默认值统一为 30**：`src/db/models.py:91`、`deploy/mysql/init/01-schema.sql:245`、`src/static/admin.js:67`。
- **实测**：三网关全设 30s → 采样 `last_heartbeat_time` 约 30s 一跳；全设 20s → 实测跳变间隔 ≈20s（`09:21:04→09:21:24→09:21:44`）；全设 3s → 间隔缩短到 ~3-7s。**字段真正生效**，改配置无需重启进程（每轮现读）。

### 2. fix: `provision_pending` 截断导致漏重建 → 位点跳跃时改走全量

- **问题**：`bump_pending` 在 JSON 超 `_MAX_PENDING_JSON=480` 字符时 `merged.pop(0)` **丢最老的名字**（`system_setting.value` 是 varchar(512)）；而 watcher 只在 pending **为空**时才全量 —— 某节点长期离线、期间变更过多（约 >24 个网关）时回来，被丢弃的网关**永远不会被重建**。
- **修复**（`src/fs_provision.py` `_sync_once`）：新增 `jumped = (seq - self._last_seq) > 1`，`if pending and not jumped` 才走精确 `resync(pending)`，否则落入全量 `rescan_all()`；日志区分原因 `full rebuild (seq jumped (pending may be truncated))` vs `(no pending names)`。
- **实测**：seq 12→16（跳跃，pending 非空）→ 日志 `full rebuild (seq jumped (pending may be truncated))`；seq 16→17（相邻）→ `resync ['test-register-gw']`（精确路径保留）。两条路径均正确。

### 3. feat: P2-c 并发打标 + 并发选路（扩展字段，复用 `switch_detail`）

- **拍板**：打标字段走**扩展字段**、**复用 `switch_detail`**，不加新列（ROADMAP §10 #4 已闭环）。
- **打标格式**：元素由 `gid:callee_out:cause` 扩为 `gid:callee_out[:conc_gw[:conc_limit]]:cause`（并发段在中间，cause 恒为最后一段）。
  - 多腿：`dialplan_xml._leg_ext` 的 `gw_detail` 从 `app._enrich_candidates` 带来的 `conc_gw/conc_limit` 拼入；**值在网关层算好，dialplan 只搬运**（守住"决策全在网关层"铁律）。
  - 单候选：新增 `dialplan_xml._single_leg_detail()`，让「切换明细」列在无 failover 时也有并发快照可看（此前恒空）。
- **解析兼容**：`esl_client._parse_switch_detail` 改**从右往左**解析（末段恒为 cause，中间段按位置映射），老 3 段记录照常解析、新 5 段带上 `conc_gw/conc_limit` 键；无并发键时不塞默认 0（避免"看起来并发是 0"的误读）。WIN 兜底补腿同样不塞 conc 键。
- **选路（D8 落地）**：新增 `app._order_by_concurrency()` —— 候选池按「是否已打满」**稳定重排**（Python 排序稳定，同组内保持 原前缀/优先级 语义），首选满则自动降级到同池还有余量的网关，**全满才 503**。`_phone_branch` 与 `_route_via_ap` 双路径均已接入。
- **503 机器可读**：新增 `app._conc_detail()`，`reject_reason` 由裸 `busy_limit_gw` 改为 `busy_limit_gw;gw=7;gw_conc=5;gw_limit=5;g_conc=12;g_limit=20;ap=5;ap_conc=3`。
- **前端**：`admin.js fmtSwitchDetail` 在有 `conc_gw` 时追加 ` [并发 x/上限y]`（`conc_limit=0` 显示 `∞`）。
- **实测（真实呼叫）**：sipp 当 UAC 呼 `cc8888`（候选池 `[testgw, testgateway]`），首腿 gw8 失败、次腿 gw7 胜出，CDR id=64 落库：
  ```json
  [{"gateway_id":8,"callee_out":"cc8888","cause":"NORMAL_TEMPORARY_FAILURE","conc_gw":0,"conc_limit":0},
   {"gateway_id":7,"callee_out":"cc8888","cause":"WIN"}]
  ```
  同时核对：单候选路径下发 `cdr_switch_detail=;7:ccc9999:0:0:WIN`；多腿路径两条腿均带 `:0:0:` 并发段。解析器往返一致性单测通过（老/新/混合格式）。

### 4. chore: 纪律更新 —— 待办只认 ROADMAP

- 用户拍板：**忽略 wb-issues 看板的「待开始」7 项，今后待办事实来源只认本 ROADMAP**。已写入 `.workbuddy/memory/MEMORY.md`。


## 2026-09-11 修复记录（一：录音 URI，#70，dev 验证通过，已推送）

### #70 录音 URI 抽象（落地 `local://`，预留 `cos://`）— 闭环 M3 T-306「录音下载/播放」

**背景**：T-306「录音下载/播放」此前未做；且录音落点写死在容器内 `${recordings_dir}/${uuid}.wav`（FS 本地路径），CDR 只存裸文件名——既无法定位到具体节点，也无法平滑上云。

**决策（设计文档见工作区 `设计方案-录音URI抽象-v1.md`，用户 2026-09-11 拍板 4 项）**：

1. **不加列**：`cdr.record_path` 由「裸文件名」升格为 **URI**（老裸路径按 `local://<归属node_uuid>/<file>` 隐式解释），不为 URI 另开列。
2. **多节点先 409**：请求他节点录音且本节点**不可达**时返回 409（`recording_remote_node`），不静默 404；若共享卷可达则正常 200。
3. **上云幂等键**（设计内）：cos 对象键 = `<prefix>/<uuid>.wav`，天然幂等；本期仅预留 `public_url` 接口，不实装。
4. **本期不转码**：录音保持 FS 原始 wav，不引入转码。

**URI 形制（三段式）**：`<scheme>://<authority>/<name>`

- 本地：`local://<node_uuid>/<file>`（authority = **归属节点**，便于定位/排障/前端告警）
- 上云：`cos://<bucket>/<prefix>/<file>`（**仅换 scheme**，前端 / CDR / 端点契约零改）
- 老数据：裸文件名 → 隐式 `local://`，authority 取该行 `cdr.fs_node_uuid`（缺失则不补）

**代码证据**：

- `src/recordings.py`（新增）：纯函数无 DB 依赖 —— `is_uri / to_uri / parse / resolve / stat_local / public_url`（cos 恒 `remote=False`）。
- `src/core/config.py`：`record.dir`（默认 `/recordings`）/`record.local_root`/`record.backend` 落地为模块级 `RECORD_DIR/RECORD_ROOT/RECORD_BACKEND`（**死配置复活**，与 #53 同型）。
- `src/api/dialplan_xml.py`：录音落点改固定挂载点 `RECORD_DIR/<NODE_UUID>`。
- `src/esl_client.py`：CDR `record_path` 落库改 `to_uri(rec_file, NODE_UUID)`，upsert 幂等兜底同样 to_uri。
- `src/api/app.py`（路由注册在 `crud_router` **之前**，#34）：`GET /api/cdr/{uuid}/recording/meta`、`GET /api/cdr/{uuid}/recording`（`public_url` 非空→302 签名直链；`resolve` 后 `os.path.isfile`→FileResponse（Starlette 自带 Range 206），`?download=1` 带 `Content-Disposition`；文件缺失且 `remote`→409；否则 404）。CDR list 对 `record_status=1` 行附 `record_ok/record_remote`。
- `src/static/admin.js`（`?v=20260911a`）：CDR 列新增 `_rec` 虚拟列（播放/下载/缺失/异节点四态）+ `playRecording` 弹层；`CDR_FORCE_PUSH` 强推给老用户（解决列固化不可见）。
- `docker-compose.yml` / `docker-compose.override.yml`：fs/gateway/fs2/gateway2 同挂 `./data/recordings`（FS rw、网关 ro），按 `/<root>/<node_uuid>/` 分片 → 容器重建不丢、多节点共享。
- `deploy/fs-config/docker-entrypoint-fs.sh`：启动 `mkdir -p ${RECORD_DIR:-/recordings}`；`config/docker/config.example.yaml` 增 record 段；`.gitignore` 加 `/data/`。

**验证（单测 + 端到端）**：

- `recordings.py` 单测全过（含 cos 恒 `remote=False`、共享卷可达不误判 remote 两处修正）。
- **31 个存量 wav**（fs1 20 + fs2 11）从容器可写层 `docker cp` 迁到 `data/recordings/<node_uuid>/`，md5 一致。
- **真实呼叫** CDR 66 落 `local://2a5f89f1b0f0ce74/2d5e3025-….wav`，宿主盘出现同名文件（录音链路真正闭环）。
- **API 6/6**：meta / 流式 / Range 206 / download / 存量兼容 / 未录音 404 / 未知 404 / 文件缺失 404（**非 500**）/ 异节点 409。
- **持久性回归**：`docker compose up --force-recreate freeswitch` 后录音 md5 不变、API 仍 200。
- 前端 11/11（`fmtRecCell` 四态 + 列定义 + `playRecording` + `closeModal` 恢复）。

**交付（2026-09-11）**：本项代码已提交并推送远端（`feat(#70): 录音 URI 抽象 local:// + 录音下载/播放闭环（T-306）`）；推送前已过 `git-push-secret-scan`（真实 IP / 密钥 0 命中）。

---

## 2026-09-11 修复记录（二：节点心跳超时判定 B1+B2，#73，dev 验证通过，代码待提交）

### 背景：用户报「fs2 没起来，但 Web 显示 node2 在线」

排查出**两个独立根因**，第 2 个是本次修复对象（#73）：

1. （已修，运维层）node2 侧 `freeswitch2`/`gateway2`/`sipp-reg` 只定义在 untracked 的
   `docker-compose.override.yml` 里，**当初没写 `restart`**（默认 `no`）→ docker daemon
   重启后 node2 侧**永不自动恢复**。已补 `restart: unless-stopped`。
   ⚠️ 排查陷阱：`docker compose ps` **默认只列 running**，看起来像"这些服务不存在"，
   必须 `docker ps -a` 才看得到 Exited。
2. （本次修复）**`fs_node.status` 是"最后写入值"**，写入方 = 该节点自己的网关进程。
   node2 的 gateway2 一死，**就再没有写入方**去改它那一行 → `status` 永远停在 1，
   形成**僵尸在线**（实测 `last_heartbeat_at` 精确停在容器被关停那一刻，`status` 仍是 1）。

> 不只是显示问题：**Phase2 选路一旦按 `fs_node.status` 过滤节点，会把流量发给僵尸节点** —— 比不选路更糟。

### 实现

| 层 | 位置 | 内容 |
|---|---|---|
| 共用判定 | `src/node_health.py::evaluate()` | 纯函数：按 `last_heartbeat_at` 现算 `(stale, age_seconds, effective_status)`；`last_heartbeat_at` 为空回落 `created_at`（新行宽限）；naive 时间**按 UTC 解释**（MySQL DATETIME 无时区，按本地时区会平移 8h 全错） |
| 阈值 | `src/node_health.py::stale_threshold()` | 自动 `max(3×node_health_interval, 90s)`；显式 `node_health_stale_threshold`（第2类热配）**下限 3×探测周期**，被钳制时打 warning（不静默忽略） |
| **B1 展示层** | `src/api/app.py::list_nodes` (`GET /api/nodes`) | 每行附 `stale` / `stale_seconds` / `effective_status`（超时强制 offline），并回 `heartbeat_threshold`；**原 `status` 保留原值**便于排查 |
| **B2 落库层** | `src/node_health.py::_sweep_stale_nodes()`，由 `NodeHealthProber._run` 每轮调用 | 任一**存活**节点的探测线程巡检 DB，把超时的**非本节点**行置 0 并走 `alert_if_changed` 告警（`reason=heartbeat_timeout`）。DB 共享 → 别人能替它改 |
| 告警文案 | `src/alerting.py` | 补 `stale_seconds` / `threshold` / `reason`（可读化 `heartbeat_timeout`） |
| 前端 | `src/static/admin.js` | 节点表按 `effective_status` 渲染 + 「心跳超时」标记 + 最后心跳列显示「已超时 Ns」；位点卡片同样标记（`?v=20260911b`） |

**两条必须守住的设计不变量**（都是实测踩出来的，见 PITFALLS #63/#64）：

- **阈值 ≥ 3×探测周期**：心跳是每个周期写一次，阈值 ≤ 周期时**健康节点会在下一次心跳到来前被对端判离线** → 互判、来回翻转、刷告警。
- **周期变更必须 ≤5s 生效**（`_wait_interval` 分片等待 + 每片重读配置）：否则改小周期后节点仍按旧的长节奏写心跳，同样触发上面的误判。仅"分片"但 deadline 定死是**伪修复**。

### 验证（dev，2026-09-11）

- **单测 19/19 PASS**（容器内真实加载 `node_health`）：新鲜/超时/阈值边界(90/91)/回落 `created_at`/naive-UTC(10s 与 200s 两向)/overload 超时强制离线/status=0 不被误升/未来时间/ dict 入参/阈值钳制 5 例。
- **端到端 18/18 PASS**（真实停掉 node2 侧容器复现原故障）：
  - B1 独立证据：`t+03s raw=1 stale=False age=16` → `t+09s raw=1 stale=True eff=0 age=22`（**DB 里 status 仍是 1**，展示层已判离线）。
  - B2：node2 行被 node1 的清扫置 0；`operation_log` 落 `node_offline` + `reason=heartbeat_timeout`（含 `stale_seconds`/`threshold`）；**本节点未被误扫**。
  - 恢复：拉起 node2 后 8s 内回到在线 + `node_online` 告警。
  - **周期变更及时性**：`60s→5s` 后心跳在 ≤5s 内恢复快节奏（旧实现要等满 60s）。
  - **回归守卫**：全程 node1 无新增误判记录、终态两节点均在线。
- **前端 12/12 PASS**（Node DOM 冒烟，真实加载 `admin.js` + `/api/nodes` fixture），且**反向对照**跑改动前的 `admin.js` 为 **8/12**（4 条针对本次改动的断言全 FAIL）—— 证明断言真的能抓回归。
- 服务端契约：`/admin` 已出 `?v=20260911b`；`/static/admin.js` 含新逻辑；`/api/nodes` 未登录仍是 **401**（新路由没绕过全局鉴权中间件）。

### 交付

代码在 dev 工作树（6 文件改动：`node_health.py` / `api/app.py` / `alerting.py` / `db/migrate.py` / `static/admin.js` / `templates/index.html`），**待提交推送**（推送前须过 `git-push-secret-scan`）。

## 2026-09-11 修复记录（三：网关主被叫规则参与降级，#74，dev 验证通过，代码待提交）

### 根因（见 PITFALLS #65）
- CDR `4f788f1a`：`caller=80000001 -> callee=ccc`，`switch_count=0` / `gateway_id=NULL` / `reject_reason=denied_by_gw_8_callee_rule:cc?1*`。
- 根因：网关维度主被叫规则（`evaluate_call_scoped(OWNER_GATEWAY,...)`）此前**只在 `_phone_branch`/`_route_via_ap` 对 `candidates[0]` 跑一次**，不通过即 `build_deny_xml(603)` 整通挂断 —— 同池里本该胜出的次选**从未被考察**，failover 链没构建。
- 对比：接入点↔落地策略（G4, `route/service._ap_gateway_allowed`）早就做了「候选池前移过滤 + 回退到同前缀下一个被允许网关」。两套语义不一致。

### 修复（用户拍板 A 方案 + 话机 caller_mid 对齐）
- 新增 `app._filter_candidates_by_gw_rules(db, candidates, caller, callee)`：剔除被本网关规则拒绝的候选，**保留者交给原有排序（前缀/优先级/并发）**，全被拒才拒呼（reason 带全部命中明细，≤64 字符兼容 `cdr.reject_reason`）。
- `_phone_branch` 与 `_route_via_ap` 双路径均接入；**顺序不变式**：资格（规则）在前，偏好（并发重排）在后。
- **话机分支 caller_mid 对齐 AP**：显式 `caller_mid = caller`（话机不经 AP 变换，无 ②c 层），空/拒/出局三处统一下发 `cdr_caller_mid`/`cdr_callee_mid`，使两分支 CDR 口径一致（D1：进入落地网关前的号）。
- 注意：`reject_reason` 是 `varchar(64)` —— `_gw_deny_reason` 对多网关全拒场景截断并以 `;+N` 收尾，宁少明细不超宽。

### 验证
- 单测（容器内，真实 DB）ALL PASS：
  - `callee=ccc` 候选池 `[gw8(testgw), gw7(testgateway)]` -> 过滤后 `kept=[7]`、`denied=[(8,'cc?1*')]`。
  - `_gw_deny_reason`：单网关与历史逐字节一致；多网关 ≤64 字符。
- E2E（真实打 `/fs/dialplan`，caller=80000001/callee=ccc）：返回 `<bridge data="sofia/gateway/testgateway/ccc"/>`，**不再整通 603**；XML 含 `cdr_caller_mid=80000001` / `cdr_callee_mid=ccc` / `cdr_gateway_id=7`。
- 反向对照：历史版本对 ccc 返回 `denied_by_gw_8_callee_rule` 的 603（switch_count=0），本次断言 `denied_by` 缺位 + `bridge` 存在，区分力成立。
- AP 分支回归：caller=1100/callee=cc8888 复放返回干净 XML（无 500/traceback）。

### 交付
代码在 dev 工作树（`src/api/app.py` 单文件改动 +130/−25，未与 #73 同提交），**待提交推送**（与 #73 一并过 `git-push-secret-scan`）。

## 2026-09-11 修复记录（四：#75 ESL 可靠性改造，dev 验证通过）

**背景**：ESL 事件为 at-most-once（无 ACK/无重放），实测单条 `CHANNEL_HANGUP_COMPLETE`
静默丢失（案例 3dd839b9：80000003→ccccc 已接通挂断，FS 通道销毁但网关未收到挂断事件），
导致进程内并发计数 `_conc` 永久漂移（gw7 卡 1/1），limit 小的落地网关被整通 503 拒呼。

**三层改造（全部在 `src/esl_client.py`，+`src/main.py` 启动挂载）**：
1. **事件异步化**：ESL reader 只解析入队（有界 10000，满则丢弃计数告警），
   `esl-event-worker` 线程消费 `handle_event` —— 消费慢不再反压 FS socket 造成丢事件。
2. **CDR 攒批落库**：`_save_cdr` 产物入队，`cdr-writer` 线程攒批（50 条/200ms）
   单事务提交；失败项退化逐条重试（3 次），最终失败落 `cdr_spool`（reaper 重灌）。
   `_upsert_cdr_dict` 新增 `db=` 共享会话参数，默认路径行为不变。
3. **对账自愈**：`esl-reconcile` 线程 30s 周期 + ESL 订阅/重连成功即触发；
   用 ESL `show channels` 快照对照 `_call_store`，对「FS 通道已消失但仍被计数」的腿
   补减计数（`_dec_count_leg` 加 `_dec_done` 幂等闸，挂断路径 dec+pop 原子化+身份校验防双扣）
   并回填骨架 CDR 终态（仅 `hangup_cause IS NULL` 行：end_time/talk/bill/cause；
   已接通=NORMAL_CLEARING，未接通=UNKNOWN；**计费金额不在对账内重算**，待 xml_cdr 真源）。

**验证**：容器内回放 10/10 PASS（正常呼叫攒批落库 / 幂等减计 / 异步消费 / 丢 HANGUP 对账自愈 /
活跃腿不误伤）；回归 pytest 15 passed；真机部署后 dialplan 恢复正常出局（bridge testgateway，
不再 busy_limit 503）。

**遗留**：① 金额兜底待 mod_xml_cdr 真源（P1）；② 并发预检改实时查询（P2，高并发前做）；
③ 对账「欠计」方向（ESL 断连期间新建的呼叫计数缺失）仅观测不修。

## 2026-09-11 工程约定：app.py 冻结 + M3 扩展点拆分（多人并行防冲突）

**背景**：M3 尾巴（角色校验/操作日志/CDR 导出）交给贡献者开发，主线同步做 P2-a（Redis）。
两边唯一冲突面是 `src/api/app.py`，故按扩展点拆分，把 app.py 变成「只挂一行」的稳定契约。

**拆分内容（commit 本节同批）**：
- `src/api/authz.py`：角色校验扩展点 —— `require_role(*roles)` 依赖工厂 + `ROLE_NAMES` 口径；
  当前 `ENFORCE_ROLE=False`（记录不拦截 fail-open），M3 实现角色判定并验证后置 True 全站生效
- `src/api/oplog.py`：operation_log 自动埋点中间件（已挂载，管理端写操作 POST/PUT/PATCH/DELETE
  记 operator/action/object/detail，失败不阻塞业务）+ `record_op()` 显式埋点入口
- `src/api/cdr_export.py`：`GET /api/cdr/export` 基础 CSV 导出（时间范围 + limit≤10万，
  字段与 /api/cdr 列表对齐；已注册在 crud 兜底路由前，PITFALLS #34）
- `app.py` 仅 +5 行挂载（import ×2 / middleware ×1 / include_router ×1）

**约定**：贡献者**不得修改 `src/api/app.py`**；所有 M3 逻辑在上述三个文件内实现；
需改 app.py 或扩展点接口签名时，先与维护者同步评估。app.py 改动权归维护者。

**验证**：py_compile + pytest 15 passed；真机冒烟 —— 登录 ✅ / CDR 导出 CSV ✅ /
dialplan 出局 ✅ / oplog 自动埋点落库 ✅（webhook-test 401 亦被记录为 anonymous）。
