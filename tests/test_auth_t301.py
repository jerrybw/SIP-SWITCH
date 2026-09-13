"""T-301 管理端鉴权冒烟测试（纯函数 + FastAPI TestClient，无需数据库）。

M3 用户管理 Phase 1 后的口径（2026-09-13 更新）：
- 真实登录已切 DB（api/auth._login_check_db：sys_user 优先、空表回落 config）。
  本模块无 MySQL，故端点级登录测试用桩替换 _login_check_db，只覆盖
  「凭据对→200+HttpOnly cookie / 凭据错→401」的响应面；
  _login_check_db 的 DB 命中/空表回落/异常拒登三分支见 tests/test_users_m3.py。
- 纯密码学部分（_pw_hash 公式、token 往返/篡改/过期）不依赖桩，真实逻辑。

注意：用 fixture 把 core.config.settings / api.auth.settings patch 成测试值，
**不改动全局字典**（避免污染其它测试模块），因此可在任意环境运行。
"""
import hashlib
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

import core.config as _cc
import api.auth as _auth_mod
from api.auth import router, sign_token, verify_token, _pw_hash

_SALT = "testsalt00000000000000000000"
_SECRET = "testjwtsecret0000000000000000"
_PW = "admin-pass-123"
_TEST_AUTH = {
    "admin_user": "admin",
    "password_salt": _SALT,
    "admin_password_hash": hashlib.sha256((_SALT + _PW).encode()).hexdigest(),
    "jwt_secret": _SECRET,
    "token_expire_minutes": 120,
}
# 与 config_settings.yaml 同形状的占位 dict（仅用于本模块，不写盘）
_CFG_DICT = {"esl": {}, "api": {}, "mysql": {}, "prepaid_enabled": False, "auth": _TEST_AUTH}


@pytest.fixture(autouse=True)
def _use_test_config():
    # patch 两个模块对 settings 的引用（api.auth 是 `from core.config import settings` 绑定到对象，
    # 必须连 api.auth.settings 一起 patch）；不改动原字典，避免污染 billing/migrate 等模块。
    with patch.object(_cc, "settings", _CFG_DICT), patch.object(_auth_mod, "settings", _CFG_DICT):
        yield


@pytest.fixture
def _login_stub(monkeypatch):
    """把 _login_check_db 替换为「config 直登」桩（M3 前的等价行为）。

    无 DB 环境下端点响应面测试用；真实 DB 优先逻辑的分支覆盖在
    tests/test_users_m3.py（同样无 DB 可跑）。
    """
    def _fake(user: str, pw: str) -> bool:
        a = _auth_mod.settings.get("auth") or {}
        from core.pw_hash import verify_password as _vp
        return (user == a.get("admin_user")
                and _vp(pw, a.get("admin_password_hash", ""),
                        legacy_salt=(a.get("password_salt") or "")))
    monkeypatch.setattr(_auth_mod, "_login_check_db", _fake)


app = FastAPI()
app.include_router(router)


def test_pw_hash_matches_sha256_salt_password():
    expect = hashlib.sha256((_SALT + _PW).encode()).hexdigest()
    assert _pw_hash(_PW) == expect


def test_token_roundtrip_and_tamper():
    tok = sign_token("admin")
    assert verify_token(tok) == "admin"
    h, p, s = tok.split(".")
    with pytest.raises(Exception):
        verify_token(f"{h}.{p}.deadbeef")
    # 过期 token 拒绝（exp 已过）
    import base64, json as _json
    def _b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    exp_payload = _b64u(_json.dumps({"sub": "admin", "exp": int(time.time()) - 10}).encode())
    header = tok.split(".")[0]
    import hmac as _hmac
    sig = _hmac.new(_SECRET.encode(), f"{header}.{exp_payload}".encode(), hashlib.sha256).hexdigest()
    with pytest.raises(Exception):
        verify_token(f"{header}.{exp_payload}.{sig}")


def test_login_endpoint_ok_and_401(_login_stub):
    c = TestClient(app)
    ok = c.post("/api/login", json={"user": "admin", "password": _PW})
    assert ok.status_code == 200
    assert ok.cookies.get("sip_admin_sid")
    bad = c.post("/api/login", json={"user": "admin", "password": "wrong"})
    assert bad.status_code == 401


def test_me_requires_cookie(_login_stub):
    c = TestClient(app)
    assert c.get("/api/me").status_code == 401
    c.post("/api/login", json={"user": "admin", "password": _PW})
    me = c.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"] == "admin"
