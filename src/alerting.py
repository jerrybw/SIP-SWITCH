"""系统告警统一出口（#69 建立；顺带闭环坑位 #18「心跳告警未闭环」）。

背景：此前所有异常（落地网关心跳上下线等）只 `print`，没有任何可查询、
可推送的载体 —— 这就是坑位 #18。本模块提供统一落库出口：

    alert_if_changed(action, object_type, object_id, detail)

语义：
- 写 `operation_log`，`operator` 固定为 `"system"`，与人工操作区分。
- **状态变化才写**：同一 (object_type, object_id) 的最近一条 action 若相同则跳过，
  避免节点持续故障时每 30 秒刷一条把表灌满。
- **独立事务**：自建 session 并立即 commit，**不复用调用方的 db**。
  这样告警落库失败（如 DB 抖动、约束冲突）绝不会把调用方的状态更新一起 rollback。
  代价是告警与业务状态不保证原子 —— 告警是旁路，可接受。
- ⚠️ `operation_log.created_at` 是 NOT NULL 且模型无 default，必须显式赋值，
  否则 SQLAlchemy 会显式插入 NULL 触发 1048（这是本表此前从无写入、坑一直没暴露的原因）。

后续要接外部通道（企微/钉钉/webhook）时，从 `operation_log` 消费即可，
不必改动任何探测代码。
"""
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select

from core.sys_setting import get_setting
from db.models import OperationLog
from db.session import SessionLocal

log = logging.getLogger("alerting")

OPERATOR_SYSTEM = "system"

# ---------------------------------------------------------------------------
# Webhook 外部推送（落地网关心跳 / FS 节点心跳 分开配置）
# ---------------------------------------------------------------------------
# 两类告警对象各配一个 webhook 地址；地址为空则不下发（用户未配 = 不推送）。
# 地址存于 DB system_setting（第2类热加载配置），改完即时生效、无需重启。
WEBHOOK_KEY_BY_TYPE = {
    "gateway": "webhook_gateway_heartbeat_url",
    "fs_node": "webhook_node_heartbeat_url",
}
_WEBHOOK_TIMEOUT = 5.0  # 秒；后台线程推送，超时即放弃，绝不影响探测主流程


def _action_label(action):
    return {
        "node_offline": "FS 节点离线",
        "node_online": "FS 节点恢复",
        "node_overload": "FS 节点过载",
        "gateway_down": "落地网关离线",
        "gateway_up": "落地网关恢复",
    }.get(action, action)


# 离线原因的可读化（B2 心跳超时清扫会带 reason=heartbeat_timeout）
_REASON_LABEL = {
    "heartbeat_timeout": "心跳超时（该节点网关进程已失联）",
    "esl_probe_failed": "ESL 探测连续失败",
}


def build_alert_markdown(object_type, action, object_id, detail):
    """构造企业微信 markdown 消息体。"""
    label = _action_label(action)
    d = detail or {}
    lines = ["**%s**" % label, "> 对象: `%s` (%s)" % (object_id, object_type)]
    def add(k, t):
        if d.get(k) is not None:
            lines.append("> %s: %s" % (t, d[k]))
    add("name", "名称")
    add("host", "地址")
    add("esl_port", "ESL 端口")
    add("ip", "IP")
    add("port", "端口")
    add("fail_count", "连续失败")
    add("concurrency", "并发数")
    add("limit", "并发上限")
    add("reg_count", "注册分机数")
    add("stale_seconds", "心跳超时")
    add("threshold", "判定阈值")
    add("last_seen", "上次在线")
    if d.get("reason"):
        lines.append("> 原因: %s" % _REASON_LABEL.get(d["reason"], d["reason"]))
    return "\n".join(lines)


def push_webhook(url, content_markdown):
    """向企业微信/兼容 webhook 推送 markdown 消息；返回 (ok, msg)。

    用标准库 urllib（不引入 requests 依赖）。企业微信 webhook 期望
    {"msgtype":"markdown","markdown":{"content": "..."}}，成功返回 errcode=0。
    """
    if not url:
        return False, "empty url"
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": content_markdown},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            if j.get("errcode", 0) == 0:
                return True, "ok"
            return False, body[:200]
        except Exception:
            return True, "sent(http=%d)" % status
    except urllib.error.HTTPError as e:
        return False, "http=%d %s" % (e.code, (e.read().decode("utf-8", "replace")[:120] if e.fp else ""))
    except Exception as e:  # 超时 / DNS / 连接拒绝 等
        return False, str(e)


def _maybe_push_webhook(object_type, action, object_id, detail):
    """按对象类型选对应 webhook 地址，异步推送（不阻塞探测线程）。"""
    key = WEBHOOK_KEY_BY_TYPE.get(object_type)
    if not key:
        return
    url = get_setting(key, "")
    if not url:
        return
    md = build_alert_markdown(object_type, action, object_id, detail)
    t = threading.Thread(target=push_webhook, args=(url, md), daemon=True)
    t.start()


def alert_if_changed(action, object_type, object_id, detail=None, level="warn") -> bool:
    """状态变化时写一条系统告警；返回 True 表示已写入。

    action 建议命名：<对象>_<状态>，如 node_offline / node_online /
    node_overload / gateway_down / gateway_up。
    """
    oid = str(object_id)
    s = None
    try:
        s = SessionLocal()
        last = s.scalar(
            select(OperationLog)
            .where(OperationLog.object_type == object_type, OperationLog.object_id == oid)
            .order_by(OperationLog.id.desc())
            .limit(1)
        )
        if last is not None and last.action == action:
            return False  # 状态未变，去重
        d = dict(detail or {})
        d.setdefault("level", level)
        s.add(OperationLog(
            operator=OPERATOR_SYSTEM,
            action=action,
            object_type=object_type,
            object_id=oid,
            detail=d,
            created_at=datetime.now(timezone.utc),  # 显式赋值：该列 NOT NULL 且无模型默认值
        ))
        s.commit()
        log.warning("[ALERT] %s/%s %s %s", object_type, oid, action, d)
        # 状态变化才推送（与落库去重一致）：落地网关 / FS 节点 各自走独立 webhook 地址
        _maybe_push_webhook(object_type, action, oid, d)
        return True
    except Exception as e:
        log.warning("[ALERT] write failed (%s/%s %s): %s", object_type, oid, action, e)
        try:
            if s is not None:
                s.rollback()
        except Exception:
            pass
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
