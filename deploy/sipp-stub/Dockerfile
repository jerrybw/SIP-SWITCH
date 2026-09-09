# sipp 桩：模拟 SIP 落地网关（carrier trunk）。
# 基于 freeswitch 镜像（已含 /usr/local/bin/sipp v3.6.1）。
#
# 应答能力（由 start-stub.sh 的 "-sn uas -aa" 提供）：
#   OPTIONS  -> 200 OK（-aa 自动应答，供 FS 网关存活探测）
#   INVITE   -> 180 -> 200(+SDP) -> ACK -> pause -> BYE -> 200
# probe-options.xml 是配套的自检场景：从别的容器发一个 OPTIONS 验证桩是否应答。
FROM sip-switch-fs:1.11.2

COPY probe-options.xml /opt/sipp/probe-options.xml
COPY start.sh /usr/local/bin/start-stub.sh
RUN chmod +x /usr/local/bin/start-stub.sh

EXPOSE 5060/udp
EXPOSE 5060/tcp

ENTRYPOINT ["/usr/local/bin/start-stub.sh"]
