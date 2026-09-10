"""出局路由选择服务（T-202 / 前缀↔落地映射 + 最长匹配 + 接入点↔落地策略）。

从 DB 读取 prefix_route + gateway，按「最长前缀匹配 + priority 优先」选出落地网关，
交由 dialplan_xml.build_outbound_xml 生成 bridge 动作。路由决策全部在网关层完成，
由 mod_xml_curl 下发给 FS（架构铁律：禁止把路由硬编码进 FreeSWITCH dialplan）。

本模块与 rules/service.py 平行，保持 dialplan_xml.py 为纯 XML 生成（不依赖 DB）。
"""
from typing import Optional, Tuple, List

from sqlalchemy import select, or_
from core.config import NODE_UUID

from db.models import (
    PrefixRoute, Gateway, GatewayNode, AccessPoint, AccessGatewayPolicy,
)

# 接入点↔落地策略：allow=1 命中才放；deny=2 命中即拒；无策略则放行。
_POLICY_ALLOW = 1
_POLICY_DENY = 2


def resolve_access_point(db, caller: str, network_addr: str = None) -> Tuple[Optional[AccessPoint], int]:
    """解析入局呼叫归属的接入点（按注册用户名或联系地址多IP），返回 (接入点, 计费单位)。

    计费单位以接入点为准（每接入点可配，最小 1s）；未匹配到接入点时返回默认 60。
    仅用于 CDR 关联与计费（T-207），不影响路由裁决。
    """
    default_unit = 60
    if caller:
        ap = db.scalar(select(AccessPoint).where(AccessPoint.reg_username == caller,
                                                  AccessPoint.status == 1))
        if ap is not None:
            return ap, int(ap.bill_unit or default_unit)
    if network_addr:
        aps = db.scalars(select(AccessPoint).where(AccessPoint.status == 1)).all()
        for ap in aps:
            hosts = [h.strip() for h in (ap.register_host or "").split(",") if h.strip()]
            if network_addr in hosts:
                return ap, int(ap.bill_unit or default_unit)
    return None, default_unit



def resolve_access_points(db, network_addr: str) -> List[AccessPoint]:
    """trunk 中继: 按来源 IP 取全部匹配的 IP 型(auth_mode=0)接入点, 按 id 升序.

    与 resolve_access_point(单返回) 区别(方案A 2026-09-02):
    - 仅取 auth_mode==0(IP/点对点型), 排除注册型(auth_mode==1),
      防止注册型 AP 也填了 register_host 时被误取进 trunk 中继路由;
    - 返回全部匹配项(显式 order_by id 升序), 供调用方逐个试限制, 全不通才拒绝.
    """
    if not network_addr:
        return []
    aps = db.scalars(
        select(AccessPoint)
        .where(AccessPoint.status == 1, AccessPoint.auth_mode == 0)
        .order_by(AccessPoint.id)
    ).all()
    return [ap for ap in aps
            if network_addr in {h.strip() for h in (ap.register_host or "").split(",") if h.strip()}]

def _ap_gateway_allowed(db, ap_id: int, gw_id: int) -> bool:
    """接入点↔落地允许/禁止（G4）：allow 列表存在则须命中；deny 列表命中则拒绝。

    无策略行 → 放行。抽成纯函数便于单测与在候选池阶段复用。
    """
    rows = db.scalars(
        select(AccessGatewayPolicy).where(AccessGatewayPolicy.access_point_id == ap_id)
    ).all()
    if not rows:
        return True
    allows, denies = [], []
    for r in rows:
        (allows if r.policy == _POLICY_ALLOW else denies).append(r.gateway_id)
    if allows and gw_id not in allows:
        return False
    if gw_id in denies:
        return False
    return True


def select_outbound_gateway(db, callee: str, ap_id: int = None) -> Optional[List[Gateway]]:
    """返回命中的 Gateway ORM 对象，或 None（无匹配 / 全部被接入点策略禁止 → NO_ROUTE）。

    匹配条件：
      - prefix_route.status = 1 且 gateway.status = 1（启用）；
      - callee 以 prefix_route.prefix 开头。
    多候选排序（最具体优先）：
      - prefix 字符串越长越优先；
      - 同长取 priority 最大；
      - 仍同取 gateway.id 最小（稳定、可预期）。
    接入点策略过滤（G4，前移）：
      - 当 ap_id 给定时，在候选池阶段即剔除被接入点禁止的网关，
        使 N:M 场景下能「回退到同前缀下一个被允许的网关」；
        若过滤后候选池为空 → 返回 None（NO_ROUTE）。
    """
    callee = (callee or "").strip()
    if not callee:
        return None
    rows = db.execute(
        select(PrefixRoute, Gateway)
        .join(Gateway, Gateway.id == PrefixRoute.gateway_id)
        .where(PrefixRoute.status == 1, Gateway.status == 1, or_(Gateway.heartbeat_enabled == 0, Gateway.heartbeat_status == 1))
    ).all()
    candidates = [(pr, g) for pr, g in rows if callee.startswith(pr.prefix)]
    if not candidates:
        return None

    # #64 多节点分片（D11）：注册型网关只归本节点，点对点全量。
    # 选路范围因此被限制在节点内（failover/并发调度同样受限），属设计预期。
    if NODE_UUID:
        mine = set(db.scalars(
            select(GatewayNode.gateway_id).where(GatewayNode.node_uuid == NODE_UUID)
        ).all())
        candidates = [
            (pr, g) for pr, g in candidates
            if int(getattr(g, "auth_type", 0) or 0) == 0 or g.id in mine
        ]
        if not candidates:
            return None

    # G4：接入点↔落地策略前移为候选池过滤（N:M 回退）
    if ap_id is not None:
        candidates = [
            (pr, g) for pr, g in candidates if _ap_gateway_allowed(db, ap_id, g.id)
        ]
        if not candidates:
            return None

    # -len: 长前缀优先；-priority: 大优先；id 升序作为最终稳定 tiebreak
    candidates.sort(key=lambda x: (-len(x[0].prefix), -x[0].priority, x[1].id))
    return [g for _, g in candidates]
