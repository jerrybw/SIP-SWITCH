"""落地网关心跳探测（T-204）。

探测方式：UDP SIP OPTIONS（自发包，带网关 name/user），而非 TCP connect。
原因：我们的落地网关是 register=false 的 UDP SIP trunk（对端只听 UDP，TCP 永远超时），
原 TCP 探测会把全部网关误判离线、选路被全杀（历史有人因此把 heartbeat_enabled 全置 0 止血）。

分组探测（用户确认）：按 (ip, port) 分组，每组只发一次 OPTIONS；
同组所有网关共享一个探测结果——"探测某 IP 正常 → 该 IP 上所有 gateway 都视为正常"。
这是当前最贴近的粒度：对端是同一台 SBC，OPTIONS 只能探到「链路可达 + SBC 认这个 user」，
区分不了单 trunk 是否真能呼出，故以 IP 为准。

防抖：连续 FAIL_THRESHOLD 次失败才置离线；任一次成功立即恢复（纳回）。
#69 起：上下线翻转时调用 alerting.alert_if_changed 落 operation_log（状态未变去重），
闭环坑位 #18「心跳告警未闭环」；仍只落库，不接外部通道（后续可从 operation_log 消费）。

⚠️ 探测周期（2026-09-10 修，PITFALLS #53）：`gateway.heartbeat_interval` 曾是**死配置**
——DB 列 / schema DDL / 前端表单 / CRUD 写白名单四处都有，但全仓无读点，
实际周期硬编码在 `main.py` 的 `HeartbeatProber(interval=30)`，改多少都还是 30s。
现在改为**每轮从 DB 现读各网关的 `heartbeat_interval`**（见 `_plan_interval`），
按「最近到期的那个网关」决定本轮睡多久，从而在不引入 per-gateway 定时器复杂度的前提下
让该字段真正生效；`interval` 构造参数退化为「DB 读不到值时的兜底周期」。
"""
import socket
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from db.session import SessionLocal
from db.models import Gateway
from alerting import alert_if_changed

FAIL_THRESHOLD = 3        # 连续失败达到此值才判离线
DEFAULT_INTERVAL = 30     # 兜底探测周期(秒)；正常路径以 gateway.heartbeat_interval 为准

# 探测周期上下界（防止把周期配成 0 导致 CPU 打满，或配成几小时导致"假死"无感知）
MIN_INTERVAL = 5
MAX_INTERVAL = 3600


def _local_ip(dst_ip: str) -> str:
    """取本机到 dst_ip 的出口 IP（仅用于 OPTIONS 的 Via/Contact；SBC 回包按实际源端口）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dst_ip, 9))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def _build_options(ip: str, port: int, user: str, local_ip: str) -> bytes:
    bid = "z9hG4bK" + uuid.uuid4().hex[:12]
    cid = uuid.uuid4().hex[:12] + "@local"
    tag = uuid.uuid4().hex[:8]
    lines = [
        "OPTIONS sip:%s:%d SIP/2.0" % (ip, port),
        "Via: SIP/2.0/UDP %s:5060;branch=%s;rport" % (local_ip, bid),
        "Max-Forwards: 70",
        "From: <sip:%s@%s:%d>;tag=%s" % (user, ip, port, tag),
        "To: <sip:%s@%s:%d>" % (user, ip, port),
        "Call-ID: %s" % cid,
        "CSeq: 1 OPTIONS",
        "Contact: <sip:%s@%s:5060>" % (user, local_ip),
        "Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


def _probe_udp_options(ip: str, port: int, user: str, to: int) -> bool:
    """发一次 SIP OPTIONS（UDP），收到任意 SIP 响应即视为链路 + SBC 可达。"""
    local_ip = _local_ip(ip)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(max(1, int(to)))
    try:
        s.sendto(_build_options(ip, port, user, local_ip), (ip, int(port)))
        data, _ = s.recvfrom(4096)
        first = data.decode(errors="ignore").split("\r\n", 1)[0]
        return first.startswith("SIP/2.0")
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


def _probe_once(db) -> None:
    gws = db.scalars(select(Gateway).where(Gateway.status == 1, Gateway.heartbeat_enabled == 1)).all()
    if not gws:
        return
    # 按 (ip, port) 分组：同一 SBC 只探一次，组内共享结果
    groups = {}
    for g in gws:
        groups.setdefault((g.ip, g.port), []).append(g)
    now = datetime.now(timezone.utc)
    for (ip, port), members in groups.items():
        rep = members[0]
        user = rep.username or rep.name
        alive = _probe_udp_options(ip, port, user, rep.heartbeat_timeout or 3)
        for g in members:
            g.last_heartbeat_time = now
            if alive:
                if g.heartbeat_status != 1 or (g.heartbeat_fail_count or 0) != 0:
                    print("[HB] gateway %s (%s:%d) UP" % (g.name, ip, port))
                    alert_if_changed("gateway_up", "gateway", g.id, {
                        "name": g.name, "ip": ip, "port": port,
                    }, level="info")
                g.heartbeat_status = 1
                g.heartbeat_fail_count = 0
            else:
                g.heartbeat_fail_count = (g.heartbeat_fail_count or 0) + 1
                if g.heartbeat_fail_count >= FAIL_THRESHOLD:
                    if g.heartbeat_status != 0:
                        print("[HB] gateway %s (%s:%d) DOWN after %d fails"
                              % (g.name, ip, port, g.heartbeat_fail_count))
                        alert_if_changed("gateway_down", "gateway", g.id, {
                            "name": g.name, "ip": ip, "port": port,
                            "fail_count": g.heartbeat_fail_count,
                        })
                    g.heartbeat_status = 0
                elif g.heartbeat_fail_count == 1:
                    print("[HB] gateway %s (%s:%d) probe fail #%d (debouncing)"
                          % (g.name, ip, port, g.heartbeat_fail_count))
    db.commit()


def _clamp(v) -> int:
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    if iv <= 0:
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, iv))


def _plan_interval(db) -> int:
    """本轮该睡多久 = 所有启用探测的网关里**最小的** `heartbeat_interval`。

    为什么取 min 而不是把每个网关拆成各自的定时器：现有实现是「一轮探测所有网关」
    （且按 (ip,port) 分组共享结果），拆定时器要重做分组与调度，收益不成比例。
    取 min 的意义是：粒度最细的那个网关的周期被严格遵守，其余网关只会被
    **更频繁**地探测（偏保守、不会漏探测）；同时该字段从死配置变成真正生效。

    DB 读不到（异常/无启用网关）时回落到 DEFAULT_INTERVAL。
    """
    vals = db.scalars(
        select(Gateway.heartbeat_interval)
        .where(Gateway.status == 1, Gateway.heartbeat_enabled == 1)
    ).all()
    if not vals:
        return DEFAULT_INTERVAL
    return min(_clamp(v) for v in vals)


class HeartbeatProber:
    def __init__(self, interval: int = DEFAULT_INTERVAL):
        # interval 退化为「DB 读不到值时的兜底周期」，正常路径走 _plan_interval
        self.interval = _clamp(interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            sleep_s = self.interval
            try:
                db = SessionLocal()
                try:
                    _probe_once(db)
                    # 探测完立刻按最新配置算下一轮周期（网关增删/改间隔无需重启进程）
                    sleep_s = _plan_interval(db)
                finally:
                    db.close()
            except Exception as e:
                print("[HB] probe error:", e)
            self._stop.wait(sleep_s)
