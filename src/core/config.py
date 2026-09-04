"""Config loader: read config_settings.yaml -> settings dict."""
import os
import yaml

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CFG = os.environ.get("GATEWAY_CONFIG", os.path.join(_BASE, "config_settings.yaml"))


def _load():
    with open(_CFG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


settings = _load()
