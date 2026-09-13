# -*- coding: utf-8 -*-
"""P2 G2：/api/stats/concurrency 来源标记（source 字段）单测。

覆盖三种口径（app.stats_concurrency，路由须注册在 crud_router 前——#34）：
- backend=redis 且 snapshot() 成功 → source=redis，值取真源快照
- backend=redis 但 snapshot() 失败（None）→ 回落影子，source=shadow
- backend=local → 无 Redis 真源，source=shadow

不连 Redis / MySQL：concurrency 与 get_concurrency 均 monkeypatch，
db 用假 scalars 对象（端点只读 AccessPoint/Gateway 的 concurrent_limit）。
"""
import pytest


class _FakeScalars:
    def all(self):
        return []


class _FakeDB:
    def scalars(self, *a, **k):
        return _FakeScalars()


def _patch_conc(monkeypatch, backend, snap, shadow):
    import api.app as A
    monkeypatch.setattr(A.concurrency, "backend", lambda: backend)
    if backend == "redis":
        monkeypatch.setattr(A.concurrency, "snapshot", lambda: snap)
    monkeypatch.setattr(A, "get_concurrency", lambda: shadow)


def test_source_redis_snapshot(monkeypatch):
    _patch_conc(monkeypatch, "redis",
                {"global": 3, "ap": {1: 2}, "gw": {7: 1}},
                {"global": 99, "ap": {}, "gw": {}})
    import api.app as A
    r = A.stats_concurrency(db=_FakeDB())
    assert r["source"] == "redis"
    assert r["global"] == 3 and r["gw"] == {7: 1} and r["ap"] == {1: 2}


def test_source_shadow_on_snapshot_none(monkeypatch):
    _patch_conc(monkeypatch, "redis", None,
                {"global": 1, "ap": {}, "gw": {}})
    import api.app as A
    r = A.stats_concurrency(db=_FakeDB())
    assert r["source"] == "shadow" and r["global"] == 1


def test_source_shadow_backend_local(monkeypatch):
    _patch_conc(monkeypatch, "local", None,
                {"global": 0, "ap": {}, "gw": {}})
    import api.app as A
    r = A.stats_concurrency(db=_FakeDB())
    assert r["source"] == "shadow" and r["global"] == 0


def test_route_registered_before_crud_router():
    """#34 回归锚点：stats 路由必须能被匹配，不被 crud 兜底 /api/{entity}/{item_id} 吞掉。

    直接验证注册顺序（stats 定义位置在 include_router(crud_router) 之前），
    若有人把路由搬回去，此测试立即变红。
    """
    import inspect
    import api.app as A
    src = inspect.getsource(A)
    assert src.index('def stats_concurrency') < src.index('app.include_router(crud_router)')
    assert any(getattr(r, "path", None) == "/api/stats/concurrency"
               for r in A.app.router.routes)
