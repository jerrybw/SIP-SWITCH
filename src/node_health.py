"""FS 节点级健康检查（#69 / DEP-6）。

探测者 = 网关侧：ESL 本就由网关连，无需新增组件；FS 自己探自己没有意义。
手段 = ESL 建连 + `api show calls`（并发数）+ `api sofia status profile internal reg`（注册数）。

⚠️ Phase 1 语义：**只记录 + 告警，不做摘除** —— 单节点摘掉等于全站停服。
   Phase 2 多节点时再由选路按 node 过滤消费 `fs_node.status`。

状态：1=online / 0=offline / 2=overload。

⚠️ **僵尸在线（B1/B2，2026-09-11 修）**：`fs_node.status` 只是"最后一次写入值"，
   写入方 = 该节点自己的网关。某节点的网关一死（容器没起 / 进程崩 / daemon 重启后
   restart policy 为 no），就**再没有进程去更新那一行** → status 永远停在 1，
   于是"FS 没起来但 Web 显示在线"。这里分两层修：
   - **B1 展示层**：`evaluate()` 按 `last_heartbeat_at` 现算 stale，超时即视为离线
     （`/api/nodes` 输出 `stale` / `stale_seconds` / `effective_status`）。
   - **B2 落库层**：`_sweep_stale_nodes()` 由**任一存活节点**的心跳线程巡检 DB，
     把超时的**其它节点**行置 0 并告警。DB 共享，所以别人也能替它改。
   判据必须看 `last_heartbeat_at`，**不能只看 status** —— 否则 Phase 2 选路会把
   流量发给僵尸节点，比不选路更糟。


节点无需手工建：按 `NODE_UUID`（第1类，deploy.sh 生成）自注册 upsert，
多节点时每个节点各自上报，零配置。
"""
import logging
import re
import socket
import threading
import time
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

# 心跳超时（B1/B2）：last_heartbeat_at 距今超过阈值即视为离线。
# 自动值 = max(3×探测周期, 90s)；也可用 system_setting.node_health_stale_threshold
# 直接指定，但**不低于 3×探测周期**（低于会被钳制并打 warning，原因见 stale_threshold 文档）。
DEFAULT_STALE_MULTIPLIER = 3
DEFAULT_STALE_MIN = 90

# 已被钳制过的阈值（防"每轮刷一条 warning"），仅进程内去重
_clamp_warned = set()

_RE_TOTAL_CALLS = re.compile(r"(\d+)\s+total\.", re.I)
_RE_TOTAL_REG = re.compile(r"Total items returned:\s*(\d+)", re.I)


def _now():
    return datetime.now(timezone.utc)


def _as_aware(dt):
    """把 DB 取出的时间统一成 aware-UTC。

    MySQL DATETIME 不带时区，SQLAlchemy 读回来是 naive；本项目写入侧一律
    `datetime.now(timezone.utc)`，故 naive 值按 UTC 解释。**别按本地时区解释**，
    否则会被 UTC+8 平移 8 小时，判定全错（超时阈值直接失效）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def stale_threshold() -> int:
    """心跳超时阈值(秒)。

    - 自动（未显式配置）：`max(3×探测周期, 90)`
    - 显式 `node_health_stale_threshold`：**不低于 3×探测周期**

    ⚠️ 下限不可省（实测踩过）：心跳是**每个探测周期写一次**，所以健康节点的
    `last_heartbeat_at` 最旧也会接近一个周期。若阈值 ≤ 周期，健康节点会在
    "下一次心跳到来之前"就被**别的节点**的清扫判成离线 → 两个健康节点互判离线、
    来回翻转、刷告警（复现：周期 30s + 阈值 10s，node1/node2 互相把对方置 0）。
    这也是"配置改了不生效"的反面 —— 这里**不是静默忽略**，钳制时会打一条 warning。
    """
    interval = max(1, get_int_setting("node_health_interval", DEFAULT_INTERVAL))
    explicit = get_int_setting("node_health_stale_threshold", 0)
    if explicit and explicit > 0:
        floor = DEFAULT_STALE_MULTIPLIER * interval
        if explicit < floor:
            if explicit not in _clamp_warned:
                _clamp_warned.add(explicit)
                log.warning("[NH] node_health_stale_threshold=%ss 低于安全下限 "
                            "3×探测周期(%ss)，已按 %ss 生效", explicit, floor, floor)
            return floor
        return explicit
    return max(DEFAULT_STALE_MULTIPLIER * interval, DEFAULT_STALE_MIN)


def evaluate(node, now=None, threshold=None):
    """判定一行 fs_node 的心跳是否超时（B1 展示层 / B2 落库层共用，纯函数）。

    返回 `(stale, age_seconds, effective_status)`：
    - 参考时间取 `last_heartbeat_at`，为空时回落 `created_at`（给新行宽限，
      避免"刚注册还没探成功"就被判死）
    - `stale=True` 时 `effective_status` 强制 offline，否则等于原始 status
    - 批量场景（一次巡检多行）请显式传同一 `now`/`threshold`，避免边界抖动

    入参可以是 ORM 行，也可以是 `_to_dict` 后的 dict（都用 getattr/[] 取值）。
    """
    now = now or _now()
    thr = threshold if threshold else stale_threshold()
    raw = int((node.get("status") if isinstance(node, dict) else getattr(node, "status", None)) or 0)
    hb = node.get("last_heartbeat_at") if isinstance(node, dict) else getattr(node, "last_heartbeat_at", None)
    ct = node.get("created_at") if isinstance(node, dict) else getattr(node, "created_at", None)
    ref = _as_aware(hb) or _as_aware(ct)
    if ref is None:
        return False, None, raw
    age = int((now - ref).total_seconds())
    if age < 0:
        age = 0
    stale = age > thr
    return stale, age, (STATUS_OFFLINE if stale else raw)


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


def _sweep_stale_nodes() -> int:
    """B2 落库层：把心跳超时的**其它节点**行置离线并告警；返回被翻转的行数。

    为什么要有它：`fs_node.status` 的写入方是该节点自己的网关进程。
    某节点的网关一死，就再没人更新它那一行 → 永久"僵尸在线"。
    DB 是多节点共享的，所以**任一存活节点**的心跳线程都可以替它把状态改对。

    边界（刻意为之）：
    - 只扫 `node_uuid != NODE_UUID`：本节点由 `_probe_once` 自己负责（含连续失败防抖），
      两边都写会互相打架 —— 例如本节点 FS 正抖，别人抢先把它置 0，就绕过了防抖。
    - 已经离线的行不重复处理（幂等），避免每周期刷同一个告警。
    - 告警走 `alert_if_changed`（自带去重 + 独立事务 + webhook），失败不影响状态落库。
    """
    now = _now()
    thr = stale_threshold()
    db = SessionLocal()
    flipped = []
    try:
        rows = db.scalars(select(FsNode).where(FsNode.node_uuid != NODE_UUID)).all()
        for node in rows:
            stale, age, _eff = evaluate(node, now=now, threshold=thr)
            if not stale or int(node.status or 0) == STATUS_OFFLINE:
                continue
            flipped.append((node.node_uuid, node.host, age, int(node.status or 0)))
            node.status = STATUS_OFFLINE
        if flipped:
            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.warning("[NH] sweep failed: %s", e)
        return 0
    finally:
        db.close()

    for uuid_, host, age, prev in flipped:
        log.warning("[NH] node %s -> OFFLINE (heartbeat timeout %ss, threshold %ss, prev=%s)",
                    uuid_, age, thr, prev)
        alert_if_changed("node_offline", "fs_node", uuid_, {
            "host": host,
            "reason": "heartbeat_timeout",
            "stale_seconds": age,
            "threshold": thr,
            "prev_status": prev,
        })
    return len(flipped)


class NodeHealthProber:
    """FS 节点健康检查后台线程。周期与阈值走第2类配置，热生效（周期变更 ≤5s 生效）。"""

    def __init__(self, interval: int = DEFAULT_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="node-health")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _wait_interval(self, t_last_probe):
        """等到「距上次探测 ≥ 当前配置周期」；每片最多 5s，且**每片重读配置**。

        为什么不能一次 `wait(interval)`，也不能只分片不定死 deadline：
        改小周期必须尽快生效，否则节点仍按旧的长周期写心跳，而对端已按新阈值
        （=3×新周期）判定超时 → 健康节点被误判离线。实测踩过两次：
          ① 一次 `wait(60)`：周期 60s→5s 后仍要睡满 60s；
          ② 只分片但 deadline 在进入时定死：同样要睡满 60s（伪修复）。
        现在的语义：`min(周期, 5s)` 一片一片地等，每片重读配置 —— 调小立即提前结束，
        调大则顺延；因此"实际心跳节奏 ≈ 配置周期"这一前提对 3×周期 的安全边界成立。
        """
        while not self._stop.is_set():
            interval = max(1, get_int_setting("node_health_interval", self.interval))
            elapsed = time.monotonic() - t_last_probe
            if elapsed >= interval:
                return
            self._stop.wait(min(interval - elapsed, 5.0))

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
            t_last_probe = time.monotonic()
            # B2：心跳超时清扫（跑到别的节点头上）。独立 try，
            # 且放在 _probe_once 之后 —— 本节点探测失败也不能少扫一轮。
            try:
                _sweep_stale_nodes()
            except Exception as e:
                log.warning("[NH] sweep loop error: %s", e)
            self._wait_interval(t_last_probe)


def start_node_health(interval: int = DEFAULT_INTERVAL) -> NodeHealthProber:
    p = NodeHealthProber(interval=interval)
    p.start()
    return p
