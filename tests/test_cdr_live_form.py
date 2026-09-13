# -*- coding: utf-8 -*-
"""`/fs/cdr` live POST 形态回归（PITFALLS #75）。

zcode 排查定论（设计稿 §14.6）：FS mod_xml_cdr 在 `encode=false`（ENCODING_NONE，
本设计选定值）时发出的 live 请求是——

    body  = "cdr=" + 原始 XML（**零 URL 编码**，switch_mprintf("cdr=%s") 裸拼）
    CT    = application/x-www-form-plaintext

旧实现把 `cdr=` 开头的 body 一律交给 parse_qs，XML 里的裸 `&` 与 `=` 会把值在第一个
`&` 处切碎 → XML 解析失败 → 端点 400（zdev 真呼叫 11/11 复现，DB 无 CDR、全退回落盘）。

本文件用真机 fixture **注入含 `&`/`=` 的内容**（SIP 头/URL 类字段天然带这些字符）
后按 live 形态构造 body，锁死「三种 FS 形态 + 裸 XML 全部可解析且 uuid 一致」。
"""
import os
import urllib.parse

from cdr_truth import extract_cdr_xml, parse_xml_cdr

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "xml_cdr_a_leg.xml")
CT_PLAINTEXT = "application/x-www-form-plaintext"
CT_URLENCODED = "application/x-www-form-urlencoded"


def _xml():
    """真机 fixture + 注入一段含 `&`/`=` 的内容（触发旧 parse_qs 截断的必要条件）。"""
    with open(FIXTURE, encoding="utf-8") as f:
        raw = f.read().strip()
    injected = raw.replace(
        "</cdr>",
        "  <app_log><application app_name=\"bridge\" app_data=\""
        "sofia/gw/x@1.2.3.4?a=1&amp;b=2\"/></app_log>\n</cdr>",
    )
    assert "&amp;" in injected and "=" in injected   # 前提守卫：载荷必须自带雷
    return injected


def _uuid_of(xml_text):
    return parse_xml_cdr(xml_text).get("uuid")


def test_live_plaintext_form_cdr_prefix():
    """① ENCODING_NONE：cdr= + 原始 XML + x-www-form-plaintext（400 根因形态）。"""
    xml = _xml()
    body = ("cdr=" + xml).encode("utf-8")
    got = extract_cdr_xml(body, CT_PLAINTEXT)
    assert got == xml                      # 旧实现在此被 &/= 截断 → 长度不等
    assert _uuid_of(got)


def test_urlencoded_form_cdr_prefix():
    """② ENCODING_DEFAULT：cdr= + 整体 url_encode(XML)。"""
    xml = _xml()
    body = ("cdr=" + urllib.parse.quote(xml, safe="")).encode("utf-8")
    got = extract_cdr_xml(body, CT_URLENCODED)
    assert got == xml
    assert _uuid_of(got)


def test_bare_xml_unchanged():
    """③ 裸 XML（text/xml / 重放路径）行为不变。"""
    xml = _xml()
    assert extract_cdr_xml(xml.encode("utf-8"), "text/xml") == xml
    assert extract_cdr_xml(xml.encode("utf-8"), CT_PLAINTEXT) == xml


def test_three_forms_share_one_uuid():
    """三形态解析出的 uuid 必须一致——否则说明某条分支取值被截断。"""
    xml = _xml()
    uuids = {
        _uuid_of(extract_cdr_xml(("cdr=" + xml).encode(), CT_PLAINTEXT)),
        _uuid_of(extract_cdr_xml(("cdr=" + urllib.parse.quote(xml, safe="")).encode(),
                                 CT_URLENCODED)),
        _uuid_of(extract_cdr_xml(xml.encode(), "text/xml")),
    }
    assert len(uuids) == 1, uuids
    assert uuids.pop()
