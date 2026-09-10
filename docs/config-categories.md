# 三类配置收束（CONFIG CATEGORIES）

> 落地状态：**#68 已实现**。第2类统一实时访问层见 `src/core/sys_setting.py`；第1/3类边界见 `src/core/config.py` 模块 docstring。配套部署器为 `deploy.sh`（DEP-4 混合方案）。

## 总览

| 类别 | 载体 | 生效方式 | 示例 |
|---|---|---|---|
| 第1类 部署前配置 | `config_settings.yaml`（网关）+ `.env`（compose/FS） | 部署期由 `deploy.sh` 渲染，**重启生效** | mysql/redis 地址、ESL 密码、salt/jwt、node.uuid、external IP、端口 |
| 第2类 热加载配置 | DB `system_setting` | **改后即时生效，无需重启** | `phone_sync_interval`、`ap_sync_interval`、`provision_sync_interval`、`webhook_*` |
| 第3类 启动快照 | `core.config.settings`（=第1类里"重启才生效"的子集） | 进程启动加载一次，恒定 | DB url、Redis 地址、ESL host/port/password、node.uuid、salt/jwt |
| （不在配置里）业务配置 | DB 业务表 | 天然热生效 | 落地网关、路由、费率、接入点、账户 |

## 第1类：部署前配置（单文件 `deploy.yaml` 的落地形态）

- `deploy.sh` 读取模板 `config.example.yaml` + `.env.example`，探测 IP、一次性生成密钥，渲染出 `config_settings.yaml` + `.env`。
- **幂等保护**：检测到已有真实配置时仅刷新随 IP 变化的项（`EXT_SIP_IP` / `default_sip_domain`），**不重生成密钥**（密钥必须一次性生成 + 分发，副本各自生成会立刻让登录态失效）。
- 密钥清单：mysql root/user、esl、redis、jwt_secret、password_salt、admin 明文+hash。全部由部署器生成，落地为宿主机文件，副本只读。
- 留空回落默认：mysql/redis host 留空 → compose 服务名；rtp 端口留空 → 20000-20100；node.uuid 留空 → 主机名（生产多节点须显式唯一）。

## 第2类：热加载配置（DB `system_setting`）

- **唯一入口**：`from core.sys_setting import get_setting, get_int_setting`。
- 读取必查库（默认不缓存），保证热加载语义；**严禁在模块级 / import 期缓存值**。
- 现有 key（单位秒，下限 5）：`phone_sync_interval`（话机）、`ap_sync_interval`（接入点）、
  `provision_sync_interval`（多节点网关下发同步轮询周期，默认 5；管理端「节点状态」tab 顶部卡片可改）。
- 非数值 key：`provision_seq` / `provision_pending`（多节点下发信令，由代码写、勿手改）、
  `webhook_gateway_heartbeat_url` / `webhook_node_heartbeat_url`（外部告警地址，空=不推送）。
- 新增第2类项：直接 `INSERT INTO system_setting(key,value,description)` 或走管理端 `PUT /sys-config`，读取端用 `get_int_setting` / `get_setting`。
- FS 侧热加载（dialplan/directory via reloadxml、落地网关 via sofia rescan）仍走机制 A（xml_curl），不在此表。

## 第3类：启动快照（改了要重启）

- = 第1类中"重启才生效"的子集，由 `core.config.settings` 在 import 期加载一次、进程内恒定。
- 包括：DB 连接串、Redis 地址与密码、ESL host/port/password、`node.uuid`、salt/jwt、对外 SIP IP、镜像标签/资源限制。
- 改这些项后必须重建/重启网关（及依赖它们的 FS 容器）才生效。

## 红线

- **业务配置（网关/路由/费率/接入点/账户）绝不进配置文件** —— 否则多副本/多节点立刻不一致。它们全在 DB，靠机制 A / 选路逻辑天然热生效。
- 第2类项不得引用第1/3类里"重启才生效"的字段（如 DB 地址），否则出现"改了不生效"的悖论。
