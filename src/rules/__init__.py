"""src/rules —— 路由与接入规则引擎（M2）。

T-201 主被叫限制规则引擎入口。占位符语义见 matcher.py。
"""
from .matcher import (
    translate_pattern,
    match_number,
    evaluate_direction,
    evaluate,
    split_rules_by_direction,
    DIR_CALLER,
    DIR_CALLEE,
    ACTION_ALLOW,
    ACTION_DENY,
)
from .service import (
    load_all_rules, evaluate_call,
    evaluate_call_scoped, apply_translate,
    OWNER_GLOBAL, OWNER_ACCESS_POINT, OWNER_GATEWAY, ACT_TRANSLATE,
)

__all__ = [
    "translate_pattern",
    "match_number",
    "evaluate_direction",
    "evaluate",
    "split_rules_by_direction",
    "DIR_CALLER",
    "DIR_CALLEE",
    "ACTION_ALLOW",
    "ACTION_DENY",
    "load_all_rules",
    "evaluate_call",
    "evaluate_call_scoped",
    "apply_translate",
    "OWNER_GLOBAL",
    "OWNER_ACCESS_POINT",
    "OWNER_GATEWAY",
    "ACT_TRANSLATE",
]
