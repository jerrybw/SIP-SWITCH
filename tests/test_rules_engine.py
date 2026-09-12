"""规则引擎与号码变换测试（rules/matcher.py + rules/service.py 纯逻辑部分）。

覆盖此前零测试的核心裁决语义（CONTRIBUTING「新增关键路径请补单元测试」）：
- translate_pattern：``*``/``?`` 翻译 + 全串锚定 + 正则元字符转义。
- evaluate_direction：deny 优先 / allow 白名单 / 仅 deny 无命中放行 / 无规则放行
  （标准 ACL 语义矩阵）；主被叫分方向独立裁决。
- 变换规则：前缀锚定（不再子串命中）、``*`` 捕获引用、空 replace_to 删前缀、
  最长 pattern 优先且并列取最小 rule.id（确定性）。

写法：matcher 的 _rule_* 取值层显式兼容 dict / 任意对象（作者预留的测试钩子），
故纯函数测试无需 DB；DB 集成路径已由 CI 的 migrate/真库用例兜底。
"""
from types import SimpleNamespace

from rules.matcher import (
    DIR_CALLER, DIR_CALLEE, ACTION_ALLOW, ACTION_DENY,
    translate_pattern, match_number, evaluate_direction, evaluate,
)
from rules.service import (
    _translate_pattern_to_regex, _apply_one, _translate_direction,
)


# ---------------- 占位符翻译与单条匹配 ----------------

def test_translate_pattern_wildcards_and_anchor():
    assert translate_pattern("138*") == "^138.*$"
    assert translate_pattern("138?") == "^138.$"
    assert translate_pattern("1.0*") == r"^1\.0.*$"      # 正则元字符转义
    assert translate_pattern("") == "^$"


def test_match_number_semantics():
    assert match_number("138*", "13800001234") is True
    assert match_number("138*", "138") is True            # * 含空串
    assert match_number("138?", "1381") is True
    assert match_number("138?", "13812") is False          # ? 恰一位
    assert match_number("*", "anything") is True
    assert match_number("13?", "139") is True
    assert match_number("13?", "14") is False
    assert match_number(None, "123") is False
    assert match_number("13*", None) is False


def test_match_number_full_string_anchor():
    # 全串锚定：模式 138 不该命中 9138x（防子串命中）
    assert match_number("138", "138") is True
    assert match_number("138", "91381") is False


# ---------------- ACL 裁决矩阵 ----------------

def _r(pattern, act, direction=DIR_CALLEE, id_=1):
    return SimpleNamespace(pattern=pattern, act=act, direction=direction, id=id_)


def test_acl_empty_rules_allow_all():
    assert evaluate_direction([], "whatever") == (True, None)


def test_acl_deny_beats_allow():
    rules = [_r("138*", ACTION_ALLOW), _r("1380*", ACTION_DENY)]
    ok, failed = evaluate_direction(rules, "1380123")
    assert ok is False and failed.pattern == "1380*"


def test_acl_allow_whitelist_requires_hit():
    rules = [_r("138*", ACTION_ALLOW)]
    assert evaluate_direction(rules, "13811112222") == (True, None)
    ok, failed = evaluate_direction(rules, "15900001111")
    assert ok is False and failed.pattern == "138*"


def test_acl_deny_only_no_hit_passes():
    rules = [_r("400*", ACTION_DENY)]
    assert evaluate_direction(rules, "13800001234") == (True, None)


def test_evaluate_both_directions_independent():
    caller_allow = [_r("8000*", ACTION_ALLOW, direction=DIR_CALLER)]
    callee_deny = [_r("9*", ACTION_DENY)]
    # 被叫命中 deny → 拦截并标记 DIR_CALLEE
    ok, d, rule = evaluate(caller_allow, callee_deny, "80001234", "9001")
    assert ok is False and d == DIR_CALLEE and rule.pattern == "9*"
    # 主叫未命中白名单 → 拦截并标记 DIR_CALLER
    ok, d, rule = evaluate(caller_allow, [], "90001234", "10086")
    assert ok is False and d == DIR_CALLER
    # 双向都过
    assert evaluate(caller_allow, [], "80001234", "10086") == (True, None, None)


# ---------------- 号码变换（translate，act=3） ----------------

def _t(pattern, replace_to, id_=1):
    return SimpleNamespace(pattern=pattern, replace_to=replace_to, id=id_,
                           act=3, direction=2)


def test_translate_prefix_anchored():
    # 2026-09-10 修复语义：^ 前缀锚定 + 大小写敏感 —— 只有号首逐位命中才变换
    rx = _translate_pattern_to_regex("C?1")
    assert rx.search("C11aa") is not None           # 号首 C-1-1 命中
    assert rx.search("ccc1") is None                # 号首 c≠C 且无 "1" 在第三位
    assert rx.search("cc143") is None               # 号首 cc1 ≠ C?1（c≠C）
    assert rx.search("Cb12334") is not None         # 号首 C-b-1 命中


def test_translate_capture_and_delete_prefix():
    # replace_to 的 * 引用 pattern 中 * 的捕获组（模式 123* 命中 1234567，捕获 = "4567"）
    assert _apply_one("1234567", _t("123*", "99*")) == "994567"  # 捕获组拼到 99 后
    assert _apply_one("1234567", _t("123*", "*")) == "4567"     # 原样引用 = 删前缀
    assert _apply_one("1234567", _t("123", "")) == "4567"        # 空 replace_to = 删前缀
    assert _apply_one("999", _t("123", "0*")) == "999"           # 号首不命中 → 原号


def test_translate_star_in_replace_without_capture_is_literal():
    """回归（2026-09-12 修复）：pattern 无 * 时 replace_to 的 * 曾触发 re.error 崩溃。

    管理端可录入此类规则；此前会让 /fs/dialplan 500、呼叫挂断。现降级为字面 *。
    """
    # pattern="123"（无捕获组）+ replace_to="99*"：不再抛 re.error，按字面处理
    out = _apply_one("1234567", _t("123", "99*"))
    assert out == "99*4567"


def test_translate_longest_pattern_wins():
    # 两条都命中 80012345：取 pattern 更长者（80012*）；* 引用保住后缀 345
    rules = [_t("800*", "A*", id_=1), _t("80012*", "B*", id_=2)]
    assert _translate_direction("80012345", rules) == "B345"


def test_translate_tie_breaks_by_smallest_id():
    # pattern 等长并列：取 rule.id 最小（稳定、可预期，不依赖规则入库顺序）
    rules = [_t("800*", "A*", id_=9), _t("800*", "B*", id_=2)]
    assert _translate_direction("8001", rules) == "B1"
