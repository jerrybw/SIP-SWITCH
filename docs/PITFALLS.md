# SIP-SWITCH 避坑合集（PITFALLS）

> **仓内版说明（2026-09-12 初次入仓，@WorkBuddy）**：本文件是项目避坑合集的**仓内权威版**，所有 Agent 共享。
> **维护纪律**：**append-only** —— 按编号追加新坑，禁改写历史条目；坑已修复时在条目内标 `[已修复]`（保留复现路径，删除权仅限含敏感信息的条目，需记录删除原因）。
> **用法**：先查顶部「📑 全量索引」定位编号，再按编号搜正文，**勿通读**（>60KB）。
> **密级**：公开/脱敏级——已过敏感信息扫描（宿主 IP 以 `<DEV_HOST>` 占位）；真实凭据永不见于本文件，见 `deploy.sh` 生成机制与 `.env`（不入仓）。
# PITFALLS.md — SIP 软交换项目「已知坑」详细版

> 从 `MEMORY.md` 拆分而来（2026-09-09），主文件只保留索引 + 3 条高频坑。
> **排障时先读主文件索引，命中后再来这里看细节。** 编号稳定，可跨文件引用（如"见 PITFALLS #24"）。
> ⚠️ **已过时/被推翻的条目**：#42（→ #50）、#43（→ #47）、#44（→ #45）—— 只保留其中"仍成立的事实"，**结论以箭头指向的条目为准**。
> **用法**：先看下面的「全量索引」定位编号 → 再按编号读正文。**别整文件通读**（43KB）。

---

## 📑 全量索引（65 条 · 一句话速查）

### A. FS / sofia / 机制 A（19 条）
| # | 一句话 |
|---|---|
| 1 | ESL 看门狗：须 `recvEvent(1.0)` + 区分超时/断线，阻塞 `recvEvent()` 会永久卡死 |
| 3 | failover：`continue_on_fail` 认 Q.850 **cause 名**；dialplan 恒定追 8 个 cause |
| 4 | dest_ip/port：`_leg_ext` 逐腿下发，`_save_cdr` 用 `_resolve_dest_endpoint()` 校正 |
| 6 | 内线 404/503：`build_allow_xml` 正则须 `re.escape`；8 位分机目录由 `/fs/directory` 动态提供 |
| 7 | FS ACL：`event_socket.conf.xml` 须 `apply-inbound-acl lan`；`fs_cli` 必须 `-H 127.0.0.1 -P 8021 -p <pwd> -x` |
| 8 | 接入点匹配：IP 型 AP 靠 `register_host` **精确**匹配来源 IP；手工 curl 必 603 |
| 13 | STUN 地雷（**仅生产**）：`stun-set external_sip_ip` + ipv6 未禁 → mod_sofia 加载失败 → 全站中断 |
| 14 | `/fs/directory` 兜底 domain 用 `default_sip_domain` 配置，绝不硬编码 |
| 16 | 全新部署自愈：`gw_bootstrap.py` 等 ESL 就绪后发一次 `sofia profile external rescan` |
| 20 | trunk(5080) 403 真实修复 = commit `bd62ccc`（external.xml 移除 `apply-inbound-acl`） |
| 22 | `sofia status` 里的 `external::example.com` **不是故障**（镜像自带 vanilla 桩） |
| 24 | ⚠️ **机制 A 时序依赖**：FS 仅启动/rescan 时拉 `sofia.conf`，数据后插须补 rescan |
| 30 | FS 里 gateway 的 Proxy/Realm 是**旧快照**，`rescan` 才刷新（`restart` 期间会短暂消失） |
| 32 | rule `pattern` 是「通配符 + **全串锚定**」：不写 `*` 就是精确匹配（`138` ≠ `138*`） |
| 35 | 号码变换 act=3 已改「**前缀锚定**」（`db444d2`），推翻 #32 第 3 点旧描述 |
| 43 | ⚠️ 已推翻（→ **#47**）：早先"FS 无 gateway_state 事件"是误判 |
| 47 | `CUSTOM sofia::gateway_state` **可用**，报头名是 **`Gateway`/`State`**；进程重启须拉 `xmlstatus` 对齐初值 |
| 48 | 注册型网关不给 `expire-seconds` 就吃 **FS 默认 3600**，不是 600 → 必须应用层显式下发 |
| 56 | FS `condition` 里 `pattern='1*'` 是**正则**（`1` 零或多次 → 匹配任意串），非 glob 前缀 |

### B. sipp 桩（8 条）
| # | 一句话 |
|---|---|
| 26 | ⚠️ 端到端验证**只用真实软电话 / sipp 当 UAS 桩**，禁手搓 UAC；`fs_cli originate` 不计费 |
| 29 | sipp 答 OPTIONS 唯一正解是 `-aa`（另四条路全死）；`-i` 别填 `0.0.0.0` |
| 41 | sipp 当 Registrar 可行，但**必须覆盖 entrypoint** 且 `-sf` **绝不能配 `-aa`** |
| 42 | ⚠️ 已推翻（→ **#50**）：单实例三合一**能做到**，不需要分支 |
| 50 | 单实例 Registrar+UAS 正解 = **一条正则 recv + 统一 200 OK + `[len]`**；必须含 ACK/BYE/OPTIONS |
| 54 | sipp 场景 XML **禁中文注释**（声明 ISO-8859-1 会 `Unable to load or parse`） |
| 55 | sipp 3.6 `recv response` **只接受整数**（`".*"`/`200-503` 均报错）；禁连续 optional recv |
| 58 | 场景 XML **注释里不能出现 `--`**（XML 规范禁止）→ 同样报 `Unable to load or parse`，与 #54 表象相同根因不同 |

### C. CDR / 计费 / DB（8 条）
| # | 一句话 |
|---|---|
| 2 | CDR 重复行：`cdr` 是**分区表**，`ADD UNIQUE KEY(uuid)` 报 1503 → 用 `_upsert_cdr_dict` 对齐 start_time |
| 5 | 租户扣费：`prepaid_enabled` 已 true，账户**须先充值**否则 603 全拒 |
| 15 | `account.customer_id` **必须可空**（`ensure_account_customer_id_nullable()` 幂等迁移） |
| 25 | dev 种子规格：`sip_phone.password = phone_number`；`gateway.carrier_id` **NOT NULL**；`fs_directory` 不按 domain 过滤 |
| 33 | ⚠️ 告警/状态写入须**独立事务**；`operation_log.created_at` 无默认值会炸 1048，且会连带回滚状态更新 |
| 36 | CDR `fs_node_uuid` 落库**必须取 `NODE_UUID`**，不是 `esl.fs_node_uuid`（死字段） |
| 37 | 新建关联表 INSERT 报 `created_at cannot be null`：模型映射了该列就必须显式赋值，DDL DEFAULT 不生效 |
| 38 | 校验必须**前移到落库之前**，否则失败会留下孤儿行 |

### D. 代码结构 / 路由 / 约定（12 条）
| # | 一句话 |
|---|---|
| 9 | `src/static/` 是镜像 **COPY**：改前端须 build + bump `?v=` |
| 17 | "建表未实现"陷阱：`fs_node` / `operation_log` / `sys_user.role` 有表**无代码**；判完成看有没有代码用它 |
| 19 | "并发预检" ≠ "并发选路因子"：预检 ✅（超限直接 503），选路 P2-c（原本超限即 503 不回退） |
| 34 | ⚠️ 自建 `@app` 路由**必须注册在 `crud_router` 之前**，否则被 `{entity}` 兜底吞掉 → `unknown entity` |
| 40 | `fs_node.name` 是**容器短 ID**，UI 展示一律取 `host` |
| 44 | provision 已自动 killgw+rescan，但**只作用于本节点 FS**（多节点缺口见 #45/#51） |
| 45 | 多节点下发同步：用 **DB 版本号做信令**，别直连其它节点 ESL |
| 49 | 批量 scp 只保 basename → `src/api/app.py` 会变 `src/app.py`；回传须逐文件写全路径 + `git status` 核对 |
| 51 | `provision_pending` 是**只读的最近变更名单**，不是待办队列；判同步看 `provision_seen_<NODE_UUID>` |
| 52 | NODE_UUID **必须显式配 `node.uuid`**，否则容器重建即归属断裂（注册型网关凭空消失） |
| 60 | 录音**共享卷**下「归属他节点 ≠ 读不到」：`remote` 只对 local scheme 且 owner≠here 才为真；端点须**先 `os.path.isfile` 判可达**（可达→200，不可达且 remote 才 409） |
| 65 | 网关**主被叫规则**只对 `candidates[0]` 裁决便会整通 603 硬拒（不参与降级）；✅ **已修 #74**：前移成候选池过滤（与 G4 同构），全被拒才拒呼 |

### E. DevOps / 部署 / 凭据 / 脚本 / 文件系统（13 条）
| # | 一句话 |
|---|---|
| 10 | 改 `src/` 后**必须** `docker compose build gateway`（COPY 非挂载）；无参 build 会连带重建 FS 30–60min |
| 11 | 行尾陷阱：`db/migrate.py` 是 CRLF，其余 LF |
| 12 | WSL IP 漂移：`EXT_SIP_IP` 容器创建时注入，重启后必跑 `./dev-up.sh` |
| 21 | 旧写盘机制（已废弃）：`fs-profiles` 共享卷，2026-09-09 已全清 |
| 23 | 清理/重构后**必须全仓 grep 关键字**（只改主文件会漏 Dockerfile/docs） |
| 27 | docker 网段会漂移：`down -v` 后可从 `172.22` 变 `172.19`；但宿主进来恒为 `<WSL_HOST_IP>` |
| 28 | deploy.sh 在「已存在 mysql 卷但重新生成密码」场景下 **1045 / 网关重启循环** |
| 31 | 多目录 deploy + 换凭据：容器吃哪份 `.env` 看 `working_dir` 标签；改配置后**必须 restart gateway**（healthz ok ≠ DB 连通） |
| 39 | `ssh … 'bash -s' < 脚本` 里嵌套引号会被吞掉整段（表现为**无输出 + exit 0**） |
| 46 | `docker compose exec` 里反引号/SQL 标识符被二次解析 → 别在 exec 里拼 SQL |
| 57 | Windows 侧**别写 `2>nul`**（会落下删不掉的保留名文件 `NUL`）；删文件后**要多视角校验**（Git Bash 与 WSL drvfs 会不一致） |
| 59 | **同一文件禁并行发多个 Edit**（read-modify-write 竞态，只留最后一个，前几个仍报成功）→ 改多处用整篇 Write 或串行 |
| 61 | dev `docker-compose.override.yml`（untracked）里 node2 侧服务**没有 restart 策略** → docker daemon 重启后 node2 永不自动恢复（`docker compose ps` 还默认不显示 Exited，需 `ps -a`） |

### F. 监控 / 心跳（5 条）
| # | 一句话 |
|---|---|
| 18 | 心跳告警未闭环：探测/剔除/恢复逻辑完整，但异常**仅打日志**（✅ 已随 #69 接 webhook 告警） |
| 53 | `heartbeat_interval` 曾是死配置（✅ 已修 `17fdb14`）→ `_plan_interval()` 每轮现读 DB 取 MIN |
| 62 | `fs_node.status` 是**最后写入值**且无心跳超时判定 → 写入方（该节点自己的 gateway）进程一死即「**僵尸在线**」；判据看 `last_heartbeat_at` 而非 `status`（✅ **已修 #73**：B1 展示层现算 stale + B2 任一存活节点跨节点清扫置 0 并告警） |
| 63 | 心跳超时阈值**必须 ≥ 3×探测周期**：心跳每周期才写一次，阈值 ≤ 周期时**健康节点会在下一次心跳到来前被对端判离线** → 互判/来回翻转/刷告警（实测：周期 30s + 阈值 10s，两健康节点互相把对方置 0）。显式值低于下限须**钳制并打 warning**（别静默忽略，见 #53 同型陷阱） |
| 64 | 热配**探测周期变更必须 ≤5s 生效**：一次 `wait(interval)` 会让改小后的周期仍按旧的长节奏写心跳，从而触发 #63 的误判；**只分片但 deadline 在进入时定死是伪修复**（实测两次踩坑）→ 正确写法：每片 ≤5s 重读配置、按「距上次探测 ≥ 当前周期」判断结束 |

---

1. **ESL 看门狗**：须 `recvEvent(1.0)` + 区分超时/断线；阻塞 `recvEvent()` 会永久卡死。
2. **CDR 重复行**：`cdr` 是分区表，`ADD UNIQUE KEY(uuid)` 报 1503；✅ 用 `_upsert_cdr_dict` + `_existing_start_time(uuid)` 对齐 start_time（**收口到汇聚点**，另两条路径 pre_insert/spool 重灌会绕过 `_save_cdr`）。
3. **failover**：`continue_on_fail` 认 Q.850 **cause 名**；`dialplan_xml.py` 恒定追 8 个 cause。
4. **dest_ip/port**：`_leg_ext` 逐腿下发，`_save_cdr` 用 `_resolve_dest_endpoint()` 校正。
5. **租户扣费**：`prepaid_enabled` 已 true，账户须先充值否则 603 全拒。
6. **内线 404/503**：`build_allow_xml` 正则须 `re.escape`；8 位分机目录由 `/fs/directory` 动态提供（别补本地 xml）；503=被叫不可达。
7. **FS ACL**：`event_socket.conf.xml` 须 `apply-inbound-acl lan`；`mod_xml_curl` 须放开；`fs_cli` 必须 `-H 127.0.0.1 -P 8021 -p <pwd> -x`（否则 unix socket `Error Connecting`）。现状：`lan`=LIVE（出厂占位 192.168.42.x≈allow-all）、`domains`=DEAD、`trusted_peers`=**不存在**。
8. **接入点匹配**：IP 型 AP 靠 `register_host` **精确**匹配来源 IP。宿主软电话进来 FS 见 `<WSL_HOST_IP>`；容器内发包的源 IP 是容器自身 IP（见 #27）。手工 curl 无来源 IP 必得 `no_access_point`(603)。
9. **`src/static/` 是镜像 COPY**：改前端须 build + bump `?v=`。
10. **改 `src/` 后必须 `docker compose build gateway`**（`src` 是 COPY 非挂载）；勿用无参 build，会连带重建 FS 30–60 分钟。
11. **行尾陷阱**：`src/db/migrate.py` 是 CRLF，其余 LF；须 `newline=''` + 自适应 EOL。
12. **WSL IP 漂移**：`EXT_SIP_IP` 容器创建时注入，重启后必跑 `./dev-up.sh` 重注。
13. **STUN 地雷**（仅生产）：`vars.xml` `stun-set external_sip_ip` + ipv6 未禁用 → STUN 超时 → mod_sofia 加载失败 → 全站中断。生产无 EXT_SIP_IP 概念，internal/external.xml 都用 `$${external_sip_ip}`。
14. **`/fs/directory` 兜底 domain**：用 `default_sip_domain` 配置（请求参数→配置→空串+告警），绝不硬编码。
15. **`account.customer_id` 必须可空**：`ensure_account_customer_id_nullable()` 幂等迁移 + `01-schema.sql`。
16. **全新部署自愈**：`gw_bootstrap.py` 启动后只等 ESL 就绪发一次 `sofia profile external rescan`（机制 A 后**不再写任何 XML**）。
17. **"建表未实现"陷阱**：`fs_node`（有多节点表无分发）、`operation_log`（全代码无写入）、`sys_user.role`（无校验）。判断"是否完成"看**有没有代码用它**。
18. **心跳告警未闭环**：探测/剔除/恢复逻辑完整（UDP OPTIONS，连续 `FAIL_THRESHOLD` 次失败置离线、一次成功即恢复），但异常仅打日志。
19. **"并发预检" ≠ "并发选路因子"**：✅ 预检（全局/AP/GW 三档超限直接 503，`app.py:256-266`）；❌ 选路因子 P2-c（`candidates[0]` 取首个，排序 `(-len(prefix), -priority, id)` 无并发项，超限即 503 不回退）。真实生效因子：`status==1`、心跳状态、前缀最长匹配→priority→id、AP↔GW allow/deny。
20. **trunk(5080) 403 真实修复** = commit `bd62ccc`（external.xml 移除 `apply-inbound-acl`，鉴权下沉网关 `_access_point`）。⚠️ 旧记"trusted_peers / 704097e"已作废，二者不存在。
21. **旧写盘机制（已废弃）**：靠 Docker 命名卷 `fs-profiles` 共享目录（gateway 写、FS 读 + ESL `rescan`）。2026-09-09 已全清（compose 4 处 `01d5170`、Dockerfile+docs `95fc151`、空卷已删），全仓零残留。
22. **`sofia status` 里的 `external::example.com` 不是故障**：源于镜像 `vars.xml` `default_provider` + `directory/default/example.com.xml`，vanilla 桩网关，不影响选路（只走 DB `prefix_route`→`gateway`）。`down -v` 重建后它未必出现——均勿判为回归。
23. **清理/重构后必须全仓 grep 关键字**（`fs-profiles` 之类）：只改主文件会漏掉 Dockerfile / docs 里的死配置与误导性文档。
24. **⚠️ 机制 A 时序依赖（全新部署必踩）**：FS 仅在**启动时及 rescan 时**拉一次 `sofia.conf`。若 FS 先起、网关数据后插，网关不会自动出现，**必须补一次 `sofia profile external rescan`**。别误判成 xml_curl 坏了。
25. **dev 种子规格**：`sip_phone.password = phone_number`，`domain=''`；`gateway.carrier_id` **NOT NULL**（先插 `carrier`）；`fs_directory` 查话机**不按 domain 过滤**（只 `enabled=1`）。
26. **⚠️ 端到端验证只用现成 SIP 工具，禁止手搓 UAC 脚本（用户 2026-09-09 拍板）**：
    - ✅ **首选真实软电话**（eyeBeam/MicroSIP 注册到 `<DEV_HOST>:5060`）：源 IP `<WSL_HOST_IP>` 与 AP 天然匹配，全链路一次跑通。实测 `221b489d`（80000001→cc）`gateway_id=7 / dest_ip=sipp-stub / cost=0.01 / billed=1`，ledger -0.01、余额 100→99.99。
    - ✅ sipp 当 **UAS 桩**（`deploy/sipp-stub`，v3.6.1 在 `/usr/local/bin/sipp`）。要当 UAC 必须用**自定义场景**（已归档 `sip-cdr-billing-debug` skill 的 `scripts/uac_cdr.xml` / `uac_cc.xml`，From 写死分机号 + `[authentication]`）。
    - ❌ **别用内建 `sipp -sn uac`**：From user 硬编码 `sipp`（`-au` 只改 digest 用户名不改 From）→ `[esl403]` / `WRONG_CALL_STATE`。sipp 自带默认 uac 场景源码即 `uac_builtin.xml`。
    - ❌ 手搓 Python socket UAC 已禁（工作树内 `py_uac_cdr_test.py`/`t15_uac.py` 已于 2026-09-09 删除；`/root/t16_uac_cc.py` 同类，尚未清理）。
    - ⚠️ **`fs_cli originate` 不计费**：绕过 dialplan，CDR 恒 `gateway_id=NULL / cost=0 / billed=0`，只能验"出局可达"。计费通的三判据：`billed=1 且 cost>0` + ledger 有该 uuid 负向流水 + balance 下降。
27. **docker 网络段会漂移**：`down -v` 后可从 `172.22.0.0/16` 变 `172.19.0.0/16`（FS `172.19.0.3`、sipp-stub `172.19.0.4`）；但**宿主进来仍见 `<WSL_HOST_IP>`**，故 `register_host` 无需改。
28. **⚠️ deploy.sh 在「已存在 mysql 卷但重新生成密码」场景下 1045 / 网关重启循环（2026-09-09 实测踩）**：
    - **现象**：在新目录（如 `test_docker/SIP-SWITCH`）跑 `deploy.sh --up`，gateway 持续 `restarting`，日志 `sqlalchemy.exc.OperationalError: (1045, "Access denied for user 'sip_switch'@'...' (using password: YES)")`；连 mysql root 也登不进（1045）。FS 的 `mod_xml_curl ... http error 0` 是**连锁反应**（网关宕机拉不到 `/fs/config`），FS 本身健康。
    - **根因**：① mysql 数据卷（`sip-switch_mysql-data`）**仅在首次初始化时**写入 `sip_switch`/`root` 密码；deploy.sh 重新生成密码写进 `.env`/`config_settings.yaml`，但卷里仍是旧密码 → 错配。② **更隐蔽**：新目录的 compose 复用了原项目名 `sip-switch` 与共享命名卷，于是"另一份拷贝"实际**接管了同一套容器**（容器名仍是 `sip-switch-*`，但挂载的是新目录的配置）。
    - **修复（不丢数据）**：用**卷里真实的原始 root 密码**（在原 `/root/src/SIP-SWITCH/.env` 里）登录 mysql，执行 `ALTER USER 'sip_switch'@'%' IDENTIFIED BY '<网关配置里的密码>'; FLUSH PRIVILEGES;`（注意用户是 `%` 不是 `localhost`，后者不存在会报 1396）。改完 `docker restart sip-switch-gateway-1` 即恢复；FS 会随即重新拉到配置（`+OK scan complete`）。
    - **预防**：① 不要在两个目录跑同一 compose 项目名（会共享卷、互相接管）；② 真要从零：先 `docker compose down -v` 删卷再 `deploy.sh --up`；③ deploy.sh 后续应在"复用已有 mysql 卷但 `.env` 密码与卷不一致"时显式 `--force-reset-db` 警告（#67 待补强）。
29. **⚠️ sipp 桩要支持 OPTIONS 探测，唯一正解是 `-aa`，其余路全是死路（2026-09-09 实测，sipp 3.6.1）**：
    - **现象**：`sipp -sn uas`（server 模式）收到 OPTIONS **直接丢弃**，不发任何响应；UAC 侧表现为 `200 <---------- 0 / Timeout 1`。
    - **正解**：加 `-aa`（sipp auto-answer）——对 INFO / UPDATE / NOTIFY / **OPTIONS** / REFER 自动回 200 OK，INVITE 仍走场景。现网 `deploy/sipp-stub/start.sh` 即 `-sn uas -aa`。
    - **四条死路（都实测过，别再绕）**：
      ① `-oocsf` / `-oocsn`（out-of-call 场景）：sipp 直接拒绝启动 —— `SIPp cannot use out-of-call scenarios when running in server mode`（该能力只给 client 模式）。
      ② 在场景里写 `<recv request="OPTIONS">` + `<recv request="INVITE">` 两条分支：**场景首条必须是 mandatory recv**，新 call 只能由首条那条消息创建，第二条永远匹配不上。
      ③ `<recv request="OPTIONS" optional="global">`：无活动 call 时 OPTIONS 仍被丢弃（global 消息要挂在已有 call 上）；且连续两个 optional recv 会让 sipp 报 `<recv> before <send> sequence without a mandatory message` 拒绝加载。
      ④ `-sf` 自定义场景 **+** `-aa`：sipp 启动后立刻 `Test Terminated`（0 calls），进程秒退。`-aa` 只与内建 `-sn uas` 搭配可用。
    - **配套坑**：`-i` 别填 `0.0.0.0`（SDP 会变 `c=IN IP4 0.0.0.0`），填容器真实 IP（`hostname -i`）；**注意 `-i` 同时也是 bind 地址**，填容器 IP 后 loopback（`127.0.0.1:5060`）收不到包——容器内自测要么 `-i 127.0.0.1`，要么打容器 IP。
    - **验证方式**（跨容器，从 freeswitch 容器打 `sipp-stub:5060`）：`deploy/sipp-stub/probe-options.xml` 发 OPTIONS 看 `200 <----------` 是否为 1；`-sn uac` 发 INVITE 看 `Successful call`。已实测 OPTIONS 200✅、INVITE 180+200+BYE+200✅、FS `originate sofia/gateway/testgateway/xxx` `+OK`✅。
30. **FS 里 gateway 的 Proxy/Realm 可能是"旧快照"，`rescan` 才会刷新（#24 的变体）**：2026-09-09 见 `external::testgateway` 显示 `sip:testgateway@sipp-**stup**:5060`（拼写错），而 DB `gateway.ip` 已是 `sipp-stub`；发 `sofia profile external rescan` 后地址即更正。**注意 `sofia profile external restart` 会让网关短暂消失**（重拉未完成时 `Invalid Gateway!`），要再 rescan 一次才回来。另外该网关 `Ping 0 / PingFreq 0` —— **FS 默认不给点对点网关发 OPTIONS ping**，所以"OPTIONS 探测"需求多半来自网关侧心跳（`heartbeat_enabled`）或外部探测，不是 sofia ping。
    - **✅ 已修复（提交 `4d5a11e`，2026-09-09）**：根因不是"没触发 rescan"（`crud.py` 一直有调 `provision()`），而是 **rescan 对已存在的 gateway 无效**。修复：`fs_provision.rescan()` 增加可选 `gw_name`，先发 `sofia profile external killgw <name>` 再 rescan；`provision()` / `remove_xml()` 均传网关名（killgw 在网关不存在时失败，按 debug 忽略，不影响新增场景）。端到端验证：`PUT /api/gateways/7 {"ip":"172.20.0.4"}` → FS Proxy 自动从 `sipp-stub:5060` 变 `172.20.0.4:5060`，还原同理。

31. **⚠️ 多目录 deploy + 换凭据：容器到底吃哪份 `.env`，看 working_dir 标签；改配置后 gateway 必须 restart（2026-09-09 实测）**：
    - **现象**：在 `test_docker/SIP-SWITCH` 跑 `./deploy.sh`（**不带 `--up`，只生成配置**）拿到一套新凭据，但运行中的容器仍跑主仓 `.env` 那套旧密码 —— 新凭据"看起来生效了"其实没生效。同一 compose 项目名 `sip-switch` 下，谁最后执行 `up`，容器就归谁。
    - **一步定位**：`docker inspect <容器> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'` —— 直接告诉你最后 `up` 是在哪个目录发的。
    - **换凭据 SOP（不丢数据）**：① 备份主仓 `.env` + `config/docker/config_settings.yaml`（带时间戳）；② 用**卷里真实可用的旧 root 密码**登录，执行 `ALTER USER 'root'@'localhost' / 'root'@'%' IDENTIFIED BY <新root>;` 与 `ALTER USER 'sip_switch'@'%' IDENTIFIED BY <新sip_switch>;` + `FLUSH PRIVILEGES;`（用户 host 是 `%`，用 `localhost` 会报 1396）；③ 复制新 `.env` 与 `config_settings.yaml` 到主仓；④ `docker compose up -d`（mysql/freeswitch 会被 Recreated）；⑤ **`docker compose restart gateway`**；⑥ 验证：新 root 登录、`redis-cli -a <新密码> ping`、`/api/login` 200、FS rescan 后网关地址正确、`[HB] gateway ... UP`。
    - **最易误判的坑**：`config_settings.yaml` 是 **bind mount**，改文件**不会**触发容器重建，gateway 进程仍握着旧密码 → 日志持续刷 `1045 Access denied for 'sip_switch'@'<容器IP>'`，但 `/healthz` **仍旧返回 `{"status":"ok"}`**（healthz 不查 DB）。所以**healthz ok ≠ 数据库连通**，必须看 `docker logs` 里有没有 1045 / `phone_sync WARNING reconcile failed` / `[HB] probe error`。改完配置**必须 restart gateway**。
    - **配套小抄**：① ESL 类名是 `fs_esl_socket.ESLConnection`（**不是** `FSESLSocket`），签名 `(host, port, password, timeout=3.0)`，用 `.api(cmd, timeout=10.0)` 取返回值 → `.getBody()`；② 管理端登录路由是 **`/api/login`**（`auth.py` 的 `APIRouter(prefix="/api")` + `@router.post("/login")`），**不是** `/api/auth/login`；body `{"user":..., "password":...}`，成功返回 `{"ok":true}` + httponly cookie（无 JSON token）。
    - **预防**：不要保留两份同项目名的拷贝；真要试一键起，要么 `down -v` 清干净，要么改 `docker-compose.yml` 的 `name:` 做隔离（#28 的加强版）。
    - **🔍 根因不是绝对路径，恰恰是相对路径（2026-09-09 取证）**：`grep -n "/root/\|/opt/" deploy.sh` **0 命中** —— 代码里没有任何绝对路径。问题在 compose 的挂载全是**相对路径**：`./deploy/fs-config:/fs-config:ro`、`./config/docker:/app/config:ro`。相对路径的基准**不是当前 shell 的 cwd，而是 compose 的项目目录**（= compose 文件所在目录 = `com.docker.compose.project.working_dir`）。两个目录的 `docker-compose.yml` 都写 `name: sip-switch`，Docker 就认为它们是**同一个项目**：同一组容器名 `sip-switch-*-1`、同一个命名卷 `sip-switch_mysql-data` / `sip-switch_redis-data`。于是**谁最后执行 `up -d`，谁就成为该项目的当前定义**，容器按它的目录解析相对路径、挂载它的配置。
    - **铁证（逐容器看创建者标签）**：`docker inspect <c> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'` —— 实测 `sip-switch-redis-1` 指向 `test_docker/SIP-SWITCH`、`sip-switch-gateway-1` 指向主仓，两个目录**先后接管过同一项目**。
    - **为什么新凭据只出现在新目录**：`deploy.sh` 幂等且分两条路 —— 已有部署（config 含真实值）走"仅刷新 EXT_SIP_IP、跳过密钥生成"分支；全新目录走"生成全部密钥"分支并打印凭据。所以主仓永远拿不到新凭据，除非加 `--force`。
    - **再叠一层**：bind mount 内容变了**不触发**容器重建，进程仍读旧文件 —— 即便挂载切到新目录，也必须 `restart gateway`。

32. **主被叫 allow/deny 规则的 `pattern` 是「通配符 + 全串锚定」，不写 `*` 就是精确匹配，不是前缀匹配（2026-09-09 实证）**：
    - **位置**：`src/rules/matcher.py::translate_pattern` / `match_number`（表 `rule`，字段 `pattern`；`act`: 1=allow / 2=deny / 3=translate，`direction`: 1=主叫 / 2=被叫）。
    - **语义**：`*` → `.*`（任意长度含空）、`?` → `.`（恰好一位）、其余字符 `re.escape` 按字面处理；最后**强制加 `^...$` 全串锚定**，用 `re.fullmatch`。
    - **实测**：`138*` vs `13800138000` → True（`^138.*$`）；**`138` vs `13800138000` → False（`^138$`）**；`*8000*` vs `80000001` → True；`8000?0001` vs `80000001` → False（`?` 是一位，展开成 9 位）。
    - **⚠️ 最易踩**：想做"138 开头"必须显式写成 `138*`。只填 `138` 表示号码恰好等于 138，等于配了一条几乎永不命中的规则（若它是 allow 白名单，则所有号码都被拦）。
    - **⚠️ act=3（号码变换）是另一套语义，别混**：`src/rules/service.py::_translate_pattern_to_regex` **不锚定**、`*` → `(.*)` 捕获组、`replace_to` 里的 `*` 引用 `\1`，且是**子串替换**（`rx.sub(..., count=1)`）；多条命中取 pattern 最长者。同一个 `pattern` 列在 allow/deny 与 translate 下含义不同。
    - **裁决语义**：无规则 = 放行；**deny 优先于 allow**；存在 allow 规则时未命中任一即拦截（白名单）；主叫与被叫**分别裁决、都过才放行**。`owner_type`: 1=全局 / 2=接入点 / 3=落地网关。
    - 另注：`prefix_route.prefix`（选路）是**真正的按前缀匹配**，与 rule 的通配符语义是两套东西。

33. **告警/状态写入必须用独立事务，且 `operation_log.created_at` 无默认值会炸（2026-09-10 #69 实测）**：
    - **`created_at` 无默认**：`OperationLog` 模型 `created_at = Column(DateTime, nullable=False)` 既无 `server_default` 也无 Python `default`。插入时若省略该字段，SQLAlchemy 显式发 `NULL` → MySQL `1048 Column 'created_at' cannot be null`，整条 `commit()` 失败。修复：`alerting.py` 插入时**显式传 `created_at=datetime.now()`**（长期应给模型加 `default=datetime.utcnow`）。
    - **⚠️ 致命连带**：最初 `alert_if_changed` 与节点状态更新**共用同一个 `db` session**，告警 `commit()` 抛 1048 后 `db.rollback()` 把**节点 `status`/`fail_count` 的更新一起回滚** —— 表面看 offline/overload 永远不落库。修复：**告警写库用独立 session**（自带 `commit`/`rollback`），与健康状态更新事务彻底隔离；告警失败只 `print` + 记 warning，绝不拖累状态机。
    - **通用铁律**：任何"副作用写入（审计/告警/日志）"都不要和"核心状态变更"绑在同一事务；核心状态必须优先落库，副作用失败可丢弃。

34. **自建 `@app` 路由被 crud 兜底路由吞掉 → `unknown entity`（2026-09-10 节点状态/Webhook 实测）**：
    - **现象**：新增 `GET /api/nodes`、`POST /api/webhook-test` 后，带 admin cookie 调却返回 `{"detail":"unknown entity"}`，像是接口没注册。
    - **根因**：`app.py` 在**顶部** `app.include_router(crud_router)`（crud 含 `@router.get("/{entity}")` / `@router.post("/{entity}")` 等兜底，全量接管 `/api/<任意>`）。Starlette 按**注册顺序**匹配路由，兜底路由先注册 → 先匹配，`/api/nodes` 被当成 `entity=nodes` 吞掉。
    - **修复**：把 `@app.get("/api/nodes")` / `@app.post("/api/webhook-test")` 的**定义移到 `app.include_router(crud_router)` 之前**（或挂到另一个先注册的 router）。只要具体路由注册早于兜底路由即生效。
    - **通用铁律**：在已挂通用 `{entity}` 兜底路由的 FastAPI 应用里加具体接口，必须放在兜底路由注册**之前**，否则一律被兜底吞掉。

35. **主被叫变换（act=3）`pattern` 现已改为「前缀锚定」（2026-09-10 修正，commit `db444d2`）**：
    - **原 bug（用户实测）**：`rules/service.py::_translate_pattern_to_regex` 编译时**不加 `^`**，`_apply_one`/`_translate_direction` 用 `rx.search` 子串匹配。`C?1` 的 regex `C.?1` 会子串命中 `ccc1` 的 2-3 位（`cc1`），导致不该变换的号被改。
    - **修复**：编译加 `^` 前缀锚定（`re.compile("^" + buf)`），仅号首匹配。现 `ccc1` 不命中、`c11aa/cc143/cb12334` 前缀命中。命中后**整体前缀**替换为 `replace_to` 并保留后缀（如 `cc143`→`943`），非仅替换首字符。
    - **⚠️ 推翻 PITFALLS #32 第3点旧描述**：#32 说"act=3 不锚定、子串替换"是**旧设计**，现 translate 同为前缀锚定语义；`*`→`(.*)` 捕获组、`replace_to` 里 `*` 引用 `\1`、多条命中取 pattern 最长者 这三条不变。
    - **通用铁律**：写号码变换规则时，如需"某前缀开头"直接写 `C?1` 即可（已前缀锚定）；如需全串精确，allow/deny 走 #32 的 `^...$` 全串锚定。

36. **CDR `fs_node_uuid` 落库必须取 `NODE_UUID`，不是 `esl.fs_node_uuid`（2026-09-10 修正，commit `db444d2`）**：
    - **原 bug（用户实测）**：`esl_client._save_cdr` 写 `fs_node_uuid=ESL_CFG.get("fs_node_uuid")`，而 `ESL_CFG=settings["esl"]`，配置 `esl.fs_node_uuid:` **为空（死字段，无人写入）** → 所有 CDR `fs_node_uuid=null`、前端「FS节点」列无值。
    - **修复**：改取 `core.config.NODE_UUID`（与 `node_health` 自注册同源；`_resolve_node_uuid` 优先级 `node.uuid` → `esl.fs_node_uuid` → 主机名）。`pre_insert_cdr` 半成品不填该列，但 HANGUP 路径 `_save_cdr` 经 `_persist_cdr` 整行 upsert 覆盖，故终态带值。
    - **验证**：rebuild gateway 后新呼叫 `fs_node_uuid` 均带 NODE_UUID（`2a5f89f1b0f0ce74` 为 dev 主节点值）。
    - **通用铁律**：落库节点标识统一用 `NODE_UUID`，别从 `esl.fs_node_uuid` 取（那是占位死字段）。

37. **新建关联表 INSERT 报 `Column 'created_at' cannot be null`（2026-09-10 #64 实测）**：
    - **现象**：`gateway_node` 表 DDL 明明写了 `created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)`，但 SQLAlchemy `db.add(GatewayNode(...))` 却报 `(1048, "Column 'created_at' cannot be null")`。
    - **根因**：SQLAlchemy 对**它知道的所有列**都会显式写进 INSERT 语句；Python 侧值为 `None` 就发 `NULL`，**覆盖掉 MySQL 的 DEFAULT**。DDL 的 DEFAULT 只在"该列未出现在 INSERT 中"时才生效。
    - **修复**：按项目约定在 CRUD 里**显式写时间戳**（同 `Gateway`/`Cdr`：`obj.created_at = _now()`）。本项目模型一律是裸 `mapped_column(DateTime)`，没有 Python 侧 default。
    - **通用铁律**：新表即使 DDL 给了 DEFAULT，只要模型里映射了该列，INSERT 就必须显式赋值，否则踩 1048。

38. **校验必须前移到"落库之前"，否则失败会留下孤儿行（2026-09-10 #64 实测）**：
    - **现象**：通用 CRUD 先 `db.commit()` 提交主表、再写关联表并做业务校验；校验失败返回 400，但**主表行已经落库** → 留下"已创建却无归属节点"的孤儿网关（实测残留 `zz-dup`）。
    - **修复**：把校验抽成 `_validate_gateway_node(db, obj, gw_id, data)`，在 `_apply()` 之后、`db.commit()` **之前**调用；`_sync_*` 只负责写入，不再抛校验异常。
    - **通用铁律**：任何"主表 + 关联表"的写入，跨表业务校验（唯一性/必填）一律前置到主表提交前；提交后再校验 = 必然产生脏数据。

39. **`ssh … 'bash -s' < 脚本` 里嵌套引号会被 bash 吞掉整段（表现为无输出、exit 0）**：
    - **现象**：脚本内含 `docker compose exec -T mysql sh -c 'mysql -e "… WHERE host=\"x\"" '` 这类多层嵌套引号时，远端 bash 解析异常，把后续所有输入当字符串吃掉 → **命令无任何输出、退出码仍是 0**，极易误判成"脚本跑完但没结果"。
    - **规避**：涉及多层引号/JSON/变量替换的复杂脚本，**改用 Python 写**（`python3 - <<'PYEOF'` 内用 urllib 发请求），彻底绕开 shell 引号地狱；简单 patch 才用 bash。
    - **判据**：只要出现"exit 0 且 Stdout 完全为空"，先怀疑引号/解析被吞，而不是目标服务没响应。

40. **`fs_node.name` 是容器短 ID，UI 展示要取 `host`（2026-09-10 实测）**：
    - **现象**：节点下拉 / 网关列表「归属节点」显示成 `23b883bf556e` 这种字符串，不是 `freeswitch`。
    - **根因**：`node_health` 自注册时 `name=socket.gethostname()`，docker 里就是容器短 ID；**可读名在 `fs_node.host`**（来自 `esl.host` 配置，如 `freeswitch`/`freeswitch2`）。
    - **修复**：`_enrich_gateway_node` 返回 `node_name = host or name` 专供展示（表单提交仍用 `node_uuid`）；前端 `select-src` 的 `optt` 由 `name` 改 `host`。「节点状态」tab 已单列 `host`（地址列），不受影响。
    - **通用铁律**：凡给用户看的节点标识，一律用 `host`；`name` 只当内部 fallback。

41. **sipp 当 Registrar 让 FS 真注册（可行，但有两个硬约束）**：
    - **结论**：注册型落地网关（`register=true`）**链路已实现且可用**；用 sipp 自定义场景完全可以让 FS 进 `REGED`。
    - **场景写法**：`<label id="1"/>` → `<recv request="REGISTER" timeout="0"/>` → `<send next="1">` 回 `SIP/2.0 200 OK`（含 `[last_Via:]/[last_From:]/[last_To:];tag=…/[last_Call-ID:]/[last_CSeq:]`、`Contact`、`Expires: 3600`）→ 循环等下次重注册。FS 收到 200（无 401 挑战）即视为注册成功。
    - **硬约束 1**：sipp-stub 镜像 `ENTRYPOINT=start-stub.sh` **固定执行 `-sn uas -aa` 且忽略 command** → 自建场景必须**覆盖 `entrypoint`**（如 `entrypoint:["sh","-c"]` + 自己拼 sipp 命令）。
    - **硬约束 2**：按 PITFALLS #29，`-sf` 自定义场景**绝不能配 `-aa`**（sipp 会立刻退出，0 calls）。这里只用 `-sf`，不加 `-aa`。
    - **配套**：改已存在 gateway 的目标地址后，`rescan` 无效，须 `sofia profile external killgw <name>` + `rescan`（#30），否则一直停在旧地址（`FAIL_WAIT`）。
    - **判据**：`sofia status gateway` 出现 `REGED` 即成功；`FAIL_WAIT`/`NOREG` = 目标不可达或未启用注册。

42. **sipp 单实例三合一（OPTIONS+REGISTER+INVITE）：⚠️ 本条"做不到"的结论已推翻（2026-09-10），可行做法见 #50**：
    - 仍成立的三条：① `sipp -sn uas -aa`（sipp-stub 默认）**不答 REGISTER**（`-aa` 只自动应答 INFO/UPDATE/NOTIFY/**OPTIONS**）；② `-sf` 自定义场景**不能配 `-aa`**（#29，秒退）；③ `-sf` 用**正则首条 recv** 可同时接 REGISTER+INVITE。
    - ❌ 已推翻：原写"没有干净的方法分支、OPTIONS 必须双实例"。**错**——不需要分支，所有方法塞进同一正则、回同一个 200 OK 即可（#50）。
    - 仍别做：照抄网上的"多方法 UAS 骨架"（常配 `-aa` → 秒退，或用 `optional="global"` 兜 OPTIONS → 本版本无效）。

43. **落地网关注册状态：⚠️ 本条早先结论已推翻（2026-09-10），正确做法见 #47**：
    - 早先订阅 `CUSTOM sofia::gateway_state` 监听 75s 收到 0 条事件 → 误得"FS 无此事件、只能轮询"；当日复测证伪（完整收到 `DOWN → TRYING → REGISTER(200 OK) → REGED`）。差异原因未定论，**以 #47 的复现为准**。
    - `sofia xmlstatus gateway`（`<state>`/`<status>`/`<uptime-usec>`）仍有价值：作为**初值对齐**手段（进程重启后补齐，见 #47/#48）。
    - `_handle_sofia_reg` 处理的是**话机**注册（`sofia::register`/`expire` → `sip_phone`/`access_point`），不含落地网关。

44. **provision 已自动 killgw+rescan，但只作用于「本节点 FS」**：
    - `fs_provision.rescan(prof, gw_name)` 内部先 `killgw` 再 `rescan`；CRUD create/update 都会调 `provision(obj)` → **走 Web/API 改网关自动生效**。
    - 手动 SQL 改 DB 绕过 CRUD 不会触发（排查"改了不生效"先确认是否走了 API）。
    - ⚠️ 多节点缺口**已于 2026-09-10 修复**，见 #45/#51。

45. **多节点网关下发同步：用 DB 版本号做信令，别直连其它节点的 ESL（2026-09-10 落地，commit `b73cf0f`）**：
    - **问题**：每 FS 节点一个网关实例、共享同一 MySQL，但实例只能对自己那台 FS 发 ESL → 在任意节点改网关，其它节点不会刷新。
    - **方案**：DB `system_setting` 当跨节点信令 —— `provision_seq`（每次网关增删改 +1）+ `provision_pending`（最近变更网关名 JSON，去重保序、JSON 后超长丢最老的，截断到 varchar(512)）；各节点 `ProvisionWatcher` 后台线程轮询（默认 5s，第2类热配 `provision_sync_interval`），发现 seq 变化就对本节点 FS 做 killgw+rescan。
    - **为什么不直连其它节点 ESL**：生产多节点通常**只共享 DB/Redis，节点间网络未必互通**；且轮询方案下离线节点回来会自动补齐（seq 一直落后）。代价：其它节点有 ≤ 轮询周期的延迟，发起变更的那个节点仍是即时。
    - **幂等设计**：各节点轮询不同步，**重复 killgw/rescan 无害**，故不需要精确消费位点；首轮（last_seq=None）只对齐位点不做事，避免启动时无谓重扫。
    - **改名场景**：`crud` update 在 `_apply()` **前**捕获 `obj.name`，以 `provision(obj, old_name=...)` 传入，让其它节点也能 killgw 掉旧名对象（否则旧对象在别的节点残留）。
    - **精度**：`pending` 名单让 resync **只 killgw 变更的那些网关**，不误伤无关网关——实测 FS2 上的注册型网关在这次重扫后保持 `REGED` 不被打断。
    - **验证**：在 node1 Web 改点对点网关端口 → FS2 约 5s 自动跟上；注册型网关归属 node2→node1→node2 → 新节点自动加载、旧节点自动卸载。
    - **配套新增**：`core/sys_setting.set_setting`（upsert），与 `get_setting` 对称；写入第2类配置一律走它。

46. **`docker compose exec -T <svc> sh -c "mysql -e \"...\"" ` 里反引号/SQL 标识符会被二次解析**：
    - **现象**：查 MySQL 保留字列（`\`key\``、`\`value\``）时，反引号被外层 shell 当命令替换吃掉 → `sh: line 1: key: command not found` + `ERROR 1064`。
    - **规避**：别在 exec 里拼 SQL。**在容器内用 ORM 读**：本地写 Python，用 `subprocess.run([...list 形式..., "python3", "-"], input=script)` 把脚本喂进容器 stdin —— 不经 shell，彻底没引号问题（比 scp+docker cp 更省事）。
    - 这是 PITFALLS #39 的延伸：**只要命令里有反引号、`$()`、多层引号，就别走 shell**。

47. **`CUSTOM sofia::gateway_state` 可用，但报头名是 `Gateway`/`State`（2026-09-10 实测，已落地）**：
    - **订阅方式**：主 ESL 连接的 `event plain` 串里追加 `sofia::gateway_state` 即可（与 `sofia::register`/`expire` 同一串）：
      `"… CUSTOM sofia::register sofia::expire sofia::gateway_state"`（`esl_client.ESLClient._run` 的 `EVENT_SUB`）。
    - **⚠️ 报头名易错**：是 **`Gateway`**（网关名）和 **`State`**（FS 内部态），**不是** `Gateway-Name` / `Gateway-State`。另有 `Ping-Status`、`Register-Network-IP/Port`、`Phrase`、`Status`（仅 REGISTER 那一跳带 SIP 响应码）。事件来自 `sofia_reg.c::sofia_reg_fire_custom_gateway_state_event`。
    - **完整序列**（killgw + rescan 后，实测时间轴）：`DOWN`(37.588) → `TRYING`(38.574) → `REGISTER 200 OK`(38.574) → `REGED`(39.581)。即 **rescan 后亚秒级重注册、约 2s 内 REGED**。
    - **映射**（`src/gw_state.py::STATE_MAP`）：REGED→1 已注册；TRYING/REGISTER/PROGRESS→2 注册中；NOREG/UNREGED/EXPIRED/DOWN→0 未注册；FAIL_WAIT/FAILED/TIMEOUT/REJECT→3 注册失败。未知态**不落库**（避免乱写）。
    - **只对 `auth_type=1` 落库**：点对点网关不注册，FS 对它不产生注册事件（state 恒 NOREG），若参与回写会把状态钉死在"未注册"。
    - **必配初值对齐**：事件只在**状态跃变**时投递 —— 网关进程重启后，FS 里早已 REGED 的网关不会补发事件，DB 会一直停在"未注册"。故 `gw_state.start_gateway_state_sync()` 在启动后拉一次 `sofia xmlstatus gateway` 对齐（正则取 `<name>`+`<state>` 对）。
    - 复现探针脚本（本地 `_work/gw_state_probe.py`）：用项目自带 `fs_esl_socket.ESLConnection` 连接并 `events("plain", …)`，逐条打印全头即可。

48. **注册型网关的 `expire-seconds` 不给就吃 FS 默认 3600，不是 600（2026-09-10 实测）**：
    - 实测：日志网关未下发 `expire-seconds` 时 `sofia status gateway` 显示 `Expires 3600 / Freq 3600`；下发 `expire-seconds=600` 后重新 rescan 变为 `Expires 600 / Freq 600`。
    - 因此"默认值 600"必须是**应用层默认 + 显式下发**，不能指望 FS 默认。DB 列默认 600 + `fs_sofia_config._gateway_xml` 显式写 `<param name="expire-seconds">`。
    - 只对 `register=true` 的网关下发这两个参数（点对点写了是噪音）；取值用 `_int_or()` 夹取（0/NULL/越界→回默认，避免把非法值喂给 FS）。
    - **该参数改动必须 killgw+rescan 才生效**（#30：rescan 对已存在 gateway 无效）。

49. **批量回传源码到远端：`scp a b c dst/` 只保留 basename，会把 `src/api/app.py` 变成 `src/app.py`**：
    - 现象：一次 `cp /tmp/{app,crud,models,migrate}.py src/` 之后，git status 里真实文件（`src/api/app.py` 等）**没变**，反而多出 4 个 untracked 的 `src/app.py`、`src/crud.py`…；此时"改完不生效"会极难察觉。
    - 规避：**逐文件指定完整相对路径**（`cp /tmp/app.py src/api/app.py`）；回传后立刻 `git status --short` 核对**期望的那几个文件**是否出现在 `M ` 列表里，并 `grep` 关键改动行确认落地。

50. **sipp 单实例同时当 Registrar + UAS：用「统一响应」而不是分支（2026-09-10 实测落地）**：
    - **sipp 3.6 场景语法三坑（踩全了）**：
      1) `<label>` **必须写 `id`**，写 `name` 会报 `label is missing the required 'id' parameter` 并直接退出。
      2) **没有 `<next label="x"/>` 这个元素**（报 `Unknown element 'next'`）；跳转只能靠元素属性：`<recv ... next="2"/>`、`<send next="1">`、`<nop next="1"/>`。
      3) **不允许两条连续 `optional` recv**：`<recv request="REGISTER" optional="true"/>` 后面再跟一条 optional recv，会报
         `<recv> before <nop> sequence without a mandatory message. Please remove one 'optional=true'`。
         => "先等 REGISTER、收不到再等 INVITE"的**双分支写法根本装不进去**；而且 optional recv 遇到不匹配的消息会**丢弃**它，就算能写也接不到。
    - **正解（不需要分支）**：把所有可能出现的方法塞进**一条**正则 recv，**回同一个 200 OK** ——
      `request="(REGISTER|INVITE|ACK|BYE|OPTIONS|CANCEL|INFO|UPDATE|PRACK|SUBSCRIBE|NOTIFY)"` + `regexp_match="true" timeout="0"`，
      然后 `<send next="1">` 回 200 OK（`[last_Via:]`/`[last_From:]`/`[last_To:]`+tag/`[last_Call-ID:]`/`[last_CSeq:]`）。
      REGISTER 与 INVITE 的 200 OK 头部结构相同、差异只有 SDP body → 用 **`[len]`**（自动变量 = 本次发送消息的 body 长度）动态算 `Content-Length`，一份模板两边通用。
      **完整可用场景** = `/root/sipp-reg/reg_uas.xml`（dev untracked）+ 用户级 skill `sipp-uas-stub`，此处不再贴全文。
    - **必须把 ACK/BYE/OPTIONS 也放进正则**：否则它们撞上 mandatory recv 会让场景失败退出（桩直接死）。
      对 ACK 回 200 OK 协议上多余，但 FS 侧会当无匹配事务丢弃，无害（实测通话正常）。
    - **`-rtp_echo` 要加**：让桩回媒体，FS 侧才能形成真实双向媒体（否则只有信令通）。
    - 落地文件：`/root/sipp-reg/reg_uas.xml`（dev untracked）+ compose override 里的 `sipp-reg.command`。
    - **验证证据**（2026-09-10，FS1，网关 `test-register-gw`）：同一实例上
      `sofia status gateway` = `State REGED / Status UP / Expires 600`，同时
      `originate sofia/gateway/test-register-gw/9001 &echo()` → `+OK`，`show channels` 显示
      `callstate=ACTIVE / read_codec=PCMU / write_codec=PCMU` —— 注册与呼叫应答并存。

51. **`provision_pending` 是「只读的最近变更名单」，不是待办队列（2026-09-10 用户提问引出）**：
    - **现象**：管理端卡片显示「待同步网关 1」长期不变，看起来像"有一个网关一直没同步"。
    - **真相**：同步是**正常发生**的。`ProvisionWatcher._sync_once` 只**读** `provision_pending` 来决定"精确 killgw 哪些名字"，
      **从不消费/清空它**（清空的唯一时机是「立即全节点重扫」）。所以它表达的是"最近一次变更涉及哪些网关"，
      与"谁还没同步"无关 → **是措辞误导，不是同步故障**。
    - **修法**：① 卡片措辞改「最近变更」并把网关名以标签列出；② 新增**各节点自报位点**：
      watcher 每处理完一个 seq（含首次上线对齐、`force_all_nodes_rescan` 本节点分支）就写
      `provision_seen_<NODE_UUID> = seq`。管理端把每节点的位点与 `provision_seq` 对比：
      相等=绿色「已同步 seq N」，落后=黄色「落后 N」，从未上报=灰色「未上报」。
      **这个位点才是"是否真同步"的答案**，且节点离线时会自然停住，能暴露故障。
    - 注意 key 长度：`provision_seen_`(15) + uuid(≤36) 要 ≤ 64（`system_setting.key` 是 varchar(64)），
      故 `report_seen` 里有截断保护。sys_config 是「全部 key 直出」，所以 `/api/sys-config` 会自动带出位点，**后端无需改接口**。

52. **NODE_UUID 必须显式配 `node.uuid`，否则容器重建后网关归属会断裂（2026-09-10 排查）**：
    - `core/config.py::_resolve_node_uuid()` 的回落链是 `node.uuid` → `esl.fs_node_uuid` → **`socket.gethostname()`**。
      容器里 hostname 就是容器短 ID，**重建即变**。
    - 一旦回落到 hostname：容器重建 → NODE_UUID 变 → `gateway_node` 里的旧 node_uuid 对不上 →
      **注册型网关在该节点上凭空消失**（`sofia status gateway` 报 `Invalid Gateway!`，`killgw` 报 `no such gateway`），
      而点对点网关不受影响（全量下发）。排查时极易误判成 xml_curl 坏了。
    - 故：每个 FS 节点的 `config_settings.yaml` 里必须有 `node.uuid`（dev 的 node1/node2 均已配，实测重建后 uuid 稳定）。
    - 另注：`fs_node.name` = `socket.gethostname()`（容器短 ID，不可读），`fs_node.host` 才是可读节点名（#40）。

53. **`gateway.heartbeat_interval` 曾是死配置（✅ 已于 2026-09-10 修复，commit `17fdb14`）**：
    - **原现象**：管理端把「心跳间隔(秒)」改成任意值，实测仍是 **30s 探一次**。
    - **原根因（三层都有、唯独读的地方没有）**：
      - 列存在：`db/models.py` `heartbeat_interval`、`01-schema.sql` `DEFAULT 10`；
      - 可写：在 `crud.py` 的 EDITABLE 白名单里；前端 `admin.js` 有表单（`def: 10`）；
      - **但全仓无任何读点** —— `heartbeat.py` 从不引用它，周期来自 `main.py` `HeartbeatProber(interval=30)` 的**硬编码字面量** +
        `heartbeat.py` `DEFAULT_INTERVAL = 30`。→ 真 bug（死配置），**不是展示问题**。
    - **✅ 修复方案（粒度冲突的取舍）**：`HeartbeatProber` 是**单线程一轮探所有网关**（还按 `(ip,port)` 分组共享结果），
      而 `heartbeat_interval` 是 **per-gateway** 字段——两者天然对不齐。取「**全体启用心跳网关的 `MIN(heartbeat_interval)`** 作为本轮睡眠时长」：
      粒度最细者被严格遵守，其余网关只会被**更频繁**探测（偏保守、无害）。
      新增 `_plan_interval(db)`（每轮探测完**现读 DB** 重算，网关增删/改间隔**无需重启进程**）+ `_clamp()`（`MIN_INTERVAL=5` / `MAX_INTERVAL=3600`，
      防配 0 打满 CPU、配太大导致"假死"无感知）；`DEFAULT_INTERVAL=30` 降级为**仅 DB 读不到值时的兜底**。
      三处默认值统一为 30（`models.py` / `01-schema.sql` / `admin.js` 表单）。
    - **实测**：三网关全设 20s → `last_heartbeat_time` 跳变间隔 ≈20s（`09:21:04→09:21:24→09:21:44`）；全设 3s → ~3-7s（被 `MIN_INTERVAL=5` 托底附近）。
    - 唯一原本就真正被读取的 per-gateway 字段是 **`heartbeat_timeout`**（单次探测超时，`heartbeat.py` 用 `rep.heartbeat_timeout or 3`）；
      且因 `_probe_once` **按 (ip,port) 分组**，取的是**组内第一个成员**的 timeout，同 IP 多网关只认第一个。
    - **判据**：`gateway.last_heartbeat_time` 每轮都刷新 → 用两次采样间隔即实测周期。

54. **sipp 场景 XML 带中文注释 → `Unable to load or parse`（2026-09-10 实测）**：
    - **现象**：自建 UAC 场景 `scp` 进容器后 `sipp` 直接报 `Unable to load or parse '/sc/xxx.xml' xml scenario file`，没有任何行号提示。
    - **根因**：场景文件 XML 声明写的是 `encoding="ISO-8859-1"`（sipp 官方模板原文），但文件里塞了 **UTF-8 中文注释** → 编码冲突解析失败。
    - **修复**：声明改 `UTF-8` **并删掉全部中文注释**（最终可用版：`p2c_uac5.xml`/`p2c_uac7.xml`，纯 ASCII + UTF-8 声明）。
    - **通用铁律**：sipp 场景 XML 一律**纯 ASCII**，别留中文；报"无法解析"时先查编码而非语法。

55. **sipp 3.6 `recv response` 只接受单个整数（或逗号列表），且不允许连续 optional recv（2026-09-10 实测）**：
    - `response=".*"` → `response code, ".*" is not a valid integer!`；`response="200-503"` → **同样报错**（不支持区间）。
    - 「`100` optional → `183` optional → `200` 必收」会报 `<recv> before <pause> sequence without a mandatory message`。
    - **可用写法**：`100 optional` → `183 optional` → `200` 必收 三段的**每段都是 recv**（段之间不插 pause）才通过；
      这里的关键是**任意时刻必须有"最后一条 mandatory"收尾**，且 optional 不能连续两条存在于没有 mandatory 的上下文里。
    - 早期 RTP/1xx 时序不确定时，宁可**只收 200**（把 100/183 全删），牺牲一点真实性换取场景稳定加载。

56. **FS `condition` 里的 `pattern='1*'` 是正则，不是通配前缀（2026-09-10 实测踩）**：
    - **现象**：临时加了一条 rule `pattern='1*'` 想放行 `1` 开头的号，结果 `80000001→cc8888`、`90000001` **全被拒**（`ap=5 denied ... rule:1*`）。
    - **根因**：`pattern` 在 FS dialplan `condition` 里按**正则**解释，`1*` = 字符 `1` 出现零或多次 → **匹配任意串**（相当于全匹配）。
    - **⚠️ 与项目自身 rule 表的语义不同**：网关侧 `rule.pattern` 是「通配符 + 全串锚定」（#32），FS 侧 `condition` 是正则——**两套语义别混**。
    - **规避**：验证时若只需临时放行，直接 `DELETE FROM rule WHERE id=<临时行>`，别在 FS condition 里做前缀匹配实验。

57. **Windows 侧 `2>nul` 会落下删不掉的 `NUL` 文件；删文件后校验要多视角（2026-09-11 清理工作区时踩）**：
    - **来源**：在 Windows 目录里跑命令时误用 `2>nul` 重定向（CMD 风格），**不是** `2>/dev/null`。CMD 里 `nul` 是设备名、不出文件；但经 Git Bash / POSIX 层执行时，`nul` 被当成**普通文件名**，于是原地生成了一个 192 字节的字面文件 `NUL`。
    - **为什么删不掉**：`NUL` 是 Windows **保留设备名**，NT 层会把它解析成空设备 → 所有常规删除路径全部被拒：
      | 方式 | 结果 |
      |---|---|
      | `rm -f ./NUL` / `mv NUL x` / `find . -delete` | `Permission denied` |
      | 原生 CMD `del`（含 8.3 短名） | **连条目都看不到**（`dir /x` 里没有它） |
      | .NET `Delete("\\?\…\NUL")` | 访问被拒绝（即便先用 `SetAttributes(Normal)` 清属性） |
    - **⚠️ 三方视角不一致（最坑的点）**：Git Bash `ls` 能看到（9 条目），而原生 CMD 与 **WSL drvfs `ls /mnt/d/...` 都看不到**（8 条目）。→ 用单一视角判断"删没删掉"会得出相反结论。
    - **最终解法**：.NET `SetAttributes(path, Normal)` + `[IO.File]::Delete(\\?\ 扩展路径)` 组合后消失（调用**仍抛访问被拒异常，但实际已生效**）。之后三视角统一为 8 条目。
    - **通用铁律**：① Windows 侧**永远别写 `2>nul`**，统一 `2>/dev/null`；② **删除文件后的校验必须多视角**（Git Bash + WSL drvfs 双查一致才算数）；③ 遇到"删了但 `ls` 还在"或"看不到却存在"，先怀疑**保留设备名**（`NUL`/`CON`/`AUX`/`PRN`/`COM1~9`/`LPT1~9`）。
    - **清理前的安全自查（本项目特有）**：工作区根目录的 `docker-compose.override.yml` 是 **node2（freeswitch2 + gateway2）的唯一定义**、**WSL SSH 私钥文件**（文件名只记录在交接方本地记忆文件，**文档内一律不写具体名**） —— 二者外形像脚手架，实为必需，**清理时必须保留**。

58. **sipp 场景 XML 的注释里出现 `--` → 同样报 `Unable to load or parse`（2026-09-11 归档 skill 资产时踩）**：
    - **现象**：与 #54 **一字不差**的错误信息（`Unable to load or parse '<file>' xml scenario file`，不给行号），但文件是**纯 ASCII、无 BOM、声明 UTF-8** —— 按 #54 的判据全部"合格"，极易误判成"sipp 又抽风"。
    - **根因**：XML 规范 **禁止注释内容出现 `--`**（`--` 是注释的结束符前缀）。我在注释里写了 `... on purpose -- sipp 3.6 aborts ...`、`"200 <----------"` 这种**教科书式英文破折号 / ASCII 箭头**，直接把 XML 写坏。
    - **定位手法**：`xml.etree.ElementTree.parse()` 会给出精确的 `line X, column Y`（sipp 不给），**先用 Python 过一遍再喂给 sipp**，能省掉一轮盲猜。校验脚本要同时查四件事：非 ASCII 字节、BOM、**注释内 `--`**、XML 可解析。
    - **修复**：注释里的 `--` 一律换成 `;` 或单个 `-`；顺带把说明性英文注释保留为 ASCII。
    - **通用铁律**：sipp 场景 XML 的合规三件套 = **纯 ASCII + 无 BOM + 注释无 `--`**（#54 只管了前两条）。

59. **同一个文件并行发出多个 Edit 会互相覆盖（2026-09-11 实测，工具用法坑）**：
    - **现象**：对一份文件连发 3 条 Edit（改第 2、7、12 行），三条**全部返回 `Successfully edited`**，但回读文件发现**只有最后一条生效**，前两条的改动根本没落盘 —— 而且没有任何报错。
    - **根因**：Edit 是 **read-modify-write**：读磁盘当前内容 → 替换 → 整文件写回。并行执行时多个调用各自读到**同一份旧快照**，最后写回的那个把前面的一起覆盖了。
    - **规避**：① **同一个文件的多个改动必须串行**（一次调用 → 等结果 → 再下一次）；② 改动点 ≥3 处时，直接用 **整篇 `Write` 重写**（读全文一次、写一次，天然原子）；③ 并行只用于**不同文件**。
    - **必查**：批量改完后**必须回读校验**（`grep -c` / 重新 parse），别信 `Successfully edited` 的回执。

60. **录音共享卷下「归属他节点」不等于「读不到」（2026-09-11，#70 落地时实测）**：
    - **现象**：初版录音 URI 解析把「`owner != 本节点`」直接判为 `remote` 并让端点返 **409**；但本方案的录音是 FS/gateway **共享卷**（`./data/recordings:/recordings`），node2 录的文件在 node1 容器里**照样读得到** → 本应 200 的请求被误拒 409。
    - **根因**：`remote`（归属他节点）是**归属语义**，不是**可达语义**。共享卷让"归属他节点"与"本节点读不到"**解耦**了。
    - **正解**：解析层 `recordings.resolve()` **始终给出 `path`**（不因归属他节点就短路）；端点先 `os.path.isfile(path)` —— **可达就 200**（返回文件流），**不可达且 `remote=True` 才 409**（`recording_remote_node`）；`remote` 的判定附加 `scheme in ("", "local")` 约束（`cos://` 恒 `False`，否则上云行也会被误判）。
    - **通用铁律**：只要存储是**共享/可复制**的，"归属"就不能用来推"可达" —— 可达性必须**实际探一下**（`isfile` / 一次 HEAD），别用元数据短路。

61. **dev `docker-compose.override.yml` 里 node2 侧服务没有 restart 策略（2026-09-11 实测）**：
    - **现象**：WSL / docker daemon 重启后，`freeswitch2` / `gateway2` / `sipp-reg` 三个容器全部 `Exited`（`freeswitch2` exit 255、另两个 exit 0），而 node1 侧 5 个容器全自动回来了。
    - **根因**：基础 `docker-compose.yml` 的 5 个服务都带 `restart: unless-stopped`；但这三个服务**只定义在 untracked 的 override 里、没写 restart** → daemon 重启时不被拉起。
    - **易误判**：`docker compose ps` **默认只显示 running**，会让人以为"这些服务根本不存在"；必须 `docker ps -a` 才看到 Exited，或 `docker compose config --services` 看解析到的服务清单。
    - **一行定位**：`docker inspect -f '{{.HostConfig.RestartPolicy.Name}} exit={{.State.ExitCode}}' <容器>`。
    - **修复**：override 里给 `freeswitch2`/`gateway2`/`sipp-reg` 各加 `restart: unless-stopped` → `docker compose up -d`（会 Recreate，见坑 6）。⚠️ 因 override 是 untracked，**本机已修但仓库里没有** → 建议放 `docker-compose.override.example.yml` 模板。

62. **`fs_node.status` 是「最后写入值」，无心跳超时判定 → 僵尸在线（2026-09-11 实测，✅ 已修 #73）**：
    - **现象**：node2 的 `gateway2` 已死停 13 分钟，Web「节点状态」页 node2 **仍显示在线**；`docker ps` 里该节点服务全是 Exited。
    - **根因**：`node_health.py` 的探测者 **只探本节点自己**（`_probe_once` → `_upsert_node(db, NODE_UUID, ...)`，host/port 取本进程 `settings.esl`），并且**只有"探到就写 online、连败就写 offline"两条路径，没有任何"心跳过期 → 置 offline"**。所以写入方（该节点自己的网关进程）一死，**就没有任何进程再去动那一行** → `status` 永远停在最后一次的 1。
    - **一票证据**（`SELECT id,host,status,last_heartbeat_at FROM fs_node;`）：`last_heartbeat_at` 精确停在容器被关停那一刻（03:10:34），而 `status` 仍是 1。
    - **判据**：**永远看 `last_heartbeat_at` 与当前时间的差**（> `~2×node_health_interval`，默认 30s → >60s 即可疑），**不要只看 `status`**。`/api/nodes` 是 `FsNode` 全字段直出，不做超时判定。
    - **危害升级**：Phase2 若按 `fs_node.status` 过滤节点选路 → **会把流量发给僵尸节点**，比不做还糟。**Phase2 前必须修**。
    - **候选方案**：B1 展示层（`/api/nodes` 算 `stale`，超时视为 offline）改动小但 DB 仍脏；B2 落库层（任一活着的网关巡检并把超时的**非本节点**行置 0 + 告警）治本。建议 **B1+B2 同做**，阈值 `max(3×interval, 90s)`。
    - **✅ 修复（#73）**：B1 = `/api/nodes` 每行附 `stale`/`stale_seconds`/`effective_status`（超时强制离线，**原 `status` 保留原值**便于排查）；B2 = `node_health._sweep_stale_nodes()` —— 任一存活节点每轮把超时的**非本节点**行置 0 并落 `node_offline(reason=heartbeat_timeout)`。共用纯函数 `node_health.evaluate() → (stale, age_seconds, effective_status)`（心跳空回落 `created_at`；naive 时间**按 UTC 解释**）。**只扫别人**：本节点由自己的探测负责（含连续失败防抖），两边都写会绕过防抖。

63. **心跳超时阈值必须 ≥ 3×探测周期，否则健康节点互判离线（2026-09-11，#73 验证时实测）**：
    - **现象**：把 `node_health_stale_threshold` 配成 10s（当时周期 30s），`operation_log` 里出现 **node1 自己（清扫者）被判 `heartbeat_timeout`** 的记录，两健康节点来回翻转、刷告警。
    - **根因**：心跳**每周期才写一次**，阈值 ≤ 周期时，对端在本节点下一次心跳到来前就已判定它超时。
    - **正解**：`stale_threshold()` 对**显式配置的值也要钳制下限 `3×周期`**（不能只钳自动值）；低于下限打 warning，**别静默忽略**（与 #53 同型：静默兜底会让人以为配置生效了）。默认 = `max(3×interval, 90s)`。
    - **通用铁律**：任何"活性判定阈值"都必须 ≥ 采样周期的整数倍，否则判据本身就会制造故障。

64. **热配的探测周期变更必须 ≤5s 生效，只分片不定死 deadline 是伪修复（2026-09-11，#73 实测两次踩坑）**：
    - **现象**：E2E 把 `node_health_interval` 从 60s 改回 5s 后，两节点**仍按 60s 写心跳** → 被 20s 阈值互判离线，测试 2 条 FAIL。
    - **根因**：`self._stop.wait(interval)` 一次睡满整个周期，**改小周期要等旧的长周期睡完才生效**。
    - **第一次修复是伪修复**：只把 `wait` 改成 ≤5s 分片，但 **`deadline` 在进入等待时就定死了** → 实测仍要睡满 60s、问题照旧。
    - **正解**：`_wait_interval()` —— 每片 ≤5s 醒来后**重读配置**，按「距上次探测 ≥ 当前周期」判断是否结束（即**不要预先算 deadline**）。
    - **通用铁律**：「热配即时生效」不能只验证"读到了新值"，要验证**行为节奏真的变了**（本例：看 `last_heartbeat_at` 的实际间隔）。

65. **网关主被叫规则 = 路由时硬拒绝，不参与降级（2026-09-11 诊断 CDR id=74）** —— **✅ 已修 #74（A 方案）**：
    - **现象**：CDR `4f788f1a-e907-48fe-ac7e-0ad39d8d70b0`（id=74，caller 80000001 → callee `ccc`）**没有降级走 testgateway**，而是整通 `CALL_REJECTED`：`gateway_id=NULL`、`switch_count=0`、`switch_detail=null`、`reject_reason=denied_by_gw_8_callee_rule:cc?1*`。
    - **候选池其实是两条腿**（实测 `select_outbound_gateway(db,"ccc",None)`）：`[(8,'testgw'), (7,'testgateway')]` —— `ccc` 同时命中 `prefix_route` 的 `ccc`(gw8, 3 字) 与 `cc`(gw7, 2 字)，最长前缀优先故 gw8 居首。
    - **规则裁决**（实测 `evaluate_call_scoped(OWNER_GATEWAY, ...)`）：gw8 `allowed=False`（命中规则 `rule31 gw=8 callee **ALLOW** pattern=cc?1*` → 正则 `^cc.1.*$`，`ccc` 不匹配 → **allow 白名单存在但没命中 = 拦截**）；gw7 `allowed=True`。**次腿本该胜出，却从未被考察。**
    - **根因**：`select_outbound_gateway` 只把「**接入点↔落地策略 (G4)**」前移成候选池过滤（源码注释明写"使 N:M 场景下能**回退到同前缀下一个被允许的网关**"）；**网关维度的主被叫规则没有同等待遇**。`app.py:431`（`_phone_branch`）与 `app.py:481`（`_route_via_ap`）都是：
      ```python
      gw = candidates[0]
      ok, fd, fr = evaluate_call_scoped(db, OWNER_GATEWAY, gw.id, caller, callee)
      if not ok: return build_deny_xml(reason)   # ← 立即挂断，candidates[1:] 从未考察
      ```
      拒绝发生在 `build_outbound_xml` **之前** → failover 链**根本没被构建**（`continue_on_fail` 都没下发）。
    - **语义坑**：`reject_reason=denied_by_gw_8_callee_rule` 读起来像"gw8 不收、换别的"，实际是**整条路由被拒**，运维极易误判。
    - **第二层原因（即便当成腿级失败也不切）**：`CALL_REJECTED` 既不在 gw8 的 `switch_codes`（`503,500,408,486`）也不在 `_ALWAYS_SWITCH_CAUSES` 里。
    - **规则 pattern 语义**（顺带澄清）：`translate_pattern` 把 `*`→`.*`、`?`→**一位任意字符**、其余字面转义，并**全串锚定** `^...$`。所以 `cc?1*` = `^cc.1.*$`（**不是**正则的 `?` 可选量词）；而 `prefix_route.prefix` 是**纯字符串 startswith**（无通配）。两者语义不同，别混。
    - **✅ 修复（#74，2026-09-11，用户拍板 A + 话机 caller_mid 对齐）**：新增 `app._filter_candidates_by_gw_rules(db, candidates, caller, callee)`，把网关规则**前移为候选池过滤**（与 G4 同构）—— 剔除被规则拒绝的网关、保留者交给原排序（前缀/优先级/并发）、**全被拒才拒呼**（reason 带全部命中明细，`_gw_deny_reason` 截断 ≤64 字符兼容 `cdr.reject_reason varchar(64)`）。`_phone_branch`/`_route_via_ap` 双路径接入；**顺序不变式：资格(规则)在前、偏好(并发)在后**。话机分支显式 `caller_mid=caller`（不经 AP 变换），使 CDR `caller_mid/callee_mid` 与 AP 分支口径一致（D1）。验证：单测(容器内真实 DB) `ccc→kept=[7],denied=[8]`；E2E 打 `/fs/dialplan` 返回 `bridge sofia/gateway/testgateway/ccc` 且 XML 含 `cdr_caller_mid=80000001`（不再整通 603）。

66. **fs_cli 报 `Error Connecting` = 密码不对，不是网络问题（2026-09-12）**：
    - fs_cli 默认密码 ClueCon，与环境 ESL 密码不符时 mod_event_socket 直接断连，fs_cli 报 **TCP 层假象** `Error Connecting []`，极易误判为 ESL 未监听。
    - 正解：`fs_cli -p <ESL密码> -x '...'`；TCP 连通性单独用 `bash -c 'echo > /dev/tcp/127.0.0.1/8021'` 验证。

67. **loopback originate 的 L16 codec 坑：INCOMPATIBLE_DESTINATION ≠ 产品回归（2026-09-12）**：
    - `originate ...loopback/xxxx &park()` 做 E2E 时呼叫全挂 `INCOMPATIBLE_DESTINATION`（CDR `switch_count=0/billed=0`），FS 日志 `Hangup sofia/external/... [CS_CONSUME_MEDIA]`，桩侧 `Failed call=N`、FS 收到合法 200 OK 后**主动发 BYE**、全程无 ERROR 日志。
    - 根因：**loopback 假 A 腿的 codec 是 L16/8000（内部直通编码）**，bridge 对外 INVITE 的 SDP codec 与桩 `RTP/AVP 0 8`（PCMU/PCMA）**零交集** → 200 后媒体协商失败。originate 变量加 `absolute_codec_string=PCMU` **无效**。
    - 真实话机 A 腿协商出 PCMU/PCMA 不受影响——**这是测试工具局限，不是产品回归**（判据：历史 `billed=1` 的 CDR 是否仍在）。
    - 排查：`sofia global siptrace on` 抓 INVITE 的 `m=`/`rtpmap` 行看 codec 集合；「收 200 却 BYE」=协商失败非桩问题。另：三合一桩（REGISTER/INVITE 混流）混流竞态会产生畸形双 tag 应答（`To:...;tag=X;tag=X`），注意分辨。

68. **docker cp 替换文件 ≠ 进程重载；容器重启又回退镜像旧层（2026-09-12）**：
    - 改 `src/` 后只 `docker cp` 进容器，**运行中进程仍跑旧模块**（Python 已加载进内存），E2E 与修复无关——表现为「修了没效果」（本次实测：竞态修复 docker cp 后 9 通复测仍是旧表现 global=9/gw7=0）。
    - 直接 `compose restart/up -d` 又会**回退镜像旧层**（COPY 非挂载）。
    - **正解**：改 src 必须 `docker compose build gateway` + `up -d`（强化 #10）；E2E 前用行为差异确认新代码真的在跑。

69. **幂等/升级短路分支必须复检全部闸门：b 凭证升级绕过 limit（P2-a 实测缺口，2026-09-12 已修）**：
    - ensure 兜底凭证（`"b":1`）在 dialplan reserve 升级时，`_LUA_RESERVE` 升级分支**无条件 INCR gw/ap**，绕过 `concurrent_limit`（gw7 limit=1，9 通并发全过、gw7=9）。
    - 修法：升级分支 INCR 前查 gw/ap limit，满则 `BUSY`（凭证保留 b 形态，挂断按 b 语义只减 global，与 release 一致）。
    - 通用教训：**幂等短路、升级、fast-path 分支必须在相同输入下与主路径等价**——尤其限额/权限类检查，漏一处就是旁路。

70. **变换规则 `replace_to` 的 `*` 引用的是 pattern 中 `*` 的捕获组；pattern 无 `*` 时 `\1` 非法 → re.sub 抛 re.error → /fs/dialplan 500（2026-09-12，已修 zcode 会话）**：管理端录入 `pattern=123, replace_to=99*` 即触发整通呼叫挂断，且无 ERROR 级日志只有 traceback。修法：无捕获组时 `*` 按字面量处理+warning（`rules/service._apply_one`）。判据：dialplan 500 + traceback 含 `invalid group reference`。

71. **sipp 3.6 场景 XML 缺 `<?xml?>` 声明 + `<!DOCTYPE scenario SYSTEM "sipp.dtd">` 时同样报无行号 `Unable to load or parse`（2026-09-12，zcode 会话实测）**：与 #54/#55/#58 表象相同根因不同——即使 ASCII 注释、无 `--`、response 全整数，缺头部声明仍 parse 失败；归档 assets 都带头部，手写最易漏。用 `skills/sipp-uas-stub/assets/check-scenario.py` 可提前抓（建议该校验器补查 XML 声明缺失）。

72. **目录下发 `a1-hash` 后，challenge-realm 必须与目录 domain 同源，否则话机注册恒 403（2026-09-13 实测，已修）**：
    - **现象**：话机 REGISTER → FS 回 401 挑战（realm=对外 IP <WSL_HOST_IP>）→ 话机带正确 digest 重发 → **403 Forbidden**；网关侧 `/fs/directory` 却是 200 OK（目录正常返回）。用户视角「全都 403」，且 FS 日志无明显错误。
    - **根因**：安全收敛（89d1bb5）后目录下发 `a1-hash=md5(user:domain:password)`，`domain` 由 `force-register-domain=$${domain}` 决定，而 `vars.xml` 里 `domain=$${local_ip_v4}` = **容器 IP**（172.18.0.2）；同时 internal profile 的 `challenge-realm=auto_from` = **话机 From 域**（对外 IP <WSL_HOST_IP>）。话机按对外 IP 算 digest，FS 用容器 IP 的 a1-hash 比对 → 恒失配。
    - **修法**：`internal.xml` 的 `challenge-realm` 改 `$${domain}`（与 `force-register-domain` 同源）；**不要**改用 `$${external_sip_ip}`（那是 STUN 探测值，可能是公网 IP）。
    - **排查方法**：① 模拟软电话完整 digest 注册脚本（401→带 Authorization 重发）复现，比抓用户话机快；② 对比 `global_getvar domain` 与 401 里的 `realm=`；③ 注意 **docker DNAT 规则带 `! -i br-*`**，从容器内访问宿主对外 IP:5060 不做 DNAT —— **容器内的测试结果不代表话机路径**，必须用宿主（或真实话机）测。
    - **易误判**：症状像「IP 注入错乱」或「profile 挂了」，实际 profile 正常（会回 401）、IP 注入也正常，是**鉴权 realm 口径**问题。
