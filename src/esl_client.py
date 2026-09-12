"""FreeSWITCH ESL 客户端：订阅事件 -> 更新内存通话状态 -> HANGUP 落 CDR。

注意：
- 本文件是 M1 T-103 骨架。路由/规则校验/故障切换/并发限制（M2 R-201~T-208）
  会接管更复杂的状态机，这里只保证「接通后通话能产出一条符合 PRD §7 口径的 CDR」。
- FS 事件的 header 名随版本略有差异，生产环境需对 variable_*/Event-Date-Timestamp
  实际取值做一次校准（建议用 fs_cli `event plain CHANNEL_HANGUP_COMPLETE` 抓样本）。
- Event-Date-Timestamp 为 Unix 微秒，见 _parse_ts。
- P2（T-206 前置）：在事件流上维护三档实时并发计数器 `_conc`
  （global / 接入点 / 落地网关），供出局并发预检与 `/api/stats/concurrency` 使用。
- P2-a（D7，2026-09-12）：并发计数真源外移 Redis 原子预留（src/concurrency.py）。
  dialplan 选路成功即 Lua 原子预留三档 + 写凭证；CHANNEL_CREATE 兜底（内线等无选路呼叫）；
  HANGUP 按凭证幂等释放；对账线程扩展「泄漏凭证释放 + 全量校准」。
  本文件的 `_conc` 降级为**影子计数**：backend=local 或 Redis 故障 fail-open 回落时使用。
"""
import threading
import json
import os
import glob
import time
from datetime import datetime
import queue
from math import ceil
from decimal import Decimal

from fs_esl_socket import ESLConnection as ESLconnection

import concurrency
from core.config import settings, NODE_UUID
from recordings import to_uri
from db.session import SessionLocal
from db.models import Cdr, SipPhone, AccessPoint, Account, Business, Gateway, Carrier, AccountLedger, CarrierLedger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import insert as mysql_insert

ESL_CFG = settings["esl"]

# call_uuid -> 通话状态。M2 路由引擎会替换为更完整状态机。
_call_store: dict[str, dict] = {}
_store_lock = threading.Lock()

# P2 实时并发计数器（T-206 前置）：三档原子计数。
#   global = 全局并发；ap/gw = owner_id -> 并发数（接入点/落地网关维度）。
# 计数口径：仅对主(A)腿计数，下游(B)腿合并不双计；cdr_* 通道变量到达即补计 ap/gw。
# ⚠️ P2-a（D7）后为**影子计数**：真源在 Redis（concurrency.reserve_leg 原子预留）。
#    影子用途：① backend=local 的主计数 ② Redis 故障 fail-open 回落时的预检快照。
#    影子与 Redis 双轨独立：事件路径两轨各自增减（Redis 增在 dialplan 预留/ensure，
#    影子增在 CHANNEL_CREATE，同秒级窗口），挂断路径两轨各自幂等释放。
_conc: dict = {"global": 0, "ap": {}, "gw": {}}
_conc_lock = threading.Lock()

# ---------------------------------------------------------------------------
# #75 可靠性改造（2026-09-11）：事件异步化 + CDR 攒批落库 + 对账自愈。
# 背景：ESL 事件是 at-most-once（无 ACK/无重放），实测单条 HANGUP_COMPLETE 丢失即导致
# _conc 永久漂移（3dd839b9 案例）+ CDR 骨架无终态。三层防御：
#   a) reader 只入队（有界），worker 线程消费 —— 消费慢不再反压 FS socket 造成丢事件；
#   b) CDR 写入走 writer 线程攒批（50 条/200ms）单事务提交 —— 高并发下落库吞吐数量级提升；
#   c) 对账线程：30s 周期 + ESL 订阅/重连成功即触发，用 ESL `show channels` 快照对照
#      _call_store，把「FS 通道已消失但仍被计数」的腿补减计数并回填 CDR 终态。
# 语义约定：对账只修「过计」方向（会把 limit 小的网关打死）；「欠计」方向无法从通道
# 列表还原 gw/ap 维度，不修。对账回填 hangup_cause：已接通=NORMAL_CLEARING，未接通=
# UNKNOWN（真实原因随事件丢失，无法还原）；计费金额不在对账内重算（待 xml_cdr 真源）。
_EVT_Q = queue.Queue(maxsize=10000)
_EVT_DROPPED = 0
_CDR_Q = queue.Queue(maxsize=10000)
_RECONCILE_REQ = threading.Event()
_RECONCILE_INTERVAL = 30


def _enqueue_event(ev) -> None:
    """reader -> worker 入队；队列满（消费持续不过来）丢弃并计数告警。"""
    global _EVT_DROPPED
    try:
        _EVT_Q.put_nowait(ev)
    except queue.Full:
        _EVT_DROPPED += 1
        if _EVT_DROPPED % 100 == 1:
            print("[ESL] event queue FULL, dropped=%d (worker too slow)" % _EVT_DROPPED, flush=True)


def _event_worker_loop() -> None:
    while True:
        ev = _EVT_Q.get()
        try:
            handle_event(ev)
        except Exception as e:  # noqa: BLE001
            print("[ESL] handle error:", e)


def _enqueue_cdr_job(uuid, job, fallback=None) -> None:
    """CDR 落库任务入队（writer 攒批执行）。队列满时同步兜底执行，绝不丢任务。"""
    try:
        _CDR_Q.put_nowait((uuid, job, fallback))
    except queue.Full:
        try:
            db = SessionLocal()
            try:
                job(db)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            print("[CDR] queue-full sync write failed (uuid=%s): %s" % (uuid, e), flush=True)
            if fallback:
                try:
                    fallback()
                except Exception as e2:
                    print("[CDR] spool fallback failed (uuid=%s): %s" % (uuid, e2), flush=True)


def _cdr_writer_loop(batch_size=50, flush_wait=0.2) -> None:
    """CDR writer：攒批（batch_size 条或 flush_wait 秒）单事务提交。
    失败项退化为逐条重试（各自独立会话，等价旧 _upsert_cdr_dict 行为），最终 spool。"""
    while True:
        batch = [_CDR_Q.get()]
        deadline = time.monotonic() + flush_wait
        while len(batch) < batch_size:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                batch.append(_CDR_Q.get(timeout=remain))
            except queue.Empty:
                break
        failed = []
        db = SessionLocal()
        try:
            for item in batch:
                try:
                    item[1](db)
                except Exception as e:
                    failed.append((item, e))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            failed = [(it, None) for it in batch]
        finally:
            db.close()
        for (uuid, job, fallback), _e in failed:
            _retry_cdr_single(uuid, job, fallback)


def _retry_cdr_single(uuid, job, fallback, attempts=3) -> None:
    for i in range(attempts):
        db = SessionLocal()
        try:
            job(db)
            db.commit()
            return
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            print("[CDR] retry %d/%d failed (uuid=%s): %s" % (i + 1, attempts, uuid, e), flush=True)
            time.sleep(0.5)
        finally:
            db.close()
    if fallback:
        try:
            fallback()
        except Exception as e:
            print("[CDR] spool fallback failed (uuid=%s): %s" % (uuid, e), flush=True)


def _reconcile_loop() -> None:
    while True:
        _RECONCILE_REQ.wait(_RECONCILE_INTERVAL)
        _RECONCILE_REQ.clear()
        try:
            _reconcile_pass()
        except Exception as e:  # noqa: BLE001
            print("[reconcile] error:", e)


def _reconcile_redis_pass(live: set) -> None:
    """P2-a：Redis 并发计数对账（凭证分片释放 + 全量校准自愈）。

    ① 泄漏凭证释放：归属**本节点**（payload.n==NODE_UUID）且 FS 通道已不在 live
       的凭证 → 幂等释放（release_leg 内部 DECR+DEL 原子）。他节点凭证由他节点
       的 reconcile 负责（分片，避免双节点互相误删在途呼叫）。
    ② 全量校准：expected 按**剩余全量凭证**统计（含他节点 → 双节点各算一致），
       与实际计数不等则 SET 重置。多节点并发对账的短暂竞态误差下一轮自收敛。

    live：本节点 FS `show channels` 的 uuid 集合（A/B 腿都在）。
    """
    if concurrency.backend() != "redis":
        return
    resv = concurrency.reservations()
    if resv is None:
        return
    leaked = [u for u, p in resv.items()
              if (p.get("n") or "") == NODE_UUID and u not in live]
    released = []
    for u in leaked:
        if concurrency.release_leg(u):
            released.append(u)
    cur = concurrency.reservations()
    if cur is None:
        cur = {u: p for u, p in resv.items() if u not in set(released)}
    exp = {"global": 0, "ap": {}, "gw": {}}
    for p in cur.values():
        exp["global"] += 1
        g = p.get("g") or 0
        a = p.get("a") or 0
        if g:
            exp["gw"][g] = exp["gw"].get(g, 0) + 1
        if a:
            exp["ap"][a] = exp["ap"].get(a, 0) + 1
    if concurrency.calibrate(exp):
        print("[reconcile] redis concurrency calibrated (global=%d gw=%s ap=%s)" % (
            exp["global"], exp["gw"], exp["ap"]), flush=True)
    if released:
        print("[reconcile] released %d leaked reservation(s): %s" % (
            len(released), [u[:8] for u in released]), flush=True)


def _reconcile_pass() -> None:
    """用 ESL `show channels` 快照对照 _call_store / Redis 凭证，自愈计数与 CDR 终态。

    - 影子层（#75）：修复「通道已消失但仍被影子计数」的腿 + 回填 CDR。
    - Redis 层（P2-a）：泄漏凭证释放 + 全量校准。两轮共用同一 live 快照。
    """
    from fs_esl_cmd import esl_api
    raw = esl_api("show channels")
    if raw is None:
        return  # ESL 查询不可用：宁可不修也不猜
    live = set()
    lines = [l for l in (raw or "").splitlines() if l.strip()]
    if len(lines) > 1:
        import csv
        import io as _io
        for row in csv.DictReader(_io.StringIO("\n".join(lines))):
            u = (row.get("uuid") or "").strip()
            if u:
                live.add(u)
    # P2-a：Redis 层对账（每轮都跑，泄漏无影子记录时也能修——如进程重启遗留凭证）
    try:
        _reconcile_redis_pass(live)
    except Exception as e:  # noqa: BLE001
        print("[reconcile] redis pass error:", e, flush=True)
    leaks = []
    with _store_lock:
        for u, rec in list(_call_store.items()):
            if rec.get("_merged_into") or not rec.get("_counted_global"):
                continue
            if u not in live and not rec.get("_dec_done"):
                leaks.append((u, rec))
    if not leaks:
        return
    end_time = datetime.utcnow()
    fixed = []
    for u, rec in leaks:
        with _store_lock:
            if _call_store.get(u) is not rec:
                continue  # 并发窗口：挂断路径已处理
            _call_store.pop(u, None)
        _dec_count_leg(rec)
        _finalize_lost_cdr(u, rec, end_time)
        fixed.append(u)
    print("[reconcile] healed %d lost-hangup leg(s): %s" % (
        len(fixed), [u[:8] for u in fixed]), flush=True)


def _finalize_lost_cdr(call_uuid: str, rec: dict, end_time) -> None:
    """对账回填：仅当该 uuid 的 CDR 终态缺失（hangup_cause IS NULL）时补终态。
    计费（cost/扣费）不在对账内重算 —— P0 只保证账本与计数自愈，金额兜底待 xml_cdr 真源。"""
    talk = None
    bill = 0
    if rec.get("answer_time") and end_time:
        talk = int((end_time - rec["answer_time"]).total_seconds())
        bu = rec.get("bill_unit") or 60
        if talk > 0:
            bill = ceil(talk / bu) * bu
    cause = "NORMAL_CLEARING" if rec.get("answer_time") else "UNKNOWN"
    try:
        db = SessionLocal()
        try:
            db.execute(
                update(Cdr)
                .where(Cdr.uuid == call_uuid, Cdr.hangup_cause.is_(None))
                .values(end_time=end_time, hangup_cause=cause,
                        talk_duration=talk, bill_duration=bill,
                        created_at=datetime.utcnow())
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print("[reconcile] finalize CDR failed (uuid=%s): %s" % (call_uuid, e), flush=True)


def start_esl_background_workers() -> None:
    """main.py 启动时调用：事件 worker / CDR writer / 对账线程各一条（daemon）。"""
    threading.Thread(target=_event_worker_loop, daemon=True, name="esl-event-worker").start()
    threading.Thread(target=_cdr_writer_loop, daemon=True, name="cdr-writer").start()
    threading.Thread(target=_reconcile_loop, daemon=True, name="esl-reconcile").start()
    print("[esl] background workers started (event-worker/cdr-writer/reconcile=%ds)" % _RECONCILE_INTERVAL, flush=True)


def _parse_ts(value):
    """FS Event-Date-Timestamp 为 Unix 微秒字符串。"""
    try:
        if not value:
            return None
        ts = float(value)
        if ts > 1e12:
            ts /= 1e6
        return datetime.utcfromtimestamp(ts)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_switch_detail(raw):
    """T-205：把网关下发的扁平串 ';gid:num[:conc[:clim]]:cause;...' 解析为 JSON 数组入库。

    元素：{gateway_id, callee_out, cause[, conc_gw, conc_limit]}；cause=WIN 表示该腿接通胜出。

    P2-c 扩展（2026-09-10）：本腿的并发快照打在同一元素里（放在 callee_out 与 cause 之间），
    **向后兼容**——老记录是 3 段（无并发），新记录是 5 段（带并发）。
    因此这里不能按固定下标取 cause，必须**从右往左**解析：最后一段恒为 cause，
    中间剩下的段位按位置映射。concurrent_limit=0 表示「不限」。
    rec.get("switch_detail") 可能是原始扁平串或已解析的 list/dict（幂等）。
    """
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    s = raw.strip().lstrip(";")
    if not s:
        return None
    out = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        seg = part.split(":")
        gid = seg[0] if len(seg) > 0 else None
        cause = seg[-1] if len(seg) > 1 else None
        mid = seg[1:-1] if len(seg) > 2 else []
        num = mid[0] if len(mid) > 0 else None
        conc_gw = mid[1] if len(mid) > 1 else None
        conc_limit = mid[2] if len(mid) > 2 else None
        item = {
            "gateway_id": int(gid) if gid and gid.isdigit() else gid,
            "callee_out": num,
            "cause": cause,
        }
        # 仅在新格式（带并发段）时才附加这两个键，保持老记录结构不变
        if conc_gw is not None:
            item["conc_gw"] = _safe_int(conc_gw) if conc_gw != "" else None
        if conc_limit is not None:
            item["conc_limit"] = _safe_int(conc_limit) if conc_limit != "" else None
        out.append(item)
    return out if out else None


def _upsert_call(call_uuid: str, **fields):
    with _store_lock:
        rec = _call_store.setdefault(call_uuid, {"uuid": call_uuid})
        rec.update(fields)


def _maybe_count_leg(leg_uuid: str) -> None:
    """影子计数（P2-a 后仅作 local 模式主计数 / fail-open 回落快照）：
    对主(A)腿做一次并发计数（幂等，靠 _counted_* 标志防止重复）：
    - global 必定 +1；
    - 若该腿已带 cdr_access_point_id / cdr_gateway_id 则对应维度 +1
      （cdr_* 变量可能迟到，在后续携带该变量的事件里补计）。
    Redis 真源不在本函数：预留写点在 dialplan（concurrency.reserve_leg），
    兜底写点在 CHANNEL_CREATE（ensure_leg），见 handle_event。
    """
    rec = _call_store.get(leg_uuid)
    if rec is None:
        return
    # 被合并的下游(B)腿不计数（防 A-leg 已先挂断 pop 后 B-leg HANGUP 误判）。
    if rec.get("_merged_into"):
        return
    with _conc_lock:
        if not rec.get("_counted_global"):
            _conc["global"] += 1
            rec["_counted_global"] = True
        ap = rec.get("access_point_id")
        if ap and not rec.get("_counted_ap"):
            _conc["ap"][ap] = _conc["ap"].get(ap, 0) + 1
            rec["_counted_ap"] = True
        gw = rec.get("gateway_id")
        if gw and not rec.get("_counted_gw"):
            _conc["gw"][gw] = _conc["gw"].get(gw, 0) + 1
            rec["_counted_gw"] = True


def _dec_count_leg(rec: dict) -> None:
    """主(A)腿挂断时按已计维度 -1（与 _maybe_count_leg 对称）。

    P2-a：同时释放 Redis 预留（concurrency.release_leg，凭证校验幂等——
    凭证不存在/已释放/Redis 不可用均无副作用或仅记日志，靠对账自愈）。
    """
    # Redis 释放放锁外：网络 IO 不持 _conc_lock（失败靠 reconcile，不阻塞事件流）
    if concurrency.backend() == "redis":
        concurrency.release_leg(rec.get("uuid") or "")
    with _conc_lock:
        if rec.get("_dec_done"):
            return  # #75：幂等减计（挂断路径与对账线程可能都触发）
        rec["_dec_done"] = True
        if rec.get("_counted_global"):
            _conc["global"] = max(0, _conc["global"] - 1)
        ap = rec.get("access_point_id")
        if ap and rec.get("_counted_ap"):
            _conc["ap"][ap] = max(0, _conc["ap"].get(ap, 0) - 1)
            if _conc["ap"][ap] <= 0:
                _conc["ap"].pop(ap, None)
        gw = rec.get("gateway_id")
        if gw and rec.get("_counted_gw"):
            _conc["gw"][gw] = max(0, _conc["gw"].get(gw, 0) - 1)
            if _conc["gw"][gw] <= 0:
                _conc["gw"].pop(gw, None)


def get_concurrency() -> dict:
    """并发快照：backend=redis 优先取 Redis 真源（P2-a）；不可用回落影子计数。

    展示接口 /api/stats/concurrency 与 local 模式预检共用。
    dialplan 预检主路径用 concurrency.snapshot(gw_ids=...) 精确取键（app.py）。
    """
    if concurrency.backend() == "redis":
        snap = concurrency.snapshot()
        if snap is not None:
            return snap
    with _conc_lock:
        return {"global": _conc["global"], "ap": dict(_conc["ap"]), "gw": dict(_conc["gw"])}


def _debug_dump(leg_uuid, rec, event):
    """T-207 诊断：把 A-leg HANGUP 时的关键变量落盘，便于通道变量偶发缺失时定位。"""
    try:
        with open("/tmp/sip_gw_debug.log", "a") as f:
            f.write(
                "HANGUP_A leg=%s gw=%r ca=%r ap=%r bu=%r rec=%r\n"
                % (
                    leg_uuid,
                    event.getHeader("variable_cdr_gateway_id"),
                    event.getHeader("variable_cdr_carrier_id"),
                    event.getHeader("variable_cdr_access_point_id"),
                    event.getHeader("variable_cdr_bill_unit"),
                    event.getHeader("variable_rec_file")
                    or event.getHeader("variable_record_path")
                    or event.getHeader("variable_record_file"),
                )
            )
            f.write("  rec_keys=%s\n" % sorted(rec.keys()))
            f.write(
                "  rec_gw=%r rec_ca=%r rec_ap=%r rec_path=%r rec_status=%r\n"
                % (
                    rec.get("gateway_id"),
                    rec.get("carrier_id"),
                    rec.get("access_point_id"),
                    rec.get("record_path"),
                    rec.get("record_status"),
                )
            )
    except Exception:
        pass


def handle_event(event) -> None:
    if event.getHeader("Event-Name") == "CUSTOM":
        _handle_sofia_reg(event)
        _handle_gateway_state(event)
        return
    etype = event.getHeader("Event-Name")
    # 用 Unique-ID 作为通道唯一键（最可靠），variable_call_uuid 仅兜底。
    leg_uuid = event.getHeader("Unique-ID") or event.getHeader("variable_call_uuid")
    if not leg_uuid:
        return
    # 桥接产生的下游(B)腿会带 Other-Leg-Unique-ID 指向主(A)腿。
    other_uuid = event.getHeader("Other-Leg-Unique-ID")
    # P2：判定当前事件是否属于「被合并的下游(B)腿」——B 腿的 Other-Leg 指向主(A)腿
    # （且 A 腿不是 merged 记录）。B 腿不计入并发，避免 A/B 双计。
    is_b_leg = (
        bool(other_uuid)
        and other_uuid != leg_uuid
        and other_uuid in _call_store
        and not _call_store.get(other_uuid, {}).get("_merged_into")
    )

    # T-207：路由贯通变量(cdr_*)与录音路径(rec_file)由 dialplan 经 `set` 写在 A-leg 上，
    # B-leg 不继承。任一事件只要携带它们就缓存进 _call_store，并同步给对端腿，
    # 避免依赖 HANGUP 事件一定携带这些变量（实测 A-leg HANGUP 偶发不含 variable_*）。
    cdr_gw = _safe_int(event.getHeader("variable_cdr_gateway_id"))
    cdr_ca = _safe_int(event.getHeader("variable_cdr_carrier_id"))
    cdr_ap = _safe_int(event.getHeader("variable_cdr_access_point_id"))
    cdr_acct = _safe_int(event.getHeader("variable_cdr_account_id"))  # 拦截呼叫显式下发的归属账户
    cdr_bu = _safe_int(event.getHeader("variable_cdr_bill_unit"))
    cdr_sc = _safe_int(event.getHeader("variable_cdr_switch_count"))
    cdr_ct = event.getHeader("variable_cdr_caller_type")
    cdr_dip = event.getHeader("variable_cdr_dst_ip")
    cdr_dport = _safe_int(event.getHeader("variable_cdr_dst_port"))
    cdr_cmid = event.getHeader("variable_cdr_caller_mid")
    cdr_ccmid = event.getHeader("variable_cdr_callee_mid")
    cdr_sd = event.getHeader("variable_cdr_switch_detail")  # T-205：故障切换尝试序列(扁平串)
    rec_file = (
        event.getHeader("variable_record_path")
        or event.getHeader("variable_record_file")
        or event.getHeader("variable_rec_file")
    )
    if (
        cdr_gw is not None
        or cdr_ca is not None
        or cdr_ap is not None
        or cdr_acct is not None
        or cdr_bu is not None
        or cdr_sc is not None
        or cdr_sd
        or rec_file
        or cdr_dip is not None
        or cdr_dport is not None
        or cdr_cmid is not None
        or cdr_ccmid is not None
    ):
        _upsert_call(
            leg_uuid,
            gateway_id=cdr_gw,
            carrier_id=cdr_ca,
            access_point_id=cdr_ap,
            account_id=cdr_acct,
            bill_unit=cdr_bu or 60,
            switch_count=cdr_sc,
            caller_type=cdr_ct,
            dest_ip=cdr_dip,
            dest_port=cdr_dport,
            caller_mid=cdr_cmid,
            callee_mid=cdr_ccmid,
            switch_detail=cdr_sd,
        )
        if rec_file:
            # #70：FS 上报的是**容器内绝对路径**，落库前统一转成可迁移 URI
            # （local://<node_uuid>/<file>）。列名 record_path 保留，语义升格为 URI，
            # 老数据是裸路径 → 读取端按隐式 local:// 兼容，故无需 migration。
            _upsert_call(leg_uuid, record_status=1,
                         record_path=to_uri(rec_file, NODE_UUID))
        # 同步给对端腿（被合并的 B-leg），保证其落库前也能带上这些值。
        if other_uuid and other_uuid != leg_uuid:
            _upsert_call(
                other_uuid,
                gateway_id=cdr_gw,
                carrier_id=cdr_ca,
                access_point_id=cdr_ap,
                account_id=cdr_acct,
                bill_unit=cdr_bu or 60,
                dest_ip=cdr_dip,
                dest_port=cdr_dport,
                caller_mid=cdr_cmid,
                callee_mid=cdr_ccmid,
                switch_detail=cdr_sd,
            )
            if rec_file:
                _upsert_call(other_uuid, record_status=1,
                             record_path=to_uri(rec_file, NODE_UUID))
        # P2 并发计数：仅对主(A)腿计数，下游(B)腿跳过（避免双计）。
        if not is_b_leg:
            _maybe_count_leg(leg_uuid)
            # P2-a：cdr_gateway_id 已到达（实际落地 gw）→ 与预留凭证不一致则转移
            # （T-205 failover 换腿场景：预留的是 candidates[0]，实际 bridge 成功的可能
            #   是第 N 腿）。幂等：凭证已指向该 gw 则不动。
            if (concurrency.backend() == "redis" and rec
                    and rec.get("gateway_id")):
                try:
                    concurrency.transfer_leg(leg_uuid, rec["gateway_id"], NODE_UUID)
                except Exception as _te:
                    print("[conc] transfer error uuid=%s: %s" % (leg_uuid[:8], _te), flush=True)

    # 网关在拒绝时写入的通道变量，随事件透传，落 CDR.reject_reason。
    reject_reason = event.getHeader("variable_sip_gateway_reject_reason")
    if reject_reason:
        _upsert_call(leg_uuid, reject_reason=reject_reason)

    if etype == "CHANNEL_CREATE":
        # 下游(B)腿：把它的主被叫记为主腿的「呼出主被叫」，并把自身标记为已合并，
        # 避免 B 腿 HANGUP 再落一条 CDR（PRD R-602 一条通话一条 CDR）。
        if other_uuid and other_uuid != leg_uuid and other_uuid in _call_store:
            _upsert_call(
                other_uuid,
                caller_out=event.getHeader("Caller-Caller-ID-Number"),
                callee_out=event.getHeader("Caller-Destination-Number"),
            )
            with _store_lock:
                _call_store[leg_uuid] = {"uuid": leg_uuid, "_merged_into": other_uuid}
            return
        _upsert_call(
            leg_uuid,
            source_ip=event.getHeader("variable_sip_network_ip"),
            source_port=_safe_int(event.getHeader("variable_sip_network_port")),
            start_time=_parse_ts(event.getHeader("Event-Date-Timestamp")),
            caller_in=event.getHeader("Caller-Caller-ID-Number"),
            callee_in=event.getHeader("Caller-Destination-Number"),
        )
        print("[esl-create] leg=%s caller=%r callee=%r requri=%r" % (
            leg_uuid,
            event.getHeader("Caller-Caller-ID-Number"),
            event.getHeader("Caller-Destination-Number"),
            event.getHeader("variable_sip_req_uri")), flush=True)
        # P2：确保主(A)腿至少计入 global（ap/gw 待 cdr_* 变量到达后补计）。
        _maybe_count_leg(leg_uuid)
        # P2-a：Redis 兜底预留 —— 出局呼叫 dialplan 已预留（凭证在 → 0 不重复）；
        # 内线互拨等不经出局选路的呼叫在此补占 global 档。Redis 不可用静默跳过
        # （影子计数兜底，Redis 恢复后 reconcile 校准）。
        if concurrency.backend() == "redis":
            try:
                concurrency.ensure_leg(leg_uuid, NODE_UUID)
            except Exception as _ee:
                print("[conc] ensure error uuid=%s: %s" % (leg_uuid[:8], _ee), flush=True)
    elif etype in ("CHANNEL_PROGRESS", "CHANNEL_PROGRESS_MEDIA"):
        # 180 / 183 均记为振铃时间（PRD：收到 180/183 即记）
        _upsert_call(leg_uuid, ring_time=_parse_ts(event.getHeader("Event-Date-Timestamp")))
    elif etype == "CHANNEL_ANSWER":
        _upsert_call(leg_uuid, answer_time=_parse_ts(event.getHeader("Event-Date-Timestamp")))
    elif etype == "CHANNEL_BRIDGE":
        # 出局段主被叫写回主(A)腿（若有对端），否则写本腿。
        target = other_uuid if (other_uuid and other_uuid in _call_store) else leg_uuid
        _upsert_call(
            target,
            caller_out=event.getHeader("Caller-Caller-ID-Number"),
            callee_out=event.getHeader("Caller-Destination-Number"),
        )
    elif etype == "CHANNEL_HANGUP_COMPLETE":
        with _store_lock:
            rec = _call_store.get(leg_uuid)
        if rec is None:
            # 403 等早期拒绝（CS_NEW 无 CHANNEL_CREATE 写入）：用事件头落 minimal CDR
            ctx = {}
            try:
                from api.directory_xml import _sip_call_ctx
                cid = event.getHeader("variable_sip_call_id") or event.getHeader("sip_call_id")
                ctx = _sip_call_ctx.get(cid) or {}
            except Exception:
                cid = None
            rec = {
                "uuid": leg_uuid,
                "caller_in": (event.getHeader("Caller-Caller-ID-Number")
                              or event.getHeader("Caller-Username")
                              or event.getHeader("variable_sip_from_user")
                              or ctx.get("caller") or ""),
                "callee_in": (event.getHeader("Caller-Destination-Number")
                              or event.getHeader("variable_sip_req_user")
                              or event.getHeader("variable_sip_to_user")
                              or ctx.get("callee") or ""),
                "source_ip": event.getHeader("variable_sip_network_ip"),
                "source_port": _safe_int(event.getHeader("variable_sip_network_port")),
                "start_time": _parse_ts(event.getHeader("Event-Date-Timestamp")),
            }
            try:
                _nm = [h for h in event.getHeaderNames()
                       if "sip" in h.lower() or "dest" in h.lower() or "call" in h.lower()]
                print("[esl403] leg=%s cid=%r ctx=%r heads=%s" % (
                    leg_uuid, cid, ctx, {h: event.getHeader(h) for h in _nm}), flush=True)
            except Exception:
                pass
            _save_cdr(leg_uuid, rec, event)
            return
        # 下游(B)腿：不落库，直接丢弃。结束时间以主(A)腿自身 HANGUP 时间戳为准，
        # 不再回填 B 腿时间——故障切换会产生多个 B 腿，首个失败腿的挂断时间早于真实
        # 应答时间，回填会把 end_time 污染成「呼叫开始时间」（见 84c2d228 案例）。
        if rec.get("_merged_into"):
            with _store_lock:
                _call_store.pop(leg_uuid, None)
            return
        # 主(A)腿落库；cdr_* 与录音路径已在上方 capture 块缓存进 rec，直接沿用。
        _debug_dump(leg_uuid, rec, event)
        # P2：主(A)腿挂断，按已计维度减计并发。
        # #75：dec+pop 原子化 + 身份校验，与对账线程互斥（防双重减计/误清）。
        with _store_lock:
            if _call_store.get(leg_uuid) is rec:
                _dec_count_leg(rec)
                _call_store.pop(leg_uuid, None)
        _save_cdr(leg_uuid, rec, event)


def _hangup_direction(rec: dict, event) -> int:
    """话单挂断方向（2026-09-03 用户规则）：0=服务器 / 1=主叫 / 2=被叫 / 3=其他。

    A 腿 HANGUP_COMPLETE 事件 best-effort 判定：
    - 接通后：A 腿收到主叫 BYE(sip_hangup_disposition/term 含 recv_bye) → 主叫(1)；
      bridge 由对端(被叫)结束、A 腿被收尾挂断 → 被叫(2)。（平台暂无接通后服务器主动
      拆线操作；如后续加管理拆线再按 send_bye 细分 0。）
    - 未接通：规则拒绝(reject_reason)/CALL_REJECTED(603)/NO_ROUTE/未出局 → 服务器(0)；
      出局振铃中 ORIGINATOR_CANCEL → 主叫取消(1)；其余出局后被叫异常信令/拆线 → 被叫(2)。
    兜底 3(其他)基本不触发。样本可再校准。
    """
    cause = (event.getHeader("Hangup-Cause") or "").upper()
    disp = ((event.getHeader("variable_sip_hangup_disposition") or "")
            + " " + (event.getHeader("variable_sip_term_status") or "")).lower()
    answered = bool(rec.get("answer_time"))
    if answered:
        if "recv_bye" in disp:
            return 1  # 主叫 BYE
        return 2      # 对端(被叫)先挂 / bridge 结束
    if rec.get("reject_reason"):
        return 0      # 业务规则拒绝 → 服务器
    if cause in ("CALL_REJECTED", "NO_ROUTE_DESTINATION"):
        return 0      # 限制 603 / 无路由 → 服务器
    if not (rec.get("gateway_id") or rec.get("callee_out") or rec.get("switch_detail")):
        return 0      # 未出局：服务器未路由成功 / 并发 503 等
    if cause == "ORIGINATOR_CANCEL":
        return 1      # 出局振铃中主叫取消
    return 2          # 出局后被叫异常信令 / 被叫拆线



def _resolve_dest_endpoint(gateway_id, dest_ip, dest_port):
    """以最终胜出的 gateway_id 为准校正落地 IP/端口（2026-09-08）。

    dialplan 已逐腿下发 cdr_dst_ip/cdr_dst_port（与 cdr_gateway_id 同腿覆盖），
    但历史上（或 B 腿事件变量缺失时）dest_ip 可能停留在首选网关 / 为 None。
    落库前按最终 gateway_id 回查 gateway 表再校正一次，保证
    **gateway_id 与 dest_ip/dest_port 永远指向同一个落地网关**。
    gateway 行缺失或 ip/port 为空时回退到事件携带值。
    """
    if not gateway_id:
        return dest_ip, dest_port
    db = None
    try:
        db = SessionLocal()
        gw = db.get(Gateway, gateway_id)
        if gw is not None:
            if getattr(gw, "ip", None):
                dest_ip = gw.ip
            if getattr(gw, "port", None):
                dest_port = gw.port
    except Exception as e:
        print("[cdr] resolve dest endpoint failed (gw=%s): %s" % (gateway_id, e))
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return dest_ip, dest_port

def _save_cdr(call_uuid: str, rec: dict, event) -> None:
    # 结束时间以主(A)腿自身 HANGUP 事件时间戳为准（B 腿已不回填，见 handle_event），
    # 仅当事件时间戳缺失时才回退 rec 里可能残留的 end_time。
    end_time = _parse_ts(event.getHeader("Event-Date-Timestamp")) or rec.get("end_time")
    talk = None
    bill = 0
    if rec.get("answer_time") and end_time:
        talk = int((end_time - rec["answer_time"]).total_seconds())
        bill_unit = rec.get("bill_unit") or 60
        if talk > 0:
            bill = ceil(talk / bill_unit) * bill_unit

    # v0.3：当通消费(收入侧) + 当通成本(成本侧) + 账户维度解析（仅接通计费；费率链见 _compute_billing）
    cost, rate_used, account_id, business_id, customer_id, cost_price, cost_rate_used, cost_bill_unit = _compute_billing(rec, talk, bill)
    # dialplan 阶段拦截（预付费余额不足 603 等）显式下发 cdr_account_id：无 bridge、_compute_billing
    # 无从按接入点推导账户，故此处以显式值为准（正常路由呼叫不会下发该变量，回落到推导值）。
    if rec.get("account_id") is not None:
        account_id = rec.get("account_id")

    # T-205：switch_detail = 全部尝试过的网关腿序列(含胜出腿)；真实切换次数 =
    # 腿数 - 1（非「失败腿数」）。网关下发的 cdr_switch_count(=gw_failover_count)
    # 是「失败腿数」，语义不对，故这里以 switch_detail 条数反推，二者不一致时以条数为准。
    #
    # 胜出腿兜底（d08211a7 主叫先挂场景）：成功接通（A 腿已 answer）但 detail 缺 WIN 段——
    # 主叫先挂时 A 腿自身先进入 HANGUP，bridge 后置 set 无机会执行（FS 通道挂断后不再跑
    # dialplan），WIN 只能在此兜底：A 腿 cdr_gateway_id/callee_out 的最后一次赋值即胜出
    # 网关（head_sets 每腿覆盖 + CHANNEL_BRIDGE 写被叫）。被叫挂场景 dialplan 已记 WIN →
    # 有 WIN 段则跳过（幂等）；全失败场景 answer_time 为空 → 不补。
    switch_detail = _parse_switch_detail(rec.get("switch_detail"))
    if (
        rec.get("answer_time")
        and rec.get("gateway_id")
        and rec.get("callee_out")
        and not (
            isinstance(switch_detail, list)
            and any(isinstance(d, dict) and d.get("cause") == "WIN" for d in switch_detail)
        )
    ):
        if not isinstance(switch_detail, list):
            switch_detail = []
        # 兜底补的 WIN 腿没有 dialplan 路径上的并发段（本腿值未知）→ 不塞 conc_* 键，
        # 与老格式元素保持一致；前端按「键存在才展示」处理，避免显示 0 造成误解。
        switch_detail = switch_detail + [{
            "gateway_id": int(rec["gateway_id"])
            if isinstance(rec["gateway_id"], int)
            else rec["gateway_id"],
            "callee_out": rec["callee_out"],
            "cause": "WIN",
        }]
    switch_count = (
        len(switch_detail) - 1
        if isinstance(switch_detail, list)
        else (rec.get("switch_count") or 0)
    )

    # 落地 IP/端口与最终胜出网关对齐（故障切换后 gateway_id 是切换后的值，
    # dest_ip 必须同步跟随，口径见 _resolve_dest_endpoint）。
    dest_ip, dest_port = _resolve_dest_endpoint(
        rec.get("gateway_id"), rec.get("dest_ip"), rec.get("dest_port")
    )

    cdr = Cdr(
        uuid=call_uuid,
        caller_in=rec.get("caller_in") or "",
        callee_in=rec.get("callee_in") or "",
        caller_mid=rec.get("caller_mid"),
        callee_mid=rec.get("callee_mid"),
        caller_out=rec.get("caller_out"),
        callee_out=rec.get("callee_out"),
        # 事件未携带 start_time 时复用预落库的时间（NULL 会让唯一键失效→重复行）
        start_time=_existing_start_time(call_uuid) or rec.get("start_time") or end_time,
        ring_time=rec.get("ring_time"),
        answer_time=rec.get("answer_time"),
        end_time=end_time,
        talk_duration=talk,
        bill_unit=rec.get("bill_unit", 60),
        bill_duration=bill,
        gateway_id=rec.get("gateway_id"),
        carrier_id=rec.get("carrier_id"),
        access_point_id=rec.get("access_point_id"),
        customer_id=customer_id,
        account_id=account_id,
        business_id=business_id,
        source_ip=rec.get("source_ip"),
        source_port=rec.get("source_port"),
        dest_ip=dest_ip,
        dest_port=dest_port,
        # caller_type 列 NOT NULL DEFAULT ''；rec 中该字段可能未下发(为 None)，
        # 显式传 None 会触发 IntegrityError 且 DB 默认不生效，故兜底为空串。
        caller_type=rec.get("caller_type") or "",
        hangup_cause=event.getHeader("Hangup-Cause"),
        hangup_direction=_hangup_direction(rec, event),
        sip_code=_safe_int(
            event.getHeader("variable_sip_term_status")
            or event.getHeader("variable_sip_invite_failure_status")
        ),
        sip_invite_failure_status=event.getHeader("variable_sip_invite_failure_status"),
        reject_reason=rec.get("reject_reason"),
        switch_count=switch_count,
        switch_detail=switch_detail,
        record_status=rec.get("record_status", 0),
        # #70：兜底再规范一次（to_uri 幂等）。正常路径上 record_path 在采集时已是 URI，
        # 这里只是保证「无论从哪条分支写入」落库值都符合契约。
        record_path=to_uri(rec.get("record_path"), NODE_UUID),
        cost=cost,
        rate_used=rate_used,
        # v0.3 成本侧落库（与收入同事务算出）
        cost_price=cost_price,
        cost_rate_used=cost_rate_used,
        cost_bill_unit=cost_bill_unit,
        profit=cost - cost_price,
        # billed 列 NOT NULL DEFAULT 0；显式传 0，避免 getattr 未赋值时为 None → IntegrityError(1048)，
        # 导致 _persist_cdr 全列构造 vals 时把 None 写进 INSERT（DEFAULT 仅在列被省略时生效）。
        billed=0,
        fs_node_uuid=NODE_UUID,
        # 终态覆盖 created_at：以落终态的当前时间写入（用户预期；不再沿用阶段①的呼叫起始时间，
        # 否则 created_at 会早于挂断时间）。upsert 的 ON DUPLICATE KEY UPDATE 已放开 created_at 列。
        created_at=datetime.utcnow(),
    )

    # T-208 (R-608): 落库失败重试 + 主库故障暂存磁盘，不得静默丢失。
    # #75：写入移入 CDR writer 线程攒批提交（worker 不再阻塞在 DB 上）；语义不变：
    # 落库成功才扣费，落库最终失败 spool 兜底。
    def _persist_and_charge(db):
        ok = _upsert_cdr_dict(
            {c.name: getattr(cdr, c.name) for c in cdr.__table__.columns}, db=db)
        if not ok:
            raise RuntimeError("upsert returned False")
        # v0.3 预付费：落库成功后扣费（仅在接通且消费>0 且未扣过）。三道防重扣闸见 _charge_account。
        # 仅当「预付费开关」开启时挪动余额：开关关闭（停机窗口启用前/验证期）只做成本归集与报表，
        # 不扣余额、不写扣费流水，与 §5.5 第 3 步「余额校验（预付费开关开启时）」语义一致（fail-open 监控）。
        try:
            if settings.get("prepaid_enabled", False) and rec.get("answer_time") and cost and cost > 0:
                _charge_account(cdr.uuid, cost)
        except Exception as e:
            print("[billing] charge account failed (uuid=%s): %s" % (cdr.uuid, e))
        # v0.3.1 成本侧：运营商余额扣减（**始终扣费**，不依赖 prepaid_enabled，不拦截）。
        # 仅接通且成本>0 且有运营商归属才扣；防重扣靠 carrier_ledger.uk_carrier_ledger_cdr 唯一键。
        try:
            if rec.get("answer_time") and cost_price and cost_price > 0 and rec.get("carrier_id"):
                _charge_carrier(cdr.uuid, cost_price)
        except Exception as e:
            print("[billing] charge carrier failed (uuid=%s): %s" % (cdr.uuid, e))

    _enqueue_cdr_job(call_uuid, _persist_and_charge, fallback=lambda: _spool_cdr(cdr))


class ESLClient:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # 事件订阅串（inbound ESL 连接订阅后 FS 才投递；订阅失败/丢失需重连重订）。
        EVENT_SUB = (
            "CHANNEL_CREATE CHANNEL_PROGRESS CHANNEL_PROGRESS_MEDIA "
            "CHANNEL_ANSWER CHANNEL_BRIDGE CHANNEL_HANGUP_COMPLETE "
            "CUSTOM sofia::register sofia::expire sofia::gateway_state"
        )
        # 自愈参数：周期重订阅间隔 / 无事件看门狗阈值（秒）。
        RESUB_INTERVAL = 60
        WATCHDOG = 120
        while not self._stop.is_set():
            try:
                con = ESLconnection(ESL_CFG["host"], ESL_CFG["port"], ESL_CFG["password"])
                if not con.connected():
                    print("[ESL] connect failed, retry in %ss" % ESL_CFG.get("reconnect_interval", 3))
                    time_sleep(ESL_CFG.get("reconnect_interval", 3))
                    continue
                print("[ESL] connected")
                if not con.events("plain", EVENT_SUB):
                    # 订阅静默失败（FS 事件套接字尚未就绪等）：断开重连后重订。
                    print("[ESL] subscribe failed, reconnect")
                    con.disconnect()
                    time_sleep(ESL_CFG.get("reconnect_interval", 3))
                    continue
                print("[ESL] subscribed")
                _RECONCILE_REQ.set()  # #75：订阅/重连成功即对账一次（补断连缺口）
                last_sub = time.monotonic()
                last_event = time.monotonic()
                while not self._stop.is_set():
                    # 看门狗：长时间无事件 = 连接僵死/订阅丢失 → 强制重连重订。
                    if time.monotonic() - last_event > WATCHDOG:
                        print("[ESL] no events for %ss, reconnecting" % WATCHDOG)
                        break
                    ev = con.recvEvent(1.0)
                    if ev is None:
                        # 非阻塞轮询：超时（连接存活但暂无事件）不要重连，
                        # 靠顶部看门狗与 60s 周期重订阅自愈；仅连接确已断开才重连。
                        if not con.connected():
                            print("[ESL] connection lost, reconnecting")
                            break
                        last_event = time.monotonic()
                        continue
                    last_event = time.monotonic()
                    # #75：reader 只入队，worker 线程消费（消费慢不再反压 FS socket）。
                    _enqueue_event(ev)
                    # 周期重订阅：keepalive + 自愈可能丢失的订阅（不依赖整条连接重连）。
                    now = time.monotonic()
                    if now - last_sub > RESUB_INTERVAL:
                        con.events("plain", EVENT_SUB)
                        last_sub = now
                con.disconnect()
            except Exception as e:  # noqa: BLE001
                print("[ESL] loop error:", e)
                time_sleep(ESL_CFG.get("reconnect_interval", 3))


def time_sleep(secs):
    import time
    time.sleep(secs)
def _persist_cdr(cdr, attempts=3):
    """落库（T-208）：基于 cdr.uuid 的 MySQL upsert，重复事件/重灌幂等，不双插。"""
    vals = {c.name: getattr(cdr, c.name) for c in cdr.__table__.columns}
    return _upsert_cdr_dict(vals, attempts)


def _existing_start_time(call_uuid):
    """取该 uuid 已落行的 start_time（跳过 NULL），供预落库/终态复用。

    背景：cdr 是分区表，唯一键只能是 (uuid, start_time)。而 MySQL 对**含 NULL 的唯一键
    不判冲突**，且每次落库若写一个新的 start_time 同样绕过冲突——两者叠加导致同一 uuid
    被插入多行（故障切换每腿重入 + 终态各一行）。复用首次 start_time 之后，复合唯一键
    才能真正命中并走 ON DUPLICATE KEY UPDATE，实现「一个 uuid 一行」。
    """
    if not call_uuid:
        return None
    try:
        db = SessionLocal()
        try:
            row = db.execute(
                select(Cdr.start_time)
                .where(Cdr.uuid == call_uuid, Cdr.start_time.isnot(None))
                .order_by(Cdr.id)
                .limit(1)
            ).first()
            return row[0] if row else None
        finally:
            db.close()
    except Exception:
        return None

def pre_insert_cdr(call_uuid, caller_in="", callee_in="", account_id=None,
                    gateway_id=None, carrier_id=None, source_ip=None,
                    source_port=None, reject_reason="", caller_type="phone"):
    """dialplan 出口预落库（Task13/A 方案）：只要有 INVITE 进来就落一条 CDR。

    在 gateway 返回 dialplan XML 的同时写入（xml_curl 是 INVITE 必经之路），
    不依赖 ESL 事件流——ESL 半死时呼叫仍留痕。后续 HANGUP 事件用同一 uuid
    upsert 补终态（_upsert_cdr_dict ON DUPLICATE KEY UPDATE，幂等不双插）。

    注意：本函数只落「进行中/决策」半成品；成本/计费/终态字段由 HANGUP 路径
    _save_cdr 补齐。若 ESL 一直没事件，该记录 end_time 为空 = 未完成呼叫，
    由管理端按 start_time 展示为失败/未接通。

    重入安全：T-205 故障切换链 gw_leg_0..N-1 每腿都触发同 uuid 的 xml_curl，本函数
    走 ignore_existing=True（等同 INSERT IGNORE），绝不覆盖 HANGUP 已落的 hangup_cause /
    sip_code / start_time 等终态字段；也不双插。
    """
    try:
        from datetime import datetime as _dt
        import time as _t
        vals = {
            "uuid": call_uuid,
            "caller_in": caller_in or "",
            "callee_in": callee_in or "",
            # 复用首次 start_time：否则故障切换每腿重入都会因 start_time 不同而插新行
            "start_time": _existing_start_time(call_uuid) or _dt.utcnow(),
            "account_id": account_id,
            "gateway_id": gateway_id,
            "carrier_id": carrier_id,
            "source_ip": source_ip,
            "source_port": source_port,
            "reject_reason": reject_reason or None,
            "caller_type": caller_type or "phone",
            "hangup_cause": None,
            "sip_code": None,
        }
        # ignore_existing=True：dialplan 重入（同 uuid）时即使 HANGUP 已落也绝不覆盖终态。
        return _upsert_cdr_dict(vals, ignore_existing=True)
    except Exception as e:
        print("[CDR] pre_insert failed (uuid=%s): %s" % (call_uuid, e), flush=True)
        return False


def _upsert_cdr_dict(vals: dict, attempts=3, ignore_existing=False, db=None):
    """T-208/T-计费：MySQL upsert（ON DUPLICATE KEY UPDATE）。

    重复事件 / reaper 重灌均幂等：冲突时按 uuid 更新（排除 id/uuid；created_at 由终态覆盖写入），
    cost 随 CDR 一起算好，重灌不会重算也不会双计。

    ignore_existing=True 时用于「预落库」场景：uuid 已存在就什么都不做（保护已落 HANGUP 终态不被
    覆盖）；不存在就插入。语义等同 INSERT IGNORE 但不吞 IntegrityError，仍会 retry。
    """
    cols = [c.name for c in Cdr.__table__.columns]
    # NOT NULL 列兜底：DB 已标 NOT NULL DEFAULT 的列若 vals 显式传 None 会触发 IntegrityError(1048)
    # 且 DEFAULT 不生效（默认仅在列被省略时生效）。覆盖 _save_cdr 构造遗漏与 reaper 重灌（旧 spool
    # 里 billed=None）两条路径。billed 是 v0.3 新增后曾导致全部 CDR 落库失败的根因。
    _nn_defaults = {
        "billed": 0,
        "cost": 0, "cost_price": 0, "profit": 0,
        "bill_unit": 60, "bill_duration": 0,
        "switch_count": 0, "record_status": 0,
        "hangup_direction": 0, "caller_type": "",
    }
    for _k, _d in _nn_defaults.items():
        if vals.get(_k) is None:
            vals[_k] = _d
    # 统一收口（所有落库路径：_save_cdr / pre_insert_cdr / spool 重灌 都汇聚于此）：
    # 对齐到该 uuid 已落行的 start_time。cdr 是分区表，唯一键 (uuid, start_time) 在
    # start_time 取不同值（事件时间 vs utcnow）或为 NULL 时均不判冲突，导致同一通话落多行。
    if vals.get("uuid"):
        _st = _existing_start_time(vals["uuid"])
        if _st is not None:
            vals["start_time"] = _st
        elif vals.get("start_time") is None:
            vals["start_time"] = datetime.utcnow()
    # #75：db 参数 —— writer 攒批时传入共享会话（异常向上抛，由 writer 统一
    # 回滚/逐条重试）；默认 None 时保持旧行为（自开会话、独立 commit、attempts 重试）。
    i = 0
    while True:
        own = db is None
        s = db if db is not None else SessionLocal()
        try:
            stmt = mysql_insert(Cdr).values(**vals)
            if ignore_existing:
                # 预落库场景：UUID 已存在时不更新任何列（保护 HANGUP 路径落下的终态）。
                upd = {"id": Cdr.id}
            else:
                upd = {c: stmt.inserted[c] for c in cols if c not in ("id", "uuid")}
            stmt = stmt.on_duplicate_key_update(**upd)
            s.execute(stmt)
            if own:
                s.commit()
            return True
        except Exception as e:
            if not own:
                raise
            try:
                s.rollback()
            except Exception:
                pass
            i += 1
            if i >= attempts:
                return False
            print('[CDR] upsert attempt %d failed:' % i, e)
            time.sleep(0.5)
        finally:
            if own:
                s.close()


def _spool_cdr(cdr):
    try:
        spool = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cdr_spool')
        os.makedirs(spool, exist_ok=True)
        path = os.path.join(spool, cdr.uuid + '.json')
        data = {c.name: getattr(cdr, c.name) for c in cdr.__table__.columns}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, default=str, ensure_ascii=False)
        print('[CDR] SPOOLED', path)
    except Exception as e:
        print('[CDR] SPOOL FAILED, CDR LOST:', e)


# T-计费 内存费率缓存（key -> (过期时间戳, 值)），TTL 60s，避免每通查库。
_RATE_CACHE = {}
_RATE_TTL = 60.0


def _rate_cache_get(key):
    v = _RATE_CACHE.get(key)
    if v and v[0] > time.time():
        return v[1]
    return None


def _rate_cache_set(key, val):
    _RATE_CACHE[key] = (time.time() + _RATE_TTL, val)


def clear_rate_cache():
    """T-计费：费率变更后立即失效内存缓存，下通呼叫按新费率算（避免 60s TTL 内仍用旧值）。"""
    _RATE_CACHE.clear()


def _eff_rate(v):
    """费率链取值：NULL 或 <= 0 一律视为「本层未配置」，继续向下一级回落。

    ⚠️ 为什么 0 也算未配置：管理端 collectForm 对 number 类型输入框，留空会提交成 0
    （而非 NULL），于是「从未填过费率」的话机/接入点在库里是 0.0000。若把 0 当有效费率，
    费率链会在话机层就被「免费」截断，账户兜底费率永远不生效（用户实测 uuid
    f7f26d2b… 即此问题：话机 80015432 rate=0.0000 → 本应回落账户 sss 的 0.0110）。

    业务上 0 元/计费单位不是有意义的费率；真要「免费」，在最兜底的那一级（账户 / 运营商）
    留空或填 0 即可——链路走到底同样不计费。
    """
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return None if d <= 0 else d


def _compute_billing(rec, talk, bill):
    """v0.3：算当通消费(收入侧) + 当通成本(成本侧)并解析账户维度。

    返回 (cost, rate_used, account_id, business_id, customer_id, cost_price, cost_rate_used, cost_bill_unit)。

    收入侧费率链：话机.rate → 接入点.rate → 账户.rate；任一级为 NULL 或 <= 0 视为未配置，
                 继续回落下一级（见 _eff_rate）；仅接通计费。
    成本侧链：gateway.cost_rate → carrier.cost_rate；**成本侧使用独立的计费单位**(gateway/carrier.bill_unit)，
             与收入侧接入点 bill_unit 解耦；仅接通计费。
    """
    ap_id = rec.get("access_point_id")
    cost = Decimal("0")
    rate_used = None
    account_id = business_id = customer_id = None
    ap_rate = account_rate = None

    # 1) 接入点维度：一次 join 取 business_id/account_id/customer_id 与接入点/账户费率。
    #    v0.3 多租户：接入点直挂账户（AccessPoint.account_id），不再经 Business 中转。
    #    注意：话机注册呼叫**没有接入点**，此处会跳过，由第 3 步按话机归属兜底。
    if ap_id:
        row = _rate_cache_get("ap_%d" % ap_id)
        if row is None:
            db = SessionLocal()
            try:
                row = db.execute(
                    select(AccessPoint.business_id, AccessPoint.account_id, AccessPoint.rate,
                           Account.customer_id, Account.rate)
                    .join(Account, Account.id == AccessPoint.account_id)
                    .where(AccessPoint.id == ap_id)
                ).first()
            except Exception as e:
                print("[billing] ap resolve failed:", e)
                row = None
            finally:
                db.close()
            if row is not None:
                _rate_cache_set("ap_%d" % ap_id, row)
        if row is not None:
            business_id, account_id, ap_rate, customer_id, account_rate = row

    # 2) 话机维度：取本通关联话机的费率与归属账户（主叫优先）。
    #    同时为第 3 步提供归属兜底；查询不再限定 rate 非空，否则 rate 为 NULL 的话机
    #    连账户都解析不到（多租户下归属比费率更基础）。
    phone_rate = phone_account = None
    mids = [m for m in (rec.get("caller_mid"), rec.get("callee_mid")) if m]
    if mids:
        pk = "ph_" + "|".join(str(m) for m in mids)
        cached = _rate_cache_get(pk)
        if cached is None:
            db = SessionLocal()
            try:
                rows = db.execute(
                    select(SipPhone.phone_number, SipPhone.rate, SipPhone.account_id)
                    .where(SipPhone.phone_number.in_(mids))
                ).all()
                by_num = {str(r[0]): (r[1], r[2]) for r in rows}
                # 主叫优先：双向均命中（内线互拨）时以主叫话机为准；
                # 旧实现用 .first() 取 in_(mids) 首行，顺序不确定，会导致归属随机。
                picked = (by_num.get(str(rec.get("caller_mid") or ""))
                          or by_num.get(str(rec.get("callee_mid") or ""))
                          or (None, None))
                cached = picked
            except Exception as e:
                print("[billing] phone resolve failed:", e)
                cached = (None, None)
            finally:
                db.close()
            _rate_cache_set(pk, cached)
        phone_rate, phone_account = cached

    # 3) 归属兜底：无接入点（话机注册呼叫 / 内线互拨 / 历史话单）→ 直接按话机归属账户解析
    #    customer_id 与账户费率。
    #    ⚠️ 旧实现在此处 `if not ap_id: return 全空`，导致话机通话话单既无账户也不计费。
    if account_id is None and phone_account is not None:
        account_id = phone_account
        arow = _rate_cache_get("acct_%d" % account_id)
        if arow is None:
            db = SessionLocal()
            try:
                a = db.execute(
                    select(Account.customer_id, Account.rate).where(Account.id == account_id)
                ).first()
                biz = db.scalar(select(Business.id).where(Business.account_id == account_id).limit(1))
                arow = (a[0], a[1], biz) if a else (None, None, None)
            except Exception as e:
                print("[billing] account resolve failed:", e)
                arow = (None, None, None)
            finally:
                db.close()
            _rate_cache_set("acct_%d" % account_id, arow)
        customer_id, account_rate, business_id = arow

    # 归属不一致 → 忽略话机费率，回落接入点/账户（防异常配置串档）
    if phone_account is not None and account_id is not None and phone_account != account_id:
        phone_rate = None

    # 费率链（收入侧）：话机 → 接入点 → 账户（每一级 NULL/<=0 均视为未配置，继续回落）
    rate_used = _eff_rate(phone_rate)
    if rate_used is None:
        rate_used = _eff_rate(ap_rate)
    if rate_used is None:
        rate_used = _eff_rate(account_rate)
    if rec.get("answer_time") and talk and talk > 0 and rate_used is not None:
        bu = Decimal(str(rec.get("bill_unit") or 60))
        if bu > 0:
            units = int(Decimal(str(bill)) / bu)
            cost = (rate_used * units).quantize(Decimal("0.0001"))

    # ---- 成本侧（v0.3）：落地网关成本费率优先，回落运营商；独立成本计费单位 ----
    cost_price = Decimal("0")
    cost_rate_used = None
    cost_bill_unit = None
    gw_id = rec.get("gateway_id")
    ca_id = rec.get("carrier_id")
    try:
        gw_id = int(gw_id) if gw_id and str(gw_id).isdigit() else None
        ca_id = int(ca_id) if ca_id and str(ca_id).isdigit() else None
    except (ValueError, TypeError):
        gw_id = ca_id = None
    gw_cost = cr_cost = None
    if gw_id:
        gk = "gw_%d" % gw_id
        gw_cost = _rate_cache_get(gk)
        if gw_cost is None:
            db = SessionLocal()
            try:
                g = db.execute(
                    select(Gateway.cost_rate, Gateway.bill_unit).where(Gateway.id == gw_id)
                ).first()
                gw_cost = (g[0], g[1]) if g else (None, None)
            except Exception as e:
                print("[billing] gw cost resolve failed:", e)
                gw_cost = (None, None)
            finally:
                db.close()
            _rate_cache_set(gk, gw_cost)
    # 网关成本费率为 NULL 或 <=0（含遗留 0.0000，从未配置）一律视为「本层未配置」，
    # 继续回落运营商（与收入侧 _eff_rate 对称，见 f7f26d2b 修正；CDR 311925b9 即此问题：
    # 网关 gw-cost_rate=0.0000 → 本应回落运营商 0.0060，却记成 0）。
    if gw_cost is not None and _eff_rate(gw_cost[0]) is None and ca_id:
        ck = "cr_%d" % ca_id
        cr_cost = _rate_cache_get(ck)
        if cr_cost is None:
            db = SessionLocal()
            try:
                c = db.execute(
                    select(Carrier.cost_rate, Carrier.bill_unit).where(Carrier.id == ca_id)
                ).first()
                cr_cost = (c[0], c[1]) if c else (None, None)
            except Exception as e:
                print("[billing] carrier cost resolve failed:", e)
                cr_cost = (None, None)
            finally:
                db.close()
            _rate_cache_set(ck, cr_cost)
    # 选取值：网关优先（成本费率 NULL/<=0 视为未配置，见 _eff_rate），否则回落运营商
    sel = gw_cost if (gw_cost and _eff_rate(gw_cost[0]) is not None) else cr_cost
    if sel and sel[0] is not None:
        cost_rate_used = sel[0]
        cost_bill_unit = sel[1] if sel[1] else 60
    if rec.get("answer_time") and talk and talk > 0 and cost_rate_used is not None:
        cbu = Decimal(str(cost_bill_unit or 60))
        if cbu > 0:
            # 与收入侧口径一致（§5.3）：成本计费单位数取 ceil(通话秒/成本计费单位)，不可用 int 截断（会少算一个单位、虚增毛利）
            cunits = ceil(Decimal(str(talk)) / cbu)
            cost_price = (cost_rate_used * cunits).quantize(Decimal("0.0001"))

    return cost, rate_used, account_id, business_id, customer_id, cost_price, cost_rate_used, cost_bill_unit


def _charge_account(cdr_uuid: str, cost) -> bool:
    """v0.3 预付费：通话落库后扣减账户余额并写流水。

    三道防重扣闸（与 T-208 幂等对齐）：
      1. account_ledger.uk_ledger_cdr(cdr_uuid) 唯一键：重复插入直接 IntegrityError 跳过；
      2. cdr.billed 标记：扣前检查，扣后置 1（且 UPDATE ... WHERE billed=0 保证只置一次）；
      3. 事务内 `with_for_update` 行锁：防并发下余额竞态。
    归属失败（account_id 为空）或已扣则直接返回，不报错。
    """
    cost = Decimal(str(cost))
    if cost <= 0:
        return False
    db = SessionLocal()
    try:
        cdr_row = db.execute(
            select(Cdr.id, Cdr.account_id, Cdr.billed).where(Cdr.uuid == cdr_uuid)
        ).first()
        if cdr_row is None:
            return False
        cdr_id, account_id, billed = cdr_row
        if billed == 1:
            return False  # 已扣，跳过
        if account_id is None:
            return False
        # 行锁账户，避免并发余额竞态
        acc = db.get(Account, account_id, with_for_update=True)
        if acc is None:
            return False
        # 先插流水（唯一键 cdr_uuid 拦截重复扣费）——冲突即视为已扣，回滚返回
        try:
            lg = AccountLedger(
                account_id=account_id, cdr_uuid=cdr_uuid, type=2,
                amount=-cost, balance_after=(acc.balance or Decimal("0")) - cost,
                remark="通话扣费", created_at=datetime.now(),
            )
            db.add(lg)
            db.flush()
        except IntegrityError:
            db.rollback()
            return False  # 重复扣费（重灌/重复事件），跳过
        acc.balance = (acc.balance or Decimal("0")) - cost
        db.execute(update(Cdr).where(Cdr.uuid == cdr_uuid, Cdr.billed == 0).values(billed=1))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print("[billing] charge account error: %s" % e)
        return False
    finally:
        db.close()


def _charge_carrier(cdr_uuid: str, cost_price) -> bool:
    """v0.3.1 成本侧扣费：话单成本从运营商余额扣减并写流水。

    与 _charge_account 对称，但**关键差异**：
      - 不受 prepaid_enabled 门控——运营商成本始终归集（用户规则：只做扣费与计费，
        无「余额不足不通」的拦截机制），故**始终扣减、余额可负**。
      - 防重扣只靠 carrier_ledger.uk_carrier_ledger_cdr(cdr_uuid) 唯一键（与账户侧
        uk_ledger_cdr 对称）；运营商不维护 cdr.billed 标记（该标记专供账户侧）。
    归属失败（carrier_id 为空）或已扣（唯一键冲突）则跳过，不报错。
    """
    cost_price = Decimal(str(cost_price))
    if cost_price <= 0:
        return False
    db = SessionLocal()
    try:
        cdr_row = db.execute(
            select(Cdr.id, Cdr.carrier_id).where(Cdr.uuid == cdr_uuid)
        ).first()
        if cdr_row is None:
            return False
        cdr_id, carrier_id = cdr_row
        if carrier_id is None:
            return False
        # 行锁运营商，避免并发余额竞态
        car = db.get(Carrier, carrier_id, with_for_update=True)
        if car is None:
            return False
        # 先插流水（唯一键 cdr_uuid 拦截重复扣费）——冲突即视为已扣，回滚返回
        try:
            lg = CarrierLedger(
                carrier_id=carrier_id, cdr_uuid=cdr_uuid, type=2,
                amount=-cost_price, balance_after=(car.balance or Decimal("0")) - cost_price,
                remark="通话成本扣费", created_at=datetime.now(),
            )
            db.add(lg)
            db.flush()
        except IntegrityError:
            db.rollback()
            return False  # 重复扣费（重灌/重复事件），跳过
        car.balance = (car.balance or Decimal("0")) - cost_price
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print("[billing] charge carrier error: %s" % e)
        return False
    finally:
        db.close()


def _resolve_caller_account(caller_mid) -> int:
    """v0.3 预付费：解析主叫所属 Account。返回 account_id 或 None（解析失败 → fail-open 放通）。

    优先级：话机号 → 其 account_id；其次按接入点主叫也走不动（此处仅处理话机主叫）。
    caller_mid 可能为 8 位租户号或旧 4 位号（迁移前）。号码全局唯一查询即安全。
    """
    if not caller_mid:
        return None
    db = SessionLocal()
    try:
        ph = db.execute(
            select(SipPhone.account_id).where(SipPhone.phone_number == str(caller_mid))
        ).first()
        return ph[0] if ph and ph[0] is not None else None
    except Exception as e:
        print("[billing] resolve caller account failed:", e)
        return None
    finally:
        db.close()


def _check_balance_allowed(account_id: int) -> bool:
    """v0.3 预付费：可用余额校验。余额充足返回 True，否则返回 False（拒呼）。

    可用余额 = balance + credit_limit - min_balance。
    预付费开关关闭时一律放通（见 config prepaid_enabled）。
    """
    from core.config import settings
    if not settings.get("prepaid_enabled", False):
        return True  # 开关未开 → 不拦截（默认关闭，验证通过后再开）
    db = SessionLocal()
    try:
        acc = db.get(Account, account_id)
        if acc is None:
            return True  # 找不到账户 → fail-open，宁可漏拦不可误拒
        bal = acc.balance or Decimal("0")
        cl = acc.credit_limit or Decimal("0")
        mb = acc.min_balance or Decimal("0")
        return (bal + cl - mb) > 0
    except Exception as e:
        print("[billing] balance check failed (fail-open):", e)
        return True  # 异常 → fail-open
    finally:
        db.close()


def _replay_spool():
    """T-208：重灌 cdr_spool 中落库失败的 CDR（DB 抖动恢复后补足，不双插）。"""
    spool = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cdr_spool')
    if not os.path.isdir(spool):
        return
    for fp in glob.glob(os.path.join(spool, '*.json')):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            vals = {c.name: None for c in Cdr.__table__.columns}
            for k, v in data.items():
                if k in vals:
                    vals[k] = v
            if _upsert_cdr_dict(vals):
                os.remove(fp)
                print('[CDR] reaper replayed', os.path.basename(fp))
        except Exception as e:
            print('[CDR] reaper replay failed', fp, e)


def start_cdr_reaper(interval=30):
    """T-208：周期重灌 cdr_spool（默认 30s）。主库抖动期间落盘失败的通话在恢复后自动补录。"""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                _replay_spool()
            except Exception as e:
                print('[CDR] reaper error', e)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print('[CDR] reaper started, interval=%ss' % interval)


def _handle_gateway_state(event) -> None:
    """落地网关注册状态回写（CUSTOM sofia::gateway_state）。

    实测报文头是 `Gateway` / `State`（不是 Gateway-Name / Gateway-State），
    翻译与落库逻辑见 gw_state.py（含「只对注册型网关生效」的原因）。
    """
    try:
        try:
            from gw_state import handle_event as _gw_handle
        except ImportError:  # pragma: no cover
            from src.gw_state import handle_event as _gw_handle
        _gw_handle(event)
    except Exception as e:
        print("[gw-state] handler failed:", e, flush=True)


def _handle_sofia_reg(event) -> None:
    """话机注册状态同步（sofia::register / sofia::expire）。"""
    sub = event.getHeader("Event-Subclass") or ""
    if sub not in ("sofia::register", "sofia::expire"):
        return
    user = (event.getHeader("from-user") or event.getHeader("username")
            or event.getHeader("user") or "")
    if not user:
        return
    st = 1 if sub == "sofia::register" else 0
    db = SessionLocal()
    try:
        changed = False
        row = db.scalar(select(SipPhone).where(SipPhone.phone_number == user))
        if row is not None and row.status != st:
            row.status = st
            row.updated_at = datetime.utcnow()
            changed = True
        ap = db.scalar(select(AccessPoint).where(AccessPoint.reg_username == user))
        if ap is not None and ap.reg_status != st:
            ap.reg_status = st
            ap.updated_at = datetime.utcnow()
            changed = True
        if changed:
            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print("[phone-sync] event update failed:", e, flush=True)
    finally:
        db.close()
