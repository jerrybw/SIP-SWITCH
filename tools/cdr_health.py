#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CDR 健康检查：识别「reconcile 回填」的可疑话单（PITFALLS #73）。

背景
----
ESL 主路径处理失败时（事件处理异常 / CDR 落库失败），该腿会残留在 `_call_store`，
由 `esl-reconcile` 线程事后回填骨架 CDR。这类行的指纹是：

    hangup_cause = 'UNKNOWN'  AND  fs_node_uuid IS NULL

（正常落库的 CDR 一定带 `fs_node_uuid` —— `_save_cdr` 写入 `NODE_UUID`。）

因此这两列的组合是一个**便宜且强**的健康判据：只要出现，就说明 ESL 主路径没处理成功。
这正是「话单 UNKNOWN + 缺字段」类问题（曾静默数小时）的自动判据。

用法（需连 MySQL，通常在 gateway 容器内跑）
----------------------------------------
    docker cp tools/cdr_health.py <gateway>:/app/tools/cdr_health.py
    docker exec <gateway> python /app/tools/cdr_health.py --minutes 60

退出码：0 = 健康；1 = 存在可疑行（便于 E2E / CI 断言）。
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sqlalchemy import text  # noqa: E402

from db.session import SessionLocal  # noqa: E402

SUSPECT_WHERE = "hangup_cause = 'UNKNOWN' AND fs_node_uuid IS NULL"


def main() -> int:
    ap = argparse.ArgumentParser(description="CDR 健康检查（reconcile 回填识别）")
    ap.add_argument("--minutes", type=int, default=60, help="回看窗口（分钟），默认 60")
    args = ap.parse_args()

    since = datetime.utcnow() - timedelta(minutes=args.minutes)
    db = SessionLocal()
    try:
        total = db.execute(
            text("SELECT COUNT(*) FROM cdr WHERE created_at >= :s"), {"s": since}
        ).scalar() or 0
        bad = db.execute(
            text("SELECT COUNT(*) FROM cdr WHERE created_at >= :s AND " + SUSPECT_WHERE),
            {"s": since},
        ).scalar() or 0

        print("[cdr-health] 窗口 %d 分钟：总 CDR = %d，可疑（reconcile 回填）= %d"
              % (args.minutes, total, bad))

        if not bad:
            print("[cdr-health] OK：无 reconcile 回填行")
            return 0

        rows = db.execute(
            text("SELECT uuid, caller_in, callee_in, end_time FROM cdr "
                 "WHERE created_at >= :s AND " + SUSPECT_WHERE +
                 " ORDER BY id DESC LIMIT 10"),
            {"s": since},
        ).all()
        print("[cdr-health] 明细（最多 10 条）：")
        for r in rows:
            print("  %s  %s -> %s  end=%s" % (str(r[0])[:8], r[1], r[2], r[3]))
        print("[cdr-health] ！可疑行 > 0：ESL 主路径未成功处理该腿 HANGUP。")
        print("[cdr-health]   排查：① docker logs <gateway> | grep 'handle error'")
        print("[cdr-health]         ② docker logs <gateway> | grep -E 'CDR.*failed|Data too long'")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
