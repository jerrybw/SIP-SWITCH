# -*- coding: utf-8 -*-
"""P1 · CDR 真源（`mod_xml_cdr`）单元测试。

覆盖 `设计稿-P1-CDR真源.md` v1.1 §9 的 **A7**（parse_xml_cdr / build_patch 全分支）
与 **A4**（未接通不计费），fixture 为**真机抓取并脱敏**的 XML CDR。

口径提醒（与本模块保持一致）：
  · XML CDR **不提供金额** —— 金额由注入的 `billing_fn` 用权威时长重算；
  · 默认 `reconcile_only`：只补缺失、不覆盖、不重算已有有效金额。
"""

import os
from datetime import datetime
from decimal import Decimal

import pytest

from cdr_truth import (
    DEFAULT_MODE,
    MODE_AUTHORITATIVE,
    build_patch,
    parse_xml_cdr,
    apply_xml_cdr,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "xml_cdr_a_leg.xml")

UUID = "0fad61b6-a704-4292-890a-0f2e43581721"


@pytest.fixture(scope="module")
def xml_text():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def fields(xml_text):
    return parse_xml_cdr(xml_text)


# ---------------------------------------------------------------------------
# parse_xml_cdr
# ---------------------------------------------------------------------------

def test_parse_identity_and_duration(fields):
    """uuid / 主被叫 / 权威时长。"""
    assert fields["uuid"] == UUID
    assert fields["caller_in"] == "80000001"
    assert fields["callee_in"] == "ccccc"
    assert fields["billsec"] == 21
    assert fields["duration"] == 21
    assert fields["bill_unit"] == 60


def test_parse_business_dimensions_from_xml(fields):
    """业务维度**直接取自 XML**（不按号码反查）。"""
    assert fields["account_id"] == 8004
    assert fields["gateway_id"] == 7
    assert fields["carrier_id"] == 4
    assert fields["caller_mid"] == "80000001"
    assert fields["callee_mid"] == "ccccc"
    assert fields["caller_out"] == "80000001"
    assert fields["callee_out"] == "ccccc"
    assert fields["caller_type"] == "phone"
    # 该通非「经接入点」呼叫 → 无 cdr_access_point_id（dialplan 才 set）
    assert fields["access_point_id"] is None


def test_parse_hangup_and_times(fields):
    """挂断原因 / sip_code / 起止时间。"""
    assert fields["hangup_cause"] == "NORMAL_CLEARING"
    assert fields["sip_code"] == 200
    assert fields["answered"] is True
    assert fields["start_time"] == datetime(2026, 9, 13, 8, 9, 51)
    assert fields["answer_time"] == datetime(2026, 9, 13, 8, 9, 51)
    assert fields["end_time"] == datetime(2026, 9, 13, 8, 10, 12)


def test_parse_url_decodes_values(fields):
    """★ 实测要点：mod_xml_cdr 输出的变量值是 URL-encoded，必须解码。"""
    assert fields["source_ip"] == "192.0.2.1"
    assert fields["dest_ip"] == "sipp-reg"
    assert fields["dest_port"] == 5060
    assert fields["switch_detail"] == ";7:ccccc:0:1:WIN"
    assert fields["record_path"].startswith("/recordings/")
    # 落库值里不应残留 %xx 转义
    for k, v in fields.items():
        if isinstance(v, str) and k != "raw":
            assert "%3A" not in v and "%20" not in v, "%s 未解码: %r" % (k, v)


def test_parse_rejects_bad_input():
    with pytest.raises(ValueError):
        parse_xml_cdr("")
    with pytest.raises(ValueError):
        parse_xml_cdr("<not-a-cdr/>")
    with pytest.raises(ValueError):
        parse_xml_cdr("<cdr><variables><direction>inbound</direction></variables></cdr>")


# ---------------------------------------------------------------------------
# build_patch —— insert 分支
# ---------------------------------------------------------------------------

def _billing_stub(record):
    def _fn(rec, talk, bill):
        record.append((rec, talk, bill))
        return (Decimal("2.1000"), Decimal("0.1000"), 8004, 1, 9,
                Decimal("1.0500"), Decimal("0.0500"), 60)
    return _fn


def test_build_patch_insert_full_row(fields):
    calls = []
    p = build_patch(fields, None, billing_fn=_billing_stub(calls), node_uuid="NODE-A")
    assert p["action"] == "insert"
    v = p["vals"]
    assert v["uuid"] == UUID
    assert v["caller_in"] == "80000001" and v["callee_in"] == "ccccc"
    assert v["talk_duration"] == 21
    assert v["bill_duration"] == 60          # ceil(21/60)*60
    assert v["end_time"] == datetime(2026, 9, 13, 8, 10, 12)
    assert v["hangup_cause"] == "NORMAL_CLEARING"
    assert v["fs_node_uuid"] == "NODE-A"
    assert v["billed"] == 0
    # 计费：用 XML 权威时长（talk=billsec）
    assert len(calls) == 1 and calls[0][1] == 21
    assert v["cost"] == Decimal("2.1000")
    assert v["profit"] == Decimal("2.1000") - Decimal("1.0500")


def test_build_patch_insert_switch_detail_marker(fields):
    """来源标记落在 switch_detail，腿信息保留，且不污染 switch_count。"""
    p = build_patch(fields, None, billing_fn=None)
    sd = p["vals"]["switch_detail"]
    assert sd[-1] == {"cdr_source": "xml_cdr"}
    assert sd[0]["gateway_id"] == 7 and sd[0]["cause"] == "WIN"
    assert p["vals"]["switch_count"] == 0


def test_build_patch_insert_without_attribution_skips_billing(fields):
    """无任何业务归属 → 不计费（避免把费用记到错误主体）。"""
    calls = []
    f2 = dict(fields)
    for k in ("account_id", "access_point_id", "caller_mid", "callee_mid"):
        f2[k] = None
    p = build_patch(f2, None, billing_fn=_billing_stub(calls), node_uuid="NODE-A")
    assert p["action"] == "insert"
    assert calls == []
    assert p["reason"] == "no_row+no_attribution"
    assert p["vals"]["cost"] == 0


# ---------------------------------------------------------------------------
# build_patch —— 已有行：只补缺失 / 重算
# ---------------------------------------------------------------------------

def _existing(**over):
    base = {
        "id": 1,
        "uuid": UUID,
        "caller_in": "80000001",
        "callee_in": "ccccc",
        "cost": Decimal("1.5000"),
        "cost_price": Decimal("0.5000"),
        "created_at": datetime(2026, 9, 13, 8, 9, 0),
        "hangup_cause": None,
        "end_time": None,
        "answer_time": None,
        "talk_duration": None,
        "bill_duration": 0,
        "bill_unit": 60,
        "switch_detail": None,
        "switch_count": 0,
        "record_status": 0,
        "record_path": None,
    }
    base.update(over)
    return base


def test_existing_with_valid_cost_only_fills_missing(fields):
    """★ 正常呼叫零变化：金额已有效 → 只补缺失，**绝不重算**。"""
    calls = []
    p = build_patch(fields, _existing(), billing_fn=_billing_stub(calls))
    assert p["action"] == "fill"
    assert calls == []                                   # 未调计费
    assert p["vals"]["cost"] == Decimal("1.5000")        # 原有金额不变
    assert p["vals"]["cost_price"] == Decimal("0.5000")
    assert p["vals"]["hangup_cause"] == "NORMAL_CLEARING"  # 缺失被补上
    assert p["vals"]["end_time"] == datetime(2026, 9, 13, 8, 10, 12)
    assert p["vals"]["created_at"] == datetime(2026, 9, 13, 8, 9, 0)  # 不二次 bump
    assert p["preserve_cols"] == ("created_at",)
    assert "id" not in p["vals"]


def test_existing_does_not_override_present_values(fields):
    """reconcile_only：已有非空值一律不覆盖（含 hangup_cause）。"""
    p = build_patch(fields, _existing(hangup_cause="UNKNOWN"), billing_fn=None)
    assert p["vals"]["hangup_cause"] == "UNKNOWN"


def test_existing_authoritative_mode_overrides(fields):
    """authoritative 模式（默认不启用）才允许覆盖原因/时长。"""
    p = build_patch(fields, _existing(hangup_cause="UNKNOWN"),
                    mode=MODE_AUTHORITATIVE, billing_fn=None)
    assert p["vals"]["hangup_cause"] == "NORMAL_CLEARING"


def test_existing_cost_zero_recomputes(fields):
    """★ 兜底主场景（reconcile 骨架）：金额为 0 且有权威时长 → 重算。"""
    calls = []
    p = build_patch(fields, _existing(cost=0, hangup_cause="UNKNOWN"),
                    billing_fn=_billing_stub(calls))
    assert p["action"] == "recompute"
    assert len(calls) == 1 and calls[0][1] == 21
    assert p["vals"]["cost"] == Decimal("2.1000")
    assert p["vals"]["talk_duration"] == 21
    assert p["vals"]["bill_duration"] == 60
    assert p["vals"]["hangup_cause"] == "UNKNOWN"   # 非 authoritative 不覆盖
    assert p["preserve_cols"] == ("created_at",)


def test_existing_not_answered_no_billing(fields):
    """A4：未接通（billsec=0）不产生金额（仅接通计费口径不变）。"""
    calls = []
    f2 = dict(fields, billsec=0, duration=0, answer_time=None, answered=False)
    p = build_patch(f2, _existing(cost=0), billing_fn=_billing_stub(calls))
    assert p["action"] == "fill"
    assert calls == []
    assert p["vals"]["cost"] == 0
    assert p["reason"].startswith("not_answered")


def test_existing_no_attribution_no_billing(fields):
    calls = []
    f2 = dict(fields)
    for k in ("account_id", "access_point_id", "caller_mid", "callee_mid"):
        f2[k] = None
    p = build_patch(f2, _existing(cost=0), billing_fn=_billing_stub(calls))
    assert p["action"] == "fill"
    assert calls == []
    assert p["reason"].startswith("no_attribution")


def test_default_mode_is_reconcile_only():
    assert DEFAULT_MODE == "reconcile_only"


# ---------------------------------------------------------------------------
# apply_xml_cdr —— 编排（全注入，无 DB）
# ---------------------------------------------------------------------------

def test_apply_xml_cdr_wiring(xml_text):
    """入队 → 队列内读行 → build_patch → upsert（带 preserve_cols）。"""
    enqueued = []
    upserts = []

    def _enqueue_fn(uid, job):
        enqueued.append(uid)
        job(None)                      # 模拟 cdr-writer 单线程执行

    def _row_loader(db, uid):
        return _existing(cost=0)

    def _upsert_fn(vals, db=None, preserve_cols=()):
        upserts.append((vals, preserve_cols))
        return True

    out = apply_xml_cdr(xml_text, row_loader=_row_loader, upsert_fn=_upsert_fn,
                        enqueue_fn=_enqueue_fn,
                        billing_fn=_billing_stub([]), node_uuid="NODE-A")

    assert out["uuid"] == UUID
    assert enqueued == [UUID]
    assert len(upserts) == 1
    vals, preserve = upserts[0]
    assert vals["cost"] == Decimal("2.1000")
    assert preserve == ("created_at",)


def test_apply_xml_cdr_rejects_bad_xml():
    with pytest.raises(ValueError):
        apply_xml_cdr("<nope/>", row_loader=lambda db, u: None,
                      upsert_fn=lambda *a, **k: True,
                      enqueue_fn=lambda uid, job: job(None))
