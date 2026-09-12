"""CSRF 同源写校验中间件测试（api/csrf.py）。

覆盖：
- 跨源写（Origin 不同 host）→ 403；Origin/Referer 全缺 → 403。
- 同源写（Origin host == Host / X-Forwarded-Host）→ 放行。
- GET 写豁免 / login 白名单 / Authorization 头豁免（API 客户端）。
- 反代多值 X-Forwarded-Host 匹配任一即放行。
- /fs/* 非 cookie 面不在管辖（POST 无 Origin 也放行 —— FS xml_curl 不带 Origin）。

用独立 FastAPI 挂中间件 + 桩路由，不牵连 db.session（无 DB 环境可跑）。
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.csrf import csrf_middleware, is_same_origin

app = FastAPI()
app.middleware("http")(csrf_middleware)


@app.post("/api/things")
def create_thing():
    return {"ok": True}


@app.get("/api/things")
def list_things():
    return {"ok": True}


@app.post("/api/login")
def login_stub():
    return {"ok": True}


@app.post("/fs/dialplan")
def fs_stub():
    return {"ok": True}


c = TestClient(app)


def test_cross_origin_write_403():
    r = c.post("/api/things", headers={"Origin": "https://evil.example", "Host": "sip.local:8000"})
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_missing_origin_and_referer_403():
    r = c.post("/api/things", headers={"Host": "sip.local:8000"})
    assert r.status_code == 403


def test_same_origin_write_allowed():
    r = c.post("/api/things", headers={"Origin": "http://sip.local:8000", "Host": "sip.local:8000"})
    assert r.status_code == 200


def test_referer_same_origin_allowed():
    r = c.post("/api/things", headers={
        "Referer": "http://sip.local:8000/admin", "Host": "sip.local:8000"})
    assert r.status_code == 200


def test_get_not_guarded():
    # GET 不在写方法集合，无 Origin 也放行
    r = c.get("/api/things")
    assert r.status_code == 200


def test_login_whitelisted():
    r = c.post("/api/login", headers={"Origin": "https://evil.example", "Host": "sip.local:8000"})
    assert r.status_code == 200


def test_authorization_header_exempt():
    # 显式凭据式 API 客户端（token/Basic）：custom header 本身即预检屏障，豁免
    r = c.post("/api/things", headers={
        "Origin": "https://evil.example", "Host": "sip.local:8000",
        "Authorization": "Bearer xxx"})
    assert r.status_code == 200


def test_forwarded_host_match_allowed():
    # 反代场景：Host 是内网上游，真实外部名在 X-Forwarded-Host
    r = c.post("/api/things", headers={
        "Origin": "https://sip.example.com", "Host": "127.0.0.1:8000",
        "X-Forwarded-Host": "sip.example.com"})
    assert r.status_code == 200


def test_fs_callbacks_not_guarded():
    # /fs/* 是 FS xml_curl 回调面（非 cookie 面），FS 不带 Origin 头，必须放行
    r = c.post("/fs/dialplan", headers={"Host": "sip.local:8000"})
    assert r.status_code == 200


def test_is_same_origin_unit():
    class _R:
        def __init__(self, headers):
            self.headers = headers
    same = _R({"origin": "http://a:8000", "host": "a:8000"})
    diff = _R({"origin": "http://b:9000", "host": "a:8000"})
    none = _R({"host": "a:8000"})
    assert is_same_origin(same) is True
    assert is_same_origin(diff) is False
    assert is_same_origin(none) is False
