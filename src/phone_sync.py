import logging
import threading
import time
from datetime import datetime

from fs_esl_cmd import fs_api
from core.sys_setting import get_int_setting
from db.session import SessionLocal
from db.models import SipPhone, AccessPoint
from sqlalchemy import select

log = logging.getLogger("phone_sync")
_stop = False


def _fs_reg_text():
    """取 FS 注册列表文本（经 ESL；无 fs_cli 依赖）。"""
    return fs_api("show registrations")


def _setting(db, key, default):
    # 第2类热加载：经 sys_setting 实时读库，不缓存（见 docs/config-categories.md）
    return max(5, get_int_setting(key, default))

def _reconcile_once():
    db = SessionLocal()
    try:
        pi = _setting(db, "phone_sync_interval", 30)
        pai = _setting(db, "ap_sync_interval", 30)
        period = min(pi, pai)
        if period > 3600:
            period = 3600
        txt = _fs_reg_text()
        phones = db.scalars(select(SipPhone)).all()
        for r in phones:
            st = 1 if (r.phone_number and r.phone_number in txt) else 0
            if r.status != st:
                r.status = st
                r.updated_at = datetime.utcnow()
        aps = db.scalars(select(AccessPoint)).all()
        for a in aps:
            if int(a.auth_mode or 0) != 1:
                continue
            u = (a.reg_username or "").strip()
            st = 1 if (u and u in txt) else 0
            if a.reg_status != st:
                a.reg_status = st
                a.updated_at = datetime.utcnow()
        db.commit()
        return period
    except Exception as e:
        log.warning("reconcile failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 30
    finally:
        db.close()

def _loop():
    period = 30
    while not _stop:
        try:
            p = _reconcile_once()
            if isinstance(p, int) and p > 0:
                period = p
        except Exception as e:
            log.warning("loop err: %s", e)
        time.sleep(period)


def start_phone_sync():
    t = threading.Thread(target=_loop, name="phone-sync", daemon=True)
    t.start()
    log.info("phone_sync started")
