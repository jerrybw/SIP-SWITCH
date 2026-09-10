// P3 管理控制台前端：通用 CRUD 引擎 + 特殊面板
//   接入点：允许/禁止落地网关(G4) + IP 白名单(需求#4) + 四配置项规则(需求#2)
//   落地网关：路由前缀(逗号分隔, 需求#3) + 四配置项规则(需求#2)

const SECTIONS = {
  'access-points': {
    label: '接入点', list: '/api/access-points', owner_type: 2, toggleField: 'status', showId: true,
    specials: ['ap-policy', 'ap-rules'],
    topbar: [{ k: 'ap_sync_interval', label: '注册状态同步间隔(秒)' }],
    filters: [
      { k: 'name', label: '名称' },
      { k: 'register_host', label: 'IP/域名' },
      { k: 'auth_mode', label: '对接模式', type: 'select', options: [{ v: 0, t: '点对点' }, { v: 1, t: '注册' }] },
    ],
    fields: [
      { k: 'name', label: '名称', type: 'text', required: true },
      { k: 'auth_mode', label: '对接模式', type: 'select', options: [{ v: 0, t: '点对点' }, { v: 1, t: '注册' }] },
      { k: 'register_host', label: 'IP / 域名(联系地址)', type: 'text', hostList: true,
        requiredIf: { k: 'auth_mode', v: 0 },
        hint: '多个用英文逗号分隔；来源 IP 入局校验用。点对点模式必填（靠来源 IP 识别接入点），注册模式可留空=不校验来源 IP' },
      { k: 'reg_username', label: '注册账号', type: 'text', hint: '点对点可留空；注册模式填 FS 注册用户名' },
      { k: 'reg_password', label: '注册密码', type: 'password' },
      { k: 'account_id', label: '租户/账户', type: 'select-src', src: '/api/accounts', optk: 'id', optt: 'name', optt2: 'account_number', required: true, hint: '接入点归属租户（Account）；费率回落与话机号段按此租户隔离' },
      { k: 'concurrent_limit', label: '并发上限(0=不限)', type: 'number', def: 0 },
      { k: 'record_enabled', label: '录音', type: 'select', options: [{ v: 1, t: '开' }, { v: 0, t: '关' }] },
      { k: 'bill_unit', label: '计费单位(秒)', type: 'number', def: 60 },
      { k: 'rate', label: '费率(元/计费单位)', type: 'number', emptyNull: true,
        hint: '接入点级费率；留空（或 0）则回落账户默认费率' },
      { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
      { k: 'reg_status', label: '注册状态', type: 'select', ro: true, options: [{ v: 1, t: '在线' }, { v: 0, t: '离线' }] },
    ],
  },
  'gateways': {
    label: '落地网关', list: '/api/gateways', owner_type: 3, toggleField: 'status', showId: true,
    specials: ['prefixes', 'gw-rules'],
    filters: [
      { k: 'name', label: '名称' },
      { k: 'ip', label: 'IP' },
      { k: 'auth_type', label: '对接模式', type: 'select', options: [{ v: 0, t: '点对点' }, { v: 1, t: '注册' }] },
      { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
    ],
    fields: [
      { k: 'name', label: '名称', type: 'text', required: true },
      { k: 'carrier_id', label: '运营商', type: 'select-src', src: '/api/carriers/options', optk: 'id', optt: 'name', required: true, hint: '须先在「运营商」模块创建运营商；默认选中第一个可用运营商' },
      { k: 'ip', label: 'IP', type: 'text', required: true, hostSingle: true,
        hint: '出局目标地址：IPv4 / IPv6 / 域名，不含端口（端口填下方「端口」）' },
      { k: 'port', label: '端口', type: 'number', def: 5060 },
      { k: 'auth_type', label: '对接模式', type: 'select', options: [{ v: 0, t: '点对点' }, { v: 1, t: '注册' }] },
      { k: 'node_uuid', label: '归属节点', type: 'select-src', src: '/api/nodes', optk: 'node_uuid', optt: 'host',
        listKey: 'node_name', emptyText: '全量',
        requiredIf: { k: 'auth_type', v: '1' },
        hint: '注册模式：只下发给所选节点（单选）；点对点模式全量下发所有节点，无需选择' },
      { k: 'username', label: '账号', type: 'text' },
      { k: 'password', label: '密码', type: 'password' },
      { k: 'register_expire', label: '注册有效期(秒)', type: 'number', def: 600, noList: true,
        hint: '注册模式专用：下发为 expire-seconds，FS 在到期前自动续注册；不填按默认 600（历史行为是 FS 自身默认 3600）' },
      { k: 'register_retry', label: '注册重试间隔(秒)', type: 'number', def: 30, noList: true,
        hint: '注册模式专用：注册失败后的重试间隔，下发为 retry-seconds' },
      { k: 'register_status', label: '注册状态', type: 'select', ro: true,
        options: [{ v: 0, t: '未注册' }, { v: 1, t: '已注册' }, { v: 2, t: '注册中' }, { v: 3, t: '注册失败' }],
        badgeMap: { 0: 'badge-off', 1: 'badge-on', 2: 'badge-warn', 3: 'badge-fail' },
        hint: '由 FreeSWITCH 的 sofia::gateway_state 事件实时回写，不可手工修改' },
      { k: 'register_status_at', label: '状态更新时间', type: 'text', ro: true, fmt: 'time' },
      { k: 'concurrent_limit', label: '并发上限(0=不限)', type: 'number', def: 0 },
      { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
      { k: 'heartbeat_enabled', label: '心跳探测', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
      { k: 'heartbeat_interval', label: '心跳间隔(秒)', type: 'number', def: 10 },
      { k: 'heartbeat_timeout', label: '心跳超时(秒)', type: 'number', def: 3 },
      { k: 'heartbeat_status', label: '心跳状态', type: 'select', ro: true, options: [{ v: 1, t: '正常' }, { v: 0, t: '离线' }] },
      { k: 'heartbeat_fail_count', label: '连续失败', type: 'number', ro: true },
      { k: 'last_heartbeat_time', label: '最后探测', type: 'text', ro: true },
      { k: 'switch_codes', label: '故障切换码(逗号分隔)', type: 'text', def: '503,500,408,486',
        hint: '本网关失败且 cause 命中时，允许切到下一个候选网关' },
      { k: 'failover_pre_ring_only', label: '未振铃才切换', type: 'select',
        options: [{ v: 0, t: '关闭(任意失败均切换)' }, { v: 1, t: '开启(已振铃180/183则不切)' }], def: 0 },
      { k: 'bill_unit', label: '成本计费单位(秒)', type: 'number', def: 60,
        hint: '成本侧计费单位（独立于收入侧接入点计费单位）；空则回落运营商' },
      { k: 'cost_rate', label: '成本费率(元/计费单位)', type: 'number', emptyNull: true,
        hint: '网关级成本费率，优先于运营商级；留空则回落运营商级，二者皆空则成本为 0' },
    ],
  },
  'prefix-routes': {
    label: '前缀路由', list: '/api/prefix-routes', showId: true,
    filters: [
      { k: 'prefix', label: '前缀' },
      { k: 'gateway_id', label: '落地网关ID' },
      { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
    ],
    fields: [
      { k: 'gateway_id', label: '落地网关', type: 'select-src', src: '/api/gateways', optk: 'id', optt: 'name' },
      { k: 'prefix', label: '前缀', type: 'text', required: true, hint: '被叫号以此前缀开头即命中（如 86）' },
      { k: 'priority', label: '优先级', type: 'number', def: 0, hint: '同前缀多网关时取大优先' },
      { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
    ],
  },
  'rules': {
    label: '限制/变换规则(全局)', list: '/api/rules', showId: true,
    fields: [
      { k: 'owner_type', label: '归属类型', type: 'select', options: [{ v: 1, t: '全局' }, { v: 2, t: '接入点' }, { v: 3, t: '落地网关' }] },
      { k: 'owner_id', label: '归属ID', type: 'number', hint: '全局填 0；接入点/落地网关填对应 ID' },
      { k: 'direction', label: '方向', type: 'select', options: [{ v: 1, t: '主叫' }, { v: 2, t: '被叫' }] },
      { k: 'act', label: '动作', type: 'select', options: [{ v: 1, t: '允许(白名单)' }, { v: 2, t: '拒绝(黑名单)' }, { v: 3, t: '变换' }] },
      { k: 'pattern', label: '匹配(支持 * ?)', type: 'text', required: true, hint: '* 任意长，? 单字符' },
      { k: 'replace_to', label: '变换目标', type: 'text', hint: '仅变换动作：* 引用捕获段，空=删前缀' },
    ],
  },
};

SECTIONS['sip-phones'] = {
  label: '话机管理', list: '/api/sip-phones', toggleField: 'enabled',
  topbar: [{ k: 'phone_sync_interval', label: '状态同步间隔(秒)' }],
  filters: [
    { k: 'phone_number', label: '号码' },
    { k: 'enabled', label: '启用状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
  ],
  fields: [
    { k: 'account_id', label: '所属租户', type: 'select-src', src: '/api/accounts', optk: 'id', optt: 'name', optt2: 'account_number', required: true,
      hint: '话机归属租户；号码须以该租户号开头（8 位）' },
    { k: 'phone_number', label: '话机号码', type: 'text', required: true,
      placeholder: '80000001', hint: '必须为 8 位且以所属租户号开头（如 80000001），否则保存失败' },
    { k: 'password', label: '密码', type: 'password' },
    { k: 'enabled', label: '启用状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }], def: 1,
      hint: '停用后 FS 目录不再下发该话机：无法注册、无法被叫' },
    { k: 'status', label: '注册状态', type: 'select', ro: true, options: [{ v: 1, t: '在线' }, { v: 0, t: '离线' }] },
    { k: 'domain', label: '目录域', type: 'text' },
    { k: 'rate', label: '费率(元/计费单位)', type: 'number', emptyNull: true,
      hint: '话机级费率，优先于接入点/账户；留空（或 0）则回落接入点/账户费率' },
    { k: 'description', label: '备注', type: 'text' },
  ],
};
SECTIONS['cdr'] = { label: '话单', list: '/api/cdr', custom: 'cdr' };
SECTIONS['billing'] = { label: '计费', custom: 'billing' };
SECTIONS['carriers'] = {
  label: '运营商', list: '/api/carriers', custom: 'carriers', showId: true,
  fields: [
    { k: 'name', label: '名称', type: 'text', required: true },
    { k: 'bill_unit', label: '成本计费单位(秒)', type: 'number', def: 60,
      hint: '成本侧计费单位（独立于收入侧接入点计费单位），运营商常按 6 秒计' },
    { k: 'cost_rate', label: '成本费率(元/计费单位)', type: 'number', emptyNull: true,
      hint: '运营商级成本费率；网关留空时回落到此值，二者皆空则成本为 0' },
    { k: 'balance', label: '余额(元)', type: 'number', ro: true, hint: '话单成本从此扣减（可扣成负数）；支持充值' },
    { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
  ],
};
SECTIONS['accounts'] = {
  label: '租户', list: '/api/accounts', custom: 'accounts', showId: true,
  fields: [
    { k: 'account_number', label: '租户号', type: 'text', ro: true, hint: '由系统自动分配（8000 起），同时作为话机号码前缀；保存后回显' },
    { k: 'name', label: '租户名称', type: 'text', required: true },
    { k: 'rate', label: '费率(元/计费单位)', type: 'number', required: true, hint: '账户级默认费率（兜底层）；留空则无' },
    { k: 'balance', label: '余额(元)', type: 'number', ro: true },
    { k: 'credit_limit', label: '信用额度(元)', type: 'number', def: 0, hint: '允许透支上限，0=不允许透支' },
    { k: 'min_balance', label: '预留额度(元)', type: 'number', def: 0, hint: '呼叫前可用余额需 > 此值才放通' },
    { k: 'status', label: '状态', type: 'select', options: [{ v: 1, t: '启用' }, { v: 0, t: '停用' }] },
  ],
};
SECTIONS['nodes'] = { label: '节点状态', custom: 'nodes' };
function renderAccounts(key, st) {
  st = st || { page: 1, page_size: 50 };
  var c = document.getElementById('content');
  c.innerHTML = '<div class="section-head"><h2>租户</h2>' +
    '<button class="btn btn-primary btn-sm" id="acct-add">+ 新增</button></div>' +
    '<div id="acct_result"><div class="placeholder">加载中…</div></div>';
  document.getElementById('acct-add').onclick = function () { openForm('accounts', null); };
  api(SECTIONS['accounts'].list + '?page=' + st.page + '&page_size=' + st.page_size).then(function (data) {
    var rows = data.items || [];
    var head = '<tr><th>ID</th><th>租户号</th><th>名称</th><th>费率(元/单位)</th><th>余额(元)</th>' +
      '<th>信用额度(元)</th><th>预留额度(元)</th><th>状态</th><th class="sticky-right">操作</th></tr>';
    var body = rows.length ? rows.map(function (r) {
      var on = String(r.status) === '1';
      return '<tr><td>' + r.id + '</td><td>' + escapeAttr(r.account_number || '') + '</td><td>' + escapeAttr(r.name) + '</td>' +
        '<td>' + (r.rate !== null && r.rate !== undefined ? Number(r.rate).toFixed(4) : '—') + '</td>' +
        '<td>' + (r.balance !== null && r.balance !== undefined ? Number(r.balance).toFixed(4) : '0.0000') + '</td>' +
        '<td>' + (r.credit_limit !== null && r.credit_limit !== undefined ? Number(r.credit_limit).toFixed(4) : '0.0000') + '</td>' +
        '<td>' + (r.min_balance !== null && r.min_balance !== undefined ? Number(r.min_balance).toFixed(4) : '0.0000') + '</td>' +
        '<td>' + (on ? '启用' : '停用') + '</td>' +
        '<td class="sticky-right">' +
        '<button class="btn btn-sm" onclick="openForm(\'accounts\',' + r.id + ')">编辑</button>' +
        '<button class="btn btn-sm" onclick="acctRecharge(' + r.id + ')">充值</button>' +
        '<button class="btn btn-sm" onclick="acctLedger(' + r.id + ')">流水</button>' +
        '<button class="btn btn-sm btn-danger" onclick="delRow(\'accounts\',' + r.id + ')">删除</button>' +
        '</td></tr>';
    }).join('') : '<tr><td colspan="9" class="muted">暂无租户</td></tr>';
    document.getElementById('acct_result').innerHTML = '<div class="table-scroll"><table><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
    renderPager(key, data);
  }).catch(function (e) { document.getElementById('acct_result').innerHTML = '<div class="placeholder">加载失败：' + escapeAttr(e.message) + '</div>'; });
}
window.renderAccounts = renderAccounts;
function acctRecharge(id) {
  var amt = prompt('金额（元，正数=充值 / 负数=扣费）：');
  if (amt === null) return;
  amt = Number(amt);
  if (amt === 0 || isNaN(amt)) { toast('金额必须为非 0 数值（正数充值 / 负数扣费）', true); return; }
  var def = amt > 0 ? '充值' : '人工扣费';
  var remark = prompt('备注（可选）：', def) || def;
  api('/api/accounts/' + id + '/recharge', 'POST', { amount: amt, remark: remark }).then(function (r) {
    toast('操作成功，当前余额 ' + Number(r.balance).toFixed(4));
    showSection('accounts');
  }).catch(function (e) { toast('操作失败：' + e.message, true); });
}
window.acctRecharge = acctRecharge;
function fmtTime(s) {
  if (!s) return '';
  return String(s).replace('T', ' ').replace(/\.\d+$/, '');
}
window.fmtTime = fmtTime;
function renderLedgerRows(targetId, rows) {
  var typeText = { 1: '充值', 2: '通话扣费', 3: '人工调整', 4: '退款' };
  var body = rows.length ? rows.map(function (r) {
    return '<tr><td>' + fmtTime(r.created_at) + '</td><td>' + (typeText[r.type] || r.type) + '</td>' +
      '<td>' + (r.amount >= 0 ? '+' : '') + Number(r.amount).toFixed(4) + '</td>' +
      '<td>' + Number(r.balance_after).toFixed(4) + '</td>' +
      '<td>' + escapeAttr(r.remark || '') + '</td></tr>';
  }).join('') : '<tr><td colspan="5" class="muted">暂无流水</td></tr>';
  document.getElementById(targetId).innerHTML = '<table><thead><tr><th>时间</th><th>类型</th><th>金额(元)</th><th>余额(元)</th><th>备注</th></tr></thead><tbody>' + body + '</tbody></table>';
}
window.renderLedgerRows = renderLedgerRows;
function acctLedger(id) {
  var c = document.getElementById('content');
  c.innerHTML = '<div class="section-head"><h2>余额流水 · 账户 #' + id + '</h2>' +
    '<button class="btn btn-sm" onclick="showSection(\'accounts\')">返回</button></div>' +
    '<div class="cdr-filter" id="acct_ld_filter">' +
    '<select id="acct_ld_type" class="pager-input"><option value="">全部类型</option><option value="1">充值</option><option value="2">通话扣费</option><option value="3">人工调整</option><option value="4">退款</option></select>' +
    '<input id="acct_ld_from" type="date" placeholder="开始日期">' +
    '<input id="acct_ld_to" type="date" placeholder="结束日期">' +
    '<button class="btn btn-sm btn-primary" onclick="loadAcctLedger(' + id + ')">筛选</button>' +
    '</div>' +
    '<div id="acct_ld_result" class="table-scroll"><div class="placeholder">加载中…</div></div>';
  loadAcctLedger(id);
}
window.acctLedger = acctLedger;
function loadAcctLedger(id) {
  var type = document.getElementById('acct_ld_type').value;
  var from = document.getElementById('acct_ld_from').value;
  var to = document.getElementById('acct_ld_to').value;
  var qs = '?page_size=500';
  if (type) qs += '&type=' + encodeURIComponent(type);
  if (from) qs += '&from=' + encodeURIComponent(from);
  if (to) qs += '&to=' + encodeURIComponent(to);
  api('/api/accounts/' + id + '/ledger' + qs).then(function (data) {
    renderLedgerRows('acct_ld_result', data.items || []);
  }).catch(function (e) { document.getElementById('acct_ld_result').innerHTML = '<div class="placeholder">加载失败：' + escapeAttr(e.message) + '</div>'; });
}
window.loadAcctLedger = loadAcctLedger;
function renderCarriers(key, st) {
  st = st || { page: 1, page_size: 50 };
  var c = document.getElementById('content');
  c.innerHTML = '<div class="section-head"><h2>运营商</h2>' +
    '<button class="btn btn-primary btn-sm" id="cr-add">+ 新增</button></div>' +
    '<div id="cr_result"><div class="placeholder">加载中…</div></div>';
  document.getElementById('cr-add').onclick = function () { openForm('carriers', null); };
  api(SECTIONS['carriers'].list + '?page=' + st.page + '&page_size=' + st.page_size).then(function (data) {
    var rows = data.items || [];
    var head = '<tr><th>ID</th><th>名称</th><th>成本计费单位(秒)</th><th>成本费率(元/单位)</th><th>余额(元)</th><th>状态</th><th class="sticky-right">操作</th></tr>';
    var body = rows.length ? rows.map(function (r) {
      var on = String(r.status) === '1';
      return '<tr><td>' + r.id + '</td><td>' + escapeAttr(r.name) + '</td>' +
        '<td>' + (r.bill_unit !== null && r.bill_unit !== undefined ? r.bill_unit : '60') + '</td>' +
        '<td>' + (r.cost_rate !== null && r.cost_rate !== undefined ? Number(r.cost_rate).toFixed(4) : '—') + '</td>' +
        '<td>' + (r.balance !== null && r.balance !== undefined ? Number(r.balance).toFixed(4) : '0.0000') + '</td>' +
        '<td>' + (on ? '启用' : '停用') + '</td>' +
        '<td class="sticky-right">' +
        '<button class="btn btn-sm" onclick="openForm(\'carriers\',' + r.id + ')">编辑</button>' +
        '<button class="btn btn-sm" onclick="carrierRecharge(' + r.id + ')">充值</button>' +
        '<button class="btn btn-sm" onclick="carrierLedger(' + r.id + ')">流水</button>' +
        '<button class="btn btn-sm btn-danger" onclick="delRow(\'carriers\',' + r.id + ')">删除</button>' +
        '</td></tr>';
    }).join('') : '<tr><td colspan="7" class="muted">暂无运营商</td></tr>';
    document.getElementById('cr_result').innerHTML = '<div class="table-scroll"><table><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
    renderPager(key, data);
  }).catch(function (e) { document.getElementById('cr_result').innerHTML = '<div class="placeholder">加载失败：' + escapeAttr(e.message) + '</div>'; });
}
window.renderCarriers = renderCarriers;
function carrierRecharge(id) {
  var amt = prompt('金额（元，正数=充值 / 负数=扣费）：');
  if (amt === null) return;
  amt = Number(amt);
  if (amt === 0 || isNaN(amt)) { toast('金额必须为非 0 数值（正数充值 / 负数扣费）', true); return; }
  var def = amt > 0 ? '充值' : '人工扣费';
  var remark = prompt('备注（可选）：', def) || def;
  api('/api/carriers/' + id + '/recharge', 'POST', { amount: amt, remark: remark }).then(function (r) {
    toast('操作成功，当前余额 ' + Number(r.balance).toFixed(4));
    showSection('carriers');
  }).catch(function (e) { toast('操作失败：' + e.message, true); });
}
window.carrierRecharge = carrierRecharge;
function carrierLedger(id) {
  var c = document.getElementById('content');
  c.innerHTML = '<div class="section-head"><h2>余额流水 · 运营商 #' + id + '</h2>' +
    '<button class="btn btn-sm" onclick="showSection(\'carriers\')">返回</button></div>' +
    '<div class="cdr-filter" id="cr_ld_filter">' +
    '<select id="cr_ld_type" class="pager-input"><option value="">全部类型</option><option value="1">充值</option><option value="2">通话扣费</option><option value="3">人工调整</option><option value="4">退款</option></select>' +
    '<input id="cr_ld_from" type="date" placeholder="开始日期">' +
    '<input id="cr_ld_to" type="date" placeholder="结束日期">' +
    '<button class="btn btn-sm btn-primary" onclick="loadCarrierLedger(' + id + ')">筛选</button>' +
    '</div>' +
    '<div id="cr_ld_result" class="table-scroll"><div class="placeholder">加载中…</div></div>';
  loadCarrierLedger(id);
}
window.carrierLedger = carrierLedger;
function loadCarrierLedger(id) {
  var type = document.getElementById('cr_ld_type').value;
  var from = document.getElementById('cr_ld_from').value;
  var to = document.getElementById('cr_ld_to').value;
  var qs = '?page_size=500';
  if (type) qs += '&type=' + encodeURIComponent(type);
  if (from) qs += '&from=' + encodeURIComponent(from);
  if (to) qs += '&to=' + encodeURIComponent(to);
  api('/api/carriers/' + id + '/ledger' + qs).then(function (data) {
    renderLedgerRows('cr_ld_result', data.items || []);
  }).catch(function (e) { document.getElementById('cr_ld_result').innerHTML = '<div class="placeholder">加载失败：' + escapeAttr(e.message) + '</div>'; });
}
window.loadCarrierLedger = loadCarrierLedger;

const RULE_BLOCKS = [
  { key: 'caller_restrict', title: '主叫限制', direction: 1, act: null, kind: 'restrict' },
  { key: 'callee_restrict', title: '被叫限制', direction: 2, act: null, kind: 'restrict' },
  { key: 'caller_translate', title: '主叫变换', direction: 1, act: 3, kind: 'translate' },
  { key: 'callee_translate', title: '被叫变换', direction: 2, act: 3, kind: 'translate' },
];
let RULE_STATE = { caller_restrict: [], callee_restrict: [], caller_translate: [], callee_translate: [] };

const OPT_CACHE = {};
let CURRENT = null;
let FORM_CTX = null;

async function api(url, method, body) {
  method = method || 'GET';
  const opt = { method: method, headers: {} };
  if (body !== null) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (!r.ok) {
    if (r.status === 401) { showLogin(); }
    let msg = String(r.status);
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(r.status === 401 ? '请重新登录' : msg);
  }
  if (r.status === 204) return null;
  return await r.json();
}
function toast(msg, isErr) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast' + (isErr ? ' err' : '');
  setTimeout(function () { t.classList.add('hidden'); }, 2600);
}
async function loadOptions(field) {
  if (!field.src) return field.options || [];
  if (OPT_CACHE[field.src]) return OPT_CACHE[field.src];
  const data = await api(field.src + '?page_size=500');
  const rows = Array.isArray(data) ? data : (data.items || []);
  const opts = rows.map(function (r) {
    let t = r[field.optt];
    if (field.optt2 && r[field.optt2] != null && r[field.optt2] !== '') t = r[field.optt2] + ' · ' + (t != null ? t : '');
    return { v: r[field.optk], t: t };
  });
  OPT_CACHE[field.src] = opts;
  return opts;
}
// 任一数据变更（新增/编辑/删除）后清空选项缓存，避免跨模块引用 stale。
// 例：新增落地网关后，前缀路由列表的 gateway_id 经 OPT_CACHE 映射网关名，
// 若不失效则仍用旧列表 → 显示 id，须 F5 整页刷新才重新拉取。清缓存后
// 下次 showSection 的预加载会重新发请求拿到最新选项。
function invalidateOptCache() { for (const k in OPT_CACHE) delete OPT_CACHE[k]; }
function textOf(field, value) {
  if (field.fmt === 'time' || field.k === 'last_heartbeat_time') {
    if (value === null || value === undefined || value === '') return '<span class="muted">—</span>';
    return fmtBJ(value);
  }
  if (value === null || value === undefined || value === '') {
    return '<span class="muted">' + (field.emptyText || '—') + '</span>';
  }
  if (field.options) {
    const o = field.options.find(function (x) { return String(x.v) === String(value); });
    if (o) return o.t;
  }
  if (field.src && OPT_CACHE[field.src]) {
    const o = OPT_CACHE[field.src].find(function (x) { return String(x.v) === String(value); });
    if (o) return o.t;
  }
  return String(value);
}

function renderSidebar() {
  const nav = document.getElementById('sidebar');
  nav.innerHTML = '';
  Object.keys(SECTIONS).forEach(function (key) {
    const a = document.createElement('a');
    a.textContent = SECTIONS[key].label;
    a.onclick = function () { showSection(key); };
    a.dataset.key = key;
    nav.appendChild(a);
  });
}
async function showSection(key) {
  if (window._nodesTimer) { clearInterval(window._nodesTimer); window._nodesTimer = null; }
  CURRENT = key;
  document.querySelectorAll('#sidebar a').forEach(function (a) {
    a.classList.toggle('active', a.dataset.key === key);
  });
  const scEl = document.getElementById('content'); if (scEl) scEl.scrollTop = 0;
  await Promise.all(Object.values(SECTIONS).map(function (s) {
    return (s.fields || []).filter(function (f) { return f.src; }).map(loadOptions);
  }));
  const sec = SECTIONS[key];
  if (!sec) { toast('未知模块: ' + key, true); return; }
  window.PAGE_STATE = window.PAGE_STATE || {}; const st = window.PAGE_STATE[key] = Object.assign({ page: 1, page_size: 50 }, window.PAGE_STATE[key] || {});
  if (sec.custom === 'cdr') { renderCdr(key, st); return; }
  if (sec.custom === 'billing') { renderBilling(key, st); return; }
  if (sec.custom === 'accounts') { renderAccounts(key, st); return; }
  if (sec.custom === 'carriers') { renderCarriers(key, st); return; }
  if (sec.custom === 'nodes') { renderNodes(key, st); return; }
  let fq = '';
  if (sec.filters) fq = filterQs(st.filters || {});
  const data = await api(sec.list + '?page=' + st.page + '&page_size=' + st.page_size + fq);
  if (sec.topbar) { try { window._sysConfig = await api('/api/sys-config'); } catch (e) {} }
  const rows = data.items || [];
  if (key === 'gateways') {
    try {
      const pr = await api('/api/prefix-routes?page_size=500');
      const items = Array.isArray(pr) ? pr : (pr.items || []);
      const byGw = {};
      items.forEach(function (x) { (byGw[x.gateway_id] = byGw[x.gateway_id] || []).push(x.prefix); });
      rows.forEach(function (row) { row._prefixes = byGw[row.id] || []; });
    } catch (e) {}
  }
  renderTable(key, rows, st);
  renderPager(key, data);
}
function renderTable(key, rows, st) {
  const sec = SECTIONS[key];
  if (!sec) { toast('未知模块: ' + key, true); return; }
  const c = document.getElementById('content');
  let html = '<div class="section-head"><h2>' + sec.label + '</h2>' +
    '<button class="btn btn-primary btn-sm" id="add-btn">+ 新增</button></div>';
  if (sec.filters) html += filterBarHtml(key, sec, st || {});
  if (sec.topbar) html += topBarHtml(key, sec);
  html += '<div class="table-scroll">';
  if (!rows.length) {
    html += '<div class="placeholder">暂无数据。<button class="btn btn-sm" onclick="showSection(\'' + key + '\')">刷新</button></div>';
  } else {
    html += '<table><thead><tr>';
    if (sec.showId) html += '<th>ID</th>';
    sec.fields.forEach(function (f) { if (!f.noList) html += '<th>' + f.label + '</th>'; });
    (sec.specials || []).forEach(function (sp) {
      if (sp === 'prefixes') html += '<th>路由前缀</th>';
      else if (sp === 'ap-policy') html += '<th>允许/禁止网关</th>';
      else if (sp === 'ap-rules' || sp === 'gw-rules') html += '<th>规则</th>';
    });
    html += '<th class="sticky-right">操作</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>';
      if (sec.showId) html += '<td>' + (r.id !== undefined && r.id !== null ? r.id : '') + '</td>';
      sec.fields.forEach(function (f) {
        if (f.noList) return;
        const v = r[f.listKey || f.k];
        if (f.badgeMap) {
          const cls = f.badgeMap[String(v)];
          const txt = textOf(f, v);
          html += '<td>' + (cls ? '<span class="badge ' + cls + '">' + txt + '</span>' : txt) + '</td>';
          return;
        }
        html += '<td>' + textOf(f, v) + '</td>';
      });
      (sec.specials || []).forEach(function (sp) {
        if (sp === 'prefixes') {
          const pfx = (r._prefixes && r._prefixes.length) ? r._prefixes.join(',') : '';
          html += '<td><input class="pf-input" value="' + escapeAttr(pfx) + '" onchange="savePrefixesInline(\'' + key + '\',' + r.id + ',this)"></td>';
        } else if (sp === 'ap-policy' || sp === 'ap-rules' || sp === 'gw-rules') html += '<td class="muted">…</td>';
      });
      html += '<td class="sticky-right">' +
        (function () {
          var tf = sec.toggleField;
          if (tf === undefined) return '';
          var on = String(r[tf]) === '1';
          return '<button class="btn btn-sm" onclick="toggleRow(\'' + key + '\',' + r.id + ',\'' + tf + '\',' + (on ? 0 : 1) + ')">' + (on ? '停用' : '启用') + '</button>';
        }()) +
        '<button class="btn btn-sm" onclick="openForm(\'' + key + '\', ' + r.id + ')">编辑</button>' +
        '<button class="btn btn-sm btn-danger" onclick="delRow(\'' + key + '\', ' + r.id + ')">删除</button>' +
        '</td></tr>';
    });
    html += '</tbody></table>';
  }
  html += '</div>';
  c.innerHTML = html;
  document.getElementById('add-btn').onclick = function () { openForm(key, null); };
  if (sec.filters) bindFilterBar(key, sec);
  if (sec.topbar) bindTopBar(key, sec);
}
window.showSection = showSection;

async function delRow(key, id) {
  if (!confirm('确认删除？')) return;
  try { const sec = SECTIONS[key];
  if (!sec) { toast('未知模块: ' + key, true); return; }
  await api(sec.list + '/' + id, 'DELETE'); toast('已删除'); invalidateOptCache(); showSection(key); }
  catch (e) { toast('删除失败：' + e.message, true); }
}
window.delRow = delRow;

// 2026-09-03：列表行内快捷启用/停用（编辑按钮左侧）。依据当前值显示目标动作：
// 启用(1)状态显示「停用」→ 置 0；停用(0)状态显示「启用」→ 置 1。
async function toggleRow(key, id, field, next) {
  const sec = SECTIONS[key]; if (!sec) { toast('未知模块: ' + key, true); return; }
  const act = next === 1 ? '启用' : '停用';
  if (!confirm('确认' + act + '？')) return;
  try { const body = {}; body[field] = next;
    await api(sec.list + '/' + id, 'PUT', body); toast('已' + act); showSection(key); }
  catch (e) { toast(act + '失败：' + e.message, true); }
}
window.toggleRow = toggleRow;

function syncRuleInputs() {
  document.querySelectorAll('#f_ruleblocks [data-rk]').forEach(function (el) {
    const rk = el.dataset.rk, ri = +el.dataset.ri, f = el.dataset.f;
    if (RULE_STATE[rk] && RULE_STATE[rk][ri] !== undefined) RULE_STATE[rk][ri][f] = el.value;
  });
}
function blockHtml(block) {
  const rows = RULE_STATE[block.key];
  let rowsHtml = rows.map(function (row, i) {
    if (block.kind === 'restrict') {
      return '<div class="rule-row">' +
        '<select data-rk="' + block.key + '" data-ri="' + i + '" data-f="act">' +
          '<option value="1" ' + (row.act == 1 ? 'selected' : '') + '>允许</option>' +
          '<option value="2" ' + (row.act == 2 ? 'selected' : '') + '>拒绝</option>' +
        '</select>' +
        '<input data-rk="' + block.key + '" data-ri="' + i + '" data-f="pattern" value="' + (row.pattern || '') + '" placeholder="匹配(支持 * ?)">' +
        '<button type="button" class="btn btn-sm" onclick="delRuleRow(\'' + block.key + '\', ' + i + ')">×</button>' +
      '</div>';
    }
    return '<div class="rule-row">' +
      '<input data-rk="' + block.key + '" data-ri="' + i + '" data-f="pattern" value="' + (row.pattern || '') + '" placeholder="改写前(必填)">' +
      '<input data-rk="' + block.key + '" data-ri="' + i + '" data-f="replace_to" value="' + (row.replace_to || '') + '" placeholder="改写后(可空=删前缀)">' +
      '<button type="button" class="btn btn-sm" onclick="delRuleRow(\'' + block.key + '\', ' + i + ')">×</button>' +
    '</div>';
  }).join('');
  return '<div class="rule-block">' +
    '<div class="rule-block-head">' + block.title + ' <button type="button" class="btn btn-sm" onclick="addRuleRow(\'' + block.key + '\')">+ 添加</button></div>' +
    rowsHtml +
    '<div class="hint">' + (block.kind === 'restrict' ? '默认：允许所有（未配置即放行）；可多条' : '多条时按最长匹配(pattern 最长)生效；改写前必填，改写后可空') + '</div>' +
  '</div>';
}
function renderRuleBlocks() {
  const c = document.getElementById('f_ruleblocks');
  c.innerHTML = RULE_BLOCKS.map(blockHtml).join('');
}
window.addRuleRow = function (key) {
  syncRuleInputs();
  const b = RULE_BLOCKS.find(function (x) { return x.key === key; });
  RULE_STATE[key].push(b.kind === 'restrict' ? { act: 1, pattern: '' } : { pattern: '', replace_to: '' });
  renderRuleBlocks();
};
window.delRuleRow = function (key, i) {
  syncRuleInputs();
  RULE_STATE[key].splice(i, 1);
  renderRuleBlocks();
};

async function openForm(key, id) {
  const sec = SECTIONS[key];
  if (!sec) { toast('未知模块: ' + key, true); return; }
  const isEdit = id !== null;
  let data = {};
  if (isEdit) data = await api(sec.list + '/' + id);
  FORM_CTX = { key: key, id: id, specials: sec.specials || [] };
  const body = document.getElementById('modal-body');
  body.innerHTML = '';
  for (const f of sec.fields) {
    const opts = (f.type === 'select-src') ? await loadOptions(f) : (f.options || []);
    // select-src 默认值：优先数据值 → 显式 def → options 首项（真实存在的 id）。
    // 硬编码 def（如 def:1）会指向已删除的行 → 外键 1452 → 400；动态取首项可根治。
    let val = (data[f.k] !== undefined && data[f.k] !== null) ? data[f.k]
      : (f.def !== undefined ? f.def : ((f.type === 'select-src' && opts.length) ? opts[0].v : ''));
    // select-src 无可用选项时（如尚未创建运营商），提示用户先建基础数据，避免保存后才发现 400
    if (f.type === 'select-src' && !opts.length && !f.ro) {
      console.warn('[admin] select-src 无可用选项: ' + f.k + ' src=' + f.src);
    }
    const wrap = document.createElement('div');
    wrap.className = 'field';
    let control = '';
    if (f.ro) {
      var disp = val;
      if (f.fmt === 'time' || f.k === 'last_heartbeat_time') disp = val ? fmtBJ(val) : '—';
      else if (f.options) { var _o = f.options.find(function (x) { return String(x.v) === String(val); }); if (_o) disp = _o.t; }
      // 自动分配字段（如租户号）新建时为空，给出回填占位提示，避免用户误以为保存会失败
      if (!disp && f.k === 'account_number') disp = '（保存后自动生成）';
      control = '<input id="f_' + f.k + '" type="text" value="' + disp + '" readonly>';
    } else if (f.type === 'select' || f.type === 'select-src') {
      control = '<select id="f_' + f.k + '">' +
        '<option value="">（默认）</option>' +
        opts.map(function (o) { return '<option value="' + o.v + '" ' + (String(o.v) === String(val) ? 'selected' : '') + '>' + o.t + '</option>'; }).join('') +
        '</select>';
    } else if (f.type === 'textarea') {
      control = '<textarea id="f_' + f.k + '">' + val + '</textarea>';
    } else {
      const tp = f.type === 'number' ? 'number' : (f.type === 'password' ? 'password' : 'text');
      control = '<input id="f_' + f.k + '" type="' + tp + '" value="' + val + '">';
    }
    wrap.innerHTML = '<label>' + f.label + (f.required ? ' *' : '') + '</label>' + control +
      (f.hint ? '<div class="hint">' + f.hint + '</div>' : '');
    body.appendChild(wrap);
  }
  for (const sp of FORM_CTX.specials) {
    if (sp === 'ap-policy') {
      const gws = await loadOptions({ src: '/api/gateways', optk: 'id', optt: 'name' });
      const pols = isEdit ? await api('/api/access-points/' + id + '/gateway-policies') : [];
      const allow = pols.filter(function (p) { return p.policy === 1; }).map(function (p) { return String(p.gateway_id); });
      const deny = pols.filter(function (p) { return p.policy === 2; }).map(function (p) { return String(p.gateway_id); });
      const optsHtml = gws.map(function (g) { return '<tr data-gw="' + g.v + '"><td>' + g.t + '</td><td><label style="margin-right:14px"><input type="checkbox" class="pg-allow">允许</label><label><input type="checkbox" class="pg-deny">禁止</label></td></tr>'; }).join('');
      const w = document.createElement('div'); w.className = 'field';
      w.innerHTML = '<label>落地网关策略（允许/禁止，不勾=不限）</label><table><thead><tr><th>落地网关</th><th>操作</th></tr></thead><tbody id="f_policy_grid">' + optsHtml + '</tbody></table>';
      body.appendChild(w);
      document.querySelectorAll('#f_policy_grid tr[data-gw]').forEach(function (row) {
        const gw = row.dataset.gw;
        const a = row.querySelector('.pg-allow'), d = row.querySelector('.pg-deny');
        a.checked = allow.indexOf(gw) >= 0; d.checked = deny.indexOf(gw) >= 0;
        a.onchange = function () { if (a.checked) d.checked = false; };
        d.onchange = function () { if (d.checked) a.checked = false; };
      });
    } else if (sp === 'ap-rules' || sp === 'gw-rules') {
      const rl = isEdit ? await api('/api/rules?owner_type=' + sec.owner_type + '&owner_id=' + id + '&page_size=500') : { items: [] };
      const existing = Array.isArray(rl) ? rl : (rl.items || []);
      RULE_STATE = { caller_restrict: [], callee_restrict: [], caller_translate: [], callee_translate: [] };
      existing.forEach(function (r) {
        if (r.direction == 1 && r.act != 3) RULE_STATE.caller_restrict.push({ act: r.act, pattern: r.pattern });
        else if (r.direction == 2 && r.act != 3) RULE_STATE.callee_restrict.push({ act: r.act, pattern: r.pattern });
        else if (r.direction == 1 && r.act == 3) RULE_STATE.caller_translate.push({ pattern: r.pattern, replace_to: r.replace_to || '' });
        else if (r.direction == 2 && r.act == 3) RULE_STATE.callee_translate.push({ pattern: r.pattern, replace_to: r.replace_to || '' });
      });
      const w = document.createElement('div'); w.className = 'field';
      w.innerHTML = '<label>主被叫限制 / 变换（需求 #2，四个配置项）</label><div id="f_ruleblocks"></div>';
      body.appendChild(w);
      renderRuleBlocks();
    }
  }
  if (key === 'rules') {
    const otEl = document.getElementById('f_owner_type');
    const actEl = document.getElementById('f_act');
    const sync = function () {
      const opt3 = [...actEl.options].find(function (o) { return o.value === '3'; });
      if (opt3) opt3.disabled = (otEl.value === '1');
    };
    otEl.onchange = sync; sync();
  }
  if (key === 'gateways') {
    // #64：归属节点仅在「注册」模式下需要且显示；点对点全量下发 → 隐藏并清空。
    var _atEl = document.getElementById('f_auth_type');
    var _ndEl = document.getElementById('f_node_uuid');
    if (_ndEl) {
      var _row = _ndEl.closest('.field');
      var _syncNode = function () {
        var isReg = _atEl && String(_atEl.value) === '1';
        _row.style.display = isReg ? '' : 'none';
        if (!isReg) _ndEl.value = '';
      };
      _syncNode();
      if (_atEl) _atEl.addEventListener('change', _syncNode);
    }
  }
  document.getElementById('modal-title').textContent = (isEdit ? '编辑' : '新增') + ' · ' + sec.label;
  document.getElementById('modal').classList.remove('hidden');
}
window.openForm = openForm;

// IP/域名合法性校验：IPv4 / IPv6 / 域名。规则与后端 crud._valid_host 保持一致，
// 前端先做即时反馈，后端仍会再校验一次（防绕过）。
// 注：纯数字点分串必须是合法 IPv4，否则视为残缺 IP（如 "111.22"）予以拒绝。
function isIPv4(s) {
  const p = String(s).split('.');
  if (p.length !== 4) return false;
  for (let i = 0; i < 4; i++) {
    if (!/^\d{1,3}$/.test(p[i])) return false;
    if (p[i].length > 1 && p[i][0] === '0') return false;
    if (Number(p[i]) > 255) return false;
  }
  return true;
}
function isIPv6(s) {
  s = String(s);
  if (s.indexOf(':') < 0) return false;
  // 至少两个冒号：把 "192.168.1.1:5060"（IPv4 带端口）挡在门外——
  // 端口应由 port 字段承载，与后端 ipaddress.IPv6Address 的判定保持一致。
  if ((s.match(/:/g) || []).length < 2) return false;
  if (s.split('::').length > 2) return false;
  // 各段 1-4 位十六进制；末段允许点分形式（IPv4-mapped，如 ::ffff:192.168.1.1）
  return /^[0-9A-Fa-f:]+(\.[0-9A-Fa-f:]+)*$/.test(s);
}
function validHost(h) {
  const s = String(h || '').trim();
  if (!s) return false;
  if (/^[0-9.]+$/.test(s)) return isIPv4(s);
  if (isIPv6(s)) return true;
  return /^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/.test(s);
}

function collectForm() {
  const sec = SECTIONS[FORM_CTX.key];
  const body = {};
  for (const f of sec.fields) {
    if (f.ro) continue;
    const el = document.getElementById('f_' + f.k);
    if (!el) continue;
    let v = el.value;
    // 数字框留空：默认提交 0；标注 emptyNull 的费率类字段提交 null（表示「本层未配置」，
    // 让费率链继续向下一级回落）。费率留空若被写成 0，会被当成有效费率 0 元而截断链路
    // （话机/接入点层「免费」→ 账户兜底费率永不生效）。
    if (f.type === 'number') v = v === '' ? (f.emptyNull ? null : 0) : Number(v);
    if (v === '' && f.type !== 'password') v = null;
    if (f.type === 'password' && v === '') continue;
    body[f.k] = v;
  }
  return body;
}

async function saveForm() {
  const ctx = FORM_CTX;
  const key = ctx.key, id = ctx.id, specials = ctx.specials;
  const sec = SECTIONS[key];
  if (!sec) { toast('未知模块: ' + key, true); return; }
  for (const f of sec.fields) {
    // 条件必填：requiredIf={k:'auth_mode',v:0} 表示 auth_mode 选 0(点对点) 时本字段必填。
    // 点对点靠来源 IP 识别接入点，留空则无法鉴权；注册模式靠用户名识别，IP 可留空。
    const need = f.required || (f.requiredIf && (function () {
      const t = document.getElementById('f_' + f.requiredIf.k);
      return t && String(t.value) === String(f.requiredIf.v);
    })());
    if (need) {
      const el = document.getElementById('f_' + f.k);
      if (!el || el.value === '') { toast('请填写：' + f.label, true); return; }
    }
    // IP/域名格式预校验：hostList=多值逗号分隔(接入点)，hostSingle=单值(落地网关)
    if (f.hostList || f.hostSingle) {
      const el = document.getElementById('f_' + f.k);
      if (el && el.value.trim()) {
        const parts = f.hostList ? el.value.split(',') : [el.value];
        const bad = parts.map(function (s) { return s.trim(); })
          .filter(function (s) { return s && !validHost(s); });
        if (bad.length) {
          toast(f.label + ' 格式不合法：' + bad.join('、') + '（应为 IPv4/IPv6/域名）', true);
          return;
        }
      }
    }
  }
  const body = collectForm();
  try {
    let saved;
    if (id === null) saved = await api(SECTIONS[key].list, 'POST', body);
    else saved = await api(SECTIONS[key].list + '/' + id, 'PUT', body);
    const savedId = saved && saved.id ? saved.id : id;
    if (specials.indexOf('ap-policy') >= 0) await syncApPolicy(savedId);
    if (specials.indexOf('ap-rules') >= 0 || specials.indexOf('gw-rules') >= 0) {
      syncRuleInputs();
      const desired = [];
      RULE_BLOCKS.forEach(function (b) {
        RULE_STATE[b.key].forEach(function (row) {
          if (!row.pattern) return;
          if (b.kind === 'restrict') desired.push({ direction: b.direction, act: Number(row.act), pattern: row.pattern, replace_to: null });
          else desired.push({ direction: b.direction, act: 3, pattern: row.pattern, replace_to: row.replace_to || null });
        });
      });
      await syncOwnerRules(sec.owner_type, savedId, desired);
    }
    toast('已保存');
    closeModal();
    invalidateOptCache();
    showSection(key);
  } catch (e) { toast('保存失败：' + e.message, true); }
}
document.getElementById('modal-save').onclick = saveForm;

async function syncPrefixes(gwId, text) {
  const desired = text.split(/[\n,]/).map(function (s) { return s.trim(); }).filter(Boolean);
  const curRes = await api('/api/prefix-routes?gateway_id=' + gwId + '&page_size=500');
  const current = Array.isArray(curRes) ? curRes : (curRes.items || []);
  const curMap = {}; current.forEach(function (r) { curMap[r.prefix] = r.id; });
  for (const p of desired) {
    if (!(p in curMap)) await api('/api/prefix-routes', 'POST', { gateway_id: gwId, prefix: p, priority: 0, status: 1 });
  }
  for (const r of current) {
    if (!desired.includes(r.prefix)) await api('/api/prefix-routes/' + r.id, 'DELETE');
  }
}
function escapeAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
// 列表内联编辑路由前缀（逗号分隔，失焦保存）。逻辑复用 saveForm 的 syncPrefixes。
async function savePrefixesInline(key, id, inputEl) {
  const text = inputEl.value;
  try { await syncPrefixes(id, text); toast('路由前缀已保存'); }
  catch (e) { toast('保存失败：' + e.message, true); }
}
window.savePrefixesInline = savePrefixesInline;
async function syncApPolicy(apId) {
  const allow = [];
  const deny = [];
  document.querySelectorAll('#f_policy_grid tr[data-gw]').forEach(function (row) {
    const gw = row.dataset.gw;
    if (row.querySelector('.pg-allow').checked) allow.push(gw);
    if (row.querySelector('.pg-deny').checked) deny.push(gw);
  });
    const existing = await api('/api/access-points/' + apId + '/gateway-policies');
  const cur = Array.isArray(existing) ? existing : [];
  const want = {}; allow.forEach(function (g) { want[g] = 1; }); deny.forEach(function (g) { want[g] = 2; });
  for (const p of cur) {
    if (!(String(p.gateway_id) in want)) await api('/api/access-points/' + apId + '/gateway-policies/' + p.gateway_id, 'DELETE');
  }
  for (const g in want) {
    await api('/api/access-points/' + apId + '/gateway-policies', 'POST', { gateway_id: Number(g), policy: want[g] });
  }
}
async function syncOwnerRules(ownerType, ownerId, desired) {
  const rl = await api('/api/rules?owner_type=' + ownerType + '&owner_id=' + ownerId + '&page_size=500');
  const existing = Array.isArray(rl) ? rl : (rl.items || []);
  for (const r of existing) {
    await api('/api/rules/' + r.id, 'DELETE');
  }
  for (const d of desired) {
    await api('/api/rules', 'POST', {
      owner_type: ownerType, owner_id: ownerId,
      direction: d.direction, act: d.act, pattern: d.pattern, replace_to: d.replace_to
    });
  }
}

function closeModal() { document.getElementById('modal').classList.add('hidden'); }
document.getElementById('modal-close').onclick = closeModal;
document.getElementById('modal-cancel').onclick = closeModal;

renderSidebar();
bootAuth();
function renderPager(key, env) {
  let bar = document.getElementById('pager');
  const CE = document['create' + 'Element'].bind(document);
  if (!bar) { bar = CE('div'); bar.id = 'pager'; bar.className = 'pager'; document.getElementById('content').appendChild(bar); }
  const pp = env.page || 1, tp = env.total_pages || 1, tot = env.total || 0, ps = env.page_size || 50;
  bar.innerHTML = '';
  const row = CE('div'); row.className = 'pager-row';
  row.appendChild(document.createTextNode('共 ' + tot + ' 条 | 第 ' + pp + ' / ' + tp + ' 页'));
  const bPrev = CE('button'); bPrev.className = 'btn btn-sm'; bPrev.id = 'pg-prev'; bPrev.textContent = '上一页'; if (pp <= 1) bPrev.disabled = true; bPrev.onclick = function () { goPage(key, pp - 1); }; row.appendChild(bPrev);
  const bNext = CE('button'); bNext.className = 'btn btn-sm'; bNext.id = 'pg-next'; bNext.textContent = '下一页'; if (pp >= tp) bNext.disabled = true; bNext.onclick = function () { goPage(key, pp + 1); }; row.appendChild(bNext);
  const lbl = CE('span'); lbl.textContent = '跳至'; row.appendChild(lbl);
  const jump = CE('input'); jump.id = 'pg-jump'; jump.className = 'pager-input'; jump.type = 'number'; jump.min = 1; jump.max = tp; jump.value = pp; jump.style.width = '64px'; row.appendChild(jump);
  const lbl2 = CE('span'); lbl2.textContent = '页'; row.appendChild(lbl2);
  const bGo = CE('button'); bGo.className = 'btn btn-sm'; bGo.id = 'pg-go'; bGo.textContent = '跳转'; bGo.onclick = function () { const v = parseInt(jump.value, 10); if (v >= 1) goPage(key, v); }; row.appendChild(bGo);
  const lbl3 = CE('span'); lbl3.textContent = '每页'; row.appendChild(lbl3);
  const sel = CE('select'); sel.className = 'pager-input'; sel.id = 'pg-size'; [20,50,100].forEach(function (n) { const o = CE('option'); o.value = n; o.textContent = n; if (n == ps) o.selected = true; sel.appendChild(o); }); sel.onchange = function () { sizePage(key, this.value); }; row.appendChild(sel);
  bar.appendChild(row);
}
window.renderPager = renderPager;
window.goPage = function (key, p) { window.PAGE_STATE = window.PAGE_STATE || {}; window.PAGE_STATE[key] = window.PAGE_STATE[key] || {}; window.PAGE_STATE[key].page = p; showSection(key); };
window.sizePage = function (key, n) { window.PAGE_STATE = window.PAGE_STATE || {}; window.PAGE_STATE[key] = window.PAGE_STATE[key] || {}; window.PAGE_STATE[key].page = 1; window.PAGE_STATE[key].page_size = parseInt(n, 10); showSection(key); };
var CDR_COLS = [
  {k:'id', t:'ID'}, {k:'uuid', t:'UUID'},
  {k:'customer_id', t:'客户'}, {k:'account_id', t:'账户'}, {k:'business_id', t:'业务'},
  {k:'access_point_id', t:'接入点'}, {k:'gateway_id', t:'落地网关'}, {k:'carrier_id', t:'运营商'}, {k:'source_ip', t:'来源IP'}, {k:'source_port', t:'来源端口'}, {k:'dest_ip', t:'落地IP'}, {k:'dest_port', t:'落地端口'},
  {k:'caller_in', t:'主叫(入)'}, {k:'callee_in', t:'被叫(入)'},
  {k:'caller_mid', t:'主叫(中)'}, {k:'callee_mid', t:'被叫(中)'},
  {k:'caller_out', t:'主叫(出)'}, {k:'callee_out', t:'被叫(出)'},
  {k:'start_time', t:'开始时间'}, {k:'ring_time', t:'振铃时间'}, {k:'answer_time', t:'应答时间'}, {k:'end_time', t:'结束时间'},
  {k:'talk_duration', t:'通话时长(秒)'}, {k:'bill_unit', t:'计费单位'}, {k:'bill_duration', t:'计费时长'},
  {k:'cost', t:'消费额(元)'}, {k:'rate_used', t:'适用费率(元/单位)'},
  {k:'cost_price', t:'成本(元)'}, {k:'cost_rate_used', t:'成本费率(元/单位)'}, {k:'cost_bill_unit', t:'成本计费单位'}, {k:'profit', t:'毛利(元)'},
  {k:'hangup_cause', t:'挂断原因'}, {k:'hangup_direction', t:'挂断方向'}, {k:'sip_code', t:'SIP码'}, {k:'sip_invite_failure_status', t:'邀请失败'},
  {k:'reject_reason', t:'拒绝原因'}, {k:'switch_count', t:'切换次数'}, {k:'switch_detail', t:'切换明细'},
  {k:'record_status', t:'录音状态'}, {k:'record_path', t:'录音路径'},
  {k:'fs_node_uuid', t:'FS节点'}, {k:'created_at', t:'创建时间'}
];
var CDR_DIR_TEXT = { 0: '服务器', 1: '主叫', 2: '被叫', 3: '其他' };
function fmtBJ(v){if(v===null||v===undefined||v==='')return v;var t=String(v).replace(' ','T');if(!/Z|[+-]\d\d:?\d\d$/.test(t))t+='Z';var d=new Date(t);if(isNaN(d.getTime()))return String(v);var b=new Date(d.getTime()+8*3600*1000);var p=function(n){return(n<10?'0':'')+n;};return b.getUTCFullYear()+'-'+p(b.getUTCMonth()+1)+'-'+p(b.getUTCDate())+' '+p(b.getUTCHours())+':'+p(b.getUTCMinutes())+':'+p(b.getUTCSeconds());}
var CDR_TIME_KEYS=['start_time','ring_time','answer_time','end_time','created_at'];
// 话单默认显示列：账户/接入点/落地网关/运营商按 id 存储、渲染时映射为名称（fmtCdrCell）。
var CDR_DEFAULT_COLS = ['uuid','caller_in','callee_in','account_id','access_point_id','gateway_id','carrier_id','start_time','end_time','talk_duration','cost','rate_used','cost_price','cost_rate_used','cost_bill_unit','profit','hangup_cause','hangup_direction','record_status','source_ip','source_port','dest_ip','dest_port'];
// 存量 localStorage.cdr_cols 强制补列（改 CDR_COLS 定义对老用户无效，须 push 回写才可见）。
var CDR_FORCE_PUSH = ['account_id','access_point_id','carrier_id','source_ip','source_port','dest_ip','dest_port','hangup_direction','cost','rate_used','cost_price','cost_rate_used','cost_bill_unit','profit'];

// 切换明细格式化：把 switch_detail(JSON 数组)渲染成「网关名(出局号) → 失败码」可读列表，
// gateway_id 经 /api/gateways 映射成网关名。原始值可能是字符串(JSON)或已解析数组。
var GW_MAP = null;
function ensureGwMap() {
  if (GW_MAP) return Promise.resolve(GW_MAP);
  return api('/api/gateways?page_size=500').then(function (d) {
    GW_MAP = {};
    (d.items || []).forEach(function (g) { GW_MAP[g.id] = g.name; });
    return GW_MAP;
  }).catch(function () { GW_MAP = {}; return GW_MAP; });
}
// ---- 话单 ID → 名称映射（账户/接入点/落地网关/运营商/业务/客户）----
// 各 id 列入库存原始 id，渲染时经映射表显示名称；映射失败回退 #id。
var CDR_ID_MAP = {};
var CDR_ID_MAPS_LOADED = false;
function ensureCdrIdMaps() {
  if (CDR_ID_MAPS_LOADED) return Promise.resolve();
  var jobs = [
    api('/api/accounts?page_size=500').then(function (d) {
      var m = {};
      (d.items || []).forEach(function (x) { m[x.id] = (x.account_number ? x.account_number + ' · ' : '') + (x.name || ('#' + x.id)); });
      CDR_ID_MAP.account_id = m;
    }),
    api('/api/access-points?page_size=500').then(function (d) {
      var m = {};
      (d.items || []).forEach(function (x) { m[x.id] = x.name || ('#' + x.id); });
      CDR_ID_MAP.access_point_id = m;
    }),
    api('/api/carriers?page_size=500').then(function (d) {
      var m = {};
      (d.items || []).forEach(function (x) { m[x.id] = x.name || ('#' + x.id); });
      CDR_ID_MAP.carrier_id = m;
    }),
    api('/api/businesses/options').then(function (d) {
      var m = {};
      (d || []).forEach(function (x) { m[x.id] = x.name || ('#' + x.id); });
      CDR_ID_MAP.business_id = m;
    }),
    api('/api/customers/options').then(function (d) {
      var m = {};
      (d || []).forEach(function (x) { m[x.id] = x.name || ('#' + x.id); });
      CDR_ID_MAP.customer_id = m;
    }),
    ensureGwMap()
  ];
  return Promise.all(jobs).then(function () {
    var m = {}; if (GW_MAP) for (var g in GW_MAP) m[g] = GW_MAP[g];
    CDR_ID_MAP.gateway_id = m;
    CDR_ID_MAPS_LOADED = true;
  }).catch(function () { CDR_ID_MAPS_LOADED = true; });
}
function cdrIdName(colKey, id) {
  if (id === null || id === undefined || id === '') return null;
  var m = CDR_ID_MAP[colKey];
  if (!m) return null;
  var nm = m[String(id)];
  return (nm !== undefined && nm !== null && nm !== '') ? nm : ('#' + id);
}
function fmtSwitchDetail(v) {
  if (v === null || v === undefined || v === '') return '<span class="muted">—</span>';
  var arr;
  try { arr = (typeof v === 'string') ? JSON.parse(v) : v; } catch (e) { arr = null; }
  if (!Array.isArray(arr) || !arr.length) {
    // 兼容历史裸串格式（如 ";3:reject:480"）
    var raw = (typeof v === 'string') ? v : JSON.stringify(v);
    return '<span class="muted">' + String(raw).replace(/</g, '&lt;') + '</span>';
  }
  var parts = arr.map(function (e, idx) {
    var gid = e.gateway_id;
    var gname = (GW_MAP && GW_MAP[gid]) ? GW_MAP[gid] : ('gw' + gid);
    var callee = e.callee_out || '';
    var cause = e.cause || e.sip_code || '';
    return (idx + 1) + '. ' + gname + (callee ? ('(' + callee + ')') : '') + (cause ? (' → ' + cause) : '');
  });
  return '<div style="white-space:nowrap">' + parts.join('<br>') + '</div>';
}
function fmtCdrCell(k, v) {
  if (k === 'switch_detail') return fmtSwitchDetail(v);
  if (k === 'hangup_direction') {
    if (v === null || v === undefined || v === '') return '<span class="muted">—</span>';
    return String(CDR_DIR_TEXT[v] !== undefined ? CDR_DIR_TEXT[v] : v).replace(/</g, '&lt;');
  }
  // 账户/接入点/落地网关/运营商/业务/客户：id → 名称
  var nm = cdrIdName(k, v);
  if (nm !== null) return String(nm).replace(/</g, '&lt;');
  if (v === null || v === undefined || v === '') return '<span class="muted">—</span>';
  return String(v).replace(/</g, '&lt;');
}

function renderCdr(key, st) {
  st = st || { page: 1, page_size: 50 };
  var flt = JSON.parse(localStorage.getItem('cdr_filter') || '{}');
  var shown = JSON.parse(localStorage.getItem('cdr_cols') || 'null');
  if (!shown) { shown = CDR_DEFAULT_COLS.slice(); localStorage.setItem('cdr_cols', JSON.stringify(shown)); }
  else { CDR_FORCE_PUSH.forEach(function(k){ if (shown.indexOf(k)<0) shown.push(k); }); localStorage.setItem('cdr_cols', JSON.stringify(shown)); }
  var c = document.getElementById('content');
  ensureCdrIdMaps().then(function () {
    // 落地网关筛选改下拉（表内网关已按名称展示）；其余筛选保留文本输入。
    var gwOpts = ['<option value="">落地网关(全部)</option>'];
    if (GW_MAP) Object.keys(GW_MAP).sort(function (a, b) { return a - b; }).forEach(function (gid) {
      gwOpts.push('<option value="' + gid + '"' + (String(flt.gateway_id) === String(gid) ? ' selected' : '') + '>' + escapeAttr(GW_MAP[gid]) + '</option>');
    });
    var filterHtml = '<div class="section-head"><h2>话单</h2></div>' +
      '<div class="cdr-filter">' +
      '<input id="cf_caller" placeholder="主叫(入)" value="' + (flt.caller||'') + '">' +
      '<input id="cf_callee" placeholder="被叫(入)" value="' + (flt.callee||'') + '">' +
      '<select id="cf_gw">' + gwOpts.join('') + '</select>' +
      '<input id="cf_cause" placeholder="挂断原因" value="' + (flt.hangup_cause||'') + '">' +
      '<button class="btn btn-sm btn-primary" id="cdr_search_btn">查询</button>' +
      '<button class="btn btn-sm" id="cdr_reset_btn">重置</button>' +
      '<button class="btn btn-sm" id="cdr_cols_btn">选择显示字段</button>' +
      '</div>';
    if (window._cdrColsOpen) {
      var picks = CDR_COLS.map(function(col){
        var ck = shown.indexOf(col.k) >= 0 ? 'checked' : '';
        return '<label style="margin-right:12px"><input type="checkbox" class="cdr-col" value="'+col.k+'" '+ck+'>'+col.t+'</label>';
      }).join('');
      filterHtml += '<div class="cdr-cols"><div style="font-weight:600;margin-right:12px;display:inline-block"><input type="checkbox" id="cdr_selall"' + (shown.length === CDR_COLS.length ? ' checked' : '') + '>全选/取消全选</div>'+picks+'<br><button class="btn btn-sm btn-primary" id="cdr_apply_btn">应用字段</button></div>';
    }
    c.innerHTML = filterHtml;
    document.getElementById('cdr_search_btn').onclick = cdrSearch;
    document.getElementById('cdr_reset_btn').onclick = cdrReset;
    document.getElementById('cdr_cols_btn').onclick = cdrToggleCols;
    if (window._cdrColsOpen) document.getElementById('cdr_apply_btn').onclick = cdrApplyCols;
    if (window._cdrColsOpen) { var sa=document.getElementById('cdr_selall'); if (sa) { sa.addEventListener('click', function(){ var ck=sa.checked; document.querySelectorAll('.cdr-col').forEach(function(el){ el.checked=ck; }); }); } }
    var qs = '?page=' + st.page + '&page_size=' + st.page_size;
    if (flt.caller) qs += '&caller=' + encodeURIComponent(flt.caller);
    if (flt.callee) qs += '&callee=' + encodeURIComponent(flt.callee);
    if (flt.gateway_id) qs += '&gateway_id=' + encodeURIComponent(flt.gateway_id);
    if (flt.hangup_cause) qs += '&hangup_cause=' + encodeURIComponent(flt.hangup_cause);
    api('/api/cdr' + qs).then(function(data){
      var rows = data.items || [];
      var head = '<tr>' + shown.map(function(k){ var col = CDR_COLS.find(function(x){return x.k===k;}); return '<th>'+(col?col.t:k)+'</th>'; }).join('') + '</tr>';
      var bodyRows = rows.map(function(r){
        return '<tr>' + shown.map(function(k){
          var v = r[k];
          if (CDR_TIME_KEYS.indexOf(k) >= 0 && v) v = fmtBJ(v);
          return '<td>'+fmtCdrCell(k, v)+'</td>'; }).join('') + '</tr>';
      }).join('');
      var table = rows.length ? '<table><thead>'+head+'</thead><tbody>'+bodyRows+'</tbody></table>' : '<div class="placeholder">暂无话单</div>';
      c.insertAdjacentHTML('beforeend', '<div class="cdr-table-wrap">' + table + '</div>');
      document.getElementById('cdr_search_btn').onclick = cdrSearch;
      document.getElementById('cdr_reset_btn').onclick = cdrReset;
      document.getElementById('cdr_cols_btn').onclick = cdrToggleCols;
      if (window._cdrColsOpen) document.getElementById('cdr_apply_btn').onclick = cdrApplyCols;
      renderPager(key, data);
    }).catch(function(e){ toast('话单查询失败：'+e.message, true); });
  });
}
window.renderCdr = renderCdr;
function cdrSearch() {
  var flt = {
    caller: document.getElementById('cf_caller').value.trim(),
    callee: document.getElementById('cf_callee').value.trim(),
    gateway_id: document.getElementById('cf_gw').value.trim(),
    hangup_cause: document.getElementById('cf_cause').value.trim()
  };
  localStorage.setItem('cdr_filter', JSON.stringify(flt));
  showSection('cdr');
}
window.cdrSearch = cdrSearch;
function cdrReset() {
  localStorage.removeItem('cdr_filter');
  showSection('cdr');
}
window.cdrReset = cdrReset;
function cdrToggleCols() { window._cdrColsOpen = !window._cdrColsOpen; renderCdr('cdr', window.PAGE_STATE['cdr']); }
window.cdrToggleCols = cdrToggleCols;
function cdrApplyCols() {
  var ks = []; document.querySelectorAll('.cdr-col').forEach(function(el){ if (el.checked) ks.push(el.value); });
  localStorage.setItem('cdr_cols', JSON.stringify(ks.length?ks:CDR_DEFAULT_COLS));
  window._cdrColsOpen = false; renderCdr('cdr', window.PAGE_STATE['cdr']);
}
window.cdrApplyCols = cdrApplyCols;


// ---- T-计费：计费报表 ----
function renderBilling(key, st) {
  st = st || {};
  var c = document.getElementById('content');
  var html = '<div class="section-head"><h2>计费报表</h2>' +
    '<button class="btn btn-sm btn-primary" id="bill_export_btn">导出 CSV</button></div>';
  html += '<div class="cdr-filter">' +
    '<select id="bf_dim" class="pager-input">' +
      '<option value="account">按账户</option>' +
      '<option value="access_point">按接入点</option>' +
      '<option value="gateway">按落地网关</option>' +
      '<option value="carrier">按运营商</option>' +
    '</select>' +
    '<input id="bf_from" type="date" placeholder="开始日期">' +
    '<input id="bf_to" type="date" placeholder="结束日期">' +
    '<button class="btn btn-sm btn-primary" id="bill_search_btn">查询</button>' +
    '</div>';
  html += '<div id="bill_result"></div>';
  c.innerHTML = html;
  document.getElementById('bill_search_btn').onclick = billSearch;
  document.getElementById('bill_export_btn').onclick = billExport;
  billSearch();
}
window.renderBilling = renderBilling;
function billSearch() {
  var dim = document.getElementById('bf_dim').value;
  var from = document.getElementById('bf_from').value;
  var to = document.getElementById('bf_to').value;
  var qs = '?dim=' + encodeURIComponent(dim);
  if (from) qs += '&from=' + encodeURIComponent(from);
  if (to) qs += '&to=' + encodeURIComponent(to);
  api('/api/billing/summary' + qs).then(function (data) {
    var rows = data.rows || [];
    var tot = data.total || {};
    var head = '<tr><th>维度ID</th><th>维度名称</th><th>通话数</th><th>计费时长(秒)</th><th>消费额(元)</th><th>成本(元)</th><th>毛利(元)</th><th>接通数</th><th>接通率(%)</th></tr>';
    var body = rows.map(function (r) {
      return '<tr><td>' + (r.dim === null ? '—' : r.dim) + '</td><td>' + escapeAttr(r.dim_name) + '</td><td>' + r.calls +
        '</td><td>' + r.bill_duration + '</td><td>' + Number(r.cost).toFixed(4) + '</td><td>' + Number(r.cost_price || 0).toFixed(4) +
        '</td><td>' + Number(r.profit || 0).toFixed(4) + '</td><td>' + r.answered + '</td><td>' + r.answer_rate + '</td></tr>';
    }).join('');
    body += '<tr style="font-weight:600;background:#f5f7fa"><td>—</td><td>合计</td><td>' + tot.calls + '</td><td>' + tot.bill_duration +
      '</td><td>' + Number(tot.cost || 0).toFixed(4) + '</td><td>' + Number(tot.cost_price || 0).toFixed(4) + '</td><td>' + Number(tot.profit || 0).toFixed(4) +
      '</td><td>' + tot.answered + '</td><td>' + tot.answer_rate + '</td></tr>';
    document.getElementById('bill_result').innerHTML = '<div class="table-scroll"><table><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>';
  }).catch(function (e) { toast('计费查询失败：' + e.message, true); });
}
window.billSearch = billSearch;
function billExport() {
  var dim = document.getElementById('bf_dim').value;
  var from = document.getElementById('bf_from').value;
  var to = document.getElementById('bf_to').value;
  var qs = '?dim=' + encodeURIComponent(dim);
  if (from) qs += '&from=' + encodeURIComponent(from);
  if (to) qs += '&to=' + encodeURIComponent(to);
  window.open('/api/billing/export' + qs, '_blank');
}
window.billExport = billExport;


function filterQs(filters) {
  let qs = '';
  Object.keys(filters || {}).forEach(function (k) {
    const v = filters[k];
    if (v === null || v === undefined || String(v).trim() === '') return;
    qs += '&' + k + '=' + encodeURIComponent(String(v).trim());
  });
  return qs;
}
function filterBarHtml(key, sec, st) {
  const flt = st.filters || {};
  let html = '<div class="filter-bar">';
  sec.filters.forEach(function (f) {
    const val = flt[f.k] !== undefined ? flt[f.k] : '';
    if (f.type === 'select') {
      html += '<select id="fb_' + key + '_' + f.k + '" class="pager-input">' +
        '<option value="">' + f.label + '(全部)</option>' +
        f.options.map(function (o) { return '<option value="' + o.v + '" ' + (String(o.v) === String(val) ? 'selected' : '') + '>' + o.t + '</option>'; }).join('') +
        '</select>';
    } else {
      html += '<input id="fb_' + key + '_' + f.k + '" class="pager-input" placeholder="' + f.label + '" value="' + val + '">';
    }
  });
  html += '<button class="btn btn-sm btn-primary" id="fb_' + key + '_go">查询</button>' +
    '<button class="btn btn-sm" id="fb_' + key + '_reset">重置</button></div>';
  return html;
}
function bindFilterBar(key, sec) {
  document.getElementById('fb_' + key + '_go').onclick = function () { applyFilter(key, sec); };
  document.getElementById('fb_' + key + '_reset').onclick = function () { resetFilter(key); };
}
function applyFilter(key, sec) {
  const st = window.PAGE_STATE[key] = window.PAGE_STATE[key] || {};
  const flt = {};
  sec.filters.forEach(function (f) {
    const el = document.getElementById('fb_' + key + '_' + f.k);
    const v = el ? el.value : '';
    if (String(v).trim() !== '') flt[f.k] = String(v).trim();
  });
  st.filters = flt; st.page = 1;
  showSection(key);
}
function resetFilter(key) {
  const st = window.PAGE_STATE[key] = window.PAGE_STATE[key] || {};
  delete st.filters; st.page = 1;
  showSection(key);
}
function topBarHtml(key, sec) {
  let html = '<div class="topbar">';
  sec.topbar.forEach(function (t) {
    const v = (window._sysConfig || {})[t.k] !== undefined ? (window._sysConfig || {})[t.k] : '';
    html += '<label class="topbar-label">' + t.label + '</label>' +
      '<input id="tb_' + key + '_' + t.k + '" class="pager-input" type="number" min="5" value="' + v + '" style="width:90px">';
  });
  html += '<button class="btn btn-sm btn-primary" id="tb_' + key + '_save">保存</button></div>';
  return html;
}
function bindTopBar(key, sec) {
  document.getElementById('tb_' + key + '_save').onclick = function () { saveTopBar(key, sec); };
}
function saveTopBar(key, sec) {
  const body = {};
  sec.topbar.forEach(function (t) {
    const el = document.getElementById('tb_' + key + '_' + t.k);
    body[t.k] = el ? Number(el.value) : null;
  });
  api('/api/sys-config', 'PUT', body).then(function (r) {
    window._sysConfig = r;
    toast('已保存');
  }).catch(function (e) { toast('保存失败：' + e.message, true); });
}

// ---- 节点状态 + Webhook 配置（#70 系列）----
function renderNodes(key, st) {
  st = st || {};
  const c = document.getElementById('content');
  c.innerHTML =
    '<div class="section-head"><h2>节点状态</h2>' +
    '<button class="btn btn-sm" id="nodes-refresh">刷新</button></div>' +
    // 显眼卡片：多节点下发同步（版本号 + 周期 + 一键全节点重建）
    '<div class="prov-card">' +
      '<div class="prov-head">' +
        '<div><div class="prov-title">多节点下发同步</div>' +
        '<div class="prov-sub">任一节点改动落地网关 → 所有 FS 节点自动重建（经 DB 版本号信令，无需节点间互通）</div></div>' +
        '<button class="btn btn-primary" id="pv_rescan">立即全节点重扫</button>' +
      '</div>' +
      '<div class="prov-stats">' +
        '<div class="prov-stat"><b id="pv_seq">—</b><span>下发版本号</span></div>' +
        '<div class="prov-stat"><b id="pv_wait">—</b><span>最近变更</span></div>' +
        '<div class="prov-stat"><b id="pv_iv">—</b><span>轮询周期(秒)</span></div>' +
      '</div>' +
      // 每节点自报的已同步位点：判断「是否真的都跟上了」只看这里
      '<div class="prov-nodes" id="pv_nodes"></div>' +
      // 最近一次变更涉及的网关名（历史名单，不等于"还没同步"）
      '<div class="prov-pending" id="pv_pending_list"></div>' +
      '<div class="prov-foot">' +
        '<label>同步轮询周期</label>' +
        '<input id="pv_interval" class="pager-input" type="number" min="5" style="width:90px">' +
        '<span class="muted" style="font-size:12px">秒</span>' +
        '<button class="btn btn-sm" id="pv_save">保存周期</button>' +
        '<span id="pv_msg" class="muted"></span>' +
      '</div>' +
      '<div class="hint">各节点处理完变更后会把「已同步位点」写回 DB，因此' +
      '<b>只要每个节点的位点 = 下发版本号，就说明全节点都已生效</b>；' +
      '位点落后或显示「未上报」表示该节点离线/进程异常，而非变更被丢弃。<br>' +
      '「立即全节点重扫」= 本节点立刻 killgw 全部并 rescan，其它节点最迟一个轮询周期后跟上；' +
      '用于改完网关想马上确认所有节点都生效时。</div>' +
    '</div>' +
    '<div id="nodes-table" class="placeholder">加载中…</div>' +
    '<div class="card" style="margin-top:18px">' +
    '<div class="section-head"><h3>Webhook 推送配置</h3>' +
    '<span class="muted" style="font-size:12px">落地网关心跳与 FS 节点心跳分开配置</span></div>' +
    '<div class="webhook-row"><label>落地网关心跳 webhook</label>' +
    '<input id="wh_gw" class="pager-input" style="flex:1" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...">' +
    '<button class="btn btn-sm" id="wh_gw_test">测试</button></div>' +
    '<div class="webhook-row"><label>FS 节点心跳 webhook</label>' +
    '<input id="wh_node" class="pager-input" style="flex:1" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...">' +
    '<button class="btn btn-sm" id="wh_node_test">测试</button></div>' +
    '<div style="margin-top:12px"><button class="btn btn-primary btn-sm" id="wh_save">保存配置</button>' +
    '<span id="wh_msg" class="muted" style="margin-left:10px"></span></div>' +
    '<div class="hint" style="margin-top:8px">地址留空 = 不推送。保存后立即生效，无需重启。</div></div>';

  api('/api/sys-config').then(function (cfg) {
    const gw = document.getElementById('wh_gw');
    const nd = document.getElementById('wh_node');
    if (gw) gw.value = (cfg && cfg.webhook_gateway_heartbeat_url) || '';
    if (nd) nd.value = (cfg && cfg.webhook_node_heartbeat_url) || '';
    renderProvisionCard(cfg || {});
  }).catch(function () {});

  document.getElementById('nodes-refresh').onclick = function () { loadNodes(); loadProvisionCard(); };
  document.getElementById('wh_save').onclick = saveWebhook;
  document.getElementById('wh_gw_test').onclick = function () { testWebhook('gateway'); };
  document.getElementById('wh_node_test').onclick = function () { testWebhook('node'); };
  document.getElementById('pv_save').onclick = saveProvisionInterval;
  document.getElementById('pv_rescan').onclick = doFullRescan;
  loadNodes();
  // 进入页面后自动刷新（15s），离开节点页时由 showSection 清除定时器
  window._nodesTimer = setInterval(function () {
    if (CURRENT === 'nodes') { loadNodes(); loadProvisionCard(); }
  }, 15000);
}

// ---- 多节点下发同步卡片 ----
let _provCfg = null;
let _nodeRows = null;

function renderProvisionCard(cfg) {
  const seq = document.getElementById('pv_seq');
  if (!seq) return;
  _provCfg = cfg || {};
  seq.textContent = cfg.provision_seq != null ? cfg.provision_seq : '—';
  let pending = [];
  try { pending = JSON.parse(cfg.provision_pending || '[]') || []; } catch (e) { pending = []; }
  const w = document.getElementById('pv_wait');
  w.textContent = pending.length;
  w.title = pending.length ? ('最近变更：' + pending.join(', ')) : '暂无变更记录';
  document.getElementById('pv_iv').textContent = cfg.provision_sync_interval || '5';
  const iv = document.getElementById('pv_interval');
  if (iv && document.activeElement !== iv) iv.value = cfg.provision_sync_interval || '5';

  const pl = document.getElementById('pv_pending_list');
  if (pl) {
    pl.innerHTML = pending.length
      ? '<span class="prov-tag-label">最近变更网关</span>' +
        pending.map(function (n) { return '<code class="prov-tag">' + n + '</code>'; }).join('')
      : '<span class="muted" style="font-size:12px">暂无网关变更记录（每次增删改落地网关都会记在这里，' +
        '它是历史名单、不代表"没同步"）</span>';
  }
  renderNodeSeen();
}

// 各节点已同步位点：把 /api/nodes 的节点和 sys-config 里的 provision_seen_<uuid> 对上
function renderNodeSeen() {
  const el = document.getElementById('pv_nodes');
  if (!el || !_nodeRows) return;
  const seq = (_provCfg && _provCfg.provision_seq != null) ? Number(_provCfg.provision_seq) : null;
  if (!_nodeRows.length) {
    el.innerHTML = '<span class="muted" style="font-size:12px">暂无节点</span>';
    return;
  }
  let h = '<span class="prov-tag-label">节点同步位点</span>';
  _nodeRows.forEach(function (r) {
    const uuid = r.node_uuid || '';
    const raw = _provCfg ? _provCfg['provision_seen_' + uuid] : undefined;
    const seen = (raw === undefined || raw === null || raw === '') ? null : Number(raw);
    let cls = 'badge-off', txt = '未上报';
    if (seen !== null && !isNaN(seen)) {
      if (seq !== null && seen < seq) {
        cls = 'badge-warn';
        txt = '已同步 seq ' + seen + '（落后 ' + (seq - seen) + '）';
      } else {
        cls = 'badge-on';
        txt = '已同步 seq ' + seen;
      }
    }
    h += '<span class="prov-node"><b>' + (r.host || r.name || uuid) + '</b>' +
      '<span class="badge ' + cls + '">' + txt + '</span></span>';
  });
  el.innerHTML = h;
}

function loadProvisionCard() {
  if (!document.getElementById('pv_seq')) return;
  api('/api/sys-config').then(function (cfg) { renderProvisionCard(cfg || {}); }).catch(function () {});
}

function saveProvisionInterval() {
  const el = document.getElementById('pv_interval');
  const msg = document.getElementById('pv_msg');
  const v = Number(el.value);
  if (!v || v < 5) { toast('轮询周期不得小于 5 秒', true); return; }
  api('/api/sys-config', 'PUT', { provision_sync_interval: v }).then(function () {
    msg.textContent = '已保存，下个周期生效';
    setTimeout(function () { msg.textContent = ''; }, 2500);
    loadProvisionCard();
  }).catch(function (e) { msg.textContent = '保存失败：' + e.message; });
}

function doFullRescan() {
  const btn = document.getElementById('pv_rescan');
  const msg = document.getElementById('pv_msg');
  btn.disabled = true;
  msg.textContent = '正在通知所有节点…';
  api('/api/provision/resync-all', 'POST', {}).then(function (r) {
    msg.textContent = '已触发（版本号 ' + (r && r.seq) + '）';
    toast('已触发全节点重扫');
    setTimeout(loadProvisionCard, 1500);
  }).catch(function (e) {
    msg.textContent = '触发失败：' + e.message;
    toast('触发失败：' + e.message, true);
  }).then(function () { btn.disabled = false; });
}

function loadNodes() {
  const el = document.getElementById('nodes-table');
  if (!el) return;
  api('/api/nodes').then(function (data) {
    const rows = (data && data.items) || [];
    _nodeRows = rows;
    renderNodeSeen();  // 卡片里的「节点同步位点」随节点列表一起刷新
    if (!rows.length) {
      el.innerHTML = '<div class="placeholder">暂无节点。节点由网关按 NODE_UUID 自动注册，稍候刷新。</div>';
      return;
    }
    const stMap = { 0: ['离线', 'badge-off'], 1: ['在线', 'badge-on'], 2: ['过载', 'badge-warn'] };
    let h = '<table><thead><tr><th>UUID</th><th>名称</th><th>地址</th><th>ESL端口</th>' +
      '<th>状态</th><th>并发</th><th>注册分机</th><th>连续失败</th><th>最后心跳</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      const sm = stMap[r.status] || ['未知', ''];
      h += '<tr><td>' + (r.node_uuid || '') + '</td>' +
        '<td>' + (r.name || '') + '</td>' +
        '<td>' + (r.host || '') + '</td>' +
        '<td>' + (r.esl_port != null ? r.esl_port : '') + '</td>' +
        '<td><span class="badge ' + sm[1] + '">' + sm[0] + '</span></td>' +
        '<td>' + (r.last_concurrency != null ? r.last_concurrency : '—') + '</td>' +
        '<td>' + (r.last_reg_count != null ? r.last_reg_count : '—') + '</td>' +
        '<td>' + (r.fail_count || 0) + '</td>' +
        '<td>' + (r.last_heartbeat_at ? fmtBJ(r.last_heartbeat_at) : '—') + '</td></tr>';
    });
    h += '</tbody></table>';
    el.innerHTML = h;
  }).catch(function (e) {
    el.innerHTML = '<div class="placeholder err">加载失败：' + e.message + '</div>';
  });
}

function saveWebhook() {
  const body = {
    webhook_gateway_heartbeat_url: document.getElementById('wh_gw').value.trim(),
    webhook_node_heartbeat_url: document.getElementById('wh_node').value.trim(),
  };
  api('/api/sys-config', 'PUT', body).then(function () {
    const m = document.getElementById('wh_msg');
    m.textContent = '已保存';
    setTimeout(function () { m.textContent = ''; }, 2500);
  }).catch(function (e) {
    document.getElementById('wh_msg').textContent = '保存失败：' + e.message;
  });
}

function testWebhook(channel) {
  const url = channel === 'gateway'
    ? document.getElementById('wh_gw').value.trim()
    : document.getElementById('wh_node').value.trim();
  if (!url) { toast('请先填写 ' + (channel === 'gateway' ? '落地网关' : '节点') + ' webhook 地址', true); return; }
  const body = channel === 'gateway' ? { gateway_url: url } : { node_url: url };
  api('/api/webhook-test', 'POST', body).then(function (r) {
    const res = ((r && r.results) || [])[0] || {};
    toast(res.ok ? ('推送成功：' + res.msg) : ('推送失败：' + res.msg), !res.ok);
  }).catch(function (e) { toast('测试失败：' + e.message, true); });
}

// ---- T-301 管理端登录态 ----
function ensureLoginOverlay() {
  if (document.getElementById('login-overlay')) return;
  const ov = document.createElement('div');
  ov.id = 'login-overlay';
  ov.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;';
  ov.innerHTML =
    '<div style="background:#fff;padding:26px 30px;border-radius:10px;width:320px;box-shadow:0 8px 30px rgba(0,0,0,.25);">' +
    '<div style="font-size:17px;font-weight:600;margin-bottom:16px;color:#1f2d3d;">管理控制台登录</div>' +
    '<label style="display:block;font-size:13px;color:#5b6b7b;margin-bottom:4px;">用户名</label>' +
    '<input id="login-user" class="pager-input" autocomplete="username" style="width:100%;margin-bottom:12px;">' +
    '<label style="display:block;font-size:13px;color:#5b6b7b;margin-bottom:4px;">密码</label>' +
    '<input id="login-pw" class="pager-input" type="password" autocomplete="current-password" style="width:100%;margin-bottom:14px;">' +
    '<button id="login-go" class="btn btn-primary" style="width:100%;">登录</button>' +
    '<div id="login-err" style="color:#e74c3c;font-size:13px;margin-top:10px;min-height:16px;"></div>' +
    '</div>';
  document.body.appendChild(ov);
  document.getElementById('login-go').onclick = doLogin;
  document.getElementById('login-pw').addEventListener('keydown', function (e) { if (e.key === 'Enter') doLogin(); });
}
function showLogin() {
  ensureLoginOverlay();
  const ov = document.getElementById('login-overlay');
  ov.style.display = 'flex';
  document.getElementById('logout-btn').style.display = 'none';
  document.getElementById('admin-user').textContent = '';
  const err = document.getElementById('login-err'); if (err) err.textContent = '';
  const u = document.getElementById('login-user'); if (u) u.focus();
}
function hideLogin() {
  const ov = document.getElementById('login-overlay'); if (ov) ov.style.display = 'none';
}
async function doLogin() {
  const u = document.getElementById('login-user').value.trim();
  const p = document.getElementById('login-pw').value;
  const err = document.getElementById('login-err');
  try {
    await api('/api/login', 'POST', { user: u, password: p });
    location.reload();
  } catch (e) {
    if (err) err.textContent = '登录失败：' + e.message;
  }
}
function doLogout() {
  api('/api/logout', 'POST').then(function () { location.reload(); }).catch(function () { location.reload(); });
}
function renderUser(user) {
  window._adminUser = user;
  document.getElementById('admin-user').textContent = '管理员：' + user;
  document.getElementById('logout-btn').style.display = '';
  hideLogin();
}
async function bootAuth() {
  try {
    const me = await api('/api/me');
    renderUser(me.user);
    showSection('access-points');
  } catch (e) {
    showLogin();
  }
}
window.doLogin = doLogin;
window.doLogout = doLogout;
