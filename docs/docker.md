# Docker 快速开始（dev / 演示版）

一条命令起 **FreeSWITCH + Python 网关 + MySQL**，用于本地试用与开发。
**不适用于生产**（无资源限制、无备份、无 TLS，默认口令仅作演示）。

## 0. 前置条件

- Docker 24+ 与 Docker Compose v2（`docker compose version`）
- 宿主端口空闲：`8000/tcp`、`5060/udp`、`5080/udp`、`20000-20100/udp`（均可在 `.env` 改；`5060`=internal 话机注册，`5080`=external 落地网关出局）

## 1. 配置

```bash
cp .env.example .env
cp config/docker/config.example.yaml config/docker/config_settings.yaml
```

编辑 `.env`，至少改 `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` / `ESL_PASSWORD`。

生成管理端凭据（填进 `config/docker/config_settings.yaml` 的 `auth` 段）：

```bash
SALT=$(python3 -c "import secrets;print(secrets.token_hex(24))")
JWT=$(python3 -c "import secrets;print(secrets.token_hex(24))")
HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256((sys.argv[1]+sys.argv[2]).encode()).hexdigest())" "$SALT" '你的密码')
echo "password_salt: $SALT"
echo "jwt_secret: $JWT"
echo "admin_password_hash: $HASH"
```

> 三个值都要是 `[A-Za-z0-9_-]` 且以字母开头（配置是裸写无引号的 YAML 标量）。

## 2. 启动

```bash
docker compose up -d
docker compose ps        # 三个服务应为 running / healthy
docker compose logs -f gateway
```

首次启动时 MySQL 会执行 `deploy/mysql/init/01-schema.sql`（**纯结构、无数据**），
建出全部 18 张表（含 CDR 分区表）。网关启动后 `migrate.py` 再幂等补齐列与 CHECK 约束。

## 3. 首次使用

1. 打开 <http://localhost:8000>，用 `config_settings.yaml` 里的 `admin_user` 登录
2. 依次建：**运营商 → 客户 → 账户 → 接入点 / 落地网关 → 话机**
3. 落地网关保存后，网关会把 XML 写进共享卷 `fs-profiles`，并经 **ESL** 通知 FS `rescan`

## 4. 验收自查

- [ ] `docker compose ps` 三服务正常
- [ ] 空库建出 18 张表：`docker compose exec mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "use sip_switch; show tables;"`
- [ ] 管理端可登录、建数据成功
- [ ] 改落地网关后 FS 生效：
      `docker compose exec gateway sh -c 'ls /fs-profiles'`（应有 `<网关名>.xml`）
- [ ] ESL 可用：`docker compose logs gateway | grep -i "rescan\|ESL"`（无 `ESL connect failed`）

## 5. 排错

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 构建时 `[build] WARN: ESL python 绑定未找到` | 所用 FS 镜像没带 ESL python 绑定 | 换带绑定的 FS 镜像，或改从 FS 源码 `libs/esl` 构建（改 `Dockerfile` stage 1） |
| gateway 日志 `ESL connect failed` | `ESL_PASSWORD` 与配置不一致 / `esl.host` 不对 | 核对 `.env` 与 `config_settings.yaml` 的 `esl.password`；`esl.host` 必须是 `freeswitch` |
| FS 起不来或找不到配置 | 镜像内配置目录不是 `/etc/freeswitch` | 改 `.env` 的 `FS_CONF_DIR`（源码安装通常是 `/usr/local/freeswitch/etc/freeswitch`） |
| FS 进程没起来 | 启动命令不匹配 | 改 `.env` 的 `FS_COMMAND`，看 `docker compose logs freeswitch` |
| 通话接通但无语音 | RTP 端口范围与映射不一致 | `.env` 的 `RTP_START/RTP_END` 必须与 `RTP_RANGE` 一致；宿主端口被占用则整体换一段 |
| 页面报缺表 | schema 未执行 | 确认 `deploy/mysql/init/01-schema.sql` 已挂载；删卷重来：`docker compose down -v && docker compose up -d` |
| xml_curl 取不到 dialplan | FS 连不上 gateway | 容器内 `GATEWAY_URL` 默认 `http://gateway:8000`；确认 gateway 已监听 8000 |

## 6. 已知未验证项

本套文件在**未安装 Docker 的环境**中编写，以下需实跑确认：

1. `signalwire/freeswitch` 镜像是否存在、实际版本号、是否编译进 `mod_xml_curl` / `mod_event_socket`
2. 该镜像内 FS 配置目录路径、以及是否自带 ESL python 绑定
3. `switch.conf.xml` 的 RTP 注入 sed 是否命中（不同镜像文件布局可能不同）
4. external profile 的 `X-PRE-PROCESS include` 指向共享卷绝对路径是否被该镜像接受

确认后请回填本文档与 `docs/Docker化设计方案.md`。

## 7. 清理

```bash
docker compose down        # 停容器，保留数据卷
docker compose down -v     # 连数据卷一起删（下次从空库重建）
```
