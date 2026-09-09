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

## 1.5 首次启动前：构建 FreeSWITCH 镜像

Docker Hub 上没有可用的 FS 运行时镜像（详见 §6），因此**必须本地编译一次**：

```bash
# 1) 拉取官方编译基座（约 1.4 GB）
docker pull signalwire/freeswitch-public-base:latest

# 2) 从 v1.11.2 源码编译并打标签（JOBS 建议设为主机核数）
docker build -f deploy/fs-image/Dockerfile   --build-arg FS_REF=v1.11.2   --build-arg JOBS=8   -t sip-switch-fs:1.11.2 .
```

产物是与生产同版本（1.11.2）的运行时镜像，内含 `mod_xml_curl`、`mod_event_socket` 与 ESL python 绑定。
编译一次后 `docker compose up -d` 会直接复用，不再重编。编译约 30–60 分钟，取决于 CPU。

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
3. 落地网关保存后，FS 经 mod_xml_curl 的 configuration 绑定向网关 `/fs/config` 重新拉取
   `sofia.conf`（**机制 A：不落盘**，网关定义以 DB 的 `gateway` 表为唯一事实来源）；
   网关仅需经 **ESL** 通知 FS `rescan` 触发重拉，不写任何 XML 文件

## 4. 验收自查

- [ ] `docker compose ps` 三服务正常
- [ ] 空库建出 18 张表：`docker compose exec mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "use sip_switch; show tables;"`
- [ ] 管理端可登录、建数据成功
- [ ] 改落地网关后 FS 生效：
      `docker compose exec freeswitch fs_cli -H 127.0.0.1 -P 8021 -p "$ESL_PASSWORD" -x 'sofia status'`
      （应见 `external::<网关名>` 指向对应落地地址）
- [ ] ESL 可用：`docker compose logs gateway | grep -i "rescan\|ESL"`（无 `ESL connect failed`）

## 5. 排错

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 构建时 `[build] WARN: ESL python 绑定未找到` | 所用 FS 镜像没带 ESL python 绑定 | 换带绑定的 FS 镜像，或改从 FS 源码 `libs/esl` 构建（改 `Dockerfile` stage 1） |
| gateway 日志 `ESL connect failed` | `ESL_PASSWORD` 与配置不一致 / `esl.host` 不对 | 核对 `.env` 与 `config_settings.yaml` 的 `esl.password`；`esl.host` 必须是 `freeswitch` |
| FS 起不来或找不到配置 | 镜像内配置目录与 `FS_CONF_DIR` 不一致 | 自建镜像已默认 `/usr/local/freeswitch/etc/freeswitch`；换镜像时改 `.env` 的 `FS_CONF_DIR` |
| FS 进程没起来 | 启动命令不匹配 | 改 `.env` 的 `FS_COMMAND`，看 `docker compose logs freeswitch` |
| 通话接通但无语音 | RTP 端口范围与映射不一致 | `.env` 的 `RTP_START/RTP_END` 必须与 `RTP_RANGE` 一致；宿主端口被占用则整体换一段 |
| 页面报缺表 | schema 未执行 | 确认 `deploy/mysql/init/01-schema.sql` 已挂载；删卷重来：`docker compose down -v && docker compose up -d` |
| xml_curl 取不到 dialplan | FS 连不上 gateway | 容器内 `GATEWAY_URL` 默认 `http://gateway:8000`；确认 gateway 已监听 8000 |

## 6. 未验证项 → 实跑结论（2026-09-06）

本套文件最初在**未安装 Docker 的环境**中编写（下称「盲写」）。以下 4 项已在 WSL2 + Docker 24.0.9 环境核实：

| # | 盲写假设 | 实跑结论 |
|---|---|---|
| 1 | `signalwire/freeswitch` 镜像存在 | ❌ **不存在**。经镜像源返回 `denied`、直连 Docker Hub 返回 `connection reset by peer`。同名可拉的只有 `signalwire/freeswitch-public-base`，但它是**编译基座**（只有依赖与工具链），`which freeswitch` 为空、无 `/usr/local/freeswitch` |
| 2 | 配置目录为 `/etc/freeswitch`，且镜像自带 ESL python 绑定 | ❌ 均不成立。源码安装（prefix=`/usr/local/freeswitch`）的配置在 **`/usr/local/freeswitch/etc/freeswitch`**；ESL python 绑定不在镜像内，需从源码 `libs/esl` 用 `make py3mod` 构建 |
| 3 | `switch.conf.xml` 的 RTP 注入 sed 命中 | ⏳ 待 compose 实跑确认 |
| 4 | external profile 的 `X-PRE-PROCESS include` 接受共享卷绝对路径 | ⏳ 待 compose 实跑确认 |

顺带核实的其他硬事实：

- SignalWire 官方 apt 源 `freeswitch.signalwire.com/repo/deb/debian-release` 返回 **401 Unauthorized** —— 匿名装不了包，需 signalwire.com 账号 PAT。
- `files.freeswitch.org/releases/freeswitch/freeswitch-1.11.2.tar.gz` 返回 **404**；但 github 上 `signalwire/freeswitch` 的 **v1.11.2 tag 存在**，故自建走源码编译。
- 生产环境（Ubuntu 24.04）的同版本构建参数为 `./configure --prefix=/usr/local/freeswitch --disable-core-pgsql`，已原样写进 `deploy/fs-image/Dockerfile`。
- FS v1.11.2 在 github 上没有 `.gitmodules`，`libs/` 里不含 sofia-sip / spandsp / libks，这四个依赖必须由外部提供（生产环境装在 `/usr/local/lib`）。
- `configure.ac` 表明 `libks2` 仅在启用 `mod_verto` 时必需、`signalwire_client2` 仅在启用 `mod_signalwire` 时必需；自建镜像已禁用这两个模块（WebRTC / SignalWire 云连接用不到），从而跳过其编译。
- 发布包 `modules.conf` 默认把 `xml_int/mod_xml_curl` 注释掉了，必须打开，否则编出来的 FS 没有 xml_curl 模块（本项目靠它拉 dialplan / directory）。
- `freeswitch` 二进制在 `/usr/local/freeswitch/bin`，**不在 PATH**；自建镜像已把该路径写进 `ENV PATH`。
- 社区镜像 `ghcr.io/chapimenge3/freeswitch:latest` 默认加载 `mod_xml_curl`，可作临时替代快速验证（版本为 1.10.x，与生产 1.11.2 不一致）。

## 7. 清理

```bash
docker compose down        # 停容器，保留数据卷
docker compose down -v     # 连数据卷一起删（下次从空库重建）
```
