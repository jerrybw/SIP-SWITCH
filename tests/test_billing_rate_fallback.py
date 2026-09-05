"""计费链 _eff_rate 回落冒烟测试（需 MySQL + ESL 包，环境不具备时跳过）。

_eff_rate 是费率链取值原语：NULL 或 <=0 一律视为「本层未配置」并继续向下一级回落；
仅正数视为有效费率。这是 v0.3 计费链（话机->接入点->账户 / 网关->运营商）正确性的核心
不变量——若把 0 当有效费率，链路会在话机层被「免费」截断，账户兜底费率永不生效。

注意：import esl_client 会触发 db.session 启动期迁移，故需要可达的 MySQL；
本地无 DB 时整文件 skip。
"""
import pytest
from decimal import Decimal

try:
    from esl_client import _eff_rate
except Exception:  # noqa: BLE001
    _eff_rate = None

pytestmark = pytest.mark.skipif(
    _eff_rate is None,
    reason="需 MySQL+ESL 环境（import esl_client 触发启动期迁移）",
)


@pytest.mark.parametrize("inp,exp", [
    (None, None),
    (0, None),
    (0.0, None),
    (-0.5, None),
    ("0", None),
    (0.011, Decimal("0.011")),
    (1, Decimal("1")),
    ("1.5", Decimal("1.5")),
])
def test_eff_rate_fallback(inp, exp):
    # 仅正数视为有效；NULL/<0/==0 一律视为未配置（回落下一级）
    assert _eff_rate(inp) == exp
