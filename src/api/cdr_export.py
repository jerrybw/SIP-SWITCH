"""CDR 导出扩展点（T-306 · M3 尾巴，2026-09-11 拆分）。

工程约定（2026-09-11）：贡献者**不得修改 src/api/app.py**；导出格式/字段口径/
大数据量异步化统一在本文件实现（本 router 已在 app.py 挂载，且注册在 crud 兜底
路由之前——PITFALLS #34）。需要改挂载方式或本文件接口签名时，先与维护者同步。

当前提供基础 CSV 导出：字段与 /api/cdr 列表核心列对齐，支持时间范围与行数上限；
数据量增大后在此文件内演进（分批流式 / 异步任务 / 字段选择），不动 app.py。
"""
import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.auth import get_current_admin
from db.models import Cdr
from db.session import get_db

router = APIRouter(prefix="/api/cdr", tags=["cdr-export"])

# 导出列（M3 在此维护口径；与前端 /api/cdr 列表展示保持一致）
_FIELDS = [
    "id", "uuid", "caller_in", "callee_in", "caller_out", "callee_out",
    "gateway_id", "access_point_id", "account_id", "sip_code", "hangup_cause",
    "talk_duration", "bill_duration", "cost", "start_time", "end_time",
    "created_at", "fs_node_uuid",
]


def _fmt(v):
    return "" if v is None else str(v)


@router.get("/export")
def export_cdr(start: str = Query(None, description="ISO 时间，如 2026-09-11T00:00:00"),
               end: str = Query(None, description="ISO 时间"),
               limit: int = Query(10000, ge=1, le=100000),
               user: str = Depends(get_current_admin),
               db: Session = Depends(get_db)):
    """CSV 导出（按 id 倒序，最多 limit 行）。"""
    q = select(Cdr).order_by(Cdr.id.desc()).limit(limit)
    try:
        if start:
            q = q.where(Cdr.created_at >= datetime.fromisoformat(start))
        if end:
            q = q.where(Cdr.created_at <= datetime.fromisoformat(end))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="start/end 须为 ISO 时间格式，如 2026-09-11T00:00:00")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_FIELDS)
    for row in db.execute(q).scalars():
        writer.writerow([_fmt(getattr(row, f)) for f in _FIELDS])
    buf.seek(0)

    filename = "cdr_export_%s.csv" % datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=%s" % filename},
    )
