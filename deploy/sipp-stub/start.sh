#!/bin/bash
# sipp 桩：模拟 SIP 落地网关（carrier trunk）。
#
# 关键参数说明（sipp 3.6 实测）：
#   -sn uas  内建 UAS 场景：INVITE -> 180 -> 200(+SDP) -> ACK -> pause -> BYE -> 200
#   -aa      自动应答。sipp 的 "-aa" = auto-answer，会对 INFO/UPDATE/NOTIFY/**OPTIONS**
#            等 out-of-dialog 请求直接回 200 OK，无需在场景里写 <recv request="OPTIONS">。
#            没有 -aa 时，sipp server 模式收到 OPTIONS 会直接丢弃（实测 200 超时）。
#   -i       本地 IP，写进 Contact/Via/SDP。必须填容器真实 IP；
#            填 0.0.0.0 会让 SDP 里出现 c=IN IP4 0.0.0.0，FS 侧媒体地址不可达。
#   -d       通话保持时长(ms)，pause 之后 sipp 主动发 BYE。
#
# 已知限制（别再绕弯路，详见 PITFALLS #29）：
#   * server 模式（UAS）**不能**用 -oocsf/-oocsn（sipp 直接拒绝启动）
#   * 场景首条必须是 mandatory <recv>，所以 OPTIONS 无法作为第二条起始消息进同一场景
#   * optional="global" 也救不了：无活动 call 时 OPTIONS 仍被丢弃
#   * -sf 自定义场景 + -aa 会让 sipp 立刻退出（实测 0 calls terminated）
#   结论：OPTIONS 支持只能靠 "-sn uas -aa" 这条内建路径。
set -e

IP=$(hostname -i | awk '{print $1}')
echo "[stub] launching sipp -sn uas -aa on ${IP}:5060 (OPTIONS + INVITE auto-answer)"

exec /usr/local/bin/sipp \
    -sn uas \
    -aa \
    -i "$IP" \
    -p 5060 \
    -t u1 \
    -m 999999 \
    -l 999 \
    -d 1000 \
    -rtp_echo \
    -nostdin \
    -trace_err
