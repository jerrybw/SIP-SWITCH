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
from gw_state import start_gateway_state_sync
from phone_sync import start_phone_sync
from gw_bootstrap import start_gateway_provision
from api.app import app
from core.redis_client import startup_self_check


if __name__ == '__main__':
    esl = ESLClient()
    esl.start()
    # #75：事件 worker / CDR writer / 对账线程（daemon）
    from esl_client import start_esl_background_workers
    start_esl_background_workers()
    start_cdr_reaper()  # T-208：周期重灌 cdr_spool，DB 抖动恢复后补足落库失败的 CDR
    # 探测周期以 gateway.heartbeat_interval 为准（每轮现读 DB）；此参数仅为 DB 不可用时的兜底
    prober = HeartbeatProber()
    prober.start()
    start_phone_sync()
    start_gateway_provision()  # 全新部署时重建落地网关 XML（卷被清空的兜底）
    start_node_health()  # #69 FS 节点级健康检查（DEP-6；Phase1 只记录+告警，不摘除）
    start_provision_watcher()  # 多节点：监听 DB provision_seq，把别处的网关变更补扫到本节点 FS
    start_gateway_state_sync()  # 注册型网关：启动时用 xmlstatus 对齐一次注册状态（事件之外的兜底初值）
    startup_self_check()  # Phase1-1: Redis 接入自检（当前不阻断启动，P2-a 接入后改 D3 fail-close）
    api_cfg = settings['api']
    uvicorn.run(app, host=api_cfg.get('host', '0.0.0.0'), port=api_cfg.get('port', 8080))
