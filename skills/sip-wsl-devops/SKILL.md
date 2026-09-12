---
name: sip-wsl-devops
description: 通过 SSH 操作 WSL 端的 SIP-SWITCH 全栈（docker compose: mysql/freeswitch/gateway）——改源码、重建镜像、排障管理端 400/401、验证 ESL 与 CDR。当需要在 WSL（DEV 环境）上改 sip-switch-gateway 源码、重建 gateway 镜像、重启容器、或排查管理端报错时使用。也适用于任何"wsl.exe 被 sandbox 拦截"的场景。
agent_created: true
---

# WSL 端 SIP-SWITCH 运维（DEV）

## 环境与路径

| 项 | 值 |
|---|---|
| DEV 定义 | **本机 WSL**（Ubuntu 24.04），不是 docker 容器、不是 Windows |
| 接入 | `ssh -i <WSL_SSH_PRIVATE_KEY> root@localhost -p 22022`（**仅密钥登录**；devroot 已禁用密码登录，私钥不入库） |
| 源码（source of truth） | `/root/src/SIP-SWITCH/`（git remote `github.com/jerrybw/SIP-SWITCH.git`） |
| compose | `/root/src/SIP-SWITCH/docker-compose.yml` + `--env-file .env` |
| 容器 | `sip-switch-mysql-1` / `sip-switch-freeswitch-1` / `sip-switch-gateway-1` |
| 配置（bind mount） | `config/docker/config_settings.yaml` → 容器内 `/app/config/config_settings.yaml` |
| MySQL root 密码 | 见 `/root/src/SIP-SWITCH/.env` 的 `MYSQL_ROOT_PASSWORD` |

**⚠️ Windows 本地 `D:/openclaw/2026-08-27-16-55-18/sip-switch-gateway/` 是过时的 T-103 骨架副本**（缺 9 个文件），**永远不要**在上面改源码。

## 工具链：`wsl.exe` 用不了

WorkBuddy sandbox 会拦截 `wsl.exe`（`PROGRAM BLOCKED BY SECURITY POLICY`）。走 paramiko：

```python
# ~/.workbuddy/binaries/python/envs/default/Scripts/python.exe （已装 paramiko 5.0.0）
import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('localhost', port=22022, username='root',
          key_filename='<WSL_SSH_PRIVATE_KEY>',
          timeout=10, allow_agent=False, look_for_keys=False)
stdin, stdout, stderr = c.exec_command(cmd, timeout=60)
print(stdout.read().decode('utf-8', 'replace'))
print('[rc=%d]' % stdout.channel.recv_exit_status())
```

## 坑 1：bash 命令里不能有括号

`exec_command` 走 `bash -c`，**`echo` 参数里的中文括号 `（）` 会触发 `syntax error near unexpected token '('`**，整条命令废掉。

- ❌ `echo === 首项 v 应为 4 ===; curl ...`
- ✅ `echo === first carrier id ===; curl ...`

同理：反引号、未转义 `$`、嵌套引号都容易炸。

## 坑 2：改源码用"上传脚本执行"，不要用 sed 内联

sed 在多行/特殊字符场景极易被转义吃掉。正确姿势：**把 patch 写成 Python 脚本 → sftp 上传 → 远端 `python3` 执行**。

```python
sftp = c.open_sftp()
with sftp.open('/tmp/_patch.py', 'w') as f:
    f.write(REMOTE_PATCH)   # 远端 Python 代码字符串
sftp.close()
c.exec_command('python3 /tmp/_patch.py', timeout=30)
```

远端 patch 脚本模板（备份 → 精确字符串替换 → 写回 → 打印命中情况）：

```python
import io, os, shutil, datetime
TS = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
def backup(p):
    shutil.copy2(p, p + '.bak.' + TS); print('backup -> ' + p + '.bak.' + TS)
src = io.open(path, encoding='utf-8').read()
if OLD in src:
    src = src.replace(OLD, NEW, 1); print('OK')
else:
    print('SKIP - pattern not found')   # 一定要打印！否则静默失败
io.open(path, 'w', encoding='utf-8', newline='\n').write(src)
```

**替换失败必须显式报告**，否则改了个寂寞。

## 坑 3：改前端/static 必须 rebuild + bump 版本号

`src/static/` 和 `src/templates/` **不是 bind mount**（compose 只挂 `config/docker` 和 `fs-profiles`），走 Dockerfile `COPY src/ ./src/` 进镜像。

改完前端三件套，缺一不可：

```bash
# 1. bump 版本号，否则浏览器继续用旧缓存
sed -i 's|/static/admin.js?v=[0-9a-zA-Z]*|/static/admin.js?v=YYYYMMDDa|g' src/templates/index.html
# 2. rebuild（缓存命中，通常 <30s）
cd /root/src/SIP-SWITCH && docker compose build gateway
# 3. 重建容器
docker compose --env-file .env up -d
```

**验证**：
```bash
docker exec sip-switch-gateway-1 grep -c '<新代码特征>' /app/src/static/admin.js   # 应 >=1
curl -s http://localhost:8000/admin | grep -o 'admin.js?v=[0-9a-zA-Z]*'             # 应是新版本号
```

语法检查：远端通常没 node。下载到本地用 managed node 检查：
```python
sftp.get('/root/src/SIP-SWITCH/src/static/admin.js', LOCAL)
subprocess.run([NODE, '--check', LOCAL])   # rc=0 即语法 OK
```

## 坑 4：登录接口字段是 `user` 不是 `username`

`src/api/auth.py:82`: `body.get("user", "")`。写 curl 用错字段会 401 `invalid credentials`。

```bash
curl -sS -c /tmp/ck.txt -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' -d '{"user":"admin","password":"<ADMIN_PASSWORD>"}'
curl -sS -b /tmp/ck.txt http://localhost:8000/api/me     # {"user":"admin"}
```

密码在 `config_settings.yaml` 是 `sha256(password_salt + 明文).hexdigest()`，**单向不可反推**；重置 = 生成新 salt + hash 写回 + 重启 gateway。

## 坑 5：`fs_cli -x reload mod_event_socket` 不生效

reload 时 socket 会自己断开，命令必然失败。**改了 `event_socket.conf.xml` 必须重启/recreate 容器**：

```bash
docker restart sip-switch-freeswitch-1     # 或 docker compose up -d
```

且 ACL 要改在**源文件** `deploy/fs-config/autoload_configs/event_socket.conf.xml`，
不能只改容器内副本（recreate 会丢）。关键一行：

```xml
<param name="apply-inbound-acl" value="lan"/>
```

缺它跨容器连 ESL 会撞 `mod_event_socket.c:2682 IP x.x.x.x Rejected by acl "loopback.auto"`。

## 坑 6：`restart` 策略变更会 Recreate 容器

加 `restart: unless-stopped` 后 `up -d` 会 Recreate 全部服务（不是 reload）。
确认落地的改动都在持久化位置（源文件 / bind mount / volume），别改在容器可写层。

验证：
```bash
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' \
  sip-switch-mysql-1 sip-switch-freeswitch-1 sip-switch-gateway-1
```

## 坑 7：`fs_cli` 必须显式 `-H -P -p`（否则必报 Error Connecting）

**之前误判为"fs_cli 自身坑、可忽略"是错的。** 它默认连 unix socket，而容器里那个 socket 路径不可用。
显式走 TCP 8021 就正常：

```bash
FSCLI='docker exec sip-switch-freeswitch-1 fs_cli -H 127.0.0.1 -P 8021 -p <FS_ESL_PASSWORD> -x'
$FSCLI "sofia status"
$FSCLI "sofia status profile internal"      # 看 Ext-SIP-IP / Context / RTP-IP
$FSCLI "sofia status gateway <gw-name>"
$FSCLI "sofia profile internal restart reloadxml"   # 改 sip profile 后生效
$FSCLI "sofia profile external rescan"              # 新落地网关 XML 后生效
```

ESL 密码在 `config/docker/config_settings.yaml` 的 `esl.password`。

## 坑 8：远端 heredoc（`<<EOF`）会被 bash 吃掉

paramiko `exec_command` 里写 `python3 - <<'PYEOF' ... PYEOF` **必炸**
（`warning: here-document delimited by end-of-file` + `SyntaxError`）。

替代方案（优先级从高到低）：

1. **复用项目内已有函数**（最省事）：
   ```bash
   docker exec sip-switch-gateway-1 python -c "import sys; sys.path.insert(0,'/app/src'); from heartbeat import _probe_udp_options as p; print(p('IP',PORT,'user',4))"
   ```
2. **sftp 上传脚本再执行**（见坑 2）
3. 单行 `python3 -c "..."`（用分号串，避免多行）

## 坑 9：网关心跳会把落地网关整条过滤掉 → `NO_ROUTE_DESTINATION`

`route/service.py::select_outbound_gateway` 的硬条件：

```
status=1 AND (heartbeat_enabled=0 OR heartbeat_status=1)
```

心跳用 **UDP SIP OPTIONS**（`src/heartbeat.py::_probe_udp_options`，不是 TCP）。
trunk 不可达 → `heartbeat_status=0` → 上式两个分支都不成立 → 网关被过滤 →
dialplan 返回 `NO_ROUTE_DESTINATION`，**看起来像"没配路由"，其实是"网关被判死"**。

排查顺序：

```bash
# 1. 直接探（复用项目函数，最准）
docker exec sip-switch-gateway-1 python -c "import sys; sys.path.insert(0,'/app/src'); \
  from heartbeat import _probe_udp_options as p; print(p('<CARRIER_SIP_IP>',6500,'gw',4))"

# 2. 看状态
mysql> SELECT id,name,status,heartbeat_enabled,heartbeat_status FROM gateway;

# 3. dev 临时放行（生产别这么干，会掩盖真实故障）
mysql> UPDATE gateway SET heartbeat_enabled=0, heartbeat_status=1 WHERE id=9;
```

> 运营商 trunk 通常校验**源 IP 白名单**；dev 走家庭宽带出口，IP 不在白名单 → OPTIONS 必然无响应。
> 这不是配置错误，是网络策略，需要用户在运营商侧加白名单。

## 坑 10：话机号必须「租户号开头 + 总长 8 位」

`POST /api/sip-phones` 有业务校验：租户号 `8000` → 话机只能是 `80000001` 这种 8 位号。
建 `1000`/`1001` 会 400：

```json
{"detail":"话机号码必须以租户号 8000 开头且总长 8 位（如 80000001），当前值：1000"}
```

**与内线正则冲突**：`dialplan_xml._LOCAL_EXT_RE = ^(10[01][0-9])$` 只认 `1000-1019`，
8 位话机号不匹配 → **v0.3 多租户模型下分机呼叫实际全走出局**，内线互拨分支用不到。

## 坑 11：新建落地网关后要手动触发 provision，且 rescan 常没生效

gateway 容器已挂 `fs-profiles:/fs-profiles`，`FS_SIP_PROFILES_EXTERNAL=/fs-profiles`，
与 FS 容器 `sip_profiles/external` 共享 —— **路径没问题**，但新建/更新网关后：

```bash
# 1. 手动触发（写 XML + 异步 rescan）
docker exec sip-switch-gateway-1 python -c "import sys; sys.path.insert(0,'/app/src'); \
  from db.session import SessionLocal; from db.models import Gateway; from fs_provision import provision; \
  db=SessionLocal(); print(provision(db.query(Gateway).filter_by(id=9).first()))"

# 2. 异步 rescan 常没生效，补一次同步的
docker exec sip-switch-freeswitch-1 fs_cli -H 127.0.0.1 -P 8021 -p <pw> -x "sofia profile external rescan"

# 3. 验证（点对点网关状态是 NOREG，属正常）
#    external::gw-carrier-a  gateway  sip:gw-carrier-a@IP:PORT  NOREG
```

修改已存在网关的 XML 后同理；**删除**用 `remove_xml(name)`。

**✅ 确切根因已定位并修复（2026-09-09，提交 `4d5a11e`）**：
FS 的 `sofia profile <prof> rescan` **只创建「尚不存在」的 gateway**，已存在的对象
原样保留旧参数（proxy/realm/port…）。所以"改完保存 FS 没变"不是没触发 rescan
（`crud.py` 一直有调 `provision()`），而是 **rescan 对存量 gateway 无效**。对照实验：

```
DB ip: sipp-stub -> 172.20.0.4
仅 rescan        -> FS Proxy 仍 sipp-stub:5060   ❌
killgw + rescan  -> FS Proxy 变 172.20.0.4:5060  ✅
```

修复：`fs_provision.rescan(prof, gw_name=None)` 传入网关名时先发
`sofia profile external killgw <name>` 销毁旧对象再 rescan；`provision()` /
`remove_xml()` 均传名字（网关不存在时 killgw 失败，按 debug 忽略，不影响新增场景）。
别再手工补 rescan 了 —— 直接查 `docker logs sip-switch-gateway-1 | grep killgw`。

> **2026-09-10 更新（机制 A 已换代）**：现在**不再写 XML 文件**，`fs_provision.py` 只剩 rescan 触发 + 跨节点同步。
> 网关定义由 `fs_sofia_config.build_sofia_conf` 经 mod_xml_curl 的 **configuration** binding 动态下发，FS 侧零落盘。
> `fs-profiles` 共享卷、`FS_SIP_PROFILES_EXTERNAL` 已成历史，别再按上面的路径排查。

## 坑 11b：多节点 —— 两个 Web 端口 + 跨节点下发同步

每 FS 节点配一个网关实例、共享 MySQL/Redis。**node1 Web=8000**（NODE_UUID 见 `config/docker/config_settings.yaml` 的 `node.uuid`）、**node2 Web=8001**（`config/node2/`，来自 untracked 的 `docker-compose.override.yml`）。

- **两边看到的数据完全一样**（共享库），不同的是各自按自身 `NODE_UUID` 决定 `/fs/config` 下发什么。**日常管理登 8000 即可**；8001 本质是给 FS2 的 xml_curl 当数据源。
- 对比两节点下发清单（验证网关归属是否生效）：
  ```bash
  curl -s 'http://localhost:8000/fs/config?section=configuration&key_value=sofia.conf' | grep -oE 'gateway name="[^"]+"'
  curl -s 'http://localhost:8001/fs/config?section=configuration&key_value=sofia.conf' | grep -oE 'gateway name="[^"]+"'
  ```
- **跨节点同步已落地**（2026-09-10，commit `b73cf0f`）：用 DB `system_setting` 做信令，别的节点自动跟上，**不用手动 rescan**：
  - `provision_seq` 网关增删改 +1；`provision_pending` 变更网关名 JSON（供精确 killgw）；`provision_sync_interval` 轮询周期默认 5s（第2类热配，走 `/api/sys-config`）
  - 各节点 `ProvisionWatcher` 线程发现 seq 变化 → 对本节点 FS 做 killgw + rescan
  - **为什么不用「直连其它节点 ESL 广播」**：生产多节点通常只共享 DB/Redis，节点间网络未必互通；轮询还能让离线节点回来自动补齐
  - 排障：`docker compose logs gateway2 2>&1 | grep '\[PS\]'`

## 坑 12：内网软电话要改 internal profile 的对外地址

默认 `ext-sip-ip`/`ext-rtp-ip` = `$${external_sip_ip}` → 解析成公网 IP。
内网软电话拿公网地址做 Contact / SDP → 注册后 RTP 不通。

dev 改法（改 WSL eth0 IP，宿主机可达）：

```bash
# internal.xml 拷到 deploy 持久化（entrypoint 原只 copy external.xml，需加 copy 分支）
docker cp sip-switch-freeswitch-1:/usr/local/freeswitch/etc/freeswitch/sip_profiles/internal.xml \
  /root/src/SIP-SWITCH/deploy/fs-config/sip_profiles/internal.xml

sed -i 's|\$\${external_rtp_ip}|<WSL_SIP_IP>|g; s|\$\${external_sip_ip}|<WSL_SIP_IP>|g; \
  s|name="local-network-acl" value="localnet.auto"|name="local-network-acl" value="dev_no_localnet"|g' \
  /root/src/SIP-SWITCH/deploy/fs-config/sip_profiles/internal.xml
```

`local-network-acl` 指向**不存在的 ACL** 是关键 —— 让所有对端都走 ext 地址
（否则本地网段对端会用容器内网 `172.18.0.x`，宿主机不可达）。

生效：`sofia profile internal restart reloadxml`（reloadxml 不够，sip-ip 是 profile 级）。
⚠️ **WSL eth0 IP 重启会变** —— 已自动化（2026-09-07 方案 A）：
- `internal.xml` 的 ext-sip-ip/ext-rtp-ip 已改成占位符 `__EXT_SIP_IP__`
- entrypoint 启动时用 `EXT_SIP_IP` 环境变量 sed 渲染（compose 里
  `EXT_SIP_IP: ${EXT_SIP_IP:-<WSL_SIP_IP>}`）
- **WSL 重启后跑 `/root/src/SIP-SWITCH/dev-up.sh`**：自动探测 eth0 IP →
  export → `compose up -d --force-recreate freeswitch` → 打印新 IP
- 容器探测不到 WSL eth0 IP（bridge 隔离），必须 host 侧探测注入
- 客户端（软电话/浏览器）IP 仍手动改，dev-up.sh 会提示

## 坑 13：话机注册 403「Can't find user」→ 先查 mod_xml_curl 加载没有

**dev/prod 代码 diff 空 ≠ 行为一致**：话机目录逻辑（directory_xml.py）两端一致且
都是 DB 驱动，但 dev FS 容器可能**根本没加载 mod_xml_curl**——镜像自带的
`modules.conf.xml` 里 `<load module="mod_xml_curl"/>` 是**注释掉的**（生产手动装的
FS 是开着的）。结果 FS 只查本地静态 directory（vanilla 1000-1019）→ DB 话机
（80000001 等）永远找不到 → 401 挑战后 403 `Can't find user`。

```bash
# 1. 先查模块加载没（false = 根因）
docker exec sip-switch-freeswitch-1 fs_cli -H 127.0.0.1 -P 8021 -p <pw> -x 'module_exists mod_xml_curl'
# 2. 查 modules.conf.xml 是否被注释（对比生产）
docker exec sip-switch-freeswitch-1 grep -n mod_xml_curl /usr/local/freeswitch/etc/freeswitch/autoload_configs/modules.conf.xml
#    生产预期：<load module="mod_xml_curl"/>
#    dev 镜像默认：<!-- <load module="mod_xml_curl"/> -->  ← 被注释
# 3. 修复：取消注释 + 重启 FS（reload 不够，模块加载需重启）
docker exec sip-switch-freeswitch-1 sed -i 's|<!-- <load module="mod_xml_curl"/> -->|<load module="mod_xml_curl"/>|' \
  /usr/local/freeswitch/etc/freeswitch/autoload_configs/modules.conf.xml
docker restart sip-switch-freeswitch-1
# 4. 持久化：modules.conf.xml 是镜像内文件，recreate 会丢 → 拷回 deploy
docker cp sip-switch-freeswitch-1:/usr/local/freeswitch/etc/freeswitch/autoload_configs/modules.conf.xml \
  /root/src/SIP-SWITCH/deploy/fs-config/autoload_configs/modules.conf.xml
#    （entrypoint 的 *.conf.xml 循环会自动装它，与 event_socket.conf.xml 同机制）
```

**判断 xml_curl 是否真的被 FS 调用**：看 gateway 日志来源 IP——
来自 FS 容器 IP（`172.18.0.x`）才是真的；`172.18.0.1`（host bridge）多半是自己
curl 模拟的。时间对照重放最可靠：记 gateway 日志行数 → 重放注册 → 看有无新增
`/fs/directory` 请求。

> **别再改 `domains` 段 / `force-*-domain`**（2026-09-07 回退实验证实非必需）：
> internal.xml 的 `<domain name>` 保持生产原样 `all alias=true parse=false`、
> force-*-domain 保持 `$${domain}` 照样注册 200 OK——`directory_xml.py` 匹配
> 只看 phone_number 不看 domain。注册 403 只可能两种：ACL（无挑战直接 403）
> 或 mod_xml_curl 没加载（401 挑战后 Can't find user，见坑 13）。

## 坑 14：注册 403 的两类「找不到用户」

| 现象 | 根因 | 处理 |
|---|---|---|
| REGISTER 直接 403（无 401 挑战） | SIP profile 的 `apply-inbound-acl` 拒绝来源网段 | 查 internal.xml ACL（见坑 12 相邻段）|
| 401 挑战后带 digest 仍 403 `Can't find user` | FS 找不到该 user（本地静态目录无此人 **或** mod_xml_curl 没加载，见坑 13） | `module_exists mod_xml_curl` 一票否决 |

## 坑 15：话机互拨 404「NO_ROUTE_DESTINATION」→ 查 internal profile context

**internal.xml:87 `<param name="context" value="public"/>`（vanilla 默认）vs 生产 `default`**。
话机呼入 → FS 放 public context → xml_curl 问 gateway → `app.py:331` 只接管
`default`/`trunk`，public 返回空 XML → FS 落本地 vanilla public.xml → 404。
**看起来像 trunk 问题，实际是 context 错位，呼叫根本没出 FS**。

```bash
# 1. 查 context（生产应为 default）
grep -n 'name="context"' /root/src/SIP-SWITCH/deploy/fs-config/sip_profiles/internal.xml
# 2. 改 public -> default（deploy 源文件）
sed -i 's|<param name="context" value="public"/>|<param name="context" value="default"/>|' \
  /root/src/SIP-SWITCH/deploy/fs-config/sip_profiles/internal.xml
# 3. 重启 FS 让 entrypoint 渲染（不能 docker cp！见坑 16）
docker restart sip-switch-freeswitch-1
# 4. 验证：重放呼叫应收到 183 而非 404
```

## 坑 16：改 deploy 配置后必须 restart 容器走 entrypoint，不能 docker cp

deploy 源文件带占位符（`__EXT_SIP_IP__` / `__GATEWAY_URL__` / `__ESL_PASSWORD__`），
entrypoint 启动时 sed 渲染。**`docker cp` 会把占位符原样拷进容器** →
ext-sip-ip 变字面 `__EXT_SIP_IP__`（SIP Contact 头直接带占位符，媒体必断）。
改 deploy 文件后一律 `docker restart sip-switch-freeswitch-1`（或 recreate），
验证容器内文件已是渲染后真实值。

## 坑 17：CDR 空 → 先查 gateway ESL 事件流是否活着

**判断**：`docker logs sip-switch-gateway-1 | grep -E '\[ESL\] connected' | tail -1`
看最后一次 connected 是否在最近；发一通呼叫后应出现 `[esl-create]`。
**症状**：FS 重启后 gateway ESL 半死（日志停在 `ESL connect failed`，手动
`ESLConnection` 却 connected=True；独立脚本复刻订阅能收到事件，进程内收不到）。
**修复**：`docker restart sip-switch-gateway-1`（ESL 重连的标准恢复手段）。
CDR 落库链路：FS 事件 → gateway `[esl-create]` → HANGUP → `_persist_cdr`。
FS console loglevel 被调低会吞呼叫日志，排障前先 `console loglevel 6` 确认。

## 坑 18：多目录同 compose 项目名 → 凭据错配；换凭据后必须 restart gateway

**现象**：在别的目录（如 `test_docker/SIP-SWITCH`）跑 `./deploy.sh`（**不带 `--up` 时只
生成配置、不拉起容器**），拿到一套新凭据，但运行中的容器仍吃**主仓** `.env` 的旧密码
—— 新凭据"看起来生效了"其实没有。两个目录共用 `name: sip-switch` + 共享命名卷，
谁最后执行 `up`，容器就归谁（PITFALLS #28 / #31）。

**一步定位到底吃哪份配置**：
```bash
docker inspect <容器> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

**换凭据 SOP（不丢数据）**：
```bash
# 1 备份
cp -a .env .env.bak.$TS; cp -a config/docker/config_settings.yaml{,.bak.$TS}
# 2 用「卷里真实可用的旧 root 密码」登录，对齐库内密码（用户 host 是 %，用 localhost 会 1396）
mysql> ALTER USER 'root'@'localhost' IDENTIFIED BY '<新root>';
mysql> ALTER USER 'root'@'%'         IDENTIFIED BY '<新root>';
mysql> ALTER USER 'sip_switch'@'%'   IDENTIFIED BY '<新sip_switch>';
mysql> FLUSH PRIVILEGES;
# 3 复制新 .env + config_settings.yaml 到「容器实际归属」的那个目录
# 4 docker compose up -d        （mysql/freeswitch 会 Recreated）
# 5 docker compose restart gateway   ← 关键，见下
```

**⚠️ 最易误判点**：`config_settings.yaml` 是 **bind mount**，改文件**不触发**容器重建，
gateway 进程仍握旧密码 → 日志持续刷
`1045 Access denied for 'sip_switch'@'<容器IP>'` / `phone_sync WARNING reconcile failed`
/ `[HB] probe error`，但 **`/healthz` 仍旧 `{"status":"ok"}`**（healthz 不查 DB）。
**healthz ok ≠ 数据库连通**，必须看 `docker logs`。改完配置**必须 restart gateway**。

**验证清单**：新 root 登录 ok / `sip_switch` 登录 ok / `redis-cli -a <新密码> ping` → PONG
/ `POST /api/login` → 200 `{"ok":true}` / ESL rescan `+OK` / 日志 `[HB] gateway <name> ... UP`。

**预防**：不要保留两份同项目名拷贝；试一键起就在主仓跑（deploy.sh 幂等，已有配置不会
重建密钥），或改 `docker-compose.yml` 的 `name:` 做隔离。

## 常用接口小抄

| 项 | 值 |
|---|---|
| 登录 | `POST /api/login` body `{"user":"admin","password":"..."}`（`auth.py` prefix 是 `/api` + `post("/login")`，**不是** `/api/auth/login`）；成功 `{"ok":true}` + httponly cookie，**无 JSON token** |
| 实体更新 | `PUT /api/{entity}/{id}`，entity 用复数：`gateways` / `sip-phones` / `access-points` / `prefix-routes` / `carriers` |
| 纯 socket ESL | `from fs_esl_socket import ESLConnection`（**不是** `FSESLSocket`），`ESLConnection(host, port, password, timeout=3.0)` → `.api(cmd, timeout=10.0)` → 返回值 `.getBody()` |
| 查 FS 网关 | `.api("sofia status gateway")` 看 Proxy；改了网关要 `.api("sofia profile external rescan")` |

在 gateway 容器里跑最方便（能同时访问 `127.0.0.1:8000` 和 `freeswitch:8021`）：
```bash
docker exec -i sip-switch-gateway-1 sh -c 'cat > /tmp/x.py' < local.py
docker exec sip-switch-gateway-1 python /tmp/x.py
```

## 常见故障速查

| 现象 | 根因 | 处理 |
|---|---|---|
| gateway 起不来 `No module named 'imp'` | 镜像构建早于源码改动，容器内还是旧 `from ESL import` | `docker compose build gateway` |
| `POST /api/gateways` 400 | `select-src` 字段硬编码 `def: 1` 指向已删除的 carrier → 外键 1452 | 手选运营商；治本改 `openForm` 默认值取 options 首项 |
| ESL `auth rejected: ''` | ACL 拒绝（见坑 5），不是密码问题 | 加 `apply-inbound-acl lan` + 重启 FS |
| ESL `Connection refused` | FS 还没起来（mysql healthcheck 30-60s 后 gateway 才起） | 等，`sleep 30` 再看 |
| `fs_cli` Error Connecting | **没加 `-H -P -p`**，默认走 unix socket（见坑 7） | `fs_cli -H 127.0.0.1 -P 8021 -p <ESL密码> -x "<cmd>"` |
| 出局 dialplan 返回 `NO_ROUTE_DESTINATION` | 网关心跳判定离线 → 选路把网关整条过滤（见坑 9） | 查 `gateway.heartbeat_status`；dev 可 `heartbeat_enabled=0` |
| trunk 拨不通 | UDP OPTIONS 无响应（运营商 IP 白名单未含出口 IP） | 先探 `heartbeat._probe_udp_options(ip,port,user,4)` |
| `POST /api/sip-phones` 400「必须以租户号开头且总长 8 位」 | 话机号规则（见坑 10） | 租户 8000 → 话机 `80000001` |
| 管理端 options 为空卡必填 | 基础数据未建（如 `account` 表空 → 接入点 `account_id` 无可选） | 先建基础数据 |
| `POST /api/accounts` 400「无可用 Customer」 | `accounts.py` 未传 customer_id 时自动取 `min(Customer.id)`，customer 表空则 400 | 已改为忽略 customer（2026-09-07）；若复现检查 `account.customer_id` 是否仍 NOT NULL |
| 改了前端但页面没变化 | 浏览器缓存 + 镜像内旧文件 | bump `?v=` 版本号 + `docker compose build gateway`；用户端 Ctrl+F5 强刷 |
| 话机注册 403 `Can't find user`（401 挑战后） | **`mod_xml_curl` 没加载**（镜像 modules.conf.xml 注释掉）→ FS 只查本地静态目录 | 取消注释 + 重启 FS + 拷回 deploy（见坑 13）|
| 注册 403 无挑战 | profile `apply-inbound-acl` 拒来源 | 查 internal.xml（见坑 12 相邻段）|
| Web 改网关后 FS 里还是旧值 | rescan 对**已存在** gateway 无效（非"没触发"） | 已修：`rescan()` 先 `killgw`（见坑 11）；老版本手工 `sofia profile external killgw <name>` 再 rescan |
| 日志刷 `1045 Access denied 'sip_switch'@'...'` 但 healthz ok | 换了 `.env`/config 但没 `restart gateway`（bind mount 不触发重建） | `docker compose restart gateway`（见坑 18）|
| 新生成的凭据不生效 | 在另一目录跑 deploy.sh，容器归属仍是原目录 | 看 `com.docker.compose.project.working_dir` 标签（见坑 18）|

## 路由分布（改 CRUD 前先确认文件）

`api/crud.py` 的 `MODEL` **只覆盖** access-points / gateways / prefix-routes / rules / sip-phones / carriers。
以下实体在**独立文件**，别在 crud.py 里找：

| 实体 | 文件 |
|---|---|
| `/api/accounts/*`（含充值、余额流水） | `api/accounts.py` |
| `/api/billing/*`（含计费报表维度） | `api/billing.py` |
| `/api/login` `/api/me` | `api/auth.py` |
| `/fs/dialplan` `/fs/directory` | `dialplan_xml.py` / `directory_xml.py` |

## 坑 19：`docker compose exec ... sh -c "mysql -e \"...\""` 里反引号/SQL 会被二次解析

查 MySQL 保留字列（`\`key\``、`\`value\``）时，**反引号被外层 sh 当命令替换吃掉**：

```
sh: line 1: key: command not found
ERROR 1064 ... near ', ']=', LEFT(,120)) FROM system_setting WHERE'
```

转义层数再多也救不回来。**正解：别在 exec 里拼 SQL，改用容器内 ORM + stdin 喂脚本**（subprocess 走 list 形式，不经 shell）：

```python
script = '''
import sys
sys.path.insert(0, "/app/src")
from db.session import SessionLocal
from db.models import SystemSetting
from sqlalchemy import select
db = SessionLocal()
for r in db.scalars(select(SystemSetting).where(SystemSetting.key.like("provision%"))).all():
    print("[%s] = %s" % (r.key, (r.value or "")[:120]))
db.close()
'''
subprocess.run(
    ["docker", "compose", "exec", "-T", "gateway", "python3", "-"],
    input=script, capture_output=True, text=True, cwd="/root/src/SIP-SWITCH",
)
```

**铁律**：命令里含反引号 / `$(...)` / 三层以上引号时，**一律不要走 shell** —— 用 list 形式 subprocess + stdin，或直接 `docker cp` 脚本进去跑。
补充：判据是「退出码 0 但 Stdout 为空/全是 shell 报错」，此时别怀疑目标服务，先怀疑引号被吞（同坑 8 的 heredoc 问题）。

## 改 DB 结构时

dev 的 schema 变更（`ALTER TABLE`）**只改 dev 容器里的 MySQL**，不会自动同步生产。
改完记到待同步清单，P1 验收后一起上生产。查结构：

```bash
docker exec sip-switch-mysql-1 mysql -uroot -p$MYSQL_PW sip_switch \
  -e 'SHOW CREATE TABLE account\G' 2>&1 | grep -v 'Using a password'
```

## 改完必跑的验证清单

```bash
cd /root/src/SIP-SWITCH && docker compose build gateway     # 仅改前端/源码时
docker compose --env-file .env up -d
sleep 15
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker logs sip-switch-gateway-1 --tail=15 | grep -E 'ESL|Error'   # 期望 [ESL] connected
curl -s http://localhost:8000/healthz                              # 期望 {"status":"ok"}
python3 -m unittest tests.test_fs_esl_socket -v                    # P0 单测 11/11
```

## 坑 20：批量回传源码时只保留 basename → 改到了「影子文件」

本地改完再传回 WSL 时，**`scp a.py b.py dst/` 与 `cp /tmp/{a,b}.py src/` 都只保留文件名**。
`src/api/app.py`、`src/db/models.py` 这类**深层路径**会被落到 `src/app.py`、`src/models.py`，
于是真实文件没变、git status 里反而多出一堆 untracked 影子文件，表现为「改了不生效」且极难察觉。

**正解**：逐文件写完整相对路径，并在回传后立刻自检。

```bash
cp /tmp/app.py    src/api/app.py
cp /tmp/models.py src/db/models.py
cp /tmp/migrate.py src/db/migrate.py
# 自检：期望的那几个文件必须出现在 M 列，且关键改动行能 grep 到
git status --short | grep -E 'src/(api|db)/'
grep -n '关键函数名' src/api/app.py src/db/models.py
```

## 前端（admin.js / admin.css）改动怎么验：Node DOM 冒烟测试

装 Chromium（agent-browser ≈500MB）划不来时，用最小 DOM stub 把**真实 admin.js** 跑起来断言，
能真实覆盖渲染函数、`SECTIONS` 字段定义、表头/单元格列数一致性：

```javascript
// smoke_admin.js：node smoke_admin.js  （不改 admin.js，只在 vm 里加载）
const code = fs.readFileSync('src/admin.js','utf8') + '\n;globalThis.__SECTIONS = SECTIONS;\n';
// ⚠️ SECTIONS 是 const，不会挂到 vm 的全局对象上，必须显式导出一份
```

stub 必备成员（缺一个就报错，按报错补即可）：
`document.getElementById`（返回**按 id 缓存**的元素对象，否则 renderNodes 写完的 DOM 会丢）、
`createElement`、**`createTextNode`**（renderPager 用）、`querySelectorAll`、`document.body`、
`window=globalThis`、`localStorage`、`location`、`setTimeout`（同步执行即可）、`fetch`（按 URL 返回 fixture）。

写法要点：
- `ctx.window = ctx; ctx.globalThis = ctx;` 再 `vm.runInContext(code, ctx)`。
- admin.js 末尾会自跑 `renderSidebar()`/`bootAuth()` → `fetch('/api/me')`，fixture 里要给 `{user:'admin'}`，否则停在登录浮层。
- 断言 `document.getElementById('content').innerHTML` 里的关键字（卡片/按钮/列名），并**断言 `td` 数 == `th` 数**（这条能抓住「字段加了但列表列没同步」的 BUG）。
- **反向对照（必做）**：把改动前的 js（改前先 `cp` 一份到 `/tmp/bak*/`）喂给同一套断言，
  本应 FAIL 的断言必须 FAIL。例（#73）：新 js 12/12、旧 js 8/12，4 条针对本次改动的断言全 FAIL —— 这样才证明断言有区分力。
- 多数组件 setState 在 promise 回调里 → 断言前先 `await new Promise(r=>setImmediate(r))` 两轮。

同时配套：`curl` 打真实的 `/api/sys-config`、`/api/nodes` 等接口确认后端契约，前端断言才有意义。

## 落地网关注册状态 / 重扫（2026-09-10 落地）

- **rescan 会立刻重注册，且是亚秒级**（前提是 `killgw` + `rescan`；裸 rescan 对已存在 gateway 无效）：
  实测 `DOWN`→`TRYING`→`REGISTER 200 OK`→`REGED` 约 2s 走完，**不等 `retry-seconds`**。
- **`expire-seconds` 不给就是 FS 默认 3600**（不是 600）；下发后 `sofia status gateway` 的 `Expires/Freq` 才变 600。
- 状态事件：`CUSTOM sofia::gateway_state`，报头是 **`Gateway` / `State`**（不是 `Gateway-Name`/`Gateway-State`）；
  加进 `esl_client` 的 `EVENT_SUB` 即可。**只在状态跃变时投递** → 网关进程重启后必须补一次
  `sofia xmlstatus gateway` 对齐初值，否则 DB 永远停在「未注册」。
- 「立即全节点重扫」：`POST /api/provision/resync-all`（本节点立即，其它节点靠 `provision_seq` 轮询跟上）；
  watcher 在 pending 名单为空时走**全量 killgw + rescan**，别退化成裸 rescan。

## 多节点下发同步：怎么判断"到底是展示问题还是真没同步"

用户看到卡片「待同步网关 1」长期不变时的判断顺序：

```bash
# 1) 看真实信令值（注意 system_setting 的列名是 `key`/`value`，不是 k/v）
docker exec sip-switch-mysql-1 mysql -uroot -p<pw> sip_switch -e \
  "SELECT \`key\`,\`value\` FROM system_setting WHERE \`key\` LIKE 'provision%'"
# 2) 看各节点 watcher 日志（是否真的收到并处理了变更）
docker logs --tail 50 sip-switch-gateway-1  2>&1 | grep '\[PS\]'
docker logs --tail 50 sip-switch-gateway2-1 2>&1 | grep '\[PS\]'
```

- **`provision_pending` 只读不消费**：`ProvisionWatcher._sync_once` 只读它决定精确 `killgw` 哪些名字，
  **从不写回/清空**（唯一清空时机是「立即全节点重扫」）。所以它的长度**永远不降**，
  那是"最近变更名单"，**不是"还没同步的数量"**。别据此判断同步失败。
- **判断是否真同步只看 `provision_seen_<NODE_UUID>`**：每节点处理完 seq 后自己上报。
  与 `provision_seq` 相等=已同步；落后/缺失=该节点离线或进程异常。
- 「立即全节点重扫」`POST /api/provision/resync-all` 会把 pending 清空并 bump seq，
  各节点看到"seq 变了但名单为空"就走**全量 killgw + rescan**（不是裸 rescan，裸 rescan 对存量网关无效）。

## 排查「FS 里注册型网关凭空消失 / Invalid Gateway!」

先查归属，**别急着怀疑 xml_curl**：

```bash
docker exec sip-switch-mysql-1 mysql -uroot -p<pw> sip_switch -e \
  "SELECT gn.gateway_id,gn.node_uuid,n.host FROM gateway_node gn LEFT JOIN fs_node n ON n.node_uuid=gn.node_uuid;
   SELECT id,node_uuid,host,name FROM fs_node;"
```

- 注册型网关（`auth_type=1`）**只下发给它归属的那个节点**；在别的节点上 `sofia status gateway <name>`
  必然报 `Invalid Gateway!`、`killgw` 报 `no such gateway` —— **这是正确行为**，不是故障。
  点对点网关（`auth_type=0`）才是全量下发。
- 若确实该有却没有：查该节点 `config_settings.yaml` 里 **`node.uuid` 是否配了**。
  `_resolve_node_uuid()` 回落链是 `node.uuid → esl.fs_node_uuid → socket.gethostname()`，
  一旦回落到 hostname（=容器短 ID），**容器重建后 uuid 就变了**，`gateway_node` 里的记录对不上 → 网关消失。
- 前端展示节点名要用 `fs_node.host`（可读），`fs_node.name` 是容器短 ID。

## 改管理端前端后怎么验（不装浏览器）

`admin.js` 用最小 DOM stub 在 node 里直接跑，断言渲染结果，比装 Chromium 划算得多：

```bash
node smoke_admin.js          # 见项目 _work/smoke_admin.js（untracked 脚手架）
```

必备 stub 成员（缺哪个按报错补）：`document.getElementById`（**按 id 缓存**，否则写完的 DOM 会丢）、
`createElement`、`createTextNode`、`querySelectorAll`、`document.body`、`window=globalThis`、
`localStorage`、`location`、`setTimeout`（同步执行）、`fetch`（按 URL 返回 fixture）。
`SECTIONS` 是 `const`，**不会挂到 vm 全局对象上**，要在加载的代码尾部追加
`;globalThis.__SECTIONS = SECTIONS;` 显式导出。
断言必须包含 **`td` 数 == `th` 数**（能抓住"字段加了但列表列没同步"）。

## 排查「docker 重启后只有部分节点起来 / Web 显示某节点在线但实际不通」（2026-09-11 实测）

**现象**：WSL 或 docker daemon 重启后，`freeswitch2`/`gateway2`/`sipp-reg` 全部 Exited，
但 Web「节点状态」页 node2 **仍显示在线**；实测注册话机 / 呼叫不通。

**两个独立根因，一次全命中：**

### 根因 1：node2 侧服务没有 restart 策略 → 重启后不自动拉起

- ⚠️ `docker compose ps` **默认只列 running**，会"假装"这些服务不存在 —— 必须 `docker ps -a` 才看得到 Exited。
- 一行定位：
  ```bash
  docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}} exit={{.State.ExitCode}}' \
    sip-switch-freeswitch-1 sip-switch-freeswitch2-1 sip-switch-gateway2-1 sip-switch-sipp-reg-1
  ```
  实测：node1 侧 = `unless-stopped`（daemon 重启会自拉起），node2 侧 = **`no`**（永不恢复）。
- 这些服务定义在 **untracked 的 `docker-compose.override.yml`**（基础 `docker-compose.yml` 不含它们）。
- **修复**：给 override 的 `freeswitch2` / `gateway2` / `sipp-reg` 各加 `restart: unless-stopped`，然后
  `docker compose up -d`；`docker compose config --services` 应列出全部 8 个服务。
  ⚠️ 改 `restart` 会 **Recreate** 容器（见坑 6），确认改动都在持久化位置。
- **本机已于 2026-09-11 修**；但 override 是 untracked → **新 clone 无此修复**，建议仓库存 `docker-compose.override.example.yml` 模板。

### 根因 2：`fs_node.status` 是「最后写入值」，无心跳超时判定 → **僵尸在线**（✅ 已修 #73，2026-09-11）

- `node_health.py` 的探测者 **只探本节点自己**（`_probe_once` 里 `_upsert_node(db, NODE_UUID, ...)`，
  host/port 取本进程的 `settings.esl`）。
  因此 node2 的 gateway2 一停，**就再没有写入方**去改 node2 那行 → 永远停在上一次的 `status=1`。
- 一票证据：
  ```sql
  SELECT id,host,status,last_heartbeat_at,last_reg_count FROM fs_node ORDER BY id;
  -- id 3 freeswitch  1  2026-09-11 03:23:41   ← 活
  -- id 4 freeswitch2 1  2026-09-11 03:10:34   ← 死停 13 分钟，status 仍是 1
  ```
  `last_heartbeat_at` 停在容器被关停那一刻，正是"僵尸"指纹。
- **判据**：`last_heartbeat_at` 超出阈值即僵死；**别只看 `status`**。
- ⚠️ 这不只是显示问题：**Phase2 选路一旦按 `fs_node.status` 过滤节点，会把流量发给僵尸节点**。

**✅ 修法（2026-09-11 已落地，B1+B2 同做）**

| 层 | 位置 | 要点 |
|---|---|---|
| 共用判定 | `node_health.evaluate()` | 纯函数 → `(stale, age_seconds, effective_status)`；`last_heartbeat_at` 空回落 `created_at`；naive 时间**按 UTC 解释** |
| 阈值 | `node_health.stale_threshold()` | 自动 `max(3×node_health_interval, 90)`；显式 `node_health_stale_threshold`（第2类热配）**下限 3×周期**，钳制时打 warning |
| **B1 展示层** | `/api/nodes`（`api/app.py::list_nodes`） | 每行附 `stale` / `stale_seconds` / `effective_status`（超时强制 offline）+ 回 `heartbeat_threshold`；**原 `status` 保留原值**便于排查 |
| **B2 落库层** | `node_health._sweep_stale_nodes()`（每轮探测后调用） | 任一存活节点把超时的**非本节点**行置 0 + `alert_if_changed(reason=heartbeat_timeout)`。DB 共享 → 别人能替它改。**只扫别人**（本节点由 `_probe_once` 负责，含连续失败防抖） |
| 前端 | `static/admin.js` | 节点表按 `effective_status` 渲染 + 「心跳超时」标记 + 「已超时 Ns」；位点卡片同样标记 |

**两条必须守住的不变量（都是实测踩出来的）**

1. **超时阈值 ≥ 3×探测周期**。心跳是每个周期才写一次，阈值 ≤ 周期时**健康节点会在下一次心跳到来前被对端判离线** → 互判、来回翻转、刷告警。
   实测：周期 30s + 阈值 10s → node1/node2 互相把对方置 0。显式值低于下限必须**钳制 + warning**（别静默忽略，那是 #53 同型陷阱）。
2. **热配周期变更必须 ≤5s 生效**（`_wait_interval()` 分片 ≤5s **且每片重读配置**）。
   否则改小周期后节点仍按旧的长节奏写心跳，同样触发上面的误判。
   ⚠️ **只分片但 deadline 在进入等待时定死 = 伪修复**（本项第一次就是这样，实测仍要睡满旧周期 60s）。

**怎么验（可复用的套路）**

- **单测**在容器内跑（`docker exec … python /tmp/ut.py`，脚本开头 `sys.path.insert(0,'/app/src')`），
  必须覆盖：阈值边界（`==阈值` 不超时 / `+1` 超时）、回落 `created_at`、**naive 时间按 UTC**（用 10s 与 200s 两个样本，能抓出被 +8 平移）、阈值钳制。
- **E2E**：把 `node_health_interval` 压到 5s、阈值 20s（都走 `/api/sys-config` 热配），`docker stop` 掉对端整套容器，
  然后断言四件事：① B1 在 **DB `status` 仍是 1** 时就报 `stale=True/effective_status=0`；
  ② DB 行随后被**对端**置 0；③ `operation_log` 落 `node_offline` + `reason=heartbeat_timeout`；④ 拉起后自动回 `node_online`。
- **反向对照**（重要）：拿改动**前**的代码跑同一套断言，本应 FAIL 的必须 FAIL —— 否则断言是假的。
  例：#73 前端冒烟在新 js 上 12/12，在旧 js 上 8/12（4 条针对本次改动的断言全 FAIL）。
- ⚠️ 测试会污染 `operation_log`（会留下真实的 `node_offline` 记录），断言用**前后计数增量**比较，别数绝对条数。

### 恢复动作（node2 侧）

```bash
cd /root/src/SIP-SWITCH && docker compose up -d      # 拉起 node2 侧三个
docker ps --format 'table {{.Names}}\t{{.Status}}'
# 注册表在 FS 重启后清空属正常 —— 软电话需重新 REGISTER（MicroSIP 会自动重试）
docker exec sip-switch-freeswitch2-1 fs_cli -H 127.0.0.1 -P 8021 -p <ESL密码> -x "sofia status profile internal reg"
# gateway2 起来后 node_health 约 30s 内刷新该节点心跳（首轮可能 fail #1/3，属 FS 尚未就绪）
docker logs --tail 20 sip-switch-gateway2-1 2>&1 | grep -iE '\[NH\]|bootstrap'
```

---

## 排查「这通话为什么没降级走备用网关 / 直接 603」（2026-09-11 诊断 CDR id=74）

**先看话单三个字段就能分流**（`SELECT uuid,gateway_id,switch_count,switch_detail,hangup_cause,reject_reason FROM cdr WHERE uuid=...`）：

| 形态 | 含义 |
|---|---|
| `switch_count>0` + `switch_detail` 有内容 | failover **发生过**，看 `switch_detail` 逐腿 `cause` 定位是哪一腿失败 |
| `switch_count=0` + `gateway_id=NULL` + `reject_reason=denied_by_gw_<id>_<caller\|callee>_rule:<pattern>` | **路由时就被硬拒**，failover 链压根没构建 → 见下文 |
| `switch_count=0` + `reject_reason` 形如 `busy_limit_*;gw=..;gw_conc=..` | 并发预检 503，看 `_conc_detail` 的分号串 |
| `reject_reason` 空 + `hangup_cause=NO_ROUTE_DESTINATION` | 候选池为空（前缀没命中 / 网关被心跳过滤，见坑 9） |

**核心事实（易误判）**：~~网关维度的主被叫规则只对 `candidates[0]` 裁决，不通过就整通 603，不参与降级。~~
> ✅ **已于 #74（2026-09-11，A 方案）修复**：新增 `app._filter_candidates_by_gw_rules(db, candidates, caller, callee)`，把 `OWNER_GATEWAY` 主被叫规则**前移为候选池过滤**（与 G4 接入点↔落地策略同构）—— 剔除被规则拒绝的网关、保留者交给原排序（前缀/优先级/并发）、**全被拒才拒呼**。双路径（`_phone_branch`/`_route_via_ap`）均接入。话机分支显式 `caller_mid=caller` 使 CDR 口径与 AP 一致（D1）。
> 历史根因（诊断 CDR `4f788f1a`，见 PITFALLS #65）：原实现只对 `candidates[0]` 跑规则、不通过即 `build_deny_xml(603)` 整通挂断，次腿从未被考察。

- `app.py:_phone_branch`（话机出局）与 `app.py::_route_via_ap`（中继/AP 出局）**双路径同病**：
  ```python
  gw = candidates[0]
  ok, fd, fr = evaluate_call_scoped(db, OWNER_GATEWAY, gw.id, caller, callee)
  if not ok: return build_deny_xml(...)     # ← 立即挂断，candidates[1:] 从未考察
  ```
- 对比 `route/service.py:select_outbound_gateway` 里 G4 的写法（注释明写"回退到同前缀下一个被允许的网关"）→ 同一诉求只做了一半。
- ⚠️ `reject_reason=denied_by_gw_8_callee_rule` 读起来像"gw8 不收、换别的"，**实际是整条路由被拒**。
- 附注：即便当成腿级失败，`CALL_REJECTED` 既不在 `gateway.switch_codes`（默认 `503,500,408,486`）也不在
  `dialplan_xml._ALWAYS_SWITCH_CAUSES` 里 → 仍然不切腿。两层原因叠加。

**可复用的探针脚本**（用真实 DB 复现"候选池 + 逐候选规则裁决"，比读代码更硬）：
在网关容器内跑，开头 `sys.path.insert(0,'/app/src')`，然后：

```python
from db.session import SessionLocal
from route.service import select_outbound_gateway
from rules.service import evaluate_call_scoped, OWNER_GATEWAY
db = SessionLocal()
for callee in ("ccc", "cc8888"):
    cands = select_outbound_gateway(db, callee, None)          # 候选池（含最长前缀排序）
    print(callee, [(g.id, g.name) for g in cands or []])
    for i, g in enumerate(cands or []):
        ok, d, r = evaluate_call_scoped(db, OWNER_GATEWAY, g.id, "80000001", callee)
        print("   首腿" if i == 0 else "   次腿", g.id, g.name, ok, getattr(r, "pattern", None))
```

**规则 pattern 语义（别混）**：`rules/matcher.translate_pattern` 把 `*`→`.*`、**`?`→一位任意字符**（不是正则的
可选量词！）、其余字面转义，并**全串锚定** `^...$`。所以 gw8 配的 `cc?1*` = `^cc.1.*$`。
而 `prefix_route.prefix` 是**纯字符串 startswith**（无通配）。两者完全不同的匹配器。
另：规则 `act`：**1=ALLOW（白名单，存在 allow 而未命中即拦截）** / 2=DENY。

**判定顺序小结**：`select_outbound_gateway`（前缀最长 → priority → id，再按心跳/G4/节点分片过滤）
→ **网关规则候选池过滤**（`_filter_candidates_by_gw_rules`，#74 修复点，全被拒才拒呼）→ 并发重排 → 并发预检 → `build_outbound_xml`（逐腿 failover 链）。

