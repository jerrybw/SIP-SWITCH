"""M3 用户管理 Phase 1 测试（T-301 · 64ef74a 拍板范围）。

三个模块的**无 DB 面**（不起 MySQL/Redis 即可跑；真库集成走容器化测试/E2E）：
1. api/auth._login_check_db —— DB 优先登录三分支（命中/空表回落/异常拒登），
   SessionLocal 桩到内存 SQLite。
2. api/users —— CRUD 业务守卫（最后 super 不可降级/停删、不可自停自删、
   入参校验）。真 ORM 路径（内存 SQLite）+ 真 require_role（get_current_admin
   桩成固定身份）。require_role 是闭包工厂、dependency_overrides 按对象匹配
   拦不住它，故桩更底层的 get_current_admin，鉴权链其余全真。
3. api/authz —— require_role 403 矩阵 + write_guard viewer 只读（TestClient，
   真登录 cookie + 真 token 校验）。

⚠️ 导入链陷阱：db/models.py 顶层 `from db.session import Base`，而真实
db.session **模块级连 MySQL 跑自迁移**，无 DB 环境导入即炸。故本文件在
sys.modules 里先用**桩 db.session**（仅 Base + get_db，无任何迁移副作用）
顶替，再导入 models/authz/users。SQLite 建表与 ORM 路径完全真实。
"""
import sys
import types
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, "src")

# SQLite 测 MySQL 模型的经典坑：BigInteger 主键在 SQLite 无 rowid 别名 -> 不自增。
# dialect 级编译覆写：BigInteger 在 SQLite 上渲染为 INTEGER（知名配方，仅测试进程内生效）。
@compiles(BigInteger, "sqlite")
def _bigint_as_integer_sqlite(type_, compiler, **kw):
    return "INTEGER"

# --- 桩 db.session：先占坑，阻断真实模块的 MySQL 连接/迁移副作用 -----------------
if "db.session" not in sys.modules or not getattr(sys.modules["db.session"], "_STUB", False):
    _stub = types.ModuleType("db.session")
    _stub._STUB = True

    class _StubBase(DeclarativeBase):
        pass

    _stub.Base = _StubBase
    _stub.SessionLocal = None       # 测试内按需替换
    _stub.get_db = None            # 仅占位（TestClient 用 dependency_overrides 改绑）
    sys.modules["db.session"] = _stub

if "db" not in sys.modules:
    import db  # noqa: F401,E402

import core.config as _cc  # noqa: E402
import api.auth as _auth_mod  # noqa: E402
import api.authz as _authz_mod  # noqa: E402
import db.session as _ds_mod  # noqa: E402
from db.models import SysUser  # noqa: E402
from api.users import (router as users_router, _user_out, update_user,  # noqa: E402
                       UserUpdate)
from api.authz import require_role, write_guard_middleware, _role_of  # noqa: E402

_Base = sys.modules["db.session"].Base  # models 顶层绑定的就是它

_SALT = "testsalt00000000000000000000"
_SECRET = "testjwtsecret0000000000000000"

_TEST_AUTH = {"admin_user": "admin", "password_salt": _SALT,
              "admin_password_hash": "", "jwt_secret": _SECRET,
              "token_expire_minutes": 120}


@contextmanager
def _auth_settings():
    """把 core.config / api.auth 的 settings 换成带 jwt_secret 的测试配置。"""
    cfg = {"esl": {}, "api": {},
           "mysql": {"url": "sqlite://"},
           "prepaid_enabled": False, "auth": _TEST_AUTH}
    with patch.object(_cc, "settings", cfg), patch.object(_auth_mod, "settings", cfg):
        yield


# ---------------------------------------------------------------------------
# 内存 SQLite：每个测试函数独立库（防串扰），全部表建齐
# ---------------------------------------------------------------------------

@pytest.fixture
def db_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    _Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    S = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    s = S()
    yield s
    s.close()


def _mk_user(sess, username, role=1, status=1, pw="pass-word-123"):
    from core.pw_hash import hash_password
    u = SysUser(username=username, password_hash=hash_password(pw),
                role=role, status=status, created_at=datetime.utcnow())
    sess.add(u)
    sess.commit()
    sess.refresh(u)
    return u


# ===========================================================================
# 1. _login_check_db 三分支（DB 优先登录）
# ===========================================================================

def _login_check(sess, user, pw):
    with _auth_settings(), patch("db.session.SessionLocal", lambda: sess):
        return _auth_mod._login_check_db(user, pw)


def test_login_db_hit_enabled_user(db_session):
    """命中启用行 -> verify_password 校验（含错口令拒登）。"""
    _mk_user(db_session, "alice", role=1)
    assert _login_check(db_session, "alice", "pass-word-123") is True
    assert _login_check(db_session, "alice", "wrong-password") is False


def test_login_db_disabled_user_rejected(db_session):
    """status=0 的行不命中 -> 直接拒登（不回落 config，防停用账号经 config 复活）。"""
    _mk_user(db_session, "bob", status=0)
    assert _login_check(db_session, "bob", "pass-word-123") is False


def test_login_db_empty_table_falls_back_config(db_session):
    """空表 -> bootstrap 回落 config admin 直登（pbkdf2 新格式）。"""
    from core.pw_hash import hash_password
    _TEST_AUTH["admin_password_hash"] = hash_password("boot-strap-99")
    assert _login_check(db_session, "admin", "boot-strap-99") is True
    assert _login_check(db_session, "admin", "nope") is False


def test_login_db_nonempty_table_never_falls_back(db_session):
    """表里有行 -> 未知用户直接拒登（单一事实源 = DB，config 不再是后门）。"""
    _mk_user(db_session, "alice")
    assert _login_check(db_session, "someone-else", "whatever-pass") is False


def test_login_db_error_fails_closed(db_session):
    """DB 异常 -> 返回 False（宁拒登不裸奔；种子/查库恢复后自愈）。"""
    class _Boom:
        def scalar(self, *a, **k):
            raise RuntimeError("db down")
        def close(self):
            pass
    with _auth_settings(), patch("db.session.SessionLocal", _Boom):
        assert _auth_mod._login_check_db("admin", "x") is False


# ===========================================================================
# 2. users.py CRUD 守卫（真 require_role；get_current_admin 桩成固定 super 身份）
# ===========================================================================

@pytest.fixture
def super_actor(db_session, monkeypatch):
    """actor = root-super（DB 里真实存在的 super）。

    require_role 是闭包工厂、每次调用产生新函数对象，dependency_overrides
    按对象匹配拦不住它 —— 桩更底层的 get_current_admin（require_role 内部
    实际调用的鉴权点），角色裁决 _role_of 走真 DB。
    """
    _mk_user(db_session, "root-super", role=2)
    monkeypatch.setattr(_authz_mod, "get_current_admin",
                        lambda request: "root-super")


def _users_app(sess):
    app = FastAPI()
    app.dependency_overrides[_ds_mod.get_db] = lambda: sess
    app.include_router(users_router)
    return app


def _client(sess):
    return TestClient(_users_app(sess))


def test_create_user_validations(db_session, super_actor):
    c = _client(db_session)
    # 口令过短 / 非法角色 / 空用户名 / 用户名重复
    assert c.post("/api/users", json={"username": "x", "password": "short", "role": 1}).status_code == 400
    assert c.post("/api/users", json={"username": "x", "password": "long-enough-99", "role": 9}).status_code == 400
    assert c.post("/api/users", json={"username": "", "password": "long-enough-99", "role": 1}).status_code == 400
    r = c.post("/api/users", json={"username": "eve", "password": "long-enough-99", "role": 0})
    assert r.status_code == 201 and r.json()["role_name"] == "viewer"
    assert c.post("/api/users", json={"username": "eve", "password": "long-enough-99", "role": 1}).status_code == 400


def test_update_last_super_guards_self(db_session, super_actor):
    """唯一 super 是自己：降级/停用均 400（自我守卫与最后 super 守卫双命中）。"""
    su = db_session.scalar(select(SysUser).where(SysUser.username == "root-super"))
    c = _client(db_session)
    assert c.put(f"/api/users/{su.id}", json={"role": 1}).status_code == 400
    assert c.put(f"/api/users/{su.id}", json={"status": 0}).status_code == 400


def test_update_last_super_guard_other(db_session):
    """直调端点：target 是最后一个启用 super、actor 是别的 super ->
    last-super 守卫 400（TestClient 路径下 actor 必在 DB，触发不到此分支，故直调）。"""
    su = _mk_user(db_session, "solo-super", role=2)
    with pytest.raises(HTTPException) as e:
        update_user(su.id, UserUpdate(role=1), db=db_session,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        update_user(su.id, UserUpdate(status=0), db=db_session,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400


def test_update_two_supers_one_may_pause(db_session, super_actor):
    """两个启用 super：不可停/降自己；可停别人（仍留一个）。"""
    su = db_session.scalar(select(SysUser).where(SysUser.username == "root-super"))
    _mk_user(db_session, "backup-super", role=2)
    c = _client(db_session)
    assert c.put(f"/api/users/{su.id}", json={"status": 0}).status_code == 400  # 自己
    b = db_session.scalar(select(SysUser).where(SysUser.username == "backup-super"))
    assert c.put(f"/api/users/{b.id}", json={"status": 0}).status_code == 200   # 停别人
    # backup-super 已停用 -> 再降级它不触发 last-super 守卫（启用 super 仍有一个）
    assert c.put(f"/api/users/{b.id}", json={"role": 1}).status_code == 200


def test_delete_guards(db_session, super_actor):
    """删自己 400；删最后一个启用 super 400（此处同一人）；删普通用户 200。"""
    su = db_session.scalar(select(SysUser).where(SysUser.username == "root-super"))
    c = _client(db_session)
    assert c.delete(f"/api/users/{su.id}").status_code == 400  # 删自己（也是最后 super）
    u = _mk_user(db_session, "norm")
    assert c.delete(f"/api/users/{u.id}").status_code == 200


def test_reset_password_flow(db_session, super_actor):
    u = _mk_user(db_session, "alice")
    c = _client(db_session)
    assert c.post(f"/api/users/{u.id}/reset-password",
                  json={"password": "tiny"}).status_code == 400
    assert c.post(f"/api/users/{u.id}/reset-password",
                  json={"password": "new-pass-456"}).status_code == 200
    from core.pw_hash import verify_password
    row = db_session.get(SysUser, u.id)
    assert verify_password("new-pass-456", row.password_hash) is True
    assert verify_password("pass-word-123", row.password_hash) is False


def test_change_own_password_requires_old(db_session, super_actor):
    c = _client(db_session)
    assert c.post("/api/users/me/password",
                  json={"old_password": "bad-old", "new_password": "new-pass-456"}).status_code == 401
    r = c.post("/api/users/me/password",
               json={"old_password": "pass-word-123", "new_password": "new-pass-456"})
    assert r.status_code == 200
    row = db_session.scalar(select(SysUser).where(SysUser.username == "root-super"))
    from core.pw_hash import verify_password
    assert verify_password("new-pass-456", row.password_hash) is True


def test_user_out_shape(db_session):
    u = _mk_user(db_session, "carol", role=0, status=1)
    d = _user_out(u)
    assert d == {"id": u.id, "username": "carol", "role": 0, "role_name": "viewer",
                 "status": 1, "created_at": u.created_at}


# ===========================================================================
# 3. authz：require_role 403 矩阵 + write_guard viewer 只读（真 cookie 鉴权）
# ===========================================================================

def _guard_app(sess):
    app = FastAPI()
    app.dependency_overrides[_ds_mod.get_db] = lambda: sess
    app.middleware("http")(write_guard_middleware)
    app.include_router(users_router)
    return app


def _guard_client(sess, monkeypatch):
    monkeypatch.setattr(_ds_mod, "SessionLocal", lambda: sess)
    monkeypatch.setattr(_authz_mod, "SessionLocal", lambda: sess)
    return TestClient(_guard_app(sess))


def _ck(user):
    """真 token cookie（走 sign_token + _TEST_AUTH jwt_secret）。"""
    return {"sip_admin_sid": _auth_mod.sign_token(user)}


def test_viewer_write_forbidden(db_session, monkeypatch):
    """viewer：业务写 POST /api/users -> 403（write_guard 全站只读）。"""
    _mk_user(db_session, "vicky", role=0)
    c = _guard_client(db_session, monkeypatch)
    with _auth_settings():
        r = c.post("/api/users", headers=_ck("vicky"),
                   json={"username": "nn", "password": "long-enough-99", "role": 1})
    assert r.status_code == 403
    assert "viewer" in r.json()["detail"]


def test_viewer_can_change_own_password(db_session, monkeypatch):
    """viewer 改自己密码放行（只读指业务数据，不含自身凭据；旧口令校验兜底）。"""
    _mk_user(db_session, "vicky", role=0)
    c = _guard_client(db_session, monkeypatch)
    with _auth_settings():
        r = c.post("/api/users/me/password", headers=_ck("vicky"),
                   json={"old_password": "pass-word-123", "new_password": "new-pass-456"})
    assert r.status_code == 200


def test_role_matrix_admin_vs_super(db_session, monkeypatch):
    """用户管理整页 super-only：admin 列表 403（回落 admin 同样拒），super 200。"""
    _mk_user(db_session, "adam", role=1)
    _mk_user(db_session, "sue", role=2)
    c = _guard_client(db_session, monkeypatch)
    with _auth_settings():
        assert c.get("/api/users", headers=_ck("adam")).status_code == 403
        r = c.get("/api/users", headers=_ck("sue"))
    assert r.status_code == 200 and "items" in r.json()


def test_unauthenticated_write_returns_401(db_session, monkeypatch):
    """未登录写请求不被 write_guard 吞成 403，而是放行给下游 _auth_guard 回 401。"""
    c = _guard_client(db_session, monkeypatch)
    with _auth_settings():
        r = c.post("/api/users", json={"username": "x", "password": "long-enough-99", "role": 1})
    assert r.status_code == 401


def test_role_of_fallbacks(db_session):
    """用户缺失/查询异常回落 admin（fail-open 口径，业务可用性优先）。"""
    assert _role_of(db_session, "ghost") == "admin"

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
        def close(self):
            pass
    assert _role_of(_Boom(), "anyone") == "admin"


def test_role_names_contract():
    """三档口径契约（migrate 注释 / 前端角色名映射都依赖它，防漂移）。"""
    from api.authz import ROLE_NAMES
    assert ROLE_NAMES == {0: "viewer", 1: "admin", 2: "super"}
