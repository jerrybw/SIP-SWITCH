import os, sys, subprocess, logging, threading
from core.config import settings
log = logging.getLogger("fs_provision")
FSD = "/usr/local/freeswitch/etc/freeswitch/sip_profiles/external"
FSC = "/usr/local/freeswitch/bin/fs_cli"
PROF = "external"


def bgw(gw):
    p = "{}:{}".format(gw.ip, gw.port or 5060)
    # 2026-09-03：管理停用(status=0)的网关下发 register=false —— 停用即停止向对端注册
    # （update/PUT 触发 provision 时会重写本文件并 rescan，FS 随后注销）。
    st = int(getattr(gw, "status", 1) or 1)
    reg = "false" if st != 1 else ("true" if int(getattr(gw, "auth_type", 0) or 0) == 1 else "false")
    u = getattr(gw, "username", None) or gw.name
    w = getattr(gw, "password", None) or ""
    return (
        "<include>\n"
        + '  <gateway name="%s">\n' % gw.name
        + '    <param name="proxy" value="%s"/>\n' % p
        + '    <param name="realm" value="%s"/>\n' % p
        + '    <param name="register" value="%s"/>\n' % reg
        + '    <param name="username" value="%s"/>\n' % u
        + '    <param name="password" value="%s"/>\n' % w
        + '    <param name="caller-id-in-from" value="true"/>\n'
        + "  </gateway>\n"
        + "</include>\n"
    )


def gpath(n):
    return os.path.join(FSD, n + ".xml")


def wxml(gw):
    x = bgw(gw); p = gpath(gw.name); open(p, "w").write(x); log.info("wrote %s", p); return p
def rescan(prof=PROF):
    def _run():
        try:
            _esl = settings.get("esl", {})
            cmd = [FSC]
            if _esl.get("host"): cmd += ["-H", str(_esl["host"])]
            if _esl.get("port"): cmd += ["-P", str(_esl["port"])]
            if _esl.get("password"): cmd += ["-p", str(_esl["password"])]
            cmd += ["-x", "sofia profile %s rescan" % prof]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            o = (r.stdout or "")+(r.stderr or ""); log.info("rescan %s: %s", prof, o.strip())
        except Exception as e:
            log.error("rescan fail: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return "async"

def provision(gw):
    try:
        p = wxml(gw); o = rescan(); return {"ok": True, "path": p, "rescan": o.strip()}
    except Exception as e:
        log.error("provision fail: %s", e); return {"ok": False, "error": str(e)}
def remove_xml(n):
    p = gpath(n)
    if os.path.exists(p):
        os.remove(p); log.info("removed %s", p)
        try:
            rescan()
        except Exception:
            pass
        return True
    return False
