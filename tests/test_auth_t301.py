"""T-301 管理端鉴权冒烟测试（纯函数 + FastAPI TestClient，无需数据库）。

覆盖：
- 密码哈希公式与 src/api/auth.py:_pw_hash 同源（sha256(salt+password)）。
- JWT 自签 token 的签发/校验往返；篡改签名或过期均判 401。
- 登录端点：正确凭据 200 + 下发 HttpOnly cookie；错误凭据 401。
- 受保护端点 /api/me：带 cookie 200、无 cookie 401。

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


def test_login_endpoint_ok_and_401():
    c = TestClient(app)
    ok = c.post("/api/login", json={"user": "admin", "password": _PW})
    assert ok.status_code == 200
    assert ok.cookies.get("sip_admin_sid")
    bad = c.post("/api/login", json={"user": "admin", "password": "wrong"})
    assert bad.status_code == 401


def test_me_requires_cookie():
    c = TestClient(app)
    assert c.get("/api/me").status_code == 401
    c.post("/api/login", json={"user": "admin", "password": _PW})
    me = c.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"] == "admin"
