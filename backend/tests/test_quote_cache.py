"""行情缓存测试。"""
from datetime import datetime, timedelta

from app.realtime.quote_cache import QuoteCache

from tests.helpers import make_quote


def test_update_and_get_latest():
    cache = QuoteCache(window_size=10)
    quote = make_quote(symbol="600000", price=10.0)
    cache.update(quote)
    assert cache.get_latest("600000").price == 10.0


def test_rolling_window_trim():
    """窗口大小限制：只保留最近 N 根。"""
    cache = QuoteCache(window_size=5)
    start = datetime.utcnow()
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
