# SIP-SWITCH · WSL 开发环境与上手（本地参考）

> 本文件是给**接手人**的 DEV 环境实操手册。特性/架构/部署形态等已在仓库 `README.md` 与 `docs/` 讲清，这里只讲「怎么把环境跑起来、怎么改、坑在哪」。
> 配套交接总览见同目录 `SIP-SWITCH-交接文档.md`。

---

## 1. 环境定位

| 项 | 值 |
|---|---|
| DEV | 本机 **WSL（Ubuntu 24.04）** |
| 源码唯一真源 | WSL `/root/src/SIP-SWITCH/`（`github.com/jerrybw/SIP-SWITCH.git`，main） |
| 接入 | `ssh -i <你的 wsl_dev_key> root@localhost -p 22022` |
| 部署形态 | docker compose（项目名 `sip-switch`） |
| 技术栈 | FreeSWITCH 1.11.2（源码编译镜像 `sip-switch-fs:1.11.2`）+ MySQL 8 + Redis + FastAPI |

**compose 服务**：`mysql` / `freeswitch`(fs1) / `freeswitch2`(fs2, node2) / `gateway` / `gateway2`(node2) / `redis` / `sipp-stub` / `sipp-reg`。

> ⚠️ 本机 `D:/openclaw/2026-08-27-16-55-18/sip-switch-gateway/` 是**过时 T-103 骨架，只看不改**。所有改动在 WSL 仓。

---

## 2. 接入与凭据

- **SSH 私钥** `wsl_dev_key` 在本机工作区（dev 接入用），交接时一并转交或重新生成。
- 所有运行密钥集中在 **`.env` + `config/docker/config_settings.yaml`**，**均已被 `.gitignore` 忽略，禁止入仓**，也不要在 Issue/PR 贴出。
- 真实值由 `./deploy.sh --up` **一次性生成**（MySQL/ESL/Redis/JWT/admin 哈希等）。
- **换凭据后必须 `docker compose restart gateway`**：`config_settings.yaml` 是 bind mount，改文件不触发重建，进程仍握旧密码 → 日志刷 `1045`。
- 管理端：`http://<WSL_IP>:8000/admin`，登录接口 `POST /api/login`，body `{"user":..., "password":...}`（**字段名是 `user`**，不是 `/api/auth/login`）。
  > ⚠️ `dev-up.sh` 末尾打印的 `admin / admin123` 是**占位提示**，真实口令在 `config_settings.yaml`。
- ⚠️ **dev 凭据仅作演示**：开源/正式交接前请 `./deploy.sh --force` 重生成全部密钥。

---

## 3. 单节点起法

```bash
# 首次（全新机器）：探测对外 SIP IP + 生成全部密钥 + 渲染配置 + 拉起整套服务
git clone https://github.com/jerrybw/SIP-SWITCH.git
cd SIP-SWITCH
./deploy.sh --up

# WSL 重启后（IP 会漂移）：探测 eth0 IP → 写 .env → 全栈 up → force-recreate freeswitch 注入新 IP
./dev-up.sh
```

端口：管理 `8000`；话机注册 `5060`（internal profile，需鉴权）；落地出局 `5080`（external profile）。
软电话/浏览器用 WSL 宿主 IP（如 `172.22.x.x`）；`dev-up.sh` 跑完会打印。

---

## 4. 多节点（node2）起法

node2 = `freeswitch2` + `gateway2`，**定义全部在 `docker-compose.override.yml`**（该文件 untracked、禁止入仓，含 node2 专属 `vars.xml` / `config/node2` / 端口 `6060/6080/8001`）。

- docker compose 会**自动合并** override，所以正常 `docker compose up -d` 就连带把 node2 起起来；只想拉基础栈用 `docker compose -f docker-compose.yml up -d`。
- ⚠️ node2 起不来的两大原因：
  1. `config/node2/config_settings.yaml` 缺失（gateway2 的 `NODE_UUID` 在这里，= `d57407aa94a14d54`）；
  2. `deploy/fs-config/node2/vars.xml` 缺失（fs2 把监听端口改成 `6060/6080` 以匹配 host 发布端口）。
- 节点归属：每个 FS 节点**必须在 `fs_node` 表配 `node.uuid`**，否则容器重建即归属断裂。当前 node1=`2a5f89f1b0f0ce74`，node2=`d57407aa94a14d54`。
- node2 管理端：`http://<WSL_IP>:8001/admin`。

---

## 5. 常用命令

| 目的 | 命令 |
|---|---|
| 看网关日志 | `docker compose logs -f gateway` |
| FS 实时状态 | `docker compose exec freeswitch fs_cli -x "sofia status"` |
| 节点健康（超时强制 offline） | `GET /api/nodes`（看 `effective_status`） |
| 网关注册状态 | `GET /api/gateways`（展示归属节点 + 注册状态） |
| 改源码后生效 | `docker compose build gateway`（COPY 非挂载，**必须重建镜像**；无参 `build` 会连带重建 FS 30–60min，勿用） |
| 落地网关配置刷新 | FS 里是旧快照 → 必须 killgw + rescan（rescan 对已存在 gateway 无效） |
| WSL 重启后重注 IP | `./dev-up.sh` |

---

## 6. 关键坑位（接手必读，完整 65 条见 `PITFALLS.md`）

- **WSL 重启 → `EXT_SIP_IP` 漂移** → 媒体/Contact 不可达 → 必须 `dev-up.sh` 重注。
- **改 `src/` → 必须 `docker compose build gateway`**（不是 restart）。
- **改落地网关 → FS 旧快照** → killgw + rescan 才生效。
- **`healthz` ok ≠ DB 连通**（1045 时仍返回 ok）→ 看 `docker logs` 有无 `1045` / `phone_sync WARNING` / `[HB] probe error`。
- **IP 型接入点**靠 `register_host` 精确匹配来源 IP；容器内发包源 IP 是容器自身 IP，手工 curl 必 603。
- **录音共享卷**：归属他节点 ≠ 读不到，端点先 `os.path.isfile` 可达判 200，不可达且 remote 才 409。
- **机制 A 不落盘**：FS 经 `mod_xml_curl` 反拉配置；数据后插须补 rescan。
- **端到端验证只用真实软电话 / sipp 当 UAS 桩，禁手搓 UAC**（`fs_cli originate` 不计费）。

---

## 7. 验证 / 测试

- 单元：`pip install pytest && pytest`（`tests/` 5 个文件；纯函数本地可跑，需 MySQL/ESL 的自动 skip）。
- CI：`.github/workflows/tests.yml`（push/PR 自动跑冒烟）。
- 端到端：sipp 桩场景在 `sipp-uas-stub` skill 的 `assets/`（三合一 `reg_uas.xml` 等），已归档别再手写。
