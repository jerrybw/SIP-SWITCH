# -*- coding: utf-8 -*-
"""P1 · R1 回归：`preserve_cols` 降级保护（需真 MySQL，随 DB 用例在容器内执行）。

背景（设计稿 v1.1 §13.1）：`_upsert_cdr_dict` 的 `upd` 是**全列覆盖**，因此
「ESL 降级后到」会把 XML CDR 真源（P1）已算好的金额**清零**。修法是给
`_upsert_cdr_dict` 加 `preserve_cols`，降级腿把计费组列排除在 UPDATE 之外。

本测试直接打真库，断言「同一 uuid 的金额不被降级写入清零」。
"""

import uuid as _uuid
from datetime import datetime

import pytest

from esl_client import BILLING_COLS, _upsert_cdr_dict
from db.session import SessionLocal
from db.models import Cdr
from sqlalchemy import select


UID = "test-p1-preserve-" + str(_uuid.uuid4())

#: Cdr 全列 —— `_upsert_cdr_dict(ignore_existing=False)` 的 vals **必须全列宽**：
#: MySQL 8 的 `INSERT ... AS new ON DUPLICATE KEY UPDATE` 里 `new.<col>` 引用的列
#: 必须出现在 INSERT 列清单中，否则 `1054 Unknown column 'new.xxx'`。
COLS = [c.name for c in Cdr.__table__.columns if c.name != "id"]


def _base_vals(**over):
    now = datetime.utcnow()
    vals = {c: None for c in COLS}
    vals.update({
        "uuid": UID,
        "caller_in": "80000001",
        "callee_in": "ccccc",
        "start_time": now,
        "created_at": now,
        "end_time": now,
        "answer_time": now,
        "bill_unit": 60,
        "bill_duration": 60,
        "talk_duration": 21,
        "cost": 0,
        "cost_price": 0,
        "profit": 0,
        "billed": 0,
    })
    vals.update(over)
    return vals


def _read():
    db = SessionLocal()
    try:
        return db.scalar(select(Cdr).where(Cdr.uuid == UID))
    finally:
        db.close()


def _cleanup():
    db = SessionLocal()
    try:
        row = db.scalar(select(Cdr).where(Cdr.uuid == UID))
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _isolate():
    _cleanup()
    yield
    _cleanup()


def test_billing_cols_content():
    """常量内容固定（改动需同步设计稿 §13.1）。"""
    assert set(BILLING_COLS) == {
        "cost", "rate_used", "cost_price", "cost_rate_used",
        "cost_bill_unit", "profit", "bill_duration", "talk_duration",
    }


def test_preserve_cols_keeps_amount_intact():
    """★ R1 核心：带 preserve_cols 的降级写入**不得**把已有金额清零。"""
    assert _upsert_cdr_dict(_base_vals(cost=1.5, talk_duration=21, bill_duration=60))
    row = _read()
    assert row is not None and float(row.cost) == 1.5

    # 模拟「ESL 降级后到」：cost=0 / talk=None，但带降级保护
    assert _upsert_cdr_dict(
        _base_vals(cost=0, cost_price=0, profit=0, talk_duration=None, bill_duration=0),
        preserve_cols=BILLING_COLS)

    row = _read()
    assert float(row.cost) == 1.5, "降级写入把金额清零了 —— R1 保护失效"
    assert row.talk_duration == 21
    assert row.bill_duration == 60


def test_without_preserve_cols_behavior_unchanged():
    """默认（无 preserve_cols）行为与改造前一致：全列覆盖会写 0。"""
    assert _upsert_cdr_dict(_base_vals(cost=1.5))
    assert _upsert_cdr_dict(_base_vals(cost=0, talk_duration=None, bill_duration=0))
    row = _read()
    assert float(row.cost) == 0
    assert row.talk_duration is None


def test_ignore_existing_still_protects_terminal_state():
    """预落库路径（ignore_existing）行为不受本次改动影响。"""
    assert _upsert_cdr_dict(_base_vals(cost=2.0, hangup_cause="NORMAL_CLEARING"))
    assert _upsert_cdr_dict(_base_vals(cost=0, hangup_cause=None), ignore_existing=True)
    row = _read()
    assert float(row.cost) == 2.0
    assert row.hangup_cause == "NORMAL_CLEARING"
