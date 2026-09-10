"""注册型落地网关的「当前注册状态」回写（2026-09-10）。

数据源
------
Event Socket 的 `CUSTOM sofia::gateway_state` 事件，由 FS `sofia_reg.c` 的
`sofia_reg_fire_custom_gateway_state_event()` 抛出。实测报文（FS 1.11.2）：

    Event-Name: CUSTOM
    Event-Subclass: sofia::gateway_state
    Gateway: test-register-gw        <- 网关名（注意不是 Gateway-Name）
    State: REGED                     <- FS 内部态（注意不是 Gateway-State）
    Ping-Status: UP
    Status: 200 / Phrase: OK         <- 仅 REGISTER 这一跳带 SIP 响应码
    Register-Network-IP/Port: ...

一次 killgw+rescan 的完整序列（本机实测）：
    DOWN(37.588) -> TRYING(38.574) -> REGISTER 200 OK(38.574) -> REGED(39.581)
即 rescan 后**亚秒级**就重注册，不等 retry-seconds。

为什么只看基线 messaging：写成简单 map + 幂等 upsert，轮询/事件两条路都收敛到
同一个 `apply_state()`，避免状态被两个来源互相覆盖。

⚠️ 只对注册型网关(auth_type=1)落库：点对点网关不向对端注册，FS 不会为它产生注册
事件（state 恒 NOREG），回写会把状态钉死在「未注册」，故按 auth_type=1 过滤。
"""
import logging
import re
from datetime import datetime

from sqlalchemy import select

log = logging.getLogger("gw_state")

ST_UNREG, ST_REGED, ST_TRYING, ST_FAILED = 0, 1, 2, 3

# FS gateway state -> 本地状态码。未知态一律 None（不落库，避免乱写）。
STATE_MAP = {
    "REGED": ST_REGED,
    "TRYING": ST_TRYING,
    "REGISTER": ST_TRYING,
    "PROGRESS": ST_TRYING,
    "NOREG": ST_UNREG,
    "UNREGED": ST_UNREG,
    "UNREGISTER": ST_UNREG,
    "EXPIRED": ST_UNREG,
    "DOWN": ST_UNREG,
    "FAIL_WAIT": ST_FAILED,
    "FAILED": ST_FAILED,
    "FAIL": ST_FAILED,
    "TIMEOUT": ST_FAILED,
    "REJECT": ST_FAILED,
}

STATE_TEXT = {ST_UNREG: "未注册", ST_REGED: "已注册", ST_TRYING: "注册中", ST_FAILED: "注册失败"}


def map_state(fs_state):
    """FS 态 -> 本地状态码；未知态返回 None（调用方跳过，不落库）。"""
    return STATE_MAP.get((fs_state or "").strip().upper())


def apply_state(db, name, fs_state, source="event"):
    """把某个网关的注册状态写回 DB（幂等，值未变则不写）。

    :return: True 表示状态发生了变更
    """
    st = map_state(fs_state)
    if st is None or not name:
        return False
    try:
        from db.models import Gateway
    except ImportError:  # pragma: no cover
        from src.db.models import Gateway
    row = db.scalar(select(Gateway).where(
        Gateway.name == name, Gateway.auth_type == 1))
    if row is None:
        return False
    if row.register_status == st:
        return False
    old = row.register_status
    row.register_status = st
    row.register_status_at = datetime.utcnow()
    db.commit()
    log.info("[gw-state] %s: %s -> %s (%s, src=%s)",
             name, STATE_TEXT.get(old, old), STATE_TEXT.get(st, st), fs_state, source)
    return True


def handle_event(event):
    """ESL 事件回调：处理 `CUSTOM sofia::gateway_state`。"""
    if (event.getHeader("Event-Subclass") or "") != "sofia::gateway_state":
        return
    name = event.getHeader("Gateway")
    state = event.getHeader("State")
    if not name or not state:
        return
    try:
        from db.session import SessionLocal
    except ImportError:  # pragma: no cover
        from src.db.session import SessionLocal
    db = SessionLocal()
    try:
        apply_state(db, name, state)
    except Exception as e:
        log.warning("[gw-state] event update failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def reconcile_once():
    """启动时用 `sofia xmlstatus gateway` 做一次全量对齐。

    事件的局限：只在状态**跃变**时投递。网关进程重启时，FS 里早已 REGED 的网关
    不会再有事件过来，DB 里的 register_status 会一直停在「未注册」。故进程起来后
    主动拉一次快照补齐初值。

    :return: 更新条数；-1 表示这次没取到有效快照（FS 可能尚未就绪）
    """
    try:
        from fs_esl_cmd import fs_api
    except ImportError:  # pragma: no cover
        from src.fs_esl_cmd import fs_api
    try:
        from db.session import SessionLocal
    except ImportError:  # pragma: no cover
        from src.db.session import SessionLocal

    txt = fs_api("sofia xmlstatus gateway") or ""
    pairs = re.findall(r"<name>(.*?)</name>.*?<state>(.*?)</state>", txt, re.S)
    if not pairs:
        return -1
    db = SessionLocal()
    n = 0
    try:
        for raw_name, raw_state in pairs:
            name = (raw_name or "").strip()
            # UNREGED/NOREG 之类都要落到 0，故不能用 truthy 判断
            if apply_state(db, name, (raw_state or "").strip(), source="startup"):
                n += 1
    except Exception as e:
        log.warning("[gw-state] reconcile failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    if n:
        log.info("[gw-state] startup reconcile updated %d gateway(s)", n)
    return n


def start_gateway_state_sync(delay=5.0, attempts=6, gap=5.0):
    """后台做一次初值对齐（等 FS/ESL 就绪后重试若干次）。"""
    import threading
    import time

    def _run():
        time.sleep(delay)
        for i in range(attempts):
            try:
                if reconcile_once() >= 0:
                    return
            except Exception as e:
                log.warning("[gw-state] startup reconcile attempt %d failed: %s", i + 1, e)
            time.sleep(gap)

    threading.Thread(target=_run, daemon=True, name="gw-state-sync").start()
