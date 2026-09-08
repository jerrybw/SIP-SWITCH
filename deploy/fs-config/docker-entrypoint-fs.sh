#!/bin/sh
# FreeSWITCH 容器入口：
#   1) 把含占位符的配置渲染进 FS 配置目录（避免明文凭据入仓）
#   2) 注入 RTP 端口范围（必须与 compose 的端口映射一致）
#   3) 启动 FreeSWITCH
set -e

FS_CONF_DIR="${FS_CONF_DIR:-/etc/freeswitch}"
GATEWAY_URL="${GATEWAY_URL:-http://gateway:8000}"
EXT_SIP_IP="${EXT_SIP_IP:-WSL_IP_REDACTED}"   # 宿主机可达的 WSL eth0 IP，dev-up.sh 每次自动探测注入
ESL_PASSWORD="${ESL_PASSWORD:-ClueCon}"
RTP_START="${RTP_START:-20000}"
RTP_END="${RTP_END:-20100}"

mkdir -p "$FS_CONF_DIR/autoload_configs" "$FS_CONF_DIR/sip_profiles/external"

# 1) 渲染配置：占位符 -> 环境变量
for f in /fs-config/autoload_configs/*.conf.xml; do
  [ -f "$f" ] || continue
  b=$(basename "$f")
  sed -e "s|__ESL_PASSWORD__|${ESL_PASSWORD}|g" \
      -e "s|__GATEWAY_URL__|${GATEWAY_URL}|g" \
      "$f" > "$FS_CONF_DIR/autoload_configs/$b"
  echo "[entrypoint] rendered autoload_configs/$b"
done

# 落地网关 include 路径：共享卷挂到 sip_profiles/external，
# external.xml 用相对 include external/*.xml 读取（与 vanilla example.xml 同机制）。
# 注意：FS 仅在进程启动时展开该 include glob；新增/修改网关文件后需重启 FS 进程，
#       reloadxml / sofia profile external rescan|restart 均不会发现新文件。
if [ -f /fs-config/sip_profiles/external.xml ]; then
  cp /fs-config/sip_profiles/external.xml "$FS_CONF_DIR/sip_profiles/external.xml"
  echo "[entrypoint] installed sip_profiles/external.xml"
fi

if [ -f /fs-config/sip_profiles/internal.xml ]; then
  sed -e "s|__EXT_SIP_IP__|${EXT_SIP_IP}|g" \
      /fs-config/sip_profiles/internal.xml > "$FS_CONF_DIR/sip_profiles/internal.xml"
  echo "[entrypoint] rendered sip_profiles/internal.xml (ext-ip=${EXT_SIP_IP})"
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
