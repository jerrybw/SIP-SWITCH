"""P2 全局并发上限（G1 死配置修复）单测 —— 纯函数面，无 Redis / MySQL 依赖。

覆盖 `core/config.py::_apply_defaults` 的 `concurrent_limit_global` 口径：
- 缺省 / 显式 0 → 0（**0 = 不限制**，非"禁止"——注释与测试双锁定语义）
- 正数透传；负数钳 0；数字串强转；垃圾值回落 0
- 键落在顶层（与 node/record 同级），不嵌在 concurrency 段——
  app.py 六处读取点全部 `settings.get("concurrent_limit_global")` 顶层取值，
  嵌套会让所有读取点静默失效（PITFALLS #53 死配置同型，双保险）。
"""
import core.config as cc


def _d(cfg):
    return cc._apply_defaults(cfg)


def test_default_when_absent():
    """配置未写该键 → 0（=不限制，与 92d6d1a 行为完全一致，回归零变化）。"""
    assert _d({})["concurrent_limit_global"] == 0


def test_zero_means_unlimited():
    """显式 0 与缺省等价——0 是「不限制」不是「禁止」。"""
    assert _d({"concurrent_limit_global": 0})["concurrent_limit_global"] == 0


def test_positive_passthrough():
    assert _d({"concurrent_limit_global": 5})["concurrent_limit_global"] == 5


def test_negative_clamped_to_zero():
    assert _d({"concurrent_limit_global": -3})["concurrent_limit_global"] == 0


def test_numeric_string_coerced():
    assert _d({"concurrent_limit_global": "7"})["concurrent_limit_global"] == 7


def test_garbage_falls_back_to_zero():
    assert _d({"concurrent_limit_global": "abc"})["concurrent_limit_global"] == 0


def test_key_is_top_level_not_nested():
    """键必须在顶层：嵌进 concurrency 段会让 app.py 全部读取点静默拿 0。"""
    cfg = _d({"concurrent_limit_global": 4})
    assert cfg["concurrent_limit_global"] == 4
    assert "concurrent_limit_global" not in (cfg.get("concurrency") or {})
