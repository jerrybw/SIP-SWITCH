"""ORM 模型：与 schema.sql 严格对应。

M1 先用全部模型承接落库与配置读写；路由/规则引擎逻辑在 M2 的 service 层补充。
"""
from sqlalchemy import (
    BigInteger, String, Integer, Text, JSON, SmallInteger, Numeric, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.session import Base


class Customer(Base):
    __tablename__ = "customer"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name = mapped_column(String(128), nullable=False)
    remark = mapped_column(String(512))
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class Account(Base):
    """租户 = 计费主体（v0.3 多租户）。

    account_number 即租户号（8000 起自增，4 位），同时是话机号码的前缀：
    话机号码 = account_number(4) + 分机(4) = 8 位，故号码天然全局唯一、天然隔离。
    """
    __tablename__ = "account"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # customer 为历史遗留维度：业务上已不使用（customer 表为空），允许 NULL。
    # 2026-09-07 由 nullable=False 放开，否则 POST /api/accounts 不传 customer_id 会撞 1048。
    customer_id = mapped_column(BigInteger, ForeignKey("customer.id"), nullable=True)
    name = mapped_column(String(128), nullable=False)
    balance = mapped_column(Numeric(14, 4), default=0.0)
    currency = mapped_column(String(8), default="CNY")
    # 计费（T-计费）：账户级默认费率（元/计费单位），兜底层级。
    rate = mapped_column(Numeric(10, 4))
    # v0.3 多租户：租户号（8000 起自增，全局唯一），同时作为话机号码前缀。
    account_number = mapped_column(String(16), unique=True)
    # v0.3 预付费：信用额度（允许透支上限，0=不允许透支），逐租户可配。
    credit_limit = mapped_column(Numeric(14, 4), default=0)
    # v0.3 预付费：呼叫前预留安全额度（可用余额需 > 此值才放通）。
    min_balance = mapped_column(Numeric(14, 4), default=0)
    status = mapped_column(SmallInteger, default=1)
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class Business(Base):
    __tablename__ = "business"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id = mapped_column(BigInteger, ForeignKey("account.id"), nullable=False)
    name = mapped_column(String(128), nullable=False)
    concurrent_limit = mapped_column(Integer, default=0)
    rate_template_id = mapped_column(BigInteger)
    status = mapped_column(SmallInteger, default=1)
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class Carrier(Base):
    __tablename__ = "carrier"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name = mapped_column(String(128), nullable=False)
    cost_rate_template_id = mapped_column(BigInteger)
    # v0.3 成本侧：**成本侧计费单位**(秒)，独立于收入侧接入点 bill_unit（运营商常按 6 秒计费）。
    bill_unit = mapped_column(Integer, default=60)
    # v0.3 成本侧：运营商级成本费率（元/成本计费单位），网关为空时回落到此值。
    cost_rate = mapped_column(Numeric(10, 4))
    # v0.3.1 成本侧扣费：运营商余额（与账户同语义），但**无余额不足拦截**——
    # 话单成本始终从此扣减，余额可扣成负数（运营商不阻断通话）。
    balance = mapped_column(Numeric(14, 4), default=0)
    currency = mapped_column(String(8), default="CNY")
    status = mapped_column(SmallInteger, default=1)
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class Gateway(Base):
    __tablename__ = "gateway"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    carrier_id = mapped_column(BigInteger, ForeignKey("carrier.id"), nullable=False)
    name = mapped_column(String(128), nullable=False)
    ip = mapped_column(String(45), nullable=False)
    port = mapped_column(Integer, default=5060)
    auth_type = mapped_column(SmallInteger, default=0)
    username = mapped_column(String(128))
    password = mapped_column(String(128))
    concurrent_limit = mapped_column(Integer, default=0)
    heartbeat_enabled = mapped_column(SmallInteger, default=1)
    heartbeat_interval = mapped_column(Integer, default=30)
    heartbeat_timeout = mapped_column(Integer, default=3)
    heartbeat_status = mapped_column(SmallInteger, default=1)
    last_heartbeat_time = mapped_column(DateTime)
    # T-204 心跳防抖：连续失败计数，达到阈值才判离线（任一次成功清零并纳回）。
    heartbeat_fail_count = mapped_column(Integer, default=0)
    switch_mode = mapped_column(SmallInteger, default=0)
    switch_timeout = mapped_column(Integer, default=12)
    switch_codes = mapped_column(String(64), default="503,500,408,486")
    # T-205 故障切换：未振铃才允许切换(1=开启，即本腿已收到 180/183 则不再切走)。
    failover_pre_ring_only = mapped_column(SmallInteger, default=0)
    # v0.3 成本侧：**成本侧计费单位**(秒)，独立于收入侧接入点 bill_unit。
    bill_unit = mapped_column(Integer, default=60)
    # v0.3 成本侧：网关级成本费率（元/成本计费单位），优先于运营商级。
    cost_rate = mapped_column(Numeric(10, 4))
    status = mapped_column(SmallInteger, default=1)
    # ---- 注册型网关：注册参数 + 实时注册状态（2026-09-10）----
    # 注册有效期(秒)：下发为 <param name="expire-seconds">；FS 会在到期前自动续注册。
    # 历史行为：不下发时 FS 用自身默认 3600（实测 Expires/Freq 都是 3600）。
    register_expire = mapped_column(Integer, default=600)
    # 注册失败后的重试间隔(秒)：下发为 <param name="retry-seconds">。
    register_retry = mapped_column(Integer, default=30)
    # 当前注册状态：0 未注册 / 1 已注册 / 2 注册中 / 3 注册失败。
    # 由 ESL `CUSTOM sofia::gateway_state` 事件回写（见 gw_state.py），点对点网关恒为 0。
    register_status = mapped_column(SmallInteger, default=0)
    # 最后一次状态变更时间（UTC），供判断状态新鲜度。
    register_status_at = mapped_column(DateTime)
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class PrefixRoute(Base):
    __tablename__ = "prefix_route"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gateway_id = mapped_column(BigInteger, ForeignKey("gateway.id"), nullable=False)
    prefix = mapped_column(String(32), nullable=False)
    priority = mapped_column(Integer, default=0)
    status = mapped_column(SmallInteger, default=1)
    created_at = mapped_column(DateTime)


class AccessPoint(Base):
    __tablename__ = "access_point"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # v0.3 多租户：接入点直挂账户（租户）；商户(business)降级为可选历史维度，故可空。
    business_id = mapped_column(BigInteger, ForeignKey("business.id"), nullable=True)
    account_id = mapped_column(BigInteger, ForeignKey("account.id"))
    name = mapped_column(String(128), nullable=False)
    auth_mode = mapped_column(SmallInteger, default=0)
    reg_username = mapped_column(String(128))
    reg_password = mapped_column(String(128))
    # 来源 IP/域名白名单，**多个用逗号分隔**（入局校验按逗号拆分后匹配）。
    # 2026-09-05：原 varchar(45) 仅够放单个 IPv6(39 字符)，多值必然超长/截断，
    # 与「多 IP 用逗号分隔」的设计冲突 → 扩到 512。
    register_host = mapped_column(String(512))
    concurrent_limit = mapped_column(Integer, default=0)
    record_enabled = mapped_column(SmallInteger, default=1)
    # 计费单位(秒)：每接入点可配，最小 1s。CDR 计费时长 = ceil(通话秒/计费单位)*计费单位。
    bill_unit = mapped_column(Integer, default=60, nullable=False)
    # 计费（T-计费）：接入点级费率（元/计费单位），缺则取账户兜底。
    rate = mapped_column(Numeric(10, 4))
    status = mapped_column(SmallInteger, default=1)
    reg_status = mapped_column(SmallInteger, default=0)
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class AccessWhitelist(Base):
    __tablename__ = "access_whitelist"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    access_point_id = mapped_column(BigInteger, ForeignKey("access_point.id"), nullable=False)
    ip = mapped_column(String(45), nullable=False)
    port = mapped_column(Integer)
    remark = mapped_column(String(256))
    created_at = mapped_column(DateTime)


class Rule(Base):
    __tablename__ = "rule"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_type = mapped_column(SmallInteger, nullable=False)
    owner_id = mapped_column(BigInteger, nullable=False)
    direction = mapped_column(SmallInteger, nullable=False)  # 1=主叫, 2=被叫
    act = mapped_column(SmallInteger, nullable=False, default=1)  # 1=allow(白名单), 2=deny(黑名单), 3=translate(变换)；避开MySQL保留字action
    pattern = mapped_column(String(64), nullable=False)
    replace_to = mapped_column(String(255), nullable=True)  # act=3 变换目标：支持 * 引用捕获片段；空串=删前缀
    created_at = mapped_column(DateTime)


class AccessGatewayPolicy(Base):
    __tablename__ = "access_gateway_policy"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    access_point_id = mapped_column(BigInteger, ForeignKey("access_point.id"), nullable=False)
    gateway_id = mapped_column(BigInteger, ForeignKey("gateway.id"), nullable=False)
    policy = mapped_column(SmallInteger, nullable=False)
    created_at = mapped_column(DateTime)


class SystemSetting(Base):
    __tablename__ = "system_setting"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key = mapped_column(String(64), nullable=False)
    value = mapped_column(String(512), nullable=False)
    description = mapped_column(String(256))
    updated_at = mapped_column(DateTime)


class SysUser(Base):
    __tablename__ = "sys_user"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username = mapped_column(String(64), nullable=False)
    password_hash = mapped_column(String(128), nullable=False)
    role = mapped_column(SmallInteger, default=1)
    status = mapped_column(SmallInteger, default=1)
    created_at = mapped_column(DateTime)


class OperationLog(Base):
    __tablename__ = "operation_log"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operator = mapped_column(String(64), nullable=False)
    action = mapped_column(String(32), nullable=False)
    object_type = mapped_column(String(32), nullable=False)
    object_id = mapped_column(String(64))
    detail = mapped_column(JSON)
    created_at = mapped_column(DateTime)


class GatewayNode(Base):
    """#64 网关-节点归属：仅注册型网关(auth_type=1)单选唯一归属节点。

    uk_gateway(gateway_id) 保证单选；点对点网关(auth_type=0)全量下发，不在此表。
    三要素(ip+port+账号)唯一性为跨表约束，由 gateway CRUD 在应用层校验。
    """
    __tablename__ = "gateway_node"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gateway_id = mapped_column(BigInteger, ForeignKey("gateway.id"), nullable=False)
    node_uuid = mapped_column(String(64), nullable=False)
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class FsNode(Base):
    __tablename__ = "fs_node"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_uuid = mapped_column(String(64), nullable=False)
    name = mapped_column(String(128), nullable=False)
    host = mapped_column(String(45), nullable=False)
    esl_port = mapped_column(Integer, default=8021)
    status = mapped_column(SmallInteger, default=1)
    last_health_time = mapped_column(DateTime)
    # ---- #69 FS 节点健康检查（DEP-6）----
    last_heartbeat_at = mapped_column(DateTime)          # 最后一次探测成功时间
    last_concurrency = mapped_column(Integer, default=0)  # 最近并发数（show calls）
    last_reg_count = mapped_column(Integer, default=0)    # 最近注册分机数
    max_concurrency = mapped_column(Integer)              # NULL = 回落 system_setting
    fail_count = mapped_column(SmallInteger, default=0)   # 连续失败次数（防抖）
    created_at = mapped_column(DateTime)


class Cdr(Base):
    __tablename__ = "cdr"
    __table_args__ = (
        UniqueConstraint("uuid", name="uq_cdr_uuid"),
    )
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid = mapped_column(String(64), nullable=False)
    customer_id = mapped_column(BigInteger)
    account_id = mapped_column(BigInteger)
    business_id = mapped_column(BigInteger)
    access_point_id = mapped_column(BigInteger)
    source_ip = mapped_column(String(64))
    source_port = mapped_column(Integer)
    dest_ip = mapped_column(String(64))
    dest_port = mapped_column(Integer)
    caller_type = mapped_column(String(16), default='')
    gateway_id = mapped_column(BigInteger)
    carrier_id = mapped_column(BigInteger)
    # 三段主被叫
    caller_in = mapped_column(String(64), nullable=False)
    callee_in = mapped_column(String(64), nullable=False)
    caller_mid = mapped_column(String(64))
    callee_mid = mapped_column(String(64))
    caller_out = mapped_column(String(64))
    callee_out = mapped_column(String(64))
    # 时间戳
    start_time = mapped_column(DateTime)
    ring_time = mapped_column(DateTime)
    answer_time = mapped_column(DateTime)
    end_time = mapped_column(DateTime)
    # 时长
    talk_duration = mapped_column(Integer)
    bill_unit = mapped_column(Integer, default=60)
    bill_duration = mapped_column(Integer, default=0)
    # 信令与结果
    hangup_cause = mapped_column(String(32))
    sip_code = mapped_column(Integer)
    sip_invite_failure_status = mapped_column(String(16))
    reject_reason = mapped_column(String(64))
    # 挂断方向(2026-09-03 新增)：0=服务器 1=主叫 2=被叫 3=其他（esl_client 按呼叫阶段+信令判定）
    hangup_direction = mapped_column(SmallInteger, default=0)
    switch_count = mapped_column(Integer, default=0)
    switch_detail = mapped_column(JSON)
    # 录音
    record_status = mapped_column(SmallInteger, default=0)
    record_path = mapped_column(String(512))
    # 计费收入侧（T-计费 v0.2）：当通消费（入库即算，仅接通计费）；rate_used 为实际采用费率。
    cost = mapped_column(Numeric(12, 4), default=0)
    rate_used = mapped_column(Numeric(10, 4))
    # 计费成本侧（v0.3）：当通成本（付给上游，仅接通计费）；与收入侧同事务算出。
    cost_price = mapped_column(Numeric(12, 4), default=0)
    # 实际采用的成本费率（元/成本计费单位），对账/审计。
    cost_rate_used = mapped_column(Numeric(10, 4))
    # 实际采用的**成本侧**计费单位（秒），独立于收入侧 bill_unit（运营商常按 6 秒计费）。
    cost_bill_unit = mapped_column(Integer)
    # 毛利 = cost - cost_price（冗余落库，报表可直接 SUM，可为负）。
    profit = mapped_column(Numeric(12, 4), default=0)
    # 预付费扣费标记：0 未扣 / 1 已扣。防重扣第二道闸（第一道为 ledger 唯一键）。
    billed = mapped_column(SmallInteger, default=0)
    # 运维
    fs_node_uuid = mapped_column(String(64))
    created_at = mapped_column(DateTime)


class SipPhone(Base):
    __tablename__ = "sip_phone"
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # v0.3 多租户：话机归属租户（核心修复——此前无归属字段，全局按号码匹配会跨租户串号）。
    account_id = mapped_column(BigInteger, ForeignKey("account.id"))
    phone_number = mapped_column(String(64), nullable=False)
    password = mapped_column(String(128), nullable=False)
    status = mapped_column(SmallInteger, default=0)  # 注册在线状态(phone_sync 写)：1在线/0离线
    enabled = mapped_column(SmallInteger, default=1)  # 管理启停：1启用/0停用(2026-09-03 新增, 与注册状态分离)
    sync_interval = mapped_column(Integer, default=30)
    domain = mapped_column(String(128), default="")
    # 计费（T-计费）：话机级费率（元/计费单位），优先于接入点/账户。
    rate = mapped_column(Numeric(10, 4))
    description = mapped_column(String(256))
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)


class AccountLedger(Base):
    """账户余额流水（v0.3 预付费）。

    · type: 1=充值 2=通话扣费 3=人工调整 4=退款
    · amount: 正=入账 / 负=出账
    · uk_ledger_cdr(cdr_uuid) 是**防重扣第一道闸**——同话单只能扣一次。
      MySQL 中 NULL 不参与唯一冲突，故充值/调整类流水（cdr_uuid=NULL）不受限。
    ⚠️ 本表**不做分区**；若日后按 created_at 分区，唯一键必须含分区键，否则触发 1503。
    """
    __tablename__ = "account_ledger"
    __table_args__ = (
        UniqueConstraint("cdr_uuid", name="uk_ledger_cdr"),
    )
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id = mapped_column(BigInteger, nullable=False)
    cdr_uuid = mapped_column(String(64))
    type = mapped_column(SmallInteger, nullable=False)  # 1充值 2通话扣费 3人工调整 4退款
    amount = mapped_column(Numeric(14, 4), nullable=False)  # 正=入账 负=出账
    balance_after = mapped_column(Numeric(14, 4), nullable=False)
    remark = mapped_column(String(256))
    created_at = mapped_column(DateTime, nullable=False)


class CarrierLedger(Base):
    """运营商余额流水（v0.3.1 成本侧扣费）。

    · type: 1=充值 2=通话扣费 3=人工调整 4=退款
    · amount: 正=入账 / 负=出账
    · uk_carrier_ledger_cdr(cdr_uuid) 防重扣第一道闸（与账户侧 uk_ledger_cdr 对称）。
    · 运营商不拦截余额不足，扣费只写流水、不阻断通话；余额可负。
    ⚠️ 本表**不分区**；若日后按 created_at 分区，唯一键必须含分区键，否则触发 1503。
    """
    __tablename__ = "carrier_ledger"
    __table_args__ = (
        UniqueConstraint("cdr_uuid", name="uk_carrier_ledger_cdr"),
    )
    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    carrier_id = mapped_column(BigInteger, nullable=False)
    cdr_uuid = mapped_column(String(64))
    type = mapped_column(SmallInteger, nullable=False)  # 1充值 2通话扣费 3人工调整 4退款
    amount = mapped_column(Numeric(14, 4), nullable=False)  # 正=入账 负=出账
    balance_after = mapped_column(Numeric(14, 4), nullable=False)
    remark = mapped_column(String(256))
    created_at = mapped_column(DateTime, nullable=False)
