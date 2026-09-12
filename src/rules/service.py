"""规则加载与裁决服务（T-201 / 业务网关层）。

从 DB 加载 ``rule`` 表，按方向/owner 拆分后交给 ``rules.matcher`` 裁决主被叫限制；
P1 新增按 owner 维度裁决（接入点/落地网关）与号码变换（translate）。
所有路由/限制决策都在网关层完成，由 mod_xml_curl 下发给 FS（架构铁律）。
"""
import logging
from typing import List, Optional, Tuple

from sqlalchemy import select

from db.models import Rule
from rules.matcher import (
    DIR_CALLER, DIR_CALLEE,
    evaluate_direction,
)

_log = logging.getLogger("rules.service")

# owner 维度编码（与 Rule.owner_type 对齐；1=全局, 2=接入点, 3=落地网关）
OWNER_GLOBAL = 1
OWNER_ACCESS_POINT = 2
OWNER_GATEWAY = 3

# 动作编码（与 Rule.act 对齐；act=3 仅用于变换，不参与 allow/deny 裁决）
ACT_TRANSLATE = 3


def load_all_rules(db) -> List[Rule]:
    """加载全部规则（兼容旧调用；新代码请用 load_rules 按 owner 过滤）。"""
    return list(db.scalars(select(Rule)).all())


def evaluate_call(db, caller, callee) -> Tuple[bool, Optional[int], Optional[object]]:
    """对一通呼叫的主被叫做全局限制裁决（owner_type=1, owner_id=0）。"""
    return evaluate_call_scoped(db, OWNER_GLOBAL, 0, caller, callee)


def load_rules(db, owner_type: int, owner_id: int,
               direction: Optional[int] = None,
               acts: Optional[Tuple[int, ...]] = None) -> List[Rule]:
    """按 owner + 可选 direction / acts 过滤加载规则。"""
    q = select(Rule).where(Rule.owner_type == owner_type, Rule.owner_id == owner_id)
    if direction is not None:
        q = q.where(Rule.direction == direction)
    if acts is not None:
        q = q.where(Rule.act.in_(acts))
    return list(db.scalars(q).all())


def evaluate_call_scoped(db, owner_type: int, owner_id: int,
                         caller: str, callee: str) -> Tuple[bool, Optional[int], Optional[object]]:
    """对指定 owner（接入点/落地网关/全局）做两方向限制裁决。

    复用 ``matcher.evaluate_direction``：deny 优先 + allow 白名单（全串锚定匹配）。
    返回 (allowed, failed_dir, failed_rule)；口径：caller/callee 用进入本资源前的原始号。
    """
    rules = load_rules(db, owner_type, owner_id)
    caller_rules = [r for r in rules if r.direction == DIR_CALLER]
    callee_rules = [r for r in rules if r.direction == DIR_CALLEE]
    ok_c, fail_c = evaluate_direction(caller_rules, caller)
    if not ok_c:
        return False, DIR_CALLER, fail_c
    ok_e, fail_e = evaluate_direction(callee_rules, callee)
    if not ok_e:
        return False, DIR_CALLEE, fail_e
    return True, None, None


# ---- 号码变换（§2.5.1：号首前缀匹配，不复用 matcher 全串锚定）----
import re as _re


def _translate_pattern_to_regex(pattern: str):
    """变换专用：* → (.*) 捕获组；? → . 单字符；其余字面转义（^ 前缀锚定，仅匹配号首，不再子串命中）。"""
    buf = []
    for ch in pattern:
        if ch == "*":
            buf.append("(.*)")
        elif ch == "?":
            buf.append(".")
        else:
            buf.append(_re.escape(ch))
    return _re.compile("^" + "".join(buf), _re.DOTALL)


def _apply_one(number: str, rule: Rule) -> str:
    """对单条 translate 规则做前缀替换：仅当号首命中 pattern 时替换（^ 锚定）。

    replace_to 中 ``*`` 引用捕获片段；空 replace_to = 删除匹配到的前缀。
    防御（2026-09-12）：replace_to 含 ``*`` 但 pattern 不含 ``*``（无捕获组）时，
    原 \1 引用会让 re.sub 抛 re.error → /fs/dialplan 500、该呼叫挂断。此处
    把无法引用的 ``*`` 降级为字面量并打日志，宁可变换结果怪异也不让整通呼叫失败。
    """
    rx = _translate_pattern_to_regex(rule.pattern)
    if not rx.search(number):
        return number
    repl = rule.replace_to or ""
    if "*" in rule.replace_to:
        if "*" in (rule.pattern or ""):
            repl = repl.replace("*", r"\1")
        else:
            # pattern 无捕获组：* 无从引用，降级为字面 *（防 re.error 崩溃）
            _log.warning("[translate] rule %s: replace_to 含 * 但 pattern %r 无 *（无捕获组），"
                          "按字面 * 处理", getattr(rule, "id", "?"), rule.pattern)
    return rx.sub(repl, number, count=1)


def _translate_direction(number: str, rules: List[Rule]) -> str:
    """对某方向的一组 translate 规则，选最长匹配(pattern 最长、并列 rule.id 最小)的那条应用；
    无命中返回原号。

    最长匹配（需求 #2）：单通电话在多条变换规则都命中时，取最具体的（pattern 字符串最长）生效，
    使变换确定可预期，不再依赖规则入库顺序。
    """
    candidates = [r for r in rules if _translate_pattern_to_regex(r.pattern).search(number)]
    if not candidates:
        return number
    best = max(candidates, key=lambda r: (len(r.pattern or ""), -r.id))
    return _apply_one(number, best)


def apply_translate(db, owner_type: int, owner_id: int,
                    caller: str, callee: str) -> Tuple[str, str]:
    """对指定 owner 做主叫/被叫变换；每方向取最长匹配规则生效（见 _translate_direction）。

    返回 (caller_out, callee_out)。
    """
    caller_rules = load_rules(db, owner_type, owner_id, direction=DIR_CALLER, acts=(ACT_TRANSLATE,))
    callee_rules = load_rules(db, owner_type, owner_id, direction=DIR_CALLEE, acts=(ACT_TRANSLATE,))
    caller_out = _translate_direction(caller, caller_rules)
    callee_out = _translate_direction(callee, callee_rules)
    return caller_out, callee_out
