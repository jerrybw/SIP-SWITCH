#!/bin/sh
# FreeSWITCH 容器入口：
#   1) 把含占位符的配置渲染进 FS 配置目录（避免明文凭据入仓）
#   2) 注入 RTP 端口范围（必须与 compose 的端口映射一致）
#   3) 启动 FreeSWITCH
set -e

FS_CONF_DIR="${FS_CONF_DIR:-/etc/freeswitch}"
GATEWAY_URL="${GATEWAY_URL:-http://gateway:8000}"
EXT_SIP_IP="${EXT_SIP_IP:-}"
if [ -z "${EXT_SIP_IP}" ]; then
  echo "[entrypoint] WARN: EXT_SIP_IP 未注入，ext-sip-ip 回退为容器内网地址" >&2
  echo "[entrypoint] WARN: WSL 重启/IP 变更后请执行 ./dev-up.sh 重新注入" >&2
  EXT_SIP_IP='$${local_ip_v4}'
fi
if [ -z "${ESL_PASSWORD}" ]; then
  echo "[entrypoint] WARN: ESL_PASSWORD 未注入，生成随机 ESL 密码（避免默认 ClueCon）" >&2
  ESL_PASSWORD=$(head -c 48 /dev/urandom | base64 | tr -d "/+=" | head -c 32)
  echo "[entrypoint] 本次生成的 ESL_PASSWORD=${ESL_PASSWORD}（仅本次容器生命周期有效）" >&2
fi
# xml_curl 回调凭据（user:password）：网关侧 /fs/* 已启用 HTTP Basic 认证。
# 缺失时生成随机值只会让网关侧对不上（全部 401），因此与 ESL_PASSWORD 同款告警，
# 提示必须与 config_settings.yaml [xml_curl] 同源注入（deploy.sh 已自动处理）。
if [ -z "${XMLCURL_USER}" ] || [ -z "${XMLCURL_PASSWORD}" ]; then
  echo "[entrypoint] WARN: XMLCURL_USER/XMLCURL_PASSWORD 未注入，生成随机值" >&2
  echo "[entrypoint] WARN: 网关侧 config_settings.yaml [xml_curl] 若非同值，/fs/* 将全部 401（xml_curl 失效）" >&2
  XMLCURL_USER="${XMLCURL_USER:-xmlcurl}"
  XMLCURL_PASSWORD=$(head -c 48 /dev/urandom | base64 | tr -d "/+=" | head -c 32)
  echo "[entrypoint] 本次生成的 XMLCURL_PASSWORD=${XMLCURL_PASSWORD}（仅本次容器生命周期有效）" >&2
fi
RTP_START="${RTP_START:-20000}"
RTP_END="${RTP_END:-20100}"

# #70 录音目录：compose 把宿主 ./data/recordings 挂到这里（容器内固定 /recordings）。
# 未挂卷时也建出来，避免 dialplan 指向的路径不存在导致 record_session 静默失败。
RECORD_DIR="${RECORD_DIR:-/recordings}"
mkdir -p "${RECORD_DIR}" || echo "[entrypoint] WARN: 无法创建录音目录 ${RECORD_DIR}" >&2

mkdir -p "$FS_CONF_DIR/autoload_configs" "$FS_CONF_DIR/sip_profiles/external"

# 1) 渲染配置：占位符 -> 环境变量
for f in /fs-config/autoload_configs/*.conf.xml; do
  [ -f "$f" ] || continue
  b=$(basename "$f")
  sed -e "s|__ESL_PASSWORD__|${ESL_PASSWORD}|g" \
      -e "s|__GATEWAY_URL__|${GATEWAY_URL}|g" \
      -e "s|__XMLCURL_USER__|${XMLCURL_USER}|g" \
      -e "s|__XMLCURL_PASSWORD__|${XMLCURL_PASSWORD}|g" \
      "$f" > "$FS_CONF_DIR/autoload_configs/$b"
  echo "[entrypoint] rendered autoload_configs/$b"
done

# 落地网关 include 路径：共享卷挂到 sip_profiles/external，
# external.xml 用相对 include external/*.xml 读取（与 vanilla example.xml 同机制）。
# 注意：FS 仅在进程启动时展开该 include glob；新增/修改网关文件后需重启 FS 进程，
#       reloadxml / sofia profile external rescan|restart 均不会发现新文件。
if [ -f /fs-config/sip_profiles/external.xml ]; then
  sed -e "s|__EXT_SIP_IP__|${EXT_SIP_IP}|g" \
      /fs-config/sip_profiles/external.xml > "$FS_CONF_DIR/sip_profiles/external.xml"
  echo "[entrypoint] rendered sip_profiles/external.xml (ext-ip=${EXT_SIP_IP})"
fi

if [ -f /fs-config/sip_profiles/internal.xml ]; then
  sed -e "s|__EXT_SIP_IP__|${EXT_SIP_IP}|g" \
      /fs-config/sip_profiles/internal.xml > "$FS_CONF_DIR/sip_profiles/internal.xml"
  echo "[entrypoint] rendered sip_profiles/internal.xml (ext-ip=${EXT_SIP_IP})"

# 禁用镜像自带的 IPv6 profile（我们只用 internal:5060 / external:5080 两个 IPv4 profile）。
# 原因：其 ext-rtp-ip / ext-sip-ip 取 $${external_rtp_ip} / $${external_sip_ip}，
# 而这两个变量由 vars.xml 的 stun-set 从 stun.freeswitch.org 探测；STUN 超时即为空，
# 导致 profile 创建失败并连带 mod_sofia 整体加载失败 —— SIP 全挂。
for prof in external-ipv6 internal-ipv6; do
  if [ -f "${FS_CONF_DIR}/sip_profiles/${prof}.xml" ]; then
    mv "${FS_CONF_DIR}/sip_profiles/${prof}.xml" "${FS_CONF_DIR}/sip_profiles/${prof}.xml.disabled"
    echo "[entrypoint] disabled unused STUN-dependent profile ${prof}"
  fi
done

fi

# 2) 注入 RTP 端口范围
SW="$FS_CONF_DIR/autoload_configs/switch.conf.xml"
if [ -f "$SW" ]; then
  sed -i "s|</settings>|  <param name=\"rtp-start-port\" value=\"${RTP_START}\"/>\\
  <param name=\"rtp-end-port\" value=\"${RTP_END}\"/>\\
</settings>|" "$SW"
  echo "[entrypoint] RTP range -> ${RTP_START}-${RTP_END}"
else
  echo "[entrypoint] WARN: $SW not found, RTP 范围未注入（请确认 FS_CONF_DIR）"
fi

exec "$@"
