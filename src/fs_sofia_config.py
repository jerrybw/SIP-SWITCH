"""动态生成 sofia.conf（落地网关不落盘，机制 A）。

FS 通过 mod_xml_curl 的 `configuration` 绑定，在加载 sofia 配置时向本应用请求
`section=configuration&key_value=sofia.conf`。本模块按 gateway 表（单一事实来源）生成
含 <gateways> 的完整 sofia.conf，FS 侧零落盘。新增 FS 节点只需把 xml_curl 指向同一
网关端点，底层无需改动（多机拆分零成本）。

#64 多节点分片（D11）：下发按 NODE_UUID 过滤 ——
注册型网关只下发给其归属节点，点对点网关全量下发。

其余配置（acl.conf / event_socket.conf / modules.conf 等）本端点统一回空文档，
FS 按 mod_xml_curl 标准回退行为使用磁盘文件，保持原行为不变。
"""
import logging
import os
import re

from sqlalchemy import select
from db.models import Gateway, GatewayNode
from core.config import NODE_UUID

log = logging.getLogger("fs_sofia_config")

# FS 配置只读挂载（compose 把 ./deploy/fs-config 挂到此处），仅读取 profile 骨架，
# 网关定义由 DB 动态注入，避免两份真相源。
FS_CONFIG_RO = os.environ.get("FS_CONFIG_RO", "/fs-config-ro")
EXT_SIP_IP = os.environ.get("EXT_SIP_IP", "")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        log.warning("read %s failed: %s", path, e)
        return ""


def _gateway_xml(gw):
    """单条落地网关 XML（与旧 fs_provision.bgw 完全一致，仅去 <include> 包裹）。"""
    p = "{}:{}".format(gw.ip, gw.port or 5060)
    st = int(getattr(gw, "status", 1) or 1)
    reg = "false" if st != 1 else ("true" if int(getattr(gw, "auth_type", 0) or 0) == 1 else "false")
    u = getattr(gw, "username", None) or gw.name
    w = getattr(gw, "password", None) or ""
    return (
        '    <gateway name="%s">\n' % gw.name
        + '      <param name="proxy" value="%s"/>\n' % p
        + '      <param name="realm" value="%s"/>\n' % p
        + '      <param name="register" value="%s"/>\n' % reg
        + '      <param name="username" value="%s"/>\n' % u
        + '      <param name="password" value="%s"/>\n' % w
        + '      <param name="caller-id-in-from" value="true"/>\n'
        + '    </gateway>'
    )


def _gateways_block(db):
    """#64 按节点下发（D11）：
      - 注册型(auth_type=1)：只下发归属本 NODE_UUID 的网关；
      - 点对点(auth_type=0)：全量下发到所有节点。
    本网关实例用自身 NODE_UUID 过滤，故每个 FS 节点拉到的配置各不相同。
    """
    gws = db.scalars(select(Gateway)).all()
    if not gws:
        return "<gateways></gateways>"
    if NODE_UUID:
        mine = set(db.scalars(
            select(GatewayNode.gateway_id).where(GatewayNode.node_uuid == NODE_UUID)
        ).all())
    else:
        mine = set()
    items = []
    for g in gws:
        if int(getattr(g, "auth_type", 0) or 0) == 1 and g.id not in mine:
            continue
        items.append(_gateway_xml(g))
    if not items:
        return "<gateways></gateways>"
    return "<gateways>\n" + "\n".join(items) + "\n</gateways>"


def _render_profile(path, db, with_gateways=False):
    """读取 profile 骨架，按需注入 DB 网关，渲染 EXT_SIP_IP 占位符。"""
    txt = _read(path)
    if not txt:
        return ""
    if with_gateways:
        txt = re.sub(r"<gateways>.*?</gateways>",
                     lambda m: _gateways_block(db), txt, flags=re.DOTALL)
    txt = txt.replace("__EXT_SIP_IP__", EXT_SIP_IP)
    return txt


def build_sofia_conf(db):
    internal = _render_profile(
        os.path.join(FS_CONFIG_RO, "sip_profiles", "internal.xml"), db, with_gateways=False)
    external = _render_profile(
        os.path.join(FS_CONFIG_RO, "sip_profiles", "external.xml"), db, with_gateways=True)
    return (
        '<document type="freeswitch/xml">\n'
        '  <section name="configuration">\n'
        '    <configuration name="sofia.conf" description="Sofia SIP Stack">\n'
        '      <global_settings>\n'
        '        <param name="log-level" value="0"/>\n'
        '        <param name="auto-restart" value="false"/>\n'
        '        <param name="debug-presence" value="0"/>\n'
        '      </global_settings>\n'
        '      <profiles>\n'
        + internal + "\n"
        + external + "\n"
        '      </profiles>\n'
        '    </configuration>\n'
        '  </section>\n'
        '</document>\n'
    )


def build_config_response(key_value, db):
    """xml_curl configuration 段总入口。

    - sofia.conf：动态生成（含 DB 落地网关），FS 零落盘。
    - 其余配置：回空文档，FS 回退磁盘文件（acl/event_socket/modules 等保持原行为）。
    """
    if key_value == "sofia.conf":
        return build_sofia_conf(db)
    return '<document type="freeswitch/xml"></document>\n'
