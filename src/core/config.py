"""配置加载器：读取 config_settings.yaml -> settings（dict）。

配置分三类（详见 docs/config-categories.md）：
- 第1类 部署前配置：本文件即其落地载体，由 deploy.sh 从 config.example.yaml 渲染而来。
- 第2类 热加载配置：存于 DB `system_setting`，经 core/sys_setting.py 实时读取（不在此文件）。
- 第3类 启动快照：本 settings 在 import 期加载一次，进程内恒定；改了须重启才生效
  （DB url、Redis 地址、ESL 密码、node.uuid、salt/jwt 等）。

⚠️ 业务配置（落地网关/路由/费率/接入点/账户）绝不在本文件，全在 DB。
"""
import os
import socket
import yaml

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CFG = os.environ.get("GATEWAY_CONFIG", os.path.join(_BASE, "config_settings.yaml"))


def _apply_defaults(cfg):
    """为可选配置段补齐默认值（缺失或留空时回落）。

    node:       节点标识。多节点部署须唯一；留空回落主机名（单机/开发可用）。
    record:     录音（第1类；具体 URI 抽象由 #70 实现）。
    redis:      留空回落 compose 服务名 redis:6379（即启用容器 Redis）；
                切云 Redis 时显式填 host / port / password / db。
    """
    # node
    n = cfg.get("node")
    if not isinstance(n, dict):
        n = {}
    n.setdefault("uuid", "")
    cfg["node"] = n

    # record（#70 录音 URI 抽象）
    # 两个「视图」要拆开：FS 与网关各有自己的文件系统，`/recordings` 只是二者共享的挂载点。
    #   dir        —— **FS 视角**的写入根，写进 dialplan（record_session 的落点）
    #   local_root —— **网关视角**的读取根（解析 local:// 时拼路径、做 stat/流式回源）
    # 单机部署（compose 把宿主 ./data/recordings 同挂到两个容器）两者填同一个值即可。
    r = cfg.get("record")
    if not isinstance(r, dict):
        r = {}
    r.setdefault("dir", "/recordings")
    r.setdefault("local_root", r.get("dir") or "/recordings")
    r.setdefault("backend", "local")  # local（默认，本次落地）| cos（上云阶段）
    cfg["record"] = r

    # redis
    rd = cfg.get("redis")
    if not isinstance(rd, dict):
        rd = {}
    rd.setdefault("enabled", True)
    rd.setdefault("host", "redis")
    rd.setdefault("port", 6379)
    rd.setdefault("password", "")
    rd.setdefault("db", 0)
    if not rd.get("host"):
        rd["host"] = "redis"
    if rd.get("port") in (None, ""):
        rd["port"] = 6379
    if rd.get("db") in (None, ""):
        rd["db"] = 0
    cfg["redis"] = rd
    return cfg


def _load():
    with open(_CFG, "r", encoding="utf-8") as f:
        return _apply_defaults(yaml.safe_load(f) or {})


settings = _load()


def _resolve_node_uuid():
    """解析节点唯一标识：node.uuid -> esl.fs_node_uuid -> 主机名回落。"""
    u = (settings.get("node") or {}).get("uuid") or ""
    if u:
        return u
    fs = (settings.get("esl") or {}).get("fs_node_uuid") or ""
    if fs:
        return fs
    return socket.gethostname()


# 节点标识（第1/3类，启动期确定，进程内恒定）。供 #69 FS 节点健康检查复用。
NODE_UUID = _resolve_node_uuid()

# 录音（第1/3类，启动期快照 —— 改了须重启网关才生效，与「第3类」语义一致）。
# 由 #70 引入：修掉原先 `record.dir` / `record.backend` 的「死配置」问题
# （配置项/schema/样例三处齐全却全仓无读点，见 PITFALLS #53 同类）。
_record = settings.get("record") or {}
RECORD_DIR = _record.get("dir") or "/recordings"          # FS 写入根（dialplan 下发）
RECORD_ROOT = _record.get("local_root") or RECORD_DIR      # 网关读取根（URI 解析）
RECORD_BACKEND = _record.get("backend") or "local"         # local | cos（上云阶段）
