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
├── config_settings.yaml      # 运行配置（含密钥，**已被 .gitignore 忽略，禁止提交**）
├── config.example.yaml       # 配置模板（无真实值）
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

### 1. 依赖

```bash
cd sip-switch-gateway
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt        # 含 python-esl（见文件内注释）
```

### 2. 配置

```bash
cp config.example.yaml config_settings.yaml
# 编辑 config_settings.yaml：填入 esl/mysql/auth 真实值（见模板内注释）
```

> ⚠️ `config_settings.yaml` 含全部密钥（ESL 密码 / MySQL 密码 / JWT 密钥 / admin 密码哈希），
> 已被 `.gitignore` 忽略。**切勿提交**，也不要在 Issue/PR 中贴出。

### 3. 运行

```bash
python -m src.main
# 管理后台: http://<host>:8000/admin
# 健康检查: GET /healthz
```

> 以下「FreeSWITCH 对接」一节显式列出 FS 的版本、安装与配置。落地网关由网关在管理端创建/编辑/删除时**自动下发**到 FS（见 §4.3），无需手工编辑 FS 配置文件。

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
  > ⚠️ 首次部署若数据库里**已有**落地网关记录（如恢复备份 / 演示数据），这些记录不会在启动时自动下发——需到管理端对每个网关「编辑→保存」触发一次 `provision()`（或手动调用 `fs_provision.provision(gw)`）。之后增删改均自动。

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

---

## 安全与开源合规

- 所有密钥集中在 `config_settings.yaml`（已 gitignore）。对外只提供 `config.example.yaml`。
- 管理端 T-301 鉴权：白名单放行 FS 内部回调/健康检查/登录登出/静态资源/管理页外壳，
  其余 `/api/*` 需有效会话 Cookie。
- 传输层建议 nginx 443 反代 `127.0.0.1:8000` 并收口公网 8000。

---

## License

[MIT](./LICENSE) © 2026 jerrybw@163.com
