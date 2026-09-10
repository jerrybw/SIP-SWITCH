"""第2类热加载配置的统一访问层。

第2类配置（如话机/接入点注册状态同步间隔等）存于 DB `system_setting`，
改后须立即生效，不应在 import / 启动期缓存到模块级变量。

契约（由 #68 建立）：
- 仅承载「改了要热生效、无需重启」的项。
- 读取必须走本模块；默认每次都查库（不缓存），保证热加载语义。
- 第1类（部署前 / `config_settings.yaml`）与第3类（启动快照，见 core/config.py）
  配置绝不放这里；业务配置（落地网关/路由/费率/接入点/账户）更不在此，全在 DB。
"""
from datetime import datetime

from db.session import SessionLocal
from db.models import SystemSetting
from sqlalchemy import select


def get_setting(key, default=None):
    """读取第2类配置项；DB 不可用时回落 default。"""
    db = None
    try:
        db = SessionLocal()
        row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
        if row is not None and row.value != "":
            return row.value
    except Exception:
        pass
    finally:
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
    return default


def get_int_setting(key, default=0):
    """以 int 读取第2类配置项；解析失败回落 default。"""
    v = get_setting(key, None)
    if v is None:
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def set_setting(key, value, description=None):
    """写入第2类配置项（upsert）；成功 True，失败 False。

    与 get_setting 对称：写后立即被所有节点的下一次读取看到（不缓存语义）。
    """
    db = None
    try:
        db = SessionLocal()
        row = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
        if row is None:
            row = SystemSetting(key=key, value=str(value), description=description)
            if hasattr(row, "updated_at"):
                row.updated_at = datetime.utcnow()
            db.add(row)
        else:
            row.value = str(value)
            if description is not None:
                row.description = description
            if hasattr(row, "updated_at"):
                row.updated_at = datetime.utcnow()
        db.commit()
        return True
    except Exception:
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
