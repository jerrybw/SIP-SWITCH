"""管理员口令哈希升级测试（PR: PBKDF2 版本化格式 + 登录比对恒时序）。

覆盖：
- hash_password 生成 pbkdf2$ 格式；同口令两次生成盐不同（独立盐）。
- verify_password：新格式往返 / 错口令拒绝 / 畸形 pbkdf2 串拒绝。
- 遗留兼容：sha256(salt+password) 存量格式仍可登录（平滑升级路径）。
- 登录端点：新格式与遗留格式都能 200；错口令 401。
- CLI 工具冒烟：tools/hash_password.py 输出可被 verify_password 校验。

与 test_auth_t301.py 同款 fixture：patch settings，不污染全局配置。
"""
import hashlib
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.config as _cc
import api.auth as _auth_mod
from api.auth import router, hash_password, verify_password

_SALT = "testsalt00000000000000000000"
_SECRET = "testjwtsecret0000000000000000"
_PW = "admin-pass-123"


def _legacy_hash(pw):
    return hashlib.sha256((_SALT + pw).encode()).hexdigest()


def _cfg_with(hash_value):
    return {"esl": {}, "api": {}, "mysql": {}, "prepaid_enabled": False,
            "auth": {"admin_user": "admin", "password_salt": _SALT,
                     "admin_password_hash": hash_value, "jwt_secret": _SECRET,
                     "token_expire_minutes": 120}}


app = FastAPI()
app.include_router(router)


def test_hash_password_format_and_unique_salt():
    h1, h2 = hash_password(_PW), hash_password(_PW)
    for h in (h1, h2):
        assert h.startswith("pbkdf2$")
        _, iters, salt_hex, dk_hex = h.split("$")
        assert int(iters) >= 100_000
        assert len(salt_hex) == 32      # 16 字节盐
        assert len(dk_hex) == 64        # 32 字节 DK
    assert h1 != h2                     # 独立盐：两次生成不相等


def test_verify_roundtrip_new_format():
    h = hash_password(_PW)
    assert verify_password(_PW, h) is True
    assert verify_password("wrong", h) is False


def test_verify_rejects_malformed():
    with patch.object(_auth_mod, "settings", _cfg_with(hash_password(_PW))):
        assert verify_password(_PW, "pbkdf2$not$hex$$") is False
        assert verify_password(_PW, "pbkdf2$abc$zz$zz") is False
        assert verify_password(_PW, "") is False


def test_verify_legacy_sha256_still_works():
    """存量部署平滑升级：旧 sha256(salt+password) 哈希仍可登录。"""
    with patch.object(_auth_mod, "settings", _cfg_with(_legacy_hash(_PW))):
        assert verify_password(_PW, _legacy_hash(_PW)) is True
        assert verify_password("wrong", _legacy_hash(_PW)) is False


def test_login_new_format_200():
    cfg = _cfg_with(hash_password(_PW))
    with patch.object(_cc, "settings", cfg), patch.object(_auth_mod, "settings", cfg):
        c = TestClient(app)
        r = c.post("/api/login", json={"user": "admin", "password": _PW})
        assert r.status_code == 200 and r.cookies.get("sip_admin_sid")


def test_login_legacy_format_200():
    cfg = _cfg_with(_legacy_hash(_PW))
    with patch.object(_cc, "settings", cfg), patch.object(_auth_mod, "settings", cfg):
        c = TestClient(app)
        r = c.post("/api/login", json={"user": "admin", "password": _PW})
        assert r.status_code == 200
        assert c.post("/api/login", json={"user": "admin", "password": "bad"}).status_code == 401


def test_hash_password_cli_smoke():
    """CLI 生成的哈希能被 verify_password 校验（新格式闭环）。"""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "tools/hash_password.py", _PW],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    assert out.returncode == 0, out.stderr
    h = out.stdout.strip()
    assert h.startswith("pbkdf2$")
    cfg = _cfg_with(h)
    with patch.object(_auth_mod, "settings", cfg):
        assert verify_password(_PW, h) is True
