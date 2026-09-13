"""角色校验扩展点（T-301 · M3 尾巴，2026-09-11 拆分；2026-09-12 M3 落地）。

工程约定（2026-09-11）：贡献者**不得修改 src/api/app.py**；M3 角色校验的实现
统一在本文件完成。需要改 app.py 或本文件接口签名时，先与维护者同步评估。

M3 用户管理 Phase 1（64ef74a 拍板）后的生效语义：
- ENFORCE_ROLE=True（2026-09-12 随角色判定落地翻转，E2E 403 矩阵已验证）：
  require_role 硬拦截；write_guard_middleware 对 viewer 全站只读。
- 三档口径：viewer(0) 只读 / admin(1) 业务管理 / super(2) 用户管理+系统设置。
- 角色每请求现查 DB（token 不携带角色，改角色即时生效）；查询异常回落 admin
  （fail-open，业务可用性优先）——唯独 users.py 的 require_role("super") 在
  回落 admin 时会 403（用户管理宁可误拒不误放）。

用法（在具体路由/路由器上）：
    from api.authz import require_role
    @router.get("/xxx", dependencies=[Depends(require_role("admin"))])
"""
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from api.auth import get_current_admin
from db.models import SysUser
from db.session import get_db, SessionLocal

# 2026-09-12 M3 落地翻转（原 fail-open 观察期结束；验证记录见 PR 描述/E2E 矩阵）
ENFORCE_ROLE = True

# sys_user.role 数值 -> 角色名口径（M3 在此维护；与 migrate.ensure_sys_user_role_comment 同步）
ROLE_NAMES = {0: "viewer", 1: "admin", 2: "super"}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 无登录态或幂等的写路径不拦（与 oplog._SKIP_PATHS 口径一致）
_SKIP_PATHS = {"/api/login", "/api/logout",
               # viewer 改自己密码放行（只读指业务数据，不含自身凭据，见 users.py 注释）；
               # 路由级 require_role() 仍要求登录 + 旧口令校验兜底
               "/api/users/me/password"}


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


async def write_guard_middleware(request, call_next):
    """viewer 全站只读守卫（挂在 app.py，一行；实现全在本文件）。

    语义：/api/* 写方法（POST/PUT/PATCH/DELETE）对 viewer 一律 403；
    admin/super 放行（更细粒度由路由级 require_role 叠加，如 users=super）。
    未登录请求放行给下游 _auth_guard 统一回 401（职责分离，不重复判）。
    """
    path = request.url.path
    if (ENFORCE_ROLE and request.method in _WRITE_METHODS
            and path.startswith("/api/") and path not in _SKIP_PATHS):
        try:
            user = get_current_admin(request)   # 未登录 -> 交给下游 401
        except HTTPException:
            return await call_next(request)
        db = SessionLocal()
        try:
            role = _role_of(db, user)
        finally:
            db.close()
        if role == "viewer":
            return JSONResponse(
                {"detail": "forbidden: viewer is read-only"}, status_code=403)
    return await call_next(request)

