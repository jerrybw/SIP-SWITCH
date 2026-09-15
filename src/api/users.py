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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.authz import require_role, ROLE_NAMES
from core.pw_hash import hash_password, verify_password
from db.models import Role, SysUser
from db.session import get_db

router = APIRouter(prefix="/api/users", tags=["users"])


def _user_out(u: SysUser, role_name: str | None = None) -> dict:
    """role_name：int 三档口径（内置）；自定义角色由 list/create/update 查 roles 表
    补 display 名（_user_out 本身不查库——直调测试与端点共用同一形状）。"""
    return {"id": u.id, "username": u.username, "role": u.role,
            "role_code": (u.role_code or "").strip() or None,
            "role_name": role_name if role_name is not None
            else ROLE_NAMES.get(u.role, "admin"),
            "status": u.status, "created_at": u.created_at}


def _role_display(db: Session, u: SysUser) -> dict:
    """行展示信息：role_code 非空时补自定义角色名（roles 行缺失/停用仍展示 code，
    与 authz._role_of 回落 admin 的判定口径分开——展示忠实于数据，判定忠实于安全）。"""
    out = _user_out(u)
    if out["role_code"]:
        r = db.scalar(select(Role).where(Role.code == out["role_code"]))
        if r is not None:
            out["role_name"] = r.name + ("（停用）" if not r.enabled else "")
    return out


def _valid_role_code(db: Session, code: str) -> str:
    """校验并归一 role_code：内置 code（viewer/admin/super）合法但走 int 列表达
    （保持三档数据单一口径）；自定义 code 须存在且启用（停用角色再挂人 =
    隐性权限变更，创建期拒绝、存量由 _role_of 回落 admin 兜底）。"""
    from api.authz import _BUILTIN
    code = (code or "").strip()
    if not code:
        return ""
    if code in _BUILTIN:
        raise HTTPException(status_code=400,
                            detail="builtin role goes via role int field")
    r = db.scalar(select(Role).where(Role.code == code))
    if r is None:
        raise HTTPException(status_code=400, detail="unknown role_code: %s" % code)
    if not r.enabled:
        raise HTTPException(status_code=400, detail="role disabled: %s" % code)
    return code


def _enabled_super_count(db: Session) -> int:
    return len(db.scalars(select(SysUser).where(
        SysUser.role == 2, SysUser.status == 1)).all())


class UserCreate(BaseModel):
    username: str
    password: str
    role: int = 1
    role_code: str | None = None    # M3-P2：自定义角色（与 role 互斥表达：给出即优先）


class UserUpdate(BaseModel):
    role: int | None = None
    role_code: str | None = None    # None=不改；""=清回 int 三档
    status: int | None = None
    username: str | None = None


class PasswordReset(BaseModel):
    password: str


@router.get("", dependencies=[Depends(require_role("super"))])
def list_users(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
               db: Session = Depends(get_db)):
    """分页口径与 operation-logs 一致（total/total_pages/offset+limit，id 正序）。"""
    total = db.scalar(select(func.count()).select_from(SysUser)) or 0
    total_pages = (total + page_size - 1) // page_size if total else 1
    if page > total_pages:
        page = total_pages
    rows = db.scalars(select(SysUser).order_by(SysUser.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [_role_display(db, u) for u in rows], "page": page,
            "page_size": page_size, "total": total, "total_pages": total_pages}


@router.post("", status_code=201, dependencies=[Depends(require_role("super"))])
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    username = (body.username or "").strip()
    if not username or len(username) > 64:
        raise HTTPException(status_code=400, detail="invalid username")
    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="password too short (min 8)")
    if body.role not in ROLE_NAMES:
        raise HTTPException(status_code=400, detail="invalid role")
    code = _valid_role_code(db, body.role_code or "")
    dup = db.scalar(select(SysUser).where(SysUser.username == username))
    if dup is not None:
        raise HTTPException(status_code=400, detail="username exists")
    # 自定义角色用户：int 列同步记 admin(1)——_role_of 只认 role_code，int 仅作
    # 老代码/报表兜底口径（不落 0/2：防清空 role_code 后把人误判成 viewer/super）
    u = SysUser(username=username, password_hash=hash_password(body.password),
                role=1 if code else body.role,
                role_code=code or None, status=1, created_at=datetime.utcnow())
    db.add(u)
    db.commit()
    db.refresh(u)
    return _role_display(db, u)


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
    # role_code 语义：None=不改；""=清回 int 三档；值=挂自定义角色（校验存在+启用）。
    new_code = _valid_role_code(db, body.role_code) if body.role_code is not None else None
    # 不变量：最后一个启用的 super 不可降级/停用；不可停用/降级自己。
    # 降级守卫只保护**启用的** super 行：已停用的 super 不在「启用 super 计数」内，
    # 降级它不会让系统失去可登录的 super（守卫口径与 _enabled_super_count 一致）。
    # M3-P2：把 super 挂上自定义角色（role_code 非空）同为降级——_role_of 里
    # role_code 优先，挂着自定义 code 的行不再是 super。
    becomes_custom = (new_code is not None and new_code != "")
    demote = (u.role == 2 and u.status == 1
              and ((body.role is not None and body.role != 2) or becomes_custom))
    disable = (u.status == 1 and body.status == 0)
    if demote or disable:
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
    if body.role_code is not None:
        u.role_code = new_code or None
        if becomes_custom:
            u.role = 1    # 与 create 同口径：自定义角色行 int 列记 admin 兜底
    if body.status is not None:
        u.status = body.status
    db.commit()
    db.refresh(u)
    return _role_display(db, u)


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
    # 与 update 同口径：只有「启用的 super」 deletion 才受 last-super 守卫
    # （停用的 super 行删除不减少可登录 super 数）。
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
