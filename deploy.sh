#!/usr/bin/env bash
# ============================================================================
# SIP-SWITCH 部署器（DEP-4 混合方案）
#   —— 部署前在宿主机运行，负责：探测对外 IP + 生成密钥 + 渲染配置；
#      DB 表结构迁移留给网关启动时自做（不在此处）。compose 拉起请用 dev-up.sh / docker compose up。
#
# 用法：
#   ./deploy.sh            # 首次：探测 IP + 生成密钥 + 渲染 .env / config_settings.yaml
#                         # 已存在真实配置：仅刷新 EXT_SIP_IP / default_sip_domain，跳过密钥生成
#   ./deploy.sh --force   # 强制重新生成全部密钥并重渲染（会令 mysql/fs/gateway 需重建，慎用）
#   ./deploy.sh --up      # 渲染完成后顺便 docker compose up -d
#   ./deploy.sh --force --up
#
# 幂等说明：密钥属于「一次性生成 + 分发」，绝不可让每个副本各自生成
#   （否则 jwt_secret / password_salt 不一致，副本间登录态立刻失效，见部署底座设计 §4.1）。
#   因此检测到已有真实配置时默认只刷新会变的 IP，不重生成密钥。
#
# Phase1 范围：仅渲染单机形态（现有 docker-compose.yml）。split / multi 节点形态为 Phase2。
# ============================================================================
set -euo pipefail

UP=0
FORCE=0
for a in "$@"; do
  case "$a" in
    --up) UP=1 ;;
    --force) FORCE=1 ;;
    *) echo "[deploy] 未知参数: $a" >&2; exit 1 ;;
  esac
done

cd "$(dirname "$0")"   # 切到仓库根

# ---------- 依赖检查 ----------
for b in docker python3 openssl; do
  command -v "$b" >/dev/null 2>&1 || { echo "[deploy] 缺少依赖: $b" >&2; exit 1; }
done

# ---------- 1) 探测对外 SIP IP ----------
probe_ip() {
  local ip=""
  ip=$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
  [ -z "$ip" ] && ip=$(ip route get 1 2>/dev/null | awk '{print $7; exit}')
  [ -z "$ip" ] && ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "$ip"
}
EXT_SIP_IP=$(probe_ip)
[ -z "$EXT_SIP_IP" ] && { echo "[deploy] ERROR: 探测不到对外 IP" >&2; exit 1; }
echo "[deploy] 对外 SIP IP = $EXT_SIP_IP"

CFG_EXAMPLE=config/docker/config.example.yaml
CFG_LIVE=config/docker/config_settings.yaml
ENV_EXAMPLE=.env.example
ENV_LIVE=.env

# ---------- 判断是否为已存在部署（含真实值则跳过密钥生成） ----------
existing_deploy=0
if [ -f "$CFG_LIVE" ] && ! grep -q 'change-me-esl' "$CFG_LIVE"; then
  existing_deploy=1
fi

if [ "$existing_deploy" -eq 1 ] && [ "$FORCE" -eq 0 ]; then
  echo "[deploy] 检测到已有部署（config_settings.yaml 含真实值），仅刷新随 IP 变化的项，跳过密钥生成。"
  echo "[deploy] 如需重新生成全部密钥并重建服务，请加 --force。"
  # 刷新 .env 的 EXT_SIP_IP
  if grep -q '^EXT_SIP_IP=' "$ENV_LIVE"; then
    sed -i "s|^EXT_SIP_IP=.*|EXT_SIP_IP=$EXT_SIP_IP|" "$ENV_LIVE"
  else
    printf '\n# 由 deploy.sh 自动探测注入\nEXT_SIP_IP=%s\n' "$EXT_SIP_IP" >> "$ENV_LIVE"
  fi
  # 刷新 config 的 default_sip_domain（本机 IP 变化时需同步）
  if grep -q '^default_sip_domain:' "$CFG_LIVE"; then
    sed -i "s|^default_sip_domain:.*|default_sip_domain: $EXT_SIP_IP|" "$CFG_LIVE"
  fi
  echo "[deploy] 已更新 EXT_SIP_IP=$EXT_SIP_IP (default_sip_domain 同步)。"
  [ "$UP" -eq 1 ] && docker compose --env-file .env up -d
  exit 0
fi

# ---------- 2) 生成密钥（仅一次；--force 或首次才会到这里） ----------
echo "[deploy] 生成部署密钥 ..."
gen_hex() { openssl rand -hex "${1:-16}"; }
MYSQL_ROOT_PW=$(gen_hex 16)
MYSQL_PW=$(gen_hex 16)
ESL_PW=$(gen_hex 16)
REDIS_PW=$(gen_hex 16)
JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(24))')
SALT=$(python3 -c 'import secrets;print(secrets.token_hex(24))')
ADMIN_PW=$(openssl rand -base64 16 | tr -dc 'A-Za-z0-9' | head -c 12)
ADMIN_HASH=$(python3 -c "import hashlib,sys; print(hashlib.sha256(('$SALT'+'$ADMIN_PW').encode()).hexdigest())")
NODE_UUID=$(python3 -c 'import secrets;print(secrets.token_hex(8))')

# ---------- 3) 渲染 .env（从 .env.example） ----------
echo "[deploy] 渲染 $ENV_LIVE ..."
python3 - "$ENV_EXAMPLE" "$ENV_LIVE" "$EXT_SIP_IP" "$MYSQL_ROOT_PW" "$MYSQL_PW" "$ESL_PW" "$REDIS_PW" <<'PY'
import sys, re
src, dst, ip, mr, mp, esl, rp = sys.argv[1:8]
t = open(src).read()
t = t.replace('change-me-root', mr)   # 先替换更具体的，避免误伤
t = t.replace('change-me-esl', esl)
t = t.replace('change-me', mp)
if 'EXT_SIP_IP=' not in t:
    t += '\n# 由 deploy.sh 自动探测注入\nEXT_SIP_IP=%s\n' % ip
else:
    t = re.sub(r'^EXT_SIP_IP=.*', 'EXT_SIP_IP=%s' % ip, t, flags=re.M)
if 'REDIS_PASSWORD=' not in t:
    t += 'REDIS_PASSWORD=%s\n' % rp
else:
    t = re.sub(r'^REDIS_PASSWORD=.*', 'REDIS_PASSWORD=%s' % rp, t, flags=re.M)
open(dst, 'w').write(t)
PY

# ---------- 4) 渲染 config_settings.yaml（从 config.example.yaml） ----------
echo "[deploy] 渲染 $CFG_LIVE ..."
python3 - "$CFG_EXAMPLE" "$CFG_LIVE" "$EXT_SIP_IP" "$MYSQL_PW" "$ESL_PW" "$REDIS_PW" "$JWT_SECRET" "$SALT" "$ADMIN_HASH" "$NODE_UUID" <<'PY'
import sys
src, dst, ip, mp, esl, rp, jwt, salt, ah, node = sys.argv[1:11]
t = open(src).read()
t = t.replace('change-me-esl', esl)          # 先替换更具体的
t = t.replace('change-me', mp)               # mysql url 里的密码占位
t = t.replace('<sip-domain>', ip)            # default_sip_domain
t = t.replace('<random-salt>', salt)
t = t.replace('<random-secret>', jwt)
t = t.replace('<sha256-salt-password>', ah)
t = t.replace('password: ""', 'password: %s' % rp)   # redis 密码（空=无认证，向后兼容）
t = t.replace('__NODE_UUID__', node)                  # 节点唯一标识
open(dst, 'w').write(t)
PY

# ---------- 5) 校验渲染产物为合法 YAML ----------
python3 -c "import yaml,sys; yaml.safe_load(open('$CFG_LIVE')); print('[deploy] config_settings.yaml YAML 校验通过')"

# ---------- 6) 提示（凭据仅显示一次） ----------
echo ""
echo "==================================================================="
echo "[deploy] 部署配置已生成。请妥善保存以下凭据（仅显示一次）："
echo "  管理端账号            : admin"
echo "  管理端密码            : $ADMIN_PW"
echo "  MySQL 用户 sip_switch : $MYSQL_PW"
echo "  MySQL root 密码       : $MYSQL_ROOT_PW"
echo "  ESL 密码              : $ESL_PW"
echo "  Redis 密码            : $REDIS_PW"
echo "==================================================================="
echo "[deploy] 下一步：docker compose --env-file .env up -d   （或上方加 --up 已自动执行）"

[ "$UP" -eq 1 ] && docker compose --env-file .env up -d
