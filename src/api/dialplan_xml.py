"""mod_xml_curl 拨号计划 XML 生成（T-201 / T-202 / P1 / P2）。

FS mod_xml_curl 要求拨号计划扩展必须位于 <context name="default"> 内，且 <context> 必须
嵌在 <document><section name="dialplan"> 之中（顺序写反会导致 NO_ROUTE_DESTINATION）。
各 build_* 已按上述正确嵌套生成扩展；build_empty_xml 为「交回 FS 既有逻辑」语义，不包 <context>。
T-202 新增 build_outbound_xml（section description="sip-gateway-outbound"，含 T-205 逐腿故障切换）。
"""


def _act(app: str, data: str) -> str:
    # 必须输出合法的自闭合标签 "<action .../>"，否则 FS 无法解析动作。
    return f'<action application="{app}" data="{data}"/>'


def _var(name: str) -> str:
    # 构造 ${name} 形式的 FS 通道变量引用。
    return "${" + name + "}"


# 与 FS 本地 Local_Extension 对齐：匹配 1000-1019，桥接到注册用户。
_LOCAL_EXT_RE = r"^(10[01][0-9])$"

def _cdr_common_sets(access_point_id=None, caller_in=None, callee_in=None,
                     caller_mid=None, callee_mid=None, account_id=None):
    """生成 CDR 关联通道变量的 set 动作（无路由/拒绝时也保留接入点与入局号，T-207 贯通）。

    account_id 用于 dialplan 阶段拦截（预付费余额不足等）显式标注归属账户：
    拦截呼叫不会产生 bridge，_save_cdr 无从按接入点推导账户，须在此显式下发 cdr_account_id。
    """
    out = []
    if account_id is not None:
        out.append("          " + _act('set', 'cdr_account_id=%s' % account_id))
    if access_point_id is not None:
        out.append("          " + _act('set', 'cdr_access_point_id=%s' % access_point_id))
    if caller_in is not None:
        out.append("          " + _act('set', 'cdr_caller_in=%s' % caller_in))
    if callee_in is not None:
        out.append("          " + _act('set', 'cdr_callee_in=%s' % callee_in))
    if caller_mid is not None:
        out.append("          " + _act('set', 'cdr_caller_mid=%s' % caller_mid))
    if callee_mid is not None:
        out.append("          " + _act('set', 'cdr_callee_mid=%s' % callee_mid))
    return out


def build_allow_xml(callee: str, access_point_id=None, bill_unit=60, caller_type=None,
                    context="default", account_id=None) -> str:
    """放行：复刻 Local_Extension 的 bridge（含自动录音）。

    access_point_id / bill_unit 由路由层解析接入点后透传，供 CDR 关联（T-207）。
    account_id：话机注册呼叫（无接入点）时显式下发归属账户，使内线互拨也能落 account_id 并计费。
    """
    rec = "rec_file=${recordings_dir}/${uuid}.wav"
    rec_session_data = "${rec_file}"
    ringback = "${us-ring}"
    transfer_ringback = "${hold_music}"
    bridge_data = "user/$1@${domain_name}"
    lines = [
        "<document type=\"freeswitch/xml\">",
        "  <section name=\"dialplan\" description=\"sip-gateway\">",
        "    <context name=\"%s\">" % context,
        "      <extension name=\"gw_local_extension\">",
        "        <condition field=\"destination_number\" expression=\"" + _LOCAL_EXT_RE + "\">",
        "          " + _act('set', 'dialed_extension=$1'),
        "          " + _act('set', 'ringback=' + ringback),
        "          " + _act('set', 'transfer_ringback=' + transfer_ringback),
        "          " + _act('set', 'call_timeout=30'),
        "          " + _act('set', rec),
        "          " + _act('record_session', rec_session_data),
    ]
    if access_point_id is not None:
        lines.append("          " + _act('set', 'cdr_access_point_id=%s' % access_point_id))
    if account_id is not None:
        lines.append("          " + _act('set', 'cdr_account_id=%s' % account_id))
    if caller_type is not None:
        lines.append("          " + _act('set', 'cdr_caller_type=%s' % caller_type))
    lines.append("          " + _act('set', 'cdr_bill_unit=%s' % bill_unit))
    lines += [
        "          " + _act('bridge', bridge_data),
        "        </condition>",
        "      </extension>",
        "    </context>",
        "  </section>",
        "</document>",
    ]
    return "\n".join(lines) + "\n"


# 拒绝原因写入通道变量，FS 在 CHANNEL_HANGUP_COMPLETE 事件中以
# variable_sip_gateway_reject_reason 暴露，由 ESL 订阅落 CDR.reject_reason。
REJECT_VAR = "sip_gateway_reject_reason"

# P2（D3）：并发超限回 SIP 503，FS 以 NETWORK_OUT_OF_ORDER 表达；
# 限制命中仍用 CALL_REJECTED(603)。
_CAUSE_BY_SIP = {
    "503": "NETWORK_OUT_OF_ORDER",
    "603": "CALL_REJECTED",
}

# T-205 方案A：运维在 gateway.switch_codes 填的是 SIP 状态码(如 503,500,408,486)，
# 但 FS 的 continue_on_fail 只认 **Q.850 hangup_cause 名**(或数字码)，不认 SIP 码——
# 原样塞 SIP 码会导致 failover 永不触发。此处网关层做一层 SIP→FS cause 映射，
# 依据 FS sofia_glue_sip_to_cause 的标准 SIP→cause 转换。
# 非数字 token(运维直接填 cause 名 / Q.850 数字)原样透传；未知 SIP 码回退 NETWORK_OUT_OF_ORDER。
_SIP_TO_FAIL_CAUSE = {
    "400": "INCOMPATIBLE_DESTINATION",
    "401": "UNAUTHORIZED",
    "403": "UNAUTHORIZED",
    "404": "UNALLOCATED_NUMBER",
    "408": "RECOVERY_ON_TIMER_EXPIRE",
    "480": "NO_USER_RESPONSE",
    "481": "UNALLOCATED_NUMBER",
    "486": "USER_BUSY",
    "487": "ORIGINATOR_CANCEL",
    "488": "INCOMPATIBLE_DESTINATION",
    "500": "NORMAL_TEMPORARY_FAILURE",
    "502": "NETWORK_OUT_OF_ORDER",
    "503": "NETWORK_OUT_OF_ORDER",
    "504": "RECOVERY_ON_TIMER_EXPIRE",
    "600": "USER_BUSY",
    "603": "CALL_REJECTED",
}


def _sip_codes_to_continue_on_fail(raw):
    """把运维填写的 SIP 码列表转成 FS continue_on_fail 字符串（逗号分隔 hangup_cause 名）。"""
    out = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            out.append(_SIP_TO_FAIL_CAUSE.get(tok, "NETWORK_OUT_OF_ORDER"))
        else:
            out.append(tok)
    return ",".join(out)


def build_deny_xml(reason: str = "", sip_code: str = "603", context="default",
                   access_point_id=None, caller_in=None, callee_in=None,
                   caller_mid=None, callee_mid=None, account_id=None) -> str:
    """拒绝：匹配任意号码，记录拒绝原因后挂断。

    - reason 形如 "denied_by_caller_rule:138*"，会写入通道变量供 CDR 采集。
    - sip_code 默认 "603"（CALL_REJECTED，限制命中）；P2 并发超限传 "503"
      （NETWORK_OUT_OF_ORDER，D3）。未知 code 回退 CALL_REJECTED。
    """
    cause = _CAUSE_BY_SIP.get(sip_code, "CALL_REJECTED")
    set_reason = _act("set", f"{REJECT_VAR}={reason}") if reason else ""
    lines = [
        "<document type=\"freeswitch/xml\">",
        "  <section name=\"dialplan\" description=\"sip-gateway-deny\">",
        "    <context name=\"%s\">" % context,
        "      <extension name=\"gw_rule_deny\">",
        "        <condition field=\"destination_number\" expression=\"^(.*)$\">",
    ]
    lines += _cdr_common_sets(access_point_id, caller_in, callee_in, caller_mid, callee_mid, account_id)
    if set_reason:
        lines.append("          " + set_reason)
    lines += [
        "          " + _act('hangup', cause),
        "        </condition>",
        "      </extension>",
        "    </context>",
        "  </section>",
        "</document>",
    ]
    return "\n".join(lines) + "\n"


def build_outbound_xml(callee: str, candidates: list, gateway_id=None, carrier_id=None,
                       bill_unit=60, access_point_id=None, record_enabled=1,
                       caller=None, caller_type=None, context="default", caller_in=None, callee_in=None, dst_ip=None, dst_port=None, caller_mid=None, callee_mid=None, account_id=None) -> str:
    """出局路由（T-202 + T-205 故障切换）：命中前缀路由后，桥接到落地网关。

    候选字典结构（每个候选网关一条）：
        {gateway_id, name, carrier_id, caller_out(逐腿变换后主叫),
         callee_out(逐腿变换后被叫), switch_codes(本gw failover码),
         failover_pre_ring_only(1=未振铃才切)}

    - 单候选：单 bridge（无故障切换），CDR 影响=零，行为同旧。
    - 多候选：逐腿展开（unrolled）故障切换（取代依赖 list_get 的 transfer 回环；
      本构建 list_get 在表达式上下文不可用，实机 eval 返回 -ERR no reply）。
        * 网关动态生成 N 个独立 extension(gw_leg_0..N-1)，每个硬编码自身
          name/gid/carrier/callee_out/caller_out/switch_codes(per-leg)；
        * 每腿 bridge 前 set continue_on_fail=<本gw码映射cause>（A 腿通道变量），
          180/183/应答钩子不再使用——execute_on_* 的 set 落在 B 腿、A 腿读不到；
        * bridge 后置决策改用 A 腿可靠变量 sip_invite_failure_status（桥前清空）：
          有失败码=本腿 INVITE 失败(命中 continue_on_fail 才走到后置) → 切下一腿/末腿 exhausted；
          无失败码=成功接通并通话结束（hangup_after_bridge=false 让 bridge 返回后置继续）
          → 追加本腿 WIN 到 switch_detail → transfer gw_leg_done_keep 挂断收尾；
        * 每腿 set cdr_gateway_id/callee_out/caller_out → 落库=实际胜出网关(修续29)；
        * 累计 gw_failover_count + gw_failover_detail → cdr_switch_count/cdr_switch_detail；
          成功腿后置被跳过导致胜出腿 WIN 不落库的缺陷（22f4c12f）由此修复；
        * 候选耗尽→ 末腿失败码命中 → gw_leg_exhausted（hangup ${originate_failed_cause}）。
      重入：leg/done 扩展排在 gw_outbound_pre 之前（PRE 的 ^(.*)$ 仅首呼匹配）；
        transfer 重取 xml_curl 时由 app.py 按 Chat-Unique-ID 缓存返回同一完整文档，
        通道变量(gw_failover_*)随重取回传，loop 自然延续。
    要求：FS external profile 已存在 <gateway name="<gw_name>">。
    """
    cdr_common = _cdr_common_sets(access_point_id, caller_in, callee_in, caller_mid, callee_mid, account_id)

    rec = []
    if record_enabled:
        r = "rec_file=${recordings_dir}/${uuid}.wav"
        rec_session_data = "${rec_file}"
        rec.append("          " + _act('set', r))
        rec.append("          " + _act('record_session', rec_session_data))

    doc_open = [
        "<document type=\"freeswitch/xml\">",
        "  <section name=\"dialplan\" description=\"sip-gateway-outbound\">",
        "    <context name=\"%s\">" % context,
    ]
    doc_close = [
        "    </context>",
        "  </section>",
        "</document>",
    ]

    if len(candidates) <= 1:
        # 单候选：单 bridge（保留旧行为，CDR 影响=零）
        c = candidates[0]
        bridge_data = "sofia/gateway/%s/%s" % (c.get("name"), c.get("callee_out") or callee)
        head = [
            "      <extension name=\"gw_outbound_route\">",
            "        <condition field=\"destination_number\" expression=\"^(.*)$\">",
            "          " + _act('set', 'call_timeout=30'),
            "          " + _act('set', 'sip-force-contact=NDLB-connectile-dysfunction'),
        ]
        cdr = list(cdr_common)
        cdr.append("          " + _act('set', 'cdr_gateway_id=%s' % c.get("gateway_id")))
        cdr.append("          " + _act('set', 'cdr_carrier_id=%s' % c.get("carrier_id")))
        cdr.append("          " + _act('set', 'cdr_callee_out=%s' % c.get("callee_out")))
        cdr.append("          " + _act('set', 'cdr_caller_out=%s' % c.get("caller_out")))
        cdr.append("          " + _act('set', 'effective_caller_id_number=%s' % c.get("caller_out")))
        cdr.append("          " + _act('set', 'caller_id_number=%s' % c.get("caller_out")))
        if caller_type is not None:
            cdr.append("          " + _act('set', 'cdr_caller_type=%s' % caller_type))
        if dst_ip is not None:
            cdr.append("          " + _act('set', 'cdr_dst_ip=%s' % dst_ip))
        if dst_port is not None:
            cdr.append("          " + _act('set', 'cdr_dst_port=%s' % dst_port))
        cdr.append("          " + _act('set', 'cdr_switch_count=0'))
        cdr.append("          " + _act('set', 'cdr_bill_unit=%s' % bill_unit))
        cdr.append("          " + _act('set', 'hangup_after_bridge=true'))
        tail = [
            "          " + _act('bridge', bridge_data),
            "        </condition>",
            "      </extension>",
        ]
        return "\n".join(doc_open + head + cdr + rec + tail + doc_close) + "\n"

    # ===== 多候选：逐腿展开（unrolled）故障切换 =====
    # 取代依赖 list_get 的 transfer 回环（本构建 list_get 在表达式上下文不可用，
    # 实机 eval 返回 -ERR no reply）。每个候选硬编码成一个独立 extension，用 transfer 串链。
    # 顺序铁律：leg/done 扩展必须排在 gw_outbound_pre 之前，否则 PRE 的 ^(.*)$ 会抢匹配。
    # 重入由 app.py 按 Chat-Unique-ID 缓存同一完整文档（transfer 重取 xml_curl 时返回），
    # 通道变量 gw_failover_* 随重取回传，loop 自然延续。

    def _leg_ext(i, c, is_last):
        gid = c.get("gateway_id")
        name = c.get("name")
        carrier = c.get("carrier_id")
        callee_out = c.get("callee_out") or callee
        caller_out = c.get("caller_out")
        raw_codes = c.get("switch_codes") or "503,500,408,486"
        prering = int(c.get("failover_pre_ring_only") or 0)
        # 统一路径：continue_on_fail=<映射码> + 后置 set 记录本腿 switch_detail + transfer 下一扩展。
        # 该路径已被 2026-09-03 实测证明可靠：bridge 失败、命中映射码后，后置 set 与
        # transfer 全部执行（1bb908bd/570f367e leg_0/末腿）。末腿失败走 gw_leg_exhausted，
        # 普通挂断扩展（非 bridge）可正常执行后置 set，把末腿也记入 switch_detail。
        # 注：本构建「末腿用 continue_on_fail=true / transfer_on_fail」均无法接管
        # （f6f8c576 leg_1 实测：bridge 失败直接 Hangup [USER_BUSY] + skip receive message，
        # 无 transfer 日志），故末腿也必须用「具体映射码 + 普通 next 扩展」这一已验证路径。
        codes = _sip_codes_to_continue_on_fail(raw_codes)
        # 末腿命中失败码后转移到的扩展：gw_leg_exhausted 用 ${originate_failed_cause} 挂断，
        # 保留真实失败 cause（如 USER_BUSY=486），避免被改写成 NO_ROUTE_DESTINATION 或
        # NORMAL_CLEARING 干扰下游按 cause 统计。pre_ring_only 命中（已振铃不再切）时
        # 非末腿同样 transfer 至此收尾（该扩展按 destination_number 匹配，无腿序号概念）。
        next_leg = "gw_leg_exhausted" if is_last else ("gw_leg_%d" % (i + 1))
        head_sets = [
            "          " + _act('set', 'cdr_gateway_id=%s' % gid),
            "          " + _act('set', 'cdr_carrier_id=%s' % carrier),
            "          " + _act('set', 'cdr_callee_out=%s' % callee_out),
            "          " + _act('set', 'cdr_caller_out=%s' % caller_out),
            "          " + _act('set', 'effective_caller_id_number=%s' % caller_out),
            "          " + _act('set', 'caller_id_number=%s' % caller_out),
            # pre_ring_only(未振铃才切换)：gw_prering=本网关开关；gw_leg_progress 由 B 腿
            # 收到 180/183 时经 api_on_ring/api_on_pre_answer 执行 uuid_setvar 回写 A 腿=1
            # （见 bridge 前缀），每腿桥前重置 0。
            "          " + _act('set', 'gw_prering=%d' % prering),
            "          " + _act('set', 'gw_leg_progress=0'),
            # 每腿桥前把 originate_failed_cause 重置为哨兵 NONE：audio_bridge_function 在
            # bridge 失败（originate 失败）时**同步直写 A 腿** originate_failed_cause=<Q.850 cause 名>
            # （mod_dptools.c:3636，570f367e exhausted 已实证），成功接通则不写 → 后置以
            # `== NONE` 判断本腿成败。⚠️ 不能 set 空值：FS 空 set=UNDEF/unset，cond 引用 UNDEF
            # 变量直接 -ERR（a01f1df2 实测）；也不能依赖 sofia 的 sip_invite_failure_status
            # （它经 set_variable_partner 同步到 A 腿，sofia.c:6653，秒拒场景 partner 链未建
            # 立 → A 腿读不到）。
            "          " + _act('set', 'originate_failed_cause=NONE'),
            # T-205 关键：continue_on_fail 必须作为 **A 腿通道变量** 用 set 下发。
            # 写成 bridge data 的 {continue_on_fail=...} 前缀只会落到被呼(B)腿，
            # switch_channel_handle_cause 从 A 腿读取，读不到 → 486 等非默认码直接挂断、
            # 后置 set 全部被跳过（2026-09-03 c8643ca1 实测根因，见 FS 源码 switch_channel.c:4907）。
            "          " + _act('set', 'continue_on_fail=%s' % codes),
        ]
        ext_open = [
            "      <extension name=\"gw_leg_%d\">" % i,
            "        <condition field=\"destination_number\" expression=\"^gw_leg_%d$\">" % i,
        ]
        ext_close = [
            "        </condition>",
            "      </extension>",
        ]
        # 接通/失败判定说明（2026-09-03 定稿）：
        # - 成败判据 = A 腿 originate_failed_cause（桥前哨兵 NONE；audio_bridge 失败时同步直写
        #   cause 名，成功不写）——execute_on_* 的 set 落 B 腿、A 腿读不到，原 gw_answered/
        #   progress_received 判断实际恒为 A 腿初值（22f4c12f 潜在缺陷），已弃用。
        # - pre_ring_only(未振铃才切换) 2026-09-03 重新实现：bridge 前缀 export 本腿 A-uuid
        #   给 B 腿(gw_a_uuid=${uuid})，B 腿收 180(PROGRESS)/183(PROGRESS_MEDIA) 时由
        #   api_on_ring/api_on_pre_answer 执行 `uuid_setvar <A-uuid> gw_leg_progress 1`
        #   **回写 A 腿**（execute_on_ring 的 set 做不到，故用 api 钩子）；A 腿后置据此判断
        #   「本腿是否已振铃」——失败且本网关开 pre_ring_only 且已振铃 → 不再切，transfer
        #   gw_leg_exhausted 保留真实 cause 挂断（对端已回铃说明线路通，拒接/忙属于正常结果）。
        #   注：网关 failover_pre_ring_only=1 的判定此前依赖 B 腿 progress_received 从未生效
        #   （c7f66d34 实测 486 仍切），本次为可靠回写方案。
        # ⚠️ cond 条件必须用 `== 哨兵/1` 完整比较（变量恒存在）：FS cond/strlen 引用空或
        # UNDEF 变量直接返回 -ERR（a01f1df2 实测 cond("" ? …)/cond(${undef} ? …) 均 -ERR）。
        gw_rang_blocked = _var("cond(" + _var("gw_prering") + " == 1 ? "
                                + _var("cond(" + _var("gw_leg_progress") + " == 1 ? yes : no)") + " : no)")
        gw_cause = _var("cond(" + _var("originate_failed_cause") + " == NONE ? WIN : "
                         + _var("originate_failed_cause") + ")")
        gw_detail = (_var("gw_failover_detail") + ";%s:%s:" % (gid, callee_out) + _var("gw_cause"))
        gw_count = _var("cond(" + _var("originate_failed_cause") + " == NONE ? "
                         + _var("gw_failover_count") + " : "
                         + _var("expr(" + _var("gw_failover_count") + " + 1)") + ")")
        # 决策：成功(NONE) → done_keep 收尾；失败 + pre_ring 已振铃 → exhausted 保留 cause 停；
        # 失败未振铃(或未开 pre_ring) → 切 next_leg。
        transfer_to = _var("cond(" + _var("originate_failed_cause") + " == NONE ? gw_leg_done_keep : "
                            + _var("cond(" + _var("gw_rang_blocked") + " == yes ? gw_leg_exhausted : "
                                    + next_leg + ")") + ")")
        # pre_ring_only 网关：bridge 前缀 export A 腿 uuid 的 api 钩子到 B 腿，B 腿 180/183 时
        # uuid_setvar 回写 A 腿 gw_leg_progress=1。⚠️ 前缀各 key=value 在同一遍求值：不能引用
        # 前缀内先定义的其它变量（${gw_a_uuid} 在 api_on_ring 值求值时尚未入变量表 → 空，
        # f9b1a861 实测 uuid_setvar <空> 没回写成功）；直接写 ${uuid}——前缀在 A 腿求值，
        # ${uuid} 即 A 腿 uuid 字面量，export 到 B 腿后可直接执行。
        bridge_data = "sofia/gateway/%s/%s" % (name, callee_out)
        if prering:
            bridge_data = ("{api_on_ring=uuid_setvar " + _var("uuid") + " gw_leg_progress 1"
                           + ",api_on_pre_answer=uuid_setvar " + _var("uuid") + " gw_leg_progress 1}"
                           + bridge_data)
        return ext_open + head_sets + [
            "          " + _act('bridge', bridge_data),
            "          " + _act('set', 'gw_rang_blocked=' + gw_rang_blocked),
            "          " + _act('set', 'gw_cause=' + gw_cause),
            "          " + _act('set', 'gw_failover_detail=' + gw_detail),
            "          " + _act('set', 'gw_failover_count=' + gw_count),
            "          " + _act('set', 'cdr_switch_count=' + _var("gw_failover_count")),
            "          " + _act('set', 'cdr_switch_detail=' + _var("gw_failover_detail")),
            "          " + _act('transfer', transfer_to),
        ] + ext_close

    n = len(candidates)
    leg_exts = []
    for i, c in enumerate(candidates):
        leg_exts += _leg_ext(i, c, i == n - 1)
    # 成功腿（bridge 接通并通话结束）收尾：hangup_after_bridge=false 使 bridge 返回后继续
    # 执行后置 set（本腿 WIN 已追加进 switch_detail），随后 transfer 至此挂断收尾。
    # 空参 hangup = NORMAL_CLEARING，与通话正常结束语义一致；若此处只 set 不挂断会
    # fallthrough 到 gw_outbound_pre 的 ^(.*)$ 重新初始化并 transfer gw_leg_0 → 重复拨号。
    done_keep = [
        "      <extension name=\"gw_leg_done_keep\">",
        "        <condition field=\"destination_number\" expression=\"^gw_leg_done_keep$\">",
        "          " + _act('hangup', 'NORMAL_CLEARING'),
        "        </condition>",
        "      </extension>",
    ]
    # 防呆扩展：旧版（2026-09-03 前）曾 transfer 至此，保留以防在途旧文档引用。
    done_no_route = [
        "      <extension name=\"gw_leg_done_no_route\">",
        "        <condition field=\"destination_number\" expression=\"^gw_leg_done_no_route$\">",
        "          " + _act('hangup', 'NO_ROUTE_DESTINATION'),
        "        </condition>",
        "      </extension>",
    ]
    # 末腿命中失败码后的收尾：hangup 用 ${originate_failed_cause} 保留真实失败 cause。
    # FS audio_bridge_function 在 bridge 失败时会先往 A 腿写 originate_failed_cause=<Q.850 cause 名>
    # （如 USER_BUSY），再调用 switch_channel_handle_cause；普通 hangup() 空参恒为 NORMAL_CLEARING
    # （mod_dptools.c hangup_function 源码：data 为空直接 SWITCH_CAUSE_NORMAL_CLEARING，不读通道 cause），
    # 故必须显式传 ${originate_failed_cause}。switch_detail 已在本腿后置 set 中记录完毕。
    exhausted = [
        "      <extension name=\"gw_leg_exhausted\">",
        "        <condition field=\"destination_number\" expression=\"^gw_leg_exhausted$\">",
        "          " + _act('hangup', _var("originate_failed_cause")),
        "        </condition>",
        "      </extension>",
    ]
    pre_ext = [
        "      <extension name=\"gw_outbound_pre\">",
        "        <condition field=\"destination_number\" expression=\"^(.*)$\">",
        "          " + _act('set', 'call_timeout=30'),
        "          " + _act('set', 'sip-force-contact=NDLB-connectile-dysfunction'),
    ]
    pre_ext += cdr_common
    if caller_type is not None:
        pre_ext.append("          " + _act('set', 'cdr_caller_type=%s' % caller_type))
    if dst_ip is not None:
        pre_ext.append("          " + _act('set', 'cdr_dst_ip=%s' % dst_ip))
    if dst_port is not None:
        pre_ext.append("          " + _act('set', 'cdr_dst_port=%s' % dst_port))
    pre_ext += [
        "          " + _act('set', 'cdr_bill_unit=%s' % bill_unit),
        "          " + _act('set', 'gw_failover_detail='),
        "          " + _act('set', 'gw_failover_count=0'),
        # hangup_after_bridge=false：让「成功接通并通话结束」的腿 bridge 返回后继续执行
        # 后置 set（把本腿 WIN 追加进 switch_detail）再 transfer gw_leg_done_keep 挂断；
        # 失败腿不受影响（未接通走 continue_on_fail/handle_cause，与 hangup_after_bridge 无关）。
        # 22f4c12f 实测：=true 时成功腿直接 Hangup [NORMAL_CLEARING]、后置 set 全跳过，
        # 胜出腿 WIN 从未落 switch_detail（对比 1bb908bd 末腿失败能落，因失败腿走后置路径）。
        "          " + _act('set', 'hangup_after_bridge=false'),
    ]
    pre_ext += rec
    pre_ext += [
        "          " + _act('transfer', 'gw_leg_0'),
        "        </condition>",
        "      </extension>",
    ]

    # leg/done 在前，PRE 在后（PRE ^(.*)$ 仅首呼匹配）
    return "\n".join(doc_open + leg_exts + done_keep + done_no_route + exhausted + pre_ext + doc_close) + "\n"



def build_empty_xml(access_point_id=None, caller_in=None, callee_in=None,
                    caller_mid=None, callee_mid=None, context="default", account_id=None) -> str:
    """无出局路由（接入点已匹配但落地无候选）：挂断前先写 CDR 关联变量（T-207 贯通）。

    不再交回 FS 静态拨号（避免 enum 等静态扩展劫持导致主叫收 480/无 CDR 关联）。
    """
    cdr_sets = _cdr_common_sets(access_point_id, caller_in, callee_in, caller_mid, callee_mid, account_id)
    lines = [
        "<document type=\"freeswitch/xml\">",
        "  <section name=\"dialplan\" description=\"sip-gateway-no-route\">",
        "    <context name=\"%s\">" % context,
        "      <extension name=\"gw_no_route\">",
        "        <condition field=\"destination_number\" expression=\"^(.*)$\">",
    ]
    lines += cdr_sets
    lines += [
        "          " + _act('hangup', 'NO_ROUTE_DESTINATION'),
        "        </condition>",
        "      </extension>",
        "    </context>",
        "  </section>",
        "</document>",
    ]
    return "\n".join(lines) + "\n"
