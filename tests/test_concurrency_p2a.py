"""P2-a 并发原子预留层测试（src/concurrency.py）。

依赖真实 Redis（DEV compose 的 redis 服务；无 Redis 时整文件 skip ——
CI smoke 与 dev 容器内均可达）。测试用独立 key 前缀，跑前清、跑后清。
覆盖：三档预留/拒绝、幂等（DUP 释放）、防负钳 0、转移、ensure 兜底、
并发竞态（N 线程抢 M 名额）、对账（reservations/calibrate）、fail 语义。
"""
import threading

import pytest

import concurrency as C


def _redis():
    try:
        from core.redis_client import client
        r = client()
        r.ping()
        return r
    except Exception:
        return None


r = _redis()
pytestmark = pytest.mark.skipif(r is None, reason="redis unavailable")


def setup_function(function):
    # 清掉计数与凭证（dev 共享 redis：仅动 conc:* 命名空间）
    for k in list(r.scan_iter(match="conc:cnt:*")) + list(r.scan_iter(match="conc:resv:*")):
        r.delete(k)


def teardown_function(function):
    setup_function(None)


def _cnt(key):
    return int(r.get(key) or 0)


# ---------------------------------------------------------------------------
# 基础预留 / 拒绝
# ---------------------------------------------------------------------------

def test_reserve_increments_all_dims_and_writes_resv():
    res = C.reserve_leg("u1", gw_id=7, ap_id=3, g_limit=0, ap_limit=0, gw_limit=0, node="n1")
    assert res == {"ok": True}
    assert _cnt("conc:cnt:global") == 1
    assert _cnt("conc:cnt:ap:3") == 1
    assert _cnt("conc:cnt:gw:7") == 1
    raw = r.get("conc:resv:u1")
    assert raw is not None
    p = C._parse_payload(raw)
    assert (p["g"], p["a"], p["n"]) == (7, 3, "n1")


def test_reserve_no_ap_occupies_only_global_and_gw():
    res = C.reserve_leg("u2", gw_id=8, ap_id=0, g_limit=0, ap_limit=0, gw_limit=0, node="n1")
    assert res["ok"] is True
    assert _cnt("conc:cnt:global") == 1
    assert _cnt("conc:cnt:gw:8") == 1
    assert r.get("conc:cnt:ap:0") is None  # ap=0 占位键不产生
    # 释放时不减 ap 档
    assert C.release_leg("u2") is True
    assert _cnt("conc:cnt:global") == 0
    assert _cnt("conc:cnt:gw:8") == 0


def test_reserve_gw_limit_rejects_without_side_effect():
    assert C.reserve_leg("a", 7, 0, 0, 0, 2, "n1")["ok"] is True
    assert C.reserve_leg("b", 7, 0, 0, 0, 2, "n1")["ok"] is True
    res = C.reserve_leg("c", 7, 0, 0, 0, 2, "n1")
    assert res["ok"] is False
    assert res["reason"] == "busy_limit_gw"
    assert res["cur"] == 2 and res["limit"] == 2
    # 拒绝必须无副作用：三档计数都不变，也不写凭证
    assert _cnt("conc:cnt:global") == 2
    assert _cnt("conc:cnt:gw:7") == 2
    assert r.get("conc:resv:c") is None


def test_reserve_global_limit_rejects():
    res = C.reserve_leg("g1", gw_id=7, ap_id=0, g_limit=0, ap_limit=0, gw_limit=0, node="n1")
    assert res["ok"] is True
    res = C.reserve_leg("g2", gw_id=8, ap_id=0, g_limit=1, ap_limit=0, gw_limit=0, node="n1")
    assert res["ok"] is False and res["reason"] == "busy_limit_global"
    assert res["cur"] == 1 and res["limit"] == 1
    assert r.get("conc:resv:g2") is None


def test_reserve_ap_limit_rejects():
    assert C.reserve_leg("p1", gw_id=7, ap_id=5, g_limit=0, ap_limit=1, gw_limit=0, node="n1")["ok"] is True
    res = C.reserve_leg("p2", gw_id=8, ap_id=5, g_limit=0, ap_limit=1, gw_limit=0, node="n1")
    assert res["ok"] is False and res["reason"] == "busy_limit_ap"
    assert res["cur"] == 1 and res["limit"] == 1
    # ap 拒绝也不留副作用（gw8 未被占用）
    assert r.get("conc:cnt:gw:8") is None
    assert r.get("conc:resv:p2") is None


def test_reserve_dup_is_idempotent():
    assert C.reserve_leg("d1", 7, 0, 0, 0, 0, "n1")["ok"] is True
    assert C.reserve_leg("d1", 7, 0, 0, 0, 0, "n1")["ok"] is True  # DUP → ok
    assert _cnt("conc:cnt:global") == 1  # 只加计一次


# ---------------------------------------------------------------------------
# 释放 / 转移 / 兜底
# ---------------------------------------------------------------------------

def test_release_idempotent_and_clamps_negative():
    C.reserve_leg("r1", 7, 3, 0, 0, 0, "n1")
    assert C.release_leg("r1") is True
    assert C.release_leg("r1") is False  # 凭证已删：幂等，不再减
    assert _cnt("conc:cnt:global") == 0
    # 计数被 calibrate 重置小于凭证占用 → 释放钳 0 不为负
    C.reserve_leg("r2", 9, 0, 0, 0, 0, "n1")
    r.set("conc:cnt:gw:9", 0)
    assert C.release_leg("r2") is True
    assert _cnt("conc:cnt:gw:9") == 0
    assert _cnt("conc:cnt:global") == 0


def test_release_unknown_uuid_is_noop():
    assert C.release_leg("never-existed") is False
    assert _cnt("conc:cnt:global") == 0


def test_transfer_moves_gw_dim_and_updates_payload():
    C.reserve_leg("t1", 7, 3, 0, 0, 0, "n1")
    assert C.transfer_leg("t1", 8, "n1") is True
    assert _cnt("conc:cnt:gw:7") == 0
    assert _cnt("conc:cnt:gw:8") == 1
    assert _cnt("conc:cnt:ap:3") == 1  # ap 不动
    p = C._parse_payload(r.get("conc:resv:t1"))
    assert p["g"] == 8
    # 幂等：已指向 8 再转移不动
    assert C.transfer_leg("t1", 8, "n1") is True
    assert _cnt("conc:cnt:gw:8") == 1


def test_transfer_without_reservation_is_noop():
    assert C.transfer_leg("t-none", 9, "n1") is False
    assert r.get("conc:cnt:gw:9") is None


def test_ensure_fills_global_only_when_missing():
    # 无凭证：兜底 global
    assert C.ensure_leg("e1", "n1") is True
    assert _cnt("conc:cnt:global") == 1
    p = C._parse_payload(r.get("conc:resv:e1"))
    assert (p["g"], p["a"]) == (0, 0)
    # 已有凭证（出局预留）：不重复
    C.reserve_leg("e2", 7, 0, 0, 0, 0, "n1")
    before = _cnt("conc:cnt:global")
    assert C.ensure_leg("e2", "n1") is False
    assert _cnt("conc:cnt:global") == before


def test_reserve_upgrades_bootstrap_resv_no_double_count():
    """事件先于 dialplan 到达的竞态（实测 BUG 2026-09-12）：
    CHANNEL_CREATE ensure 写 g=0 兜底凭证 → dialplan reserve 必须**升级**补 gw/ap 档，
    global 不重复计；否则 gw 档永久漏计。"""
    assert C.ensure_leg("b1", "n1") is True          # 事件先到：global=1，b 凭证
    assert _cnt("conc:cnt:global") == 1
    assert r.get("conc:cnt:gw:7") is None
    res = C.reserve_leg("b1", gw_id=7, ap_id=5, g_limit=0, ap_limit=0, gw_limit=0, node="n1")
    assert res["ok"] is True                          # 升级成功
    assert _cnt("conc:cnt:global") == 1               # global 不双计
    assert _cnt("conc:cnt:gw:7") == 1                 # gw 档补计
    assert _cnt("conc:cnt:ap:5") == 1                 # ap 档补计
    p = C._parse_payload(r.get("conc:resv:b1"))
    assert p["g"] == 7 and p["a"] == 5                # 凭证已升级为完整预留
    # 升级后挂断：三档全释放
    assert C.release_leg("b1") is True
    assert _cnt("conc:cnt:global") == 0
    assert _cnt("conc:cnt:gw:7") == 0
    assert _cnt("conc:cnt:ap:5") == 0


def test_transfer_on_bootstrap_resv_is_safe():
    """b 凭证在 dialplan 尚未到达时 capture 到 gw（理论时序外，但必须无害）：
    transfer 跳过 gw:0 DECR（升级语义：INCR 新 gw + 刷新凭证），不产生幽灵键。"""
    C.ensure_leg("b2", "n1")
    assert C.transfer_leg("b2", 7, "n1") is True
    assert r.get("conc:cnt:gw:0") is None             # 无幽灵键
    p = C._parse_payload(r.get("conc:resv:b2"))
    assert p["g"] == 7


def test_release_on_pure_bootstrap_resv_no_gw0_ghost():
    """纯兜底凭证（ensure 后未升级直接挂断，如内线互拨）释放只减 global，
    不得 DECR conc:cnt:gw:0 留幽灵键。"""
    C.ensure_leg("b3", "n1")
    assert _cnt("conc:cnt:global") == 1
    assert C.release_leg("b3") is True
    assert _cnt("conc:cnt:global") == 0
    assert r.get("conc:cnt:gw:0") is None             # 无幽灵键
    assert r.get("conc:resv:b3") is None              # 凭证已删


# ---------------------------------------------------------------------------
# 并发竞态（原子性核心价值）
# ---------------------------------------------------------------------------

def test_reserve_upgrade_respects_gw_limit():
    """升级分支也必须过 limit 闸门（9 通并发 E2E 实测缺口 2026-09-12）：
    ensure 兜底凭证在 dialplan reserve 升级时，gw 满应 BUSY 拒绝而非无条件 INCR。"""
    C.ensure_leg("u1", "n1")                          # global=1，b 凭证
    C.ensure_leg("u2", "n1")                          # global=2，b 凭证
    res1 = C.reserve_leg("u1", gw_id=7, ap_id=0, g_limit=0, ap_limit=0, gw_limit=1, node="n1")
    assert res1["ok"] is True                         # 第 1 通升级成功（gw 1/1）
    res2 = C.reserve_leg("u2", gw_id=7, ap_id=0, g_limit=0, ap_limit=0, gw_limit=1, node="n1")
    assert res2["ok"] is False                        # 第 2 通升级被 gw 闸门拒绝
    assert res2["reason"] == "busy_limit_gw"
    assert C._parse_payload(r.get("conc:resv:u2"))["g"] == 0   # 凭证保留为 b 形态
    assert _cnt("conc:cnt:gw:7") == 1
    # 挂断按 b 语义释放：只减 global，无 gw:0 幽灵键
    assert C.release_leg("u2") is True
    assert r.get("conc:cnt:gw:0") is None
    C.release_leg("u1")
    assert _cnt("conc:cnt:global") == 0 and _cnt("conc:cnt:gw:7") == 0


def test_reserve_race_n_threads_m_winners():
    LIMIT = 5
    THREADS = 24
    ok_count = []
    lock = threading.Lock()

    def worker(i):
        res = C.reserve_leg("race-%d" % i, 100, 0, 0, 0, LIMIT, "n1")
        with lock:
            ok_count.append(1 if res.get("ok") else 0)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(ok_count) == LIMIT  # 恰好 LIMIT 个成功，一个不多
    assert _cnt("conc:cnt:gw:100") == LIMIT
    assert _cnt("conc:cnt:global") == LIMIT


# ---------------------------------------------------------------------------
# 快照 / 对账
# ---------------------------------------------------------------------------

def test_snapshot_exact_keys():
    C.reserve_leg("s1", 7, 3, 0, 0, 0, "n1")
    C.reserve_leg("s2", 8, 0, 0, 0, 0, "n1")
    snap = C.snapshot(gw_ids=[7, 8], ap_id=3)
    assert snap["global"] == 2
    assert snap["gw"] == {7: 1, 8: 1}
    assert snap["ap"] == {3: 1}


def test_reservations_and_calibrate():
    C.reserve_leg("c1", 7, 3, 0, 0, 0, "n1")
    C.reserve_leg("c2", 7, 0, 0, 0, 0, "n2")
    resv = C.reservations()
    assert set(resv.keys()) == {"c1", "c2"}
    exp = {"global": 0, "ap": {}, "gw": {}}
    for p in resv.values():
        exp["global"] += 1
        if p["g"]:
            exp["gw"][p["g"]] = exp["gw"].get(p["g"], 0) + 1
        if p["a"]:
            exp["ap"][p["a"]] = exp["ap"].get(p["a"], 0) + 1
    assert exp == {"global": 2, "ap": {3: 1}, "gw": {7: 2}}
    # 人为制造漂移 → calibrate 修正
    r.set("conc:cnt:gw:7", 9)
    r.set("conc:cnt:global", 5)
    r.set("conc:cnt:gw:99", 1)  # 幽灵键
    assert C.calibrate(exp) is True
    assert _cnt("conc:cnt:gw:7") == 2
    assert _cnt("conc:cnt:global") == 2
    assert r.get("conc:cnt:gw:99") is None
    # 一致时不再修
    assert C.calibrate(exp) is False


def test_unavailable_redis_fail_semantics(monkeypatch):
    # Redis 不可用：reserve → ok=None（调用方按 fail_open 决定）；release/ensure → False
    monkeypatch.setattr(C, "_r", lambda: None)
    assert C.reserve_leg("x1", 7, 0, 0, 0, 0, "n1")["ok"] is None
    assert C.release_leg("x1") is False
    assert C.ensure_leg("x1", "n1") is False
    assert C.snapshot() is None
    assert C.reservations() is None
    assert C.calibrate({"global": 0, "ap": {}, "gw": {}}) is False


def test_lease_ttl_written():
    C.reserve_leg("ttl1", 7, 0, 0, 0, 0, "n1")
    ttl = r.ttl("conc:resv:ttl1")
    assert 0 < ttl <= C.lease_ttl()
