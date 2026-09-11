"""拨号计划 XML 生成冒烟测试（纯函数，无需数据库/FreeSWITCH）。

覆盖 T-201/T-202/T-205 的 build_* 生成器：输出须含合法 FS 动作标签，
且整体能被 minidom 解析（防止回归出 NO_ROUTE_DESTINATION 或 XML 解析失败）。
"""
import xml.dom.minidom as minidom

from api.dialplan_xml import (
    build_allow_xml,
    build_deny_xml,
    build_empty_xml,
    build_outbound_xml,
)


def _assert_well_formed(xml: str, label: str):
    try:
        minidom.parseString(xml)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"{label} 不是合法 XML: {e}\n{xml}")


def test_build_allow_xml_contains_bridge_and_record():
    xml = build_allow_xml("1000", access_point_id=2, bill_unit=60, account_id=1)
    _assert_well_formed(xml, "build_allow_xml")
    assert '<action application="bridge"' in xml
    assert "record_session" in xml
    assert "rec_file=" in xml  # #70: 录音落点已从 ${recordings_dir} 改为 record.dir/<node_uuid>/
    assert "${uuid}.wav" in xml
    assert 'name="default"' in xml  # context 必须嵌在 default，否则 FS 找不到路由


def test_build_deny_xml_603_and_503():
    # 限制命中=603(CALL_REJECTED)，并发超限=503(NETWORK_OUT_OF_ORDER)
    for sip_code in ("603", "503"):
        xml = build_deny_xml("denied_by_rule:138*", sip_code)
        _assert_well_formed(xml, f"build_deny_xml({sip_code})")
        assert "<document" in xml
        assert "sip_gateway_reject_reason" in xml  # 拒绝原因通道变量


def test_build_outbound_xml_multi_leg_failover():
    candidates = [
        {"gateway_id": 1, "name": "gw1", "carrier_id": 1, "caller_out": "0",
         "callee_out": "00123456789", "switch_codes": "503", "failover_pre_ring_only": 0},
        {"gateway_id": 2, "name": "gw2", "carrier_id": 1, "caller_out": "0",
         "callee_out": "00123456789", "switch_codes": "408", "failover_pre_ring_only": 0},
    ]
    xml = build_outbound_xml("00123456789", candidates, access_point_id=2)
    _assert_well_formed(xml, "build_outbound_xml")
    assert 'description="sip-gateway-outbound"' in xml
    assert "continue_on_fail" in xml  # T-205 故障切换关键变量


def test_build_empty_xml_no_route():
    xml = build_empty_xml()
    _assert_well_formed(xml, "build_empty_xml")
    # 无出局路由：先写 CDR 关联变量，再以 NO_ROUTE_DESTINATION 挂断（不交回 FS 静态拨号）
    assert 'name="default"' in xml
    assert "gw_no_route" in xml
    assert "NO_ROUTE_DESTINATION" in xml
