"""东方财富市场数据 API 测试（用桩服务，不依赖外部网络）。"""
from fastapi.testclient import TestClient

from app.main import app
from app.market_data.eastmoney_datacenter import (
    DRAGON_TIGER,
    DatasetResult,
)
from app.market_data.eastmoney_market import (
    BoardMember,
    BoardQuote,
    FundFlowPoint,
    FundFlowRow,
)
from app.market_data.eastmoney_limit_up import LIMIT_UP, LimitUpResult


def _board() -> BoardQuote:
    return BoardQuote(
        code="BK1592",
        name="通信线缆及配套",
        kind="industry",
        index_value=27073.48,
        change_pct=5.84,
        change_amount=1493.62,
        volume=1115654700.0,
        amount=50462477548.0,
        amplitude=8.37,
        turnover_rate=10.23,
        main_net_inflow=2671127808.0,
        main_net_inflow_pct=5.29,
        up_count=12,
        down_count=1,
        flat_count=0,
        leader_symbol="300563",
        leader_name="神宇股份",
        leader_change_pct=19.98,
    )


def _flow() -> FundFlowRow:
    return FundFlowRow(
        code="300308",
        name="中际旭创",
        kind="stock",
        price=926.0,
        change_pct=4.03,
        main_net_inflow=3119764480.0,
        main_net_inflow_pct=10.41,
        super_large_net_inflow=2067054848.0,
        super_large_net_inflow_pct=6.9,
        large_net_inflow=1052709632.0,
        large_net_inflow_pct=3.51,
        medium_net_inflow=-1000000.0,
        medium_net_inflow_pct=-0.2,
        small_net_inflow=-500000.0,
        small_net_inflow_pct=-0.1,
    )


class _StubMarketService:
    """可控的市场数据桩服务。"""

    def __init__(self, *, exc: Exception | None = None):
        self._exc = exc
        self.calls: list[str] = []

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self._exc is not None:
            raise self._exc

    async def list_boards(self, kind="industry", limit=50, order="desc"):
        self._check("list_boards")
        return [_board()]

    async def board_constituents(self, board_code, limit=100):
        self._check("board_constituents")
        return [
            BoardMember(
                symbol="600016",
                name="民生银行",
                price=3.7,
                change_pct=1.37,
                volume=10000.0,
                amount=37000.0,
                turnover_rate=1.2,
                main_net_inflow=151856816.0,
                main_net_inflow_pct=3.1,
            )
        ]

    async def board_fund_flow(self, kind="industry", limit=50, order="desc"):
        self._check("board_fund_flow")
        return [_flow()]

    async def stock_fund_flow_rank(self, limit=50, order="desc"):
        self._check("stock_fund_flow_rank")
        return [_flow()]

    async def stock_fund_flow_history(self, symbol, days=60):
        self._check("stock_fund_flow_history")
        return [
            FundFlowPoint(
                trade_date="2026-09-11",
                main_net_inflow=-188044816.0,
                small_net_inflow=-208537.0,
                medium_net_inflow=188253344.0,
                large_net_inflow=-25435488.0,
                super_large_net_inflow=-162609328.0,
                main_net_inflow_pct=-4.24,
                close_price=1275.16,
                change_pct=-0.78,
            )
        ]

    async def close(self) -> None:
        return None


class _StubDatacenterService:
    """可控的数据中心桩服务。"""

    def __init__(self, *, exc: Exception | None = None):
        self._exc = exc
        self.calls: list[tuple] = []

    def _check(self, *call) -> None:
        self.calls.append(call)
        if self._exc is not None:
            raise self._exc

    async def query(self, dataset, **kwargs):
        self._check("query", dataset, kwargs)
        return DatasetResult(
            spec=DRAGON_TIGER,
            total=68,
            rows=[{"trade_date": "2026-09-11", "symbol": "300808"}],
        )

    async def dragon_tiger_seats(self, symbol, **kwargs):
        self._check("seats", symbol, kwargs)
        return {
            "buy": [{"seat_name": "平安证券股份有限公司北京市分公司"}],
            "sell": [{"seat_name": "五矿证券有限公司广州分公司"}],
        }

    async def close(self) -> None:
        return None


class _StubLimitUpService:
    """可控的涨停板情绪池桩服务。"""

    def __init__(self, *, exc: Exception | None = None):
        self._exc = exc
        self.calls: list[tuple] = []

    def _check(self, *call) -> None:
        self.calls.append(call)
        if self._exc is not None:
            raise self._exc

    async def query(self, pool, **kwargs):
        self._check("query", pool, kwargs)
        return LimitUpResult(
            pool=LIMIT_UP,
            trade_date="2026-09-11",
            total=40,
            page=kwargs.get("page", 1),
            items=[{"symbol": "000993", "name": "闽东电力", "price": 13.88}],
        )

    async def close(self) -> None:
        return None


def _client(service: _StubMarketService, datacenter=None, limit_up=None) -> TestClient:
    client = TestClient(app)
    client.__enter__()
    client.app.state.market_service = service
    if datacenter is not None:
        client.app.state.datacenter_service = datacenter
    if limit_up is not None:
        client.app.state.limit_up_service = limit_up
    return client


def test_list_boards_returns_items():
    service = _StubMarketService()
    client = _client(service)
    try:
        resp = client.get("/api/market/boards", params={"kind": "industry", "limit": 5})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "industry"
    assert body["count"] == 1
    assert body["items"][0]["code"] == "BK1592"
    assert body["items"][0]["leader_name"] == "神宇股份"
    assert service.calls == ["list_boards"]


def test_list_boards_rejects_bad_kind():
    service = _StubMarketService(exc=ValueError("不支持的板块类型: 'bogus'"))
    client = _client(service)
    try:
        resp = client.get("/api/market/boards", params={"kind": "bogus"})
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422


def test_list_boards_returns_503_when_source_down():
    service = _StubMarketService(exc=RuntimeError("all hosts down"))
    client = _client(service)
    try:
        resp = client.get("/api/market/boards")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503
    assert "东方财富数据源暂不可用" in resp.json()["detail"]


def test_board_constituents_endpoint():
    service = _StubMarketService()
    client = _client(service)
    try:
        resp = client.get("/api/market/boards/BK0475/constituents")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["board_code"] == "BK0475"
    assert body["items"][0]["symbol"] == "600016"


def test_board_fund_flow_endpoint():
    service = _StubMarketService()
    client = _client(service)
    try:
        resp = client.get("/api/market/fund-flow/boards", params={"kind": "concept"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    assert resp.json()["kind"] == "concept"


def test_stock_fund_flow_rank_endpoint():
    service = _StubMarketService()
    client = _client(service)
    try:
        resp = client.get("/api/market/fund-flow/stocks", params={"limit": 10})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    assert resp.json()["items"][0]["main_net_inflow"] == 3119764480.0


def test_stock_fund_flow_history_endpoint():
    service = _StubMarketService()
    client = _client(service)
    try:
        resp = client.get("/api/market/fund-flow/stocks/600519", params={"days": 20})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "600519"
    assert body["items"][0]["trade_date"] == "2026-09-11"


def test_stock_fund_flow_history_rejects_bad_symbol():
    service = _StubMarketService()
    client = _client(service)
    try:
        resp = client.get("/api/market/fund-flow/stocks/60051")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 422
    assert service.calls == []


# ──────── 数据中心 ────────


def test_datacenter_catalog_lists_datasets():
    client = _client(_StubMarketService())
    try:
        resp = client.get("/api/market/datacenter")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    # 12 个数据集 + 席位字段清单（供前端渲染 EM-09 的 12 个席位列）
    assert body["count"] == 13
    keys = [item["key"] for item in body["datasets"]]
    assert "dragon-tiger" in keys and "northbound" in keys
    assert "executive-hold" in keys and "pledge" in keys
    assert "convertible-bond" in keys
    assert "dragon-tiger-seats" in keys
    dragon = body["datasets"][keys.index("dragon-tiger")]
    assert dragon["supports_date"] is True
    assert dragon["supports_symbol"] is True
    assert any(field["title"] == "交易日期" for field in dragon["fields"])


def test_datacenter_query_passes_filters():
    service = _StubDatacenterService()
    client = _client(_StubMarketService(), service)
    try:
        resp = client.get(
            "/api/market/datacenter/dragon-tiger",
            params={
                "date": "2026-09-11",
                "symbol": "300808",
                "limit": 20,
                "page": 2,
                "order": "asc",
            },
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset"] == "dragon-tiger"
    assert body["label"] == "龙虎榜"
    assert body["total"] == 68
    assert body["page"] == 2
    assert body["count"] == 1
    assert body["rows"][0]["symbol"] == "300808"
    assert service.calls == [
        (
            "query",
            "dragon-tiger",
            {
                "date": "2026-09-11",
                "date_from": None,
                "date_to": None,
                "symbol": "300808",
                "limit": 20,
                "page": 2,
                "order": "asc",
            },
        )
    ]


def test_datacenter_query_rejects_bad_dataset():
    service = _StubDatacenterService(exc=ValueError("未知数据集: 'bogus'"))
    client = _client(_StubMarketService(), service)
    try:
        resp = client.get("/api/market/datacenter/bogus")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422
    assert "未知数据集" in resp.json()["detail"]


def test_datacenter_query_rejects_bad_date():
    service = _StubDatacenterService()
    client = _client(_StubMarketService(), service)
    try:
        resp = client.get(
            "/api/market/datacenter/dragon-tiger", params={"date": "2026-9-1"}
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422
    assert service.calls == []


def test_datacenter_query_returns_503_when_source_down():
    service = _StubDatacenterService(exc=RuntimeError("all hosts busy"))
    client = _client(_StubMarketService(), service)
    try:
        resp = client.get("/api/market/datacenter/northbound")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503
    assert "东方财富数据源暂不可用" in resp.json()["detail"]


def test_dragon_tiger_seats_endpoint():
    service = _StubDatacenterService()
    client = _client(_StubMarketService(), service)
    try:
        resp = client.get(
            "/api/market/datacenter/dragon-tiger/300808/seats",
            params={"trade_date": "2026-09-11", "limit": 10},
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "300808"
    assert body["trade_date"] == "2026-09-11"
    assert body["buy"][0]["seat_name"] == "平安证券股份有限公司北京市分公司"
    assert len(body["sell"]) == 1
    assert service.calls == [
        ("seats", "300808", {"trade_date": "2026-09-11", "limit": 10, "order": None})
    ]


def test_dragon_tiger_seats_rejects_bad_symbol():
    service = _StubDatacenterService()
    client = _client(_StubMarketService(), service)
    try:
        resp = client.get("/api/market/datacenter/dragon-tiger/30/seats")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422
    assert service.calls == []


# ──────── 涨停板情绪池 ────────


def test_limit_up_catalog_lists_pools():
    client = _client(_StubMarketService())
    try:
        resp = client.get("/api/market/limit-up")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 5
    keys = [item["key"] for item in body["pools"]]
    assert keys == ["limit-up", "limit-down", "broken-board", "strong", "sub-new"]
    limit_up = body["pools"][keys.index("limit-up")]
    assert limit_up["label"] == "涨停股池"
    assert any(field["title"] == "封板资金(元)" for field in limit_up["fields"])


def test_limit_up_pool_returns_items():
    service = _StubLimitUpService()
    client = _client(_StubMarketService(), limit_up=service)
    try:
        resp = client.get(
            "/api/market/limit-up/limit-up", params={"limit": 3, "page": 2}
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["pool"] == "limit-up"
    assert body["label"] == "涨停股池"
    assert body["trade_date"] == "2026-09-11"
    assert body["total"] == 40
    assert body["count"] == 1
    assert body["items"][0]["symbol"] == "000993"
    assert service.calls == [
        (
            "query",
            "limit-up",
            {"limit": 3, "page": 2, "order": None, "trade_date": None},
        )
    ]


def test_limit_up_pool_rejects_bad_pool():
    service = _StubLimitUpService(exc=ValueError("未知情绪池: 'bogus'"))
    client = _client(_StubMarketService(), limit_up=service)
    try:
        resp = client.get("/api/market/limit-up/bogus")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422
    assert "未知情绪池" in resp.json()["detail"]


def test_limit_up_pool_returns_503_when_source_down():
    service = _StubLimitUpService(exc=RuntimeError("all hosts down"))
    client = _client(_StubMarketService(), limit_up=service)
    try:
        resp = client.get("/api/market/limit-up/limit-up")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503


def test_limit_up_pool_validates_query_bounds():
    service = _StubLimitUpService()
    client = _client(_StubMarketService(), limit_up=service)
    try:
        too_big = client.get("/api/market/limit-up/limit-up", params={"limit": 999})
        bad_order = client.get(
            "/api/market/limit-up/limit-up", params={"order": "sideways"}
        )
    finally:
        client.__exit__(None, None, None)
    assert too_big.status_code == 422
    assert bad_order.status_code == 422
    assert service.calls == []
