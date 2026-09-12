"""东方财富涨停板情绪池服务测试，全部用桩传输，不依赖外部网络。"""
import httpx
import pytest

from app.market_data.eastmoney_limit_up import (
    BROKEN_BOARD,
    LIMIT_DOWN,
    LIMIT_UP,
    POOLS,
    STRONG,
    SUB_NEW,
    EastmoneyLimitUpError,
    EastmoneyLimitUpService,
    LimitUpPool,
    _parse_pool,
    pool_catalog,
)


def _row(pool: LimitUpPool, **overrides) -> dict:
    """按声明造一行结构完整的 push2ex 返回行，再覆盖需要断言的字段。"""
    row: dict = {}
    for item in pool.fields:
        if item.kind == "text":
            row[item.column] = item.column
        elif item.kind == "price":
            row[item.column] = 13880
        elif item.kind == "clock":
            row[item.column] = 92500
        elif item.kind == "ymd":
            row[item.column] = 20260911
        elif item.kind == "yesno":
            row[item.column] = "1"
        elif item.kind == "zttj":
            row[item.column] = {"days": 3, "ct": 3}
        elif item.kind == "int":
            row[item.column] = 3
        else:
            row[item.column] = 1.5
    row.update(overrides)
    return row


def _payload(rows, *, total=None, qdate=20260911, rc=0) -> dict:
    return {
        "rc": rc,
        "data": {
            "tc": len(rows) if total is None else total,
            "qdate": qdate,
            "pool": rows,
        },
    }


def _service(handler, **kwargs) -> EastmoneyLimitUpService:
    service = EastmoneyLimitUpService(**kwargs)
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return service


# ──────── 池声明 ────────


def test_catalog_describes_every_pool():
    catalog = pool_catalog()
    assert [item["key"] for item in catalog] == list(POOLS)
    assert len(catalog) == 5
    for item in catalog:
        assert item["label"]
        assert item["description"]
        assert item["fields"]
        assert all(field["key"] and field["title"] for field in item["fields"])


def test_declared_fields_are_unique_and_deduplicated():
    for pool in POOLS.values():
        keys = [item.key for item in pool.fields]
        assert len(keys) == len(set(keys)), pool.key
        assert len(set(pool.columns)) == len(pool.columns), pool.key
    # 涨停统计只请求一次，但提供「涨停统计」与「连板数」两个视角
    assert [item.column for item in LIMIT_UP.fields].count("zttj") == 1
    assert LIMIT_UP.sort_field == "fbt" and LIMIT_UP.sort_order == "asc"
    assert LIMIT_DOWN.sort_order == "desc"


def test_every_pool_has_a_distinct_upstream_path():
    paths = [pool.path for pool in POOLS.values()]
    assert len(paths) == len(set(paths))
    assert all(path.startswith("/getTopic") for path in paths)


# ──────── 行解析 ────────


def test_parse_pool_maps_declared_fields():
    row = _row(
        LIMIT_UP,
        c="000993",
        n="闽东电力",
        p=13880,
        zdp=9.984,
        amount=664912544.0,
        hs=10.47,
        fund=135527678.0,
        fbt=92500,
        lbt=93236,
        zbc=1,
        zttj={"days": 3, "ct": 3},
        lbc=3,
        ltsz=6356366195.0,
        tshare=6356366209.0,
        hybk="电力",
    )
    parsed = _parse_pool(LIMIT_UP, [row])[0]
    assert parsed["symbol"] == "000993"
    assert parsed["name"] == "闽东电力"
    # 价格字段是「元 × 1000」
    assert parsed["price"] == pytest.approx(13.88)
    assert parsed["seal_amount"] == pytest.approx(135527678.0)
    assert parsed["first_seal_time"] == "09:25:00"
    assert parsed["last_seal_time"] == "09:32:36"
    assert parsed["limit_up_stat"] == "3天3板"
    assert parsed["boards"] == 3
    assert parsed["industry"] == "电力"


def test_parse_pool_returns_empty_for_no_rows():
    assert _parse_pool(LIMIT_UP, []) == []


def test_parse_pool_rejects_missing_upstream_column():
    """上游改名/删列必须立即报错，而不是静默返回空字段。"""
    row = _row(LIMIT_UP)
    del row["hybk"]
    with pytest.raises(EastmoneyLimitUpError, match="hybk"):
        _parse_pool(LIMIT_UP, [row])


def test_placeholder_price_becomes_none():
    """上市首日等无涨跌幅限制场景，上游用 1e9 占位，不能当成 1000000 元。"""
    parsed = _parse_pool(SUB_NEW, [_row(SUB_NEW, ztp=1_000_000_000)])[0]
    assert parsed["limit_up_price"] is None
    real = _parse_pool(SUB_NEW, [_row(SUB_NEW, ztp=28_540)])[0]
    assert real["limit_up_price"] == pytest.approx(28.54)


def test_clock_parser_handles_zero_and_missing():
    parsed = _parse_pool(LIMIT_UP, [_row(LIMIT_UP, fbt=0, lbt=None)])[0]
    assert parsed["first_seal_time"] == ""
    assert parsed["last_seal_time"] == ""


def test_limit_up_stat_is_blank_without_streak():
    parsed = _parse_pool(STRONG, [_row(STRONG, zttj={"days": 0, "ct": 0})])[0]
    assert parsed["limit_up_stat"] == ""
    # 上游偶尔给字符串，原样返回即可
    text = _parse_pool(STRONG, [_row(STRONG, zttj="2天2板")])[0]
    assert text["limit_up_stat"] == "2天2板"


def test_yes_no_and_date_parsers():
    row = _row(SUB_NEW, ztf="0", nh="1", ipod=20260911)
    parsed = _parse_pool(SUB_NEW, [row])[0]
    assert parsed["is_limit_up"] == "否"
    assert parsed["is_new_high"] == "是"
    assert parsed["listed_date"] == "2026-09-11"


# ──────── 服务查询 ────────


async def test_query_returns_parsed_pool_and_metadata():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=_payload([_row(LIMIT_UP, c="000993", n="闽东电力")], total=40)
        )

    service = _service(handler)
    try:
        result = await service.query("limit-up", limit=3)
    finally:
        await service.close()

    assert result.pool is LIMIT_UP
    assert result.trade_date == "2026-09-11"
    assert result.total == 40
    assert result.page == 1
    assert len(result.items) == 1
    assert result.items[0]["symbol"] == "000993"

    request = seen[0]
    assert request.url.host == "push2ex.eastmoney.com"
    assert request.url.path == "/getTopicZTPool"
    assert request.url.params["dpt"] == "wz.ztzt"
    assert request.url.params["pagesize"] == "3"
    assert request.url.params["Pageindex"] == "0"
    assert request.url.params["sort"] == "fbt:asc"
    # 上游忽略 date，但我们仍按东财页面的约定传当日日期
    assert len(request.url.params["date"]) == 8


async def test_query_paginates_and_clamps_page_size():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload([]))

    service = _service(handler)
    try:
        result = await service.query("strong", limit=9999, page=3, order="asc")
    finally:
        await service.close()

    assert seen[0].url.params["pagesize"] == "200"
    assert seen[0].url.params["Pageindex"] == "2"
    assert seen[0].url.params["sort"] == "zdp:asc"
    assert result.page == 3


async def test_query_uses_pool_default_order_when_unspecified():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload([]))

    service = _service(handler)
    try:
        await service.query("broken-board")
    finally:
        await service.close()
    assert seen[0].url.params["sort"] == "fbt:asc"


async def test_query_treats_missing_data_as_empty_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rc": 0, "data": None})

    service = _service(handler)
    try:
        result = await service.query("limit-down")
    finally:
        await service.close()
    assert result.items == []
    assert result.total == 0
    assert result.trade_date == ""


async def test_query_raises_when_upstream_reports_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rc": 1, "data": None})

    service = _service(handler)
    try:
        with pytest.raises(Exception, match="未返回所需数据"):
            await service.query("limit-up")
    finally:
        await service.close()


async def test_query_rejects_non_list_pool_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rc": 0, "data": {"pool": {"c": "000993"}}})

    service = _service(handler)
    try:
        with pytest.raises(EastmoneyLimitUpError, match="非预期的数据结构"):
            await service.query("limit-up")
    finally:
        await service.close()


async def test_query_rejects_unknown_pool():
    service = _service(lambda request: httpx.Response(200, json=_payload([])))
    try:
        with pytest.raises(ValueError, match="未知情绪池"):
            await service.query("bogus")
    finally:
        await service.close()


def test_keys_and_pool_lookup():
    assert EastmoneyLimitUpService.keys() == list(POOLS)
    assert EastmoneyLimitUpService.pool("LIMIT-UP") is LIMIT_UP
    assert EastmoneyLimitUpService.pool(" broken-board ") is BROKEN_BOARD
