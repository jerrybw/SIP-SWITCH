"""Redis 客户端封装（Phase1-1：基础设施接入）。

⚠️ 范围声明
-----------
本模块**只提供连接与连通性自检**，当前**不参与任何业务决策**
（选路、计费、并发控制均未使用 Redis）。

按 DEP-2，Redis 是**强制**组件；但「强制」的语义要到 **P2-a（并发原子预留）**
接入时才真正生效 —— 届时按 D3 fail-close（Redis 不可用则拒绝新增呼叫）
/ D4（只拦新增，不拦已在途呼叫）处理。

本阶段 Redis 不可用时**只记录 ERROR 并继续启动**：
Redis 此刻尚未承担任何职责，若现在就阻断启动，等于凭空给全站引入一个新的单点故障。
P2-a 接入时请把 `startup_self_check()` 的失败处理改为按 D3 执行。
"""
import logging
import threading

from core.config import settings

log = logging.getLogger("redis_client")

_client = None
_lock = threading.Lock()
_last_ok = None


def _cfg():
    return settings.get("redis") or {}


def enabled():
    """配置开关。默认 True（DEP-2 强制）。"""
    return bool(_cfg().get("enabled", True))


def client():
    """返回全局 Redis 客户端（懒加载、线程安全）。"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        # 延迟导入：未启用/未安装时不影响其他模块启动
        import redis
        c = _cfg()
        host = c.get("host") or "redis"
        port = int(c.get("port") or 6379)
        password = c.get("password") or None
        db = int(c.get("db") or 0)
        _client = redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=True,
            health_check_interval=30,
        )
        log.info("[redis] client created: %s:%s db=%s", host, port, db)
        return _client


def ping():
    """探活并刷新缓存结果。永不抛异常，返回 bool。"""
    global _last_ok
    try:
        _last_ok = bool(client().ping())
    except Exception as e:  # noqa: BLE001 - 探活不应中断调用方
        _last_ok = False
        log.error("[redis] ping failed: %s", e)
    return _last_ok


def available():
    """返回上次探活结果；从未探过则现探一次。"""
    if _last_ok is None:
        return ping()
    return _last_ok


def startup_self_check():
    """启动自检：打日志，**不阻断进程启动**（理由见模块 docstring）。"""
    if not enabled():
        log.warning("[redis] disabled by config (redis.enabled=false)")
        return False
    c = _cfg()
    ok = ping()
    log.info(
        "[redis] startup self-check %s (host=%s port=%s)",
        "OK" if ok else "FAILED",
        c.get("host") or "redis",
        c.get("port") or 6379,
    )
    if not ok:
        log.error(
            "[redis] unavailable — 本阶段不阻断启动（Redis 尚未参与业务决策）；"
            "P2-a 接入后须按 D3 fail-close 拒绝新增呼叫"
        )
    return ok
