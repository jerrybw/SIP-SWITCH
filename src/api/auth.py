"""T-301 管理端鉴权：账号密码登录 -> HMAC 自签 token -> HttpOnly cookie。

零第三方依赖（标准库 hmac / hashlib / base64 / json / time / secrets）。
- token 形如 base64url(header).base64url(payload).HMAC-SHA256(signing_input, jwt_secret)
- 密码不存明文：config 存版本化哈希（hash_password，pbkdf2$iter$salt$dk）；
  校验端兼容历史 sha256(salt+password) 格式（存量部署免重置口令平滑升级）
- 缺 jwt_secret 时 fail-fast（_cfg 抛 RuntimeError），避免「以为有鉴权其实没配」
"""
import base64
import hashlib
import hmac
import json
import time
import secrets
from fastapi import APIRouter, Request, Response, HTTPException
from core.config import settings
from core.pw_hash import hash_password, verify_password as _verify_pw

router = APIRouter(prefix="/api", tags=["auth"])
_COOKIE = "sip_admin_sid"


def _cfg() -> dict:
    a = settings.get("auth") or {}
    if not a.get("jwt_secret"):
        # fail-fast：没配 jwt_secret 就别让服务以为有鉴权（否则全 401 死锁）
        raise RuntimeError("config_settings.yaml [auth] 缺少 jwt_secret，无法启用鉴权")
    return a


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def verify_password(password: str, stored: str) -> bool:
    """校验管理员口令：新 pbkdf2$ 格式优先，兼容遗留 sha256(salt+password)。

    遗留格式的 salt 取 config auth.password_salt（存量部署平滑升级路径）。
    """
    return _verify_pw(password, stored, legacy_salt=(_cfg().get("password_salt") or ""))


def _pw_hash(password: str) -> str:
    """遗留 sha256(salt+password) 公式。仅存量格式兼容与既有测试引用，新哈希走 hash_password。"""
    a = _cfg()
    salt = a.get("password_salt", "")
    return hashlib.sha256((salt + password).encode()).hexdigest()


def sign_token(user: str) -> str:
    a = _cfg()
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    exp = int(time.time()) + int(a.get("token_expire_minutes", 120)) * 60
    payload = _b64u(json.dumps({"sub": user, "exp": exp, "jti": secrets.token_hex(8)}).encode())
    sig = hmac.new(a["jwt_secret"].encode(), f"{header}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{header}.{payload}.{sig}"


def verify_token(tok: str) -> str:
    a = _cfg()
    try:
        h, p, s = tok.split(".")
    except Exception:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        payload = json.loads(_b64d(p))
    except Exception:
        raise HTTPException(status_code=401, detail="unauthorized")
    if payload.get("exp") is None or payload["exp"] < int(time.time()):
        raise HTTPException(status_code=401, detail="unauthorized")
    calc = hmac.new(a["jwt_secret"].encode(), f"{h}.{p}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, s):
        raise HTTPException(status_code=401, detail="unauthorized")
    return payload.get("sub")


def get_current_admin(request: Request) -> str:
    tok = request.cookies.get(_COOKIE)
    if not tok:
        raise HTTPException(status_code=401, detail="unauthorized")
    return verify_token(tok)


@router.post("/login")
async def login(request: Request, response: Response):
    a = _cfg()
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        raise HTTPException(status_code=400, detail="bad request")
    user = body.get("user", "")
    pw = body.get("password", "")
    # M3 用户管理 Phase 1（64ef74a 拍板）：登录切 DB —— 查 sys_user（status=1），
    # 密码校验复用 core/pw_hash（pbkdf2$ 新格式优先，遗留 sha256 兼容）。
    # config 的 admin 降级为「首次启动种子」（db/session.py ensure_sys_user_seed），
    # 仅当 sys_user **空表**时回落 config 直登（bootstrap 态：种子失败/全新库），
    # 表里有行后永不回落（单一事实源 = DB）。
    if _login_check_db(user, pw):
        tok = sign_token(user)
        response.set_cookie(
            _COOKIE, tok,
            httponly=True, samesite="lax",
            max_age=int(a.get("token_expire_minutes", 120)) * 60,
        )
        return {"ok": True}
    raise HTTPException(status_code=401, detail="invalid credentials")


def _login_check_db(user: str, pw: str) -> bool:
    """DB 优先的登录校验。惰性导入 db（保持本模块可被无 DB 环境单测导入）。

    - 命中 sys_user 行（status=1）→ verify_password 校验（双格式兼容）
    - sys_user 空表 → 回落 config admin 直登（bootstrap，打印醒目日志）
    - DB 异常 → 记日志返回 False（宁拒登不裸奔；种子/查库恢复后自愈）
    """
    try:
        from db.session import SessionLocal
        from db.models import SysUser
        from sqlalchemy import select, func
        db = SessionLocal()
        try:
            row = db.scalar(select(SysUser).where(
                SysUser.username == user, SysUser.status == 1))
            if row is not None:
                return _verify_pw(pw, row.password_hash or "",
                                  legacy_salt=(_cfg().get("password_salt") or ""))
            # SQLAlchemy 2.0：Select 无 .count()（Query 时代 API 已移除），走 func.count
            n = db.scalar(select(func.count()).select_from(SysUser)) or 0
            if n == 0:
                print("[auth] sys_user 空表 -> bootstrap 回落 config admin（重启后将按 "
                      "config 种子 super 用户；之后请走系统改密）", flush=True)
                a = _cfg()
                return (user == a.get("admin_user")
                        and _verify_pw(pw, a.get("admin_password_hash", ""),
                                       legacy_salt=(a.get("password_salt") or "")))
            return False
        finally:
            db.close()
    except Exception as e:
        print("[auth] login DB check failed: %s" % e, flush=True)
        return False


@router.post("/logout")
def logout(response: Response):
    response.set_cookie(_COOKIE, "", httponly=True, samesite="lax", max_age=0)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = get_current_admin(request)
    # M3：附带角色（前端按角色显隐用户管理/写按钮）。角色每请求现查 DB（token 不携带，
    # 改角色即时生效，无需等 token 过期）；查不到/异常回落 admin（与 authz._role_of 同口径）。
    role = "admin"
    try:
        from api.authz import _role_of
        from db.session import SessionLocal
        db = SessionLocal()
        try:
            role = _role_of(db, user)
        finally:
            db.close()
    except Exception:
        pass
    return {"user": user, "role": role}
