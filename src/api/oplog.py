"""操作日志写入扩展点（T-301 · M3 尾巴，2026-09-11 拆分）。

工程约定（2026-09-11）：贡献者**不得修改 src/api/app.py**；operation_log 的
写入策略/字段口径统一在本文件实现。需要改 app.py 或本文件接口签名时，先与
维护者同步评估。

两级用法：
1. 自动埋点（app.py 已挂载 oplog_middleware，本文件只管实现）：
   管理端写操作（POST/PUT/PATCH/DELETE 且路径以 /api/ 开头，排除 login/logout）
   成功或业务失败（<500）即记一行，operator 取当前登录用户。
2. 显式埋点：业务代码记录非 HTTP 语义操作时调用 record_op(...)。
"""
from datetime import datetime

from db.models import OperationLog
from db.session import SessionLocal

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SKIP_PATHS = {"/api/login", "/api/logout"}


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
