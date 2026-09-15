# -*- coding: utf-8 -*-
"""需求①：系统配置 schema + 写白名单单测（纯函数面，无 DB / 无 Redis）。

覆盖 api/sys_config.py：
- 热/冷的分界线**以代码读法为准**（get_setting=热；settings.get=冷）；
  `prepaid_enabled` / `concurrent_limit_global` 必须落在 **cold** ——
  这是 2026-09-15 实测纠偏的核心：它们只经 settings.get() 读 yaml，
  若被误标成 hot，用户改完以为生效、实际纹丝不动（PITFALLS #53「死配置」同型）。
- 凭据键**绝不出值**（整体不出现在 cold 里），mysql.url 只脱敏出 host/db。
- 写白名单：越界整请求拒绝；bool 必须归一成 "1"/"0"（写 "True" 会被
  int(float()) 静默解析失败 → 改了等于没改）；null = 不修改（沿用改造前语义）。
"""
import api.sys_config as SC


CFG = {
    "esl": {"host": "freeswitch", "port": 8021, "password": "s3cr3t",
            "reconnect_interval": 5, "fs_node_uuid": ""},
    "api": {"host": "0.0.0.0", "port": 8000},
    "mysql": {"url": "mysql+pymysql://root:pa55@mysql:3306/sip_switch", "pool_recycle": 3600},
    "default_sip_domain": "sip.example.com",
    "prepaid_enabled": True,
    "auth": {"admin_user": "admin", "password_salt": "deadbeef",
             "admin_password_hash": "cafe", "jwt_secret": "jwt-secret",
             "token_expire_minutes": 120},
    "node": {"uuid": "2a5f89f1b0f0ce74"},
    "record": {"dir": "/recordings", "local_root": "/recordings", "backend": "local"},
    "redis": {"enabled": True, "host": "redis", "port": 6379, "password": "rpw", "db": 0},
    "concurrency": {"backend": "redis", "fail_open": False, "lease_ttl": 86400},
    "concurrent_limit_global": 0,
    "xml_curl": {"user": "xmlcurl", "password": "xcpw"},
}


# ---------------------------------------------------------------------------
# 清单结构
# ---------------------------------------------------------------------------
def test_schema_three_groups_present():
    s = SC.build_schema(CFG, {})
    assert set(s.keys()) == {"hot", "cold", "masked_keys"}
    assert s["hot"] and s["cold"] and s["masked_keys"]


def test_empty_schema_shape_is_contract():
    """契约形状：hot 每项 key/label/type/value/hint/effect；cold 每项 key/label/type/value/hint。"""
    s = SC.build_schema(CFG, {})
    for it in s["hot"]:
        assert set(it.keys()) >= {"key", "label", "type", "value", "hint", "effect"}
    for it in s["cold"]:
        assert set(it.keys()) == {"key", "label", "type", "value", "hint"}


def test_hot_keys_are_exactly_the_runtime_read_ones():
    """热清单 = 全仓 get_setting/get_int_setting 实际读的键（含前端在用的那 5 个）。"""
    assert SC.HOT_KEYS == {
        "phone_sync_interval", "ap_sync_interval", "provision_sync_interval",
        "node_health_interval", "node_health_fail_threshold",
        "node_health_stale_threshold", "node_max_concurrency",
        "cdr_xml_enabled", "cdr_xml_mode",
        "webhook_gateway_heartbeat_url", "webhook_node_heartbeat_url",
    }


def test_every_frontend_writable_key_is_allowed():
    """回归锚点：现有 admin.js 的 PUT 键必须仍可写，否则改造会打断页面。

    实测的 3 个写入点：topbar(ap_sync_interval / phone_sync_interval)、
    provision_sync_interval、saveWebhook(webhook_gateway_heartbeat_url /
    webhook_node_heartbeat_url)。
    """
    for k in ("ap_sync_interval", "phone_sync_interval", "provision_sync_interval",
              "webhook_gateway_heartbeat_url", "webhook_node_heartbeat_url"):
        assert k in SC.HOT_KEYS, k


def test_yaml_only_keys_are_cold_not_hot():
    """只经 settings.get() 读 yaml 的键必须是冷配置（实测定性，防再次搞混）。"""
    cold_keys = {it["key"] for it in SC.build_cold(CFG)}
    for k in ("prepaid_enabled", "concurrent_limit_global", "default_sip_domain"):
        assert k in cold_keys, k
        assert k not in SC.HOT_KEYS, k


# ---------------------------------------------------------------------------
# 凭据不外泄
# ---------------------------------------------------------------------------
def test_masked_keys_absent_from_cold_entirely():
    cold_keys = {it["key"] for it in SC.build_cold(CFG)}
    for k in SC.MASKED_KEYS:
        assert k not in cold_keys, k


def test_no_secret_value_leaks_anywhere_in_schema():
    """整包序列化后不得出现任何凭据明文（最硬的一条）。"""
    import json
    text = json.dumps(SC.build_schema(CFG, {"webhook_node_heartbeat_url": "https://x/y"}),
                      ensure_ascii=False)
    for secret in ("s3cr3t", "rpw", "xcpw", "jwt-secret", "cafe", "deadbeef", "pa55"):
        assert secret not in text, secret


def test_mysql_url_password_stripped_but_host_kept():
    cold = {it["key"]: it["value"] for it in SC.build_cold(CFG)}
    assert cold["mysql.url"] == "mysql+pymysql://root:***@mysql:3306/sip_switch"


def test_mask_url_password_variants():
    assert SC.mask_url_password("mysql+pymysql://u:p@h:3306/d") == "mysql+pymysql://u:***@h:3306/d"
    assert SC.mask_url_password("redis://:only@h") == "redis://:***@h"
    assert SC.mask_url_password("noscheme:pw@h") == "***@h"
    assert SC.mask_url_password("no-at-sign") == "no-at-sign"
    assert SC.mask_url_password(None) == ""


def test_internal_keys_are_not_configurable():
    assert SC.is_internal_key("provision_seq")
    assert SC.is_internal_key("provision_pending")
    assert SC.is_internal_key("provision_seen_2a5f89f1b0f0ce74")
    assert not SC.is_internal_key("provision_sync_interval")  # 这个是真的可配置
    assert "provision_seq" not in SC.HOT_KEYS


# ---------------------------------------------------------------------------
# hot 值归一
# ---------------------------------------------------------------------------
def test_hot_bool_coerced_to_real_bool():
    hot = {it["key"]: it for it in SC.build_hot({"cdr_xml_enabled": "1"})}
    assert hot["cdr_xml_enabled"]["value"] is True
    hot = {it["key"]: it for it in SC.build_hot({"cdr_xml_enabled": "0"})}
    assert hot["cdr_xml_enabled"]["value"] is False


def test_hot_int_falls_back_to_default_on_garbage():
    hot = {it["key"]: it for it in SC.build_hot({"node_health_interval": "abc"})}
    assert hot["node_health_interval"]["value"] == 30


def test_hot_int_unit_and_bounds_exposed():
    hot = {it["key"]: it for it in SC.build_hot({})}
    assert hot["node_health_interval"]["unit"] == "秒"
    assert hot["node_health_interval"]["min"] == 1
    assert hot["phone_sync_interval"]["min"] == 5


# ---------------------------------------------------------------------------
# 写白名单 / 归一
# ---------------------------------------------------------------------------
def test_write_accepts_whitelisted_keys():
    ok, errs = SC.normalize_write({"node_health_interval": 15, "cdr_xml_enabled": True})
    assert errs == []
    assert ok == {"node_health_interval": "15", "cdr_xml_enabled": "1"}


def test_write_bool_never_stores_python_True():
    """写 "True" 会被 get_int_setting 的 int(float(v)) 解析失败 → 静默回落默认值。"""
    ok, errs = SC.normalize_write({"cdr_xml_enabled": "True"})
    assert errs == []
    assert ok["cdr_xml_enabled"] == "1"
    ok, errs = SC.normalize_write({"cdr_xml_enabled": "false"})
    assert ok["cdr_xml_enabled"] == "0"


def test_write_rejects_cold_key():
    """冷配置必须被拒 —— 否则用户以为改完生效了（死配置）。"""
    ok, errs = SC.normalize_write({"prepaid_enabled": False,
                                   "concurrent_limit_global": 100})
    assert ok == {}
    assert {k for k, _ in errs} == {"prepaid_enabled", "concurrent_limit_global"}


def test_write_rejects_internal_key_with_distinct_reason():
    ok, errs = SC.normalize_write({"provision_seq": 99})
    assert ok == {} and len(errs) == 1
    assert errs[0][0] == "provision_seq"
    assert "系统自维护" in errs[0][1]


def test_write_null_means_leave_unchanged():
    """沿用改造前语义：值为 null = 不修改（前端取不到元素时会发 null）。"""
    ok, errs = SC.normalize_write({"node_health_interval": None, "ap_sync_interval": 20})
    assert errs == []
    assert ok == {"ap_sync_interval": "20"}


def test_write_int_bounds_enforced():
    ok, errs = SC.normalize_write({"provision_sync_interval": 0})
    assert ok == {} and errs[0][1].find("不能小于 1") >= 0
    ok, errs = SC.normalize_write({"node_health_interval": "abc"})
    assert ok == {} and errs[0][1] == "必须是整数"


def test_write_webhook_url_shape_checked_but_empty_allowed():
    ok, errs = SC.normalize_write({"webhook_node_heartbeat_url": ""})
    assert errs == [] and ok["webhook_node_heartbeat_url"] == ""
    ok, errs = SC.normalize_write({"webhook_node_heartbeat_url": "qyapi.weixin.qq.com/x"})
    assert ok == {} and "http(s)" in errs[0][1]
    ok, errs = SC.normalize_write({"webhook_node_heartbeat_url": "https://qyapi.weixin.qq.com/x"})
    assert errs == []


def test_write_string_trimmed():
    ok, _ = SC.normalize_write({"cdr_xml_mode": "  override  "})
    assert ok["cdr_xml_mode"] == "override"


def test_flatten_keeps_nesting_and_order():
    flat = SC.flatten({"a": {"b": 1, "c": {"d": 2}}, "e": 3})
    assert flat == [("a.b", 1), ("a.c.d", 2), ("e", 3)]
    assert SC.flatten(None) == []
