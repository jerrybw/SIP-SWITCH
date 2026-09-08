"""启动期落地网关全量下发（provision）。

背景
----
落地网关 XML 平时由管理端保存网关时触发 fs_provision.provision() 生成，
写入与 FS 共享的卷（/fs-profiles）。一旦该卷被清空（全新部署
`docker compose down -v`），FS 侧就没有任何 <gateway> 定义，出局全断，
且不会自愈 —— 必须人工去管理端改一次网关才会重新下发。

本模块在网关启动时按 DB 现状重建全部 XML，并在 FS/ESL 就绪后补一次 rescan，
使「全新部署」也能一键可用。全部幂等，可安全重复执行。

注意：只写不删。卷内可能存在镜像自带的 example.xml 等文件，
不做清理以免误删非网关 XML。
"""
import logging
import os
import threading
import time

from sqlalchemy import select

from db.session import SessionLocal
from db.models import Gateway

try:  # 以 /app/src 为根运行（python src/main.py）
    from fs_provision import wxml, FSD, PROF
    from fs_esl_cmd import fs_api
except ImportError:  # 以 /app 为根运行（src 作为包被导入）
    from src.fs_provision import wxml, FSD, PROF
    from src.fs_esl_cmd import fs_api

log = logging.getLogger("gw_bootstrap")

# FS 启动较慢（mod_sofia 加载 + ESL 监听），rescan 需要等待其就绪
RESCAN_MAX_WAIT = 180
RESCAN_INTERVAL = 5


def _write_all():
    """按 DB 现状重建全部落地网关 XML（纯本地写文件，不依赖 FS 就绪）。"""
    n = 0
    db = SessionLocal()
    try:
        try:
            os.makedirs(FSD, exist_ok=True)
        except Exception as e:
            log.warning("makedirs %s failed: %s", FSD, e)
            return 0
        gws = db.scalars(select(Gateway)).all()
        for gw in gws:
            try:
                wxml(gw)
                n += 1
            except Exception as e:
                log.warning("bootstrap provision %s failed: %s",
                            getattr(gw, "name", "?"), e)
    except Exception as e:
        log.warning("bootstrap load gateways failed: %s", e)
    finally:
        db.close()
    return n


def _wait_rescan():
    """FS/ESL 就绪后补一次 rescan（ESL 起来前会失败，轮询等待）。"""
    deadline = time.time() + RESCAN_MAX_WAIT
    while time.time() < deadline:
        try:
            out = fs_api("sofia profile %s rescan" % PROF)
            if out and out.strip():
                log.info("bootstrap rescan ok: %s", out.strip()[:120])
                return True
        except Exception as e:
            log.debug("bootstrap rescan not ready: %s", e)
        time.sleep(RESCAN_INTERVAL)
    log.warning("bootstrap rescan timeout after %ss (FS/ESL 未就绪?)", RESCAN_MAX_WAIT)
    return False


def _loop():
    try:
        n = _write_all()
        log.info("bootstrap: wrote %d gateway xml into %s", n, FSD)
        if n == 0:
            return
        _wait_rescan()
    except Exception as e:
        log.warning("bootstrap loop error: %s", e)


def start_gateway_provision():
    t = threading.Thread(target=_loop, name="gw-bootstrap", daemon=True)
    t.start()
    log.info("gateway bootstrap provision started")
    return t
