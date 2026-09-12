"""版本化口令哈希（纯标准库，无框架依赖）。

供 api/auth.py（登录校验）与 tools/hash_password.py（CLI 生成）共用；
单独成模块的原因：CLI 工具不应为算一个哈希背上 FastAPI 整条 import 链。

格式：pbkdf2$<iterations>$<salt_hex>$<dk_hex>
- 独立随机盐（16 字节），iterations 记录在串内，未来调参不影响校验旧哈希。
- 登录端同时兼容历史 sha256(salt+password) 格式（存量部署平滑升级）。
"""
import hashlib
import hmac
import secrets

_ITERATIONS = 120_000
_DKLEN = 32


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    """生成版本化 PBKDF2 哈希。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, _DKLEN)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str, legacy_salt: str = "") -> bool:
    """按存储格式校验：pbkdf2$… 走 PBKDF2；其余（64 位 hex）视为遗留 sha256(legacy_salt+password)。

    legacy_salt 即 config 的 auth.password_salt（仅遗留格式需要）。两条路径
    都走 hmac.compare_digest（恒时序）。
    """
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, dk_hex = stored.split("$", 3)
            salt = bytes.fromhex(salt_hex)
            expect = bytes.fromhex(dk_hex)
            got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt,
                                      int(iters), len(expect))
            return hmac.compare_digest(got, expect)
        except (ValueError, IndexError):
            return False
    legacy = hashlib.sha256((legacy_salt + password).encode()).hexdigest()
    return hmac.compare_digest(legacy, stored)
