#!/bin/bash
# sipp 桩：用内建 uas 场景（纯 INVITE→180→200→ACK→BYE 闭环）。
# heartbeat_enabled=0 所以不会有 OPTIONS，无需处理。
set -e

echo "[stub] launching sipp -sn uas on 0.0.0.0:5060"
exec /usr/local/bin/sipp \
    -sn uas \
    -i 0.0.0.0 \
    -p 5060 \
    -t u1 \
    -m 999999 \
    -l 999 \
    -d 1000 \
    -nostdin \
    -trace_err \
    127.0.0.1:5060
