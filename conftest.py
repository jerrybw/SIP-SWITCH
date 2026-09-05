"""pytest 会话级准备：保证 core.config 在任意环境都能加载。

core.config 在 import 时急切读取 config_settings.yaml。本 conftest 在收集测试前：
- 若仓库根已有真实 config_settings.yaml（如部署服务器），不动它；
- 若没有（本地无 MySQL 环境），在系统临时目录写一个最小占位配置（mysql.url 指向不可达
  地址），并通过 GATEWAY_CONFIG 环境变量指向它，使纯测试可跑、DB 测试在无 MySQL 时优雅 skip。
占位文件写在 /tmp，不污染仓库，也不会被提交。
"""
import os
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent
_REAL = _ROOT / "config_settings.yaml"

if not os.environ.get("GATEWAY_CONFIG") and not _REAL.exists():
    fd, p = tempfile.mkstemp(suffix=".yaml", prefix="sipgw_smoke_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(
            "esl: {}\n"
            "api: {}\n"
            "mysql:\n"
            "  url: mysql+pymysql://u:p@127.0.0.1:3399/none\n"  # 不可达，触发 DB 测试 skip
            "  pool_recycle: 3600\n"
            "prepaid_enabled: false\n"
            "auth: {}\n"
        )
    os.environ["GATEWAY_CONFIG"] = p
