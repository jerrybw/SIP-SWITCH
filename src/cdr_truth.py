# -*- coding: utf-8 -*-
"""P1 · CDR 真源（FreeSWITCH `mod_xml_cdr`）。

背景（`docs/ROADMAP.md` §7「#75 修复记录」遗留 ①）：
    CDR 完全由 ESL 事件拼装（`esl_client._save_cdr`）。异常路径（丢 HANGUP /
    网关重启错过事件）下只剩 `esl-reconcile` 回填的骨架 —— 时长是估算值、
    金额为空。本模块引入 FS 自带 `mod_xml_cdr` 产出的**权威 XML CDR** 做兜底。

核心口径（最容易搞错，务必对齐）：
    **XML CDR 不提供金额**（FS 不知道我们的费率）。它给的是**权威时长**
    （`billsec`/`duration`）。所以「金额兜底」= 用 XML 的权威时长 +
    我们自己的费率链`_compute_billing` **重算**，不是从 XML 里读金额。

设计稿：`设计稿-P1-CDR真源.md` v1.1（§13 定稿修订）。

纯函数边界：
    `parse_xml_cdr` / `build_patch` **不碰 DB、不 import esl_client** ——
    DB 相关能力（计费、落库、入队）全部由调用方**注入**，
    因此本模块可直接单测（对齐 `recordings.py` 的纯函数风格）。

实测要点（2026-09-13 真机 fixture，见 `tests/fixtures/`）：
    · XML 结构：`<cdr>` 下 `channel_data` / `call-stats` / `variables` /
      `app_log` / `callflow`；**业务字段全在 `<variables>` 里**，
      且**值是 URL-encoded**（`%20`/`%3A`/`%3B`/`%40`）→ 取值必须 unquote。
    · 业务维度变量：`cdr_account_id` / `cdr_access_point_id` / `cdr_caller_in` /
      `cdr_callee_in` / `cdr_caller_mid` / `cdr_callee_mid` / `cdr_gateway_id` /
      `cdr_carrier_id` / `cdr_caller_type` / `cdr_dst_ip` / `cdr_dst_port` /
      `cdr_bill_unit` / `cdr_switch_detail`。
      注意 `cdr_access_point_id` **仅「经接入点」的呼叫才有**（dialplan 才 set）。
    · 权威时长：`billsec` / `duration`（秒），另有 `billmsec` / `flow_billsec`。
    · 时间戳：`start_epoch` / `answer_epoch` / `end_epoch`（秒），
      另有 `*_uepoch`（微秒）；`0` 表示该阶段未发生。
"""

from __future__ import annotations

import math
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

__all__ = [
    "DEFAULT_MODE",
    "MODE_AUTHORITATIVE",
    "FILL_IF_EMPTY_COLS",
    "AUTHORITATIVE_COLS",
    "BILLING_COLS",
    "XML_SOURCE",
    "MAX_PAYLOAD_BYTES",
    "parse_xml_cdr",
    "build_patch",
    "apply_xml_cdr",
    "extract_cdr_xml",
    "make_esl_wiring",
]

#: `/fs/cdr` 请求体上限（§13.3 #5：超限 413）。实测 XML CDR ~23KB，256KB 留足余量。
MAX_PAYLOAD_BYTES = 256 * 1024

#: 判定「这通呼叫是本系统经手/关注的」的通道变量 —— FS 对**每个**通道都会产出
#: XML CDR，若不加此闸门，无关呼叫会在话单里插出业务维度全 NULL 的垃圾行。
CDR_IDENTITY_VARS = (
    "cdr_account_id", "cdr_access_point_id",
    "cdr_caller_in", "cdr_callee_in", "cdr_caller_mid", "cdr_callee_mid",
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_MODE = "reconcile_only"     # 只兜底、不覆盖（默认）
MODE_AUTHORITATIVE = "authoritative"  # XML 优先覆盖原因/时长（默认不启用）

#: 标记本行金额/终态来自 XML CDR（落在 `switch_detail` 扩展位）
XML_SOURCE = "xml_cdr"

#: 「只补不覆盖」允许回填的列 —— 仅当现值**为空**(None/"")时才写。
#: 注意：0 是合法值（switch_count / record_status），不作为「空」。
FILL_IF_EMPTY_COLS = (
    "end_time", "answer_time", "ring_time",
    "hangup_cause", "sip_code",
    "talk_duration", "bill_duration", "bill_unit",
    "caller_out", "callee_out",
    "gateway_id", "carrier_id", "access_point_id", "account_id",
    "business_id", "customer_id",
    "source_ip", "source_port", "dest_ip", "dest_port",
    "switch_count", "switch_detail",
    "record_status", "record_path",
)

#: authoritative 模式下允许被 XML 覆盖的列（默认模式不覆盖）
AUTHORITATIVE_COLS = (
    "hangup_cause", "sip_code",
    "end_time", "answer_time",
    "talk_duration", "bill_duration",
)

#: 计费相关列（重算时整组写入，避免只写一半）
BILLING_COLS = (
    "cost", "rate_used", "cost_price", "cost_rate_used",
    "cost_bill_unit", "profit", "bill_duration", "talk_duration",
)

#: 判定「这通有业务归属、可以计费」的维度键（任一存在即可）。
#: 都不存在 → 不重算金额（避免把费用记到错误主体上）。
ATTRIBUTION_KEYS = ("account_id", "access_point_id", "caller_mid", "callee_mid")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _unquote(s):
    """mod_xml_cdr 输出的变量值是 URL-encoded（实测），取值统一解码。

    不含 `%` 时直接返回（避免对普通文本做无谓处理）；解码异常时原样返回。
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return ""
    if "%" not in s:
        return s
    try:
        return urllib.parse.unquote(s)
    except Exception:      # noqa: BLE001 —— 解码失败不该影响整通 CDR
        return s


def _safe_int(v):
    try:
        if v is None or v == "":
            return None
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _epoch_to_dt(sec):
    """FS 的 `*_epoch` → naive UTC datetime；0 / 空 / 负数 → None。"""
    n = _safe_int(sec)
    if not n or n <= 0:
        return None
    try:
        return datetime.utcfromtimestamp(n)
    except (OverflowError, OSError, ValueError):
        return None


def _now():
    return datetime.utcnow()


def _is_empty(v):
    """「空缺」判定：None 或空串。**0 不算空**（switch_count/record_status 合法值）。"""
    return v is None or v == ""


# ---------------------------------------------------------------------------
# 1) 解析
# ---------------------------------------------------------------------------

def parse_xml_cdr(xml_text, include_raw=False):
    """XML CDR → 规范化字段字典。

    只做「取值 + 归一化」，不做任何业务判断（判断在 `build_patch`）。

    返回键（缺失为 None）::

        uuid, start_time, answer_time, end_time, ring_time,
        billsec, duration, hangup_cause, sip_code, bill_unit,
        account_id, access_point_id, gateway_id, carrier_id,
        caller_in, callee_in, caller_mid, callee_mid, caller_out, callee_out,
        caller_type, source_ip, source_port, dest_ip, dest_port,
        switch_detail, record_path, fs_hostname, core_uuid, answered

    include_raw=True 时额外返回 `raw`（全部通道变量，便于排查，不落库）。

    :raises ValueError: 非 XML / 非 xml_cdr 输出 / 缺 uuid。
    """
    if xml_text is None:
        raise ValueError("xml cdr 内容为空")
    if isinstance(xml_text, bytes):
        xml_text = xml_text.decode("utf-8", "replace")
    if not str(xml_text).strip():
        raise ValueError("xml cdr 内容为空")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError("XML 解析失败: %s" % e) from e

    if root.tag != "cdr":
        inner = root.find("cdr")
        if inner is None:
            raise ValueError("不是 xml_cdr 输出（root=<%s>）" % root.tag)
        root = inner

    vars_node = root.find("variables")
    raw = {}
    if vars_node is not None:
        for el in list(vars_node):
            # 同名元素（如 DP_MATCH 会出现多次）取**首次**值：首次即原始赋值，
            # 后续多为流程内重复 set，语义上不如首次稳定。
            if el.tag not in raw:
                raw[el.tag] = _unquote(el.text)

    def g(key):
        v = raw.get(key)
        return v if v not in ("", None) else None

    uuid = g("uuid") or g("call_uuid")
    if not uuid:
        raise ValueError("xml cdr 缺少 uuid（既无 uuid 也无 call_uuid）")

    # 时间：优先 *_epoch（秒，稳健），回落 *_stamp 文本
    start_time = _epoch_to_dt(g("start_epoch")) or _parse_stamp(g("start_stamp"))
    answer_time = _epoch_to_dt(g("answer_epoch"))
    end_time = _epoch_to_dt(g("end_epoch")) or _parse_stamp(g("end_stamp"))
    # 振铃时刻：progress_media_epoch = 回铃音开始（比 progress_epoch 更贴近「振铃」）
    ring_time = _epoch_to_dt(g("progress_media_epoch")) or _epoch_to_dt(g("progress_epoch"))

    billsec = _safe_int(g("billsec"))
    if billsec is None:
        ms = _safe_int(g("billmsec"))
        billsec = int(ms // 1000) if ms else None
    duration = _safe_int(g("duration"))
    if duration is None:
        ms = _safe_int(g("mduration"))
        duration = int(ms // 1000) if ms else None

    fields = {
        "uuid": str(uuid),
        "start_time": start_time,
        "answer_time": answer_time,
        "end_time": end_time,
        "ring_time": ring_time,
        "answered": answer_time is not None,
        "billsec": billsec or 0,
        "duration": duration or 0,
        "hangup_cause": g("hangup_cause") or g("bridge_hangup_cause"),
        "sip_code": _safe_int(g("sip_term_status"))
                    or _safe_int(g("sip_invite_failure_status")),
        "bill_unit": _safe_int(g("cdr_bill_unit")),
        # 业务维度 —— 一律**直接取 XML**，不按号码反查（口径见设计稿 §13.3 #1）
        "account_id": _safe_int(g("cdr_account_id")),
        "access_point_id": _safe_int(g("cdr_access_point_id")),
        "gateway_id": _safe_int(g("cdr_gateway_id")),
        "carrier_id": _safe_int(g("cdr_carrier_id")),
        "caller_in": g("cdr_caller_in") or g("caller_id_number"),
        "callee_in": g("cdr_callee_in") or g("sip_to_user"),
        "caller_mid": g("cdr_caller_mid"),
        "callee_mid": g("cdr_callee_mid"),
        "caller_out": g("cdr_caller_out"),
        "callee_out": g("cdr_callee_out"),
        "caller_type": g("cdr_caller_type"),
        # 源/目的：与 ESL 路径口径一致（ESL 用 variable_sip_network_ip）
        "source_ip": g("sip_network_ip") or g("sip_received_ip"),
        "source_port": _safe_int(g("sip_network_port") or g("sip_received_port")),
        "dest_ip": g("cdr_dst_ip"),
        "dest_port": _safe_int(g("cdr_dst_port")),
        "switch_detail": g("cdr_switch_detail"),
        "record_path": g("rec_file"),
        "fs_hostname": g("FreeSWITCH-Hostname"),
        "core_uuid": g("Core-UUID"),
        # 是否为「本系统经手」的呼叫（决定能否新建 CDR 行，见 _patch_insert）
        "has_cdr_identity": any(raw.get(k) for k in CDR_IDENTITY_VARS),
    }
    if include_raw:
        fields["raw"] = raw
    return fields


def _parse_stamp(value):
    """回落解析 `*_stamp`（形如 `2026-09-13 08:09:51`，可能带 .微秒）。"""
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 2) 补丁决策
# ---------------------------------------------------------------------------

def _bill_seconds(talk, bill_unit):
    """按「向上取整到计费单位」算计费秒数（与 `_save_cdr` 同口径）。"""
    unit = bill_unit or 60
    if not talk or talk <= 0:
        return 0
    return int(math.ceil(talk / unit) * unit)


def _has_attribution(fields):
    return any(fields.get(k) for k in ATTRIBUTION_KEYS)


def _xml_fill_values(fields):
    """XML 能为「补缺失」提供的列值（不含金额，金额另算）。"""
    unit = fields.get("bill_unit") or 60
    talk = fields.get("billsec") or 0
    bill = _bill_seconds(talk, unit)
    sd = _switch_detail_with_source(fields.get("switch_detail"))
    vals = {
        "end_time": fields.get("end_time"),
        "answer_time": fields.get("answer_time"),
        "ring_time": fields.get("ring_time"),
        "hangup_cause": fields.get("hangup_cause"),
        "sip_code": fields.get("sip_code"),
        "talk_duration": talk if talk > 0 else None,
        "bill_duration": bill if talk > 0 else None,
        "bill_unit": fields.get("bill_unit"),
        "caller_out": fields.get("caller_out"),
        "callee_out": fields.get("callee_out"),
        "gateway_id": fields.get("gateway_id"),
        "carrier_id": fields.get("carrier_id"),
        "access_point_id": fields.get("access_point_id"),
        "account_id": fields.get("account_id"),
        "source_ip": fields.get("source_ip"),
        "source_port": fields.get("source_port"),
        "dest_ip": fields.get("dest_ip"),
        "dest_port": fields.get("dest_port"),
        "switch_count": sd.get("switch_count") if sd else None,
        "switch_detail": sd.get("switch_detail") if sd else None,
        "record_path": fields.get("record_path"),
        "record_status": 1 if fields.get("record_path") else None,
    }
    return vals


def _switch_detail_with_source(raw):
    """把 dialplan 的 `cdr_switch_detail` 扁平串解析为 JSON 数组 + 追加来源标记。

    串格式：`;gid:num[:conc_gw[:conc_limit]]:cause;...`（见 `_parse_switch_detail`）。
    **本模块不 import esl_client**，故在此独立实现同一解析（保持口径一致）。

    追加 `{"cdr_source": "xml_cdr"}` 作为**非腿元素**用于溯源；前端已按
    「无 gateway_id 的元素跳过渲染」处理（`fmtSwitchDetail`），不会污染腿列表。
    """
    marker = {"cdr_source": XML_SOURCE}
    if raw is None or str(raw).strip() == "":
        return {"switch_detail": [marker], "switch_count": 0}
    s = str(raw).strip().lstrip(";")
    if not s:
        return {"switch_detail": [marker], "switch_count": 0}
    legs = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        seg = part.split(":")
        item = {
            "gateway_id": _safe_int(seg[0]),
            "cause": seg[-1] if len(seg) > 1 else None,
        }
        mid = seg[1:-1] if len(seg) > 2 else []
        if len(mid) > 0:
            item["callee_out"] = mid[0]
        if len(mid) > 1:
            item["conc_gw"] = _safe_int(mid[1])
        if len(mid) > 2:
            item["conc_limit"] = _safe_int(mid[2])
        legs.append(item)
    return {"switch_detail": legs + [marker], "switch_count": max(len(legs) - 1, 0)}


def build_patch(fields, existing, mode=DEFAULT_MODE, billing_fn=None, node_uuid=None,
                full_cols=None):
    """**【核心】** 决定补什么、是否重算金额，产出「全列宽」vals 供 uuid 幂等 upsert。

    :param fields:     `parse_xml_cdr` 的输出
    :param existing:   现有 CDR 行的全列 dict（含 id 亦可，内部丢弃）；None = 无该行
    :param mode:       `reconcile_only`（默认，只兜底不覆盖）/ `authoritative`
    :param billing_fn: 计费函数，签名同 `esl_client._compute_billing`：
                       `f(rec, talk, bill) -> (cost, rate_used, account_id,
                       business_id, customer_id, cost_price, cost_rate_used,
                       cost_bill_unit)`；**纯函数单测时可传 None**（则不算金额）
    :param node_uuid:  本节点 UUID（写 `fs_node_uuid`）；None 则沿用 existing
    :param full_cols:  Cdr 表全列名。给了就把 vals **补齐为该列集**（缺的置 None）。
                       **必须给**（`apply_xml_cdr` 会强制校验）：MySQL 8 的
                       `INSERT ... AS new ON DUPLICATE KEY UPDATE` **要求 `new.<col>`
                       引用的列出现在 INSERT 列清单里**，否则报
                       `1054 Unknown column 'new.xxx'`。`_upsert_cdr_dict` 的
                       `upd` 正是全列引用，故 `ignore_existing=False` 时 vals 必须全列宽。

    :return: `{"action", "vals", "preserve_cols", "reason", "fields"}`
             action ∈ `insert`（新建行）/ `recompute`（重算金额）/ `fill`（仅补缺失）
    """
    if not fields.get("uuid"):
        raise ValueError("fields 缺少 uuid")

    if existing is None:
        patch = _patch_insert(fields, billing_fn, node_uuid)
    else:
        patch = _patch_existing(fields, existing, mode, billing_fn, node_uuid)

    if full_cols and patch["action"] != "skip":
        cols = [c for c in full_cols if c != "id"]
        patch["vals"] = {c: patch["vals"].get(c) for c in cols}
        patch["widened"] = True
    return patch


def _patch_insert(fields, billing_fn, node_uuid):
    """库里没有该 uuid → 依 XML 新建一行（业务维度全取自 XML）。"""
    if not fields.get("has_cdr_identity", True):
        # 无任何 cdr_* 业务身份 → 不是本系统经手/关注的呼叫（FS 对每个通道都会产出
        # XML CDR，含内线测试呼叫等）。为免污染话单，**不新建行**，仅记录跳过。
        return {
            "action": "skip",
            "vals": {},
            "preserve_cols": (),
            "reason": "no_cdr_identity",
            "fields": fields,
        }
    unit = fields.get("bill_unit") or 60
    talk = fields.get("billsec") or 0
    bill = _bill_seconds(talk, unit)

    vals = {
        "uuid": fields["uuid"],
        "caller_in": fields.get("caller_in") or "",
        "callee_in": fields.get("callee_in") or "",
        "start_time": fields.get("start_time") or fields.get("end_time") or _now(),
        "ring_time": fields.get("ring_time"),
        "answer_time": fields.get("answer_time"),
        "end_time": fields.get("end_time"),
        "talk_duration": talk if talk > 0 else None,
        "bill_unit": unit,
        "bill_duration": bill,
        "hangup_cause": fields.get("hangup_cause"),
        "sip_code": fields.get("sip_code"),
        "caller_mid": fields.get("caller_mid"),
        "callee_mid": fields.get("callee_mid"),
        "caller_out": fields.get("caller_out"),
        "callee_out": fields.get("callee_out"),
        "caller_type": fields.get("caller_type") or "",
        "gateway_id": fields.get("gateway_id"),
        "carrier_id": fields.get("carrier_id"),
        "access_point_id": fields.get("access_point_id"),
        "account_id": fields.get("account_id"),
        "source_ip": fields.get("source_ip"),
        "source_port": fields.get("source_port"),
        "dest_ip": fields.get("dest_ip"),
        "dest_port": fields.get("dest_port"),
        "record_path": fields.get("record_path"),
        "record_status": 1 if fields.get("record_path") else 0,
        "billed": 0,
        "created_at": _now(),
        "fs_node_uuid": node_uuid,
    }
    sd = _switch_detail_with_source(fields.get("switch_detail"))
    vals["switch_detail"] = sd["switch_detail"]
    vals["switch_count"] = sd["switch_count"]

    reason = "no_row"
    if talk > 0 and _has_attribution(fields):
        reason = "no_row+billing"
        _apply_billing(vals, fields, talk, bill, billing_fn)
    elif talk > 0:
        # 有通话时长但无任何业务归属（如手工 originate 造数）→ 不重算，
        # 避免把费用记到错误主体（设计稿 §13.3 #1「缺变量 → 不计费降级」）。
        reason = "no_row+no_attribution"
    vals.setdefault("cost", 0)
    vals.setdefault("cost_price", 0)
    vals.setdefault("profit", 0)

    return {
        "action": "insert",
        "vals": vals,
        # insert 分支不需要 preserve（库里本来就没有旧值）
        "preserve_cols": (),
        "reason": reason,
        "fields": fields,
    }


def _patch_existing(fields, existing, mode, billing_fn, node_uuid=None):
    """库里已有该 uuid → 只补缺失 / 按需重算金额；**默认绝不覆盖已有非空值**。"""
    vals = {k: v for k, v in existing.items() if k != "id"}
    vals["uuid"] = fields["uuid"]

    filled = []
    for col, v in _xml_fill_values(fields).items():
        if v is None:
            continue
        if _is_empty(vals.get(col)):
            vals[col] = v
            filled.append(col)

    # 节点归属：XML 路径直接写本节点 NODE_UUID（§13.4 ③），不从 XML 反查。
    # 意义：reconcile 回填的骨架行 fs_node_uuid 为 NULL（是 cdr_health 的
    # 「可疑行」指纹）；被 XML 补齐终态/金额后应带上节点，否则会被误判为未修复。
    if node_uuid and _is_empty(vals.get("fs_node_uuid")):
        vals["fs_node_uuid"] = node_uuid
        filled.append("fs_node_uuid")

    # authoritative 模式：允许 XML 覆盖原因/时长（默认不启用，留待评审后决定）
    if mode == MODE_AUTHORITATIVE:
        for col, v in _xml_fill_values(fields).items():
            if col in AUTHORITATIVE_COLS and v is not None:
                vals[col] = v

    unit = vals.get("bill_unit") or fields.get("bill_unit") or 60
    talk = fields.get("billsec") or 0
    bill = _bill_seconds(talk, unit)

    has_cost = bool(existing.get("cost")) and float(existing.get("cost") or 0) > 0
    if has_cost:
        # ESL 路径已算出有效金额 → 只补缺失，不重算（零变化）
        return {
            "action": "fill",
            "vals": vals,
            "preserve_cols": ("created_at",),
            "reason": "cost_present" + ("+filled:%s" % ",".join(filled) if filled else ""),
            "fields": fields,
        }

    if talk > 0 and _has_attribution(fields):
        _apply_billing(vals, fields, talk, bill, billing_fn)
        vals["talk_duration"] = talk
        vals["bill_duration"] = bill
        return {
            "action": "recompute",
            "vals": vals,
            "preserve_cols": ("created_at",),
            "reason": "cost_missing+recompute" + ("+filled:%s" % ",".join(filled) if filled else ""),
            "fields": fields,
        }

    # 未接通（billsec=0）本就不计费；或归属不明无法安全计费 → 仅补缺失
    return {
        "action": "fill",
        "vals": vals,
        "preserve_cols": ("created_at",),
        "reason": ("no_attribution" if talk > 0 else "not_answered")
                  + ("+filled:%s" % ",".join(filled) if filled else ""),
        "fields": fields,
    }


def _apply_billing(vals, fields, talk, bill, billing_fn):
    """调注入的计费函数，把金额组列写进 vals（含账户维度回落）。"""
    if billing_fn is None:
        return False
    rec = {
        "access_point_id": fields.get("access_point_id"),
        "caller_mid": fields.get("caller_mid"),
        "callee_mid": fields.get("callee_mid"),
        "gateway_id": fields.get("gateway_id"),
        "carrier_id": fields.get("carrier_id"),
        "bill_unit": vals.get("bill_unit") or fields.get("bill_unit") or 60,
        "answer_time": fields.get("answer_time"),
        "account_id": fields.get("account_id"),
    }
    try:
        # pylint: disable=too-many-function-args
        cost, rate_used, account_id, business_id, customer_id, cost_price, cost_rate_used, cost_bill_unit = \
            billing_fn(rec, talk, bill)
    except Exception as e:      # noqa: BLE001 —— 计费失败不该丢整通 CDR
        print("[cdr-xml] billing failed (uuid=%s): %s" % (fields.get("uuid"), e), flush=True)
        return False

    # 与 `_save_cdr` 同口径：dialplan 显式下发的 cdr_account_id 优先于推导值
    if fields.get("account_id") is not None:
        account_id = fields.get("account_id")
    vals.update({
        "cost": cost,
        "rate_used": rate_used,
        "account_id": account_id,
        "business_id": business_id,
        "customer_id": customer_id,
        "cost_price": cost_price,
        "cost_rate_used": cost_rate_used,
        "cost_bill_unit": cost_bill_unit,
        "profit": (cost or 0) - (cost_price or 0),
    })
    return True


# ---------------------------------------------------------------------------
# 3) 落库编排（DB 能力全部注入 —— 本模块不 import esl_client）
# ---------------------------------------------------------------------------

def apply_xml_cdr(xml_text, *, row_loader, upsert_fn, enqueue_fn, full_cols=None,
                  mode=DEFAULT_MODE, billing_fn=None, node_uuid=None,
                  preserve_supported=True):
    """解析 XML → 交给 cdr-writer 单线程队列 → 读现有行 → build_patch → upsert。

    写入**必须**经 `enqueue_fn`（= `esl_client._enqueue_cdr_job`）进 cdr-writer
    队列，与 ESL 路径**串行化**，把并发写窗口整个消掉（设计稿 §13.2）。

    :param row_loader: `f(db, uuid) -> dict|None` 读该 uuid 的现有行（全列）
    :param upsert_fn:  `f(vals, db=db, preserve_cols=...)` = `esl_client._upsert_cdr_dict`
    :param enqueue_fn: `f(uuid, job)` = `esl_client._enqueue_cdr_job`
    :param full_cols:  **必给** —— Cdr 表全列名（vals 要补齐为全列宽，
                       否则 MySQL 报 1054，见 `build_patch` 说明）
    :return: `parse_xml_cdr` 的字段（供端点回显/日志）
    """
    if not full_cols:
        raise ValueError("apply_xml_cdr 必须传 full_cols（MySQL upsert 要求全列宽）")
    fields = parse_xml_cdr(xml_text)

    def _job(db):
        existing = row_loader(db, fields["uuid"])
        patch = build_patch(fields, existing, mode=mode, billing_fn=billing_fn,
                            node_uuid=node_uuid, full_cols=full_cols)
        if patch["action"] == "skip":
            print("[cdr-xml] skip uuid=%s reason=%s" % (fields["uuid"], patch.get("reason")),
                  flush=True)
            return False
        kwargs = {"db": db}
        if preserve_supported:
            kwargs["preserve_cols"] = patch.get("preserve_cols") or ()
        ok = upsert_fn(patch["vals"], **kwargs)
        print("[cdr-xml] %s uuid=%s billsec=%s cost=%s reason=%s ok=%s"
              % (patch["action"], fields["uuid"], fields.get("billsec"),
                 patch["vals"].get("cost"), patch.get("reason"), ok), flush=True)
        return ok

    enqueue_fn(fields["uuid"], _job)
    return fields


# ---------------------------------------------------------------------------
# 4) 端点装配辅助（供 api/app.py 使用；本模块保持「不 import esl_client/db」）
# ---------------------------------------------------------------------------

def extract_cdr_xml(body, content_type=""):
    """从 `/fs/cdr` 请求体取出 XML 文本（纯函数）。

    mod_xml_cdr 以 `application/x-www-form-urlencoded` 提交，XML 放在某个字段里；
    **字段名随 FS 版本/配置而异**（设计稿 §5.4 列为待实测项），故这里三者都兼容：
      ① 表单字段 `cdr`（预期）　② 表单字段 `xml` / `cdr_xml` / `data`
      ③ 整个 body 就是 XML（`<` 开头，未编码）
    取不到时返回空串（端点回 400）。
    """
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body).decode("utf-8", "replace")
    else:
        raw = body or ""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("<"):
        return raw
    ctype = (content_type or "").lower()
    if "form-urlencoded" in ctype or "=" in raw:
        try:
            q = urllib.parse.parse_qs(raw, keep_blank_values=True)
        except Exception:       # noqa: BLE001
            q = {}
        for key in ("cdr", "xml", "cdr_xml", "data"):
            vals = q.get(key)
            if vals and vals[0].strip():
                return vals[0].strip()
        # 兜底：任何看起来像 XML 的值
        for vals in q.values():
            for v in vals:
                if v.strip().startswith("<"):
                    return v.strip()
    return ""


def make_esl_wiring():
    """装配 `/fs/cdr` 端点所需的依赖（**延迟 import**，保持本模块可独立单测）。

    :return: dict（可直接 `**` 展开进 `apply_xml_cdr`）
             row_loader / upsert_fn / enqueue_fn / billing_fn / full_cols / node_uuid
    """
    from sqlalchemy import select

    from esl_client import (_upsert_cdr_dict, _enqueue_cdr_job, _compute_billing)
    from db.models import Cdr
    from core.config import NODE_UUID

    full_cols = [c.name for c in Cdr.__table__.columns]
    all_cols = list(Cdr.__table__.columns)

    def _row_loader(db, call_uuid):
        """读该 uuid 的现有 CDR 行（全列 dict）；无则 None。

        注意：本函数**必须**在 cdr-writer 线程内被调用（apply_xml_cdr 的 job 里），
        与 ESL 路径的写串行化，才没有读-改-写窗口（设计稿 §13.2）。
        """
        row = db.scalar(select(Cdr).where(Cdr.uuid == call_uuid))
        if row is None:
            return None
        return {c.name: getattr(row, c.name) for c in all_cols}

    return {
        "row_loader": _row_loader,
        "upsert_fn": _upsert_cdr_dict,
        "enqueue_fn": _enqueue_cdr_job,
        "billing_fn": _compute_billing,
        "full_cols": full_cols,
        "node_uuid": NODE_UUID,
    }
