# SIP Switch Gateway

面向中小企业 VoIP 运营的 **FreeSWITCH 业务网关**：把路由决策、主被叫限制、故障切换、
并发控制、计费（多租户 + 成本 + 预付费）、话单（CDR）与运营后台从 FreeSWITCH 内部
剥离到一个独立的 Python 服务里，FreeSWITCH 只负责信令 / 媒体 / 录音。

> 设计原则：**FS 只做信令+媒体+录音；接入/落地/路由/限制/故障切换/话单打标/计费 全归业务网关层。**
> 禁止把接入逻辑硬编码进 FS 本地 acl/dialplan。

---

## 特性

- **多租户号码体系**：话机 8 位 = 租户号(4) + 分机(4)；接入点直挂账户。
- **路由与限制（M2/P1）**：前缀路由出局、主被叫规则限制、号码变换。
- **逐腿故障切换（T-205）**：落地网关不可达时按 SIP→Q.850 cause 映射自动切下一腿。
- **实时并发（P2）**：全局 / 接入点 / 落地网关三维并发计数，超限回 503。
- **计费（v0.3）**：收入侧费率链（话机→接入点→账户）+ 成本侧（网关→运营商），
  费率链 `NULL` 或 `<=0` 视为「未配置」继续回落；预付费余额不足 603 拒呼、挂断后扣费。
- **运营后台**：FastAPI 提供 REST CRUD + Jinja 管理页（`/admin`），T-301 鉴权（JWT + HttpOnly Cookie）。
- **DB 层校验兜底**：17 个 CHECK 约束，即便直接写库绕过应用层也能兜住号码位数 / NOT NULL / 数值下限 / 费率取值。
- **ESL 纯 socket 客户端（P0）**：`src/fs_esl_socket.py` 自建 ESL 客户端，摆脱 python-esl 绑定
  （ABI 不匹配时不可用）；自带 URL 编码 / Content-Length 分帧 / 60s 探活与看门狗重连。
- **访问控制下沉**：FS 的 SIP profile 默认对公网放开，来源 IP 白名单交给
  服务器防火墙（安全组 / iptables）+ 网关侧接入点授权（未命中即 `603 no_access_point`）。

---

## 架构

```
                 FreeSWITCH (信令/媒体/录音)
                      ▲  ▲  ▲
    mod_xml_curl      │  │  │   ESL 事件
   /fs/dialplan ──────┘  │  └─────────────┐
   /fs/directory ────────┘                │
                      │                   │
              ┌────────┴───────────────────┴────────┐
              │      SIP Switch Gateway (Python)      │
              │  - 路由/限制/故障切换/并发预检          │
              │  - 计费 + 预付费扣费                    │
              │  - CDR 落库 + 约束迁移                  │
              │  - REST 管理 API + 鉴权                │
              └───────────────┬───────────────────────┘
                              │
                        MySQL (sip_switch)
```

- FreeSWITCH 每通呼叫经 `mod_xml_curl` 向网关 HTTP 拉取 **拨号计划**（`/fs/dialplan`）
  与 **目录**（`/fs/directory`）；通话结束经 **ESL** 事件落 CDR。
- 网关配置在 `/usr/local/freeswitch/etc/freeswitch/`（FS 真实配置），与网关源码分离。

---

## 目录结构

```
sip-switch-gateway/
├── config/                  # 配置目录
│   ├── docker/config_settings.yaml   # 运行配置（含密钥，**已被 .gitignore 忽略，禁止提交**）
│   └── docker/config.example.yaml    # 配置模板（无真实值）
├── requirements.txt          # 依赖（钉版本）
├── pytest.ini                # 测试配置（pythonpath=src）
├── src/
│   ├── main.py               # 入口：启动期把 src/ 加入 sys.path 并拉起服务
│   ├── esl_client.py         # ESL 订阅 -> 实时并发 -> HANGUP 落 CDR + 计费
│   ├── fs_provision.py       # 落地网关 FS 配置生成
│   ├── api/
│   │   ├── app.py            # FastAPI 应用 + T-301 鉴权中间件 + /fs/* 端点
│   │   ├── auth.py           # 登录/登出/me（JWT + Cookie）
│   │   ├── crud.py           # 接入点/网关/路由/规则/话机/账户 REST CRUD
│   │   ├── billing.py        # 计费报表/导出/账户默认费率
│   │   ├── dialplan_xml.py   # 拨号计划 XML 生成（T-201/T-202/T-205）
│   │   └── directory_xml.py  # 目录 XML 生成
│   ├── db/
│   │   ├── models.py         # ORM 模型（分区表 cdr 等）
│   │   ├── session.py        # 引擎 + 启动期自迁移注册
│   │   └── migrate.py        # 幂等自迁移（列补齐 + CHECK 约束）
│   ├── core/config.py        # 读取 config_settings.yaml
│   ├── rules/                # 主被叫规则匹配 + 号码变换
│   ├── route/                # 前缀路由 + 接入点解析
│   ├── static/  templates/   # 管理端前端
└── tests/                    # 冒烟测试（pytest）
```

---

## 快速开始

本项目以 **Docker Compose 一键部署** 为主路径，FreeSWITCH / MySQL / 网关 / Redis 全部容器化。
裸机 `python -m src.main` 仅用于本地单元测试 / 调试（见 §0.3），不构成本地完整系统。

### 0.1 一键起（推荐）

```bash
git clone https://github.com/jerrybw/SIP-SWITCH.git
cd SIP-SWITCH

# 全新机器：探测对外 SIP IP + 生成全部密钥 + 渲染配置 + 拉起整套服务
./deploy.sh --up
```

`deploy.sh` 会依次完成：
1. 探测本机对外 SIP IP（eth0 → 默认路由 → `hostname -I` 兜底），写入 `.env` 的 `EXT_SIP_IP`；
2. **一次性生成** MySQL / ESL / Redis / JWT / 密码盐 / admin 明文+哈希 等全部密钥，并写入 `.env` 与 `config/docker/config_settings.yaml`；
3. 从 `.env.example` + `config/docker/config.example.yaml` 模板渲染出真实配置；
4. `docker compose up -d` 拉起 `mysql / freeswitch / gateway / redis / sipp-stub`。

> 幂等：检测到已有配置时只刷新随 IP 变化的项，**不会重生成密钥**（`--force` 才重生成全部）。
> 起的栈默认不含真实运营商网关，需在管理端 `/admin` 手工建落地网关与接入点（见下文）。

### 0.2 WSL 开发环境

```bash
# 已克隆、只想拉起/重启栈，或 WSL 重启后重注对外 IP：
./dev-up.sh
```

> `dev-up.sh` 先全栈 `docker compose up -d`，再 `--force-recreate freeswitch` 把最新探测的
> `EXT_SIP_IP` 注入容器（WSL 重启 IP 会漂移，必须重注）。

### 0.3 仅网关进程（开发 / 调试，需自备 FS + MySQL）

```bash
cd SIP-SWITCH
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp config/docker/config.example.yaml config/docker/config_settings.yaml
# 编辑 config/docker/config_settings.yaml：填入 esl/mysql/auth 真实值
python -m src.main
# 管理后台: http://<host>:8000/admin   健康检查: GET /healthz
```

> ⚠️ `config/docker/config_settings.yaml` 含全部密钥（ESL / MySQL / JWT / admin 哈希），
> 已被 `.gitignore` 忽略，**切勿提交**，也不要在 Issue/PR 中贴出。
> 该路径仅供独立调试网关逻辑；完整系统请走 §0.1 的 docker 一键起。

---

## FreeSWITCH 对接

网关只负责业务逻辑，FreeSWITCH（FS）负责信令 / 媒体 / 录音。下面的版本、安装、配置步骤均已显式列出。

### 4.1 版本要求与检测
- **要求**：FreeSWITCH **1.10.x 或 1.11.x**。本项目在 **1.11.2 源码编译**（Ubuntu 24.04）验证通过。
- **所需模块**：`mod_xml_curl`、`mod_event_socket`、`mod_sofia`（含 external profile）、录音模块。
- **版本检测**（部署前先确认装的是哪个版本）：
  ```bash
  fs_cli -x "version"      # 或 freeswitch --version
  # 期望输出含：FreeSWITCH Version 1.11.2 ...
  ```
  > 网关本身**不强制校验** FS 版本（靠 XML/ESL 协议兼容工作）；若需运行时自检，可在 `esl_client` 连上后执行 `api version` 读取并告警。

### 4.2 安装（源码编译示例，Ubuntu 24.04）
```bash
apt-get update && apt-get install -y git build-essential cmake \
  libssl-dev libcurl4-openssl-dev libpcre3-dev libspeexdsp-dev \
  libsqlite3-dev libldns-dev libedit-dev libopus-dev portaudio19-dev
# ⚠️ 上列为常见依赖示例，请按目标发行版 / 你当时的实际编译环境核对补全
git clone https://github.com/signalwire/freeswitch.git -b v1.11.2 /usr/src/freeswitch
cd /usr/src/freeswitch && ./bootstrap.sh -j && ./configure
make -j"$(nproc)" && make install      # 默认装到 /usr/local/freeswitch
```

### 4.3 关键配置（均在 `/usr/local/freeswitch/etc/freeswitch/`）
- **mod_xml_curl**（让 FS 向网关实时拉拨号计划 / 目录）：编辑 `autoload_configs/xml_curl.conf.xml`：
  ```xml
  <configuration name="xml_curl.conf">
    <bindings>
      <binding name="dialplan">
        <param name="gateway-url" value="http://<gateway>:8000/fs/dialplan" bindings="dialplan"/>
      </binding>
      <binding name="directory">
        <param name="gateway-url" value="http://<gateway>:8000/fs/directory" bindings="directory"/>
      </binding>
    </bindings>
  </configuration>
  ```
  并确认 `modules.conf.xml` 中 `<load module="mod_xml_curl"/>` 未被注释。
- **mod_event_socket**：编辑 `autoload_configs/event_socket.conf.xml`，password 与 `config_settings.yaml[esl].password` 一致：
  ```xml
  <param name="password" value="<esl_password>"/>
  <param name="listen-ip" value="127.0.0.1"/>
  <param name="listen-port" value="8021"/>
  ```
- **落地网关（自动下发，无需手工写）**：在管理端「落地网关」创建 / 编辑 / 删除时，网关会自动调用
  `src/fs_provision.py` 的 `provision(gw)`——把如下 `<gateway>` XML 写入
  `sip_profiles/external/<name>.xml`，并立即执行 `sofia profile external rescan` 让 FS 生效（无需重启 FS）：
  ```xml
  <include>
    <gateway name="{name}">
      <param name="proxy" value="{ip}:{port|5060}"/>
      <param name="realm" value="{ip}:{port|5060}"/>
      <param name="register" value="{auth_type==1 ? true : false}"/>
      <param name="username" value="{username|name}"/>
      <param name="password" value="{password}"/>
      <param name="caller-id-in-from" value="true"/>
    </gateway>
  </include>
  ```
  > ⚠️ 部署约束：`fs_provision` 是**直接写 FS 配置目录 + 调用本机 `fs_cli`**，因此网关服务必须与 FS 部署在**同一台主机**（或网关主机能访问 FS 配置目录且 `fs_cli` 可达）。当前项目即同机部署。
  > ✅ 落地网关 XML 由 `src/gw_bootstrap.py` 在**网关进程启动时**按 DB 全量重建（纯本地写文件，不依赖 FS 就绪），并在 ESL 连上后自动 `sofia profile external rescan`。因此即使共享卷被清空（如 `docker compose down -v` 全新部署）也无需人工干预。只写不删，卷内镜像自带的 `example.xml` 等非网关 XML 不受影响。

---

## 部署形态与对外地址（ext-sip-ip）

FreeSWITCH 需要在 SIP 的 Contact / Via 与 SDP `c=` 行里**通告一个对端可达的地址**，
即 `ext-sip-ip` / `ext-rtp-ip`。它**不是**机器自己的网卡 IP（容器场景尤其如此）。
配错的后果逐级恶化：注册 Contact 错 → 信令回包丢失（`NO_ANSWER` / `408`）→
**SDP 地址错导致单通或全哑（最常见）** → BYE 发不到 → 话单卡在 `end_time IS NULL`。

本项目两种部署形态的取值逻辑不同：

| | DEV（WSL + docker compose） | 生产（Lighthouse 原生部署） |
|---|---|---|
| 部署方式 | `docker compose up -d`（mysql / freeswitch / gateway / sipp-stub） | FS 原生安装（systemd `freeswitch.service`）+ 网关 venv（`sip-gateway.service`） |
| FS 真实 IP | 容器内网 `172.18.0.2`（局域网不可达） | 私网 IP（如 `10.x.x.x`，公网不可达） |
| 对外地址来源 | 环境变量 `EXT_SIP_IP`，由 `dev-up.sh` 每次探测 WSL eth0 IP 注入 | `vars.xml` 的 `stun-set` 向 `stun.freeswitch.org` 探测，写入 `$${external_sip_ip}` |
| 生效值示例 | WSL 宿主 IP（如 `172.22.x.x`） | 云主机公网 IP |
| 关联配置 | `deploy/fs-config/sip_profiles/internal.xml`（占位符 `__EXT_SIP_IP__`，由 entrypoint 渲染） | `sip_profiles/internal.xml` 与 `external.xml` 直接引用 `$${external_sip_ip}` |

> ⚠️ **WSL 重启后 IP 会漂移**（实测 `172.22.x.x` → `172.22.y.y`，docker 网桥子网也会变）。
> 容器虽已配置 `restart: unless-stopped` 会自动拉起，但 `EXT_SIP_IP` 是**容器创建时**注入的，
> 所以**每次 WSL 重启后必须执行 `./dev-up.sh`** 重新注入（脚本探测 IP 并持久化到 `.env`）。

> ⚠️ **STUN 地雷（v0.4 已修复 DEV 侧）**：镜像自带的 `external-ipv6.xml` / `internal-ipv6.xml`
> 我们完全不用，但它们的 `ext-*-ip` 依赖 `$${external_rtp_ip}` / `$${external_sip_ip}`，
> 而这两个变量要靠 STUN 探测。STUN 一旦超时，这两个 profile 建不起来，
> 会**连带整个 `mod_sofia` 加载失败 → SIP 全挂**。现象很有迷惑性：FS 显示 `is ready`，
> 但 `module_exists mod_sofia = false`、`sofia status` 报 `-ERR Command not found`。
> DEV 侧已由 entrypoint 在启动时把这两个文件改名为 `*.xml.disabled`。
> 生产侧同样存在此风险（目前靠 STUN 可用侥幸未触发），建议把 `vars.xml` 的 `stun-set`
> 改成固定公网 IP 的 `set`，或同样禁用这两个 IPv6 profile。

---

## 计费模型

- **收入侧费率链**：话机.rate → 接入点.rate → 账户.rate；任一级 `NULL` 或 `<=0` 视为未配置，继续回落下一级。
- **成本侧费率链**：网关.cost_rate（独立 bill_unit）→ 运营商.cost_rate；同样 `NULL`/`<=0` 回落。
- 消费 = 费率 × `ceil(通话秒 / bill_unit)`；仅接通（有 answer_time 且 talk>0）计费。
- 预付费：`prepaid_enabled=true` 时，dialplan 阶段余额不足回 603 拒呼，挂断后行锁扣费并写 `account_ledger`。

---

## 测试

冒烟为主，不要求覆盖率：

```bash
pip install pytest
pytest
```

- `tests/test_dialplan_xml.py` — 拨号计划 XML 生成（纯函数，无需 DB）。
- `tests/test_auth_t301.py` — 登录/鉴权（纯函数 + TestClient，无需 DB）。
- `tests/test_billing_rate_fallback.py` — 计费链 `_eff_rate` 回落（需 MySQL+ESL，无环境时跳过）。
- `tests/test_migrate_idempotent.py` — 约束迁移幂等（需 MySQL，无环境时跳过）。

> 纯函数测试可本地直接跑；涉及 MySQL/ESL 的测试会在无相应环境时自动 `skip`。
> 本仓库已配置 GitHub Actions（`.github/workflows/tests.yml`），每次 push/PR 自动跑 `pytest` 冒烟。

---

## 安全与开源合规

- 所有密钥集中在 `config_settings.yaml`（已 gitignore）。对外只提供 `config.example.yaml`。
- 管理端 T-301 鉴权：白名单放行 FS 内部回调/健康检查/登录登出/静态资源/管理页外壳，
  其余 `/api/*` 需有效会话 Cookie。
- 传输层建议 nginx 443 反代 `127.0.0.1:8000` 并收口公网 8000。

---

## 版本记录

### v0.4（2026-09-08）— DEV 栈稳定性与对外地址正确性

- **WSL 重启自恢复**：compose 四个服务加 `restart: unless-stopped`；`dev-up.sh` 改为
  先全栈拉起、再 `--force-recreate freeswitch` 注入新 IP。
- **修复 `EXT_SIP_IP` 注入链路断裂**：compose 的 freeswitch `environment` 补上 `EXT_SIP_IP`
  （此前 `dev-up.sh` 探测到的 IP 根本传不进容器），entrypoint 去掉陈旧的硬编码兜底，
  `dev-up.sh` 把探测结果持久化到 `.env`。
- **修复 STUN 超时导致 `mod_sofia` 整体加载失败**：禁用未使用的 `external-ipv6` / `internal-ipv6` profile。
- **SIP profile 默认对公网放开**：移除 `apply-inbound-acl` / `trusted_peers`，访问控制下沉到
  防火墙 + 网关侧接入点授权（ESL 控制面仍保留 `lan`）。
- **ESL 稳定性**：看门狗改非阻塞 `recvEvent(1.0)`，空闲探活成功即重置计时，避免误重连丢事件。
- **CDR**：`dest_ip` / `dest_port` 统一为最终落地网关；终态覆盖 `created_at`。
- **全新部署自愈**：新增启动期全量 `provision` 落地网关 XML（`src/gw_bootstrap.py`），`docker compose down -v` 后无需人工去管理端保存网关；
  同时补齐 `account.customer_id` 可空迁移并同步 `deploy/mysql/init/01-schema.sql`（此前该列可空仅存在于 dev 现库，未回流到 schema 与迁移）。

- **目录兜底域配置化**：`/fs/directory` 的兜底 domain 改从配置 `default_sip_domain` 读取，
  不再硬编码生产私网 IP（该硬编码会在 FS 里生成一个来源不明的 profile 别名）。

### v0.3
计费（收入侧 + 成本侧费率链、预付费扣费）与运营后台（T-301 鉴权）。

---

## License

[MIT](./LICENSE) © 2026 jerrybw@163.com
