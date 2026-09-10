"""落地网关 provision（机制 A：不落盘）+ 多节点下发同步。

落地网关定义现由网关应用经 mod_xml_curl 的 configuration 段动态下发
（src/fs_sofia_config.build_sofia_conf），FS 侧零落盘。管理端保存/删除网关后，
只需让 FS 重新拉取 sofia.conf 即可，不再写任何 XML 文件——故本模块只保留 rescan 触发。

⚠️ 关键前提（2026-09-09 实测，见 PITFALLS #31）：
FS 的 `sofia profile <prof> rescan` **不会重建已存在的 gateway** —— 只有该 gateway
在 FS 里不存在时才会按最新 XML 创建；已存在的对象会原样保留旧参数（proxy/realm/port…）。
因此「改完网关点保存，FS 里还是旧值」的根因不是"没触发 rescan"，而是 rescan 对存量
gateway 无效。正确姿势：先 `killgw <name>` 销毁旧对象，再 rescan 让 FS 重建。

⚠️ 多节点同步（2026-09-10，PITFALLS #44）：
每个 FS 节点各配一个网关实例，共享同一 MySQL，但**每个实例只能对自己那台 FS 发 ESL 命令**。
所以「在 node1 的 Web 上改网关，FS2 不会自动刷新」是默认行为 —— 本模块负责消除它。

做法：用 DB `system_setting` 当跨节点信令
- `provision_seq`：变更版本号，每次网关增删改 +1
- `provision_pending`：最近变更的网关名 JSON 数组（让各节点能精确 killgw 重建）

每个节点跑一个 ProvisionWatcher 后台线程轮询 seq，发现变化就对本节点 FS 补做
killgw + rescan。

**为什么不直连其它节点的 ESL 广播**：生产多节点通常只共享 DB / Redis，节点间网络
未必互通；且离线节点在线后能自动补齐（版本号一直落后，回来就补）。
代价：其它节点有 ≤ 轮询周期的延迟（默认 5s，热配 key = provision_sync_interval）。
本节点（发起变更的那个）仍是即时 rescan，无延迟。
"""
import json
import logging
import threading

try:
    from fs_esl_cmd import fs_api
except ImportError:
    from src.fs_esl_cmd import fs_api

log = logging.getLogger("fs_provision")

PROF = "external"

# 第2类配置 key（多节点下发同步）
KEY_SEQ = "provision_seq"
KEY_PENDING = "provision_pending"
KEY_INTERVAL = "provision_sync_interval"
DEFAULT_SYNC_INTERVAL = 5
_MAX_PENDING_JSON = 480  # system_setting.value 是 varchar(512)

# 当前进程的 watcher 实例（由 start_provision_watcher 赋值）：
# force_all_nodes_rescan 需要把它的位点对齐，避免本节点刚全量重建完又被自己触发一次。
_WATCHER = None


def _sys_setting():
    """延迟 import，避免与 db.session 的启动期迁移互相拖累。"""
    try:
        from core import sys_setting
    except ImportError:  # pragma: no cover
        from src.core import sys_setting
    return sys_setting


def bump_pending(names):
    """登记一次网关变更，供其它节点经 DB 感知后补扫。

    :param names: 网关名列表（可含 None，自动去重去空）；改名场景须同时传旧名与新名
    :return: 新的 provision_seq
    """
    ss = _sys_setting()
    seq = ss.get_int_setting(KEY_SEQ, 0) + 1
    try:
        cur = json.loads(ss.get_setting(KEY_PENDING, "[]") or "[]")
        if not isinstance(cur, list):
            cur = []
    except Exception:
        cur = []

    merged = []
    for n in list(cur) + [str(n) for n in names if n]:
        if n not in merged:
            merged.append(n)

    s = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
    while len(s) > _MAX_PENDING_JSON and len(merged) > 1:
        merged.pop(0)  # 超长时丢最老的
        s = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))

    ss.set_setting(KEY_SEQ, str(seq), "网关下发变更版本号(每次增删改+1，各节点据此补扫)")
    ss.set_setting(KEY_PENDING, s, "最近变更的网关名 JSON 数组(供各节点精确 killgw)")
    return seq


def rescan(prof=PROF, gw_name=None):
    """异步重扫 sofia profile：触发 FS 经 xml_curl 重新拉取 sofia.conf（含最新 DB 网关）。

    传 gw_name 时会先 `killgw <gw_name>`：强制 FS 丢弃该网关的旧对象，
    随后的 rescan 才会用新配置重建它（改 IP/端口/账号等场景必须如此，否则改完不生效）。

    :param prof: sofia profile 名
    :param gw_name: 需要强制重建的网关名；None 表示只做普通 rescan（新增/删除场景）
    """
    def _run():
        try:
            if gw_name:
                try:
                    r = fs_api("sofia profile %s killgw %s" % (prof, gw_name))
                    log.info("killgw %s: %s", gw_name, (r or "").strip()[:120])
                except Exception as e:
                    # 网关本就不存在（新增场景）时 killgw 会失败，忽略即可
                    log.debug("killgw %s skipped: %s", gw_name, e)
            out = fs_api("sofia profile %s rescan" % prof)
            if out and out.strip():
                log.info("rescan %s: %s", prof, out.strip())
            else:
                log.warning("rescan %s: no output (ESL/fs_cli 均不可用?)", prof)
        except Exception as e:
            log.warning("rescan %s failed: %s", prof, e)
    threading.Thread(target=_run, daemon=True).start()
    return "async"


def resync(names, prof=PROF):
    """多节点补扫：先逐个 killgw，最后统一 rescan 一次（避免 N 次 profile 重扫）。

    幂等：重复执行无害。
    """
    def _run():
        try:
            for n in names or []:
                try:
                    r = fs_api("sofia profile %s killgw %s" % (prof, n))
                    log.debug("resync killgw %s: %s", n, (r or "").strip()[:80])
                except Exception as e:
                    log.debug("resync killgw %s skipped: %s", n, e)
            out = fs_api("sofia profile %s rescan" % prof)
            log.info("resync gateway(s) %s: rescan %s", names, (out or "").strip()[:120])
        except Exception as e:
            log.warning("resync %s failed: %s", names, e)
    threading.Thread(target=_run, daemon=True).start()
    return "async"


def provision(gw, old_name=None):
    """网关变更后触发 FS 重新拉取 sofia.conf（不再写盘）。

    注意必须先 killgw 同名旧对象，否则 FS 会保留旧 proxy/realm（rescan 对存量网关无效）。
    同时 bump `provision_seq`，让其它 FS 节点经 DB 感知后自行补扫（多节点必需）。
    """
    try:
        name = getattr(gw, "name", None)
        try:
            seq = bump_pending([old_name, name])
            log.info("provision bump seq=%s gws=%s", seq, [n for n in (old_name, name) if n])
        except Exception as e:
            log.warning("bump provision_seq failed (其它节点刷新可能延迟): %s", e)
        o = rescan(gw_name=name)
        return {"ok": True, "rescan": o.strip(),
                "note": "gateway now served via xml_curl, no disk write"}
    except Exception as e:
        log.error("provision fail: %s", e)
        return {"ok": False, "error": str(e)}


def remove_xml(n):
    """网关删除后触发 FS 重新拉取 sofia.conf（网关将从 DB 消失）。"""
    try:
        try:
            bump_pending([n])
        except Exception as e:
            log.warning("bump provision_seq failed: %s", e)
        rescan(gw_name=n)
        return True
    except Exception:
        return False


def rescan_all(prof=PROF):
    """全量重建**本节点**的落地网关：逐个 killgw 后统一 rescan 一次。

    为什么不是只 `rescan`：`sofia profile rescan` 对已存在的 gateway 无效（PITFALLS #30），
    想真正重建必须先把旧对象 kill 掉。用于「立即全节点重扫」按钮与人工兜底。
    """
    names = local_gateway_names()
    log.info("rescan_all: rebuild %d gateway(s) on this node: %s", len(names), names)
    return resync(names, prof=prof)


def local_gateway_names():
    """本节点可见的落地网关名（与 fs_sofia_config._gateways_block 同一套过滤规则）。

    注册型(1) 只取归属本 NODE_UUID 的；点对点(0) 全量。
    """
    try:
        from sqlalchemy import select
        from db.session import SessionLocal
        from db.models import Gateway, GatewayNode
        from core.config import NODE_UUID
    except ImportError:  # pragma: no cover
        from src.db.session import SessionLocal  # type: ignore
        from src.db.models import Gateway, GatewayNode  # type: ignore
        from src.core.config import NODE_UUID  # type: ignore
        from sqlalchemy import select  # type: ignore
    db = SessionLocal()
    try:
        if NODE_UUID:
            mine = set(db.scalars(select(GatewayNode.gateway_id).where(
                GatewayNode.node_uuid == NODE_UUID)).all())
        else:
            mine = set()
        out = []
        for g in db.scalars(select(Gateway)).all():
            if int(getattr(g, "auth_type", 0) or 0) == 1 and g.id not in mine:
                continue
            out.append(g.name)
        return out
    except Exception as e:
        log.warning("list local gateways failed: %s", e)
        return []
    finally:
        db.close()


def force_all_nodes_rescan():
    """「立即全节点重扫」：让**所有**节点立刻全量重建落地网关。

    做法：bump `provision_seq` 并把 `provision_pending` 清空 —— 各节点的
    ProvisionWatcher 看到 seq 变化且名单为空，就退化成一次全量 rescan（见 _sync_once）；
    本节点则**立即**执行，不等轮询周期。

    :return: 新的 provision_seq
    """
    ss = _sys_setting()
    seq = ss.get_int_setting(KEY_SEQ, 0) + 1
    ss.set_setting(KEY_SEQ, str(seq), "网关下发变更版本号(每次增删改+1，各节点据此补扫)")
    ss.set_setting(KEY_PENDING, "[]", "最近变更的网关名 JSON 数组(供各节点精确 killgw)")
    try:
        rescan_all()
    except Exception as e:
        log.warning("local full rescan failed: %s", e)
    if _WATCHER is not None:
        # 本节点刚做过全量重建，把位点对齐，避免 watcher 下一轮再重复扫一次
        _WATCHER._last_seq = seq
    log.info("force_all_nodes_rescan: seq=%s (local rebuilt immediately)", seq)
    return seq


class ProvisionWatcher:
    """监听 DB 中的网关下发变更，对本节点 FS 补做 killgw + rescan。

    多节点共享 DB 时，任意节点上的网关增删改都会 bump `provision_seq`；
    本线程发现版本号变化后，按 `provision_pending` 名单重建这些网关,
    使「在别的节点管理端做的变更」也落到本节点 FS 上。

    幂等性：各节点轮询周期不同步，重复 killgw/rescan 无害，故不需要精确消费位点。
    """

    def __init__(self, interval: int = DEFAULT_SYNC_INTERVAL):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="provision-sync")
        self._last_seq = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _sync_once(self):
        ss = _sys_setting()
        seq = ss.get_int_setting(KEY_SEQ, 0)
        if seq == self._last_seq:
            return
        if self._last_seq is None:
            # 首轮只对齐位点：启动时 gw_bootstrap 已负责初始下发，避免无谓重扫
            self._last_seq = seq
            log.info("[PS] provision watcher online at seq=%s", seq)
            return

        pending = []
        try:
            pending = json.loads(ss.get_setting(KEY_PENDING, "[]") or "[]")
            if not isinstance(pending, list):
                pending = []
        except Exception:
            pending = []

        if pending:
            log.info("[PS] seq %s -> %s: resync %s", self._last_seq, seq, pending)
            resync(pending)
        else:
            # 没有精确名单（「立即全节点重扫」会显式清空 pending）：做**全量重建**。
            # 注意不能退化成裸 rescan —— rescan 对已存在的 gateway 无效（PITFALLS #30），
            # 那样点一下按钮等于什么都没发生。
            log.info("[PS] seq %s -> %s: full rebuild (no pending names)", self._last_seq, seq)
            rescan_all()
        self._last_seq = seq

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sync_once()
            except Exception as e:
                log.warning("[PS] loop error: %s", e)
            # 周期每次重读，改 system_setting 后立即生效
            self._stop.wait(_sys_setting().get_int_setting(KEY_INTERVAL, self.interval))


def start_provision_watcher(interval: int = DEFAULT_SYNC_INTERVAL) -> "ProvisionWatcher":
    global _WATCHER
    w = ProvisionWatcher(interval=interval)
    w.start()
    _WATCHER = w  # 供 force_all_nodes_rescan 对齐位点（避免本节点重复全量重扫）
    return w
