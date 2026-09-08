#!/bin/sh
# DEV 一键起（WSL 重启后执行）：探测本机 eth0 IP -> 注入 EXT_SIP_IP -> 全栈拉起并重建 FS
# 用法: cd /root/src/SIP-SWITCH && ./dev-up.sh
set -e

# 1) 探测 WSL eth0 IP（172.22.x.x，重启会变）
IP=$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
if [ -z "$IP" ]; then
  echo "[dev-up] ERROR: 探测不到 eth0 IP" >&2
  exit 1
fi
echo "[dev-up] WSL eth0 IP = $IP"

# 2) 注入环境变量
export EXT_SIP_IP="$IP"

# 3) 全栈拉起：mysql/gateway/sipp-stub 若未运行则启动；FS 按新 env 重建
docker compose --env-file .env up -d

# 4) 强制重建 FS，确保使用最新 EXT_SIP_IP（即使其他服务未变）
docker compose --env-file .env up -d --force-recreate freeswitch

# 5) 提示
echo ""
echo "[dev-up] 完成。FS NAT 对外地址已更新为 $IP"
echo "[dev-up] 软电话/浏览器请使用:"
echo "  SIP 服务器: $IP :5060   (80000001 / 80000002)"
echo "  管理端:     http://$IP:8000/admin  (admin / admin123)"
echo "  RTP:        $IP:20000-20100/udp"
echo "[dev-up] 若 IP 变了，软电话里的 SIP 服务器也要改成 $IP"
