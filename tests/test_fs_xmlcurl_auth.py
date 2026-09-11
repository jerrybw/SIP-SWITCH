"""安全修复测试：/fs/* xml_curl 回调 HTTP Basic 认证 + 目录不再全量回显明文密码。

覆盖（对应 PR「xml_curl 认证 + 目录泄露」）：
- fs_auth.fs_basic_auth_ok：无凭据/错凭据/正确凭据/畸形 Base64/缺配置 fail-closed（纯函数，
  任意环境可跑）。
- app 中间件接线：/fs/dialplan /fs/directory /fs/config 无凭据 401（带 WWW-Authenticate），
  带凭据放行；/healthz 等原有白名单不受影响。
- 目录收敛：无 user 参数不再回显任何用户（历史版本回显全部话机+接入点明文密码）；
  带 user 时返回 a1-hash（md5(user:domain:password)）而非明文；domain 为空时回退明文参数。

app 级用例依赖 db.session 的启动期自迁移（import 链牵连 DB），按仓库惯例
（见 tests/test_migrate_idempotent.py / conftest.py）先探测 MySQL 可达性，
不可达则整块 skip —— CI（smoke workflow 带 MySQL service）会真实执行。
"""
import base64
import hashlib
import socket
from unittest.mock import patch

import pytest

import core.config as _cc
import api.fs_auth as _fs_auth_mod
from api.fs_auth import fs_basic_auth_ok


# ---------------- 纯函数部分（任意环境） ----------------

_FS_CREDS = {"xml_curl": {"user": "fsuser", "password": "fspass"}}


class _FakeReq:
    def __init__(self, auth_header):
        self.headers = {"authorization": auth_header} if auth_header else {}


def _basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


@pytest.fixture
def _creds_cfg():
    with patch.object(_fs_auth_mod, "settings", _FS_CREDS):
        yield


def test_fs_basic_auth_ok_accepts_correct(_creds_cfg):
    assert fs_basic_auth_ok(_FakeReq(_basic("fsuser", "fspass"))) is True


def test_fs_basic_auth_ok_rejects_wrong(_creds_cfg):
    assert fs_basic_auth_ok(_FakeReq(_basic("fsuser", "wrong"))) is False
    assert fs_basic_auth_ok(_FakeReq(_basic("wrong", "fspass"))) is False


def test_fs_basic_auth_ok_rejects_missing_or_malformed(_creds_cfg):
    assert fs_basic_auth_ok(_FakeReq(None)) is False
    assert fs_basic_auth_ok(_FakeReq("Bearer xyz")) is False
    assert fs_basic_auth_ok(_FakeReq("Basic !!!not-base64!!!")) is False
    # 合法 base64 但无冒号分隔
    assert fs_basic_auth_ok(_FakeReq("Basic " + base64.b64encode(b"nocolon").decode())) is False


def test_fs_basic_auth_fail_closed_without_config():
    """缺 [xml_curl] 配置时必须拒绝（fail-closed），不能裸奔。"""
    with patch.object(_fs_auth_mod, "settings", {"xml_curl": {"user": "", "password": ""}}):
        assert fs_basic_auth_ok(_FakeReq(_basic("fsuser", "fspass"))) is False
    with patch.object(_fs_auth_mod, "settings", {}):
        assert fs_basic_auth_ok(_FakeReq(_basic("fsuser", "fspass"))) is False


# ---------------- app 中间件接线（需 DB） ----------------

def _mysql_reachable() -> bool:
    """按 conftest 占位配置探测 MySQL（与 test_migrate_idempotent 同思路）。"""
    url = (_cc.settings.get("mysql") or {}).get("url") or ""
    # mysql+pymysql://user:pw@host:port/db
    try:
        hostport = url.split("@", 1)[1].split("/", 1)[0]
        host, port = hostport.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except Exception:
        return False


_db_ok = _mysql_reachable()

pytestmark_app = pytest.mark.skipif(not _db_ok, reason="需要可达的 MySQL（app 导入链含自迁移）")

if _db_ok:
    from datetime import datetime
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from api.app import app as full_app
    from db.session import SessionLocal
    from db.models import SipPhone, Account


@pytestmark_app
class TestFsAuthMiddleware:
    """中间件接线：/fs/* 全部 401（无/错凭据）→ 200（正确凭据）；白名单不受影响。"""

    @pytest.fixture
    def client(self):
        with patch.object(_fs_auth_mod, "settings", _FS_CREDS):
            yield TestClient(full_app)

    def test_fs_endpoints_401_without_creds(self, client):
        for p in ("/fs/dialplan", "/fs/directory", "/fs/config"):
            r = client.get(p)
            assert r.status_code == 401, p
            assert r.headers.get("www-authenticate") == "Basic"

    def test_fs_endpoints_401_wrong_creds(self, client):
        for p in ("/fs/dialplan", "/fs/directory", "/fs/config"):
            r = client.get(p, headers={"Authorization": _basic("fsuser", "nope")})
            assert r.status_code == 401, p

    def test_fs_endpoints_200_with_creds(self, client):
        hdrs = {"Authorization": _basic("fsuser", "fspass")}
        assert client.get("/fs/config", headers=hdrs).status_code == 200
        assert client.get("/fs/directory", headers=hdrs).status_code == 200
        # dialplan 裸请求（无 caller/callee）也应正常出 XML 而非 401/500
        r = client.get("/fs/dialplan", headers=hdrs)
        assert r.status_code == 200 and "freeswitch/xml" in r.text

    def test_healthz_still_open_and_api_still_guarded(self, client):
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/me").status_code == 401


@pytestmark_app
def test_user_xml_a1hash_and_fallback():
    """domain 非空 → a1-hash（不含明文）；domain 空 → 回退明文参数（保守兼容）。"""
    from api.directory_xml import _user_xml
    x = _user_xml("80000001", "pw123", "sip.test")
    expect = hashlib.md5(b"80000001:sip.test:pw123").hexdigest()
    assert 'name="a1-hash"' in x and expect in x
    assert "pw123" not in x
    y = _user_xml("80000001", "pw123", "")
    assert 'name="password" value="pw123"' in y and "a1-hash" not in y


@pytestmark_app
class TestDirectoryLeak:
    """目录泄露收敛：全量回显没了；单用户查询走 a1-hash。"""

    PHONE = "88889999"
    PASSWORD = "pw-plain-123"

    @pytest.fixture(autouse=True)
    def _seed_phone(self):
        db = SessionLocal()
        try:
            old = db.scalar(select(SipPhone).where(SipPhone.phone_number == self.PHONE))
            if old is not None:
                db.delete(old)
                db.commit()
            # sip_phone.account_id 在 DB 里 NOT NULL：借一个既有账户（CI 库里通常有种子），没有则造一个
            acct = db.scalar(select(Account).order_by(Account.id))
            if acct is None:
                acct = Account(name="pytest", account_number="8899", status=1,
                               created_at=datetime.utcnow(), updated_at=datetime.utcnow())
                db.add(acct)
                db.commit()
            db.add(SipPhone(phone_number=self.PHONE, password=self.PASSWORD,
                            enabled=1, status=1, account_id=acct.id))
            db.commit()
        finally:
            db.close()
        yield
        db = SessionLocal()
        try:
            old = db.scalar(select(SipPhone).where(SipPhone.phone_number == self.PHONE))
            if old is not None:
                db.delete(old)
                db.commit()
        finally:
            db.close()

    def _client(self):
        # 每个用例自建 client：patch 窗口即请求窗口，避免跨用例泄露 settings 补丁
        p = patch.object(_fs_auth_mod, "settings", _FS_CREDS)
        p.start()
        try:
            return TestClient(full_app), p
        except Exception:
            p.stop()
            raise

    def _get(self, c, qs):
        return c.get("/fs/directory?" + qs,
                     headers={"Authorization": _basic("fsuser", "fspass")})

    def test_no_full_dump_without_user(self):
        c, p = self._client()
        try:
            r = self._get(c, "")
            assert r.status_code == 200
            # 空 <users/>：已种的话机不得出现，明文密码更不得出现
            assert "<users></users>" in r.text
            assert self.PHONE not in r.text
            assert self.PASSWORD not in r.text
        finally:
            p.stop()

    def test_user_lookup_returns_a1hash_not_plaintext(self):
        c, p = self._client()
        try:
            r = self._get(c, f"user={self.PHONE}&domain=sip.test")
            assert r.status_code == 200
            expect = hashlib.md5(
                f"{self.PHONE}:sip.test:{self.PASSWORD}".encode()).hexdigest()
            assert 'name="a1-hash"' in r.text and expect in r.text
            assert self.PASSWORD not in r.text
            assert 'name="password"' not in r.text
        finally:
            p.stop()

    def test_unknown_user_returns_empty(self):
        c, p = self._client()
        try:
            r = self._get(c, "user=00000000&domain=sip.test")
            assert r.status_code == 200
            assert "<users></users>" in r.text
        finally:
            p.stop()
