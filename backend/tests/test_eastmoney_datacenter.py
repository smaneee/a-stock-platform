"""东方财富数据中心（datacenter-web）服务测试，全部用桩传输，不依赖外部网络。"""
import httpx
import pytest

from app.market_data.eastmoney_datacenter import (
    CONVERTIBLE_BOND,
    DATASETS,
    DATA_HOSTS,
    DRAGON_TIGER,
    DRAGON_TIGER_SEATS,
    EXECUTIVE_HOLD,
    MUTUAL_TYPE_LABELS,
    NORTHBOUND,
    PLEDGE,
    DatasetSpec,
    EastmoneyDataError,
    EastmoneyDatacenterService,
    _parse_rows,
    dataset_catalog,
)


def _row(spec: DatasetSpec, **overrides) -> dict:
    """按声明造一行结构完整的东财返回行，再覆盖需要断言的列。"""
    row: dict = {}
    for item in spec.fields:
        if item.kind == "date":
            row[item.column] = "2026-09-11 00:00:00"
        elif item.kind == "text":
            row[item.column] = item.column
        else:
            row[item.column] = 1.5
    row.update(overrides)
    return row


def _payload(rows, *, total=None, success=True, code=0) -> dict:
    if not success:
        return {"success": False, "code": code, "result": None, "message": "数据繁忙"}
    return {
        "success": True,
        "code": 0,
        "result": {
            "pages": 1,
            "data": rows,
            "count": len(rows) if total is None else total,
        },
    }


def _service(handler, **kwargs) -> EastmoneyDatacenterService:
    service = EastmoneyDatacenterService(**kwargs)
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return service


# ──────── 数据集声明 ────────


def test_catalog_describes_every_dataset():
    catalog = dataset_catalog()
    keys = [item["key"] for item in catalog]
    # 12 个可独立查询的数据集 + 1 个席位字段清单（dragon-tiger-seats，挂在
    # dragon-tiger 行上，不单独查询，但需要向前端暴露字段以避免 EM-09 漏渲染）
    assert keys == list(DATASETS) + ["dragon-tiger-seats"]
    assert len(catalog) == 13
    for item in catalog:
        assert item["label"]
        assert item["description"]
        assert item["fields"]
        assert all(field["key"] and field["title"] for field in item["fields"])
        assert all("note" in field for field in item["fields"])


def test_declared_fields_are_unique():
    """输出名不能重复；请求给东财的列名要去重。"""
    for spec in DATASETS.values():
        keys = [item.key for item in spec.fields]
        assert len(keys) == len(set(keys)), spec.key
        assert len(set(spec.columns)) == len(spec.columns), spec.key
    # 通道代码与通道名称共用 MUTUAL_TYPE，但只请求一次
    assert [item.column for item in NORTHBOUND.fields].count("MUTUAL_TYPE") == 2
    assert NORTHBOUND.columns.count("MUTUAL_TYPE") == 1


def test_date_and_symbol_support_flags():
    assert DRAGON_TIGER.supports_date and DRAGON_TIGER.supports_symbol
    assert NORTHBOUND.supports_date and not NORTHBOUND.supports_symbol


def test_new_datasets_are_registered_with_expected_labels():
    assert [EXECUTIVE_HOLD.key, PLEDGE.key, CONVERTIBLE_BOND.key] == [
        "executive-hold",
        "pledge",
        "convertible-bond",
    ]
    assert [item.label for item in (EXECUTIVE_HOLD, PLEDGE, CONVERTIBLE_BOND)] == [
        "高管持股变动",
        "股权质押比例",
        "可转债",
    ]
    assert EXECUTIVE_HOLD.supports_date and EXECUTIVE_HOLD.supports_symbol
    assert PLEDGE.supports_date and PLEDGE.supports_symbol
    # 可转债报表没有逐行交易日，按代码过滤即可
    assert CONVERTIBLE_BOND.supports_symbol and not CONVERTIBLE_BOND.supports_date


# ──────── 行解析 ────────


def test_parse_rows_maps_declared_fields():
    row = _row(
        DRAGON_TIGER,
        TRADE_DATE="2026-09-11 00:00:00",
        SECURITY_CODE="300808",
        SECURITY_NAME_ABBR="久量股份",
        CLOSE_PRICE=20.28,
        BILLBOARD_NET_AMT=-30848753.0,
        EXPLANATION="日跌幅达到15%的前5只证券",
        D1_CLOSE_ADJCHRATE=None,
    )
    rows = _parse_rows(DRAGON_TIGER, [row])
    assert len(rows) == 1
    parsed = rows[0]
    assert parsed["trade_date"] == "2026-09-11"
    assert parsed["symbol"] == "300808"
    assert parsed["name"] == "久量股份"
    assert parsed["close"] == 20.28
    assert parsed["billboard_net_amount"] == -30848753.0
    assert parsed["reason"] == "日跌幅达到15%的前5只证券"
    assert parsed["change_1d_pct"] is None


def test_parse_rows_keeps_signed_executive_change():
    """减持是负数：变动股数与变动金额必须保留符号，不能被 em_to_float 抹平。"""
    row = _row(
        EXECUTIVE_HOLD,
        SECURITY_CODE="600165",
        SECURITY_NAME="宁科生物",
        PERSON_NAME="符杰",
        POSITION_NAME="董事",
        CHANGE_SHARES=-300000,
        AVERAGE_PRICE=3.21,
        CHANGE_AMOUNT=-963000,
        CHANGE_RATIO=0.0186,
        CHANGE_AFTER_HOLDNUM=780500,
    )
    parsed = _parse_rows(EXECUTIVE_HOLD, [row])[0]
    assert parsed["change_date"] == "2026-09-11"
    assert parsed["symbol"] == "600165"
    assert parsed["person_name"] == "符杰"
    assert parsed["position"] == "董事"
    assert parsed["change_shares"] == -300000.0
    assert parsed["change_amount"] == -963000.0
    assert parsed["change_ratio"] == 0.0186


def test_pledge_fields_keep_upstream_units():
    """质押股数与质押市值按上游口径返回万股 / 万元，不做二次换算。"""
    row = _row(
        PLEDGE,
        SECURITY_CODE="000001",
        SECURITY_NAME_ABBR="平安银行",
        PLEDGE_RATIO=0.13,
        REPURCHASE_BALANCE=2438.48,
        PLEDGE_MARKET_CAP=28627.7552,
    )
    parsed = _parse_rows(PLEDGE, [row])[0]
    assert parsed["pledge_ratio"] == 0.13
    assert parsed["pledge_shares"] == 2438.48
    assert parsed["pledge_market_cap"] == 28627.7552
    titles = [item.title for item in PLEDGE.fields]
    assert "质押股数(万股)" in titles
    assert "质押市值(万元)" in titles


def test_parse_rows_handles_dash_and_empty():
    row = _row(NORTHBOUND, DEAL_AMT="-", NET_DEAL_AMT=None, MUTUAL_TYPE="999")
    parsed = _parse_rows(NORTHBOUND, [row])[0]
    # 可选字段保留「无数据」语义：null / "-" 都是 None
    assert parsed["deal_amount"] is None
    assert parsed["net_deal_amount"] is None
    # 未知通道代码原样返回，不丢数据
    assert parsed["channel"] == "999"
    # 必有值的数值字段遇到 "-"（停牌/无数据）统一转 0
    tiger = _parse_rows(DRAGON_TIGER, [_row(DRAGON_TIGER, BILLBOARD_NET_AMT="-")])[0]
    assert tiger["billboard_net_amount"] == 0.0


def test_parse_rows_maps_known_mutual_types():
    for code, label in MUTUAL_TYPE_LABELS.items():
        row = _row(NORTHBOUND, MUTUAL_TYPE=code)
        parsed = _parse_rows(NORTHBOUND, [row])[0]
        assert parsed["mutual_type"] == code
        assert parsed["channel"] == label


def test_northbound_amounts_are_converted_from_millions():
    """沪股通成交额原始值 142256.09（百万元）→ 1422.56 亿元。"""
    row = _row(NORTHBOUND, DEAL_AMT=142256.09, HOLD_MARKET_CAP=1.5e13)
    parsed = _parse_rows(NORTHBOUND, [row])[0]
    assert parsed["deal_amount"] == pytest.approx(142256.09 * 1_000_000)
    # 持股市值本身就是元，不再放大
    assert parsed["hold_market_cap"] == pytest.approx(1.5e13)


def test_parse_rows_raises_when_upstream_renames_column():
    row = _row(DRAGON_TIGER)
    row.pop("SECURITY_NAME_ABBR")
    with pytest.raises(EastmoneyDataError) as excinfo:
        _parse_rows(DRAGON_TIGER, [row])
    assert "SECURITY_NAME_ABBR" in str(excinfo.value)


def test_parse_rows_returns_empty_for_no_rows():
    assert _parse_rows(DRAGON_TIGER, []) == []


# ──────── 请求行为 ────────


@pytest.mark.asyncio
async def test_query_sends_expected_params():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=_payload([_row(DRAGON_TIGER)], total=68))

    service = _service(handler)
    try:
        result = await service.query(
            "dragon-tiger", date="2026-09-11", symbol="300808", limit=20, page=2
        )
    finally:
        await service.close()

    assert captured["reportName"] == "RPT_DAILYBILLBOARD_DETAILSNEW"
    assert captured["pageSize"] == "20"
    assert captured["pageNumber"] == "2"
    assert captured["sortColumns"] == "TRADE_DATE"
    assert captured["sortTypes"] == "-1"
    assert captured["source"] == "WEB"
    assert captured["filter"] == "(TRADE_DATE='2026-09-11')(SECURITY_CODE=\"300808\")"
    assert "SECURITY_NAME_ABBR" in captured["columns"]
    assert result.total == 68
    assert result.spec.key == "dragon-tiger"
    assert len(result.rows) == 1


@pytest.mark.asyncio
async def test_query_caps_page_size_and_uses_dataset_default_sort():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=_payload([]))

    service = _service(handler)
    try:
        result = await service.query("holder-number", limit=9999)
    finally:
        await service.close()

    assert captured["pageSize"] == "500"
    assert captured["sortColumns"] == "HOLDER_NUM"
    assert "filter" not in captured
    assert result.total == 0
    assert result.rows == []


@pytest.mark.asyncio
async def test_query_maps_order_to_sort_direction():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params)["sortTypes"])
        return httpx.Response(200, json=_payload([]))

    service = _service(handler)
    try:
        await service.query("dividend", order="asc")
        await service.query("dividend", order="desc")
    finally:
        await service.close()
    assert seen == ["1", "-1"]


@pytest.mark.asyncio
async def test_query_returns_empty_for_9201():
    """东财用 9201 表示「数据为空」，是正常结果而不是故障。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "code": 9201, "result": None, "message": "数据为空"},
        )

    service = _service(handler)
    try:
        result = await service.query("dragon-tiger", date="2099-01-01")
    finally:
        await service.close()
    assert result.total == 0
    assert result.rows == []


@pytest.mark.asyncio
async def test_query_fails_over_on_transport_error():
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == DATA_HOSTS[0]:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json=_payload([_row(DRAGON_TIGER)]))

    service = _service(handler)
    try:
        result = await service.query("dragon-tiger")
    finally:
        await service.close()
    assert hosts == [DATA_HOSTS[0], DATA_HOSTS[1]]
    assert len(result.rows) == 1


@pytest.mark.asyncio
async def test_query_fails_over_when_host_is_busy():
    """success=False（如 9701 数据繁忙）不算可用结果，应切换备用主机。"""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == DATA_HOSTS[0]:
            return httpx.Response(
                200,
                json={"success": False, "code": 9701, "result": None, "message": "数据繁忙"},
            )
        return httpx.Response(200, json=_payload([_row(NORTHBOUND)]))

    service = _service(handler)
    try:
        result = await service.query("northbound")
    finally:
        await service.close()
    assert hosts == list(DATA_HOSTS)
    assert len(result.rows) == 1


@pytest.mark.asyncio
async def test_query_raises_when_every_host_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "code": 9701, "result": None, "message": "数据繁忙"}
        )

    service = _service(handler)
    try:
        with pytest.raises(RuntimeError):
            await service.query("northbound")
    finally:
        await service.close()


# ──────── 参数校验（不得把用户输入直接拼进 filter） ────────


def _no_request_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"参数校验失败时不应发起请求: {request.url}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"date": "2026-9-1"},
        {"date": "2026/09/11"},
        {"date_from": "2026-09-11')(SECURITY_CODE=\"1"},
        {"date_to": "abc"},
        {"symbol": "60"},
        {"symbol": "30080a"},
        {"symbol": "300808'"},
    ],
)
async def test_query_rejects_invalid_filters(kwargs):
    service = _service(_no_request_handler)
    try:
        with pytest.raises(ValueError):
            await service.query("dragon-tiger", **kwargs)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_query_rejects_unknown_dataset():
    service = _service(_no_request_handler)
    try:
        with pytest.raises(ValueError) as excinfo:
            await service.query("bogus")
    finally:
        await service.close()
    assert "未知数据集" in str(excinfo.value)


@pytest.mark.asyncio
async def test_query_rejects_unsupported_filters():
    service = _service(_no_request_handler)
    try:
        with pytest.raises(ValueError) as symbol_exc:
            await service.query("northbound", symbol="600519")
        assert "不支持按股票代码过滤" in str(symbol_exc.value)
    finally:
        await service.close()


# ──────── 龙虎榜席位 ────────


@pytest.mark.asyncio
async def test_dragon_tiger_seats_queries_both_sides():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append((params["reportName"], params.get("filter", "")))
        return httpx.Response(200, json=_payload([_row(DRAGON_TIGER_SEATS)]))

    service = _service(handler)
    try:
        seats = await service.dragon_tiger_seats("300808", trade_date="2026-09-11")
    finally:
        await service.close()

    assert [report for report, _ in calls] == [
        "RPT_BILLBOARD_DAILYDETAILSBUY",
        "RPT_BILLBOARD_DAILYDETAILSSELL",
    ]
    assert all(
        filter_expr == '(SECURITY_CODE="300808")(TRADE_DATE=\'2026-09-11\')'
        for _, filter_expr in calls
    )
    assert len(seats["buy"]) == 1
    assert len(seats["sell"]) == 1
    assert seats["buy"][0]["seat_name"] == "OPERATEDEPT_NAME"


@pytest.mark.asyncio
async def test_dragon_tiger_seats_rejects_bad_symbol_before_request():
    service = _service(_no_request_handler)
    try:
        with pytest.raises(ValueError):
            await service.dragon_tiger_seats("30")
    finally:
        await service.close()
