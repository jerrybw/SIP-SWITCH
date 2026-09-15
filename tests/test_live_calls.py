# -*- coding: utf-8 -*-
"""需求②-2：实时通话 /api/stats/live-calls 单测（无 DB / 无 FS / 无网络）。

覆盖 api/live_calls.py：
- `parse_channels` 的 **None / [] 双语义** —— 这是本模块最关键的一条：
  「命令不被支持」与「真的没有通话」必须可区分，否则"节点少算了"会被读成"没有通话"。
- 归并成「一次呼叫一行」：A/B 腿按 call_uuid 合并，取 A 腿（uuid==call_uuid）为准。
- 5 个必填字段（所在节点/接入点/主叫话机/落地网关/被叫话机）取不到时**留空不编造**。
- 节点探测失败 → nodes[].ok=false + degraded=true，**不返 500**。
- 分页/过滤/排序确定性。
- #34 回归锚点：路由必须注册在 crud 兜底之前。
"""
import inspect
import json
from datetime import datetime, timezone

import pytest

import api.live_calls as LC


# ---------------------------------------------------------------------------
# 假 ESL
# ---------------------------------------------------------------------------
class _Ev:
    def __init__(self, body):
        self._b = body

    def getBody(self):
        return self._b


class _FakeCon:
    def __init__(self, responses, connected=True, raise_on=None):
        self.responses = responses
        self._connected = connected
        self.raise_on = raise_on
        self.disconnected = False

    def connected(self):
        return self._connected

    def api(self, cmd, timeout=10.0):
        if self.raise_on and cmd == self.raise_on:
            raise OSError("boom")
        v = self.responses.get(cmd)
        return None if v is None else _Ev(v)

    def disconnect(self):
        self.disconnected = True


def _patch_esl(monkeypatch, responses, connected=True, raise_on=None, holder=None):
    def factory(host, port, password, timeout=3.0, **kw):
        con = _FakeCon(responses, connected, raise_on)
        if holder is not None:
            holder.append(con)
        return con

    monkeypatch.setattr(LC, "ESLConnection", factory)


# ---------------------------------------------------------------------------
# 通道行 / dump 构造（字段名照 FS 1.11.2 实测）
# ---------------------------------------------------------------------------
def _row(uuid, call_uuid=None, cid="80000001", dest="0013", state="CS_EXECUTE",
         epoch=1789456665, cid_name=""):
    return {"uuid": uuid, "call_uuid": call_uuid or uuid, "cid_num": cid, "dest": dest,
            "state": state, "created_epoch": str(epoch), "cid_name": cid_name}


def _dump(uid, **kw):
    d = {"Caller-Unique-ID": uid, "Answer-State": "ringing",
         "Caller-Channel-Created-Time": "1789456665000251",
         "Caller-Channel-Answered-Time": "0",
         "Caller-Caller-ID-Number": "", "Caller-Destination-Number": ""}
    d.update(kw)
    return d


NOW = datetime.fromtimestamp(1789456675, timezone.utc)


# ---------------------------------------------------------------------------
# 解析：None vs []
# ---------------------------------------------------------------------------
def test_parse_channels_empty_is_valid_empty_list():
    assert LC.parse_channels('{"row_count":0}') == []
    assert LC.parse_channels('{"row_count":0,"rows":[]}') == []


def test_parse_channels_err_body_is_none_not_empty():
    """-ERR（命令不支持/语法错）= 没探到，**不能**当成"没有通话"。"""
    assert LC.parse_channels("-ERR no such command") is None
    assert LC.parse_channels("") is None
    assert LC.parse_channels(None) is None
    assert LC.parse_channels('{"foo":1}') is None


def test_parse_channels_rows_and_bare_list():
    rows = LC.parse_channels(json.dumps({"row_count": 1, "rows": [_row("a")]}))
    assert len(rows) == 1 and rows[0]["uuid"] == "a"
    assert LC.parse_channels(json.dumps([_row("b")]))[0]["uuid"] == "b"


def test_parse_dump_tolerant():
    assert LC.parse_dump("nonsense") == {}
    assert LC.parse_dump("[1,2]") == {}
    assert LC.parse_dump('{"Answer-State":"answered"}')["Answer-State"] == "answered"


# ---------------------------------------------------------------------------
# 归并
# ---------------------------------------------------------------------------
def test_merge_single_leg_without_vars_leaves_ids_empty():
    """内线/未出局呼叫：业务变量本就是空 → 必须留空（不编造），主被叫取自通道行。"""
    items = LC.merge_calls([_row("u1", cid="80000001", dest="80000002")],
                           {"u1": _dump("u1", **{"Caller-Caller-ID-Number": "80000001",
                                                 "Caller-Destination-Number": "80000002"})},
                           "node-1", "freeswitch", now=NOW)
    assert len(items) == 1
    it = items[0]
    assert it["call_uuid"] == "u1"
    assert it["access_point_id"] is None and it["gateway_id"] is None
    assert it["carrier_id"] is None
    assert it["access_point_name"] == "" and it["gateway_name"] == "" and it["carrier_name"] == ""
    assert it["caller"] == "80000001" and it["callee"] == "80000002"
    assert it["node_uuid"] == "node-1" and it["node_name"] == "freeswitch"
    assert it["direction"] == "internal"


def test_merge_two_legs_into_one_row_taking_a_leg():
    """A/B 腿必须合成一行；业务变量取 A 腿（uuid == call_uuid）。"""
    a = _row("leg-a", call_uuid="leg-a", cid="80000001", dest="001380000001")
    b = _row("leg-b", call_uuid="leg-a", cid="80000001", dest="001380000001")
    dumps = {
        "leg-a": _dump("leg-a", **{LC.V_ACCESS_POINT: "7", LC.V_GATEWAY: "3",
                                   LC.V_CARRIER: "2",
                                   "Answer-State": "answered",
                                   "Caller-Channel-Answered-Time": "1789456666000000"}),
        "leg-b": _dump("leg-b", **{LC.V_GATEWAY: "99"}),   # B 腿的值不得覆盖 A 腿
    }
    items = LC.merge_calls([a, b], dumps, "n1", "fs1", now=NOW)
    assert len(items) == 1
    it = items[0]
    assert it["gateway_id"] == 3 and it["access_point_id"] == 7 and it["carrier_id"] == 2
    assert it["direction"] == "outbound"
    assert it["state"] == "answered"
    assert it["duration_sec"] == 9        # 1789456675 - 1789456666


def test_merge_falls_back_to_other_leg_only_to_fill_blanks():
    """A 腿该值为空时才回落同组其它腿 —— 只补齐，不覆盖。"""
    a = _row("leg-a", call_uuid="leg-a")
    b = _row("leg-b", call_uuid="leg-a")
    dumps = {"leg-a": _dump("leg-a"), "leg-b": _dump("leg-b", **{LC.V_GATEWAY: "5"})}
    items = LC.merge_calls([a, b], dumps, "n1", "fs1", now=NOW)
    assert items[0]["gateway_id"] == 5


def test_merge_inbound_when_ap_but_no_gateway():
    a = _row("leg-a")
    dumps = {"leg-a": _dump("leg-a", **{LC.V_ACCESS_POINT: "7"})}
    assert LC.merge_calls([a], dumps, "n1", "fs1", now=NOW)[0]["direction"] == "inbound"


def test_merge_duration_uses_created_when_not_answered():
    a = _row("leg-a", epoch=1789456665)
    dumps = {"leg-a": _dump("leg-a", **{
        "Caller-Channel-Created-Time": "1789456665000000"})}
    it = LC.merge_calls([a], dumps, "n1", "fs1", now=NOW)[0]
    assert it["state"] == "ringing" and it["duration_sec"] == 10
    assert it["create_time"].startswith("2026-09-15T")


def test_merge_state_falls_back_to_call_state_when_answer_state_missing():
    a = _row("leg-a")
    dumps = {"leg-a": {"Channel-Call-State": "ACTIVE",
                       "Caller-Channel-Created-Time": "1789456665000251"}}
    assert LC.merge_calls([a], dumps, "n1", "fs1", now=NOW)[0]["state"] == "answered"


def test_merge_without_dump_still_works_using_row():
    """uuid_dump 失败也不能丢行：主被叫回落 show channels 的 cid_num/dest。"""
    it = LC.merge_calls([_row("u1", cid="111", dest="222", epoch=1789456665,
                              state="CS_ROUTING")],
                        {}, "n1", "fs1", now=NOW)[0]
    assert it["caller"] == "111" and it["callee"] == "222"
    assert it["duration_sec"] == 10 and it["state"] == "ringing"


def test_merge_skips_channel_without_uuid():
    assert LC.merge_calls([{"call_uuid": "x"}], {}, "n1", "fs1", now=NOW) == []


# ---------------------------------------------------------------------------
# probe_node
# ---------------------------------------------------------------------------
def test_probe_node_ok(monkeypatch):
    responses = {
        LC.CHANNELS_CMD: json.dumps({"row_count": 1, "rows": [_row("u1")]}),
        LC.DUMP_CMD % "u1": json.dumps(_dump("u1", **{LC.V_GATEWAY: "3"})),
    }
    holder = []
    _patch_esl(monkeypatch, responses, holder=holder)
    ok, rows, dumps, err = LC.probe_node("fs1", 8021, "pw", timeout=1.0)
    assert ok is True and err == "" and len(rows) == 1
    assert dumps["u1"][LC.V_GATEWAY] == "3"
    assert holder[0].disconnected is True          # 必须断开短连接


def test_probe_node_empty_channels_is_success(monkeypatch):
    _patch_esl(monkeypatch, {LC.CHANNELS_CMD: '{"row_count":0}'})
    ok, rows, dumps, err = LC.probe_node("fs1", 8021, "pw")
    assert (ok, rows, dumps, err) == (True, [], {}, "")


def test_probe_node_bad_output_is_failure(monkeypatch):
    _patch_esl(monkeypatch, {LC.CHANNELS_CMD: "-ERR command not found"})
    ok, _rows, _d, err = LC.probe_node("fs1", 8021, "pw")
    assert ok is False and err == "bad_show_channels_output"


def test_probe_node_connect_failed(monkeypatch):
    _patch_esl(monkeypatch, {}, connected=False)
    ok, _r, _d, err = LC.probe_node("fs1", 8021, "pw")
    assert ok is False and err == "esl_connect_failed"


def test_probe_node_no_reply(monkeypatch):
    _patch_esl(monkeypatch, {})
    ok, _r, _d, err = LC.probe_node("fs1", 8021, "pw")
    assert ok is False and err == "show_channels_no_reply"


def test_probe_node_exception_is_captured(monkeypatch):
    _patch_esl(monkeypatch, {LC.CHANNELS_CMD: '{"row_count":0}'}, raise_on=LC.CHANNELS_CMD)
    ok, _r, _d, err = LC.probe_node("fs1", 8021, "pw")
    assert ok is False and err.startswith("probe_error")


def test_probe_node_over_limit_fails_loudly(monkeypatch):
    """超过单节点详查上限 → 显性失败（进 degraded），绝不静默截断。"""
    rows = [_row("u%d" % i) for i in range(3)]
    _patch_esl(monkeypatch, {LC.CHANNELS_CMD: json.dumps({"row_count": 3, "rows": rows})})
    ok, r, _d, err = LC.probe_node("fs1", 8021, "pw", max_channels=2)
    assert ok is False and err.startswith("channel_count_over_limit") and len(r) == 3


def test_probe_all_gives_entry_for_every_target(monkeypatch):
    def fake_probe(host, port, password, timeout=2.0, **kw):
        if host == "bad":
            raise RuntimeError("thread blew up")
        return True, [_row("u-" + host)], {}, ""

    monkeypatch.setattr(LC, "probe_node", fake_probe)
    out = LC.probe_all([("n1", "fs1", "good", 8021), ("n2", "fs2", "bad", 8021)], "pw")
    assert set(out.keys()) == {"n1", "n2"}
    assert out["n1"][0] is True and out["n2"][0] is False
    assert out["n2"][3].startswith("probe_error")
    assert LC.probe_all([], "pw") == {}


# ---------------------------------------------------------------------------
# query_live_calls：契约形状 / 降级 / 过滤 / 分页
# ---------------------------------------------------------------------------
class _Node:
    def __init__(self, uuid, host, port=8021, name="ctr-id", status=1):
        self.node_uuid, self.host, self.esl_port = uuid, host, port
        self.name, self.status = name, status


class _Rows:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _DB:
    def __init__(self, *batches):
        self._b = list(batches)
        self.queries = []

    def scalars(self, q, *a, **k):
        self.queries.append(q)
        return _Rows(self._b.pop(0) if self._b else [])


class _AP:
    def __init__(self, i, name):
        self.id, self.name = i, name


class _GW:
    def __init__(self, i, name, carrier_id):
        self.id, self.name, self.carrier_id = i, name, carrier_id


class _Carrier:
    def __init__(self, i, name):
        self.id, self.name = i, name


def _stub_probe(monkeypatch, per_node):
    """per_node = {node_uuid: (ok, channels, dumps, err)}"""
    monkeypatch.setattr(LC, "probe_all",
                        lambda targets, password, timeout=LC.DEFAULT_TIMEOUT, workers=8:
                        {u: per_node[u] for (u, _n, _h, _p) in targets})


def test_query_contract_shape(monkeypatch):
    node = _Node("n1", "fs1")
    ch = _row("leg-a", cid="80000001", dest="0013")
    dumps = {"leg-a": _dump("leg-a", **{LC.V_ACCESS_POINT: "7", LC.V_GATEWAY: "3"})}
    _stub_probe(monkeypatch, {"n1": (True, [ch], dumps, "")})
    db = _DB([node], [_AP(7, "接入点A")], [_GW(3, "网关X", 2)], [_Carrier(2, "运营商Y")])

    r = LC.query_live_calls(db)
    assert set(r.keys()) == {"items", "total", "page", "page_size", "total_pages",
                             "snapshot_at", "nodes", "degraded"}
    assert r["total"] == 1 and r["total_pages"] == 1 and r["page"] == 1
    assert r["page_size"] == 50 and r["degraded"] is False
    assert r["snapshot_at"]
    assert r["nodes"] == [{"node_uuid": "n1", "name": "fs1", "ok": True, "error": ""}]
    it = r["items"][0]
    assert set(it.keys()) == {"call_uuid", "node_uuid", "node_name", "access_point_id",
                              "access_point_name", "caller", "caller_name", "callee",
                              "gateway_id", "gateway_name", "carrier_id", "carrier_name",
                              "direction", "state", "duration_sec", "create_time"}
    assert it["access_point_name"] == "接入点A"
    assert it["gateway_name"] == "网关X"
    assert it["carrier_name"] == "运营商Y"
    assert it["node_uuid"] == "n1" and it["node_name"] == "fs1"


def test_query_failed_node_is_degraded_not_500(monkeypatch):
    node = _Node("n1", "fs1")
    _stub_probe(monkeypatch, {"n1": (False, [], {}, "esl_connect_failed")})
    r = LC.query_live_calls(_DB([node]))
    assert r["items"] == [] and r["degraded"] is True and r["total"] == 0
    assert r["nodes"] == [{"node_uuid": "n1", "name": "fs1", "ok": False,
                           "error": "esl_connect_failed"}]


def test_query_partial_failure_keeps_good_node_data(monkeypatch):
    good, bad = _Node("n1", "fs1"), _Node("n2", "fs2")
    _stub_probe(monkeypatch, {"n1": (True, [_row("u1")], {"u1": _dump("u1")}, ""),
                              "n2": (False, [], {}, "esl_connect_failed")})
    r = LC.query_live_calls(_DB([good, bad]))
    assert r["total"] == 1 and r["degraded"] is True
    assert [n["ok"] for n in r["nodes"]] == [True, False]


def test_query_no_nodes_returns_empty_without_degraded(monkeypatch):
    monkeypatch.setattr(LC, "probe_all", lambda *a, **k: {})
    r = LC.query_live_calls(_DB([]))
    assert r["items"] == [] and r["nodes"] == [] and r["degraded"] is False


def test_query_filters_and_pagination(monkeypatch):
    node = _Node("n1", "fs1")
    chans = [_row("u%d" % i, epoch=1789456660 + i) for i in range(3)]
    # dump 里的创建时间必须各不相同，否则排序只能退到 call_uuid，测不出"新呼叫在前"
    dumps = {}
    for i, gw in ((0, "3"), (1, "4"), (2, "3")):
        dumps["u%d" % i] = _dump("u%d" % i, **{
            LC.V_GATEWAY: gw,
            "Caller-Channel-Created-Time": str((1789456660 + i) * 1000000)})
    _stub_probe(monkeypatch, {"n1": (True, chans, dumps, "")})

    r = LC.query_live_calls(_DB([node]), gateway_id=3)
    assert r["total"] == 2 and {i["gateway_id"] for i in r["items"]} == {3}
    # 新呼叫在前：u2(创建时间最大) 先于 u0
    assert [i["call_uuid"] for i in r["items"]] == ["u2", "u0"]


def test_query_server_side_pagination(monkeypatch):
    node = _Node("n1", "fs1")
    chans = [_row("u%d" % i, epoch=1789456660 + i) for i in range(3)]
    dumps = {"u%d" % i: _dump("u%d" % i) for i in range(3)}
    _stub_probe(monkeypatch, {"n1": (True, chans, dumps, "")})
    r = LC.query_live_calls(_DB([node]), page=1, page_size=2)
    assert r["total"] == 3 and r["total_pages"] == 2 and len(r["items"]) == 2
    r2 = LC.query_live_calls(_DB([node]), page=2, page_size=2)
    assert len(r2["items"]) == 1 and r2["page"] == 2
    # 越界页被钳到最后一页（不返空页）
    r3 = LC.query_live_calls(_DB([node]), page=99, page_size=2)
    assert r3["page"] == 2 and len(r3["items"]) == 1


def test_query_carrier_backfilled_from_gateway(monkeypatch):
    """通道变量没带 carrier_id 时，用网关自身的 carrier 兜底（仍在网关表里查得）。"""
    node = _Node("n1", "fs1")
    _stub_probe(monkeypatch, {"n1": (True, [_row("u1")],
                                     {"u1": _dump("u1", **{LC.V_GATEWAY: "3"})}, "")})
    db = _DB([node], [_GW(3, "网关X", 2)], [_Carrier(2, "运营商Y")])
    r = LC.query_live_calls(db)
    assert r["items"][0]["carrier_id"] == 2
    assert r["items"][0]["carrier_name"] == "运营商Y"


def _sql(q) -> str:
    """编译成字面量 SQL 并**归一空白**：SQLAlchemy 会在 WHERE 前换行，直接按 ' WHERE ' 切会切不开。"""
    return " ".join(str(q.compile(compile_kwargs={"literal_binds": True})).split())


def _where(sql: str) -> str:
    return sql.split(" WHERE ", 1)[1] if " WHERE " in sql else ""


def test_query_sql_excludes_known_offline_nodes(monkeypatch):
    monkeypatch.setattr(LC, "probe_all", lambda *a, **k: {})
    db = _DB([_Node("n1", "fs1")])
    LC.query_live_calls(db)
    w = _where(_sql(db.queries[0]))          # 只在 WHERE 里判定，避免误判 SELECT 的列清单
    assert "fs_node.status" in w
    assert "!= 0" in w or "<> 0" in w


def test_query_sql_honours_explicit_node_uuid_without_status_filter(monkeypatch):
    monkeypatch.setattr(LC, "probe_all", lambda *a, **k: {})
    db = _DB([_Node("n1", "fs1", status=0)])
    LC.query_live_calls(db, node_uuid="n1")
    w = _where(_sql(db.queries[0]))
    assert "fs_node.node_uuid" in w and "n1" in w
    assert "fs_node.status" not in w


# ---------------------------------------------------------------------------
# #34 回归锚点：路由注册顺序
# ---------------------------------------------------------------------------
def test_route_registered_before_crud_router():
    """stats 路由必须落在 crud 兜底 /api/{entity}/{item_id} 之前，否则 422（PITFALLS #34）。

    ⚠️ **不能用 `app.router.routes` 的 `r.path` 判定**：FastAPI 0.141 / Starlette 1.6 起，
    `include_router` 把子路由包成 `_IncludedRouter`（**连 path 属性都没有**），
    直接扫 path 会"看不到"任何 include 进来的路由 —— 实测踩过：一度误判成"路由没注册"。
    改用 `app.openapi()["paths"]`：版本无关，且能证明端点真的挂上了。
    """
    import api.app as A
    src = inspect.getsource(A)
    assert src.index("app.include_router(live_calls_router)") < \
        src.index("app.include_router(crud_router)")
    paths = A.app.openapi()["paths"]
    assert "/api/stats/live-calls" in paths
    assert paths["/api/stats/live-calls"].get("get") is not None


def test_sys_config_schema_route_registered_before_generic_crud():
    """schema 路由必须在 crud 的 /{entity}/{item_id} 之前，否则被当成 item_id=schema → 422。"""
    import inspect as _insp
    import api.app as A
    import api.crud as C
    src = _insp.getsource(C)
    assert src.index('@router.get("/sys-config/schema")') < \
        src.index('@router.get("/{entity}/{item_id}")')
    assert "/api/sys-config/schema" in A.app.openapi()["paths"]


# ---------------------------------------------------------------------------
# 空串筛选参数容错（联调抓出的真 bug：前端默认拼 access_point_id= 空串 → 422）
# ---------------------------------------------------------------------------
def test_to_int_or_none_accepts_frontend_empty_string():
    """前端未选筛选时拼出 `access_point_id=`（空串），必须规整成 None（即不过滤）。"""
    assert LC._to_int_or_none("") is None
    assert LC._to_int_or_none(None) is None
    assert LC._to_int_or_none("   ") is None
    assert LC._to_int_or_none("abc") is None          # 非法值也按不过滤处理，不报错
    assert LC._to_int_or_none(5) == 5                  # 已是 int 原样返回
    assert LC._to_int_or_none("7") == 7                # 数字串解析成 int


def test_live_calls_endpoint_tolerates_empty_string_filters(monkeypatch):
    """端点入口收 str：空串筛选不能 422，必须等价于不过滤。

    这是**联调时前端默认 URL（`?node_uuid=&access_point_id=&gateway_id=`）**直接打过来的场景。
    """
    node = _Node("n1", "fs1")
    _stub_probe(monkeypatch, {"n1": (True, [_row("u1")], {"u1": _dump("u1")}, "")})
    r = LC.live_calls(node_uuid="", access_point_id="", gateway_id="",
                      page=1, page_size=50, db=_DB([node]))
    assert r["total"] == 1 and r["degraded"] is False
    assert r["items"][0]["call_uuid"] == "u1"
