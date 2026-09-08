"""v0.3 多租户：账户(Account)管理接口（增删改查 + 充值 + 余额流水）。

账户即租户、即计费主体：
- account_number 自动分配（8000 起自增，全局唯一，同时作为话机号码前缀）。
- name / rate 业务必填（rate 为账户级默认费率，兜底层）。
- credit_limit / min_balance 控制预付费透支与预留额度（逐租户可配）。
- 删除有依赖（话机/业务）时拒绝。
全部受 T-301 鉴权守护（app.py 中间件白名单外的接口均守护）。
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, func

from db.session import get_db, SessionLocal
from db.models import Account, AccountLedger, Customer, SipPhone, Business, Carrier, CarrierLedger

router = APIRouter(prefix="/api", tags=["accounts"])


def _next_account_number(db) -> str:
    """取当前已用最大租户号 +1，基线 8000（存量 id=1 已占 8000，新建从 8001 起）。"""
    used = db.execute(
        select(Account.account_number).where(Account.account_number.isnot(None))
    ).fetchall()
    nums = [int(r[0]) for r in used if r[0] and str(r[0]).isdigit()]
    nxt = max(nums + [7999]) + 1
    return str(nxt)


def _to_dict(a: Account) -> dict:
    return {
        "id": a.id,
        "customer_id": a.customer_id,
        "name": a.name,
        "balance": float(a.balance) if a.balance is not None else 0.0,
        "currency": a.currency,
        "rate": float(a.rate) if a.rate is not None else None,
        "account_number": a.account_number,
        "credit_limit": float(a.credit_limit) if a.credit_limit is not None else 0.0,
        "min_balance": float(a.min_balance) if a.min_balance is not None else 0.0,
        "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.get("/accounts")
def list_accounts(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                  db=Depends(get_db)):
    total = db.scalar(select(func.count()).select_from(Account))
    total_pages = ceil(total / page_size) if total else 1
    if page > total_pages:
        page = total_pages
    rows = db.scalars(
        select(Account).order_by(Account.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"items": [_to_dict(r) for r in rows], "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages}


@router.post("/accounts")
async def create_account(request: Request, db=Depends(get_db)):
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="账户名称(name)必填")
    rate = data.get("rate")
    if rate in (None, ""):
        raise HTTPException(status_code=400, detail="账户费率(rate)必填")
    try:
        rate = Decimal(str(rate))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="费率(rate)非法")
    # customer_id：历史遗留维度，业务上不使用（customer 表为空）。
    # 2026-09-07 起完全忽略：不传即写 NULL，不再要求 Customer 存在、不再自动归拢。
    cid = data.get("customer_id")
    if cid in (None, "", "null"):
        cid = None
    # status 前端 select 未选时 collectForm 会置 null（非缺省 1），int(None) 会 TypeError→500，须兜底
    _status = data.get("status")
    status = int(_status) if _status not in (None, "") else 1
    acct = Account(
        customer_id=(int(cid) if cid not in (None, "", "null") else None),
        name=name,
        rate=rate,
        account_number=_next_account_number(db),
        credit_limit=Decimal(str(data["credit_limit"])) if data.get("credit_limit") not in (None, "") else Decimal("0"),
        min_balance=Decimal(str(data["min_balance"])) if data.get("min_balance") not in (None, "") else Decimal("0"),
        balance=Decimal("0"),
        currency=data.get("currency") or "CNY",
        status=status,
        # 账户表 created_at/updated_at 实际 NOT NULL（与模型 metadata 标注不一致，见 server_facts）；
        # 不显式赋值会触发 IntegrityError(1048) → POST /api/accounts 500。
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(acct)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="create failed: %s" % e)
    db.refresh(acct)
    return _to_dict(acct)


@router.get("/accounts/{account_id}")
def get_account(account_id: int, db=Depends(get_db)):
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="not found")
    return _to_dict(a)


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, request: Request, db=Depends(get_db)):
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="not found")
    data = await request.json()
    if "name" in data and data["name"] is not None:
        nm = str(data["name"]).strip()
        if not nm:
            raise HTTPException(status_code=400, detail="账户名称(name)不能为空")
        a.name = nm
    if "rate" in data and data["rate"] not in (None, ""):
        try:
            a.rate = Decimal(str(data["rate"]))
        except (InvalidOperation, ValueError, TypeError):
            raise HTTPException(status_code=400, detail="费率(rate)非法")
    for f in ("credit_limit", "min_balance"):
        if f in data and data[f] not in (None, ""):
            setattr(a, f, Decimal(str(data[f])))
    if "status" in data and data["status"] is not None:
        a.status = int(data["status"])
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="update failed: %s" % e)
    # 费率变更：立即失效计费缓存（会影响该账户下话单的兜底费率）
    if "rate" in data:
        try:
            from esl_client import clear_rate_cache
            clear_rate_cache()
        except Exception as _e:
            import logging
            logging.getLogger("accounts").error("clear rate cache failed: %s", _e)
    db.refresh(a)
    return _to_dict(a)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db=Depends(get_db)):
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="not found")
    ph = db.scalar(select(func.count()).select_from(SipPhone).where(SipPhone.account_id == account_id))
    if ph:
        raise HTTPException(status_code=400, detail="账户仍绑定 %d 个话机，请先解绑后再删除" % ph)
    biz = db.scalar(select(func.count()).select_from(Business).where(Business.account_id == account_id))
    if biz:
        raise HTTPException(status_code=400, detail="账户仍绑定 %d 个业务，请先解绑后再删除" % biz)
    db.delete(a)
    db.commit()
    return {"ok": True, "id": account_id}


@router.get("/accounts/{account_id}/ledger")
def list_ledger(account_id: int, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                ledger_type: int = Query(None, alias="type"),
                from_: str = Query(None, alias="from"), to: str = Query(None),
                db=Depends(get_db)):
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="not found")
    base = select(AccountLedger).where(AccountLedger.account_id == account_id)
    if ledger_type is not None:
        base = base.where(AccountLedger.type == ledger_type)
    if from_:
        base = base.where(AccountLedger.created_at >= from_)
    if to:
        base = base.where(AccountLedger.created_at <= to + " 23:59:59")
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    total_pages = ceil(total / page_size) if total else 1
    if page > total_pages:
        page = total_pages
    rows = db.scalars(
        base.order_by(AccountLedger.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "account_id": r.account_id,
            "cdr_uuid": r.cdr_uuid,
            "type": r.type,
            "amount": float(r.amount) if r.amount is not None else 0.0,
            "balance_after": float(r.balance_after) if r.balance_after is not None else 0.0,
            "remark": r.remark,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": out, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}


@router.post("/accounts/{account_id}/recharge")
async def recharge(account_id: int, request: Request, db=Depends(get_db)):
    a = db.get(Account, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="not found")
    data = await request.json()
    amount = data.get("amount")
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="金额(amount)非法")
    if amount == 0:
        raise HTTPException(status_code=400, detail="金额不能为 0（正数充值 / 负数扣费）")
    # 用独立写会话 + 行锁，保证余额与流水一致
    # 负数即扣费：type 取 3(人工调整) 且 remark 默认「人工扣费」；正数充值 type=1。
    _ltype = 1 if amount > 0 else 3
    _remark = data.get("remark") or ("充值" if amount > 0 else "人工扣费")
    wdb = SessionLocal()
    try:
        acc = wdb.get(Account, account_id, with_for_update=True)
        acc.balance = (acc.balance or Decimal("0")) + amount
        bal_after = acc.balance
        lg = AccountLedger(
            account_id=account_id, cdr_uuid=None, type=_ltype, amount=amount,
            balance_after=bal_after, remark=_remark,
            created_at=datetime.now(),
        )
        wdb.add(lg)
        wdb.commit()
    except Exception as e:
        wdb.rollback()
        raise HTTPException(status_code=400, detail="recharge failed: %s" % e)
    finally:
        wdb.close()
    return {"id": account_id, "balance": float(bal_after), "amount": float(amount)}


# ---------------------------------------------------------------------------
# v0.3.1 成本侧扣费：运营商余额充值 / 流水（与账户侧对称，但运营商不拦截余额不足）
# ---------------------------------------------------------------------------

@router.post("/carriers/{carrier_id}/recharge")
async def recharge_carrier(carrier_id: int, request: Request, db=Depends(get_db)):
    ca = db.get(Carrier, carrier_id)
    if ca is None:
        raise HTTPException(status_code=404, detail="not found")
    data = await request.json()
    amount = data.get("amount")
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="金额(amount)非法")
    if amount == 0:
        raise HTTPException(status_code=400, detail="金额不能为 0（正数充值 / 负数扣费）")
    # 正数=充值(type1) / 负数=扣费(type3 人工调整)；与账户侧语义一致
    _ltype = 1 if amount > 0 else 3
    _remark = data.get("remark") or ("充值" if amount > 0 else "人工扣费")
    wdb = SessionLocal()
    try:
        car = wdb.get(Carrier, carrier_id, with_for_update=True)
        car.balance = (car.balance or Decimal("0")) + amount
        bal_after = car.balance
        lg = CarrierLedger(
            carrier_id=carrier_id, cdr_uuid=None, type=_ltype, amount=amount,
            balance_after=bal_after, remark=_remark,
            created_at=datetime.now(),
        )
        wdb.add(lg)
        wdb.commit()
    except Exception as e:
        wdb.rollback()
        raise HTTPException(status_code=400, detail="recharge failed: %s" % e)
    finally:
        wdb.close()
    return {"id": carrier_id, "balance": float(bal_after), "amount": float(amount)}


@router.get("/carriers/{carrier_id}/ledger")
def list_carrier_ledger(carrier_id: int, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                        ledger_type: int = Query(None, alias="type"),
                        from_: str = Query(None, alias="from"), to: str = Query(None),
                        db=Depends(get_db)):
    ca = db.get(Carrier, carrier_id)
    if ca is None:
        raise HTTPException(status_code=404, detail="not found")
    base = select(CarrierLedger).where(CarrierLedger.carrier_id == carrier_id)
    if ledger_type is not None:
        base = base.where(CarrierLedger.type == ledger_type)
    if from_:
        base = base.where(CarrierLedger.created_at >= from_)
    if to:
        base = base.where(CarrierLedger.created_at <= to + " 23:59:59")
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    total_pages = ceil(total / page_size) if total else 1
    if page > total_pages:
        page = total_pages
    rows = db.scalars(
        base.order_by(CarrierLedger.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "carrier_id": r.carrier_id,
            "cdr_uuid": r.cdr_uuid,
            "type": r.type,
            "amount": float(r.amount) if r.amount is not None else 0.0,
            "balance_after": float(r.balance_after) if r.balance_after is not None else 0.0,
            "remark": r.remark,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": out, "page": page, "page_size": page_size, "total": total, "total_pages": total_pages}
