"""启动期落地网关自愈（机制 A：不落盘）。

落地网关定义现由网关应用经 mod_xml_curl 的 configuration 段动态下发，FS 侧零落盘。
全新部署 `docker compose down -v` 后 FS 启动即从网关应用拉取 sofia.conf，无需写任何 XML。
本模块仅负责：FS/ESL 就绪后补一次 rescan，确保首拉成功（FS 启动期网关应用可能尚未就绪）。
"""
import logging
import threading
import time

try:
    from fs_esl_cmd import fs_api
except ImportError:
    from src.fs_esl_cmd import fs_api

log = logging.getLogger("gw_bootstrap")

PROF = "external"
RESCAN_MAX_WAIT = 180
RESCAN_INTERVAL = 5


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
        _wait_rescan()
    except Exception as e:
        log.warning("bootstrap loop error: %s", e)


def start_gateway_provision():
    t = threading.Thread(target=_loop, name="gw-bootstrap", daemon=True)
    t.start()
    log.info("gateway bootstrap (no-disk) started")
    return t
