import logging
import os
import glob
import re
from fastapi import Request, Response
from sqlalchemy import select
from db.models import SipPhone, AccessPoint
log = logging.getLogger("directory_xml")
FS_DIR = "/usr/local/freeswitch/etc/freeswitch/directory"
LT = chr(60)
GT = chr(62)
Q = chr(34)
BS = chr(92)


def _static_users():
    out = {}
    if not os.path.isdir(FS_DIR):
        return out
    pu = LT + "user" + BS + "s+id=" + Q + "([^" + Q + "]+)" + Q
    pp = (LT + "param" + BS + "s+name=" + Q + "password" + Q
          + BS + "s+value=" + Q + "([^" + Q + "]*)" + Q)
    for fp in glob.glob(os.path.join(FS_DIR, "**", "*.xml"), recursive=True):
        try:
            txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        m = re.search(pu, txt)
        if not m:
            continue
        pm = re.search(pp, txt)
        out[m.group(1)] = pm.group(1) if pm else ""
    return out


def _esc(s):
    s = str(s if s is not None else "")
    s = s.replace("&", "&amp;")
    s = s.replace(LT, "&lt;")
    s = s.replace(GT, "&gt;")
    s = s.replace(Q, "&quot;")
    return s


def _user_xml(user, password):
    inner = (LT + "params" + GT
             + LT + "param name=" + Q + "password" + Q
             + " value=" + Q + _esc(password) + Q + chr(47) + GT
             + LT + "param na" + "me=" + Q + "context" + Q
             + " value=" + Q + "default" + Q + chr(47) + GT
             + LT + chr(47) + "params" + GT)
    return (LT + "user id=" + Q + _esc(user) + Q + GT + inner
            + LT + chr(47) + "user" + GT)
def _doc(domain, users_xml):
    # 2026-09-03 修正：dial-string 只有第一组 {} 是「通道变量前缀」，其后必须是裸的
    # ${sofia_contact(...)} 展开结果作为真正的 origination URL。
    # 旧写法把 dial string 也包进了 {}，被 switch_ivr_originate.c 的
    # switch_event_create_brackets 全部吃掉 → data 为空 →
    # "No origination URL specified!"（内部分机互拨必失败）。
    # 与 vanilla directory/default.xml 保持一致（去掉未启用的 verto_contact）。
    ds = ("{^^:sip_invite_domain=${dialed_domain}"
          ":presence_id=${dialed_user}@${dialed_domain}}"
          "${sofia_contact(*/${dialed_user}@${dialed_domain})}")
    body = (LT + "document type=" + Q + "freeswitch/xml" + Q + GT
            + LT + "section name=" + Q + "directory" + Q + GT
            + LT + "domain name=" + Q + _esc(domain) + Q + GT
            + LT + "params" + GT
            + LT + "param name=" + Q + "dial-string" + Q
            + " value=" + Q + ds + Q + chr(47) + GT
            + LT + "/params" + GT
            + LT + "users" + GT + users_xml + LT + "/users" + GT
            + LT + "/domain" + GT + LT + "/section" + GT
            + LT + "/document" + GT)
    return Response(content=body, media_type="text/xml")


_sip_call_ctx = {}


def fs_directory(params, db):
    print("DIRQ", dict(params), flush=True)
    qp = params
    ru = (qp.get("user") or qp.get("sip_auth_username")
          or qp.get("sip_from_user"))
    domain = qp.get("domain") or qp.get("key_value") or "LIGHTHOUSE_PRIVATE_IP_REDACTED"
    # 2026-09-03：只对「管理启用」的话机出目录（enabled=1）；停用话机目录不可见 →
    # FS 拒绝其注册/呼入。注册型接入点同样按 status=1 过滤。
    rows = db.scalars(select(SipPhone).where(SipPhone.enabled == 1)).all()
    merged = {r.phone_number: r.password for r in rows}
    for a in db.scalars(select(AccessPoint).where(AccessPoint.status == 1)).all():
        if a.auth_mode == 1 and a.reg_username:
            merged[a.reg_username] = a.reg_password or ""
    if ru:
        pw = merged.get(ru)
        if pw is None:
            return _doc(domain, "")
        return _doc(domain, _user_xml(ru, pw))
    ux = "".join(_user_xml(u, p) for u, p in merged.items())
    return _doc(domain, ux)
