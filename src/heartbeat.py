"""落地网关心跳探测（T-204）。

探测方式：UDP SIP OPTIONS（自发包，带网关 name/user），而非 TCP connect。
原因：我们的落地网关是 register=false 的 UDP SIP trunk（对端只听 UDP，TCP 永远超时），
原 TCP 探测会把全部网关误判离线、选路被全杀（历史有人因此把 heartbeat_enabled 全置 0 止血）。

分组探测（用户确认）：按 (ip, port) 分组，每组只发一次 OPTIONS；
同组所有网关共享一个探测结果——"探测某 IP 正常 → 该 IP 上所有 gateway 都视为正常"。
这是当前最贴近的粒度：对端是同一台 SBC，OPTIONS 只能探到「链路可达 + SBC 认这个 user」，
区分不了单 trunk 是否真能呼出，故以 IP 为准。

防抖：连续 FAIL_THRESHOLD 次失败才置离线；任一次成功立即恢复（纳回）。
仅打日志（不接外部告警）。
"""
import socket
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from db.session import SessionLocal
from db.models import Gateway

FAIL_THRESHOLD = 3        # 连续失败达到此值才判离线
DEFAULT_INTERVAL = 30     # 探测周期(秒)，用户确认 30s


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
                g.heartbeat_status = 1
                g.heartbeat_fail_count = 0
            else:
                g.heartbeat_fail_count = (g.heartbeat_fail_count or 0) + 1
                if g.heartbeat_fail_count >= FAIL_THRESHOLD:
                    if g.heartbeat_status != 0:
                        print("[HB] gateway %s (%s:%d) DOWN after %d fails"
                              % (g.name, ip, port, g.heartbeat_fail_count))
                    g.heartbeat_status = 0
                elif g.heartbeat_fail_count == 1:
                    print("[HB] gateway %s (%s:%d) probe fail #%d (debouncing)"
                          % (g.name, ip, port, g.heartbeat_fail_count))
    db.commit()


class HeartbeatProber:
    def __init__(self, interval: int = DEFAULT_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

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
                print("[HB] probe error:", e)
            self._stop.wait(self.interval)
