"""M3 Phase 2「自定义角色 + 权限矩阵」测试（T-301 深化 · 2026-09-14 实施方案 §6 A-G）。

与 test_users_m3.py 同一套环境口径（文件头注释必读）：
- 无 DB 环境可跑：db.session 桩（真实模块优先）+ 内存 SQLite + 真 ORM 路径；
- py3.10 不走 TestClient 网络层 —— 直调端点函数 / 中间件；
- E 组（migrate 幂等，需 MySQL）按既有口径容器内跑，本地不写。

覆盖（实施方案 §6）：
  A 角色判定 _role_of：role_code 优先 / 缺失回落 int / 禁用回落 admin / 未知回落 admin
  B perm_guard 矩阵：自定义只读写→403 / none 读→403 / other 兜底（内置 write、自定义 none）
    / 豁免路径不受影响 / 未登录写放行给下游 401
  C 内置等价回归：BUILTIN_FALLBACK 14 feature × 3 角色与实施方案 §1 矩阵逐格断言
  D 角色管理守卫（roles.py）：builtin 不可删改 / 删被引用 400 / 停被引用 400 /
    users-system 恒 none 400 / 新角色默认全 none / code 校验与保留字
  F 兼容：仅 int 角色用户（role_code NULL）行为与 Phase 1 一致
  G me/perms：_effective_perms 内置不查库 / 自定义缺行补 none / DB 异常 None
"""
import sys
import types
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import BigInteger

sys.path.insert(0, "src")

@compiles(BigInteger, "sqlite")
def _bigint_as_integer_sqlite(type_, compiler, **kw):
    return "INTEGER"

if "db.session" not in sys.modules:
    try:
        import db.session  # noqa: F401,E402
    except Exception:
        _stub = types.ModuleType("db.session")
        _stub._STUB = True

        class _StubBase:
            pass

        from sqlalchemy.orm import DeclarativeBase
        class _StubBase(DeclarativeBase):
            pass
        _stub.Base = _StubBase
        _stub.SessionLocal = None
        _stub.get_db = None
        sys.modules["db.session"] = _stub

if "db" not in sys.modules:
    import db  # noqa: F401,E402

from db.models import Role, RolePerm, SysUser  # noqa: E402
import api.authz as _authz_mod  # noqa: E402
import db.session as _ds_mod  # noqa: E402
from api.roles import (create_role, update_role, delete_role,  # noqa: E402
                       list_roles, role_options, RoleCreate, RoleUpdate,
                       _validate_perms, FEATURES)

_Meta = SysUser.__table__.metadata


# ---------------------------------------------------------------------------
# 库与夹具（口径同 test_users_m3）
# ---------------------------------------------------------------------------

@pytest.fixture
def db_engine():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    _Meta.create_all(engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    S = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    s = S()
    yield s
    s.close()


def _mk_user(sess, username, role=1, status=1, role_code=None,
             pw="pass-word-123"):
    from core.pw_hash import hash_password
    u = SysUser(username=username, password_hash=hash_password(pw),
                role=role, role_code=role_code, status=status,
                created_at=datetime.utcnow())
    sess.add(u)
    sess.commit()
    sess.refresh(u)
    return u


def _mk_role(sess, code, name=None, builtin=0, enabled=1, perms=None):
    r = Role(code=code, name=name or code, builtin=builtin, enabled=enabled,
             sort=0 if builtin else 100, created_at=datetime.utcnow())
    sess.add(r)
    for f, p in (perms or {}).items():
        sess.add(RolePerm(role_code=code, feature=f, perm=p))
    sess.commit()
    sess.refresh(r)
    return r


_CUR_SESSION = {"db": None}


class _FakeRequest:
    def __init__(self, method="GET", path="/api/users", cookies=None):
        self.method = method
        self.url = types.SimpleNamespace(path=path)
        self.cookies = cookies or {}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body


class _Next:
    """call_next 桩：记录放行请求，返回 200。"""
    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request.url.path)
        return _Resp(200, {"ok": True})


def _as(monkeypatch, username="root-super", sess=None):
    monkeypatch.setattr(_authz_mod, "get_current_admin", lambda r: username)
    s = sess or _CUR_SESSION["db"]
    # authz.py 是 from db.session import SessionLocal —— 模块级名字要双点替换
    monkeypatch.setattr(_ds_mod, "SessionLocal", lambda: s)
    monkeypatch.setattr(_authz_mod, "SessionLocal", lambda: s)


# ===========================================================================
# A. _role_of 角色判定
# ===========================================================================

def test_role_of_role_code_priority(db_session):
    """role_code 非空优先于 int 列（int 记 admin 兜底口径也不影响判定）。"""
    _mk_role(db_session, "ops_ro", enabled=1)
    _mk_user(db_session, "carol", role=1, role_code="ops_ro")
    assert _authz_mod._role_of(db_session, "carol") == "ops_ro"


def test_role_of_int_fallback_when_null(db_session):
    """role_code NULL -> int 三档（Phase 1 行为，老用户零感知）。"""
    _mk_user(db_session, "dan", role=0)
    assert _authz_mod._role_of(db_session, "dan") == "viewer"
    _mk_user(db_session, "erin", role=2)
    assert _authz_mod._role_of(db_session, "erin") == "super"


def test_role_of_builtin_code_trusted_without_row(db_session):
    """内置 code 不查 roles 表直接信任（种子失败不破防）。"""
    _mk_user(db_session, "frank", role=1, role_code="viewer")
    assert _authz_mod._role_of(db_session, "frank") == "viewer"


def test_role_of_disabled_custom_falls_back_admin(db_session):
    """自定义角色被禁用 -> 回落 admin（用户不被锁死；roles 页停用须先转移用户）。"""
    _mk_role(db_session, "gone", enabled=0)
    _mk_user(db_session, "grace", role=1, role_code="gone")
    assert _authz_mod._role_of(db_session, "grace") == "admin"


def test_role_of_unknown_code_falls_back_admin(db_session):
    """role_code 指向不存在的角色 -> 回落 admin。"""
    _mk_user(db_session, "heidi", role=1, role_code="ghost")
    assert _authz_mod._role_of(db_session, "heidi") == "admin"


def test_role_of_missing_user_admin(db_session):
    assert _authz_mod._role_of(db_session, "nobody") == "admin"


# ===========================================================================
# B. perm_guard（write_guard_middleware 直调）
# ===========================================================================

def _guard(monkeypatch, sess, method, path, username="root-super"):
    _as(monkeypatch, username, sess)
    req = _FakeRequest(method=method, path=path)
    nxt = _Next()

    async def run():
        return await _authz_mod.write_guard_middleware(req, nxt)

    import asyncio
    return asyncio.new_event_loop().run_until_complete(run()), nxt


def test_guard_custom_readonly_write_403(db_session, monkeypatch):
    """自定义只读角色写业务 feature -> 403（判据 #1）。"""
    _mk_user(db_session, "root-super", role=2)
    _mk_role(db_session, "ops_ro", perms={"gateways": "read"})
    _mk_user(db_session, "ivan", role=1, role_code="ops_ro")
    resp, nxt = _guard(monkeypatch, db_session, "POST", "/api/gateways", "ivan")
    assert resp.status_code == 403
    assert not nxt.calls


def test_guard_custom_read_get_ok(db_session, monkeypatch):
    _mk_user(db_session, "root-super", role=2)
    _mk_role(db_session, "ops_ro", perms={"gateways": "read"})
    _mk_user(db_session, "ivan", role=1, role_code="ops_ro")
    resp, nxt = _guard(monkeypatch, db_session, "GET", "/api/gateways", "ivan")
    assert resp.status_code == 200 and nxt.calls == ["/api/gateways"]


def test_guard_custom_none_feature_403(db_session, monkeypatch):
    """矩阵 none 的 feature 读也 403（fail-closed；缺行=none 同口径）。"""
    _mk_user(db_session, "root-super", role=2)
    _mk_role(db_session, "ops_gw_only", perms={"gateways": "write"})
    _mk_user(db_session, "judy", role=1, role_code="ops_gw_only")
    resp, _ = _guard(monkeypatch, db_session, "GET", "/api/users", "judy")
    assert resp.status_code == 403
    resp, _ = _guard(monkeypatch, db_session, "GET", "/api/accounts", "judy")
    assert resp.status_code == 403


def test_guard_other_builtin_write_custom_none(db_session, monkeypatch):
    """未映射路径 other：内置=write（Phase 1 零回归）/ 自定义=none（fail-closed）。"""
    _mk_user(db_session, "root-super", role=2)
    _mk_role(db_session, "ops_any", perms={"gateways": "read"})
    _mk_user(db_session, "mallory", role=1, role_code="ops_any")
    resp, nxt = _guard(monkeypatch, db_session, "GET", "/api/some-unknown", "mallory")
    assert resp.status_code == 403                      # 自定义 other=none
    resp, nxt = _guard(monkeypatch, db_session, "GET", "/api/some-unknown", "root-super")
    assert resp.status_code == 200 and nxt.calls        # 内置 other 放行


def test_guard_exempt_paths_pass(db_session, monkeypatch):
    """豁免路径不受矩阵影响：login/me 放行；viewer 改自己密码（POST）放行。"""
    _mk_user(db_session, "root-super", role=2)
    _mk_user(db_session, "nia", role=0)   # viewer
    resp, nxt = _guard(monkeypatch, db_session, "POST", "/api/users/me/password", "nia")
    assert resp.status_code == 200
    resp, nxt = _guard(monkeypatch, db_session, "GET", "/api/me", "nia")
    assert resp.status_code == 200
    resp, nxt = _guard(monkeypatch, db_session, "POST", "/api/login", "nia")
    assert resp.status_code == 200


def test_guard_unauthenticated_write_passes_through(db_session, monkeypatch):
    """未登录写请求放行给下游 _auth_guard 统一 401（职责分离，不吞不改判）。"""
    _mk_user(db_session, "root-super", role=2)
    monkeypatch.setattr(_authz_mod, "get_current_admin",
                        lambda r: (_ for _ in ()).throw(HTTPException(401)))
    req = _FakeRequest(method="DELETE", path="/api/gateways")
    nxt = _Next()

    async def run():
        return await _authz_mod.write_guard_middleware(req, nxt)

    import asyncio
    resp = asyncio.new_event_loop().run_until_complete(run())
    assert resp.status_code == 200 and nxt.calls == ["/api/gateways"]  # 未拦=下游判


def test_guard_super_users_ok_admin_403(db_session, monkeypatch):
    """users feature：super 全通 / admin none 403（判据 #1）。"""
    _mk_user(db_session, "root-super", role=2)
    _mk_user(db_session, "oscar", role=1)
    resp, _ = _guard(monkeypatch, db_session, "GET", "/api/users", "oscar")
    assert resp.status_code == 403
    resp, nxt = _guard(monkeypatch, db_session, "GET", "/api/users", "root-super")
    assert resp.status_code == 200 and nxt.calls


# ===========================================================================
# C. 内置等价回归（14 × 3 逐格，与实施方案 §1 矩阵对齐）
# ===========================================================================

_EXPECTED_BUILTIN = {
    "viewer": {"access-points": "read", "gateways": "read", "routes": "read",
               "rules": "read", "sip-phones": "read", "carriers": "read",
               "accounts": "read", "billing": "read", "cdr": "read",
               "cdr.export": "none", "nodes": "read", "users": "none",
               "oplogs": "read", "system": "read"},
    "admin": {"access-points": "write", "gateways": "write", "routes": "write",
              "rules": "write", "sip-phones": "write", "carriers": "write",
              "accounts": "write", "billing": "write", "cdr": "read",
              "cdr.export": "write", "nodes": "read", "users": "none",
              "oplogs": "read", "system": "read"},
    "super": {"access-points": "write", "gateways": "write", "routes": "write",
              "rules": "write", "sip-phones": "write", "carriers": "write",
              "accounts": "write", "billing": "write", "cdr": "write",
              "cdr.export": "write", "nodes": "write", "users": "write",
              "oplogs": "read", "system": "write"},
}


def test_builtin_fallback_matches_design_matrix():
    """BUILTIN_FALLBACK 与实施方案 §1 定稿矩阵逐格一致（含收紧拍板 P2/P3/P4：
    nodes/system admin=read、cdr.export viewer=none）。"""
    for role, matrix in _EXPECTED_BUILTIN.items():
        for feat, want in matrix.items():
            assert _authz_mod.BUILTIN_FALLBACK[role][feat] == want, \
                "%s.%s" % (role, feat)


def test_builtin_perm_of_no_rows(db_session):
    """_perm_of：内置角色不走 DB（恒 BUILTIN_FALLBACK），空表不破防。"""
    assert _authz_mod._perm_of(db_session, "admin", "system", True) == "read"
    assert _authz_mod._perm_of(db_session, "super", "system", True) == "write"
    assert _authz_mod._perm_of(db_session, "viewer", "cdr.export", False) == "none"


def test_custom_missing_feature_none(db_session):
    """自定义角色缺 feature 行 = none（守卫 5，fail-closed）。"""
    _mk_role(db_session, "ops_partial", perms={"gateways": "read"})
    assert _authz_mod._perm_of(db_session, "ops_partial", "rules", False) == "none"
    assert _authz_mod._perm_of(db_session, "ops_partial", "other", True) == "none"


# ===========================================================================
# D. roles.py 守卫（直调端点）
# ===========================================================================

@pytest.fixture
def super_env(db_session, monkeypatch):
    _mk_user(db_session, "root-super", role=2)
    _as(monkeypatch, "root-super", db_session)
    yield db_session


def test_create_role_defaults_all_none(super_env):
    """新角色默认全 none（守卫 3）+ 全量 14 行落库。"""
    r = create_role(RoleCreate(code="ops_ro", name="运维只读", perms={}), db=super_env)
    assert r["code"] == "ops_ro" and r["builtin"] == 0
    perms = {p.feature: p.perm for p in super_env.scalars(
        select(RolePerm).where(RolePerm.role_code == "ops_ro")).all()}
    assert perms == {f: "none" for f in FEATURES}


def test_create_role_full_matrix(super_env):
    r = create_role(RoleCreate(code="ops_gw", name="网关运维",
                               perms={"gateways": "write", "routes": "read"}),
                    db=super_env)
    assert r["perms"]["gateways"] == "write"
    assert r["perms"]["routes"] == "read"
    assert r["perms"]["cdr"] == "none"      # 未给的 feature 补 none


def test_create_role_validations(super_env):
    """code 格式 / 保留字 / 重复 / users-system 拒绝（守卫 4）。"""
    for bad in ("Admin", "1abc", "a", "x" * 40, "viewer", "admin", "super"):
        with pytest.raises(HTTPException) as e:
            create_role(RoleCreate(code=bad, name="x", perms={}), db=super_env)
        assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:   # users/system 显式给值 -> 400
        create_role(RoleCreate(code="ops_x", name="x",
                               perms={"users": "write"}), db=super_env)
    assert e.value.status_code == 400
    create_role(RoleCreate(code="ops_ok", name="ok", perms={}), db=super_env)
    with pytest.raises(HTTPException) as e:  # code 冲突
        create_role(RoleCreate(code="ops_ok", name="dup", perms={}), db=super_env)
    assert e.value.status_code == 400


def test_update_builtin_immutable(super_env):
    """内置三档改名/改矩阵/停用全 400（守卫 1：矩阵真源=代码）。"""
    _mk_role(super_env, "viewer", builtin=1, enabled=1)
    with pytest.raises(HTTPException) as e:
        update_role("viewer", RoleUpdate(name="新名"), db=super_env)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        update_role("viewer", RoleUpdate(enabled=0), db=super_env)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        update_role("viewer", RoleUpdate(perms={"cdr": "write"}), db=super_env)
    assert e.value.status_code == 400


def test_delete_builtin_400(super_env):
    _mk_role(super_env, "admin", builtin=1)
    with pytest.raises(HTTPException) as e:
        delete_role("admin", db=super_env)
    assert e.value.status_code == 400


def test_delete_referenced_role_400(super_env):
    """删被引用角色 400，先转移用户（守卫 2）。"""
    _mk_role(super_env, "ops_ro")
    _mk_user(super_env, "paul", role=1, role_code="ops_ro")
    with pytest.raises(HTTPException) as e:
        delete_role("ops_ro", db=super_env)
    assert e.value.status_code == 400
    assert "move them first" in e.value.detail


def test_disable_referenced_role_400(super_env):
    """停用被引用角色同口径 400（停用=隐性权限变更，须先转移）。"""
    _mk_role(super_env, "ops_ro")
    _mk_user(super_env, "quirk", role=1, role_code="ops_ro")
    with pytest.raises(HTTPException) as e:
        update_role("ops_ro", RoleUpdate(enabled=0), db=super_env)
    assert e.value.status_code == 400


def test_update_role_perms_take_effect(super_env):
    """改 perms 整组覆盖 + 下一请求即生效（守卫 5：authz 零缓存，直查库证）。"""
    _mk_role(super_env, "ops_ro", perms={"gateways": "read"})
    r = update_role("ops_ro", RoleUpdate(perms={"accounts": "write"}), db=super_env)
    assert r["perms"]["accounts"] == "write"
    assert r["perms"]["gateways"] == "none"    # 整组覆盖：未给的回落 none
    assert _authz_mod._perm_of(super_env, "ops_ro", "accounts", True) == "write"


def test_delete_role_cleans_perms(super_env):
    _mk_role(super_env, "ops_tmp", perms={"gateways": "read"})
    r = delete_role("ops_tmp", db=super_env)
    assert r["ok"] is True
    assert super_env.scalars(select(RolePerm)
        .where(RolePerm.role_code == "ops_tmp")).all() == []


def test_list_and_options(super_env):
    _mk_role(super_env, "viewer", builtin=1, enabled=1)
    _mk_role(super_env, "ops_ro", perms={"gateways": "read"})
    _mk_role(super_env, "ops_off", enabled=0)
    lst = list_roles(db=super_env)
    codes = [i["code"] for i in lst["items"]]
    assert codes == ["viewer", "ops_ro", "ops_off"]
    assert lst["features"] == FEATURES
    opts = role_options(db=super_env)
    # options 只给启用的（users 下拉挂不到停用角色）
    assert [i["v"] for i in opts["items"]] == ["viewer", "ops_ro"]


# ===========================================================================
# F. 老用户兼容（仅 int 角色，role_code NULL——与 Phase 1 行为一致）
# ===========================================================================

def test_int_only_user_phase1_equivalent(db_session, monkeypatch):
    """viewer(0)/admin(1)/super(2) 仅 int 角色：登录后判定 + guard 行为
    与 Phase 1 逐格一致（role_perm 无行也不漂移——内置恒走硬编码表）。"""
    for name, role_int, path, method, want in (
            ("v-user", 0, "/api/gateways", "GET", 200),
            ("v-user", 0, "/api/gateways", "POST", 403),
            ("a-user", 1, "/api/gateways", "POST", 200),
            ("a-user", 1, "/api/users", "GET", 403),
            ("s-user", 2, "/api/users", "POST", 200)):
        _mk_user(db_session, name, role=role_int)
        resp, _ = _guard(monkeypatch, db_session, method, path, name)
        assert resp.status_code == want, (name, method, path)


# ===========================================================================
# G. me/perms（_effective_perms）
# ===========================================================================

def test_effective_perms_builtin_no_db(db_session):
    """内置三档：不查库（BUILTIN_FALLBACK 真源），14 feature 全量返回。"""
    p = _authz_mod._effective_perms(db_session, "viewer")
    assert p == _EXPECTED_BUILTIN["viewer"]
    assert "other" not in p


def test_effective_perms_custom_fills_none(db_session):
    """自定义角色缺行补 none（14 全量，前端不用猜缺省）。"""
    _mk_role(db_session, "ops_ro", perms={"gateways": "read"})
    p = _authz_mod._effective_perms(db_session, "ops_ro")
    assert p["gateways"] == "read"
    assert p["cdr"] == "none"
    assert len(p) == len(FEATURES)


def test_effective_perms_db_error_none(db_session, monkeypatch):
    """DB 异常 -> None（前端走 Phase 1 降级，不把人锁门外）。"""
    class _Boom:
        def scalar(self, *a, **k):
            raise RuntimeError("db down")
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    p = _authz_mod._effective_perms(_Boom(), "ops_ro")
    assert p is None
