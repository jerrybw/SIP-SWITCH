"""角色校验扩展点（T-301 · M3 尾巴，2026-09-11 拆分）。

工程约定（2026-09-11）：贡献者**不得修改 src/api/app.py**；M3 角色校验的实现
统一在本文件完成。需要改 app.py 或本文件接口签名时，先与维护者同步评估。

当前阶段「记录不拦截」（fail-open）：ENFORCE_ROLE=False 时 require_role 只做
登录校验 + 角色解析并放行；M3 完成角色判定并验证后把 ENFORCE_ROLE 置 True
即全站生效，无需再动 app.py。

用法（在具体路由/路由器上）：
    from api.authz import require_role
    @router.get("/xxx", dependencies=[Depends(require_role("admin"))])
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from api.auth import get_current_admin
from db.models import SysUser
from db.session import get_db

# M3 角色判定实现并验证后切 True（硬校验）
ENFORCE_ROLE = False

# sys_user.role 数值 -> 角色名口径（M3 在此维护；当前仅 1=admin 有种子账号）
ROLE_NAMES = {0: "viewer", 1: "admin", 2: "super"}


def _role_of(db, username: str) -> str:
    """查询用户角色名；查询异常/用户缺失一律回落 admin（fail-open，不阻塞管理端）。"""
    try:
        row = db.execute(select(SysUser).where(SysUser.username == username)).first()
        if row and row[0].role is not None:
            return ROLE_NAMES.get(row[0].role, "admin")
        return "admin"
    except Exception as e:  # noqa: BLE001
        print("[authz] role lookup failed (user=%s): %s" % (username, e), flush=True)
        return "admin"


def require_role(*roles):
    """FastAPI 依赖工厂：Depends(require_role("admin"))。

    返回 {"user": ..., "role": ...} 供路由取用；ENFORCE_ROLE=False 时只解析不拦截。
    """
    def dep(request: Request, db=Depends(get_db)):
        user = get_current_admin(request)  # 未登录 -> 401（与全站鉴权口径一致）
        role = _role_of(db, user)
        if ENFORCE_ROLE and roles and role not in roles:
            raise HTTPException(status_code=403, detail="forbidden: role=%s" % role)
        return {"user": user, "role": role}

    return dep
