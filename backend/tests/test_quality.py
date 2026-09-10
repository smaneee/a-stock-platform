"""历史数据质量检查测试。"""
from datetime import datetime

from app.history.quality import DataQualityChecker
from app.market_data.base import QuoteData


def _bar(symbol="600000", price=10.0, open=9.9, high=10.2, low=9.8, volume=1000, t="2026-01-05 00:00:00"):
    return QuoteData(
        symbol=symbol,
        name=symbol,
        price=price,
        open=open,
        high=high,
        low=low,
        previous_close=0.0,
        volume=volume,
        amount=10000.0,
        source="akshare",
        market_time=datetime.fromisoformat(t),
    )


def test_clean_bars():
    checker = DataQualityChecker()
    bars = [_bar(t="2026-01-05"), _bar(t="2026-01-06"), _bar(t="2026-01-07")]
    report = checker.check(bars)
    assert report.is_clean is True
    assert report.issue_codes == set()


def test_duplicate_time():
    checker = DataQualityChecker()
    bars = [_bar(t="2026-01-05"), _bar(t="2026-01-05")]
    report = checker.check(bars)
    assert "duplicate_time" in report.issue_codes


def test_time_reversal():
    checker = DataQualityChecker()
    bars = [_bar(t="2026-01-06"), _bar(t="2026-01-05")]
    report = checker.check(bars)
    assert "time_reversal" in report.issue_codes


def test_missing_field():
    checker = DataQualityChecker()
    bars = [_bar(price=0, open=0, high=0, low=0, t="2026-01-05")]
    report = checker.check(bars)
    assert "missing_field" in report.issue_codes


def test_ohlc_invalid_high_low():
    checker = DataQualityChecker()
    # 最高 < 最低
    bars = [_bar(high=9.0, low=10.0, t="2026-01-05")]
    report = checker.check(bars)
    assert "ohlc_invalid" in report.issue_codes


def test_ohlc_invalid_close_out_of_range():
    checker = DataQualityChecker()
    # 收盘价超出 [低, 高]
    bars = [_bar(price=11.0, high=10.2, low=9.8, t="2026-01-05")]
    report = checker.check(bars)
    assert "ohlc_invalid" in report.issue_codes


def test_negative_volume():
    checker = DataQualityChecker()
    bars = [_bar(volume=-100, t="2026-01-05")]
    report = checker.check(bars)
    assert "negative_volume" in report.issue_codes
