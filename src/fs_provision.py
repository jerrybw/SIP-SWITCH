import os, logging, threading
try:  # 以 /app/src 为根运行（python src/main.py，sys.path[0]=/app/src）
    from fs_esl_cmd import fs_api
except ImportError:  # 以 /app 为根运行（src 作为包被导入，sys.path 无 /app/src）
    from src.fs_esl_cmd import fs_api
log = logging.getLogger("fs_provision")

# 落地网关 XML 落盘目录（一期方案 A：网关与 FS 共享该卷）。
# 容器部署时可用环境变量覆盖，以适配不同 FS 镜像的配置路径。
FSD = os.environ.get(
    "FS_SIP_PROFILES_EXTERNAL",
    "/usr/local/freeswitch/etc/freeswitch/sip_profiles/external",
)
PROF = os.environ.get("FS_EXTERNAL_PROFILE", "external")


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
    """异步重扫 sofia profile。

    经 ESL 下发（容器镜像内无 fs_cli）；ESL 不可用时 fs_esl_cmd 自动回退本机 fs_cli。
    """
    def _run():
        out = fs_api("sofia profile %s rescan" % prof)
        if out.strip():
            log.info("rescan %s: %s", prof, out.strip())
        else:
            log.error("rescan %s: no output (ESL/fs_cli 均不可用?)", prof)
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
