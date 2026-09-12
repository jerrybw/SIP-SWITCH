"""出局路由选择集成测试（route/service.select_outbound_gateway，真 MySQL）。

覆盖此前零测试的核心选路语义（T-202）：
- 最长前缀匹配优先；同长取 priority 大；仍同取 gateway.id 小（稳定序）。
- status=0 的路由/网关不入候选。
- G4 接入点↔落地策略：allow 列表须命中、deny 命中剔除、无策略放行；
  过滤后空池 → None（NO_ROUTE 而非 500）。

依赖 MySQL（app 导入链含自迁移），不可达则整块 skip（与既有 DB 用例同口径，
CI smoke 会真跑）。种子数据自清理，不污染库。
"""
import socket
from datetime import datetime

import pytest

import core.config as _cc


def _mysql_reachable() -> bool:
    url = (_cc.settings.get("mysql") or {}).get("url") or ""
    try:
        hostport = url.split("@", 1)[1].split("/", 1)[0]
        host, port = hostport.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _mysql_reachable(),
                                reason="需要可达的 MySQL（种子/清理走真实库）")

from sqlalchemy import select  # noqa: E402
from db.session import SessionLocal  # noqa: E402
from db.models import Carrier, Gateway, PrefixRoute, AccessGatewayPolicy  # noqa: E402
from route.service import select_outbound_gateway  # noqa: E402


class TestSelectOutboundGateway:
    NOW = datetime.utcnow()
    _seeded = []   # (model, id) 供清理

    @pytest.fixture(autouse=True)
    def _seed(self):
        db = SessionLocal()
        try:
            self._cleanup(db)
            car = Carrier(name="pytest-carrier", bill_unit=60, status=1,
                          created_at=self.NOW, updated_at=self.NOW)
            db.add(car)
            db.commit()
            db.refresh(car)
            # 三条网关：id 顺序即插入顺序（同名不同 id 断言用）
            gws = []
            for i, name in enumerate(("py_gw_a", "py_gw_b", "py_gw_c")):
                g = Gateway(carrier_id=car.id, name=name, ip="10.99.0.%d" % (i + 1),
                            port=5060, auth_type=0, status=1, heartbeat_enabled=0,
                            created_at=self.NOW, updated_at=self.NOW)
                db.add(g)
                db.commit()
                db.refresh(g)
                gws.append(g)
            g_a, g_b, g_c = gws
            # 前缀：长前缀(02166)、短前缀(0)、同长不同优先级(02188 p1/p9)、停用(0300 status=0)
            prs = [
                PrefixRoute(gateway_id=g_a.id, prefix="02166", priority=1, status=1, created_at=self.NOW),
                PrefixRoute(gateway_id=g_b.id, prefix="0", priority=1, status=1, created_at=self.NOW),
                PrefixRoute(gateway_id=g_a.id, prefix="02188", priority=1, status=1, created_at=self.NOW),
                PrefixRoute(gateway_id=g_c.id, prefix="02188", priority=9, status=1, created_at=self.NOW),
                PrefixRoute(gateway_id=g_a.id, prefix="0300", priority=9, status=0, created_at=self.NOW),
            ]
            db.add_all(prs)
            db.commit()
            # G4 用例需要真实的 access_point_id（外键约束）。AP.account_id NOT NULL
            # → 先建测试账户；id 999901/999902 仅测试期存在，_cleanup 负责清干净。
            from db.models import AccessPoint, Account
            acct = db.scalar(select(Account).where(Account.account_number == "8877"))
            if acct is None:
                acct = Account(name="pytest-route", account_number="8877", status=1,
                               created_at=self.NOW, updated_at=self.NOW)
                db.add(acct)
                db.commit()
            for aid in (999901, 999902):
                db.add(AccessPoint(id=aid, account_id=acct.id, name="pytest-ap-%d" % aid,
                                   auth_mode=0, register_host="", bill_unit=60, status=1,
                                   created_at=self.NOW, updated_at=self.NOW))
            db.commit()
            self.car, self.g_a, self.g_b, self.g_c = car, g_a, g_b, g_c
        finally:
            db.close()
        yield
        db = SessionLocal()
        try:
            self._cleanup(db)
        finally:
            db.close()

    def _cleanup(self, db):
        db.query(AccessGatewayPolicy).filter(
            AccessGatewayPolicy.access_point_id.in_([999901, 999902])
        ).delete(synchronize_session=False)
        from db.models import AccessPoint, Account
        db.query(AccessPoint).filter(
            AccessPoint.id.in_([999901, 999902])).delete(synchronize_session=False)
        db.query(AccessGatewayPolicy).filter(
            AccessGatewayPolicy.gateway_id.in_(
                [g.id for g in db.query(Gateway).filter(Gateway.name.like("py_gw_%")).all()]
            )).delete(synchronize_session=False)
        db.query(PrefixRoute).filter(
            PrefixRoute.id.in_(
                [r.id for r in db.query(PrefixRoute).join(Gateway).filter(Gateway.name.like("py_gw_%")).all()]
            )).delete(synchronize_session=False)
        db.query(Gateway).filter(Gateway.name.like("py_gw_%")).delete(synchronize_session=False)
        db.query(Carrier).filter(Carrier.name == "pytest-carrier").delete(synchronize_session=False)
        # 账户只在无引用残留时删（幂等；其它用例可能复用 8877）
        db.query(Account).filter(Account.account_number == "8877",
                                ~db.query(AccessPoint.id).filter(
                                    AccessPoint.account_id == Account.id).exists()).delete(
            synchronize_session=False)
        db.commit()

    def test_longest_prefix_first(self):
        out = select_outbound_gateway(self._db(), "02166123456")
        assert [g.id for g in out][0] == self.g_a.id      # 长前缀 02166 胜出

    def test_fallback_to_short_prefix(self):
        out = select_outbound_gateway(self._db(), "05711234567")
        assert [g.id for g in out][0] == self.g_b.id      # 只有短前缀 0 命中

    def test_same_length_higher_priority_first(self):
        out = select_outbound_gateway(self._db(), "0218812345")
        ids = [g.id for g in out]
        assert ids[0] == self.g_c.id                       # priority 9 > 1
        assert self.g_a.id in ids                          # 次选保留（failover 候选池）

    def test_disabled_route_excluded(self):
        out = select_outbound_gateway(self._db(), "030012345")
        assert [g.id for g in out][0] == self.g_b.id       # 0300 status=0 → 落到短前缀 0

    def test_no_match_returns_none(self):
        assert select_outbound_gateway(self._db(), "99999") is None

    def test_g4_allow_list_filters_and_falls_back(self):
        db = self._db()
        # 02188* 有两个候选（g_c p9 优先、g_a p1 次选）。allow 列表只放 g_a
        # → g_c 被剔除，回退到同前缀次选 g_a（G4 前移过滤的核心语义）。
        db.add(AccessGatewayPolicy(access_point_id=999901, gateway_id=self.g_a.id,
                                   policy=1, created_at=self.NOW))
        db.commit()
        out = select_outbound_gateway(db, "0218812345", ap_id=999901)
        assert [g.id for g in out] == [self.g_a.id]

    def test_g4_all_denied_returns_none(self):
        db = self._db()
        # 短前缀 0 的唯一候选 g_b 被 deny → 过滤后空池 → None（NO_ROUTE）
        db.add(AccessGatewayPolicy(access_point_id=999902, gateway_id=self.g_b.id,
                                   policy=2, created_at=self.NOW))
        db.commit()
        assert select_outbound_gateway(db, "05711234567", ap_id=999902) is None

    def _db(self):
        db = SessionLocal()
        self._alive.append(db)
        return db

    _alive = []

    @pytest.fixture(autouse=True)
    def _close_db(self):
        yield
        for db in self._alive:
            try:
                db.close()
            except Exception:
                pass
        self._alive = []
