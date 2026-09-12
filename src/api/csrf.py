"""CSRF 纵深防御中间件（cookie 认证路径的同源校验）。

背景：管理端登录态走 HttpOnly cookie（SameSite=lax 已挡大部分跨站写），
但 lax 仍放行顶级导航的 GET 外带与部分浏览器差异；管理端全部变更操作
（增删改接入点/网关/路由/费率/充值）都是 cookie 认证的 POST/PUT/DELETE，
一旦被 CSRF 命中即直接影响生产话务。此中间件在 SameSite 之外加一道
**服务端同源校验**：跨源的写请求一律 403。

判定规则（保守取向，宁可多放行不可误伤运维通道）：
- 只管「写方法」POST/PUT/PATCH/DELETE；GET/HEAD/OPTIONS 全放行。
- 只管 cookie 认证面（path 以 /api/ 开头且非白名单）：
  - FS 回调 /fs/*、健康检查、login/logout、静态资源与本文件白名单中的
    网络路径不在管辖内（FS xml_curl 不带 Origin 头；login 未持 cookie）。
- 带 Authorization 头的请求豁免：这是显式凭据式 API 客户端（token/Basic），
    浏览器跨站攻击者拿不到也设不了这些头（custom header 本身即预检屏障）。
- Origin 头缺失时看 Referer；两者都缺 → 拒（现代浏览器跨站写必带其一）。
- Origin/Referer 的 host:port 与请求目标 Host 头一致（或经反代时与
  X-Forwarded-Host 一致）即视为同源放行。

失败响应 403 + 明确 detail，便于前端/脚本区分于 401。

挂载：app.py 一行 `app.middleware("http")(csrf_middleware)`（对齐 oplog 挂载模式，
遵守 app.py 冻结约定；判定逻辑全在本文件）。
"""
from urllib.parse import urlsplit

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 无登录态/非 cookie 面的路径：login 未持 cookie、logout 幂等清 cookie
_PATH_WHITELIST = {"/api/login", "/api/logout"}


def _host_of(url: str) -> str:
    """取 URL 的 authority（host[:port]）；相对 URL / 非法输入返回空。"""
    try:
        p = urlsplit(url if "//" in url else "//" + url)
        return (p.netloc or "").lower()
    except ValueError:
        return ""


def is_same_origin(request) -> bool:
    """判定写请求是否同源（Origin/Referer vs Host/X-Forwarded-Host）。"""
    for hdr in ("x-forwarded-host", "host"):
        target = (request.headers.get(hdr) or "").split(",")[0].strip().lower()
        if target:
            break
    else:
        return False
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    origin_host = _host_of(origin)
    if not origin_host:
        # 现代浏览器跨站写必带 Origin 或 Referer 之一；两者全缺按可疑处理
        return False
    if origin_host == target:
        return True
    # 反代场景：X-Forwarded-Host 可能带多值或与 Host 不同，任一匹配即放行
    forwarded = (request.headers.get("x-forwarded-host") or "").lower()
    return any(h.strip() == origin_host for h in forwarded.split(",") if h.strip())


async def csrf_middleware(request, call_next):
    """同源写校验（挂在 app.py，一行，勿展开；实现全在本文件）。"""
    path = request.url.path
    if (request.method in _WRITE_METHODS
            and path.startswith("/api/")
            and path not in _PATH_WHITELIST
            and not request.headers.get("authorization")):
        if not is_same_origin(request):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"detail": "cross-origin write rejected (CSRF guard)"},
                status_code=403)
    return await call_next(request)
