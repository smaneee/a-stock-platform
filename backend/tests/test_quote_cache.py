"""行情缓存测试。"""
from datetime import timedelta

from app.realtime.quote_cache import QuoteCache
from app.time_utils import utc_now

from tests.helpers import make_quote


def test_update_and_get_latest():
    cache = QuoteCache(window_size=10)
    quote = make_quote(symbol="600000", price=10.0)
    cache.update(quote)
    assert cache.get_latest("600000").price == 10.0


def test_rolling_window_trim():
    """窗口大小限制：只保留最近 N 根。"""
    cache = QuoteCache(window_size=5)
    start = utc_now()
    for i in range(10):
        cache.update(make_quote(symbol="600000", price=float(i), market_time=start + timedelta(minutes=i)))
    assert cache.window_size("600000") == 5
    window = cache.get_window("600000")
    assert window[0].price == 5.0  # 最早的被裁掉
    assert window[-1].price == 9.0


def test_get_latest_many():
    cache = QuoteCache(window_size=10)
    cache.update(make_quote(symbol="600000", price=10.0))
    cache.update(make_quote(symbol="000001", price=20.0))
    result = cache.get_latest_many(["600000", "000001", "999999"])
    assert "600000" in result
    assert "000001" in result
    assert "999999" not in result


def test_snapshots_in_same_minute_are_aggregated():
    """三秒快照不能被误当成三根分钟 K 线。"""
    start = utc_now().replace(second=1, microsecond=0)
    cache = QuoteCache(window_size=10)
    first = make_quote(symbol="600000", price=10.0, volume=1_000, market_time=start)
    second = make_quote(
        symbol="600000",
        price=10.2,
        volume=1_500,
        market_time=start + timedelta(seconds=3),
    )
    cache.update(first)
    cache.update(second)

    window = cache.get_window("600000")
    assert len(window) == 1
    assert window[0].open == 10.0
    assert window[0].price == 10.2
    assert window[0].high == 10.2
    assert window[0].volume == 500
