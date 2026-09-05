"""经 ESL 向 FreeSWITCH 下发命令 —— 替代容器内不可用的 fs_cli。

背景：网关容器镜像里不会安装 FreeSWITCH 客户端工具（fs_cli），
因此落地网关重扫（rescan）与注册状态查询（show registrations）
改由 ESL 短连接下发，语义等同 `fs_cli -x <cmd>`。

设计要点
- 短连接：每次命令新建 ESL 连接、执行后立即断开，不占用长连接、不影响事件监听。
- 降级链：优先 ESL；ESL 不可用（无 python-esl 绑定 / 连不上）时回退本机 fs_cli。
  这样现有「网关与 FS 同机、装有 fs_cli」的部署行为保持不变。
- 返回语义：
  - esl_api()  成功 -> str（可能为空串）；不可用/失败 -> None
  - fs_api()   始终返回 str；两条路都不通时返回空串（调用方按空串处理即可）
"""
import logging
import os
import subprocess

from core.config import settings

log = logging.getLogger("fs_esl_cmd")

FSC = "/usr/local/freeswitch/bin/fs_cli"


def esl_api(cmd, timeout=10):
    """执行一条 FS api 命令。

    返回：成功 -> 输出文本（可能为空串）；ESL 不可用或执行失败 -> None
    """
    cfg = settings.get("esl", {}) or {}
    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 8021)
    pw = str(cfg.get("password") or "")

    con = None
    try:
        from ESL import ESLconnection
    except Exception as e:  # 容器镜像内未安装 python-esl 绑定
        log.warning("ESL binding unavailable: %s", e)
        return None
    try:
        con = ESLconnection(host, port, pw)
        if con is None or not con.connected():
            log.warning("ESL connect failed: %s:%s", host, port)
            return None
        ev = con.api(cmd)
        if ev is None:
            return None
        return ev.getBody() or ""
    except Exception as e:
        log.warning("esl_api(%s) failed: %s", cmd, e)
        return None
    finally:
        if con is not None:
            try:
                con.disconnect()
            except Exception:
                pass


def _fs_cli(cmd):
    """兜底：本机装有 FreeSWITCH 时走 fs_cli；容器镜像中没有则返回 None。"""
    if not os.path.exists(FSC):
        return None
    try:
        cfg = settings.get("esl", {}) or {}
        c = [FSC]
        if cfg.get("host"):
            c += ["-H", str(cfg["host"])]
        if cfg.get("port"):
            c += ["-P", str(cfg["port"])]
        if cfg.get("password"):
            c += ["-p", str(cfg["password"])]
        c += ["-x", cmd]
        r = subprocess.run(c, capture_output=True, text=True, timeout=30)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        log.warning("fs_cli fallback failed: %s", e)
        return None


def fs_api(cmd):
    """执行 FS 命令：优先 ESL，失败回退 fs_cli；都不可用返回空串。"""
    out = esl_api(cmd)
    if out is None:
        out = _fs_cli(cmd)
    return out or ""
