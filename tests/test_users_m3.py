"""M3 用户管理 Phase 1 测试（T-301 · 64ef74a 拍板范围）。

无 DB 环境（不起 MySQL/Redis）即可跑：真库集成与 E2E 走 zstack 独立栈
（容器 py3.12 + 钉版依赖，覆盖 TestClient 网络层）。

三个模块：
1. api/auth._login_check_db —— DB 优先登录三分支（命中/空表回落/异常拒登），
   SessionLocal 桩到内存 SQLite。
2. api/users —— CRUD 业务守卫（最后 super 不可降级/停删、不可自停自删、
   入参校验）。真 ORM 路径（内存 SQLite）+ 真 require_role；鉴权点
   get_current_admin 桩成固定身份（require_role 是闭包工厂，
   dependency_overrides 按对象匹配拦不住它，故桩它内部实际调用的鉴权函数）。
3. api/authz —— require_role 403 矩阵 + write_guard viewer 只读。

⚠️ 两个环境坑（写测试前必读）：
- 导入链：db/models.py 顶层 `from db.session import Base`，而真实 db.session
  **模块级连 MySQL 跑自迁移**，无 DB 环境导入即炸。故先在 sys.modules 里用
  **桩 db.session**（仅 Base + get_db，无迁移副作用）占坑，再导入 models。
- 本地 py3.10 + pydantic 2.13 下 FastAPI 的 Session 参数注解经 TypeAdapter
  求值 ForwardRef（JoinTransactionMode/Mapper）会 PydanticUserError（py3.12
  无此问题，CI/容器环境不受影响）。因此**本文件不走 TestClient 网络层**，
  中间件用直接调用 + EndpointUnderTest 直调端点函数的方式覆盖等价逻辑面。

sys.modules 桩 + SQLite 建表 + ORM 路径完全真实；仅网络封装层（路由匹配、
序列化）在容器内 E2E 补。
"""
import sys
import types
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Request
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

# --- db.session 处理：能用真实模块就不装桩（修全量会话隔离缺陷） -----------------
# 缺陷复盘（2026-09-13，容器全量跑暴露）：真实 db.session 模块级连 MySQL 跑自迁移，
# 无 DB 环境导入即炸——所以本地单跑才需要桩。但**全量会话下**（容器/CI 有真 MySQL），
# 字母序在前的测试文件会先成功导入**真实** db.session，models 随之绑定**真实 Base**；
# 旧写法无条件以桩顶替 sys.modules 再从桩取 Base，拿到的是**空 metadata** 的 Base
# → create_all 建不出任何表 → `no such table: sys_user`（单文件跑正常、全量跑炸）。
#
# 修复口径（两条，缺一不可）：
#   1. 桩只在「真实模块未加载且导入失败」时安装（容器/部署形态下真实模块可用，直接用）；
#   2. 建表以 **models 实际注册的 metadata** 为准（SysUser.__table__.metadata），
#      与哪个 Base 无关——两种加载时序（真实先载 / 桩先载）下都正确。
if "db.session" not in sys.modules:
    try:
        import db.session  # noqa: F401,E402  容器/CI：真实模块（迁移幂等，副作用与其它测试等同）
    except Exception:
        _stub = types.ModuleType("db.session")
        _stub._STUB = True

        class _StubBase(DeclarativeBase):
            pass

        _stub.Base = _StubBase
        _stub.SessionLocal = None       # 测试内按需替换
        _stub.get_db = None            # 仅占位
        sys.modules["db.session"] = _stub

if "db" not in sys.modules:
    import db  # noqa: F401,E402

import core.config as _cc  # noqa: E402
import api.auth as _auth_mod  # noqa: E402
import api.authz as _authz_mod  # noqa: E402
import db.session as _ds_mod  # noqa: E402
from db.models import SysUser  # noqa: E402
from api.users import (create_user, update_user, delete_user,  # noqa: E402
                       reset_password, change_own_password, list_users,
                       _user_out, UserCreate, UserUpdate, PasswordReset,
                       SelfPassword, _enabled_super_count)
from api.authz import require_role, write_guard_middleware, _role_of, ENFORCE_ROLE  # noqa: E402

# 建表基准：models 实际挂的 metadata（真实 Base 或桩 Base 均自适应，见顶部复盘）
_Meta = SysUser.__table__.metadata

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
    _Meta.create_all(engine)
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
    with _auth_settings(), patch.object(_ds_mod, "SessionLocal", lambda: sess):
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
    with _auth_settings(), patch.object(_ds_mod, "SessionLocal", _Boom):
        assert _auth_mod._login_check_db("admin", "x") is False


# ===========================================================================
# 2. users.py CRUD 守卫（真 require_role + get_current_admin 桩）
# ===========================================================================

class _FakeRequest:
    """require_role / write_guard 需要的最小 Request 形状。"""
    def __init__(self, method="GET", path="/api/users", cookies=None):
        self.method = method
        self.url = types.SimpleNamespace(path=path)
        self.cookies = cookies or {}


def _as_super(monkeypatch, username="root-super"):
    """把 authz.get_current_admin 桩成固定身份（require_role 内部实际调用点），
    并让 SessionLocal 指向测试库 —— 角色/守卫逻辑全部走真实现。"""
    monkeypatch.setattr(_authz_mod, "get_current_admin",
                        lambda request: username)
    monkeypatch.setattr(_ds_mod, "SessionLocal",
                        lambda: _CUR_SESSION["db"])


_CUR_SESSION = {"db": None}


@pytest.fixture
def super_env(db_session, monkeypatch):
    """actor = root-super（DB 里的真 super 行）+ SessionLocal 指到测试库。"""
    _mk_user(db_session, "root-super", role=2)
    _CUR_SESSION["db"] = db_session
    _as_super(monkeypatch)
    yield db_session
    _CUR_SESSION["db"] = None


# --- create ---

def test_create_user_validations(super_env):
    db = super_env
    # 口令过短 / 非法角色 / 空用户名 / 用户名重复
    for body in ({"username": "x", "password": "short", "role": 1},
                 {"username": "x", "password": "long-enough-99", "role": 9},
                 {"username": "", "password": "long-enough-99", "role": 1}):
        with pytest.raises(HTTPException) as e:
            create_user(UserCreate(**body), db=db)
        assert e.value.status_code == 400
    r = create_user(UserCreate(username="eve", password="long-enough-99", role=0), db=db)
    assert r["role_name"] == "viewer" and r["username"] == "eve"
    with pytest.raises(HTTPException) as e:
        create_user(UserCreate(username="eve", password="long-enough-99", role=1), db=db)
    assert e.value.status_code == 400  # 用户名重复


def test_create_user_requires_super(db_session, monkeypatch):
    """admin 身份建用户 -> require_role("super") 403（回落 admin 同样拒）；
    未登录 -> 401。经 require_role 依赖矩阵覆盖（端点函数自身的守卫在
    test_create_user_validations 已测，此处不重复）。"""
    _mk_user(db_session, "adam", role=1)
    _CUR_SESSION["db"] = db_session
    monkeypatch.setattr(_authz_mod, "get_current_admin", lambda r: "adam")
    with pytest.raises(HTTPException) as e:
        _dep(require_role("super"), db_session)
    assert e.value.status_code == 403
    _CUR_SESSION["db"] = None


# --- update ---

def test_update_last_super_guards_self(super_env):
    """唯一 super 是自己：降级/停用均 400（自我守卫与最后 super 守卫双命中）。"""
    db = super_env
    su = db.scalar(select(SysUser).where(SysUser.username == "root-super"))
    with pytest.raises(HTTPException) as e:
        update_user(su.id, UserUpdate(role=1), db=db,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        update_user(su.id, UserUpdate(status=0), db=db,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400


def test_update_last_super_guard_other(db_session):
    """target 是最后一个启用 super、actor 是别的 super -> last-super 守卫 400。"""
    su = _mk_user(db_session, "solo-super", role=2)
    with pytest.raises(HTTPException) as e:
        update_user(su.id, UserUpdate(role=1), db=db_session,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        update_user(su.id, UserUpdate(status=0), db=db_session,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400


def test_update_two_supers_one_may_pause(super_env):
    """两个启用 super：不可停/降自己；可停别人（仍留一个）；已停用的可降级。"""
    db = super_env
    su = db.scalar(select(SysUser).where(SysUser.username == "root-super"))
    _mk_user(db, "backup-super", role=2)
    with pytest.raises(HTTPException) as e:  # 停自己
        update_user(su.id, UserUpdate(status=0), db=db,
                    actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400
    b = db.scalar(select(SysUser).where(SysUser.username == "backup-super"))
    r = update_user(b.id, UserUpdate(status=0), db=db,      # 停别人 OK
                    actor={"user": "root-super", "role": "super"})
    assert r["status"] == 0
    # backup-super 已停用 -> 再降级不触发 last-super 守卫（启用 super 仍有一个）
    r = update_user(b.id, UserUpdate(role=1), db=db,
                    actor={"user": "root-super", "role": "super"})
    assert r["role"] == 1


def test_update_normal_user_ok(super_env):
    u = _mk_user(super_env, "norm", role=1)
    r = update_user(u.id, UserUpdate(role=0, status=1), db=super_env,
                    actor={"user": "root-super", "role": "super"})
    assert r["role"] == 0 and r["status"] == 1


# --- delete ---

def test_delete_guards(super_env):
    """删自己 400；删最后一个启用 super 400（此处同一人）；删普通用户 200。"""
    db = super_env
    su = db.scalar(select(SysUser).where(SysUser.username == "root-super"))
    with pytest.raises(HTTPException) as e:  # 删自己（也是最后 super）
        delete_user(su.id, db=db, actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 400
    u = _mk_user(db, "norm")
    r = delete_user(u.id, db=db, actor={"user": "root-super", "role": "super"})
    assert r["ok"] is True


# --- reset / change own password ---

def test_reset_password_flow(super_env):
    u = _mk_user(super_env, "alice")
    with pytest.raises(HTTPException) as e:
        reset_password(u.id, PasswordReset(password="tiny"), db=super_env)
    assert e.value.status_code == 400
    r = reset_password(u.id, PasswordReset(password="new-pass-456"), db=super_env)
    assert r["ok"] is True
    from core.pw_hash import verify_password
    row = super_env.get(SysUser, u.id)
    assert verify_password("new-pass-456", row.password_hash) is True
    assert verify_password("pass-word-123", row.password_hash) is False


def test_change_own_password_requires_old(super_env):
    """改自己密码：错旧口令 401；对旧口令 200 且落库。viewer 也可用（守卫豁免）。"""
    db = super_env
    with pytest.raises(HTTPException) as e:
        change_own_password(SelfPassword(old_password="bad-old",
                                          new_password="new-pass-456"), db=db,
                            actor={"user": "root-super", "role": "super"})
    assert e.value.status_code == 401
    r = change_own_password(SelfPassword(old_password="pass-word-123",
                                         new_password="new-pass-456"), db=db,
                            actor={"user": "root-super", "role": "super"})
    assert r["ok"] is True
    row = db.scalar(select(SysUser).where(SysUser.username == "root-super"))
    from core.pw_hash import verify_password
    assert verify_password("new-pass-456", row.password_hash) is True


# --- list / shape ---

def test_list_users(super_env):
    # 直调端点：Query() 默认值不经 FastAPI 解析，分页参数显式传（与 oplog 用例同口径）
    r = list_users(page=1, page_size=50, db=super_env)
    assert [i["username"] for i in r["items"]] == ["root-super"]
    assert r["total"] == 1 and r["total_pages"] == 1 and r["page"] == 1


def test_list_users_pagination(super_env):
    """分页口径与 operation-logs 一致：total/total_pages/offset+limit。"""
    db = super_env
    for i in range(5):
        _mk_user(db, "u%d" % i)
    r1 = list_users(page=1, page_size=3, db=db)
    assert r1["total"] == 6 and r1["total_pages"] == 2
    assert [i["username"] for i in r1["items"]] == ["root-super", "u0", "u1"]
    r2 = list_users(page=2, page_size=3, db=db)
    assert [i["username"] for i in r2["items"]] == ["u2", "u3", "u4"]
    # 越界回落最后一页
    r9 = list_users(page=9, page_size=3, db=db)
    assert r9["page"] == 2 and len(r9["items"]) == 3


def test_user_out_shape(db_session):
    u = _mk_user(db_session, "carol", role=0, status=1)
    d = _user_out(u)
    assert d == {"id": u.id, "username": "carol", "role": 0, "role_name": "viewer",
                 "status": 1, "created_at": u.created_at}


# ===========================================================================
# 3. authz：require_role 403 矩阵 + write_guard viewer 只读
#    （不走 TestClient —— 见文件头注释；直调 require_role dep / write_guard）
# ===========================================================================

def _dep(requirement, db, cookies=None):
    """直接执行 require_role 依赖（等价 FastAPI 依赖解析后的调用）。

    db 形参显式注入测试 session（require_role 的 dep 签名是
    `db=Depends(get_db)` —— 直调不经 FastAPI 解析，需手动喂）。
    """
    req = _FakeRequest(cookies=cookies or {})
    import inspect
    sig = inspect.signature(requirement)
    kwargs = {"request": req}
    for name, p in sig.parameters.items():
        if p.default is not inspect.Parameter.empty and "Depends" in str(p.default):
            kwargs[name] = db
    return requirement(**kwargs)


def test_require_role_matrix(db_session, monkeypatch):
    """矩阵：viewer/admin 打 super-only 依赖 -> 403；super -> 放行；未登录 -> 401。"""
    _mk_user(db_session, "vicky", role=0)
    _mk_user(db_session, "adam", role=1)
    _mk_user(db_session, "sue", role=2)
    _CUR_SESSION["db"] = db_session
    super_dep = require_role("super")
    plain_dep = require_role()

    for actor in ("vicky", "adam"):
        monkeypatch.setattr(_authz_mod, "get_current_admin", lambda r, a=actor: a)
        with pytest.raises(HTTPException) as e:
            _dep(super_dep, db_session)
        assert e.value.status_code == 403

    monkeypatch.setattr(_authz_mod, "get_current_admin", lambda r: "sue")
    out = _dep(super_dep, db_session)
    assert out == {"user": "sue", "role": "super"}

    # 未登录：get_current_admin 抛 401 -> 依赖返回 401（与全站鉴权口径一致）
    def _no_cookie(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    monkeypatch.setattr(_authz_mod, "get_current_admin", _no_cookie)
    with pytest.raises(HTTPException) as e:
        _dep(super_dep, db_session)
    assert e.value.status_code == 401
    _CUR_SESSION["db"] = None


def test_write_guard_viewer_readonly(db_session, monkeypatch):
    import asyncio

    async def _run():
        return await _check_write_guard(db_session, monkeypatch)

    asyncio.run(_run())


async def _check_write_guard(db_session, monkeypatch):
    """write_guard：viewer 的业务写路径 403 JSON；viewer 改自己密码豁免；
    admin/super 写放行；未登录放行给下游（_auth_guard 统一 401）。"""
    import asyncio
    _mk_user(db_session, "vicky", role=0)
    _mk_user(db_session, "adam", role=1)
    _mk_user(db_session, "sue", role=2)
    _CUR_SESSION["db"] = db_session
    # authz 是 `from db.session import SessionLocal` 直接绑定 —— 须 patch 它自己
    # 模块命名空间的引用（write_guard 内部直接调用，不走 Depends）
    monkeypatch.setattr(_authz_mod, "SessionLocal",
                        lambda: _CUR_SESSION["db"])

    async def _ok(request):
        return "PASS"

    def _as(actor):
        monkeypatch.setattr(_authz_mod, "get_current_admin", lambda r, a=actor: a)

    # viewer 业务写 -> 403（fastapi JSONResponse）
    _as("vicky")
    resp = await write_guard_middleware(
        _FakeRequest(method="POST", path="/api/gateways"), _ok)
    assert resp.status_code == 403
    assert "viewer" in resp.body.decode()

    # viewer 改自己密码 -> 豁免（_SKIP_PATHS）
    resp = await write_guard_middleware(
        _FakeRequest(method="POST", path="/api/users/me/password"), _ok)
    assert resp == "PASS"

    # admin / super 写 -> 放行
    for actor in ("adam", "sue"):
        _as(actor)
        resp = await write_guard_middleware(
            _FakeRequest(method="POST", path="/api/gateways"), _ok)
        assert resp == "PASS"

    # 未登录写 -> 放行给下游 _auth_guard（统一 401，不被 write_guard 吞成 403）
    def _no_cookie(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    monkeypatch.setattr(_authz_mod, "get_current_admin", _no_cookie)
    resp = await write_guard_middleware(
        _FakeRequest(method="POST", path="/api/gateways"), _ok)
    assert resp == "PASS"

    # GET 不拦（只读面）
    _as("vicky")
    resp = await write_guard_middleware(
        _FakeRequest(method="GET", path="/api/gateways"), _ok)
    assert resp == "PASS"
    _CUR_SESSION["db"] = None


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


def test_enforce_role_flipped():
    """M3 Phase 1 验证后 ENFORCE_ROLE=True（fail-open 观察期结束）。
    该断言守住「不许再静默翻回 False」——回退须改测试说明理由。"""
    assert ENFORCE_ROLE is True


# ===========================================================================
# 4. oplog 查询面（T-301 尾巴，2026-09-13 维护者拍板挂载）
#    口径：登录即可查、不限角色（viewer 可查）；ENFORCE_ROLE=True 下不被
#    write_guard / require_role 误伤。middleware 已挂载不重复测挂载本身。
# ===========================================================================

def test_oplog_query_requires_login(db_session, monkeypatch):
    """① 未登录 -> 401（require_role() 空 roles = 仅校验登录，显式声明）。"""
    _CUR_SESSION["db"] = db_session
    monkeypatch.setattr(_ds_mod, "SessionLocal", lambda: _CUR_SESSION["db"])
    monkeypatch.setattr(_authz_mod, "SessionLocal", lambda: _CUR_SESSION["db"])

    def _no_cookie(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    monkeypatch.setattr(_authz_mod, "get_current_admin", _no_cookie)
    with pytest.raises(HTTPException) as e:
        _dep(require_role(), db_session)
    assert e.value.status_code == 401
    _CUR_SESSION["db"] = None


@pytest.mark.parametrize("actor,role", [("vicky", 0), ("adam", 1), ("sue", 2)])
def test_oplog_query_all_roles_allowed(db_session, monkeypatch, actor, role):
    """② 登录后任何角色（含 viewer）-> 依赖放行；ENFORCE_ROLE=True 状态下断言
    「角色不被限制」——viewer 不被 require_role（空 roles）或只读守卫误伤。"""
    assert ENFORCE_ROLE is True, "本断言在 ENFORCE_ROLE=True 口径下才有意义"
    _mk_user(db_session, actor, role=role)
    _CUR_SESSION["db"] = db_session
    monkeypatch.setattr(_authz_mod, "SessionLocal", lambda: _CUR_SESSION["db"])
    monkeypatch.setattr(_authz_mod, "get_current_admin", lambda r: actor)
    out = _dep(require_role(), db_session)   # oplog 路由的 dependencies 同款依赖
    assert out["user"] == actor
    assert out["role"] == ROLE_NAME_OF(role)
    # GET 不在 write_guard 的 _WRITE_METHODS 内 -> 查询面不受只读守卫影响
    assert "GET" not in _authz_mod._WRITE_METHODS
    _CUR_SESSION["db"] = None


def ROLE_NAME_OF(v):
    return {0: "viewer", 1: "admin", 2: "super"}[v]


def test_oplog_query_filters_and_paging(db_session, monkeypatch):
    """③ 查询逻辑本身：record_op 写入面 -> 列表过滤（operator 模糊 / action 精确）
    + 分页形状。record_op 走 SessionLocal（真实写入路径，桩到 SQLite）。"""
    import api.oplog as _oplog_mod
    _CUR_SESSION["db"] = db_session
    monkeypatch.setattr(_oplog_mod, "SessionLocal", lambda: _CUR_SESSION["db"])
    # 造三条：两条 super 的 post、一条 anonymous 的 delete
    _oplog_mod.record_op("sue", "post", "http", "/api/users", {"status": 201})
    _oplog_mod.record_op("sue", "post", "http", "/api/gateways", {"status": 200})
    _oplog_mod.record_op("anonymous", "delete", "http", "/api/users/1", {"status": 200})
    db_session.expire_all()

    from api.oplog import list_operation_logs
    # 直调端点：Query() 默认值不经 FastAPI 解析，page/page_size 须显式传（与
    # 真实请求等价；E2E 在 zdev 容器已覆盖 URL 参数路径）
    r_all = list_operation_logs(page=1, page_size=50, db=db_session)
    assert r_all["total"] == 3 and len(r_all["items"]) == 3
    assert r_all["page"] == 1 and r_all["total_pages"] == 1

    r_op = list_operation_logs(page=1, page_size=50, operator="su", db=db_session)
    assert r_op["total"] == 2
    r_act = list_operation_logs(page=1, page_size=50, action="delete", db=db_session)
    assert r_act["total"] == 1 and r_act["items"][0]["operator"] == "anonymous"

    r_pg = list_operation_logs(page=2, page_size=2, db=db_session)
    assert r_pg["total"] == 3 and r_pg["total_pages"] == 2
    assert len(r_pg["items"]) == 1                                  # 第 2 页剩 1 条
    assert [i["id"] for i in r_all["items"]] == sorted(
        [i["id"] for i in r_all["items"]], reverse=True)           # id 倒序
    _CUR_SESSION["db"] = None
