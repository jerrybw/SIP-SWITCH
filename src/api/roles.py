"""M3 Phase 2 角色管理（T-301 深化 · 2026-09-14 实施方案，P1-P6 全批）。

仅 super 可操作（require_role("super")——自定义角色恒不匹配，纵深兜底）。
路由经 app.py 挂载两行（include 于 crud 兜底前，PITFALLS #34）。

防锁死守卫（实施方案 §4 / 参考稿 §4，缺一不交付）：
1. 内置三档 builtin=1：不可删除、不可停用、perms 不可改（矩阵是权限语义的
   "宪法"，改内置矩阵=改代码——BUILTIN_FALLBACK 是唯一真源，DB 行仅自定义角色）。
2. 删除被引用角色（sys_user.role_code 仍有行）→ 400，要求先转移用户。
3. 新角色默认全 none（fail-closed，不做继承、不默认放行）。
4. 自定义角色的 users / system 恒 none（只有内置 super 能管用户和系统——
   即便 super 操作者也摘不掉自己的权限，天然满足守卫 4）。
5. 改动下一次请求即生效（authz 每请求查 DB，本文件零缓存）。
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.authz import require_role, FEATURE_PATHS, _BUILTIN
from db.models import Role, RolePerm, SysUser
from db.session import get_db

router = APIRouter(prefix="/api/roles", tags=["roles"])

# 14 feature 的合法值清单（含排序供前端勾选页展示）
FEATURES = [
    "access-points", "gateways", "routes", "rules", "sip-phones", "carriers",
    "accounts", "billing", "cdr", "cdr.export", "nodes", "users", "oplogs",
    "system",
]
_PERMS = ("none", "read", "write")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
# 自定义角色恒 none 的功能点（守卫 4：用户/系统管理只属于内置 super）
_FORCE_NONE = ("users", "system")


def _validate_perms(perms: dict) -> dict:
    """入参 perms 校验 + 强制口径：未知 feature / 非法 perm 400；users/system 恒 none。"""
    out = {}
    for k, v in (perms or {}).items():
        if k not in FEATURES:
            raise HTTPException(status_code=400, detail="unknown feature: %s" % k)
        if v not in _PERMS:
            raise HTTPException(status_code=400, detail="invalid perm: %s" % v)
        out[k] = v
    if "users" in out or "system" in out:
        # 显式要求给自定义角色开 users/system 是配置错误，400 拒绝（不是静默改写——
        # 静默会让管理员以为给到了）
        raise HTTPException(status_code=400,
                            detail="custom role cannot have users/system perm")
    return out


def _role_out(r: Role, perms: dict) -> dict:
    return {"id": r.id, "code": r.code, "name": r.name, "builtin": r.builtin,
            "enabled": r.enabled, "sort": r.sort, "perms": perms,
            "created_at": r.created_at}


def _load_perms(db: Session, code: str) -> dict:
    rows = db.scalars(select(RolePerm).where(RolePerm.role_code == code)).all()
    return {r.feature: r.perm for r in rows}


def _write_perms(db: Session, code: str, perms: dict) -> None:
    """整组覆盖（delete-then-insert，事务内）。"""
    for r in db.scalars(select(RolePerm).where(RolePerm.role_code == code)).all():
        db.delete(r)
    for feature, perm in sorted((perms or {}).items()):
        db.add(RolePerm(role_code=code, feature=feature, perm=perm))


class RoleCreate(BaseModel):
    code: str
    name: str
    perms: dict = {}


class RoleUpdate(BaseModel):
    name: str = None
    enabled: int = None
    perms: dict = None


@router.get("", dependencies=[Depends(require_role("super"))])
def list_roles(db: Session = Depends(get_db)):
    """角色列表 + 每角色 perm 聚合（前端勾选页一次拉齐）。"""
    rows = db.scalars(select(Role).order_by(Role.sort, Role.id)).all()
    by_role = {}
    for r in db.scalars(select(RolePerm)).all():
        by_role.setdefault(r.role_code, {})[r.feature] = r.perm
    return {"items": [_role_out(r, by_role.get(r.code, {})) for r in rows],
            "features": FEATURES, "perms": list(_PERMS)}


@router.get("/options", dependencies=[Depends(require_role("super"))])
def role_options(db: Session = Depends(get_db)):
    """users 页角色下拉（内置 + 启用的自定义）。"""
    rows = db.scalars(select(Role).where(Role.enabled == 1)
                      .order_by(Role.sort, Role.id)).all()
    return {"items": [{"v": r.code, "t": r.name, "builtin": r.builtin} for r in rows]}


@router.post("", status_code=201, dependencies=[Depends(require_role("super"))])
def create_role(body: RoleCreate, db: Session = Depends(get_db)):
    code = (body.code or "").strip()
    name = (body.name or "").strip()
    if not _CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="invalid code (^[a-z][a-z0-9_]{1,31}$)")
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="invalid name")
    if code in _BUILTIN:
        raise HTTPException(status_code=400, detail="builtin code reserved")
    if db.scalar(select(Role).where(Role.code == code)) is not None:
        raise HTTPException(status_code=400, detail="code exists")
    perms = _validate_perms(body.perms)
    # 守卫 3：默认全 none —— 入参没给的 feature 一律 none，不继承任何角色
    full = {f: perms.get(f, "none") for f in FEATURES}
    r = Role(code=code, name=name, builtin=0, enabled=1,
             sort=100, created_at=datetime.utcnow())
    db.add(r)
    db.flush()
    _write_perms(db, code, full)
    db.commit()
    db.refresh(r)
    return _role_out(r, full)


@router.put("/{code}", dependencies=[Depends(require_role("super"))])
def update_role(code: str, body: RoleUpdate, db: Session = Depends(get_db)):
    r = db.scalar(select(Role).where(Role.code == code))
    if r is None:
        raise HTTPException(status_code=404, detail="role not found")
    if r.builtin:
        # 守卫 1：内置三档完全不可改（改名也不行——前端展示口径与代码真源强一致）
        raise HTTPException(status_code=400, detail="builtin role is immutable")
    if body.enabled is not None:
        if body.enabled not in (0, 1):
            raise HTTPException(status_code=400, detail="invalid enabled")
        # 守卫 4（延伸）：停用角色前须先转移其用户——停用即 _role_of 回落 admin，
        # 属隐性权限变更；此处要求显式清引用，与删除同口径
        n = db.scalar(select(func.count()).select_from(SysUser)
                      .where(SysUser.role_code == code)) or 0
        if body.enabled == 0 and n:
            raise HTTPException(status_code=400,
                                detail="role still referenced by %d user(s); move them first" % n)
        r.enabled = body.enabled
    if body.name is not None:
        name = body.name.strip()
        if not name or len(name) > 64:
            raise HTTPException(status_code=400, detail="invalid name")
        r.name = name
    if body.perms is not None:
        perms = _validate_perms(body.perms)
        full = {f: perms.get(f, "none") for f in FEATURES}
        _write_perms(db, code, full)
    db.commit()
    db.refresh(r)
    return _role_out(r, _load_perms(db, code))


@router.delete("/{code}", dependencies=[Depends(require_role("super"))])
def delete_role(code: str, db: Session = Depends(get_db)):
    r = db.scalar(select(Role).where(Role.code == code))
    if r is None:
        raise HTTPException(status_code=404, detail="role not found")
    if r.builtin:
        raise HTTPException(status_code=400, detail="builtin role cannot be deleted")
    n = db.scalar(select(func.count()).select_from(SysUser)
                  .where(SysUser.role_code == code)) or 0
    if n:
        raise HTTPException(status_code=400,
                            detail="role still referenced by %d user(s); move them first" % n)
    for row in db.scalars(select(RolePerm).where(RolePerm.role_code == code)).all():
        db.delete(row)
    db.delete(r)
    db.commit()
    return {"ok": True, "code": code}
