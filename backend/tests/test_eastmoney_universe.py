"""东方财富股票池 Provider 测试。"""
from datetime import date

import httpx
import pytest

from app.universe.eastmoney_universe import (
    EastmoneyUniverseProvider,
    classify_exchange,
    is_bond_row,
    resolve_trading_status,
)
from app.universe.providers import ProviderError, _is_delisted_name


def test_classify_exchange_covers_three_markets():
    assert classify_exchange("600519") == "SH"
    assert classify_exchange("688981") == "SH"
    assert classify_exchange("000001") == "SZ"
    assert classify_exchange("300750") == "SZ"
    assert classify_exchange("302132") == "SZ"
    # 920xxx 是北交所新号段，不能因为 9 开头判成沪市
    assert classify_exchange("920000") == "BJ"
    assert classify_exchange("830799") == "BJ"
    assert classify_exchange("430047") == "BJ"
    # 非 A 股（基金/债券/指数）返回空
    assert classify_exchange("510300") == ""
    assert classify_exchange("113050") == ""
    assert classify_exchange("810011") == ""


def test_is_bond_row_filters_bj_convertible_bonds():
    assert is_bond_row("810011", "张行转债") is True
    assert is_bond_row("820001", "某某转") is True
    assert is_bond_row("920000", "晨光电缆") is False
    assert is_bond_row("600519", "贵州茅台") is False


def test_is_delisted_name_covers_all_three_conventions():
    """退市简称的三种写法都要识别。

    回归：原来只认「退市…」/「退…」，漏掉退市整理期的「…退」，实测有 92 只
    这类退市股（国华退 / 康得退 / 东海A退…）因此留在股票池显示「可交易」。
    """
    assert _is_delisted_name("国华退") is True
    assert _is_delisted_name("康得退") is True
    assert _is_delisted_name("东海A退") is True
    assert _is_delisted_name("退市大集") is True
    assert _is_delisted_name("PT水仙") is True
    assert _is_delisted_name("pt金田A") is True
    # 正常公司与 ST（ST 由独立规则处理，不能误判成退市）
    assert _is_delisted_name("贵州茅台") is False
    assert _is_delisted_name("ST星源") is False
    assert _is_delisted_name("") is False
    assert _is_delisted_name("   ") is False


def test_resolve_trading_status_maps_all_f292_codes():
    """f292 实测只有 13/6/7/9 四种取值，全部要有确定映射。

    交叉验证方式：通达信日线 —— 13 的最后一根日线就是最近交易日；
    6 的停在停牌前；7 / 9 整段无日线。
    """
    assert resolve_trading_status(13, "浦发银行") == "active"
    assert resolve_trading_status(6, "新华传媒") == "suspended"
    assert resolve_trading_status(7, "ST星源") == "delisted"
    assert resolve_trading_status(9, "力勤资源") == "pending_listing"


def test_resolve_trading_status_falls_back_when_f292_missing():
    """f292 缺失时退回名称启发式，行为与修复前一致。"""
    assert resolve_trading_status(None, "国华退") == "delisted"
    assert resolve_trading_status(None, "退市大集") == "delisted"
    assert resolve_trading_status(None, "PT水仙") == "delisted"
    assert resolve_trading_status(None, "贵州茅台") == "active"
    # 字符串数字也要能解析（fltt 变化时东财会返回字符串）
    assert resolve_trading_status("7", "ST星源") == "delisted"
    # 未知取值不猜：宁可多留，也不静默剔除
    assert resolve_trading_status(42, "贵州茅台") == "active"
    assert resolve_trading_status("", "贵州茅台") == "active"


def test_resolve_trading_status_name_wins_over_f292():
    """退市整理期名称一定带「退」；f292 偶尔晚一拍，因此名称优先。"""
    assert resolve_trading_status(13, "国华退") == "delisted"


def test_to_records_marks_status_from_f292():
    provider = EastmoneyUniverseProvider()
    rows = [
        {**_row("600519", "贵州茅台", "酿酒行业"), "f292": 13},
        {**_row("000005", "ST星源", "房地产"), "f292": 7},
        {**_row("600825", "新华传媒", "文化传媒"), "f292": 6},
        {**_row("001246", "力勤资源", "-", listed=None), "f292": 9},
    ]

    records = provider._to_records(rows, date(2026, 9, 11))

    assert {r.symbol: r.trading_status for r in records} == {
        "600519": "active",
        "000005": "delisted",
        "600825": "suspended",
        "001246": "pending_listing",
    }


def _row(code: str, name: str = "", sector: str = "银行", listed: int = 20100101):
    return {"f12": code, "f14": name or f"股票{code}", "f26": listed, "f100": sector}


def test_to_records_maps_sector_board_and_st():
    provider = EastmoneyUniverseProvider()
    records = provider._to_records(
        [
            _row("600519", "贵州茅台", "酿酒行业"),
            _row("920000", "晨光电缆", "-"),
            _row("810011", "张行转债", "-"),
            _row("510300", "沪深300ETF", "-"),
            _row("000005", "ST星源", "房地产"),
        ],
        date(2026, 9, 11),
    )
    by_symbol = {record.symbol: record for record in records}

    assert set(by_symbol) == {"600519", "920000", "000005"}
    assert by_symbol["600519"].exchange == "SH"
    assert by_symbol["600519"].board == "main"
    assert by_symbol["600519"].sector == "酿酒行业"
    assert by_symbol["600519"].listing_date == date(2010, 1, 1)
    assert by_symbol["600519"].as_of_date == date(2026, 9, 11)
    assert by_symbol["920000"].exchange == "BJ"
    assert by_symbol["920000"].board == "bj"
    assert by_symbol["920000"].sector is None  # '-' 视为无行业
    assert by_symbol["000005"].is_st is True


@pytest.mark.asyncio
async def test_fetch_all_rejects_historical_as_of_date():
    """东财只有当前名单，历史日期必须显式拒绝（避免幸存者偏差）。"""
    provider = EastmoneyUniverseProvider(as_of_date=date(2020, 1, 1))
    with pytest.raises(ProviderError) as excinfo:
        await provider.fetch_all()
    assert "不支持历史快照" in str(excinfo.value)


def _market_rows() -> list[dict]:
    """构造一份过得了全市场质量门槛的沪深京样本（3500 只 + 1 只转债）。"""
    rows: list[dict] = []
    for index in range(1400):
        rows.append(_row(f"{600000 + index:06d}", f"沪股{index}", "综合"))
    for index in range(1, 1701):
        rows.append(_row(f"{index:06d}", f"深股{index}", "综合"))
    for index in range(400):
        rows.append(_row(f"{920000 + index:06d}", f"北股{index}", "-"))
    rows.append(_row("810011", "张行转债", "-"))
    return rows


def _transport(rows: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        page_number = int(params["pn"])
        size = int(params["pz"])
        start = (page_number - 1) * size
        return httpx.Response(
            200,
            json={"data": {"total": len(rows), "diff": rows[start : start + size]}},
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_all_pages_and_passes_market_coverage():
    rows = _market_rows()
    provider = EastmoneyUniverseProvider(
        transport=_transport(rows), page_size=100, max_concurrency=4
    )
    records = await provider.fetch_all()

    assert len(records) == 3500  # 可转债被过滤掉
    by_exchange: dict[str, int] = {}
    for record in records:
        by_exchange[record.exchange] = by_exchange.get(record.exchange, 0) + 1
    assert by_exchange == {"SH": 1400, "SZ": 1700, "BJ": 400}
    assert all(record.sector == "综合" for record in records if record.exchange != "BJ")


@pytest.mark.asyncio
async def test_fetch_all_fails_on_shrunk_market():
    """数据源缩量（只有 10 行）必须抛 ProviderError，不允许落库。"""
    rows = [_row(f"{600000 + index:06d}") for index in range(10)]
    provider = EastmoneyUniverseProvider(transport=_transport(rows))
    with pytest.raises(ProviderError) as excinfo:
        await provider.fetch_all()
    assert "质量门槛" in str(excinfo.value)
