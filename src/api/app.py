"""管理端 / 内部 REST 骨架（FastAPI）。

M1 仅冒烟：健康检查 + CDR 查询 + 监控占位。M3（T-301~T-306）补鉴权/角色/
接入落地/路由/实时监控仪表盘/录音播放下载。

M2 T-201：mod_xml_curl 拨号计划端点（业务网关层下发路由/限制决策）。
M2 T-202：出局路由分支（前缀路由 → 落地网关 bridge）。
P1：接入点/落地网关维度主被叫限制 + 号码变换（evaluate_call_scoped / apply_translate）。
P2：实时并发统计（esl_client._conc）+ 出局并发预检（超限回 503，D3）+ /api/stats/concurrency。
P3：管理端 REST CRUD（api/crud）+ Jinja 管理页（/admin）。
"""
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import Response, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import re

from core.config import settings, NODE_UUID, RECORD_ROOT, RECORD_BACKEND
from db.session import get_db
from db.models import Cdr, AccessPoint, Gateway, SipPhone, FsNode
from rules.service import (
    evaluate_call, evaluate_call_scoped, apply_translate,
    OWNER_ACCESS_POINT, OWNER_GATEWAY,
)
from rules.matcher import DIR_CALLER, DIR_CALLEE
from api.dialplan_xml import (
    build_allow_xml, build_deny_xml, build_empty_xml, build_outbound_xml, _LOCAL_EXT_RE
)
from api.fs_auth import fs_basic_auth_ok, FS_BASIC_PATHS
from esl_client import _resolve_caller_account, _check_balance_allowed
from esl_client import pre_insert_cdr
from route.service import select_outbound_gateway, resolve_access_point, resolve_access_points
from esl_client import get_concurrency
import concurrency  # P2-a：Redis 并发原子预留（D7）
from api.directory_xml import fs_directory
from fs_sofia_config import build_config_response
from alerting import push_webhook
from node_health import evaluate as nh_evaluate, stale_threshold as nh_stale_threshold
import recordings

app = FastAPI(title="SIP Switch Gateway API", version="0.2.0")

# T-205 逐腿故障切换：transfer 到 gw_leg_* 会触发**新的** xml_curl 请求，
# 该重入请求丢失原始候选上下文（dest 变为 gw_leg_N）。按呼叫 uuid 缓存首呼生成的
# 完整多腿文档，重入时原样返回，使 loop 延续（通道变量 gw_failover_* 随重取回传）。
_FAILOVER_CACHE: dict = {}

# P3：Jinja 管理页 + 静态资源
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
TEMPLATES_DIR = os.path.join(_SRC, "templates")
STATIC_DIR = os.path.join(_SRC, "static")
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# P3：CRUD 路由

@app.middleware("http")
async def _static_no_cache(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.middleware("http")
async def _auth_guard(request, call_next):
    # T-301 管理端鉴权：白名单放行 FS 内部回调/健康检查/登录登出/静态资源/根/SPA 外壳(/admin)。
    # /admin 仅承载登录页与前端 JS，不含任何数据；真正的数据经 /api/* 受保护，
    # 由前端 bootAuth() 探 /api/me(401) 后在客户端渲染登录覆盖层。
    path = request.url.path
    # /fs/* xml_curl 回调：HTTP Basic 共享凭据（安全修复，校验逻辑在 api/fs_auth.py；
    # app.py 冻结约定下本处仅按 FS_BASIC_PATHS 分流插入，白名单其余行为不变）
    if path in FS_BASIC_PATHS and not fs_basic_auth_ok(request):
        return Response("unauthorized", status_code=401,
                        media_type="text/plain; charset=utf-8",
                        headers={"WWW-Authenticate": "Basic"})
    if (path in ("/fs/dialplan", "/fs/directory", "/fs/config", "/healthz", "/api/login", "/api/logout")
            or path.startswith("/static/") or path == "/" or path == "/admin"):
        return await call_next(request)
    try:
        get_current_admin(request)
    except HTTPException:
        if path == "/admin" or (request.headers.get("accept") or "").startswith("text/html"):
            return Response("unauthorized", status_code=401, media_type="text/plain; charset=utf-8")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)



@app.api_route("/fs/directory", methods=["GET", "POST"])
async def fs_directory_api(request: Request, db: Session = Depends(get_db)):
    """mod_xml_curl 目录服务：分机鉴权与呼入定位，数据源为 sip_phone 表。"""
    if request.method == "POST":
        params = await request.form()
    else:
        params = request.query_params
    try:
        cid = params.get("sip_call_id")
        if cid:
            from api.directory_xml import _sip_call_ctx
            _sip_call_ctx[cid] = {"caller": params.get("sip_from_user"),
                                  "callee": params.get("sip_request_user") or params.get("sip_to_user")}
            if len(_sip_call_ctx) > 2000:
                _sip_call_ctx.clear()
    except Exception:
        pass
    return fs_directory(params, db)


@app.get("/api/cdr")
def list_cdr_api(page: int = Query(1, ge=1),
                 page_size: int = Query(50, ge=1, le=500),
                 caller: str = None, callee: str = None,
                 gateway_id: int = None, access_point_id: int = None,
                 hangup_cause: str = None,
                 dt_from: str = None, dt_to: str = None,
                 db: Session = Depends(get_db)):
    q = select(Cdr)
    if caller:
        q = q.where(Cdr.caller_in.like("%" + caller + "%"))
    if callee:
        q = q.where(Cdr.callee_in.like("%" + callee + "%"))
    if gateway_id is not None:
        q = q.where(Cdr.gateway_id == gateway_id)
    if access_point_id is not None:
        q = q.where(Cdr.access_point_id == access_point_id)
    if hangup_cause:
        q = q.where(Cdr.hangup_cause == hangup_cause)
    if dt_from:
        q = q.where(Cdr.start_time >= dt_from)
    if dt_to:
        q = q.where(Cdr.start_time <= dt_to)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    tp = (total + page_size - 1) // page_size if total else 1
    if page > tp:
        page = tp
    off = (page - 1) * page_size
    qq = q.order_by(Cdr.id.desc()).offset(off).limit(page_size)
    rows = db.scalars(qq).all()
    cols = Cdr.__table__.columns
    items = [{c.name: getattr(r, c.name) for c in cols} for r in rows]
    # #70：录音列除了「有没有录」还要知道「文件是否真的在盘上」——卷没挂好 / 容器重建丢文件时
    # 要显性提示，而不是等用户点播放才 404。只对 record_status=1 的行做一次本地 stat
    # （网关与 FS 共享只读卷，开销可忽略）；任何异常都不许影响话单查询本身。
    for it in items:
        if int(it.get("record_status") or 0) == 1:
            try:
                # 存量裸路径没有 authority 段，按该条 CDR 自己的 fs_node_uuid 归属
                # （不是一律按当前节点 —— 多节点下 node2 录的音不能在 node1 上找）。
                st = recordings.stat_local(it.get("record_path"), RECORD_ROOT,
                                           it.get("fs_node_uuid") or NODE_UUID,
                                           local_node=NODE_UUID)
                it["record_ok"] = bool(st["exists"])
                it["record_remote"] = bool(st["remote"])
            except Exception:
                pass
    return {"items": items, "page": page, "page_size": page_size,
            "total": total, "total_pages": tp}
from api.crud import router as crud_router
from api.auth import get_current_admin, router as auth_router
from api.billing import router as billing_router
from api.accounts import router as accounts_router
from api.cdr_export import router as cdr_export_router  # T-306 扩展点
from api.oplog import oplog_middleware  # T-301 扩展点
from api.csrf import csrf_middleware  # 安全扩展点：同源写校验（实现见 api/csrf.py）

# ---------------------------------------------------------------------------
# 节点状态 + Webhook 推送（#70 系列：节点健康可视化 + 外部告警落地）
# 必须注册在 crud_router 之前：crud 的 /api/{entity} 兜底路由会吞掉 /api/nodes。
# ---------------------------------------------------------------------------
@app.get("/api/nodes")
def list_nodes(db: Session = Depends(get_db)):
    """FS 节点健康检查快照（DEP-6 / #69）：各节点在线状态 + 并发 + 注册数 + 最后心跳。

    B1（2026-09-11）：`fs_node.status` 只是"最后写入值"，写入方（该节点自己的网关）
    一死就永久停在 1 → 僵尸在线。这里按 `last_heartbeat_at` **现算**超时：

    - `stale` / `stale_seconds`：心跳是否已超时 / 超时多少秒
    - `effective_status`：超时强制 offline，否则等于原 `status`
    - `heartbeat_threshold`：本次判定阈值（秒），便于前端与排查对齐

    前端一律按 `effective_status` 渲染；原 `status` 字段保留原值，便于排查
    "DB 脏值 vs 展示口径" 的差异。B2 的落库清扫见 `node_health._sweep_stale_nodes`。
    """
    now = datetime.now(timezone.utc)
    thr = nh_stale_threshold()
    rows = db.scalars(select(FsNode).order_by(FsNode.id)).all()
    cols = FsNode.__table__.columns
    items = []
    for r in rows:
        stale, age, eff = nh_evaluate(r, now=now, threshold=thr)
        d = {c.name: getattr(r, c.name) for c in cols}
        d["stale"] = stale
        d["stale_seconds"] = age
        d["effective_status"] = eff
        d["heartbeat_threshold"] = thr
        items.append(d)
    return {"items": items, "heartbeat_threshold": thr,
            "server_time": now.isoformat()}


@app.post("/api/webhook-test")
async def webhook_test(request: Request):
    """向指定 webhook 地址发送一条测试消息，验证配置是否可达。

    入参：{ "gateway_url": "...", "node_url": "..." }（哪个有值测哪个）。
    复用与真实告警相同的 push_webhook，保证测试等价于真实推送。
    """
    data = await request.json()
    targets = []
    if data.get("gateway_url"):
        targets.append(("gateway", data["gateway_url"]))
    if data.get("node_url"):
        targets.append(("node", data["node_url"]))
    results = []
    for ch, url in targets:
        md = ("**Webhook 推送测试**\n"
              "> 渠道: %s\n"
              "> 这是一条来自 SIP 路由网关的测试消息，说明 webhook 配置已生效。") % ch
        ok, msg = push_webhook(url, md)
        results.append({"channel": ch, "url": url, "ok": ok, "msg": msg})
    return {"results": results}


@app.post("/api/provision/resync-all")
def provision_resync_all():
    """「立即全节点重扫」：让所有 FS 节点全量重建落地网关（killgw + rescan）。

    - 本节点：立刻执行（不等轮询周期）
    - 其它节点：经 DB `provision_seq` 变化 + 空 pending 名单，退化为全量 rescan，
      最迟一个 `provision_sync_interval` 后完成。
    """
    from fs_provision import force_all_nodes_rescan
    seq = force_all_nodes_rescan()
    return {"ok": True, "seq": seq,
            "note": "本节点已立即重建；其它节点最迟一个轮询周期后跟上"}


# ---------------------------------------------------------------------------
# #70 录音回放 / 下载（FS 写、网关只读回源）
# 端点契约对前端是**稳定**的：本地阶段返回文件流，上云阶段返回 302 到对象存储签名直链，
# 前端与 CDR 表结构都不用改 —— 这正是 URI 抽象的收益。
# 必须注册在 crud_router 之前：crud 的 /api/{entity} 兜底路由会吞掉 /api/*（PITFALLS #34）。
# ---------------------------------------------------------------------------
def _load_recording_row(db, uuid: str):
    return db.execute(
        select(Cdr.uuid, Cdr.record_status, Cdr.record_path, Cdr.fs_node_uuid)
        .where(Cdr.uuid == uuid)
    ).first()


def _rec_node(row) -> str:
    """本条 CDR 的录音归属节点。存量裸路径没有 authority 段，靠 cdr.fs_node_uuid 补
    （只在 legacy 分支用得到；URI 里已有 authority 时以 URI 为准）。"""
    return getattr(row, "fs_node_uuid", None) or NODE_UUID


@app.get("/api/cdr/{uuid}/recording/meta")
def cdr_recording_meta(uuid: str, db: Session = Depends(get_db)):
    """录音元信息：供前端决定按钮形态（▶ 播放 / ⚠ 缺失）与展示定位信息。"""
    row = _load_recording_row(db, uuid)
    if row is None:
        raise HTTPException(status_code=404, detail="cdr_not_found")
    stored = row.record_path or ""
    node = _rec_node(row)
    info = recordings.resolve(stored, RECORD_ROOT, node, local_node=NODE_UUID)
    st = recordings.stat_local(stored, RECORD_ROOT, node, local_node=NODE_UUID)
    return {
        "uuid": uuid,
        "record_status": int(row.record_status or 0),
        "record_uri": info["uri"],
        "scheme": info["scheme"],
        "backend": RECORD_BACKEND,
        "node_uuid": info["authority"],
        "legacy": info["legacy"],
        "remote": st["remote"],
        "exists": st["exists"],
        "size": st["size"],
    }


@app.get("/api/cdr/{uuid}/recording")
def cdr_recording(uuid: str, download: int = 0, db: Session = Depends(get_db)):
    """录音回源：本地流式（支持 Range → 可拖动进度）+ `?download=1` 附件下载。

    分支：无录音 → 404；上云 → 302 签名直链；录音在别的节点 → 409；
    文件缺失 → 404（**不是 500**），让前端显性提示「文件缺失」而不是静默失败。
    """
    row = _load_recording_row(db, uuid)
    if row is None:
        raise HTTPException(status_code=404, detail="cdr_not_found")
    stored = row.record_path or ""
    if not stored or not int(row.record_status or 0):
        raise HTTPException(status_code=404, detail="no_recording")

    # 上云阶段：302 到对象存储签名直链（public_url 目前恒返回 None，契约先固化在此）
    url = recordings.public_url(stored, settings.get("record") or {})
    if url:
        return RedirectResponse(url, status_code=302)

    info = recordings.resolve(stored, RECORD_ROOT, _rec_node(row), local_node=NODE_UUID)
    if info["scheme"] == recordings.SCHEME_COS:
        raise HTTPException(status_code=503, detail="object_storage_not_configured")
    path = info["path"]
    if path and os.path.isfile(path):
        # FileResponse 自带 Range 支持（Accept-Ranges: bytes / 206），音频可拖动播放；
        # 传 filename 即自动附 Content-Disposition: attachment。
        if download:
            return FileResponse(path, media_type="audio/wav",
                                filename=info["name"] or (uuid + ".wav"))
        return FileResponse(path, media_type="audio/wav")
    if info["remote"]:
        # 读不到 **且** 归属别的节点 → 409 告知去哪拿（比裸 404 有信息量）。
        # 注：录音目录是共享挂载，别的节点的文件在本节点通常**也读得到**（走上面的 200 分支）；
        # 真正落到这里的场景是真·多机（P3 多 FS 各自本地盘）。届时把这里改成
        # 302 跳到该节点的同名端点即可，前端不用动。
        return JSONResponse(
            {"error": "recording_on_other_node", "node_uuid": info["authority"],
             "hint": "录音归属节点 %s 且本节点不可达" % info["authority"]},
            status_code=409)
    raise HTTPException(status_code=404, detail="recording_file_missing")


# T-301 操作日志自动埋点（实现见 api/oplog.py，本文件只挂载）
app.middleware("http")(oplog_middleware)
# CSRF 同源写校验（实现见 api/csrf.py，本文件只挂载）
app.middleware("http")(csrf_middleware)

app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(accounts_router)
app.include_router(cdr_export_router)  # T-306：须在 crud 兜底路由前注册（PITFALLS #34）
app.include_router(crud_router)


# ---------------------------------------------------------------------------
# mod_xml_curl 拨号计划端点（T-201 / T-202 / P1 / P2 / G4）
# ---------------------------------------------------------------------------
# FS 通过 mod_xml_curl 以 GET/POST 拉取拨号计划，网关在此完成：
#   全局限制 → 解析接入点 → ②a IP 白名单 → ②b 接入点限制 → ②c 接入点变换
#   → 本地分机 / 出局路由（前缀匹配 + G4 接入点↔落地策略前移过滤）→ ④b 落地限制
#   → ④c 落地变换 → ⑤ 并发预检（超限回 503）→ bridge。
# 限制命中回 CALL_REJECTED(603)；并发超限(P2, D3)回 503(NETWORK_OUT_OF_ORDER)。
# 架构铁律：决策全在网关层。
# ---------------------------------------------------------------------------




def _enrich_candidates(db, candidates, caller, callee, conc=None):
    """逐腿候选：对每个候选网关计算其专属出局号(apply_translate)与 failover 配置，
    供 build_outbound_xml 逐腿桥接（T-205：每腿用本 gw 的变换号与 switch_codes）。

    P2-c：额外附带**并发预检快照**，供下游打标（落进 switch_detail 的扩展字段）：
    - `conc_gw`：该网关当前并发；`conc_limit`：其 concurrent_limit（0=不限）
    - `at_capacity`：该腿此刻是否已打满 —— 多腿文档里据此标注"为何这一腿被跳过/为何最终 503"
    `conc` 复用调用方已取的 get_concurrency() 结果，避免每个候选各读一次 Redis。
    """
    conc = conc or get_concurrency()
    gw_conc = (conc or {}).get("gw") or {}
    legs = []
    for g in candidates:
        co, ce = apply_translate(db, OWNER_GATEWAY, g.id, caller, callee)
        cur = int(gw_conc.get(g.id, 0) or 0)
        lim = int(getattr(g, "concurrent_limit", 0) or 0)
        legs.append({
            "gateway_id": g.id,
            "name": g.name,
            "carrier_id": g.carrier_id,
            "caller_out": co,
            "callee_out": ce,
            "ip": getattr(g, "ip", ""),
            "port": getattr(g, "port", ""),
            "switch_codes": g.switch_codes or "503,500,408,486",
            "failover_pre_ring_only": int(getattr(g, "failover_pre_ring_only", 0) or 0),
            # P2-c 打标扩展字段（复用 switch_detail 元素结构，见 esl_client._parse_switch_detail）
            "conc_gw": cur,
            "conc_limit": lim,
            "at_capacity": 1 if (lim > 0 and cur >= lim) else 0,
        })
    return legs


def _order_by_concurrency(candidates, conc, gw_key="gw"):
    """P2-c：按「当前并发是否打满」对候选池做**稳定重排**（未打满的排前面）。

    只在顺序上做文章，不改变 select_outbound_gateway 的前缀/优先级语义：Python 排序稳定，
    同组内保持原有相对顺序（长前缀 → 大优先级 → id）。
    这样「首选网关已满」时能自动降级到同候选池里还有余量的网关，而不是直接 503
    —— 即 D8「全候选并发过滤」在**选路**层面的落地。
    """
    gw_conc = (conc or {}).get(gw_key) or {}
    if not gw_conc:
        return list(candidates)

    def full(g):
        lim = int(getattr(g, "concurrent_limit", 0) or 0)
        return 1 if (lim > 0 and int(gw_conc.get(g.id, 0) or 0) >= lim) else 0

    return sorted(candidates, key=full)


def _conc_detail(gw, conc, ap_id=None, reason=""):
    """构造并发 503 的**机器可读**原因串（P2-c 打标）。

    形如 `busy_limit_gw;gw=7;gw_conc=5;gw_limit=5;g_conc=12;g_limit=20;ap=5;ap_conc=3;ap_limit=10`
    —— 分号分隔的 key=value，既是 reject_reason 也便于日志检索与后续告警打标。
    """
    ap_conc = (((conc or {}).get("ap") or {}).get(ap_id, 0) if ap_id is not None else 0)
    parts = [reason or "busy_limit_gw"]
    if gw is not None:
        parts.append("gw=%d" % gw.id)
        parts.append("gw_conc=%d" % int(((conc or {}).get("gw") or {}).get(gw.id, 0) or 0))
        parts.append("gw_limit=%d" % int(getattr(gw, "concurrent_limit", 0) or 0))
    parts.append("g_conc=%d" % int((conc or {}).get("global", 0) or 0))
    parts.append("g_limit=%d" % int(settings.get("concurrent_limit_global", 0) or 0))
    if ap_id is not None:
        parts.append("ap=%d" % ap_id)
        parts.append("ap_conc=%d" % int(ap_conc or 0))
    return ";".join(parts)


# ---------------------------------------------------------------------------
# ⑤' P2-a 并发原子预留（D7，2026-09-12）
#
# 此前：读 get_concurrency() 快照 → 判断 → 下发。三步之间存在 TOCTOU 竞态窗口，
# 高并发瞬时误差下会超发（ROADMAP §7 P2-a 缺口）。
# 现在：快照仅用于 global/ap 预检与 P2-c 重排（偏好），**最终闸门是 Lua 原子预留**
# —— check-and-increment + 写凭证一次原子完成，预留成功的候选即下发首选（FS 的
# 多腿 failover 顺序与预留保持一致；后续腿转移由 esl_client capture 块 transfer_leg 处理）。
# fail 语义：Redis 不可用 → D3 fail-close 拒新增（503 busy_limit_redis）；
# concurrency.fail_open=true（D5 逃生，默认关）→ 回落影子计数放行。
# ---------------------------------------------------------------------------

def _conc_snapshot_or_fail(candidates, ap_id):
    """并发快照（P2-a）。redis 后端取 Redis 真源（精确取候选相关键，最小 IO）；
    Redis 不可用时按 D5 fail_open 决定：回落影子计数（返回非 None）或 fail-close（None）。"""
    if concurrency.backend() == "redis":
        snap = concurrency.snapshot(gw_ids=[g.id for g in candidates], ap_id=ap_id)
        if snap is not None:
            return snap
        if concurrency.fail_open():
            print("[conc] redis unavailable -> fail-open fallback to shadow counter (D5)", flush=True)
        else:
            return None
    return get_concurrency()


def _conc_reserve_candidates(candidates, uuid, ap_id, ap_limit, g_limit, context):
    """对候选池逐个 Lua 原子预留。返回 (预留成功的 gw, deny Response)。

    - 成功 → (gw, None)：调用方把该 gw 置为下发首选（其余候选顺延，作为 failover 腿）
    - gw 维度满 → 试下一候选（与 P2-c「全满才拒」语义一致，但判定是原子的）
    - global/ap 维度满 → 整通 503（实时值拼 _conc_detail，与旧格式对齐）
    - Redis 不可用 → fail_open 放行（不预留）/ fail-close 503 busy_limit_redis
    - local 后端或 uuid 缺失 → 不预留（保持旧行为），返回 (candidates[0], None)
    """
    if concurrency.backend() != "redis" or not uuid:
        return candidates[0], None
    last = None
    for gw in candidates:
        gl = int(getattr(gw, "concurrent_limit", 0) or 0)
        res = concurrency.reserve_leg(uuid, gw.id, ap_id, g_limit, ap_limit, gl, NODE_UUID)
        if res.get("ok") is True:
            return gw, None
        if res.get("ok") is None:
            if concurrency.fail_open():
                print(f"[conc] redis lost during reserve -> fail-open allow gw={gw.name} (D5)", flush=True)
                return gw, None
            print(f"[conc-limit] redis unavailable during reserve -> fail-close (gw={gw.name})", flush=True)
            return None, Response(content=build_deny_xml(
                "busy_limit_redis", sip_code="503", context=context), media_type="text/xml")
        reason = res.get("reason") or "busy_limit_gw"
        if reason != "busy_limit_gw":
            # global/ap 维度满：与候选无关，无回退余地，直接拒（Lua 实时值拼明细）
            print(f"[conc-limit] {reason} at reserve (gw={gw.name}) cur={res.get('cur')} limit={res.get('limit')}", flush=True)
            return None, Response(content=build_deny_xml(
                _conc_detail(gw, res.get("conc") or {}, ap_id, reason),
                sip_code="503", context=context), media_type="text/xml")
        last = (gw, res)
    if last:
        gw, res = last
        print(f"[conc-limit] all {len(candidates)} candidate(s) at gw limit -> reject "
              f"(last gw={gw.name} {res.get('cur')}/{res.get('limit')})", flush=True)
        return None, Response(content=build_deny_xml(
            _conc_detail(gw, res.get("conc") or {}, ap_id, "busy_limit_gw"),
            sip_code="503", context=context), media_type="text/xml")
    return candidates[0], None


# ---------------------------------------------------------------------------
# ④b 落地网关维度主被叫限制 —— **候选池过滤**（2026-09-11，A 方案）
#
# 此前只对 `candidates[0]` 跑规则，不通过就 `build_deny_xml(603)` 整通挂断 ——
# 「首选网关不收这个号」时，同池里本该胜出的次选**从未被考察**，failover 链
# 根本没被构建（CDR 特征：switch_count=0 / gateway_id=NULL，见 PITFALLS #65）。
#
# 现改为与 ④ 里「接入点↔落地策略(G4, route/service._ap_gateway_allowed)」**同构**：
# 先按规则剔除不可用网关，保留者再按 前缀/优先级/并发 排序 → **全被拒才拒呼**。
# 语义变化：网关维度的 allow/deny 从「全局硬限制」变成「该网关的选路资格」。
# 顺序不变式：**资格（规则）在前，偏好（并发重排）在后**，两者不可混。
# ---------------------------------------------------------------------------

_REJECT_REASON_MAX = 64   # = cdr.reject_reason varchar(64)，超长会被 MySQL 截断/报错


def _gw_deny_one(gw, failed_dir, failed_rule):
    """单个网关被规则拒绝时的历史格式 reason（保持逐字节兼容，勿改）。"""
    lab = "caller" if failed_dir == DIR_CALLER else "callee"
    pat = getattr(failed_rule, "pattern", None)
    return f"denied_by_gw_{gw.id}_{lab}_rule:{pat}" if pat else f"denied_by_gw_{gw.id}_{lab}_rule"


def _gw_deny_reason(denied):
    """把「候选**全部**被网关规则拒绝」压成 **≤64 字符** 的机器可读 reason。

    - 只拒 1 个网关：沿用 `denied_by_gw_<gid>_<dir>_rule:<pat>`（与旧行为逐字节一致，
      候选池原本就只有 1 个网关时**零兼容风险**）；
    - 拒多个：`denied_by_all_gw_rules:<gid>/<dir>/<pat>;...`，超出 64 字符时截断并以
      `;+N` 收尾（宁少几条明细，也不能让 MySQL 截断 —— 列宽就是 64）。
    """
    if len(denied) == 1:
        return _gw_deny_one(*denied[0])
    head = "denied_by_all_gw_rules:"
    shown = []
    for i, (gw, failed_dir, failed_rule) in enumerate(denied):
        item = "%d/%s/%s" % (gw.id, "caller" if failed_dir == DIR_CALLER else "callee",
                             getattr(failed_rule, "pattern", None) or "-")
        rest = len(denied) - i - 1
        cand = head + ";".join(shown + [item]) + ((";+%d" % rest) if rest else "")
        if len(cand) > _REJECT_REASON_MAX:
            shown.append("+%d" % (len(denied) - i))
            break
        shown.append(item)
    return head + ";".join(shown)


def _filter_candidates_by_gw_rules(db, candidates, caller, callee):
    """按落地网关维度主被叫限制过滤候选池，返回 (kept, denied)。

    denied 元素为 `(gateway, failed_dir, failed_rule)`，供全被拒时生成可读 reason。
    **只过滤、不排序** —— 顺序仍由 `select_outbound_gateway`（前缀/优先级）与
    `_order_by_concurrency`（并发偏好）决定，保持「资格」与「偏好」分离。
    口径：caller/callee 用**进入落地网关前**的号（D1，与 ④b 原语义一致）。
    """
    kept, denied = [], []
    for g in candidates:
        ok, failed_dir, failed_rule = evaluate_call_scoped(db, OWNER_GATEWAY, g.id, caller, callee)
        if ok:
            kept.append(g)
        else:
            denied.append((g, failed_dir, failed_rule))
    if denied:
        print("[gw-rule-filter] %s->%s skipped=%s kept=%s" % (
            caller, callee,
            ["%d:%s" % (g.id, _gw_deny_one(g, d, r).split("_rule")[-1]) for g, d, r in denied],
            [g.id for g in kept]), flush=True)
    return kept, denied


def _phone_branch(db, caller, callee, context="default", phone=None, uuid=""):
    # v0.3 多租户：话机注册呼叫**不经过接入点**（AP 是中继/IP 接入维度），归属账户由话机自身
    # account_id 决定。此前此处 access_point_id 恒为 None 且不下发账户 → CDR 无 account_id，
    # _compute_billing 在 `if not ap_id` 处直接返回全空 → 话单不归属账户、不计费。
    # 现显式下发 cdr_account_id，_compute_billing 亦增加「无接入点时按话机解析账户」兜底。
    acct_id = getattr(phone, "account_id", None)
    # D1 变量取值口径**对齐 AP 分支**（2026-09-11）：话机不经接入点（AP 是中继/IP 接入维度），
    # 因此**没有 ②c 接入点变换**这一层，caller_mid 恒等于「进入落地网关前的主叫号」== caller。
    # 显式声明并一路透传（empty / deny / outbound 三处都下发），使：
    #   CDR.caller_in  = 入局号（未经任何变换）   → 与 _route_via_ap 的 orig_caller 同义
    #   CDR.caller_mid = 进入落地网关前的号        → 与 _route_via_ap 的 caller_mid 同义
    #   CDR.callee_mid = 进入落地网关前的被叫号    → 同上
    # 网关维度规则亦统一按 caller_mid/callee 裁决（此前传的是裸 caller，值相同但语义未声明）。
    orig_caller, orig_callee = caller, callee
    caller_mid = caller
    # 内线互拨（Task14）：同租户话机 = 被叫前 4 位 == 主叫前 4 位 且总长 8 位纯数字。
    # 替代原 _LOCAL_EXT_RE(1000-1019)——8 位话机号（租户号+序号）不匹配旧正则，
    # 导致互拨也被当出局打去 trunk。
    _same_t = (len(callee) == 8 and callee.isdigit()
               and len(caller) >= 4 and callee[:4] == caller[:4])
    if _same_t:
        return Response(content=build_allow_xml(callee, None, 60, caller_type="phone", context=context, account_id=acct_id), media_type="text/xml")
    c=select_outbound_gateway(db, callee, ap_id=None)
    if c is None:
        return Response(content=build_empty_xml(caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller_mid, callee_mid=callee, context=context, account_id=acct_id), media_type="text/xml")
    # ④b 落地网关维度限制 —— **候选池过滤**（A 方案，2026-09-11）：首选网关不收这个号时
    # 自动降级到同池下一个候选（与 ④ 里 G4 接入点↔落地策略同构）；**全被拒才拒呼**。
    c,denied=_filter_candidates_by_gw_rules(db, c, caller_mid, callee)
    if not c:
        rs=_gw_deny_reason(denied)
        print("[rule-deny] phone",caller_mid,"->",callee,rs,flush=True)
        return Response(content=build_deny_xml(rs, caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller_mid, callee_mid=callee, account_id=acct_id), media_type="text/xml")
    gw=c[0]
    co,ce=apply_translate(db, OWNER_GATEWAY, gw.id, caller_mid, callee)
    gl=int(getattr(gw,"concurrent_limit",0) or 0)
    gg=int(settings.get("concurrent_limit_global",0) or 0)
    # ⑤' P2-a：快照仅作 global 预检与 P2-c 重排偏好；最终闸门 = Lua 原子预留（D7）
    cc=_conc_snapshot_or_fail(c, None)
    if cc is None:
        print("[conc-limit] redis unavailable, fail-close reject (fail_open=off)", flush=True)
        return Response(content=build_deny_xml("busy_limit_redis",sip_code="503",caller_in=orig_caller,callee_in=orig_callee,caller_mid=caller_mid,callee_mid=callee,account_id=acct_id),media_type="text/xml")
    if gg>0 and cc["global"]>=gg:
        return Response(content=build_deny_xml(_conc_detail(gw,cc,None,"busy_limit_global"),sip_code="503"),media_type="text/xml")
    # P2-c：网关维度先按并发重排候选池（未打满的优先），全满才 503
    if any(int(getattr(x,"concurrent_limit",0) or 0)>0 for x in c):
        ro=_order_by_concurrency(c,cc)
        if [x.id for x in ro]!=[x.id for x in c]:
            c=ro
            gw=c[0]
            gl=int(getattr(gw,"concurrent_limit",0) or 0)
            co,ce=apply_translate(db, OWNER_GATEWAY, gw.id, caller_mid, callee)
    # P2-a：逐候选原子预留（gw 维度在 Lua 内精确判定）；预留成功者置为下发首选
    gw2, deny = _conc_reserve_candidates(c, uuid, None, 0, gg, context)
    if deny is not None:
        return deny
    if gw2 is not None and gw2.id != gw.id:
        c = [gw2] + [x for x in c if x.id != gw2.id]
        gw = gw2
        gl = int(getattr(gw, "concurrent_limit", 0) or 0)
        co, ce = apply_translate(db, OWNER_GATEWAY, gw.id, caller_mid, callee)
    legs = _enrich_candidates(db, c, caller_mid, callee, conc=cc)
    print("[phone-outbound]",legs[0]["caller_out"],"->",legs[0]["callee_out"],"gw",gw.name,flush=True)
    return Response(content=build_outbound_xml(legs[0]["callee_out"],candidates=legs,gateway_id=legs[0]["gateway_id"],carrier_id=legs[0]["carrier_id"],bill_unit=60,access_point_id=None,record_enabled=1,caller=legs[0]["caller_out"],caller_type="phone", caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller_mid, callee_mid=callee, context=context, dst_ip=legs[0]["ip"], dst_port=legs[0]["port"], account_id=acct_id),media_type="text/xml")

def _route_via_ap(db, ap, bill_unit, caller, callee, context="default", uuid=""):
    """AP 确定后公共路由(方案A): ②c变换/③本地分机/④选落地/④b落地限制/④c变换/⑤并发预检/下发。default 与 trunk 共用。"""
    ap_id = ap.id
    orig_caller, orig_callee = caller, callee
    # ②c 接入点维度变换（限制通过后改写）；caller_mid/callee_mid 供 ④b 落地限制使用（D1）
    caller, callee = apply_translate(db, OWNER_ACCESS_POINT, ap_id, caller, callee)
    caller_mid = caller

    # 3) 本地分机（1000-1019）
    if re.match(_LOCAL_EXT_RE, callee):
        return Response(content=build_allow_xml(callee, ap_id, bill_unit, context=context), media_type="text/xml")

    # 4) 出局路由（最长前缀匹配 + G4 接入点↔落地策略前移过滤）
    #    select_outbound_gateway 内部已按 AccessGatewayPolicy 过滤候选池，
    #    使 N:M 场景下能回退到同前缀下一个被允许的网关；若全部被禁止则返回 None → NO_ROUTE。
    candidates = select_outbound_gateway(db, callee, ap_id)
    if candidates is None:
        return Response(content=build_empty_xml(access_point_id=ap_id, caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller, callee_mid=callee, context=context), media_type="text/xml")

    # ④b 落地网关维度限制 —— **候选池过滤**（A 方案，2026-09-11）
    #    与上面 ④ 的 G4「接入点↔落地策略」同构：先把被本网关规则拒绝的候选剔除，
    #    让同前缀的下一个顶上；**全被拒才拒呼**（reason 带全部命中明细）。
    #    此前只裁 candidates[0]、不通过即整通 603，导致「首选网关不收这个号」时
    #    次选从未被考察（CDR 特征 switch_count=0 / gateway_id=NULL，见 PITFALLS #65）。
    #    口径：用进入落地网关前的 caller_mid/callee（D1）。
    candidates, denied = _filter_candidates_by_gw_rules(db, candidates, caller_mid, callee)
    if not candidates:
        reason = _gw_deny_reason(denied)
        print(f"[rule-deny] {caller_mid}->{callee} {reason}", flush=True)
        return Response(content=build_deny_xml(reason, access_point_id=ap_id, caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller, callee_mid=callee, context=context), media_type="text/xml")

    gw = candidates[0]

    # ④c 逐腿落地网关维度变换（每个候选网关各自的出局号，T-205 故障切换需用本 gw 号）
    # P2-a 后 legs 在 ⑤ 段预留完成、候选顺序定型后统一构建（含并发打标）。

    # ⑤ 并发预检 + 原子预留（P2-a D7）
    # 快照仅用于 global/ap 预检与 P2-c 重排偏好；**最终闸门 = Lua check-and-reserve**
    # （gw 维度精确判定在预留脚本内完成，杜绝快照→下发之间的 TOCTOU 超发）。
    # 维度上限：全局取自 settings.concurrent_limit_global；接入点/落地网关取自表 concurrent_limit。
    ap_limit = int(getattr(ap, "concurrent_limit", 0) or 0)
    g_limit = int(settings.get("concurrent_limit_global", 0) or 0)
    conc = _conc_snapshot_or_fail(candidates, ap_id)
    if conc is None:
        print("[conc-limit] redis unavailable, fail-close reject (fail_open=off)", flush=True)
        return Response(content=build_deny_xml("busy_limit_redis", sip_code="503", access_point_id=ap_id, caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller, callee_mid=callee, context=context), media_type="text/xml")
    if g_limit > 0 and conc["global"] >= g_limit:
        print(f"[conc-limit] global {conc['global']}>={g_limit} busy_limit_global", flush=True)
        return Response(content=build_deny_xml(_conc_detail(gw, conc, ap_id, "busy_limit_global"), sip_code="503", context=context), media_type="text/xml")
    if ap_limit > 0 and conc["ap"].get(ap_id, 0) >= ap_limit:
        print(f"[conc-limit] ap {ap_id} {conc['ap'].get(ap_id, 0)}>={ap_limit} busy_limit_ap", flush=True)
        return Response(content=build_deny_xml(_conc_detail(gw, conc, ap_id, "busy_limit_ap"), sip_code="503", context=context), media_type="text/xml")
    # P2-c：网关维度**不再只判首选**。先按「是否已打满」稳定重排候选池（快照偏好）；
    # 全部打满时才回 503（D8 全候选并发过滤）。这避免了「首选满、次选还有空」却直接拒呼。
    if any(int(getattr(c, "concurrent_limit", 0) or 0) > 0 for c in candidates):
        reordered = _order_by_concurrency(candidates, conc)
        if [c.id for c in reordered] != [c.id for c in candidates]:
            print(f"[conc-limit] candidate reorder by concurrency: "
                  f"{[c.name for c in candidates]} -> {[c.name for c in reordered]}", flush=True)
            candidates = reordered
            gw = candidates[0]
    # P2-a：逐候选原子预留；预留成功者置为下发首选（FS failover 顺序与预留一致，
    # 后续腿实际落地 gw 的转移由 esl_client capture 块 transfer_leg 完成）
    gw2, deny = _conc_reserve_candidates(candidates, uuid, ap_id, ap_limit, g_limit, context)
    if deny is not None:
        return deny
    if gw2 is not None and gw2.id != gw.id:
        candidates = [gw2] + [x for x in candidates if x.id != gw2.id]
        gw = gw2
    legs = _enrich_candidates(db, candidates, caller_mid, callee, conc=conc)

    rec_enabled = ap.record_enabled if ap is not None else 1
    print(f"[outbound] {legs[0]['caller_out']}->{legs[0]['callee_out']} routed to gateway {gw.name} "
          f"(carrier={gw.carrier_id}, bill_unit={bill_unit}, candidates={[g.name for g in candidates]})", flush=True)
    return Response(
        content=build_outbound_xml(
            legs[0]["callee_out"], candidates=legs,
            gateway_id=legs[0]["gateway_id"], carrier_id=legs[0]["carrier_id"],
            bill_unit=bill_unit, access_point_id=ap_id, context=context, dst_ip=gw.ip, dst_port=gw.port,
            record_enabled=rec_enabled,
            caller=legs[0]["caller_out"], caller_in=orig_caller, callee_in=orig_callee, caller_mid=caller, callee_mid=callee,
        ),
        media_type="text/xml",
    )


def _trunk_branch(db, caller, callee, network_addr, context="trunk", uuid=""):
    """方案A trunk 中继分支(context=trunk): 同一来源 IP 可能匹配多个 IP 型 AP,
    按 id 升序逐个试 ②a IP 校验 + ②b AP 限制, 首个通过者用于路由, 全不通则拒绝。"""
    aps = resolve_access_points(db, network_addr)
    if not aps:
        print("[trunk] no_access_point src=%s %s->%s" % (network_addr, caller, callee), flush=True)
        return Response(content=build_deny_xml("no_access_point", caller_in=caller, callee_in=callee, context=context), media_type="text/xml")
    for ap in aps:
        wl = {h.strip() for h in (ap.register_host or "").split(",") if h.strip()}
        if network_addr not in wl:
            continue
        allowed, failed_dir, failed_rule = evaluate_call_scoped(
            db, OWNER_ACCESS_POINT, ap.id, caller, callee)
        if not allowed:
            print("[trunk] ap=%s denied %s->%s rule:%s, try next" % (
                ap.id, caller, callee, getattr(failed_rule, "pattern", None)), flush=True)
            continue
        print("[trunk] src=%s matched ap=%s(%s) %s->%s" % (
            network_addr, ap.id, ap.name, caller, callee), flush=True)
        return _route_via_ap(db, ap, int(ap.bill_unit or 60), caller, callee, context, uuid=uuid)
    print("[trunk] denied_by_all_ap src=%s tried=%s" % (network_addr, [a.id for a in aps]), flush=True)
    return Response(content=build_deny_xml("denied_by_all_ap", context=context), media_type="text/xml")

def _cache_failover_doc(uuid: str, resp):
    """T-205：首呼生成的多腿文档按 uuid 缓存，供 transfer 重入时原样返回。"""
    if uuid and getattr(resp, "body", None) and b"gw_leg_0" in resp.body:
        _FAILOVER_CACHE[uuid] = resp.body.decode("utf-8")
        if len(_FAILOVER_CACHE) > 500:
            _FAILOVER_CACHE.clear()
    return resp


@app.api_route("/fs/dialplan", methods=["GET", "POST"])
async def fs_dialplan(request: Request, db: Session = Depends(get_db)):
    """mod_xml_curl 拨号计划查询（GET 或 POST）。"""
    if request.method == "POST":
        params = await request.form()
    else:
        params = request.query_params
    caller = params.get("Caller-Caller-ID-Number") or ""
    callee = params.get("Caller-Destination-Number") or ""
    context = params.get("Hunt-Context-Name") or params.get("Hunt-Context") or params.get("key_value") or params.get("context") or "default"
    network_addr = params.get("Caller-Network-Addr") or params.get("network_addr") or ""
    # T-205 故障切换重入键：transfer 到 gw_leg_* 触发新 xml_curl 请求，按 uuid 返回缓存文档
    uuid = (params.get("Chat-Unique-ID") or params.get("Hunt-Unique-ID")
            or params.get("variable_uuid") or "")

    # Task13/A 方案：INVITE 必经 xml_curl → 先预落一条 CDR（uuid 幂等 upsert）。
    # ESL 事件流即使半死，话单也已留痕（end_time 留空待 HANGUP 或 reaper 补）。
    if uuid:
        try:
            pre_insert_cdr(uuid, caller_in=caller, callee_in=callee,
                           source_ip=network_addr)
        except Exception as _e:
            print("[pre-cdr] fail %s -> %s: %s" % (caller, callee, _e), flush=True)

    # 故障切换重入：dest 为 gw_leg_* 且命中缓存 → 原样返回首呼多腿文档，跳过规则重算
    if uuid and re.match(r"^gw_leg", callee) and uuid in _FAILOVER_CACHE:
        return Response(content=_FAILOVER_CACHE[uuid], media_type="text/xml")

    # 非 default 上下文（会议/语音信箱等）原样交回 FS，网关不接管。
    if context not in ("default", "trunk"):
        return Response(content=build_empty_xml(), media_type="text/xml")

    # v0.3 预付费：主叫可用余额校验（不足 → 603 拒呼，接通前拦截，已接通通话不因余额耗尽被强拆）。
    # 解析失败 / 开关关闭 → fail-open 放通（宁可漏拦不可误拒），详见 esl_client._check_balance_allowed。
    if settings.get("prepaid_enabled", False):
        acct_id = _resolve_caller_account(caller)
        _ap2 = None
        if acct_id is None:
            # 非话机主叫（中继/IP 点对点）：按接入点归属账户（v0.3 接入点已直挂 Account）
            _ap2, _bu = resolve_access_point(db, caller, network_addr)
            acct_id = getattr(_ap2, "account_id", None) if _ap2 is not None else None
        if acct_id is not None and not _check_balance_allowed(acct_id):
            print(f"[prepaid] reject call {caller}->{callee} by insufficient_balance (account={acct_id}, ap={getattr(_ap2, 'id', None)})", flush=True)
            # 拦截话单也须关联接入点+账户（T-207 贯通）：显式下发 cdr_access_point_id/cdr_account_id，
            # 使该通 603 拒呼的 CDR 落 access_point_id 与 account_id（无接入点的话机拦截仅下账户）。
            return Response(content=build_deny_xml(
                "insufficient_balance", sip_code="603",
                access_point_id=(_ap2.id if _ap2 is not None else None),
                account_id=acct_id,
                caller_in=caller, callee_in=callee, context=context),
                media_type="text/xml")

    # 1) 全局限制裁决
    allowed, failed_dir, failed_rule = evaluate_call(db, caller, callee)
    if not allowed:
        direction_label = "caller" if failed_dir == DIR_CALLER else "callee"
        pattern = getattr(failed_rule, "pattern", None)
        reason = f"denied_by_{direction_label}_rule:{pattern}" if pattern else f"denied_by_{direction_label}_rule"
        print(f"[rule-deny] rejected call {caller}->{callee} by {reason}", flush=True)
        return Response(content=build_deny_xml(reason), media_type="text/xml")

    # 方案 A：trunk 中继分支（context=trunk，IP 点对点接入点，多 AP 顺序匹配）
    if context == "trunk":
        return _cache_failover_doc(uuid, _trunk_branch(db, caller, callee, network_addr, context, uuid=uuid))

    # 方案 A：话机分支（caller 命中 sip_phone 且启用）-> 走话机分机/出局，跳过接入点限制/变换
    phone = db.scalar(select(SipPhone).where(SipPhone.phone_number == caller))
    if phone is not None:
        return _cache_failover_doc(uuid, _phone_branch(db, caller, callee, context, phone=phone, uuid=uuid))

    # 2) 解析接入点（注册用户名或 IP 白名单）
    ap, bill_unit = resolve_access_point(db, caller, network_addr)
    if ap is None:
        # G3+D2：既非注册用户、IP 也不在任一白名单 → 无接入点 → 拒绝
        return Response(content=build_deny_xml("no_access_point", caller_in=caller, callee_in=callee, context=context), media_type="text/xml")
    ap_id = ap.id
    orig_caller, orig_callee = caller, callee

    # ②a 入局 IP 强制校验（D2：注册用户免；仅 IP 模式强制）——来源 IP 须命中接入点联系地址(多值逗号分隔)
    is_registered = bool(ap.reg_username) and (ap.reg_username == caller)
    if not is_registered:
        wl = {h.strip() for h in (ap.register_host or "").split(",") if h.strip()}
        if network_addr not in wl:
            return Response(content=build_deny_xml("denied_by_access_ip", access_point_id=ap_id, caller_in=caller, callee_in=callee), media_type="text/xml")

    # ②b 接入点维度限制（用入口原始号 caller/callee）
    allowed, failed_dir, failed_rule = evaluate_call_scoped(
        db, OWNER_ACCESS_POINT, ap_id, caller, callee)
    if not allowed:
        direction_label = "caller" if failed_dir == DIR_CALLER else "callee"
        pattern = getattr(failed_rule, "pattern", None)
        reason = f"denied_by_ap_{ap_id}_{direction_label}_rule:{pattern}" if pattern else f"denied_by_ap_{ap_id}_{direction_label}_rule"
        print(f"[rule-deny] AP {ap_id} rejected {caller}->{callee} by {reason}", flush=True)
        return Response(content=build_deny_xml(reason, access_point_id=ap_id, caller_in=caller, callee_in=callee), media_type="text/xml")

    return _cache_failover_doc(uuid, _route_via_ap(db, ap, bill_unit, caller, callee, context, uuid=uuid))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/stats/concurrency")
def stats_concurrency(db: Session = Depends(get_db)):
    """P2：实时并发快照（global / 接入点 / 落地网关）+ 各维度上限。

    - global_limit 取自 settings.concurrent_limit_global（缺省 0=不限制）。
    - ap_limits / gw_limits 取自 access_point / gateway 表 concurrent_limit（默认 0=不限制）。
    """
    conc = get_concurrency()
    ap_rows = db.scalars(select(AccessPoint)).all()
    gw_rows = db.scalars(select(Gateway)).all()
    ap_limits = {r.id: int(getattr(r, "concurrent_limit", 0) or 0) for r in ap_rows}
    gw_limits = {r.id: int(getattr(r, "concurrent_limit", 0) or 0) for r in gw_rows}
    return {
        "global": conc["global"],
        "global_limit": int(settings.get("concurrent_limit_global", 0) or 0),
        "ap": conc["ap"],
        "ap_limits": ap_limits,
        "gw": conc["gw"],
        "gw_limits": gw_limits,
    }


@app.get("/cdr/{uuid}")
def get_cdr(uuid: str, db: Session = Depends(get_db)):
    cdr = db.scalar(select(Cdr).where(Cdr.uuid == uuid))
    if not cdr:
        raise HTTPException(status_code=404, detail="not found")
    return cdr.__dict__


@app.get("/cdr")
def list_cdr(limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    rows = db.scalars(select(Cdr).order_by(Cdr.id.desc()).limit(limit)).all()
    return [r.__dict__ for r in rows]


@app.get("/monitor/summary")
def monitor_summary():
    # TODO(M3 T-305): 真实并发来自 FS `show calls`（CALL 口径，A/B 不双计）
    from esl_client import _call_store

    return {"active_calls_in_store": len(_call_store)}


# ---------------------------------------------------------------------------
# P3：管理端页面
# ---------------------------------------------------------------------------
@app.get("/admin")
def admin_page(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/")
def root():
    return RedirectResponse(url="/admin")


@app.api_route("/fs/config", methods=["GET", "POST"])
async def fs_config_api(request: Request, db: Session = Depends(get_db)):
    """mod_xml_curl 配置服务（机制 A：落地网关不落盘）。

    FS 加载 sofia.conf 时请求本端点，按 DB gateway 表动态返回含 <gateways> 的 sofia.conf；
    其余配置（acl/event_socket/modules 等）回空文档，FS 回退磁盘默认。
    """
    # mod_xml_curl 不同段/方法参数位置不一致：configuration 段多为 POST+query，
    # dialplan/directory 多为 POST+form。合并两种来源，避免 key_value 解析失败回退磁盘。
    if request.method == "POST":
        try:
            _form = await request.form()
        except Exception:
            _form = {}
    else:
        _form = {}
    _q = request.query_params
    key_value = (_form.get("key_value") or _q.get("key_value")
                 or _form.get("name") or _q.get("name") or "")
    print("[fs_config] method=%s key_value=%s" % (request.method, key_value), flush=True)
    try:
        content = build_config_response(key_value, db)
    except Exception as e:
        print("[fs_config] error: %s" % e, flush=True)
        content = '<document type="freeswitch/xml"></document>'
    return Response(content=content, media_type="text/xml")
