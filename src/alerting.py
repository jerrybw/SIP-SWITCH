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
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import OperationLog
from db.session import SessionLocal

log = logging.getLogger("alerting")

OPERATOR_SYSTEM = "system"


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
