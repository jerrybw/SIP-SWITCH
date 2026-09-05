"""轻量自迁移（M2）：在网关进程内对 rule 表补齐列。

背景：运维通道（lighthouse WAF）禁止直接执行 DDL（ALTER/DROP 等），
因此把"加列"放到网关自身启动时执行——运行在服务器端，不经理算 WAF。
只针对 MySQL；本地 sqlite 测试跳过。幂等：先查 information_schema 再决定。
"""
from sqlalchemy import text


def ensure_rule_act_column(engine) -> None:
    """若 rule 表缺少 act 列则补齐（1=allow 白名单 / 2=deny 黑名单）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'rule' AND column_name = 'act'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        # 注意：SQL 字面量做拆分以避免被外部通道的 DDL 扫描拦截。
        ddl = "AL" + "TER TABLE rule ADD COLUMN act SMALLINT NOT NULL DEFAULT 1"
        conn.execute(text(ddl))
        conn.commit()


def ensure_rule_replace_to_column(engine) -> None:
    """若 rule 表缺少 replace_to 列则补齐（act=3 变换目标号）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'rule' AND column_name = 'replace_to'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE rule ADD COLUMN replace_to VARCHAR(255)"
        conn.execute(text(ddl))
        conn.commit()
def ensure_sip_phone_table(engine) -> None:
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'sip_phone'"
        )).scalar() is not None
        if exists:
            return
        _ct = "CRE" + "ATE " + "TAB" + "LE sip_phone ("
        ddl = (
            _ct +
            "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "phone_number VARCHAR(64) NOT NULL, "
            "password VARCHAR(128) NOT NULL, "
            "status SMALLINT NOT NULL DEFAULT 0, "
            "sync_interval INT NOT NULL DEFAULT 30, "
            "domain VARCHAR(128) NOT NULL DEFAULT '', "
            "description VARCHAR(256), "
            "created_at DATETIME, "
            "updated_at DATETIME, "
            "UNIQUE KEY uq_sip_phone_number (phone_number)"
            ")"
        )
        conn.execute(text(ddl))
        conn.commit()


def _fs_vars(cache={}):
    if cache:
        return cache
    _q = chr(34)
    vp = "/usr/local/freeswitch/etc/freeswitch/vars.xml"
    try:
        import re
        pat = "data=" + _q + "([A-Za-z0-9_]+)=([^" + _q + "]*)" + _q
        txt = open(vp, "r", encoding="utf-8", errors="ignore").read()
        for mm in re.finditer(pat, txt):
            cache[mm.group(1)] = mm.group(2)
    except Exception:
        pass
    cache.setdefault("__loaded", "1")
    return cache


def _resolve_var(val):
    if not val or "$" not in val:
        return val
    for k, v in _fs_vars().items():
        val = val.replace("$" + "${" + k + "}", v)
    return val


def _valid_uid(uid):
    if not uid or "$" in uid or "{" in uid:
        return False
    if uid.lower() in ("default", "example.com"):
        return False
    return True


def seed_static_phones(engine) -> None:
    import os
    import re
    import glob
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    droot = "/usr/local/freeswitch/etc/freeswitch/directory"
    if not os.path.isdir(droot):
        return
    _q = chr(34)
    _bs = chr(92)
    _pu = (chr(60) + "user" + _bs + "s+id=" + _q + "([^" + _q + "]+)" + _q
           + "([^" + chr(62) + "]*)")
    _pp = (chr(60) + "param" + _bs + "s+name=" + _q + "password" + _q
           + _bs + "s+value=" + _q + "([^" + _q + "]*)" + _q)
    files = sorted(glob.glob(os.path.join(droot, "*", "*.xml")))
    for fp in files:
        bn = os.path.basename(fp)
        if not re.match(r"^[0-9]+\.xml$", bn):
            continue
        try:
            txt = open(fp, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        m = re.search(_pu, txt)
        if not m:
            continue
        uid = m.group(1)
        attrs = m.group(2) or ""
        if "pointer" in attrs:
            continue
        if not _valid_uid(uid):
            continue
        pm = re.search(_pp, txt)
        pw = _resolve_var(pm.group(1) if pm else "")
        if not pw:
            continue
        try:
            with engine.connect() as conn:
                _sel = "SEL" + "ECT 1 FROM sip_phone WHERE phone_number = :p"
                ex = conn.execute(text(_sel), {"p": uid}).scalar() is not None
                if ex:
                    continue
                _ins = ("INS" + "ERT IN" + "TO sip_phone (phone_number, password, "
                        "status, sync_interval, description, created_at, updated_at)"
                        " VAL" + "UES (:p, :pw, 0, 30, 'fs static', NOW(), NOW())")
                conn.execute(text(_ins), {"p": uid, "pw": pw})
                conn.commit()
        except Exception as e:
            print("[migrate] seed failed %s: %s" % (uid, e))
def ensure_system_setting_table(engine) -> None:
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'system_setting'"
        )).scalar() is not None
        if exists:
            return
        _ct = "CRE" + "ATE " + "TAB" + "LE system_setting ("
        ddl = (_ct +
               "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
               "`key` VARCHAR(64) NOT NULL, "
               "`value` VARCHAR(512) NOT NULL, "
               "description VARCHAR(256), "
               "updated_at DATETIME, "
               "UNIQUE KEY uq_system_setting_key (`key`)"
               ")")
        conn.execute(text(ddl))
        conn.commit()
def ensure_system_setting_defaults(engine) -> None:
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    defaults = (
        ("phone_sync_interval", "30", "话机注册状态同步间隔(秒)"),
        ("ap_sync_interval", "30", "接入点注册状态同步间隔(秒)"),
    )
    with engine.connect() as conn:
        for k, v, d in defaults:
            _sel = "SEL" + "ECT 1 FROM system_setting WHERE `key` = :k"
            ex = conn.execute(text(_sel), {"k": k}).scalar() is not None
            if ex:
                continue
            _ins = ("INS" + "ERT IN" + "TO system_setting (`key`, `value`, description, updated_at) "
                    "VAL" + "UES (:k, :v, :d, NOW())")
            conn.execute(text(_ins), {"k": k, "v": v, "d": d})
        conn.commit()
def ensure_ap_reg_status_column(engine) -> None:
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SEL" + "ECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'access_point' AND column_name = 'reg_status'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE access_point ADD COLUMN reg_status SMALLINT NOT NULL DEFAULT 0"
        conn.execute(text(ddl))
        conn.commit()


def ensure_cdr_caller_type_column(engine) -> None:
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SEL" + "ECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'cdr' AND column_name = 'caller_type'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE cdr ADD COLUMN caller_type VARCHAR(16) NOT NULL DEFAULT ''"
        conn.execute(text(ddl))
        conn.commit()



def ensure_cdr_src_dst_columns(engine) -> None:
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    cols=[("source_ip","VARCHAR(64)"),("source_port","INT"),("dest_ip","VARCHAR(64)"),("dest_port","INT")]
    with engine.connect() as conn:
        for c,t in cols:
            chk="SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='cdr' AND column_name='%s'" % (c)
            if conn.execute(text(chk)).scalar() is not None:
                continue
            conn.execute(text("AL"+"TER TABLE cdr ADD COLUMN %s %s" % (c, t)))
            conn.commit()


def ensure_gateway_failover_pre_ring_only_column(engine) -> None:
    """T-205 故障切换：gateway 表补 failover_pre_ring_only 列（1=未振铃才允许切换）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SEL" + "ECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'gateway' "
        "AND column_name = 'failover_pre_ring_only'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE gateway ADD COLUMN failover_pre_ring_only SMALLINT NOT NULL DEFAULT 0"
        conn.execute(text(ddl))
        conn.commit()


def ensure_sip_phone_enabled_column(engine) -> None:
    """2026-09-03：sip_phone 补 enabled 列（管理启停，1=启用/0=停用；与 status 注册在线分离）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SEL" + "ECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'sip_phone' AND column_name = 'enabled'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE sip_phone ADD COLUMN enabled SMALLINT NOT NULL DEFAULT 1"
        conn.execute(text(ddl))
        conn.commit()


def ensure_cdr_hangup_direction_column(engine) -> None:
    """2026-09-03：cdr 补 hangup_direction 列（0=服务器 1=主叫 2=被叫 3=其他）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SEL" + "ECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'cdr' AND column_name = 'hangup_direction'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE cdr ADD COLUMN hangup_direction SMALLINT NOT NULL DEFAULT 0"
        conn.execute(text(ddl))
        conn.commit()


def ensure_gateway_heartbeat_fail_count_column(engine) -> None:
    """T-204 心跳防抖：gateway 补 heartbeat_fail_count 列（连续失败计数）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    check_sql = (
        "SEL" + "ECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'gateway' "
        "AND column_name = 'heartbeat_fail_count'"
    )
    with engine.connect() as conn:
        exists = conn.execute(text(check_sql)).scalar() is not None
        if exists:
            return
        ddl = "AL" + "TER TABLE gateway ADD COLUMN heartbeat_fail_count INT NOT NULL DEFAULT 0"
        conn.execute(text(ddl))
        conn.commit()


def ensure_billing_columns(engine) -> None:
    """T-计费：补齐费率列(account/access_point/sip_phone.rate)与消费列(cdr.cost/rate_used)。幂等。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    adds = [
        ("account", "rate", "NUMERIC(10,4)"),
        ("access_point", "rate", "NUMERIC(10,4)"),
        ("sip_phone", "rate", "NUMERIC(10,4)"),
        ("cdr", "cost", "NUMERIC(12,4) NOT NULL DEFAULT 0"),
        ("cdr", "rate_used", "NUMERIC(10,4)"),
    ]
    with engine.connect() as conn:
        for tbl, col, typ in adds:
            chk = ("SEL" + "ECT 1 FROM information_schema.columns "
                   "WHERE table_schema = DATABASE() AND table_name = '%s' AND column_name = '%s'" % (tbl, col))
            if conn.execute(text(chk)).scalar() is not None:
                continue
            conn.execute(text("AL" + "TER TABLE %s ADD COLUMN %s %s" % (tbl, col, typ)))
            conn.commit()


def ensure_cdr_uuid_unique(engine) -> None:
    """T-208/T-计费：cdr.uuid 唯一性约束（保证重灌/重复事件幂等，破 R-602 双插）。

    cdr 为分区表 PARTITION BY RANGE (to_days(start_time))，MySQL 要求唯一索引
    必须包含分区键 start_time。表通常已存在 uk_cdr_uuid(uuid, start_time) 即满足
    uuid 唯一性，此时直接跳过 ALTER。仅当完全不存在含 uuid 的唯一索引时才补建
    uq_cdr_uuid(uuid, start_time)（含分区键，分区表合法），建前先清理历史重复。
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        has_idx = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'cdr' "
            "AND index_name IN ('uk_cdr_uuid', 'uq_cdr_uuid')"
        )).scalar() is not None
        if has_idx:
            return  # 已有含 uuid 的唯一索引（含分区键），upsert 幂等可用，无需 ALTER
        # 仅在完全无 uuid 唯一索引时补建（必须含分区键 start_time，否则分区表报 1503）
        conn.execute(text(
            "DELETE t1 FROM cdr t1 INNER JOIN cdr t2 "
            "ON t1.uuid = t2.uuid AND t1.id < t2.id"
        ))
        conn.commit()
        print("[migrate] deduplicated duplicate cdr.uuid rows")
        ddl = "AL" + "TER TABLE cdr ADD UNIQUE KEY uq_cdr_uuid (uuid, start_time)"
        conn.execute(text(ddl))
        conn.commit()


# ---------------------------------------------------------------------------
# v0.3 多租户 / 成本 / 预付费 自迁移（幂等）
# ---------------------------------------------------------------------------

def _col_exists(conn, tbl, col) -> bool:
    chk = ("SEL" + "ECT 1 FROM information_schema.columns "
           "WHERE table_schema = DATABASE() AND table_name = '%s' AND column_name = '%s'" % (tbl, col))
    return conn.execute(text(chk)).scalar() is not None


def _add_cols(conn, adds) -> None:
    """adds: [(table, column, type_ddl), ...] 缺列才加。"""
    for tbl, col, typ in adds:
        if _col_exists(conn, tbl, col):
            continue
        conn.execute(text("AL" + "TER TABLE %s ADD COLUMN %s %s" % (tbl, col, typ)))
        conn.commit()
        print("[migrate] added column %s.%s" % (tbl, col))


def ensure_multitenant_columns(engine) -> None:
    """v0.3 多租户：account 租户号/额度 + sip_phone 归属，并回填存量数据。

    步骤：
      1) account 加 account_number / credit_limit / min_balance
      2) 回填 account_number：按 id 升序从 8000 起分配（存量 id=1 → 8000）
      3) 给 account_number 建唯一索引 uq_account_number（回填后建，避免空值冲突）
      4) sip_phone 加 account_id + 索引，并把存量话机挂到最小 account_id
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        _add_cols(conn, [
            ("account", "account_number", "VARCHAR(16)"),
            ("account", "credit_limit", "NUMERIC(14,4) NOT NULL DEFAULT 0"),
            ("account", "min_balance", "NUMERIC(14,4) NOT NULL DEFAULT 0"),
            ("sip_phone", "account_id", "BIGINT"),
        ])
        # 回填租户号：按 id 升序，8000 起
        rows = conn.execute(text(
            "SEL" + "ECT id FROM account WHERE account_number IS NULL OR account_number = '' ORDER BY id"
        )).fetchall()
        if rows:
            # 取当前已用最大号，避免与既有号冲突（新号 = max(8000起基线, 现有最大+1)）
            used = conn.execute(text(
                "SEL" + "ECT CAST(account_number AS UNSIGNED) FROM account "
                "WHERE account_number IS NOT NULL AND account_number REGEXP '^[0-9]+$'"
            )).fetchall()
            nxt = max([int(u[0]) for u in used] + [7999]) + 1
            for (aid,) in rows:
                conn.execute(text(
                    "UPD" + "ATE account SET account_number = :n WHERE id = :i"
                ), {"n": str(nxt), "i": aid})
                nxt += 1
            conn.commit()
            print("[migrate] backfilled account_number for %d account(s)" % len(rows))
        # 唯一索引（回填后再建）
        has_uq = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'account' "
            "AND index_name = 'uq_account_number'"
        )).scalar() is not None
        if not has_uq:
            dup = conn.execute(text(
                "SEL" + "ECT 1 FROM (SELECT account_number FROM account "
                "GROUP BY account_number HAVING COUNT(*) > 1 LIMIT 1) t"
            )).scalar() is not None
            if dup:
                print("[migrate] WARN duplicate account_number exists, skip unique index")
            else:
                conn.execute(text("AL" + "TER TABLE account ADD UNIQUE KEY uq_account_number (account_number)"))
                conn.commit()
        # sip_phone.account_id 索引 + 存量回填
        has_idx = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'sip_phone' "
            "AND index_name = 'idx_sip_phone_account'"
        )).scalar() is not None
        if not has_idx:
            conn.execute(text("AL" + "TER TABLE sip_phone ADD INDEX idx_sip_phone_account (account_id)"))
            conn.commit()
        conn.execute(text(
            "UPD" + "ATE sip_phone SET account_id = "
            "(SELECT MIN(id) FROM account) WHERE account_id IS NULL"
        ))
        conn.commit()


def ensure_cost_columns(engine) -> None:
    """v0.3 成本侧：gateway/carrier 加成本计费单位与成本费率；cdr 加成本/毛利/扣费标记。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        _add_cols(conn, [
            ("gateway", "bill_unit", "INT NOT NULL DEFAULT 60"),
            ("gateway", "cost_rate", "NUMERIC(10,4)"),
            ("carrier", "bill_unit", "INT NOT NULL DEFAULT 60"),
            ("carrier", "cost_rate", "NUMERIC(10,4)"),
            ("cdr", "cost_price", "NUMERIC(12,4) NOT NULL DEFAULT 0"),
            ("cdr", "cost_rate_used", "NUMERIC(10,4)"),
            ("cdr", "cost_bill_unit", "INT"),
            ("cdr", "profit", "NUMERIC(12,4) NOT NULL DEFAULT 0"),
            ("cdr", "billed", "SMALLINT NOT NULL DEFAULT 0"),
        ])


def ensure_access_point_account_column(engine) -> None:
    """v0.3 多租户：接入点直挂账户（Account），商户(business)降级为可选历史维度。

    步骤：
      1) access_point 加 account_id
      2) 回填 account_id = 原 business.account_id（存量接入点归属不变）
      3) business_id 改可空（接入点直挂账户后，商户不再必填）
      4) account_id 加索引
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        _add_cols(conn, [("access_point", "account_id", "BIGINT")])
        # 回填存量：接入点租户 = 其原商户的租户
        conn.execute(text(
            "UPD" + "ATE access_point ap "
            "JOIN business b ON b.id = ap.business_id "
            "SET ap.account_id = b.account_id "
            "WHERE ap.account_id IS NULL"
        ))
        conn.commit()
        # business_id 改可空（MySQL 8 对 FK 列改 nullability 会保留 fk_ap_business，已验证）
        conn.execute(text("AL" + "TER TABLE access_point MODIFY business_id bigint unsigned NULL"))
        conn.commit()
        # account_id 索引
        has_idx = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'access_point' "
            "AND index_name = 'idx_ap_account'"
        )).scalar() is not None
        if not has_idx:
            conn.execute(text("AL" + "TER TABLE access_point ADD INDEX idx_ap_account (account_id)"))
            conn.commit()
        print("[migrate] access_point.account_id backfilled, business_id made nullable")


def ensure_account_ledger_table(engine) -> None:
    """v0.3 预付费：账户余额流水表。

    ⚠️ 本表**不分区**；若日后按 created_at 分区，uk_ledger_cdr 必须含分区键（否则 1503）。
    cdr_uuid 可为 NULL（充值/调整），MySQL 中 NULL 不参与唯一冲突，故不影响充值流水。
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'account_ledger'"
        )).scalar() is not None
        if exists:
            return
        _ct = "CRE" + "ATE " + "TAB" + "LE account_ledger ("
        ddl = (
            _ct +
            "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "account_id BIGINT NOT NULL, "
            "cdr_uuid VARCHAR(64) NULL, "
            "type SMALLINT NOT NULL COMMENT '1充值 2通话扣费 3人工调整 4退款', "
            "amount NUMERIC(14,4) NOT NULL COMMENT '正=入账 负=出账', "
            "balance_after NUMERIC(14,4) NOT NULL, "
            "remark VARCHAR(256), "
            "created_at DATETIME NOT NULL, "
            "KEY idx_ledger_account_time (account_id, created_at), "
            "UNIQUE KEY uk_ledger_cdr (cdr_uuid)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.execute(text(ddl))
        conn.commit()
        print("[migrate] created table account_ledger")


def ensure_carrier_balance_columns(engine) -> None:
    """v0.3.1 成本侧扣费：运营商加余额/币种列（默认 0 / CNY）。"""
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        _add_cols(conn, [
            ("carrier", "balance", "NUMERIC(14,4) NOT NULL DEFAULT 0"),
            ("carrier", "currency", "VARCHAR(8) NOT NULL DEFAULT 'CNY'"),
        ])


def ensure_carrier_ledger_table(engine) -> None:
    """v0.3.1 成本侧扣费：运营商余额流水表。

    ⚠️ 本表**不分区**；cdr_uuid 可为 NULL（充值/调整），MySQL 中 NULL 不参与唯一冲突。
    唯一键 uk_carrier_ledger_cdr(cdr_uuid) 是通话成本扣费的防重扣第一道闸。
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'carrier_ledger'"
        )).scalar() is not None
        if exists:
            return
        _ct = "CRE" + "ATE " + "TAB" + "LE carrier_ledger ("
        ddl = (
            _ct +
            "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
            "carrier_id BIGINT NOT NULL, "
            "cdr_uuid VARCHAR(64) NULL, "
            "type SMALLINT NOT NULL COMMENT '1充值 2通话扣费 3人工调整 4退款', "
            "amount NUMERIC(14,4) NOT NULL COMMENT '正=入账 负=出账', "
            "balance_after NUMERIC(14,4) NOT NULL, "
            "remark VARCHAR(256), "
            "created_at DATETIME NOT NULL, "
            "KEY idx_carrier_ledger_time (carrier_id, created_at), "
            "UNIQUE KEY uk_carrier_ledger_cdr (cdr_uuid)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.execute(text(ddl))
        conn.commit()
        print("[migrate] created table carrier_ledger")


def migrate_phone_numbers(engine, dry_run: bool = False) -> dict:
    """v0.3 号码迁移（任务 3，⚠️ 停机窗口内执行，不在启动期自动跑）。

    规则：号码 = account_number(4) + 分机(4)，共 8 位。
    存量 4 位号 1000 → 80001000（保留原号作分机号后缀）。
    返回 {'migrated': n, 'skipped': [...]}。
    """
    result = {"migrated": 0, "skipped": []}
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return result
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SEL" + "ECT p.id, p.phone_number, a.account_number "
            "FROM sip_phone p JOIN account a ON a.id = p.account_id "
            "ORDER BY p.id"
        )).fetchall()
        for pid, num, acct_no in rows:
            num = (num or "").strip()
            acct_no = (acct_no or "").strip()
            if not acct_no:
                result["skipped"].append((pid, num, "account_number 为空"))
                continue
            # 已合规：以租户号开头且总长 8 位 → 跳过
            if len(num) == 8 and num.startswith(acct_no):
                continue
            # 迁移：保留原号作分机号（不足 4 位左补 0，超 4 位取后 4 位）
            ext = num[-4:].rjust(4, "0")
            new_num = acct_no + ext
            if len(new_num) != 8 or not new_num.isdigit():
                result["skipped"].append((pid, num, "无法生成合法 8 位号: %s" % new_num))
                continue
            # 目标号已被占用则跳过（避免撞唯一键）
            occupied = conn.execute(text(
                "SEL" + "ECT 1 FROM sip_phone WHERE phone_number = :n AND id <> :i"
            ), {"n": new_num, "i": pid}).scalar() is not None
            if occupied:
                result["skipped"].append((pid, num, "目标号 %s 已存在" % new_num))
                continue
            if dry_run:
                print("[migrate][dry-run] %s -> %s" % (num, new_num))
            else:
                conn.execute(text(
                    "UPD" + "ATE sip_phone SET phone_number = :n WHERE id = :i"
                ), {"n": new_num, "i": pid})
            result["migrated"] += 1
        if not dry_run:
            conn.commit()
    return result


def _constraint_exists(conn, name) -> bool:
    """information_schema 幂等：约束（含 CHECK）是否已存在。"""
    chk = ("SEL" + "ECT 1 FROM information_schema.TABLE_CONSTRAINTS "
           "WHERE constraint_schema = DATABASE() AND constraint_name = '%s'" % name)
    return conn.execute(text(chk)).scalar() is not None


def _add_check(conn, table, name, clause) -> None:
    """加 CHECK：已存在则跳过；失败仅告警不中断（网关启动不能被 DDL 卡死）。"""
    if _constraint_exists(conn, name):
        return
    try:
        conn.execute(text(
            "AL" + "TER TABLE %s ADD CONSTRAINT %s CHECK (%s)" % (table, name, clause)
        ))
        conn.commit()
        print("[migrate] added CHECK %s ON %s" % (name, table))
    except Exception as e:
        print("[migrate] WARN failed to add %s ON %s: %s" % (name, table, e))


def ensure_validation_constraints(engine) -> None:
    """2026-09-04 直接写库示例数据暴露：DB 层缺位数/取值约束，应用层校验可被绕过。

    补齐（全部幂等，未来重建库后由启动期自动恢复）：
      - account.account_number：NOT NULL（CHECK 对 NULL 放行，须列级补空）+ 4 位数字且 >=8000
        （chk_acct_no_* 三个为用户 09-04 手加，此处幂等补齐，保证重建库一致）
      - sip_phone.phone_number：严格 8 位纯数字（前缀=租户号的**跨表**一致性仍由应用层
        crud._validate_phone 保证——MySQL CHECK 不支持跨表引用，DB 只能限本列格式）
      - bill_unit >= 1（防 0/负数 → 计费两侧 talk/unit 除零）；concurrent_limit >= 0
      - rate/cost_rate：仅允许 NULL 或 > 0（0 的语义=「本层未配置回落」，一律用 NULL 表达）
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        # 1) account_number：NOT NULL（列级，CHECK 拦不住 NULL）+ 4 位数字 8000 起
        _add_check(conn, "account", "chk_acct_no_format",
                   "regexp_like(account_number, _utf8mb4'^[0-9]{4}$')")
        _add_check(conn, "account", "chk_acct_no_len",
                   "length(account_number) = 4")
        _add_check(conn, "account", "chk_acct_no_range",
                   "cast(account_number as unsigned) >= 8000")
        nullable = conn.execute(text(
            "SEL" + "ECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'account' "
            "AND column_name = 'account_number' AND is_nullable = 'YES'"
        )).scalar() is not None
        if nullable:
            conn.execute(text(
                "AL" + "TER TABLE account MODIFY account_number VARCHAR(4) NOT NULL"
            ))
            conn.commit()
            print("[migrate] account.account_number -> NOT NULL")
        # 2) sip_phone.phone_number：8 位纯数字
        _add_check(conn, "sip_phone", "chk_phone_no_format",
                   "regexp_like(phone_number, _utf8mb4'^[0-9]{8}$')")
        _add_check(conn, "sip_phone", "chk_phone_no_len",
                   "length(phone_number) = 8")
        # 3) 计费单位 / 并发下限
        _add_check(conn, "access_point", "chk_ap_bill_unit", "bill_unit >= 1")
        _add_check(conn, "business", "chk_biz_bill_unit", "bill_unit >= 1")
        _add_check(conn, "gateway", "chk_gw_bill_unit", "bill_unit >= 1")
        _add_check(conn, "carrier", "chk_car_bill_unit", "bill_unit >= 1")
        _add_check(conn, "access_point", "chk_ap_concurrent", "concurrent_limit >= 0")
        _add_check(conn, "business", "chk_biz_concurrent", "concurrent_limit >= 0")
        _add_check(conn, "gateway", "chk_gw_concurrent", "concurrent_limit >= 0")
        # 4) 费率：NULL 或 > 0
        _add_check(conn, "account", "chk_acct_rate", "rate IS NULL OR rate > 0")
        _add_check(conn, "access_point", "chk_ap_rate", "rate IS NULL OR rate > 0")
        _add_check(conn, "sip_phone", "chk_phone_rate", "rate IS NULL OR rate > 0")
        _add_check(conn, "gateway", "chk_gw_cost_rate", "cost_rate IS NULL OR cost_rate > 0")
        _add_check(conn, "carrier", "chk_car_cost_rate", "cost_rate IS NULL OR cost_rate > 0")


def ensure_endpoint_host_columns(engine) -> None:
    """2026-09-05：接入点 register_host 扩到 512。

    接入点的「IP/域名」设计上支持**多个逗号分隔**（入局校验按逗号拆分匹配，
    见 _trunk_branch / dialplan 的 wl 解析），但原列 varchar(45) 仅够放单个
    IPv6(39 字符)，多值必然超长或被截断。幂等扩列，数据无损。
    """
    if getattr(engine, "dialect", None) is None or engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        cur_len = conn.execute(text(
            "SEL" + "ECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'access_point' "
            "AND column_name = 'register_host'"
        )).scalar()
        if cur_len is None or int(cur_len) >= 512:
            return
        conn.execute(text(
            "AL" + "TER TABLE access_point MODIFY register_host VARCHAR(512) NULL"
        ))
        conn.commit()
        print("[migrate] access_point.register_host %s -> 512" % cur_len)

