"""落地网关 provision（机制 A：不落盘）。

落地网关定义现由网关应用经 mod_xml_curl 的 configuration 段动态下发
（src/fs_sofia_config.build_sofia_conf），FS 侧零落盘。管理端保存/删除网关后，
只需让 FS 重新拉取 sofia.conf 即可，不再写任何 XML 文件——故本模块只保留 rescan 触发。

⚠️ 关键前提（2026-09-09 实测，见 PITFALLS #31）：
FS 的 `sofia profile <prof> rescan` **不会重建已存在的 gateway** —— 只有该 gateway
在 FS 里不存在时才会按最新 XML 创建；已存在的对象会原样保留旧参数（proxy/realm/port…）。
因此「改完网关点保存，FS 里还是旧值」的根因不是"没触发 rescan"，而是 rescan 对存量
gateway 无效。正确姿势：先 `killgw <name>` 销毁旧对象，再 rescan 让 FS 重建。
"""
import logging
import threading

try:
    from fs_esl_cmd import fs_api
except ImportError:
    from src.fs_esl_cmd import fs_api

log = logging.getLogger("fs_provision")

PROF = "external"


def rescan(prof=PROF, gw_name=None):
    """异步重扫 sofia profile：触发 FS 经 xml_curl 重新拉取 sofia.conf（含最新 DB 网关）。

    传 gw_name 时会先 `killgw <gw_name>`：强制 FS 丢弃该网关的旧对象，
    随后的 rescan 才会用新配置重建它（改 IP/端口/账号等场景必须如此，否则改完不生效）。

    :param prof: sofia profile 名
    :param gw_name: 需要强制重建的网关名；None 表示只做普通 rescan（新增/删除场景）
    """
    def _run():
        try:
            if gw_name:
                try:
                    r = fs_api("sofia profile %s killgw %s" % (prof, gw_name))
                    log.info("killgw %s: %s", gw_name, (r or "").strip()[:120])
                except Exception as e:
                    # 网关本就不存在（新增场景）时 killgw 会失败，忽略即可
                    log.debug("killgw %s skipped: %s", gw_name, e)
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
    """网关变更后触发 FS 重新拉取 sofia.conf（不再写盘）。

    注意必须先 killgw 同名旧对象，否则 FS 会保留旧 proxy/realm（rescan 对存量网关无效）。
    """
    try:
        o = rescan(gw_name=getattr(gw, "name", None))
        return {"ok": True, "rescan": o.strip(),
                "note": "gateway now served via xml_curl, no disk write"}
    except Exception as e:
        log.error("provision fail: %s", e)
        return {"ok": False, "error": str(e)}


def remove_xml(n):
    """网关删除后触发 FS 重新拉取 sofia.conf（网关将从 DB 消失）。"""
    try:
        rescan(gw_name=n)
        return True
    except Exception:
        return False
