"""Config loader: read config_settings.yaml -> settings dict."""
import os
import yaml

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CFG = os.environ.get("GATEWAY_CONFIG", os.path.join(_BASE, "config_settings.yaml"))


def _apply_defaults(cfg):
    """为可选配置段补齐默认值（缺失或留空时回落）。

    redis:
      host 留空/缺失 -> 'redis'（compose 服务名，即启用容器 Redis）
      使用云 Redis 时显式填写 host / port / password / db 即可。
    """
    r = cfg.get("redis")
    if not isinstance(r, dict):
        r = {}
    r.setdefault("enabled", True)
    r.setdefault("host", "redis")
    r.setdefault("port", 6379)
    r.setdefault("password", "")
    r.setdefault("db", 0)
    # 显式留空（YAML 里 `host:` 无值 -> None）也要回落默认
    if not r.get("host"):
        r["host"] = "redis"
    if r.get("port") in (None, ""):
        r["port"] = 6379
    if r.get("db") in (None, ""):
        r["db"] = 0
    cfg["redis"] = r
    return cfg


def _load():
    with open(_CFG, "r", encoding="utf-8") as f:
        return _apply_defaults(yaml.safe_load(f) or {})


settings = _load()
