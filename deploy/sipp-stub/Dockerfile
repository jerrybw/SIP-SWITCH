# sipp 桩：模拟 SIP 落地网关，只应答 INVITE/OPTIONS。
# 基于 freeswitch 镜像（已含 /usr/local/bin/sipp v3.x），挂载 scenario + start.sh 启动。
FROM sip-switch-fs:1.11.2

COPY scenario.xml /tmp/stub.xml
COPY start.sh /usr/local/bin/start-stub.sh
RUN chmod +x /usr/local/bin/start-stub.sh

EXPOSE 5060/udp
EXPOSE 5060/tcp

ENTRYPOINT ["/usr/local/bin/start-stub.sh"]
