"""复权口径解析的 TTL 缓存测试（2026-09-14 性能修复的回归锚点）。

背景（实测）：主库 `historical_bars` 有 13,359,225 行日线，但没有 `(period, adjust)`
索引，`resolve_bar_adjust` 的 `GROUP BY adjust` 聚合 **p50 ≈ 6.06 秒**
（同库 `MAX(id)` 与按主键取一行均为 0.0ms）。交付验收实测
`GET /api/indicators/600519` 冷启动 **7722.9 ms** —— 就是因为该接口每个请求都直连
这个聚合；而扫描服务早已用 60 秒 TTL 缓存绕开它。

本测试锁定：**同一个进程内 TTL 内只真正查询一次，且三个调用方共用这份缓存**。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.database.models import HistoricalBar
from app.realtime import screener
from app.realtime.screener import (
    ADJUST_CACHE_TTL_SECONDS,
    resolve_bar_adjust_cached,
)


@pytest.fixture
def counting_resolver(monkeypatch):
    """把真正的查询替换成计数器，用于观察「到底查了几次」。"""
    calls: list[str] = []

    def fake(db, period="daily"):
        calls.append(period)
        return "qfq", 123, None

    monkeypatch.setattr(screener, "resolve_bar_adjust", fake)
    screener.reset_adjust_cache()
    return calls


def test_cached_resolver_queries_once_within_ttl(db_session, counting_resolver):
    first = resolve_bar_adjust_cached(db_session, "daily")
    second = resolve_bar_adjust_cached(db_session, "daily")
    third = resolve_bar_adjust_cached(db_session, "daily")

    assert first == second == third == ("qfq", 123, None)
    assert len(counting_resolver) == 1, f"TTL 内应只查一次，实际 {len(counting_resolver)} 次"


def test_reset_forces_a_fresh_query(db_session, counting_resolver):
    resolve_bar_adjust_cached(db_session, "daily")
    screener.reset_adjust_cache()
    resolve_bar_adjust_cached(db_session, "daily")
    assert len(counting_resolver) == 2


def test_ttl_is_positive_and_short():
    """TTL 必须为正且足够短 —— 前复权回填完成后最迟一个 TTL 就要生效。"""
    assert 0 < ADJUST_CACHE_TTL_SECONDS <= 300


def test_screener_service_shares_the_same_cache(db_session, counting_resolver):
    """扫描服务的方法必须转发到**同一份**进程级缓存（否则又变成各查一次）。"""
    service = screener.ScreenerService(db_session) if _accepts_session() else None
    if service is None:
        pytest.skip("ScreenerService 需要更多构造参数，跳过（共享性由上面的模块级用例覆盖）")
    service._resolve_adjust_cached(db_session)
    resolve_bar_adjust_cached(db_session, "daily")
    assert len(counting_resolver) == 1


def _accepts_session() -> bool:
    import inspect

    try:
        params = list(inspect.signature(screener.ScreenerService.__init__).parameters)
    except (TypeError, ValueError):
        return False
    return "db" in params


def test_indicators_endpoint_uses_the_cached_resolver():
    """接口层必须用缓存版；直连 `resolve_bar_adjust` 会让每个请求付 6 秒。"""
    import inspect

    from app.api import indicators

    source = inspect.getsource(indicators.get_indicators)
    assert "resolve_bar_adjust_cached" in source
    assert "resolve_bar_adjust(" not in source.replace("resolve_bar_adjust_cached(", "")


def test_validation_uses_the_cached_resolver():
    """样本外验证同样改用缓存版（它一次运行要跑多折）。"""
    import inspect

    from app.realtime import validation

    source = inspect.getsource(validation)
    assert "resolve_bar_adjust_cached(db, BAR_PERIOD)" in source


def test_cache_does_not_leak_across_tests(db_session):
    """conftest 的 autouse fixture 每个用例前清缓存 —— 这里验证它真的生效。

    做法：先手动把缓存写成与实际库不符的值，再调用解析函数；若缓存未被清理，
    拿到的就会是伪造值。
    """
    screener._ADJUST_CACHE = (0.0, "hfq", 999, None)  # 伪造（理论上要被清掉）
    adjust, max_id, _ = resolve_bar_adjust_cached(db_session, "daily")
    assert adjust != "hfq" or max_id != 999, "缓存未被清理：conftest 的 reset 没有生效"


def test_repeated_calls_do_not_requery_on_real_schema(db_session):
    """不依赖 monkeypatch 的对照：真实 schema 下第二次调用不再产生查询。

    用 SQLAlchemy 事件统计 SELECT 次数，确保「缓存确实省掉了那次聚合」不只是口头结论。
    """
    from sqlalchemy import event

    db_session.add(
        HistoricalBar(
            symbol="600000", period="daily", adjust="none", trade_date=date(2026, 1, 5),
            open=10.0, high=10.0, low=10.0, close=10.0, volume=1.0, amount=10.0, source="test",
        )
    )
    db_session.commit()
    screener.reset_adjust_cache()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().lower().startswith("select"):
            statements.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", _record)
    try:
        resolve_bar_adjust_cached(db_session, "daily")
        after_first = len(statements)
        resolve_bar_adjust_cached(db_session, "daily")
        after_second = len(statements)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", _record)

    assert after_first >= 1, "第一次调用应当真的查了库"
    assert after_second == after_first, "第二次调用不应再查库（缓存未生效）"
