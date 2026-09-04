"""主被叫限制规则引擎（T-201 / R-104 / R-106 / R-203 / R-204）。

占位符语义（与 PRD R-104 一致）：
- ``*`` 代表任意长度字符串（含空）
- ``?`` 代表一位任意字符

规则设计（用户确认，2026-08-30）：
- 主叫规则（direction=1）与被叫规则（direction=2）**分开**配置，互不影响。
- 一通呼叫**主被叫都要满足**才放行。
- 每条规则可设 action：allow=允许（白名单，匹配则放行）/ deny=禁止（黑名单，匹配则拦截）。
- 某方向**未配置任何规则**时，等价于 allow *（允许任意），即默认放行。

裁决优先级（标准 ACL 语义）：deny 优先于 allow。
  - 命中任意 deny 规则 → 拦截；
  - 仅存在 allow 规则时按白名单判定（未命中任一 allow → 拦截）；
  - 仅 deny 规则且无命中 → 放行；
  - 无规则 → 放行。

架构铁律：规则决策在业务网关层完成，由 mod_xml_curl 下发给 FS；
禁止把规则硬编码进 FreeSWITCH dialplan。
"""
import re
from typing import Iterable, Optional, List

# 方向编码（与 db.models.Rule.direction 对齐）
DIR_CALLER = 1
DIR_CALLEE = 2

# 动作编码（与 db.models.Rule.action 对齐）
ACTION_ALLOW = 1
ACTION_DENY = 2


def translate_pattern(pattern: str) -> str:
    """将 ``*`` / ``?`` 占位符规则翻译为正则，并做全串锚定。

    除 ``*`` 和 ``?`` 外的字符按正则原义转义，避免 ``.`` ``(`` 等业务符号被误当作正则元字符。
    例如 ``138*`` -> ``^138.*$``；``1.0*`` -> ``^1\\.0.*$``。
    """
    if not pattern:
        return "^$"
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "^" + "".join(out) + "$"


def match_number(pattern: str, number: str) -> bool:
    """单条规则对单个号码是否匹配（全串精确匹配）。"""
    if pattern is None or number is None:
        return False
    try:
        return re.fullmatch(translate_pattern(pattern), number, flags=re.DOTALL) is not None
    except re.error:
        return False


def _rule_action(rule) -> Optional[int]:
    """取规则的动作字段（兼容 ORM 对象 / dict）。

    ORM 列名 ``act``（避开 MySQL 保留字 action）；dict  fixture 用 ``action`` 也兼容。
    """
    if isinstance(rule, dict):
        v = rule.get("act")
        if v is None:
            v = rule.get("action")
        return v
    return getattr(rule, "act", None)


def _rule_pattern(rule) -> Optional[str]:
    if isinstance(rule, dict):
        return rule.get("pattern")
    return getattr(rule, "pattern", None)


def _rule_direction(rule) -> Optional[int]:
    """取规则的方向字段（兼容 ORM 对象 / dict）。"""
    if isinstance(rule, dict):
        return rule.get("direction")
    return getattr(rule, "direction", None)


def evaluate_direction(
    rules: Iterable,
    number: str,
) -> tuple[bool, Optional[object]]:
    """对单一方向（主叫或被叫）的一组规则裁决是否放行。

    规则为空 → (True, None)（默认 allow *）。
    否则按 deny 优先的 ACL 语义裁决。返回 (allowed, failed_rule)。
    """
    rules = list(rules)
    if not rules:
        return True, None
    # 1) deny 优先：命中任意 deny 规则即拦截
    for r in rules:
        if _rule_action(r) == ACTION_DENY and match_number(_rule_pattern(r), number):
            return False, r
    # 2) allow 白名单：存在 allow 规则时，未命中任一即拦截
    allow_rules = [r for r in rules if _rule_action(r) == ACTION_ALLOW]
    if allow_rules:
        for r in allow_rules:
            if match_number(_rule_pattern(r), number):
                return True, None
        return False, allow_rules[0]
    # 3) 仅 deny 规则且无命中 → 放行
    return True, None


def evaluate(
    caller_rules: Iterable,
    callee_rules: Iterable,
    caller: str,
    callee: str,
) -> tuple[bool, Optional[int], Optional[object]]:
    """主被叫分别裁决；两者都放行才整体放行。

    返回 (allowed, failed_direction, failed_rule)：
    - allowed=True 表示通过；
    - 否则 failed_direction 标记被拦截方向（DIR_CALLER / DIR_CALLEE），failed_rule 为命中规则。
    """
    ok_c, fail_c = evaluate_direction(caller_rules, caller)
    if not ok_c:
        return False, DIR_CALLER, fail_c
    ok_e, fail_e = evaluate_direction(callee_rules, callee)
    if not ok_e:
        return False, DIR_CALLEE, fail_e
    return True, None, None


def split_rules_by_direction(rules: Iterable) -> tuple[List, List]:
    """把混合规则列表按方向拆分为 (caller_rules, callee_rules)。"""
    caller_rules, callee_rules = [], []
    for r in rules:
        d = _rule_direction(r)
        if d == DIR_CALLER:
            caller_rules.append(r)
        elif d == DIR_CALLEE:
            callee_rules.append(r)
    return caller_rules, callee_rules
