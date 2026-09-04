"""T-计费：管理端计费报表 / 导出 / 账户默认费率配置。

- GET /api/billing/summary  按 账户/业务/接入点/客户 维度聚合（消费额/计费时长/通话数/接通率）
- GET /api/billing/export   CSV 导出（同筛选条件）
- GET /api/billing/accounts 账户列表（含默认费率）
- PUT /api/billing/accounts/{id} 设置账户默认费率（兜底层级）

所有接口受 T-301 鉴权守卫保护（登录 cookie）。
"""
import io
import csv

from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, case
from sqlalchemy.orm import Session

from db.session import get_db
from db.models import Cdr, Account, Business, AccessPoint, Customer, Gateway, Carrier

router = APIRouter(prefix="/api", tags=["billing"])

# 维度 -> (CDR 列, 维度模型, 名称列)
DIM_MAP = {
    "account": (Cdr.account_id, Account, "name"),
    "business": (Cdr.business_id, Business, "name"),
    "access_point": (Cdr.access_point_id, AccessPoint, "name"),
    "customer": (Cdr.customer_id, Customer, "name"),
    # v0.3 成本维度
    "gateway": (Cdr.gateway_id, Gateway, "name"),
    "carrier": (Cdr.carrier_id, Carrier, "name"),
}


def _aggregate(dim, from_, to, account_id, db):
    if dim not in DIM_MAP:
        raise HTTPException(status_code=400,
                             detail="dim must be account|business|access_point|customer|gateway|carrier")
    dim_col, dim_model, dim_name_col = DIM_MAP[dim]
    q = select(
        dim_col.label("dim"),
        func.count(Cdr.id).label("calls"),
        func.coalesce(func.sum(Cdr.bill_duration), 0).label("bill_duration"),
        func.coalesce(func.sum(Cdr.cost), 0).label("cost"),
        func.coalesce(func.sum(Cdr.cost_price), 0).label("cost_price"),
        func.coalesce(func.sum(Cdr.profit), 0).label("profit"),
        func.sum(case((Cdr.answer_time.isnot(None), 1), else_=0)).label("answered"),
    ).where(Cdr.cost.isnot(None))
    if from_:
        q = q.where(Cdr.created_at >= from_)
    if to:
        q = q.where(Cdr.created_at <= to)
    if account_id:
        q = q.where(Cdr.account_id == account_id)
    q = q.group_by(dim_col)
    rows = db.execute(q).mappings().all()
    out = []
    for r in rows:
        d = r["dim"]
        name = None
        if d is not None:
            name = db.scalar(select(getattr(dim_model, dim_name_col)).where(dim_model.id == d))
        calls = int(r["calls"] or 0)
        answered = int(r["answered"] or 0)
        out.append({
            "dim": d,
            "dim_name": name if name is not None else ("未归集" if d is None else str(d)),
            "calls": calls,
            "bill_duration": int(r["bill_duration"] or 0),
            "cost": float(r["cost"] or 0),
            "cost_price": float(r["cost_price"] or 0),
            "profit": float(r["profit"] or 0),
            "answered": answered,
            "answer_rate": round((answered / calls) * 100, 2) if calls else 0,
        })
    total_calls = sum(x["calls"] for x in out)
    total_bill = sum(x["bill_duration"] for x in out)
    total_cost = sum(x["cost"] for x in out)
    total_cost_price = sum(x["cost_price"] for x in out)
    total_profit = sum(x["profit"] for x in out)
    total_answered = sum(x["answered"] for x in out)
    return {
        "dim": dim,
        "rows": out,
        "total": {
            "calls": total_calls,
            "bill_duration": total_bill,
            "cost": round(total_cost, 4),
            "cost_price": round(total_cost_price, 4),
            "profit": round(total_profit, 4),
            "answered": total_answered,
            "answer_rate": round((total_answered / total_calls) * 100, 2) if total_calls else 0,
        },
    }


@router.get("/billing/summary")
def billing_summary(dim: str = Query("account"),
                    from_: str = Query(None, alias="from"),
                    to: str = Query(None),
                    account_id: int = None,
                    db: Session = Depends(get_db)):
    return _aggregate(dim, from_, to, account_id, db)


@router.get("/billing/export")
def billing_export(dim: str = Query("account"),
                   from_: str = Query(None, alias="from"),
                   to: str = Query(None),
                   account_id: int = None,
                   db: Session = Depends(get_db)):
    data = _aggregate(dim, from_, to, account_id, db)
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel 兼容 BOM
    w = csv.writer(buf)
    w.writerow(["维度", "维度名称", "通话数", "计费时长(秒)", "消费额(元)", "成本(元)", "毛利(元)", "接通数", "接通率(%)"])
    for r in data["rows"]:
        w.writerow([r["dim"], r["dim_name"], r["calls"], r["bill_duration"], r["cost"],
                    r["cost_price"], r["profit"], r["answered"], r["answer_rate"]])
    t = data["total"]
    w.writerow(["合计", "", t["calls"], t["bill_duration"], t["cost"], t["cost_price"], t["profit"],
                t["answered"], t["answer_rate"]])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=billing_%s.csv" % dim},
    )


@router.get("/billing/accounts")
def list_billing_accounts(db: Session = Depends(get_db)):
    rows = db.scalars(select(Account)).all()
    return [{
        "id": a.id,
        "name": a.name,
        "customer_id": a.customer_id,
        "rate": float(a.rate) if a.rate is not None else None,
        "currency": a.currency,
    } for a in rows]


@router.put("/billing/accounts/{account_id}")
async def set_billing_account_rate(account_id: int, request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    rate = data.get("rate")
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="account not found")
    acct.rate = None if (rate is None or rate == "") else float(rate)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="update failed: %s" % e)
    # 费率变更立即失效内存缓存，下通呼叫按新费率算
    try:
        from esl_client import clear_rate_cache
        clear_rate_cache()
    except Exception as _e:
        print("[billing] clear rate cache failed:", _e)
    return {"id": acct.id, "rate": float(acct.rate) if acct.rate is not None else None}
