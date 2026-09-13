"""操作日志写入扩展点（T-301 · M3 尾巴，2026-09-11 拆分）。

工程约定（2026-09-11）：贡献者**不得修改 src/api/app.py**；operation_log 的
写入策略/字段口径统一在本文件实现。需要改 app.py 或本文件接口签名时，先与
维护者同步评估。

两级用法：
1. 自动埋点（app.py 已挂载 oplog_middleware，本文件只管实现）：
   管理端写操作（POST/PUT/PATCH/DELETE 且路径以 /api/ 开头，排除 login/logout）
   成功或业务失败（<500）即记一行，operator 取当前登录用户。
2. 显式埋点：业务代码记录非 HTTP 语义操作时调用 record_op(...)。

查询面（2026-09-13 M3 收口补齐，维护者已批挂载）：GET /api/operation-logs
分页 + operator/action 过滤；登录即可查、不限角色（维护者拍板：审计面全员可见，
viewer 也可查）。登录要求**显式声明**在路由上（require_role() 空 roles = 仅校验
登录）——安全性由全站 _auth_guard 兜底，显式写法是防将来白名单调整时被绕过。
router 注册在 crud 兜底之前（app.py 已挂载，PITFALLS #34）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.authz import require_role
from db.models import OperationLog
from db.session import SessionLocal, get_db

router = APIRouter(prefix="/api/operation-logs", tags=["oplog"])

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SKIP_PATHS = {"/api/login", "/api/logout"}


@router.get("", dependencies=[Depends(require_role())])
def list_operation_logs(page: int = Query(1, ge=1),
                        page_size: int = Query(50, ge=1, le=200),
                        operator: str = None, action: str = None,
                        db: Session = Depends(get_db)):
    """管理端操作日志查询（只读；登录即可查，不限角色——维护者拍板口径）。"""
    q = select(OperationLog)
    if operator:
        q = q.where(OperationLog.operator.like("%" + operator + "%"))
    if action:
        q = q.where(OperationLog.action == action)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    total_pages = (total + page_size - 1) // page_size if total else 1
    if page > total_pages:
        page = total_pages
    rows = db.scalars(q.order_by(OperationLog.id.desc())
                     .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": r.id, "operator": r.operator, "action": r.action,
                       "object_type": r.object_type, "object_id": r.object_id,
                       "detail": r.detail, "created_at": r.created_at}
                      for r in rows],
            "page": page, "page_size": page_size,
            "total": total, "total_pages": total_pages}


def record_op(operator, action, object_type, object_id=None, detail=None):
    """写一行 operation_log。失败只打日志，绝不阻塞业务。"""
    try:
        db = SessionLocal()
        try:
            db.add(OperationLog(
                operator=(operator or "anonymous")[:64],
                action=(action or "")[:32],
                object_type=(object_type or "")[:32],
                object_id=(str(object_id)[:64] if object_id is not None else None),
                detail=detail if isinstance(detail, (dict, list))
                else ({"info": str(detail)} if detail else None),
                created_at=datetime.utcnow(),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print("[oplog] write failed: %s" % e, flush=True)


async def oplog_middleware(request, call_next):
    """HTTP 自动埋点：管理端写操作落 operation_log（挂在 app.py，一行，勿展开）。"""
    response = await call_next(request)
    try:
        path = request.url.path
        if (request.method in _WRITE_METHODS and path.startswith("/api/")
                and path not in _SKIP_PATHS and response.status_code < 500):
            user = "anonymous"
            try:
                from api.auth import get_current_admin
                user = get_current_admin(request)
            except Exception:  # noqa: BLE001
                pass
            record_op(user, request.method.lower(), "http", path,
                      {"status": response.status_code})
    except Exception as e:  # noqa: BLE001
        print("[oplog] middleware error: %s" % e, flush=True)
    return response
