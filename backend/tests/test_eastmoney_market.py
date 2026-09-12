"""东方财富板块/资金流服务测试。"""
import httpx
import pytest

from app.market_data.eastmoney_market import (
    ALL_A_SHARES_FS,
    EastmoneyMarketService,
    parse_board_member_rows,
    parse_board_rows,
    parse_fund_flow_history,
    parse_fund_flow_rows,
)

_BOARD_ROW = {
    "f2": 27073.48,
    "f3": 5.84,
    "f4": 1493.62,
    "f5": 11156547,
    "f6": 50462477548.0,
    "f7": 8.37,
    "f8": 10.23,
    "f12": "BK1592",
    "f14": "通信线缆及配套",
    "f62": 2671127808.0,
    "f104": 12,
    "f105": 1,
    "f106": 0,
    "f128": "神宇股份",
    "f136": 19.98,
    "f140": "300563",
    "f184": 5.29,
}

_MEMBER_ROW = {
    "f2": 3.7,
    "f3": 1.37,
    "f5": 100,
    "f6": 1000.0,
    "f8": 1.2,
    "f12": "600016",
    "f14": "民生银行",
    "f62": 151856816.0,
    "f184": 3.1,
}

_FLOW_ROW = {
    "f2": 926.0,
    "f3": 4.03,
    "f12": "300308",
    "f14": "中际旭创",
    "f62": 3119764480.0,
    "f66": 2067054848.0,
    "f69": 6.9,
    "f72": 1052709632.0,
    "f75": 3.51,
    "f78": -1000000.0,
    "f81": -0.2,
    "f84": -500000.0,
    "f87": -0.1,
    "f184": 10.41,
}


def test_parse_board_rows_maps_fields():
    boards = parse_board_rows({"data": {"diff": [_BOARD_ROW]}}, "industry")
    assert len(boards) == 1
    board = boards[0]
    assert board.code == "BK1592"
    assert board.name == "通信线缆及配套"
    assert board.kind == "industry"
    assert board.change_pct == 5.84
    assert board.volume == 11156547 * 100  # 手 → 股
    assert board.main_net_inflow == 2671127808.0
    assert board.up_count == 12
    assert board.down_count == 1
    assert board.leader_symbol == "300563"
    assert board.leader_name == "神宇股份"
    assert board.leader_change_pct == 19.98


def test_parse_board_rows_handles_dict_and_dash():
    """字典形式的 diff 与 '-' 占位都要能处理。"""
    row = dict(_BOARD_ROW)
    row["f140"] = "-"
    row["f128"] = "-"
    boards = parse_board_rows({"data": {"diff": {"0": row}}}, "concept")
    assert boards[0].leader_symbol is None
    assert boards[0].leader_name is None
    assert boards[0].leader_change_pct is None
    assert boards[0].kind == "concept"


def test_parse_board_rows_handles_empty_payload():
    assert parse_board_rows({}, "industry") == []
    assert parse_board_rows({"data": None}, "industry") == []
    assert parse_board_rows({"data": {"diff": ["x"]}}, "industry") == []


def test_parse_board_member_rows():
    members = parse_board_member_rows({"data": {"diff": [_MEMBER_ROW]}})
    assert members[0].symbol == "600016"
    assert members[0].volume == 100 * 100
    assert members[0].main_net_inflow_pct == 3.1


def test_parse_fund_flow_rows_defaults_to_stock_kind():
    rows = parse_fund_flow_rows({"data": {"diff": [_FLOW_ROW]}})
    assert rows[0].kind == "stock"
    assert rows[0].main_net_inflow == 3119764480.0
    assert rows[0].super_large_net_inflow == 2067054848.0
    assert rows[0].large_net_inflow == 1052709632.0
    assert rows[0].small_net_inflow == -500000.0
    assert rows[0].main_net_inflow_pct == 10.41
    # 主力 = 超大单 + 大单
    assert (
        rows[0].super_large_net_inflow + rows[0].large_net_inflow
        == rows[0].main_net_inflow
    )


def test_parse_fund_flow_history_orders_columns():
    payload = {
        "data": {
            "name": "贵州茅台",
            "klines": [
                "2026-09-11,-188044816.0,-208537.0,188253344.0,-25435488.0,"
                "-162609328.0,-4.24,-0.00,4.25,-0.57,-3.67,1275.16,-0.78,0.00,0.00"
            ],
        }
    }
    points = parse_fund_flow_history(payload)
    assert len(points) == 1
    point = points[0]
    assert point.trade_date == "2026-09-11"
    assert point.main_net_inflow == -188044816.0
    assert point.small_net_inflow == -208537.0
    assert point.medium_net_inflow == 188253344.0
    assert point.main_net_inflow_pct == -4.24
    assert point.close_price == 1275.16
    assert point.change_pct == -0.78


def test_parse_fund_flow_history_skips_bad_rows():
    payload = {
        "data": {
            "klines": [
                "",
                "2026-09-11,1,2",
                "not-a-date,1,2,3,4,5,6,7,8,9,10,11,12",
            ]
        }
    }
    assert parse_fund_flow_history(payload) == []


def _service(handler, **kwargs) -> EastmoneyMarketService:
    service = EastmoneyMarketService(**kwargs)
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return service


# ──────── 请求行为 ────────


@pytest.mark.asyncio
async def test_list_boards_sends_expected_params():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"total": 1, "diff": [_BOARD_ROW]}})

    service = _service(handler)
    try:
        boards = await service.list_boards("industry", limit=1)
    finally:
        await service.close()

    assert boards[0].code == "BK1592"
    assert captured["fs"] == "m:90+t:2"
    assert captured["fid"] == "f3"
    assert captured["fltt"] == "2"
    assert captured["pz"] == "1"
    assert "f128" in captured["fields"]
    assert captured["ut"]


@pytest.mark.asyncio
async def test_list_boards_pages_until_limit():
    pages: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        pages.append((params["pn"], params["pz"]))
        size = int(params["pz"])
        start = (int(params["pn"]) - 1) * size
        rows = []
        for index in range(size):
            row = dict(_BOARD_ROW)
            row["f12"] = f"BK{start + index:04d}"
            rows.append(row)
        return httpx.Response(200, json={"data": {"total": 150, "diff": rows}})

    service = _service(handler)
    try:
        boards = await service.list_boards("concept", limit=150)
    finally:
        await service.close()

    assert pages == [("1", "100"), ("2", "50")]
    assert len(boards) == 150


@pytest.mark.asyncio
async def test_list_boards_rejects_unknown_kind():
    service = EastmoneyMarketService()
    try:
        with pytest.raises(ValueError):
            await service.list_boards("bogus")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_board_constituents_validates_code():
    service = EastmoneyMarketService()
    try:
        with pytest.raises(ValueError):
            await service.board_constituents("600000")
        with pytest.raises(ValueError):
            await service.board_constituents("BK")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_board_constituents_uses_board_filter():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"total": 1, "diff": [_MEMBER_ROW]}})

    service = _service(handler)
    try:
        members = await service.board_constituents("bk0475", limit=1)
    finally:
        await service.close()

    assert captured["fs"] == "b:BK0475"
    assert members[0].symbol == "600016"


@pytest.mark.asyncio
async def test_stock_fund_flow_rank_uses_all_a_shares():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"total": 1, "diff": [_FLOW_ROW]}})

    service = _service(handler)
    try:
        rows = await service.stock_fund_flow_rank(limit=1)
    finally:
        await service.close()

    assert captured["fs"] == ALL_A_SHARES_FS
    assert captured["fid"] == "f62"
    assert rows[0].kind == "stock"


@pytest.mark.asyncio
async def test_stock_fund_flow_history_uses_fflow_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "data": {
                    "klines": [
                        "2026-09-11,-1.0,-2.0,3.0,-4.0,-5.0,-6.0,0,0,0,0,10.5,1.2,0,0"
                    ]
                }
            },
        )

    service = _service(handler)
    try:
        points = await service.stock_fund_flow_history("600519", days=5)
    finally:
        await service.close()

    assert captured["path"].endswith("/api/qt/stock/fflow/daykline/get")
    assert captured["secid"] == "1.600519"
    assert captured["lmt"] == "5"
    assert points[0].close_price == 10.5


@pytest.mark.asyncio
async def test_service_fails_over_when_host_lacks_data():
    """首选主机返回 data=null（没有这份数据）时自动切换备用主机。"""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "push2.eastmoney.com":
            return httpx.Response(200, json={"rc": 0, "data": None})
        return httpx.Response(200, json={"data": {"total": 1, "diff": [_BOARD_ROW]}})

    service = _service(handler)
    try:
        boards = await service.list_boards("industry", limit=1)
    finally:
        await service.close()

    assert [board.code for board in boards] == ["BK1592"]
    assert hosts == ["push2.eastmoney.com", "push2delay.eastmoney.com"]
