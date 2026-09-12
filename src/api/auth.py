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
    # 校验统一走 verify_password（新格式优先、兼容遗留格式），比对恒时序
    if user == a.get("admin_user") and verify_password(pw, a.get("admin_password_hash", "")):
        tok = sign_token(user)
        response.set_cookie(
            _COOKIE, tok,
            httponly=True, samesite="lax",
            max_age=int(a.get("token_expire_minutes", 120)) * 60,
        )
        return {"ok": True}
    raise HTTPException(status_code=401, detail="invalid credentials")


@router.post("/logout")
def logout(response: Response):
    response.set_cookie(_COOKIE, "", httponly=True, samesite="lax", max_age=0)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    return {"user": get_current_admin(request)}
