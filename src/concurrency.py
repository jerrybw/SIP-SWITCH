"""P2-a 并发原子预留层（D7，2026-09-12）。

设计要点
--------
1. **计数真源 = Redis**。dialplan 选路成功即通过 Lua 原子预留（三档 check-and-increment
   一次完成），杜绝「读快照→判断→写计数」之间的 TOCTOU 竞态（事件驱动近似计数的根治）。
2. **凭证**：`conc:resv:<a_leg_uuid>` = JSON `{"g": gw_id, "a": ap_id, "n": node_uuid}`，
   记录该腿占用/待占用的档位与归属节点。释放/转移均以凭证为准 → 天然幂等（防双释放）。
3. **计数键**：`conc:cnt:global` / `conc:cnt:ap:<id>` / `conc:cnt:gw:<id>`（无 TTL，
   由释放路径与 reconcile 校准维护；凭证带 TTL `concurrency.lease_ttl` 防极端泄漏积累）。
4. **多节点**：Redis 单真源 + 原子 Lua → 天然跨节点一致（两节点共用同一组计数键）。
   凭证带节点字段，reconcile 分片对账：各节点只释放/校准自己负责的差量，期望值按
   全量凭证视图计算 → 多节点并发对账结果一致，误差自收敛。
5. **fail 语义（D3/D4/D5）**：
   - backend=redis（默认）：Redis 不可用时按 `concurrency.fail_open` 决定 ——
     false（默认，D3 fail-close）= 拒绝**新增**呼叫（503 busy_limit_redis）；
     true（D5 逃生开关）= 回落进程内影子计数（esl_client._conc）继续放行。
   - 无论哪种，都**不影响在途呼叫**（D4：只拦新增）。挂断释放失败记日志，靠 reconcile 自愈。
6. **预留生命周期**：dialplan 预留 →（可选）failover 时 transfer 转移 gw 档 →
   A 腿 HANGUP release；HANGUP 事件丢失由 reconcile 释放泄漏凭证并校准计数。
   内线互拨等不经出局选路的呼叫，CHANNEL_CREATE 时 `ensure_leg` 兜底预留 global 档。
"""
import json
import logging

from core.redis_client import client as _redis_client, available as _redis_available

log = logging.getLogger("concurrency")

# ---------------------------------------------------------------------------
# 配置（第1类，config_settings.yaml `concurrency:` 段，进程启动生效）
# ---------------------------------------------------------------------------

def backend() -> str:
    """并发计数后端：redis（默认）| local（旧事件驱动近似计数）。"""
    c = _cfg()
    b = (c.get("backend") or "redis").lower()
    return b if b in ("redis", "local") else "redis"


def fail_open() -> bool:
    """D5 逃生开关：Redis 不可用时 true=回落本地影子计数放行（默认 false=fail-close）。"""
    return bool(_cfg().get("fail_open", False))


def lease_ttl() -> int:
    """预留凭证 TTL（秒）。正常由挂断/对账释放，TTL 仅防极端泄漏积累。"""
    try:
        return max(60, int(_cfg().get("lease_ttl", 86400) or 86400))
    except (TypeError, ValueError):
        return 86400


def _cfg() -> dict:
    from core.config import settings
    c = settings.get("concurrency")
    return c if isinstance(c, dict) else {}


# ---------------------------------------------------------------------------
# Redis key 约定
# ---------------------------------------------------------------------------

K_GLOBAL = "conc:cnt:global"
K_AP = "conc:cnt:ap:%s"
K_GW = "conc:cnt:gw:%s"
K_RESV = "conc:resv:%s"
# 无接入点（话机出局等）时 ap 档占位：凭证 a=0 → 不占/不减 ap 档
AP_NONE = "0"


def _r():
    """拿 Redis 客户端；不可用返回 None（调用方决定 fail 语义）。"""
    try:
        if not _redis_available():
            return None
        return _redis_client()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Lua 脚本（原子性核心）
# ---------------------------------------------------------------------------

# 预留：三档 check-and-increment + 写凭证，一次原子完成。
# 凭证已存在时分两种（事件与 dialplan 到达顺序竞态，实测 2026-09-12）：
#   - 完整预留（无 b 标记）→ 'DUP' 幂等短路；
#   - ensure 兜底凭证（'"b":1'，CHANNEL_CREATE 先于 dialplan 到达所写，只有 global 档）
#     → **升级**：补 INCR gw/ap 档 + 刷新凭证（global 已由 ensure 计过，勿重复）。
# KEYS: [global_cnt, ap_cnt(或占位), gw_cnt, resv]
# ARGV: [g_limit, ap_id(0=无ap), ap_limit, gw_limit, payload_json, ttl]
# 返回: "OK" | "DUP" | "BUSY|<dim>|<cur>|<limit>"
_LUA_RESERVE = """
if redis.call('EXISTS', KEYS[4]) == 1 then
  local cur = redis.call('GET', KEYS[4])
  if string.find(cur, '"b":1', 1, true) then
    local has_ap = (ARGV[2] ~= '0')
    local gwc = tonumber(redis.call('GET', KEYS[3]) or '0')
    local gwl = tonumber(ARGV[4])
    if gwl > 0 and gwc >= gwl then return 'BUSY|gw|' .. gwc .. '|' .. gwl end
    if has_ap then
      local apc = tonumber(redis.call('GET', KEYS[2]) or '0')
      local apl = tonumber(ARGV[3])
      if apl > 0 and apc >= apl then return 'BUSY|ap|' .. apc .. '|' .. apl end
    end
    if has_ap then redis.call('INCR', KEYS[2]) end
    redis.call('INCR', KEYS[3])
    redis.call('SET', KEYS[4], ARGV[5], 'EX', ARGV[6])
    return 'OK'
  end
  return 'DUP'
end
local g = tonumber(redis.call('GET', KEYS[1]) or '0')
local gl = tonumber(ARGV[1])
if gl > 0 and g >= gl then return 'BUSY|global|' .. g .. '|' .. gl end
local has_ap = (ARGV[2] ~= '0')
if has_ap then
  local apc = tonumber(redis.call('GET', KEYS[2]) or '0')
  local apl = tonumber(ARGV[3])
  if apl > 0 and apc >= apl then return 'BUSY|ap|' .. apc .. '|' .. apl end
end
local gwc = tonumber(redis.call('GET', KEYS[3]) or '0')
local gwl = tonumber(ARGV[4])
if gwl > 0 and gwc >= gwl then return 'BUSY|gw|' .. gwc .. '|' .. gwl end
redis.call('INCR', KEYS[1])
if has_ap then redis.call('INCR', KEYS[2]) end
redis.call('INCR', KEYS[3])
redis.call('SET', KEYS[4], ARGV[5], 'EX', ARGV[6])
return 'OK'
"""

# 释放：凭证存在才减（幂等防双释放），减到负数钳 0，最后删凭证。
# b 凭证（ensure 兜底）只占 global 档 → 跳过 gw/ap，防 conc:cnt:gw:0 幽灵键。
# KEYS: [resv, global_cnt, ap_cnt(或占位), gw_cnt]  ARGV: [has_ap(1/0)]
# 返回: 1=已释放 0=无凭证（本就未预留/已释放）
_LUA_RELEASE = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
redis.call('DECR', KEYS[2])
if tonumber(redis.call('GET', KEYS[2]) or '0') < 0 then redis.call('SET', KEYS[2], 0) end
if string.find(v, '"b":1', 1, true) then
  redis.call('DEL', KEYS[1])
  return 1
end
if ARGV[1] == '1' then
  redis.call('DECR', KEYS[3])
  if tonumber(redis.call('GET', KEYS[3]) or '0') < 0 then redis.call('SET', KEYS[3], 0) end
end
redis.call('DECR', KEYS[4])
if tonumber(redis.call('GET', KEYS[4]) or '0') < 0 then redis.call('SET', KEYS[4], 0) end
redis.call('DEL', KEYS[1])
return 1
"""

# 转移（failover 实际落地 gw 与预留不一致时）：decr 旧 gw + incr 新 gw + 刷新凭证。
# b 凭证（ensure 兜底）旧 gw 档未计入 → 跳过 DECR 直接 INCR 新 gw（升级语义）。
# KEYS: [resv, old_gw_cnt, new_gw_cnt]  ARGV: [new_payload, ttl]
# 返回: 1=已转移 0=无凭证
_LUA_TRANSFER_GW = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
if string.find(v, '"b":1', 1, true) then
  redis.call('INCR', KEYS[3])
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
redis.call('DECR', KEYS[2])
if tonumber(redis.call('GET', KEYS[2]) or '0') < 0 then redis.call('SET', KEYS[2], 0) end
redis.call('INCR', KEYS[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

# 兜底预留（内线互拨等不经出局选路的呼叫）：凭证不存在时仅占 global 档。
# KEYS: [resv, global_cnt]  ARGV: [payload, ttl]
# 返回: 1=已兜底 0=凭证已存在（出局预留先行，勿重复）
_LUA_ENSURE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('INCR', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


def _resv_payload(uuid: str, gw_id, ap_id, node: str, bootstrap: bool = False) -> str:
    d = {"u": uuid, "g": int(gw_id or 0), "a": int(ap_id or 0), "n": node}
    if bootstrap:
        d["b"] = 1  # ensure 兜底标记：dialplan 预留到达时升级为完整预留（见 _LUA_RESERVE）
    return json.dumps(d, separators=(",", ":"))


def _parse_payload(raw) -> dict:
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {"g": int(d.get("g") or 0), "a": int(d.get("a") or 0),
                    "n": d.get("n") or "", "u": d.get("u") or ""}
    except (TypeError, ValueError):
        pass
    return {"g": 0, "a": 0, "n": "", "u": ""}


# ---------------------------------------------------------------------------
# 核心 API（esl_client / app 调用）
# ---------------------------------------------------------------------------

def reserve_leg(uuid: str, gw_id, ap_id, g_limit: int, ap_limit: int, gw_limit: int,
                node: str = "") -> dict:
    """dialplan 选路成功后的原子预留（D7）。

    返回：
    - {"ok": True}
    - {"ok": False, "reason": "busy_limit_global|busy_limit_ap|busy_limit_gw",
       "conc": {"global":.., "ap": {ap_id:..}, "gw": {gw_id:..}}}  —— conc 供 _conc_detail
    - {"ok": None} —— Redis 不可用，调用方按 fail_open 决定放行或拒绝
    """
    r = _r()
    if r is None:
        return {"ok": None}
    payload = _resv_payload(uuid, gw_id, ap_id, node)
    try:
        res = r.eval(
            _LUA_RESERVE, 4,
            K_GLOBAL, K_AP % (ap_id or AP_NONE), K_GW % gw_id, K_RESV % uuid,
            int(g_limit or 0), int(ap_id or 0), int(ap_limit or 0), int(gw_limit or 0),
            payload, lease_ttl(),
        )
    except Exception as e:  # noqa: BLE001
        log.error("[conc] reserve failed uuid=%s gw=%s: %s", uuid, gw_id, e)
        return {"ok": None}
    s = str(res)
    if s == "OK":
        return {"ok": True}
    if s == "DUP":
        return {"ok": True}  # 幂等：该腿已有预留凭证（异常重入），不重复加计
    parts = s.split("|")
    dim, cur, lim = parts[1], parts[2], parts[3]
    reason = {"global": "busy_limit_global", "ap": "busy_limit_ap", "gw": "busy_limit_gw"}[dim]
    conc = {"global": 0, "ap": {}, "gw": {}}
    if dim == "global":
        conc["global"] = int(cur)
    elif dim == "ap":
        conc["ap"] = {int(ap_id): int(cur)}
    else:
        conc["gw"] = {int(gw_id): int(cur)}
    return {"ok": False, "reason": reason, "conc": conc,
            "cur": int(cur), "limit": int(lim)}


def release_leg(uuid: str) -> bool:
    """按凭证幂等释放（A 腿 HANGUP / reconcile 泄漏清理共用）。

    凭证不存在（回落窗口的呼叫/已释放）→ 返回 False 且无副作用。
    Redis 不可用 → 返回 False 并记 ERROR（靠 reconcile 自愈）。
    """
    r = _r()
    if r is None:
        return False
    try:
        raw = r.get(K_RESV % uuid)
        if raw is None:
            return False
        p = _parse_payload(raw)
        res = r.eval(
            _LUA_RELEASE, 4,
            K_RESV % uuid, K_GLOBAL, K_AP % (p["a"] or AP_NONE), K_GW % p["g"],
            1 if p["a"] else 0,
        )
        return bool(int(res))
    except Exception as e:  # noqa: BLE001
        log.error("[conc] release failed uuid=%s: %s", uuid, e)
        return False


def transfer_leg(uuid: str, new_gw_id, node: str = "") -> bool:
    """failover 实际落地 gw 与预留不一致时转移 gw 档（幂等：凭证已指向 new_gw 则不动）。

    凭证不存在（回落窗口呼叫）→ False，调用方跳过即可。
    """
    r = _r()
    if r is None:
        return False
    try:
        raw = r.get(K_RESV % uuid)
        if raw is None:
            return False
        p = _parse_payload(raw)
        if p["g"] == int(new_gw_id or 0):
            return True  # 已指向该 gw，幂等
        payload = _resv_payload(uuid, new_gw_id, p["a"], p["n"] or node)
        res = r.eval(
            _LUA_TRANSFER_GW, 3,
            K_RESV % uuid, K_GW % p["g"], K_GW % int(new_gw_id or 0),
            payload, lease_ttl(),
        )
        if int(res):
            log.info("[conc] transferred gw %s->%s uuid=%s", p["g"], new_gw_id, uuid[:8])
        return bool(int(res))
    except Exception as e:  # noqa: BLE001
        log.error("[conc] transfer failed uuid=%s: %s", uuid, e)
        return False


def ensure_leg(uuid: str, node: str = "") -> bool:
    """CHANNEL_CREATE 兜底预留 global 档（内线互拨等无出局选路的呼叫）。

    出局呼叫 dialplan 已预留（凭证在）→ 0，不重复计数。
    Redis 不可用 → False（影子计数兜底，Redis 恢复后 reconcile 校准）。
    """
    r = _r()
    if r is None:
        return False
    try:
        res = r.eval(_LUA_ENSURE, 2, K_RESV % uuid, K_GLOBAL,
                     _resv_payload(uuid, 0, 0, node, bootstrap=True), lease_ttl())
        return bool(int(res))
    except Exception as e:  # noqa: BLE001
        log.error("[conc] ensure failed uuid=%s: %s", uuid, e)
        return False


# ---------------------------------------------------------------------------
# 快照（dialplan 预检 / 展示 / 对账）
# ---------------------------------------------------------------------------

def snapshot(gw_ids=None, ap_id=None):
    """读并发快照（dialplan 预检 + P2-c 重排用）。

    gw_ids 给定时只取相关键（MGET，热路径最小 IO）；否则 SCAN 全量（展示用）。
    Redis 不可用 → None（调用方按 fail_open 决定）。
    """
    r = _r()
    if r is None:
        return None
    out = {"global": 0, "ap": {}, "gw": {}}
    try:
        out["global"] = int(r.get(K_GLOBAL) or 0)
        if gw_ids is not None:
            keys = [K_GW % g for g in gw_ids] + ([K_AP % ap_id] if ap_id else [])
            vals = r.mget(keys) if keys else []
            for g, v in zip(gw_ids or [], vals[:len(gw_ids or [])]):
                out["gw"][g] = int(v or 0)
            if ap_id:
                out["ap"][ap_id] = int(vals[len(gw_ids or [])] or 0)
        else:
            for k in r.scan_iter(match="conc:cnt:gw:*", count=200):
                out["gw"][int(k.split(":")[-1])] = int(r.get(k) or 0)
            for k in r.scan_iter(match="conc:cnt:ap:*", count=200):
                out["ap"][int(k.split(":")[-1])] = int(r.get(k) or 0)
        return out
    except Exception as e:  # noqa: BLE001
        log.error("[conc] snapshot failed: %s", e)
        return None


def reservations():
    """SCAN 全量预留凭证 {uuid: payload}（reconcile 对账用）；Redis 不可用 → None。"""
    r = _r()
    if r is None:
        return None
    out = {}
    try:
        for k in r.scan_iter(match="conc:resv:*", count=200):
            raw = r.get(k)
            if raw is not None:
                out[k.split(":", 2)[-1]] = _parse_payload(raw)
        return out
    except Exception as e:  # noqa: BLE001
        log.error("[conc] reservations scan failed: %s", e)
        return None


def calibrate(expected: dict) -> bool:
    """reconcile 校准：计数与全量凭证视图不一致时 SET 重置（自愈兜底）。

    expected = {"global": int, "ap": {id: int}, "gw": {id: int}}（按凭证统计）。
    多节点各算同一期望（凭证全量视图）→ 并发对账结果一致；短暂竞态误差下一轮自收敛。
    返回是否发生过修正。
    """
    r = _r()
    if r is None:
        return False
    changed = False
    try:
        cur_g = int(r.get(K_GLOBAL) or 0)
        exp_g = int(expected.get("global") or 0)
        if cur_g != exp_g:
            log.warning("[conc] calibrate global %s -> %s", cur_g, exp_g)
            r.set(K_GLOBAL, exp_g)
            changed = True
        for k_prefix, dim in ((K_GW, "gw"), (K_AP, "ap")):
            want = expected.get(dim) or {}
            got = {}
            for k in r.scan_iter(match=k_prefix % "*" if "%s" in k_prefix else k_prefix,
                                 count=200):
                got[int(k.split(":")[-1])] = int(r.get(k) or 0)
            for k_id, v in got.items():
                if want.get(k_id, 0) != v:
                    log.warning("[conc] calibrate %s %s %s -> %s",
                                dim, k_id, v, want.get(k_id, 0))
                    if want.get(k_id, 0) <= 0:
                        r.delete(k_prefix % k_id)
                    else:
                        r.set(k_prefix % k_id, want[k_id])
                    changed = True
            for k_id, v in want.items():
                if k_id not in got and v > 0:
                    log.warning("[conc] calibrate %s %s missing -> %s", dim, k_id, v)
                    r.set(k_prefix % k_id, v)
                    changed = True
        return changed
    except Exception as e:  # noqa: BLE001
        log.error("[conc] calibrate failed: %s", e)
        return False
