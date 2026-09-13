"""角色校验扩展点（T-301 · M3 尾巴，2026-09-11 拆分；2026-09-12 M3 落地；
2026-09-14 Phase 2：自定义角色 + 权限矩阵）。

工程约定（2026-09-11）：贡献者**不得修改 src/api/app.py**；M3 角色校验的实现
统一在本文件完成。需要改 app.py 或本文件接口签名时，先与维护者同步评估。

Phase 2（2026-09-14，实施方案 reviews/实施方案-M3-P2-roles-by-zcode.md，P1-P6 全批）：
- **path→feature 集中映射**（§FEATURE_PATHS 唯一一处，参考稿 §3.2 硬要求）；
- write_guard_middleware 原地升级为 perm_guard：三档 viewer 全站只读 →
  **按 role_perm 矩阵逐 feature 判 none/read/write**（内置三档矩阵缺行时回落
  Phase 1 硬编码等价表，防种子未灌破防）；
- 角色判定 `_role_of`：`sys_user.role_code` 优先 → 缺失回落 int 三档；
  自定义角色 disabled / 未知 → 回落 admin（防用户被锁死；fail-open 语义不变）；
- **自定义角色永不等于内置 super**：users/system 写权限只属于内置 super
  （require_role("super") 对自定义角色恒 403，纵深兜底）。

ENFORCE_ROLE=True（2026-09-12 翻转）；角色每请求现查 DB（token 不携带角色，
改角色下一次请求即生效，Phase 2 沿用）。

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

_BUILTIN = ("viewer", "admin", "super")

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 无登录态或幂等的写路径不拦（与 oplog._SKIP_PATHS 口径一致）
_SKIP_PATHS = {"/api/login", "/api/logout",
               # viewer 改自己密码放行（只读指业务数据，不含自身凭据，见 users.py 注释）；
               # 路由级 require_role() 仍要求登录 + 旧口令校验兜底
               "/api/users/me/password"}
# 登录态只读豁免（Phase 2）：/api/me 自身（登录后取 perms 用）
_READ_SKIP_PATHS = {"/api/me"}

# ---------------------------------------------------------------------------
# Phase 2 核心：path→feature 集中映射（**全仓唯一一处**，参考稿 §3.2 硬要求）。
# 结构：{feature: (path 前缀元组)}，前缀匹配；未命中 → "other"。
# 兜底口径：other 对内置三档 = read+write（与 Phase 1 行为完全一致，零回归）；
#           对自定义角色 = none（fail-closed）。
# 新增自建路由漏配 → 按 other 兜底 + 日志告警一行（首次命中时打）。
# ---------------------------------------------------------------------------
FEATURE_PATHS = {
    "access-points": ("/api/access-points",),
    "gateways": ("/api/gateways",),
    "routes": ("/api/prefix-routes",),
    "rules": ("/api/rules",),
    "sip-phones": ("/api/sip-phones",),
    "carriers": ("/api/carriers",),
    "accounts": ("/api/accounts",),
    "billing": ("/api/billing",),
    "cdr": ("/api/cdr",),                       # /api/cdr/export 由下表细分子串优先命中
    "cdr.export": ("/api/cdr/export",),
    "nodes": ("/api/nodes", "/api/provision/resync-all", "/api/webhook-test"),
    "users": ("/api/users",),
    "oplogs": ("/api/operation-logs",),
    "system": ("/api/sys-config", "/api/stats/concurrency", "/api/monitor/summary"),
}
_OTHER_WARNED = set()


def _feature_of(path: str) -> str:
    """path → feature（最长前缀优先，cdr.export 先于 cdr 命中）。"""
    best, best_len = "other", 0
    for feat, prefixes in FEATURE_PATHS.items():
        for p in prefixes:
            if path.startswith(p) and len(p) > best_len:
                best, best_len = feat, len(p)
    if best == "other" and path.startswith("/api/"):
        if path not in _OTHER_WARNED:
            _OTHER_WARNED.add(path)
            print("[authz] WARN unmapped api path %r -> feature=other" % path, flush=True)
    return best


# 内置三档等价矩阵（与 migrate._BUILTIN_PERMS 同源口径；role_perm 缺行时的
# 硬编码回落——防种子未灌/迁移失败时内置角色破防。Phase 1 行为的超集）。
BUILTIN_FALLBACK = {
    "viewer": {"access-points": "read", "gateways": "read", "routes": "read",
               "rules": "read", "sip-phones": "read", "carriers": "read",
               "accounts": "read", "billing": "read", "cdr": "read",
               "cdr.export": "none", "nodes": "read", "users": "none",
               "oplogs": "read", "system": "read", "other": "write"},
    "admin": {"access-points": "write", "gateways": "write", "routes": "write",
              "rules": "write", "sip-phones": "write", "carriers": "write",
              "accounts": "write", "billing": "write", "cdr": "read",
              "cdr.export": "write", "nodes": "read", "users": "none",
              "oplogs": "read", "system": "read", "other": "write"},
    "super": {"access-points": "write", "gateways": "write", "routes": "write",
              "rules": "write", "sip-phones": "write", "carriers": "write",
              "accounts": "write", "billing": "write", "cdr": "write",
              "cdr.export": "write", "nodes": "write", "users": "write",
              "oplogs": "read", "system": "write", "other": "write"},
}

_PERM_ORDER = {"none": 0, "read": 1, "write": 2}


def _perms_of(db, role_code: str) -> dict:
    """查角色矩阵；DB 异常/内置角色缺行时回落硬编码表（§BUILTIN_FALLBACK）。

    自定义角色查不到行 → 全 none（fail-closed，守卫 5：新角色默认全 none 的兜底）。
    返回 dict[feature] = perm（查库结果为唯一真源，不含 other——other 由调用方判定）。
    """
    try:
        from db.models import RolePerm
        rows = db.execute(select(RolePerm).where(RolePerm.role_code == role_code)).all()
        if not rows:
            if role_code in BUILTIN_FALLBACK:
                return dict(BUILTIN_FALLBACK[role_code])   # 内置缺行：硬编码兜底
            return {}                                       # 自定义缺行：全 none
        out = {}
        for r in rows:
            perm = r[0].perm if not isinstance(r, tuple) else r[-1]
            out[r[0].feature if not isinstance(r, tuple) else r[1]] = perm
        return out
    except Exception as e:  # noqa: BLE001 —— 矩阵查询失败：内置回落硬编码，自定义按 none
        print("[authz] role_perm lookup failed (role=%s): %s" % (role_code, e), flush=True)
        if role_code in BUILTIN_FALLBACK:
            return dict(BUILTIN_FALLBACK[role_code])
        return {}


def _role_of(db, username: str) -> str:
    """查询用户角色名（Phase 2：role_code 优先 → int 三档回落）。

    - sys_user.role_code 非空且对应角色 enabled → 返回 role_code；
    - role_code 指向的角色被禁用/不存在 → 回落 admin（防用户被锁死，禁用≠删除权限）；
    - role_code 为空 → ROLE_NAMES.get(role)（Phase 1 行为，老用户零感知）；
    - 查询异常/用户缺失 → admin（fail-open 语义不变）。
    """
    try:
        row = db.execute(select(SysUser).where(SysUser.username == username)).first()
        if not row:
            return "admin"
        u = row[0]
        code = (u.role_code or "").strip() if u.role_code is not None else ""
        if code:
            # 内置 code 直接信任（不查 roles 表）——种子失败/表空时内置三档仍成立，
            # 不允许"roles 表缺行"把 viewer/admin/super 打成 admin（fail-open 过头=破防）。
            if code in _BUILTIN:
                return code
            try:
                from db.models import Role
                r = db.scalar(select(Role).where(Role.code == code))
                if r is not None and r.enabled:
                    return code
                if r is not None and not r.enabled:
                    print("[authz] role %r disabled -> user %s falls back to admin"
                          % (code, username), flush=True)
                return "admin"
            except Exception:
                return "admin"      # roles 表异常：宁可回落也不放自定义角色
        if u.role is not None:
            return ROLE_NAMES.get(u.role, "admin")
        return "admin"
    except Exception as e:  # noqa: BLE001
        print("[authz] role lookup failed (user=%s): %s" % (username, e), flush=True)
        return "admin"


def _perm_of(db, role_code: str, feature: str, is_write: bool) -> str:
    """判定 (role, feature) 的有效 perm（供 perm_guard）。

    - 内置三档：**恒以 BUILTIN_FALLBACK 硬编码表为准**（与迁移种子同源；
      DB 行仅用于自定义角色）——理由：内置矩阵是权限语义的"宪法"，
      允许 DB 行缺失时行为漂移等于给"清空 role_perm 表"留破防口；
      管理员要改内置角色的权限 = 改代码（回滚可审）。
    - 自定义角色：以 DB 行为准，缺 feature 行 = none（fail-closed，守卫 5）；
      "other"（未命中映射的路径）对内置 = write（Phase 1 零回归）、自定义 = none。
    """
    if role_code in BUILTIN_FALLBACK:
        fb = BUILTIN_FALLBACK[role_code]
        return fb.get(feature) or fb.get("other", "read")
    perms = _perms_of(db, role_code)
    if feature in perms:
        return perms[feature]
    if feature == "other":
        return "none"
    return "none"


def require_role(*roles):
    """FastAPI 依赖工厂：Depends(require_role("admin"))。

    返回 {"user": ..., "role": ..., "role_code"?: ...}；ENFORCE_ROLE=False 时只解析不拦截。
    Phase 2 语义：roles 里是**内置**角色名才参与判定；自定义角色命中 roles 的唯一途径
    是 _role_of 返回它自身且 roles 显式含它——"super" 恒指内置 super（自定义角色
    在 users/system 端点恒 403，纵深兜底）。
    """
    def dep(request: Request, db=Depends(get_db)):
        user = get_current_admin(request)  # 未登录 -> 401（与全站鉴权口径一致）
        role = _role_of(db, user)
        if ENFORCE_ROLE and roles and role not in roles:
            raise HTTPException(status_code=403, detail="forbidden: role=%s" % role)
        return {"user": user, "role": role}

    return dep


async def write_guard_middleware(request, call_next):
    """Phase 2 perm_guard（原地升级，挂载行 app.py:342 不变）。

    语义（按 role_perm 矩阵逐 feature 判定）：
    - GET/HEAD：perm ≥ read 放行；none → 403
    - 写方法：perm = write 放行；none/read → 403
    - 未登录：放行给下游 _auth_guard 统一 401（职责分离，不重复判）
    - 豁免：_SKIP_PATHS（写豁免）/ _READ_SKIP_PATHS（读豁免）不变
    - /api/* 之外：不归矩阵管（/fs/*、/healthz、/admin 外壳等）
    - **矩阵行以 DB 为唯一真源**；内置角色缺行回落硬编码等价表（不破防），
      自定义角色缺行 = none（fail-closed）
    """
    path = request.url.path
    if not path.startswith("/api/") or path in _SKIP_PATHS:
        return await call_next(request)
    is_write = request.method in _WRITE_METHODS
    if not is_write and (request.method not in ("GET", "HEAD")
                         or path in _READ_SKIP_PATHS):
        return await call_next(request)     # 其余方法（OPTIONS 等）不管
    try:
        user = get_current_admin(request)   # 未登录 -> 交给下游 401
    except HTTPException:
        return await call_next(request)
    db = SessionLocal()
    try:
        role = _role_of(db, user)
        if ENFORCE_ROLE:
            perm = _perm_of(db, role, _feature_of(path), is_write)
            need = _PERM_ORDER["write"] if is_write else _PERM_ORDER["read"]
            if _PERM_ORDER.get(perm, 0) < need:
                return JSONResponse(
                    {"detail": "forbidden: role=%s perm=%s on %s"
                               % (role, perm, _feature_of(path))},
                    status_code=403)
    finally:
        db.close()
    return await call_next(request)
