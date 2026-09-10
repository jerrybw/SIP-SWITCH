# 入口：启动 ESL 事件线程 + 心跳探测 + FastAPI
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import uvicorn
from core.config import settings
from esl_client import ESLClient, start_cdr_reaper
from heartbeat import HeartbeatProber
from node_health import start_node_health
from fs_provision import start_provision_watcher
from phone_sync import start_phone_sync
from gw_bootstrap import start_gateway_provision
from api.app import app
from core.redis_client import startup_self_check


if __name__ == '__main__':
    esl = ESLClient()
    esl.start()
    start_cdr_reaper()  # T-208：周期重灌 cdr_spool，DB 抖动恢复后补足落库失败的 CDR
    prober = HeartbeatProber(interval=30)
    prober.start()
    start_phone_sync()
    start_gateway_provision()  # 全新部署时重建落地网关 XML（卷被清空的兜底）
    start_node_health()  # #69 FS 节点级健康检查（DEP-6；Phase1 只记录+告警，不摘除）
    start_provision_watcher()  # 多节点：监听 DB provision_seq，把别处的网关变更补扫到本节点 FS
    startup_self_check()  # Phase1-1: Redis 接入自检（当前不阻断启动，P2-a 接入后改 D3 fail-close）
    api_cfg = settings['api']
    uvicorn.run(app, host=api_cfg.get('host', '0.0.0.0'), port=api_cfg.get('port', 8080))
