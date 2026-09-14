"""前复权收益口径 + 未复权涨跌停昨收的测试（P0-05 的 D6）。

研发计划 §7.4：「除权日：未复权涨跌停判断与前复权策略收益口径各自正确，且不可互用。」

原状态：整条回测链路都用未复权日线 —— 涨跌停判断对，但策略输入与收益口径错
（除权跳空被当成真实亏损），库内 660 万行前复权日线完全未被使用。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.history.limit_reference import (
    attach_unadjusted_previous_close,
    unadjusted_previous_close_map,
)
from app.market_data.base import QuoteData


def _bar(symbol: str, day: date, price: float) -> QuoteData:
    return QuoteData(
        symbol=symbol,
        name="测试股",
        price=price,
        open=price,
        high=price,
        low=price,
        previous_close=0.0,
        volume=1_000_000.0,
        amount=price * 1_000_000.0,
        market_time=datetime(day.year, day.month, day.day, 9, 30),
    )


def _days(n: int) -> list[date]:
    start = date(2026, 1, 5)
    return [start + timedelta(days=i) for i in range(n)]


def test_previous_close_map_uses_previous_bar_close():
    days = _days(3)
    mapping = unadjusted_previous_close_map(
        [_bar("600000", days[0], 10.0), _bar("600000", days[1], 11.0), _bar("600000", days[2], 12.0)]
    )
    assert mapping[days[0]] == 0.0   # 首根没有更早数据
    assert mapping[days[1]] == 10.0
    assert mapping[days[2]] == 11.0


def test_attach_uses_unadjusted_close_not_adjusted():
    """除权日场景：前复权序列的昨收 ≠ 未复权昨收，必须取未复权的那一个。"""
    days = _days(3)
    # 未复权：10 → 10 → 5（第二天除权，价格腰斩）
    unadjusted = [
        _bar("600000", days[0], 10.0),
        _bar("600000", days[1], 10.0),
        _bar("600000", days[2], 5.0),
    ]
    # 前复权：整段被因子 0.5 归一化（历史价格减半，最新日不变）
    adjusted = [
        _bar("600000", days[0], 5.0),
        _bar("600000", days[1], 5.0),
        _bar("600000", days[2], 5.0),
    ]
    merged, missing = attach_unadjusted_previous_close(adjusted, unadjusted)
    assert missing == 0
    # 除权日（第三根）必须用未复权昨收 10.0，而不是前复权昨收 5.0
    assert merged[2].previous_close == 10.0
    assert merged[0].previous_close == 0.0
    assert merged[1].previous_close == 10.0
    # 价格本身保持前复权（策略与收益口径）
    assert [bar.price for bar in merged] == [5.0, 5.0, 5.0]


def test_attach_reports_missing_when_no_reference():
    days = _days(2)
    adjusted = [_bar("600000", days[0], 5.0), _bar("600000", days[1], 5.0)]
    merged, missing = attach_unadjusted_previous_close(adjusted, [])
    assert missing == 2
    assert all(bar.previous_close == 0.0 for bar in merged), "对不齐时必须保持 0，不能猜"


def test_attach_reports_partial_missing():
    days = _days(3)
    unadjusted = [_bar("600000", days[0], 10.0), _bar("600000", days[1], 10.0)]
    adjusted = [_bar("600000", d, 5.0) for d in days]
    merged, missing = attach_unadjusted_previous_close(adjusted, unadjusted)
    assert missing == 1                      # 第三根在未复权序列里没有
    assert merged[2].previous_close == 0.0
    assert merged[1].previous_close == 10.0


def test_attach_handles_unsorted_input():
    days = _days(3)
    unadjusted = [
        _bar("600000", days[2], 12.0),
        _bar("600000", days[0], 10.0),
        _bar("600000", days[1], 11.0),
    ]
    mapping = unadjusted_previous_close_map(unadjusted)
    assert mapping[days[1]] == 10.0
    assert mapping[days[2]] == 11.0


def test_default_backtest_adjust_is_qfq():
    """默认口径必须是前复权；涨跌停另走未复权昨收，因此不会失真。"""
    from app.config import Settings

    assert Settings().portfolio_bars_adjust == "qfq"


# ───────────── 口径必须透出到结果（否则下游会误读） ─────────────


def test_return_convention_meta_follows_actual_adjust():
    """`return_convention_meta` 必须随真实口径变化，不能写死 none。"""
    from app.backtest.metrics import return_convention_meta

    qfq = return_convention_meta("qfq")
    assert qfq["bars_adjust"] == "qfq"
    assert qfq["bars_adjust_label"] == "前复权"
    # 前复权收益下，涨跌停判定仍必须声明用的是未复权昨收
    assert qfq["limit_reference"] == "unadjusted_previous_close"
    assert "除权除息跳空已在本地还原" in qfq["return_convention_note"]

    none = return_convention_meta("none")
    assert none["bars_adjust"] == "none"
    assert none["bars_adjust_label"] == "不复权"
    assert none["limit_reference"] == "none"
    assert "未复权口径" in none["return_convention_note"]

    # 未指定时保持既有默认（不改变老调用方的行为）
    assert return_convention_meta()["bars_adjust"] == "none"


def test_portfolio_result_meta_reports_configured_adjust():
    """组合结果里的 bars_adjust 必须来自实际配置，而不是常量。"""
    from app.portfolio.engine import PortfolioResult

    payload = PortfolioResult(equity_curve=[100.0], bars_adjust="qfq").to_dict()
    assert payload["bars_adjust"] == "qfq"
    assert payload["bars_adjust_label"] == "前复权"
    assert payload["limit_reference"] == "unadjusted_previous_close"

    default = PortfolioResult(equity_curve=[100.0]).to_dict()
    assert default["bars_adjust"] == "none"


def test_qfq_is_available_for_the_stored_universe():
    """只读校验：库里前复权与未复权的标的/日期覆盖必须一致，否则不能安全切换。"""
    import sqlite3
    from pathlib import Path

    db = Path(r"E:\workbuddy work\2026-09-09-15-34-54\a-stock-platform\backend\a_stock.db")
    if not db.exists():
        pytest.skip("主库不存在（仅在本机校验用）")
    uri = "file:" + str(db).replace("\\", "/").replace(" ", "%20") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        qfq_symbols, qfq_last = con.execute(
            "select count(distinct symbol), max(trade_date) from historical_bars"
            " where period='daily' and adjust='qfq'"
        ).fetchone()
        none_symbols, none_last = con.execute(
            "select count(distinct symbol), max(trade_date) from historical_bars"
            " where period='daily' and adjust='none'"
        ).fetchone()
    finally:
        con.close()
    assert qfq_symbols == none_symbols, "前复权与未复权的标的数必须一致"
    assert qfq_last == none_last, "前复权与未复权的最后交易日必须一致"
