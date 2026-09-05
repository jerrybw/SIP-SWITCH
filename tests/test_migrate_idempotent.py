"""约束迁移幂等冒烟测试（需 MySQL，环境不具备时跳过）。

验证：ensure_validation_constraints 在已建库上连跑两次，CHECK 约束集合保持不变
（幂等、不报错、不重复创建），且关键约束名存在。这对应「重建库/换环境自动补齐表结构
与约束」的自迁移保证——直接写库绕过应用层校验时，DB 层约束仍兜住号码位数/NOT NULL/
数值下限/费率取值。

注意：import db.migrate 经由 db.session 触发启动期迁移，需要可达的 MySQL 与有效
config_settings.yaml；本地无 DB 时整文件 skip。
"""
import pytest
from sqlalchemy import create_engine, text

try:
    import core.config as _cc
    _url = (_cc.settings.get("mysql") or {}).get("url")
    from db.migrate import ensure_validation_constraints
    _have = bool(_url)
except Exception:  # noqa: BLE001
    _have = False
    ensure_validation_constraints = None
    _url = None

pytestmark = pytest.mark.skipif(
    not _have,
    reason="需配置 mysql.url 的真实库（config_settings.yaml）",
)


def _check_names(engine):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT CONSTRAINT_NAME, TABLE_NAME FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE CONSTRAINT_TYPE='CHECK' AND TABLE_SCHEMA=DATABASE()"
        )).mappings().all()
    return {(r["TABLE_NAME"], r["CONSTRAINT_NAME"]) for r in rows}


# 代表性约束（账号号格式/位数、话机号格式/位数、接入点计费单位下限、网关成本费率取值）
_EXPECTED = {
    ("account", "chk_acct_no_format"),
    ("account", "chk_acct_no_len"),
    ("sip_phone", "chk_phone_no_format"),
    ("sip_phone", "chk_phone_no_len"),
    ("access_point", "chk_ap_bill_unit"),
    ("gateway", "chk_gw_cost_rate"),
}


def test_validation_constraints_idempotent():
    engine = create_engine(_url)
    ensure_validation_constraints(engine)
    first = _check_names(engine)
    ensure_validation_constraints(engine)  # 第二次应无副作用
    second = _check_names(engine)
    assert first == second, "连跑两次后约束集合发生变化，自迁移非幂等"
    missing = _EXPECTED - first
    assert not missing, f"缺少预期约束: {missing}"
