"""实时通话明细（需求②-2，2026-09-15）。

数据源：**逐节点直连 FS 的 ESL**（复用 `node_health` 的"短连接探测"思路）
------------------------------------------------------------------------------
FS 侧两条内置命令：

1. `api show channels as json` —— 本节点**全部通道**。
   ⚠️ 字段名是**实测取得**（FS 1.11.2，39 列），不是凭印象写的；勿凭直觉改：
   `uuid, direction, created, created_epoch, name, state, cid_name, cid_num, ip_addr,
    dest, application, application_data, dialplan, context, ..., callstate,
    callee_name, callee_num, callee_direction, call_uuid, ...`
   空态返回 `{"row_count":0}` —— 注意它与"命令不被支持"（返回 `-ERR ...`）
   必须区分开：前者是"真的没有通话"，后者是"我没探到"。见 `parse_channels()` 的
   `None` / `[]` 双返回值，以及 `probe_node()` 的 `bad_show_channels_output` 分支
   （否则"节点少算了"会被读成"没有通话"）。

2. `api uuid_dump <uuid> json` —— 单通道**全部变量**，键为 FS header 名；
   通道变量带 `variable_` 前缀（实测：`variable_cdr_access_point_id`）。
   缺了它就取不到接入点/落地网关（`show channels` 不含业务变量）。

为什么不拿 Redis 并发凭证（`conc:resv:*`）当主源
------------------------------------------------
凭证里只有 `{g:gw_id, a:ap_id, n:node_uuid}`，**没有主被叫/时长**，且
`concurrency.backend=local` 或 Redis 重启后即缺失。FS 通道变量才是与**话单/告警同口径**
的权威来源；两源并用会引入"不一致时信谁"的解释成本，本期不做。

归并：一次呼叫一行（Q2 拍板）
-----------------------------
`show channels` 是**逐腿**的（A/B 腿各一行）。按 `call_uuid` 分组
（FS 对同一次呼叫的两腿写同一个 call_uuid；实测单腿场景 `call_uuid == uuid`），
取 **A 腿**（`uuid == call_uuid`，即拨号计划实际作用的那条腿）为主行。
业务变量（接入点/落地网关/运营商）由拨号计划 set 在 A 腿上，故**以 A 腿为准**；
若 A 腿该值为空，才回落到同组其它腿 —— 只做补齐，**不编造**。

字段口径（用户明确要求的 5 项缺一不可）
--------------------------------------
- 所在节点   ← 探测到该通道的那台 FS（fs_node）
- 接入点     ← `variable_cdr_access_point_id`
- 主叫话机   ← `Caller-Caller-ID-Number`（回落 `show channels` 的 `cid_num`）
- 落地网关   ← `variable_cdr_gateway_id`
- 被叫话机   ← `Caller-Destination-Number`（回落 `show channels` 的 `dest`）
⚠️ 内线互拨 / 未出局的呼叫，这些变量**本来就是空** —— 一律留空，不填占位符。

`direction` 用**业务口径**而非 FS 的技术方向（FS 的 inbound/outbound 描述的是
"FS 收还是发"，对运维没意义）：
  有落地网关 → outbound；无网关但有接入点 → inbound；两者皆无 → internal。
`state`：`Answer-State=answered` 或已应答时间戳 > 0 → answered，否则 ringing。
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from db.models import AccessPoint, Carrier, FsNode, Gateway
from db.session import get_db
from fs_esl_socket import ESLConnection
from node_health import STATUS_OFFLINE

log = logging.getLogger("live_calls")

router = APIRouter(prefix="/api", tags=["live-calls"])

CHANNELS_CMD = "show channels as json"
DUMP_CMD = "uuid_dump %s json"

# 通道变量前缀（FS 的 uuid_dump 把通道变量输出为 variable_<name>，实测确认）
_V = "variable_"
V_ACCESS_POINT = _V + "cdr_access_point_id"
V_GATEWAY = _V + "cdr_gateway_id"
V_CARRIER = _V + "cdr_carrier_id"


def _to_int_or_none(v):
    """把筛选参数规整成 int/None。

    前端默认把未选的筛选拼成空串（`access_point_id=`），直接 `int("")` 会抛错、
    且 FastAPI 声明为 `int` 时连路由都进不来（422）。这里入口收 str，空串/None/
    非法值一律按 None（即"不过滤"）处理。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None

# uuid_dump 的 header 名（同样是实测确认，勿改）
H_ANSWER_STATE = "Answer-State"                    # "answered" / "ringing"
H_CALL_STATE = "Channel-Call-State"                # DOWN/RINGING/EARLY/ACTIVE/HELD（回落用）
H_CHANNEL_STATE = "Channel-State"                  # CS_*（再兜一层）
H_CID_NUM = "Caller-Caller-ID-Number"
H_CID_NAME = "Caller-Caller-ID-Name"
H_DEST = "Caller-Destination-Number"
H_ANSWERED_US = "Caller-Channel-Answered-Time"     # 微秒级 epoch；未应答为 "0"
H_CREATED_US = "Caller-Channel-Created-Time"       # 微秒级 epoch

# 已应答的 Channel-Call-State（Answer-State 缺失时的回落链）
_ANSWERED_CALL_STATES = ("ACTIVE", "HELD")

DEFAULT_TIMEOUT = 2.0        # 单节点 ESL 超时（秒）；并行探测，取最短
DEFAULT_MAX_WORKERS = 8
MAX_CHANNELS_PER_NODE = 1000  # 单节点详查上限：超出则**显性报错**而非静默截断


# ---------------------------------------------------------------------------
# 纯函数：解析 / 归并（无 IO，可直接单测）
# ---------------------------------------------------------------------------
def _to_int(v, default=None):
    try:
        s = str(v).strip()
        if s == "" or s.lower() in ("none", "null"):
            return default
        return int(float(s))
    except (TypeError, ValueError):
        return default


def parse_channels(body):
    """解析 `show channels as json`。

    - 合法且为空 → `[]`（**真的没有通话**）
    - 不合法/命令不支持（如 `-ERR ...`）→ `None`（**没探到**，调用方须报错）
    """
    try:
        d = json.loads(body or "")
    except (TypeError, ValueError):
        return None
    if isinstance(d, dict):
        rows = d.get("rows")
        if rows is None:
            # 只有 row_count 的形态：{"row_count":0} 合法
            return [] if "row_count" in d else None
    elif isinstance(d, list):
        rows = d
    else:
        return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


def parse_dump(body) -> dict:
    """解析 `uuid_dump <uuid> json`；任何异常一律回 {}（不抛，缺字段就留空）。"""
    try:
        d = json.loads(body or "")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _pick(primary: dict, others: list, *keys):
    """先取 A 腿的值，为空再依次看同组其它腿 —— 只补齐，不编造。"""
    for src in [primary] + list(others or []):
        for k in keys:
            v = (src or {}).get(k)
            if v not in (None, ""):
                return v
    return ""


def _infer_state(dump: dict, row: dict) -> str:
    ans = str(dump.get(H_ANSWER_STATE) or "").strip().lower()
    if ans == "answered":
        return "answered"
    if ans == "ringing":
        return "ringing"
    if (_to_int(dump.get(H_ANSWERED_US), 0) or 0) > 0:
        return "answered"
    cs = str(dump.get(H_CALL_STATE) or "").strip().upper()
    if cs in _ANSWERED_CALL_STATES:
        return "answered"
    st = str(dump.get(H_CHANNEL_STATE) or row.get("state") or "").strip().upper()
    if st.startswith("CS_EXECUTE"):
        return "answered"
    return "ringing"


def merge_calls(channels, dumps, node_uuid, node_name, now=None):
    """把逐腿通道归并成「一次呼叫一行」，返回 items（名称字段留空，由调用方批量回填）。"""
    now = now or datetime.now(timezone.utc)
    groups, order = {}, []
    for ch in channels or []:
        u = ch.get("uuid") or ""
        if not u:
            continue
        key = ch.get("call_uuid") or u
        g = groups.get(key)
        if g is None:
            g = groups[key] = []
            order.append(key)
        g.append(ch)

    items = []
    for key in order:
        legs = groups[key]
        primary = (next((c for c in legs if (c.get("uuid") or "") == (c.get("call_uuid") or "")), None)
                   or next((c for c in legs if (c.get("uuid") or "") == key), None)
                   or legs[0])
        others = [dumps.get(c.get("uuid") or "") or {} for c in legs
                  if (c.get("uuid") or "") != (primary.get("uuid") or "")]
        pv = dumps.get(primary.get("uuid") or "") or {}

        ap_id = _to_int(_pick(pv, others, V_ACCESS_POINT))
        gw_id = _to_int(_pick(pv, others, V_GATEWAY))
        carrier_id = _to_int(_pick(pv, others, V_CARRIER))
        if gw_id:
            direction = "outbound"
        elif ap_id:
            direction = "inbound"
        else:
            direction = "internal"

        state = _infer_state(pv, primary)
        created_us = _to_int(pv.get(H_CREATED_US))
        if created_us is None:
            epoch = _to_int(primary.get("created_epoch"))
            created_us = (epoch * 1000000) if epoch else None
        answered_us = _to_int(pv.get(H_ANSWERED_US), 0) or 0
        base_us = answered_us if (state == "answered" and answered_us) else created_us
        duration = 0
        if base_us:
            duration = max(0, int(now.timestamp() - base_us / 1000000.0))
        create_time = ""
        if created_us:
            try:
                create_time = datetime.fromtimestamp(
                    created_us / 1000000.0, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                create_time = ""

        caller = _pick(pv, [], H_CID_NUM) or primary.get("cid_num") or ""
        callee = _pick(pv, [], H_DEST) or primary.get("dest") or ""
        caller_name = _pick(pv, [], H_CID_NAME) or primary.get("cid_name") or ""

        items.append({
            "call_uuid": key,
            "node_uuid": node_uuid,
            "node_name": node_name,
            "access_point_id": ap_id,
            "access_point_name": "",
            "caller": str(caller or ""),
            "caller_name": str(caller_name or ""),
            "callee": str(callee or ""),
            "gateway_id": gw_id,
            "gateway_name": "",
            "carrier_id": carrier_id,
            "carrier_name": "",
            "direction": direction,
            "state": state,
            "duration_sec": duration,
            "create_time": create_time,
            "_sort_epoch": created_us or 0,
        })
    return items


# ---------------------------------------------------------------------------
# ESL 探测（IO 层；单测时整层 monkeypatch）
# ---------------------------------------------------------------------------
def probe_node(host, port, password, timeout=DEFAULT_TIMEOUT,
               max_channels=MAX_CHANNELS_PER_NODE):
    """探一个节点，返回 `(ok, channels, dumps, error)`。

    每次新建短连接（与 `node_health._probe_esl` 同策略）——不与 `esl_client`
    的事件长连接争抢同一 socket。任何异常都**收成 ok=False + error**，
    绝不向上抛（否则一个坏节点会打挂整页）。
    """
    con = None
    try:
        con = ESLConnection(host, int(port or 8021), password or "", timeout=timeout)
        if not con.connected():
            return False, [], {}, "esl_connect_failed"
        ev = con.api(CHANNELS_CMD, timeout=timeout)
        if ev is None:
            return False, [], {}, "show_channels_no_reply"
        rows = parse_channels(ev.getBody())
        if rows is None:
            return False, [], {}, "bad_show_channels_output"
        if len(rows) > max_channels:
            return False, rows, {}, "channel_count_over_limit:%d>%d" % (len(rows), max_channels)
        dumps = {}
        for ch in rows:
            u = ch.get("uuid") or ""
            if not u:
                continue
            dev = con.api(DUMP_CMD % u, timeout=timeout)
            if dev is not None:
                dumps[u] = parse_dump(dev.getBody())
        return True, rows, dumps, ""
    except Exception as e:  # noqa: BLE001 —— 单节点故障不得影响整体
        log.warning("[live-calls] probe %s:%s failed: %s", host, port, e)
        return False, [], {}, "probe_error:%s" % e
    finally:
        if con is not None:
            try:
                con.disconnect()
            except Exception:
                pass


def probe_all(targets, password, timeout=DEFAULT_TIMEOUT, workers=DEFAULT_MAX_WORKERS):
    """并行探测多个节点。targets = [(node_uuid, node_name, host, port)]。

    返回 `{node_uuid: (ok, channels, dumps, error)}`（**每个 target 必有键**，
    探查线程本身炸了也要给出条目 —— 否则该节点会在 `nodes[]` 里凭空消失）。
    """
    out = {}
    if not targets:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as ex:
        futs = {ex.submit(probe_node, h, p, password, timeout): u
                for (u, _n, h, p) in targets}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                out[u] = fut.result()
            except Exception as e:  # noqa: BLE001
                out[u] = (False, [], {}, "probe_error:%s" % e)
    for (u, _n, _h, _p) in targets:
        out.setdefault(u, (False, [], {}, "probe_error:no_result"))
    return out


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _fill_names(db: Session, items: list) -> None:
    """批量回填 接入点/落地网关/运营商 名称（避免 N+1）。"""
    ap_ids = {it["access_point_id"] for it in items if it.get("access_point_id")}
    gw_ids = {it["gateway_id"] for it in items if it.get("gateway_id")}
    ap_map, gw_map, carrier_map = {}, {}, {}

    if ap_ids:
        for r in db.scalars(select(AccessPoint).where(AccessPoint.id.in_(ap_ids))).all():
            ap_map[r.id] = r.name or ""
    if gw_ids:
        for r in db.scalars(select(Gateway).where(Gateway.id.in_(gw_ids))).all():
            gw_map[r.id] = (r.name or "", r.carrier_id)
    # 网关自身的运营商优先（通道变量缺失时也能显示）
    for it in items:
        if not it.get("carrier_id") and it.get("gateway_id") in gw_map:
            it["carrier_id"] = gw_map[it["gateway_id"]][1]
    carrier_ids = {it["carrier_id"] for it in items if it.get("carrier_id")}
    if carrier_ids:
        for r in db.scalars(select(Carrier).where(Carrier.id.in_(carrier_ids))).all():
            carrier_map[r.id] = r.name or ""

    for it in items:
        it["access_point_name"] = ap_map.get(it.get("access_point_id"), "")
        it["gateway_name"] = gw_map.get(it.get("gateway_id"), ("", None))[0]
        it["carrier_name"] = carrier_map.get(it.get("carrier_id"), "")


def query_live_calls(db: Session, node_uuid=None, access_point_id=None, gateway_id=None,
                     page=1, page_size=50, timeout=DEFAULT_TIMEOUT):
    """聚合实时通话（供路由与单测共用；返回契约规定的响应体）。"""
    esl_cfg = settings.get("esl") or {}
    password = esl_cfg.get("password") or ""

    q = select(FsNode).order_by(FsNode.id)
    if node_uuid:
        # 显式指定节点时**不过滤在线状态**：用户点名要看它，哪怕它刚好被判离线
        q = q.where(FsNode.node_uuid == node_uuid)
    else:
        # 未点名：只探"非已知离线"的节点（0=offline；1=online / 2=overload 都要探）
        q = q.where(FsNode.status != STATUS_OFFLINE)
    nodes = db.scalars(q).all()

    targets = [(n.node_uuid, (n.host or n.name or ""), n.host, n.esl_port) for n in nodes]
    # fs_node.name 是容器短主机名（不可读），host 才是可读节点名 —— 与 crud 的口径一致
    name_by_uuid = {n.node_uuid: (n.host or n.name or "") for n in nodes}

    results = probe_all(targets, password, timeout=timeout)

    items, node_rows, degraded = [], [], False
    for (u, _n, _h, _p) in targets:
        ok, channels, dumps, err = results.get(u, (False, [], {}, "probe_error:no_result"))
        if not ok:
            degraded = True
        node_rows.append({"node_uuid": u, "name": name_by_uuid.get(u, ""),
                          "ok": bool(ok), "error": err or ""})
        if ok:
            items.extend(merge_calls(channels, dumps, u, name_by_uuid.get(u, "")))

    # 过滤
    if access_point_id is not None:
        items = [it for it in items if it.get("access_point_id") == access_point_id]
    if gateway_id is not None:
        items = [it for it in items if it.get("gateway_id") == gateway_id]

    # 稳定排序：新呼叫在前；同刻按 call_uuid，保证翻页不跳行
    items.sort(key=lambda it: (-(it.get("_sort_epoch") or 0), it.get("call_uuid") or ""))

    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    if total_pages and page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    page_items = items[offset:offset + page_size]

    _fill_names(db, page_items)
    for it in page_items:
        it.pop("_sort_epoch", None)

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "nodes": node_rows,
        "degraded": degraded,
    }


@router.get("/stats/live-calls")
def live_calls(node_uuid: str = None, access_point_id: str = None, gateway_id: str = None,
               page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200),
               db: Session = Depends(get_db)):
    """当前通话明细（跨节点聚合，一次呼叫一行）。

    鉴权：复用 `nodes` feature 的**读**权限（见 api/authz.py::FEATURE_PATHS）——
    不新增 feature，避免牵动 M3 权限矩阵与既有角色配置。

    ⚠️ 本路由**必须注册在 `include_router(crud_router)` 之前**，否则会被 crud 的
    兜底 `/api/{entity}/{item_id}` 抢先匹配（`stats` 当 entity、`live-calls` 当
    int 型 item_id）→ 422（PITFALLS #34，与 /api/stats/concurrency 同型）。

    ⚠️ 筛选参数**入口收 str**：前端未选筛选时拼出 `access_point_id=`（空串），
    FastAPI 无法把空串解析成 int → 422。故在此把空串/非法值规整为 None（即"不过滤"）。

    探不通的节点一律进 `nodes[].ok=false` + `degraded=true`，**绝不返 500**：
    否则"少探到几个节点"会被读成"没有通话"，那比报错更危险。
    """
    # 前端默认把未选的筛选拼成空串（access_point_id= / gateway_id=），必须容错。
    ap_id = _to_int_or_none(access_point_id)
    gw_id = _to_int_or_none(gateway_id)
    return query_live_calls(db, node_uuid=node_uuid or None, access_point_id=ap_id,
                            gateway_id=gw_id, page=page, page_size=page_size)
