"""系统级配置的「schema 视图」+ 写白名单（需求①，2026-09-15）。

职责
----
管理端「系统健康配置」页要展示并修改系统级参数，且必须能区分
**可热加载（改完立即生效）** 与 **启动期（改配置文件 + 重启才生效）**。
本模块是这个判定与清单的**唯一真源** —— 前端不硬编码任何参数清单，
后端加参数前端自动出现（`GET /api/sys-config/schema`）。

判据：看「代码怎么读的」，不是「配置写在哪儿」
--------------------------------------------
- **热（hot）**：运行期经 `core.sys_setting.get_setting()/get_int_setting()` 读 DB
  `system_setting` 的键 —— 下一次读取即生效（不缓存）。
- **冷（cold）**：`core.config.settings`（`config_settings.yaml`）在 import 期加载一次的
  启动快照（core/config.py 注释里的「第3类」），改了**必须重启网关**。

⚠️ 2026-09-15 实测纠偏（保留教训）：
  `prepaid_enabled` / `default_sip_domain` / `concurrent_limit_global` 看起来像"运行参数"，
  但全仓**只经 `settings.get()` 读取** → 它们属于**冷**配置。
  早期把 `settings.get(...)` 与 `get_setting(...)` 混在一个 grep 里，得出的"9 个热键"
  把这三个也算成热的 —— 若照此实现，用户改完以为生效、实际纹丝不动
  （PITFALLS #53「死配置」同型缺陷）。
  `concurrent_limit_global` 的注释更直接写明了"**刻意不做热配**：运行中收紧要么形同虚设、
  要么得拆在途通话"（core/config.py:91-93）—— 它进 cold 是对齐设计意图，不是遗漏。

写白名单
--------
`PUT /api/sys-config` 只接受 `HOT_KEYS`。白名单外的键一律 **400 且整请求不落库**
（原子拒绝，不做部分写入）—— 防止"往 system_setting 里写了个没人读的键"这种
**不可见配置漂移**：写成功了、页面上没了、下次没人知道它是死是活。

另有一类键既不是热也不是冷，而是**系统自维护**（`INTERNAL_KEYS` /
`INTERNAL_KEY_PREFIXES`）：下发位点/变更版本号等，由代码自己写，用户改了没有任何意义。
它们也不在白名单里，但错误提示会**单独点明**，避免用户以为是漏配。
"""
import re

# ---------------------------------------------------------------------------
# 凭据 / 密文：**绝不出值**
# ---------------------------------------------------------------------------
MASKED_KEYS = (
    "esl.password",
    "redis.password",
    "xml_curl.password",
    "auth.jwt_secret",
    "auth.admin_password_hash",
    "auth.password_salt",
)

# 含内嵌口令的 URL：不整体隐藏（host/db 对排障有用），但必须剥掉口令段
SECRET_URL_KEYS = ("mysql.url",)

# ---------------------------------------------------------------------------
# 第2类热加载键（唯一真源）
# ---------------------------------------------------------------------------
# 每项：key / label / type(int|bool|string) / unit / hint / effect / default
#       / min / max（int 的取值范围；超范围 400，防"填 0 把轮询变成忙等"这类静默故障）
HOT_SPEC = (
    {
        "key": "phone_sync_interval", "label": "话机注册同步间隔", "type": "int",
        "unit": "秒", "default": 30, "min": 5,
        "hint": "后台巡检话机注册在线状态的周期（代码内下限 5s）",
        "effect": "立即生效",
    },
    {
        "key": "ap_sync_interval", "label": "接入点注册同步间隔", "type": "int",
        "unit": "秒", "default": 30, "min": 5,
        "hint": "巡检接入点注册状态的周期；实际周期取与话机同步间隔的较小值（下限 5s）",
        "effect": "立即生效",
    },
    {
        "key": "provision_sync_interval", "label": "多节点下发同步周期", "type": "int",
        "unit": "秒", "default": 5, "min": 1,
        "hint": "各节点轮询「网关下发变更」的周期；调大 = 多节点生效更慢，调 0 会变忙等（故下限 1s）",
        "effect": "立即生效",
    },
    {
        "key": "node_health_interval", "label": "FS 节点健康检查周期", "type": "int",
        "unit": "秒", "default": 30, "min": 1,
        "hint": "探测各 FS 节点在线状态与并发的周期",
        "effect": "立即生效",
    },
    {
        "key": "node_health_fail_threshold", "label": "节点连续失败判离线阈值", "type": "int",
        "unit": "次", "default": 3, "min": 1,
        "hint": "连续探测失败达到该次数才判离线（防抖，避免单次抖动误告警）",
        "effect": "立即生效",
    },
    {
        "key": "node_health_stale_threshold", "label": "节点心跳超时阈值", "type": "int",
        "unit": "秒", "default": 0, "min": 0,
        "hint": "心跳超时即判离线；0=自动（max(3×探测周期, 90s)）。显式值低于 3×探测周期会被钳到该下限",
        "effect": "立即生效",
    },
    {
        "key": "node_max_concurrency", "label": "节点并发上限（全局默认）", "type": "int",
        "unit": "条", "default": 0, "min": 0,
        "hint": "节点级并发上限的全局默认值；0=不限制。单节点 fs_node.max_concurrency 优先于此值",
        "effect": "立即生效",
    },
    {
        "key": "cdr_xml_enabled", "label": "XML CDR 真源开关", "type": "bool",
        "default": 1,
        "hint": "开=接收 mod_xml_cdr 上报的权威 XML CDR 补全话单；关则 /fs/cdr 直接回 503",
        "effect": "立即生效",
    },
    {
        "key": "cdr_xml_mode", "label": "XML CDR 处理模式", "type": "string",
        "default": "reconcile_only",
        "hint": "reconcile_only=只兜底不覆盖（默认）| override=以 XML 为准覆盖",
        "effect": "立即生效",
    },
    {
        "key": "webhook_gateway_heartbeat_url", "label": "落地网关心跳 Webhook", "type": "string",
        "default": "",
        "hint": "落地网关上下线告警推送地址；留空=不推送",
        "effect": "立即生效",
    },
    {
        "key": "webhook_node_heartbeat_url", "label": "FS 节点心跳 Webhook", "type": "string",
        "default": "",
        "hint": "FS 节点上下线/过载告警推送地址；留空=不推送",
        "effect": "立即生效",
    },
)
HOT_KEYS = frozenset(s["key"] for s in HOT_SPEC)
_SPEC_BY_KEY = {s["key"]: s for s in HOT_SPEC}

# 系统自维护键：由代码写入（下发位点/变更版本号），不是"可配置参数"
INTERNAL_KEYS = frozenset({"provision_seq", "provision_pending"})
INTERNAL_KEY_PREFIXES = ("provision_seen_",)


def is_internal_key(key: str) -> bool:
    """是否系统自维护键（用户不该手改，PUT 会拒并单独提示）。"""
    return key in INTERNAL_KEYS or (key or "").startswith(INTERNAL_KEY_PREFIXES)


# ---------------------------------------------------------------------------
# 冷配置：从已加载的 settings 反推清单（不硬编码，新增 yaml 键自动出现）
# ---------------------------------------------------------------------------
_MASKED_SET = frozenset(MASKED_KEYS)

SECTION_LABELS = {
    "esl": "FreeSWITCH ESL",
    "api": "管理端 API",
    "mysql": "MySQL",
    "node": "节点标识",
    "record": "录音",
    "redis": "Redis",
    "concurrency": "并发计数（P2-a）",
    "auth": "管理端鉴权",
}

# 展示名：未列出的键回落为键名本身（保证新增键也能正常显示）
COLD_LABELS = {
    "esl.host": "ESL 主机",
    "esl.port": "ESL 端口",
    "esl.reconnect_interval": "ESL 重连间隔",
    "esl.fs_node_uuid": "FS 节点 UUID（回落）",
    "api.host": "API 监听地址",
    "api.port": "API 监听端口",
    "mysql.url": "MySQL 连接串",
    "mysql.pool_recycle": "MySQL 连接回收",
    "default_sip_domain": "默认 SIP 域",
    "prepaid_enabled": "预付费开关",
    "auth.admin_user": "管理员账号",
    "auth.token_expire_minutes": "登录令牌有效期",
    "node.uuid": "本节点 UUID",
    "record.dir": "录音写入根（FS 视角）",
    "record.local_root": "录音读取根（网关视角）",
    "record.backend": "录音后端",
    "redis.enabled": "Redis 启用",
    "redis.host": "Redis 主机",
    "redis.port": "Redis 端口",
    "redis.db": "Redis 库号",
    "xml_curl.user": "xml_curl 共享账号",
    "concurrency.backend": "并发计数后端",
    "concurrency.fail_open": "Redis 不可用时放行",
    "concurrency.lease_ttl": "并发凭证 TTL",
    "concurrent_limit_global": "全局并发上限",
}

COLD_HINTS = {
    "esl.fs_node_uuid": "留空则按 node.uuid 回落",
    "default_sip_domain": "FS 目录请求未带 domain 时的兜底域",
    "prepaid_enabled": "开=余额不足 603 拒呼并按话单扣费；关=纯监控不计费",
    "record.dir": "写进拨号计划；改它必须同步改 compose 的录音卷挂载点",
    "record.local_root": "解析 local:// 时拼路径做回源；改它必须同步改录音卷挂载点",
    "record.backend": "local=本地卷（本期）| cos=对象存储（上云阶段）",
    "concurrency.backend": "redis=原子预留真源（默认）| local=本地事件驱动近似计数",
    "concurrency.fail_open": "仅 Redis 不可用时生效：关（默认）=拒绝新增呼叫",
    "concurrency.lease_ttl": "预留凭证 TTL，仅防极端泄漏积累",
    "concurrent_limit_global": "0=不限制。**刻意不做热配**：运行中收紧无法正确实现",
}
COLD_HINT_DEFAULT = "修改需改配置文件并重启"


def is_masked(key: str) -> bool:
    return key in _MASKED_SET


def mask_url_password(url) -> str:
    """剥掉 URL 里的口令段：`scheme://user:pwd@host` → `scheme://user:***@host`。

    覆盖 `user:pwd@` / `:pwd@` 两种形态；无口令或非字符串原样返回。
    """
    if not isinstance(url, str) or "@" not in url:
        return url if isinstance(url, str) else ""
    head, _, tail = url.rpartition("@")
    if "//" not in head:
        return "***@" + tail
    scheme, _, cred = head.partition("//")
    if ":" in cred:
        user, _, _pwd = cred.partition(":")
        return "%s//%s:***@%s" % (scheme, user, tail)
    return url


def _type_of(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    return "string"


def flatten(cfg, prefix: str = "") -> list:
    """把嵌套 dict 拍平成 [(dotted_key, value)]（保序，dict 插入序即 yaml 序）。"""
    out = []
    if not isinstance(cfg, dict):
        return out
    for k, v in cfg.items():
        key = "%s.%s" % (prefix, k) if prefix else str(k)
        if isinstance(v, dict):
            out.extend(flatten(v, key))
        else:
            out.append((key, v))
    return out


def build_cold(cfg) -> list:
    """冷配置清单（只读展示用）。**凭据键整体剔除**（它们只出现在 masked_keys）。

    `mysql.url` 保留但脱敏 —— host/db 对排障有用，口令不能出。
    """
    items = []
    for key, val in flatten(cfg):
        if is_masked(key):
            continue
        v = val
        if key in SECRET_URL_KEYS:
            v = mask_url_password(val)
        if v is None:
            v = ""
        items.append({
            "key": key,
            "label": COLD_LABELS.get(key) or key,
            "type": _type_of(val),
            "value": v,
            "hint": COLD_HINTS.get(key) or COLD_HINT_DEFAULT,
        })
    return items


def _coerce_hot(spec, raw):
    """把 DB 里的字符串值按 type 归一，供前端直接渲染（bool 出真布尔）。"""
    t = spec["type"]
    if raw is None or raw == "":
        raw = spec.get("default")
    if t == "int":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return spec.get("default", 0)
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        try:
            return bool(int(float(s)))
        except (TypeError, ValueError):
            return bool(spec.get("default", 0))
    return str(raw)


def build_hot(hot_values: dict) -> list:
    """热配置清单。hot_values = {key: DB 原值}（缺省回落 spec.default）。"""
    hot_values = hot_values or {}
    out = []
    for spec in HOT_SPEC:
        item = {
            "key": spec["key"],
            "label": spec["label"],
            "type": spec["type"],
            "value": _coerce_hot(spec, hot_values.get(spec["key"])),
            "hint": spec["hint"],
            "effect": spec["effect"],
        }
        if spec["type"] == "int":
            item["unit"] = spec.get("unit") or ""
            if spec.get("min") is not None:
                item["min"] = spec["min"]
            if spec.get("max") is not None:
                item["max"] = spec["max"]
        out.append(item)
    return out


def build_schema(cfg, hot_values: dict) -> dict:
    """`GET /api/sys-config/schema` 的响应体。"""
    return {
        "hot": build_hot(hot_values),
        "cold": build_cold(cfg),
        "masked_keys": list(MASKED_KEYS),
    }


# ---------------------------------------------------------------------------
# 写校验（PUT /api/sys-config）
# ---------------------------------------------------------------------------
_INT_RE = re.compile(r"^-?\d+$")


def normalize_write(data: dict):
    """校验并归一写入值。

    返回 `(accepted, errors)`：
    - accepted: {key: 归一后的字符串}（可直接 `set_setting`）
    - errors:   [(key, 原因)]，**非空即整请求拒绝**（不部分写入）

    归一要点：
    - **值为 `null` = 不修改该键**（沿用改造前的跳过语义；前端 `Number('')` 取不到元素时
      会发 null，若不跳过会被判成"必须是整数"而误报 400）。
    - `bool` 必须落成 `"1"/"0"` —— `get_int_setting()` 走 `int(float(v))`，
      写 `"True"` 会解析失败并静默回落默认值（改了等于没改），必须在此挡掉。
    """
    accepted, errors = {}, []
    for k, v in (data or {}).items():
        if v is None:
            continue
        if k not in HOT_KEYS:
            errors.append((k, "不是可热加载参数" if not is_internal_key(k)
                           else "系统自维护键，不可手工修改"))
            continue
        spec = _SPEC_BY_KEY[k]
        t = spec["type"]
        if t == "bool":
            s = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            accepted[k] = "1" if s else "0"
        elif t == "int":
            try:
                iv = int(v)
            except (TypeError, ValueError):
                errors.append((k, "必须是整数"))
                continue
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and iv < lo:
                errors.append((k, "不能小于 %s" % lo))
                continue
            if hi is not None and iv > hi:
                errors.append((k, "不能大于 %s" % hi))
                continue
            accepted[k] = str(iv)
        else:
            s = "" if v is None else str(v).strip()
            if s and k.startswith("webhook_") and not s.lower().startswith(("http://", "https://")):
                errors.append((k, "必须是 http(s) 地址或留空"))
                continue
            accepted[k] = s
    return accepted, errors
