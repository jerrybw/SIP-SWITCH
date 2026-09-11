"""xml_curl 回调鉴权：/fs/* 端点的 HTTP Basic 共享凭据校验。

独立成模块（而非塞在 api/app.py）的原因：app.py 的导入链牵连 db.session 的
启动期自迁移，无 DB 环境无法导入；本模块只依赖 core.config，单测可独立运行。

凭据两侧同源：
- FS 侧：.env 的 XMLCURL_USER/XMLCURL_PASSWORD 经 docker-entrypoint-fs.sh 渲染进
  xml_curl.conf.xml 各 binding 的 gateway-credentials（libcurl CURLOPT_USERPWD）。
- 网关侧：config_settings.yaml [xml_curl] user/password（deploy.sh 自动生成同值）。
"""
import base64
import hmac
import logging

from core.config import settings

log = logging.getLogger("api.fs_auth")

_missing_warned = False

# 走 Basic 共享凭据的 /fs/* 端点（app.py 只按此表分流，校验逻辑全在本模块）
FS_BASIC_PATHS = ("/fs/dialplan", "/fs/directory", "/fs/config")


def fs_basic_auth_ok(request) -> bool:
    """校验 xml_curl 回调请求的 Basic 凭据。

    fail-closed：config [xml_curl] 缺 user/password 时一律拒绝 —— 与 auth._cfg
    「缺 jwt_secret 就 fail-fast」同一直觉：宁可让 FS 全收 401（网关日志有明确
    补配指引），也不能「以为有鉴权其实没配」地裸奔（历史版本 /fs/directory
    可被任意内网调用方拉走全部话机明文密码）。
    """
    global _missing_warned
    xc = settings.get("xml_curl") or {}
    user = (xc.get("user") or "").encode()
    password = (xc.get("password") or "").encode()
    if not user or not password:
        if not _missing_warned:
            _missing_warned = True
            log.error("[fs-auth] config_settings.yaml [xml_curl] 缺 user/password，/fs/* 全部拒绝。"
                      "请在 .env 配 XMLCURL_USER/XMLCURL_PASSWORD（deploy.sh 自动生成），"
                      "与 config_settings.yaml [xml_curl] 保持一致，然后重建 freeswitch + gateway")
        return False
    hdr = request.headers.get("authorization") or ""
    if not hdr.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(hdr[6:].strip()).decode("utf-8", "replace")
    except Exception:
        return False
    req_user, _, req_pass = raw.partition(":")
    return (hmac.compare_digest(req_user.encode(), user)
            and hmac.compare_digest(req_pass.encode(), password))
