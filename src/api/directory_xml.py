import hashlib
import logging
import os
import glob
import re
from fastapi import Request, Response
from sqlalchemy import select
from db.models import SipPhone, AccessPoint
from core.config import settings
log = logging.getLogger("directory_xml")
FS_DIR = "/usr/local/freeswitch/etc/freeswitch/directory"


def _static_users():
    out = {}
    if not os.path.isdir(FS_DIR):
        return out
    pu = r'<user\s+id="([^"]+)"'
    pp = r'<param\s+name="password"\s+value="([^"]*)"'
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
    s = s.replace("<", "&lt;")
    s = s.replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    return s


def _user_xml(user, password, domain=""):
    """单用户目录片。

    安全收敛（与 /fs/* Basic 认证配套）：
    - domain 非空时下发 `a1-hash`（md5("user:domain:password")）而非明文密码 ——
      FS 摘要认证直接用 a1-hash 比对（internal profile challenge-realm=auto_from，
      realm == 目录请求携带的 domain == 此处参与哈希的 domain，口径自洽），
      xml_curl 响应体/日志里从此不再出现明文 SIP 密码。
    - domain 为空（default_sip_domain 未配置且 FS 未携带）时退回明文密码参数：
      此时无可靠 realm 口径，宁可保守兼容注册流程，也不能让全部话机注册挂掉。
    """
    inner_params = ['<param name="context" value="default"/>']
    if domain:
        a1 = hashlib.md5(f"{user}:{domain}:{password}".encode()).hexdigest()
        inner_params.insert(0, '<param name="a1-hash" value="%s"/>' % a1)
    else:
        inner_params.insert(0, '<param name="password" value="%s"/>' % _esc(password))
    inner = "<params>" + "".join(inner_params) + "</params>"
    return '<user id="%s">' % _esc(user) + inner + "</user>"


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
    body = (
        '<document type="freeswitch/xml">'
        '<section name="directory">'
        '<domain name="%s">' % _esc(domain)
        + '<params>'
        '<param name="dial-string" value="%s"/>' % ds
        + "</params>"
        + "<users>%s</users>" % users_xml
        + "</domain></section></document>"
    )
    return Response(content=body, media_type="text/xml")


_sip_call_ctx = {}


_default_domain_warned = False


def _default_domain():
    """FS 未携带 domain/key_value 时的兜底域。

    取值：config_settings.yaml 的 default_sip_domain。
    未配置则返回空串（并只在首次告警一次），绝不回退任何硬编码地址——
    历史版本这里写死过生产私网 IP，导致该地址污染了 FS 的 profile 别名。
    """
    global _default_domain_warned
    v = (settings.get("default_sip_domain") or "").strip()
    if not v and not _default_domain_warned:
        _default_domain_warned = True
        log.warning(
            "[directory] default_sip_domain 未配置，且 FS 请求未携带 domain，目录域将为空"
        )
    return v


def _lookup_user(db, ru):
    """按用户名精确定位目录条目：先查 sip_phone（enabled），再查注册型接入点（status=1）。

    返回密码或 None。替代旧实现的全表加载合并 dict —— 全表扫既浪费（每次目录请求
    都拉全部话机+接入点），也是明文泄露面的一部分。
    """
    ph = db.scalar(select(SipPhone).where(
        SipPhone.phone_number == ru, SipPhone.enabled == 1))
    if ph is not None:
        return ph.password
    aps = db.scalars(select(AccessPoint).where(
        AccessPoint.reg_username == ru, AccessPoint.status == 1)).all()
    for a in aps:
        if a.auth_mode == 1:
            return a.reg_password or ""
    return None


def fs_directory(params, db):
    log.debug("DIRQ %s", dict(params))
    qp = params
    ru = (qp.get("user") or qp.get("sip_auth_username")
          or qp.get("sip_from_user"))
    # 兜底域：FS 的目录请求多数带 domain/key_value，但 purpose=gateways 一类请求
    # 两者皆空。此处不再硬编码任何环境 IP，改为：请求参数 -> 配置 default_sip_domain。
    domain = qp.get("domain") or qp.get("key_value") or _default_domain()
    # 安全收敛：不再支持「无 user 的全量目录导出」。FS 真实认证/定位流程（REGISTER/
    # INVITE 挑战、sofia_contact、user_exists）全部携带 user= 参数；无 user 的全量
    # 拉取只服务于人工调试，却会把**全部话机与接入点的凭据**一次性回显给调用方。
    # 此处回空 <users/>（保留 domain 级 dial-string 参数），需要排查时用带 user 的
    # 精确查询，或直接查 DB。
    if ru:
        pw = _lookup_user(db, ru)
        if pw is None:
            return _doc(domain, "")
        return _doc(domain, _user_xml(ru, pw, domain))
    return _doc(domain, "")
