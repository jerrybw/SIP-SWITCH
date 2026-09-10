"""FS 节点级健康检查（#69 / DEP-6）。

探测者 = 网关侧：ESL 本就由网关连，无需新增组件；FS 自己探自己没有意义。
手段 = ESL 建连 + `api show calls`（并发数）+ `api sofia status profile internal reg`（注册数）。

⚠️ Phase 1 语义：**只记录 + 告警，不做摘除** —— 单节点摘掉等于全站停服。
   Phase 2 多节点时再由选路按 node 过滤消费 `fs_node.status`。

状态：1=online / 0=offline / 2=overload。

节点无需手工建：按 `NODE_UUID`（第1类，deploy.sh 生成）自注册 upsert，
多节点时每个节点各自上报，零配置。
"""
import logging
import re
import socket
import threading
from datetime import datetime, timezone

from sqlalchemy import select

from alerting import alert_if_changed
from core.config import settings, NODE_UUID
from core.sys_setting import get_int_setting
from db.session import SessionLocal
from db.models import FsNode
from fs_esl_socket import ESLConnection

log = logging.getLogger("node_health")

STATUS_OFFLINE = 0
STATUS_ONLINE = 1
STATUS_OVERLOAD = 2
STATUS_NAME = {STATUS_OFFLINE: "offline", STATUS_ONLINE: "online", STATUS_OVERLOAD: "overload"}

DEFAULT_INTERVAL = 30        # 探测周期(秒)，可被 system_setting.node_health_interval 覆盖
DEFAULT_FAIL_THRESHOLD = 3   # 连续失败达此值判离线，可被 node_health_fail_threshold 覆盖

_RE_TOTAL_CALLS = re.compile(r"(\d+)\s+total\.", re.I)
_RE_TOTAL_REG = re.compile(r"Total items returned:\s*(\d+)", re.I)


def _now():
    return datetime.now(timezone.utc)


def _probe_esl(host, port, password, timeout=3.0):
    """探测一次：返回 (ok, concurrency|None, reg_count|None)。

    每次探测新建短连接，避免与 `esl_client` 的事件长连接争抢同一 socket。
    """
    con = None
    try:
        con = ESLConnection(host, port, password, timeout=timeout)
        if not con.connected():
            return False, None, None
        calls = None
        ev = con.api("show calls")
        if ev is not None:
            m = _RE_TOTAL_CALLS.search(ev.getBody() or "")
            if m:
                calls = int(m.group(1))
        reg = None
        ev2 = con.api("sofia status profile internal reg")
        if ev2 is not None:
            m2 = _RE_TOTAL_REG.search(ev2.getBody() or "")
            if m2:
                reg = int(m2.group(1))
        return True, calls, reg
    except Exception as e:
        log.warning("[NH] probe error: %s", e)
        return False, None, None
    finally:
        if con is not None:
            try:
                con.disconnect()
            except Exception:
                pass


def _upsert_node(db, node_uuid, host, port) -> FsNode:
    """按 node_uuid 自注册：无则建（name=主机名），有则复用。"""
    node = db.scalar(select(FsNode).where(FsNode.node_uuid == node_uuid))
    if node is None:
        node = FsNode(
            node_uuid=node_uuid,
            name=socket.gethostname(),
            host=host,
            esl_port=int(port),
            status=STATUS_ONLINE,
            last_concurrency=0,
            last_reg_count=0,
            fail_count=0,
            created_at=_now(),
        )
        db.add(node)
        db.flush()
        log.info("[NH] registered node %s (%s:%s)", node_uuid, host, port)
    elif node.host != host or int(node.esl_port or 0) != int(port):
        # 配置变更（如换 FS 主机/端口）：以当前配置为准刷新地址
        node.host = host
        node.esl_port = int(port)
    return node


def _probe_once(db) -> None:
    esl_cfg = settings.get("esl") or {}
    host = esl_cfg.get("host") or "127.0.0.1"
    port = int(esl_cfg.get("port") or 8021)
    password = esl_cfg.get("password") or ""

    fail_threshold = max(1, get_int_setting("node_health_fail_threshold", DEFAULT_FAIL_THRESHOLD))
    global_max = get_int_setting("node_max_concurrency", 0)  # 0 = 不限制

    node = _upsert_node(db, NODE_UUID, host, port)
    ok, calls, reg = _probe_esl(host, port, password)
    now = _now()

    if not ok:
        node.fail_count = (node.fail_count or 0) + 1
        if node.fail_count >= fail_threshold:
            if node.status != STATUS_OFFLINE:
                node.status = STATUS_OFFLINE
                alert_if_changed("node_offline", "fs_node", NODE_UUID, {
                    "host": host, "esl_port": port, "fail_count": node.fail_count,
                    "last_seen": node.last_heartbeat_at.isoformat() if node.last_heartbeat_at else None,
                })
                log.warning("[NH] node %s OFFLINE after %d fails", NODE_UUID, node.fail_count)
        else:
            log.warning("[NH] node %s probe fail #%d/%d (debouncing)",
                        NODE_UUID, node.fail_count, fail_threshold)
        db.commit()
        return

    # 探测成功
    recovered = (node.status != STATUS_ONLINE)
    node.fail_count = 0
    node.last_heartbeat_at = now
    node.last_health_time = now  # 兼容原有字段
    if calls is not None:
        node.last_concurrency = calls
    if reg is not None:
        node.last_reg_count = reg

    limit = node.max_concurrency if node.max_concurrency else global_max
    if limit and limit > 0 and calls is not None and calls >= limit:
        node.status = STATUS_OVERLOAD
        alert_if_changed("node_overload", "fs_node", NODE_UUID, {
            "host": host, "concurrency": calls, "limit": limit,
        })
        log.warning("[NH] node %s OVERLOAD concurrency=%s limit=%s", NODE_UUID, calls, limit)
    else:
        node.status = STATUS_ONLINE
        if recovered:
            alert_if_changed("node_online", "fs_node", NODE_UUID, {
                "host": host, "concurrency": calls, "reg_count": reg,
            }, level="info")
            log.info("[NH] node %s ONLINE (calls=%s reg=%s)", NODE_UUID, calls, reg)
    db.commit()


class NodeHealthProber:
    """FS 节点健康检查后台线程。周期与阈值走第2类配置，热生效。"""

    def __init__(self, interval: int = DEFAULT_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="node-health")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                db = SessionLocal()
                try:
                    _probe_once(db)
                finally:
                    db.close()
            except Exception as e:
                log.warning("[NH] loop error: %s", e)
            # 周期每次重读，改 system_setting 后立即生效
            self._stop.wait(get_int_setting("node_health_interval", self.interval))


def start_node_health(interval: int = DEFAULT_INTERVAL) -> NodeHealthProber:
    p = NodeHealthProber(interval=interval)
    p.start()
    return p
