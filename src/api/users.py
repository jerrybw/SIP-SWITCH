"""M3 用户管理 Phase 1（T-301 · 64ef74a 拍板）：管理端用户 CRUD。

仅 super 可操作（require_role("super") 硬校验；角色解析回落 admin 时同样 403，
用户管理宁可误拒不误放）。密码哈希复用 core/pw_hash（pbkdf2$ 版本化格式）。

路由须注册在 crud_router 兜底之前（PITFALLS #34，app.py 挂载处已注释）。
写操作经 oplog 中间件自动落 operation_log（operator/action/path），无需显式埋点。

业务不变量（create/update/delete/reset 统一守卫）：
- 不允许把**最后一个启用的 super** 降级 / 停用 / 删除（锁死自己 = 只能改库自救）
- 不允许对**自己**执行删除 / 停用 / 降级（防误操作把自己踢下线；改密走 reset 且不拦）
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.authz import require_role, ROLE_NAMES
from core.pw_hash import hash_password, verify_password
from db.models import SysUser
from db.session import get_db

router = APIRouter(prefix="/api/users", tags=["users"])


def _user_out(u: SysUser) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role,
            "role_name": ROLE_NAMES.get(u.role, "admin"), "status": u.status,
            "created_at": u.created_at}


def _enabled_super_count(db: Session) -> int:
    return len(db.scalars(select(SysUser).where(
        SysUser.role == 2, SysUser.status == 1)).all())


class UserCreate(BaseModel):
    username: str
    password: str
    role: int = 1


class UserUpdate(BaseModel):
    role: int | None = None
    status: int | None = None
    username: str | None = None


class PasswordReset(BaseModel):
    password: str


@router.get("", dependencies=[Depends(require_role("super"))])
def list_users(db: Session = Depends(get_db)):
    rows = db.scalars(select(SysUser).order_by(SysUser.id)).all()
    return {"items": [_user_out(u) for u in rows]}


@router.post("", status_code=201, dependencies=[Depends(require_role("super"))])
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    username = (body.username or "").strip()
    if not username or len(username) > 64:
        raise HTTPException(status_code=400, detail="invalid username")
    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="password too short (min 8)")
    if body.role not in ROLE_NAMES:
        raise HTTPException(status_code=400, detail="invalid role")
    dup = db.scalar(select(SysUser).where(SysUser.username == username))
    if dup is not None:
        raise HTTPException(status_code=400, detail="username exists")
    u = SysUser(username=username, password_hash=hash_password(body.password),
                role=body.role, status=1, created_at=datetime.utcnow())
    db.add(u)
    db.commit()
    db.refresh(u)
    return _user_out(u)


@router.put("/{uid}")
def update_user(uid: int, body: UserUpdate, db: Session = Depends(get_db),
                actor: dict = Depends(require_role("super"))):
    u = db.get(SysUser, uid)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if body.role is not None and body.role not in ROLE_NAMES:
        raise HTTPException(status_code=400, detail="invalid role")
    if body.status is not None and body.status not in (0, 1):
        raise HTTPException(status_code=400, detail="invalid status")
    # 不变量：最后一个启用的 super 不可降级/停用；不可停用/降级自己
    demote = (u.role == 2 and body.role is not None and body.role != 2)
    disable = (u.status == 1 and body.status == 0)
    if u.role == 2 and (demote or disable):
        if u.username == actor["user"]:
            raise HTTPException(status_code=400, detail="cannot demote/disable yourself")
        if _enabled_super_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote/disable the last enabled super")
    if body.username is not None:
        nu = body.username.strip()
        if not nu or len(nu) > 64:
            raise HTTPException(status_code=400, detail="invalid username")
        dup = db.scalar(select(SysUser).where(
            SysUser.username == nu, SysUser.id != uid))
        if dup is not None:
            raise HTTPException(status_code=400, detail="username exists")
        u.username = nu
    if body.role is not None:
        u.role = body.role
    if body.status is not None:
        u.status = body.status
    db.commit()
    db.refresh(u)
    return _user_out(u)


@router.post("/{uid}/reset-password", dependencies=[Depends(require_role("super"))])
def reset_password(uid: int, body: PasswordReset, db: Session = Depends(get_db)):
    u = db.get(SysUser, uid)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="password too short (min 8)")
    u.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True, "id": uid}


@router.delete("/{uid}")
def delete_user(uid: int, db: Session = Depends(get_db),
                actor: dict = Depends(require_role("super"))):
    u = db.get(SysUser, uid)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if u.username == actor["user"]:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    if u.role == 2 and u.status == 1 and _enabled_super_count(db) <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last enabled super")
    db.delete(u)
    db.commit()
    return {"ok": True, "id": uid}


# 供管理端改自己密码（非 super 也可用；需验旧密码）——不挂 require_role，
# 但 write_guard 只拦 viewer：viewer 改自己密码放行（只读指业务数据，不含自身凭据）。
class SelfPassword(BaseModel):
    old_password: str
    new_password: str


@router.post("/me/password")
def change_own_password(body: SelfPassword, db: Session = Depends(get_db),
                        actor: dict = Depends(require_role())):
    u = db.scalar(select(SysUser).where(SysUser.username == actor["user"]))
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not verify_password(body.old_password, u.password_hash or ""):
        raise HTTPException(status_code=401, detail="old password incorrect")
    if len(body.new_password or "") < 8:
        raise HTTPException(status_code=400, detail="password too short (min 8)")
    u.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}
