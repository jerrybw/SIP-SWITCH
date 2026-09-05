"""数据库会话：主写从读（R-1102）。

M1 阶段先连单库；主从就绪后：
- 写入使用 `SessionLocal`（主库 url）
- 查询使用 `ReadSessionLocal`（从库 url，在 settings 增加 database.read_url 即可）
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from core.config import settings


class Base(DeclarativeBase):
    pass


_write_engine = create_engine(settings["mysql"]["url"], echo=settings["mysql"].get("echo", False))
SessionLocal = sessionmaker(bind=_write_engine, autoflush=False, expire_on_commit=False)

# 启动期自迁移（补齐 rule.act / rule.replace_to 列），服务器端执行，避开运维通道 DDL 限制。
from db.migrate import ensure_rule_act_column, ensure_rule_replace_to_column  # noqa: E402
from db.migrate import ensure_sip_phone_table, seed_static_phones  # noqa: E402
from db.migrate import ensure_system_setting_table, ensure_system_setting_defaults, ensure_ap_reg_status_column  # noqa: E402
from db.migrate import ensure_cdr_caller_type_column  # noqa: E402
from db.migrate import ensure_cdr_src_dst_columns  # noqa: E402
from db.migrate import ensure_gateway_failover_pre_ring_only_column  # noqa: E402
from db.migrate import ensure_sip_phone_enabled_column  # noqa: E402
from db.migrate import ensure_cdr_hangup_direction_column  # noqa: E402
from db.migrate import ensure_gateway_heartbeat_fail_count_column  # noqa: E402
from db.migrate import ensure_billing_columns, ensure_cdr_uuid_unique  # noqa: E402
# v0.3 多租户 / 成本 / 预付费
from db.migrate import ensure_multitenant_columns, ensure_cost_columns, ensure_account_ledger_table  # noqa: E402
from db.migrate import ensure_access_point_account_column  # noqa: E402
# v0.3.1 成本侧扣费：运营商余额列 + 运营商流水表
from db.migrate import ensure_carrier_balance_columns, ensure_carrier_ledger_table  # noqa: E402
# 2026-09-04 DB 层校验约束（号码位数/数值下限/费率取值，防直接写库绕过应用层）
from db.migrate import ensure_validation_constraints, ensure_endpoint_host_columns  # noqa: E402

ensure_rule_act_column(_write_engine)
ensure_rule_replace_to_column(_write_engine)
ensure_sip_phone_table(_write_engine)
seed_static_phones(_write_engine)
ensure_system_setting_table(_write_engine)
ensure_system_setting_defaults(_write_engine)
ensure_ap_reg_status_column(_write_engine)
ensure_cdr_caller_type_column(_write_engine)
ensure_cdr_src_dst_columns(_write_engine)
ensure_gateway_failover_pre_ring_only_column(_write_engine)
ensure_sip_phone_enabled_column(_write_engine)
ensure_cdr_hangup_direction_column(_write_engine)
ensure_gateway_heartbeat_fail_count_column(_write_engine)
ensure_billing_columns(_write_engine)
ensure_cdr_uuid_unique(_write_engine)
# v0.3：多租户列 + 成本列 + 流水表。⚠️ 号码迁移 migrate_phone_numbers 不在此自动执行——
# 它会改写话机号码，须在停机窗口内手动触发（见设计稿 §10 R1）。
ensure_multitenant_columns(_write_engine)
ensure_cost_columns(_write_engine)
ensure_account_ledger_table(_write_engine)
ensure_access_point_account_column(_write_engine)
# v0.3.1 成本侧扣费
ensure_carrier_balance_columns(_write_engine)
ensure_carrier_ledger_table(_write_engine)
# 2026-09-04 DB 层校验约束（幂等；重启即自动补齐/校验）
ensure_validation_constraints(_write_engine)
ensure_endpoint_host_columns(_write_engine)

# 主从就绪后启用：
# _read_engine = create_engine(settings["mysql"]["read_url"], echo=False)
# ReadSessionLocal = sessionmaker(bind=_read_engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI 依赖：写入会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
