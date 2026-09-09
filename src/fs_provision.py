"""落地网关 provision（机制 A：不落盘）。

落地网关定义现由网关应用经 mod_xml_curl 的 configuration 段动态下发
（src/fs_sofia_config.build_sofia_conf），FS 侧零落盘。管理端保存/删除网关后，
只需让 FS 重新拉取 sofia.conf 即可，不再写任何 XML 文件——故本模块只保留 rescan 触发。
"""
import logging
import threading

try:
    from fs_esl_cmd import fs_api
except ImportError:
    from src.fs_esl_cmd import fs_api

log = logging.getLogger("fs_provision")

PROF = "external"


def rescan(prof=PROF):
    """异步重扫 sofia profile：触发 FS 经 xml_curl 重新拉取 sofia.conf（含最新 DB 网关）。"""
    def _run():
        try:
            out = fs_api("sofia profile %s rescan" % prof)
            if out and out.strip():
                log.info("rescan %s: %s", prof, out.strip())
            else:
                log.warning("rescan %s: no output (ESL/fs_cli 均不可用?)", prof)
        except Exception as e:
            log.warning("rescan %s failed: %s", prof, e)
    threading.Thread(target=_run, daemon=True).start()
    return "async"


def provision(gw):
    """网关变更后触发 FS 重新拉取 sofia.conf（不再写盘）。"""
    try:
        o = rescan()
        return {"ok": True, "rescan": o.strip(),
                "note": "gateway now served via xml_curl, no disk write"}
    except Exception as e:
        log.error("provision fail: %s", e)
        return {"ok": False, "error": str(e)}


def remove_xml(n):
    """网关删除后触发 FS 重新拉取 sofia.conf（网关将从 DB 消失）。"""
    try:
        rescan()
        return True
    except Exception:
        return False
