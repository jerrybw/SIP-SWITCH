"""T-301 管理端鉴权：账号密码登录 -> HMAC 自签 token -> HttpOnly cookie。

零第三方依赖（标准库 hmac / hashlib / base64 / json / time / secrets）。
- token 形如 base64url(header).base64url(payload).HMAC-SHA256(signing_input, jwt_secret)
- 密码不存明文：config 只存 password_salt + sha256(salt + password)
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


def _pw_hash(password: str) -> str:
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
    if user == a.get("admin_user") and _pw_hash(pw) == a.get("admin_password_hash"):
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
