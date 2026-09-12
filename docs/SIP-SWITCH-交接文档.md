# SIP-SWITCH 交接文档

> 给接手人的总览。特性/架构/快速开始/FS 对接/部署形态/计费模型已在仓库 `README.md` 详述，**此处不再重复**，只讲「项目到哪了、接下来干什么、风险在哪」。
> DEV 环境实操见同目录 `SIP-SWITCH-WSL环境与上手.md`。

---

## 0. 一句话定位

面向中小企业 VoIP 的 **FreeSWITCH 业务网关**：把路由决策、主被叫限制、故障切换、并发控制、计费、话单（CDR）与运营后台从 FreeSWITCH 内部剥离到独立 Python 服务；FS 只负责信令 / 媒体 / 录音（设计原则：FS 不硬编码接入逻辑）。

---

## 1. 交接物与入口

| 物 | 位置 |
|---|---|
| 源码仓库 | `github.com/jerrybw/SIP-SWITCH`（main，唯一真源在 WSL `/root/src/SIP-SWITCH`） |
| 特性/架构/部署 | 仓库 `README.md` |
| 任务真源 | **`docs/ROADMAP.md`**（待办只认它，以代码为准；勿信看板「待开始」项） |
| 部署设计 | `docs/deployment-base-design.md`（DEP-1~9） |
| 配置分类 | `docs/config-categories.md`（第 2 类热配走 `core/sys_setting.py`） |
| 演示起法 | `docs/docker.md` |
| 拆三机评估 | `docs/架构-拆分三机评估.md` |
| 避坑合集 | 仓库 `PITFALLS.md`（65 条）+ 项目记忆 `PITFALLS.md` |
| DEV 环境上手 | **同目录 `SIP-SWITCH-WSL环境与上手.md`** |

---

## 2. 当前交付进度（截至 2026-09-11）

**已合并 main**：
- #64 多节点网关归属（gateway_node 表 + 前端归属节点名）
- #69 FS 节点级健康检查 + webhook 外部告警
- #70 录音 URI 抽象（`local://`）+ 录音下载/播放闭环
- #71 落地网关注册闭环（killgw+rescan+跨节点同步）
- #72 心跳间隔死配置修复 + pending 截断漏重建 + P2-c 并发打标与选路

**⚠️ 未提交（工作树已改、dev 已验证、待 push）—— 接手第一件事就是收尾**：
- **#73** 心跳超时判定 B1+B2：修复 `fs_node` 僵尸在线（`/api/nodes` 算 `effective_status` 超时强制 offline；存活网关巡检把超时非本节点行置 offline）。涉及 `node_health.py` / `alerting.py` / `db/migrate.py` / `static/admin.js` / `templates/index.html`。
- **#74** 网关主被叫规则参与降级（A 方案）+ 话机 `caller_mid` 对齐：`_phone_branch`/`_route_via_ap` 把网关主被叫规则前移成候选池过滤（与 G4 同构），话机分支变量口径对齐 AP。涉及 `api/app.py`。
- 这两项的代码已完成、`docs/ROADMAP.md` 已写「已修复」，**但 git 未提交** → 导致 ROADMAP 与 main 自相矛盾，新人 clone 跑不出文档说的功能。**务必先 commit+push**。

**机制 A**（FS 经 `mod_xml_curl` 反拉配置、不落盘）已落地。

---

## 3. 仍挂起的未拍板项（交给接手人决策）

1. `concurrent_limit_global` **无配置入口**（全局并发限制恒 0）。
2. **多副本 CDR 幂等**：选主 vs 幂等键未定。
3. **上云时机**：`cos://` 已留接口未实装（见 #70 录音抽象）。
4. **STUN 地雷**：生产侧 `vars.xml` 仍靠 STUN 探测 `external_sip_ip`，建议改固定公网 IP 或禁用 ipv6 profile（DEV 侧已修，生产侧未动）。

---

## 4. 风险与注意事项

- **源码真源在 WSL 仓**，本机 `D:/openclaw/2026-08-27...\sip-switch-gateway/` 是过时骨架，只看不改。
- **dev 凭据仅演示**：开源/交接前请 `./deploy.sh --force` 重生成全部密钥；真实密钥在 `.env` / `config_settings.yaml`（均 gitignore）。
- **多节点 dev 依赖 untracked 文件**（`docker-compose.override.yml` / `config/node2/` / `deploy/fs-config/node2/`），含 dev 内部 IP，**严禁入仓**；接手人本地需自行准备或向原 owner 索取。
- `operation_log` 曾有过 #73 测试遗留的脏数据（node 心跳离线/在线抖动记录，含误判 node1），DEV 环境已清理（见 §6）。
- 待办只认 `docs/ROADMAP.md`，TaskList/看板会漂移，做完/取消的项要主动关。

---

## 5. 接手人第一周行动清单

1. 取 WSL 接入私钥，按 `SIP-SWITCH-WSL环境与上手.md` 起单节点 + node2（确认 `/api/nodes` 两节点 online、`/api/gateways` 注册状态正常）。
2. **收尾 #73/#74**：WSL 仓内 `git add` 那 7 个文件 → 跑 secret-scan → `git commit` → `git push`（消除 ROADMAP 与代码不一致）。
3. 跑 `pytest` 确认全绿；过一遍 `tests.yml` CI。
4. 通读 `PITFALLS.md` #1–#65，重点：**#8** IP 型接入点 / **#24** 机制 A 时序 / **#26** 验证纪律 / **#30** killgw+rescan / **#53** 死配置 / **#60** 录音共享卷 / **#62/#63/#64** 心跳超时三连 / **#65** 网关主被叫规则只裁 candidates[0]。
5. 从 §3 挂起项里挑一个开干：先在 ROADMAP 建条目，再动手，四件套走完（改代码 → 更新 ROADMAP ✅+证据 → 关 TaskList → 更新记忆）。

---

## 6. 已完成的清理动作（本次交接准备）

- `operation_log` 第 18–31 行（2026-09-11 #73 测试留下的 node1/node2 心跳 `node_offline`/`node_online` 抖动记录，含误判 node1 的一对）已删除。其余行为更早的 dev/gateway 测试痕迹，未动。

---

## 7. 协作约定（沿用）

- 改完一个任务四件套：改代码 → 更新 ROADMAP（✅+证据） → 关 TaskList → 更新记忆。
- **push 前必跑 secret-scan**；PAT 仅用于一次性 push URL，禁写 `.git/config`。
- 端到端验证只用真实软电话 / sipp 当 UAS 桩，禁手搓 UAC。
- 超范围/超里程碑需求先指出、让用户拍板再动手（敏捷：需求→设计/评审→执行）。
