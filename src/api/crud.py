"""P3：管理端 REST CRUD（接入点 / 落地网关 / 前缀路由 / 规则 / 接入点↔落地策略）。

设计要点：
- 采用「字段白名单 + 通用路由」避免为每个模型重复样板；增改仅作用于 EDITABLE 白名单字段。
- 所有 INSERT 显式写 created_at（DB 实际 NOT NULL，与模型 metadata 标注不一致，见 server_facts）。
- 含 updated_at 的模型（AccessPoint/Gateway/Carrier）在 PUT 时刷新 updated_at。
- Rule.replace_to 空串视为 None（表示删前缀变换）。
- 接入点↔落地策略（G4）以 upsert 方式维护：同一 (ap, gw) 先删后插。
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy import select, delete, func
from sqlalchemy.orm import Session

from db.session import get_db
import logging
log=logging.getLogger("crud")
try:
    from src.fs_provision import provision, remove_xml
except Exception:
    provision=None
    remove_xml=None
from db.models import (
    AccessPoint, Gateway, PrefixRoute, Rule, AccessGatewayPolicy, Carrier, Business,
    SipPhone, SystemSetting, Account, Customer,
)

router = APIRouter(prefix="/api", tags=["crud"])

# 各实体的可写字段白名单
EDITABLE = {
    "access-points": ["account_id", "name", "auth_mode", "reg_username", "reg_password",
                      "register_host", "concurrent_limit", "record_enabled", "bill_unit", "rate", "status"],
    "gateways": ["carrier_id", "name", "ip", "port", "auth_type", "username", "password",
                 "concurrent_limit", "heartbeat_enabled", "heartbeat_interval",
                 "heartbeat_timeout", "switch_mode", "switch_timeout", "switch_codes",
                 # v0.3 成本侧：落地网关的成本计费单位(秒)与成本费率(元/成本计费单位)
                 "failover_pre_ring_only", "status", "bill_unit", "cost_rate"],
    "prefix-routes": ["gateway_id", "prefix", "priority", "status"],
    "rules": ["owner_type", "owner_id", "direction", "act", "pattern", "replace_to"],
    # v0.3 多租户：account_id 为话机归属（必选），号码受租户号段约束
    "sip-phones": ["account_id", "phone_number", "password", "domain", "rate", "description", "enabled"],
    # v0.3 运营商管理菜单（对标租户菜单）：成本计费单位 + 成本费率
    "carriers": ["name", "bill_unit", "cost_rate", "status"],
}
MODEL = {
    "access-points": AccessPoint,
    "gateways": Gateway,
    "prefix-routes": PrefixRoute,
    "rules": Rule,
    "sip-phones": SipPhone,
    "carriers": Carrier,
}

LIKE_FIELDS = {
    "access-points": ["name", "register_host", "reg_username"],
    "gateways": ["name", "ip", "username"],
    "prefix-routes": ["prefix"],
    "carriers": ["name"],
}


def _now() -> datetime:
    return datetime.now()


HIDDEN_FIELDS = {"sip-phones": {"sync_interval"}}


def _to_dict(obj, entity: str = "") -> dict:
    excl = HIDDEN_FIELDS.get(entity, set())
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in excl}


def _apply(obj, data: dict, entity: str):
    model = MODEL[entity]
    for k in EDITABLE[entity]:
        if k in data and data[k] is not None:
            v = data[k]
            if entity == "rules" and k == "replace_to" and v == "":
                v = None
            col = getattr(model, k)
            py = col.type.python_type
            if py is int and not isinstance(v, int):
                v = int(v)
            elif py is float and not isinstance(v, float):
                v = float(v)
            elif py is Decimal:
                # 数值列空串 = 清空（置 None）；其余转 Decimal 避免 float 写入精度/类型问题
                if v == "" or v is None:
                    v = None
                elif not isinstance(v, Decimal):
                    try:
                        v = Decimal(str(v))
                    except (InvalidOperation, ValueError, TypeError):
                        v = None
            setattr(obj, k, v)


# ---------------------------------------------------------------------------
# 下拉数据源（须定义在通用 /{entity} 通配之前，避免被吞）
# ---------------------------------------------------------------------------
# ⚠️ v0.3 路由顺序铁律：FastAPI 按注册顺序匹配，先注册的精确路径会永久屏蔽后续通配路由。
# 原先的 GET /carriers 精简下拉会屏蔽通用 GET /{entity}，导致运营商菜单拿不到完整字段。
# 故：运营商改由通用 CRUD 提供完整列表（carriers 已加入 MODEL/EDITABLE），
#     下拉数据源改名 /options，前端 select-src 同步改（见 admin.js）。
@router.get("/carriers/options")
def list_carrier_options(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name} for c in db.scalars(select(Carrier)).all()]


@router.get("/businesses/options")
def list_business_options(db: Session = Depends(get_db)):
    return [{"id": b.id, "name": b.name} for b in db.scalars(select(Business)).all()]


@router.get("/customers/options")
def list_customer_options(db: Session = Depends(get_db)):
    """客户精简下拉（仅供话单等页面 id→名称映射；Customer 无菜单维持现状）。"""
    return [{"id": c.id, "name": c.name} for c in db.scalars(select(Customer)).all()]


# ---------------------------------------------------------------------------
# 系统级配置（话机/接入点注册状态同步间隔等，key-value）
# ---------------------------------------------------------------------------
@router.get("/sys-config")
def get_sys_config(db: Session = Depends(get_db)):
    rows = db.scalars(select(SystemSetting)).all()
    return {r.key: r.value for r in rows}


@router.put("/sys-config")
async def put_sys_config(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    out = {}
    for k, v in (data or {}).items():
        if v is None:
            continue
        row = db.scalar(select(SystemSetting).where(SystemSetting.key == k))
        if row is None:
            row = SystemSetting(key=k, value=str(v), updated_at=_now())
            db.add(row)
        else:
            row.value = str(v)
            row.updated_at = _now()
        out[k] = str(v)
    db.commit()
    return out


# ---------------------------------------------------------------------------
# 通用 CRUD：接入点 / 落地网关 / 前缀路由 / 规则
# ---------------------------------------------------------------------------
@router.get("/{entity}")
def list_entity(entity: str, request: Request, page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    if entity not in MODEL:
        raise HTTPException(status_code=404, detail="unknown entity")
    q = select(MODEL[entity])
    # 允许按白名单字段等值过滤（如 rules?owner_type=2&owner_id=1）
    for k in EDITABLE.get(entity, []):
        val = request.query_params.get(k)
        if val is not None:
            col = getattr(MODEL[entity], k)
            if k in LIKE_FIELDS.get(entity, []):
                q = q.where(col.like("%" + val + "%"))
            else:
                cv = _coerce(col, val)
                # "null"/"" 等非法数值视为「不过滤」，避免 500（前端新增时 id=null）。
                if cv is not None:
                    q = q.where(col == cv)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    total_pages = ceil(total / page_size) if total else 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    rows = db.scalars(q.order_by(MODEL[entity].id.desc()).offset(offset).limit(page_size)).all()
    return {
        "items": [_to_dict(r, entity) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def _validate_phone(db: Session, data: dict, obj_id: int = None) -> None:
    """v0.3 多租户：话机号码校验（后端强校验）。

    规则：号码 = account_number(4) + 分机(4) = 8 位；必选 Account；同号不可重复。
    旧号（如 4 位）在迁移(停机窗口)前会校验失败，提示期望格式。
    """
    num = str(data.get("phone_number") or "").strip()
    acct_id = data.get("account_id")
    if obj_id is not None:
        # 修改场景：未提交的字段沿用库中原值（只改费率/状态等场景不该因缺号码被判非法）。
        old = db.execute(
            select(SipPhone.phone_number, SipPhone.account_id).where(SipPhone.id == obj_id)
        ).first()
        if old is not None:
            if not num:
                num = str(old[0] or "").strip()
            if acct_id is None:
                acct_id = old[1]
    if acct_id is None:
        raise HTTPException(status_code=400, detail="必须选择所属 Account")
    acct = db.get(Account, acct_id)
    if acct is None:
        raise HTTPException(status_code=400, detail="所属 Account 不存在")
    acct_no = str(acct.account_number or "").strip()
    if not acct_no:
        raise HTTPException(status_code=400, detail="所选 Account 缺少租户号(account_number)，无法校验号码")
    if not num.isdigit() or len(num) != 8 or not num.startswith(acct_no):
        raise HTTPException(
            status_code=400,
            detail="话机号码必须以租户号 %s 开头且总长 8 位（如 %s0001），当前值：%s"
                   % (acct_no, acct_no, num or "(空)"),
        )
    q = select(SipPhone.id).where(SipPhone.phone_number == num)
    if obj_id is not None:
        q = q.where(SipPhone.id != obj_id)
    if db.scalar(q) is not None:
        raise HTTPException(status_code=400, detail="话机号码 %s 已存在" % num)


def _coerce(col, val: str):
    py = col.type.python_type
    if py is int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    if py is float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return val


def _reject_global_translate(data: dict):
    # 需求 #1：全局(owner_type=1)不允许变换规则(act=3)
    ot = data.get("owner_type")
    act = data.get("act")
    try:
        if ot is not None and act is not None and int(ot) == 1 and int(act) == 3:
            raise HTTPException(status_code=400, detail="全局(owner_type=1)不支持变换规则(act=3)")
    except (ValueError, TypeError):
        pass


@router.post("/{entity}")
async def create_entity(entity: str, request: Request, db: Session = Depends(get_db)):
    if entity not in MODEL:
        raise HTTPException(status_code=404, detail="unknown entity")
    data = await request.json()
    _reject_global_translate(data)
    if entity == "sip-phones":
        _validate_phone(db, data)
    obj = MODEL[entity]()
    _apply(obj, data, entity)
    obj.created_at = _now()
    if hasattr(obj, "updated_at"):
        obj.updated_at = _now()
    db.add(obj)
    try:
        db.commit()
    except Exception as e:  # NOT NULL / 外键缺失等
        db.rollback()
        raise HTTPException(status_code=400, detail=f"create failed: {e}")
    db.refresh(obj)
    if entity=="gateways" and provision is not None:
        try:
            _pv=provision(obj)
            log.info("auto-provision gateway %s: %s",obj.name,_pv)
        except Exception as _e:
            log.error("auto-provision gateway %s failed: %s",getattr(obj,"name",None),_e)
    return _to_dict(obj, entity)


@router.get("/{entity}/{item_id}")
def get_entity(entity: str, item_id: int, db: Session = Depends(get_db)):
    if entity not in MODEL:
        raise HTTPException(status_code=404, detail="unknown entity")
    obj = db.get(MODEL[entity], item_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="not found")
    return _to_dict(obj, entity)


@router.put("/{entity}/{item_id}")
async def update_entity(entity: str, item_id: int, request: Request, db: Session = Depends(get_db)):
    if entity not in MODEL:
        raise HTTPException(status_code=404, detail="unknown entity")
    obj = db.get(MODEL[entity], item_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="not found")
    data = await request.json()
    _reject_global_translate(data)
    if entity == "sip-phones":
        # 形参名是 item_id（旧代码误写 obj_id → NameError → PUT /api/sip-phones/{id} 恒 500）
        _validate_phone(db, data, item_id)
    _apply(obj, data, entity)
    if hasattr(obj, "updated_at"):
        obj.updated_at = _now()
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"update failed: {e}")
    # 费率/成本字段变更：立即失效计费缓存，下通呼叫按新值算（含收入与成本两类 key）
    if entity in ("access-points", "sip-phones") and "rate" in data:
        try:
            from esl_client import clear_rate_cache
            clear_rate_cache()
        except Exception as _e:
            log.error("clear rate cache failed: %s", _e)
    if entity in ("carriers", "gateways") and ("cost_rate" in data or "bill_unit" in data):
        try:
            from esl_client import clear_rate_cache
            clear_rate_cache()
        except Exception as _e:
            log.error("clear cost cache failed: %s", _e)
    db.refresh(obj)
    if entity=="gateways" and provision is not None:
        try:
            _pv=provision(obj)
            log.info("auto-provision gateway %s: %s",obj.name,_pv)
        except Exception as _e:
            log.error("auto-provision gateway %s failed: %s",getattr(obj,"name",None),_e)
    return _to_dict(obj, entity)


@router.delete("/{entity}/{item_id}")
def delete_entity(entity: str, item_id: int, db: Session = Depends(get_db)):
    if entity not in MODEL:
        raise HTTPException(status_code=404, detail="unknown entity")
    obj = db.get(MODEL[entity], item_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="not found")
    if entity == "gateways":
        # 级联删除关联子表，避免外键约束导致 500：
        # 落地网关被「前缀路由 / 接入点↔落地策略」引用时，先清子表再删父表。
        db.execute(delete(PrefixRoute).where(PrefixRoute.gateway_id == item_id))
        db.execute(delete(AccessGatewayPolicy).where(AccessGatewayPolicy.gateway_id == item_id))
    if entity == "carriers":
        # v0.3 运营商删除依赖拦截：仍有落地网关绑定则拒绝，提示先解绑
        gw_cnt = db.scalar(select(func.count()).select_from(Gateway).where(Gateway.carrier_id == item_id))
        if gw_cnt:
            raise HTTPException(status_code=400,
                                detail="运营商仍绑定 %d 个落地网关，请先解绑后再删除" % gw_cnt)
    db.delete(obj)
    db.commit()
    if entity=="gateways" and remove_xml is not None:
        try:
            remove_xml(obj.name)
        except Exception as _e:
            log.error("remove gateway xml %s failed: %s",getattr(obj,"name",None),_e)
    return {"ok": True, "id": item_id}


# ---------------------------------------------------------------------------
# 接入点↔落地策略（G4）
# ---------------------------------------------------------------------------
@router.get("/access-points/{ap_id}/gateway-policies")
def list_ap_policies(ap_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AccessGatewayPolicy).where(AccessGatewayPolicy.access_point_id == ap_id)
    ).all()
    return [{"id": r.id, "gateway_id": r.gateway_id, "policy": r.policy} for r in rows]


@router.post("/access-points/{ap_id}/gateway-policies")
async def set_ap_policy(ap_id: int, request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    gw_id = data.get("gateway_id")
    policy = data.get("policy")
    if gw_id is None or policy is None:
        raise HTTPException(status_code=400, detail="gateway_id and policy required")
    # upsert：同一 (ap, gw) 先删后插
    db.execute(delete(AccessGatewayPolicy).where(
        AccessGatewayPolicy.access_point_id == ap_id,
        AccessGatewayPolicy.gateway_id == gw_id,
    ))
    r = AccessGatewayPolicy(access_point_id=ap_id, gateway_id=gw_id,
                             policy=int(policy), created_at=_now())
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "access_point_id": ap_id, "gateway_id": r.gateway_id, "policy": r.policy}


@router.delete("/access-points/{ap_id}/gateway-policies/{gw_id}")
def delete_ap_policy(ap_id: int, gw_id: int, db: Session = Depends(get_db)):
    db.execute(delete(AccessGatewayPolicy).where(
        AccessGatewayPolicy.access_point_id == ap_id,
        AccessGatewayPolicy.gateway_id == gw_id,
    ))
    db.commit()
    return {"ok": True, "access_point_id": ap_id, "gateway_id": gw_id}



