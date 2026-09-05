#!/usr/bin/env bash
# SIP Switch Gateway 安装助手（参考用，非强制）
# 用法（在仓库根目录执行）：sudo ./deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${APP_DIR}/venv"
PY="${VENV}/bin/python"

echo "[1/4] 创建虚拟环境 ${VENV} ..."
python3 -m venv "${VENV}"

echo "[2/4] 安装依赖 ..."
"${VENV}/bin/pip" install --upgrade pip
"${VENV}/bin/pip" install -r "${APP_DIR}/requirements.txt" || {
  echo "⚠️ requirements.txt 已排除 python-esl（不在 PyPI）。"
  echo "   请在已编译 FreeSWITCH 的机器上执行：pip install python-esl"
}

echo "[3/4] 准备配置（如不存在）..."
if [ ! -f "${APP_DIR}/config_settings.yaml" ]; then
  cp "${APP_DIR}/config.example.yaml" "${APP_DIR}/config_settings.yaml"
  echo "   已生成 config_settings.yaml，请编辑填入 esl/mysql/auth 真实值"
fi

echo "[4/4] 安装 systemd 单元 ..."
cp "${APP_DIR}/deploy/sip-gateway.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sip-gateway || echo "⚠️ 启动失败，请检查配置与日志 /var/log/sip-gateway.log"

echo "完成。管理后台: http://<host>:8000/admin"
