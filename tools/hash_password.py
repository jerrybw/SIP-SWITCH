#!/usr/bin/env python3
"""管理员口令哈希生成 CLI（配合 api/auth.py 的版本化 PBKDF2 格式）。

用法：
  python tools/hash_password.py <明文口令>
  python tools/hash_password.py            # 交互式输入（不回显，不留 shell 历史）

输出（pbkdf2$<iterations>$<salt_hex>$<dk_hex>）写入 config_settings.yaml 的
auth.admin_password_hash 即完成换绑；旧 sha256(salt+password) 格式在登录时
仍被兼容（存量部署平滑升级），重置后建议尽快替换为新格式。
"""
import getpass
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from core.pw_hash import hash_password  # noqa: E402（纯标准库依赖，不牵连 FastAPI）


def main():
    if len(sys.argv) > 2:
        print(__doc__)
        raise SystemExit(2)
    if len(sys.argv) == 2:
        pw = sys.argv[1]
    else:
        pw = getpass.getpass("新管理员口令: ")
        if pw != getpass.getpass("确认口令: "):
            print("两次输入不一致", file=sys.stderr)
            raise SystemExit(1)
    if not pw:
        print("口令不能为空", file=sys.stderr)
        raise SystemExit(1)
    print(hash_password(pw))


if __name__ == "__main__":
    main()
