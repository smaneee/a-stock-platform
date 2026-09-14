"""买点雷达覆盖率门禁测试（P0-02/P0-04）。

背景（2026-09-14 盘前实测）：`/api/realtime/picks` 在 5550 只标的里只取到
**5 只**行情（覆盖率 0.09%），却仍然返回 200 并花 10~21 秒产出候选排序。
研发计划要求「覆盖率不足拒绝给出结论」，因此新增 `coverage_status` 判定。
"""
from __future__ import annotations

import pytest

from app.realtime.screener import MIN_COVERAGE_RATIO, coverage_status


def test_coverage_ok_when_full():
    ratio, ok, note = coverage_status(5550, 5550)
    assert ok is True
    assert ratio == 1.0
    assert "100.0%" in note


def test_coverage_ok_at_threshold():
    quoted = int(5550 * MIN_COVERAGE_RATIO)
    ratio, ok, _ = coverage_status(quoted, 5550)
    assert ok is True
    assert ratio >= MIN_COVERAGE_RATIO


@pytest.mark.parametrize("quoted", [0, 1, 5, 100, 2000])
def test_coverage_rejected_below_threshold(quoted):
    ratio, ok, note = coverage_status(quoted, 5550)
    assert ok is False
    assert ratio < MIN_COVERAGE_RATIO
    assert "不足以支撑候选排序结论" in note
    assert "不得作为研究依据" in note


def test_premarket_regression_case_is_flagged():
    """真实回归用例：盘前 5/5550 必须被标记为覆盖率不足。"""
    ratio, ok, note = coverage_status(5, 5550)
    assert ok is False
    assert ratio < 0.001
    assert "5/5550" in note


def test_empty_universe_is_rejected_not_divided_by_zero():
    ratio, ok, note = coverage_status(0, 0)
    assert ok is False
    assert ratio == 0.0
    assert "股票池为空" in note


def test_api_exposes_coverage_fields():
    """接口契约：coverage_ratio / coverage_ok 必须在响应里（前端据此拒绝展示结论）。"""
    from app.api.realtime import ScreenerResponse

    fields = ScreenerResponse.model_fields
    assert "coverage_ratio" in fields
    assert "coverage_ok" in fields
    assert "覆盖率" in fields["coverage_ok"].description


# ───────────── 扫描合并（single-flight） ─────────────
# 2026-09-14 连续竞价实测：全市场扫描 P50 32.8s / P95 66.3s，而首页默认 60 秒
# 自动刷新。不合并时并发请求会各自再跑一遍全市场扫描，互相拖慢并放大数据源压力。


@pytest.mark.asyncio
async def test_concurrent_same_config_scans_are_coalesced(monkeypatch):
    import asyncio

    from app.realtime.screener import ScreenerService

    service = ScreenerService()
    calls = 0

    async def fake_run_scan(cfg):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return f"scan-{calls}"

    monkeypatch.setattr(service, "_run_scan", fake_run_scan)
    results = await asyncio.gather(service.run(), service.run(), service.run())

    assert calls == 1, "同参数的并发扫描必须只执行一次"
    assert results == ["scan-1"] * 3, "并发调用应拿到同一次扫描的结果"
    assert service.coalesced_scans == 2


@pytest.mark.asyncio
async def test_different_configs_are_not_coalesced(monkeypatch):
    """参数不同不能复用别人的扫描结果（否则会返回错误参数下的排序）。"""
    import asyncio

    from app.realtime.screener import ScreenerConfig, ScreenerService

    service = ScreenerService()
    seen: list[int] = []

    async def fake_run_scan(cfg):
        seen.append(cfg.top_n)
        await asyncio.sleep(0.02)
        return f"top{cfg.top_n}"

    monkeypatch.setattr(service, "_run_scan", fake_run_scan)
    results = await asyncio.gather(
        service.run(ScreenerConfig(top_n=5)), service.run(ScreenerConfig(top_n=9))
    )

    assert sorted(seen) == [5, 9]
    assert set(results) == {"top5", "top9"}
    assert service.coalesced_scans == 0


@pytest.mark.asyncio
async def test_inflight_slot_released_after_scan(monkeypatch):
    """扫描结束后必须释放合并槽位，否则后续扫描会被误判为「进行中」。"""
    from app.realtime.screener import ScreenerService

    service = ScreenerService()

    async def fake_run_scan(cfg):
        return "ok"

    monkeypatch.setattr(service, "_run_scan", fake_run_scan)
    assert await service.run() == "ok"
    assert service._inflight == {}
    assert await service.run() == "ok"


# ───────────── 复权口径/缓存键 TTL 缓存（2026-09-14 盘中优化） ─────────────


def test_resolve_adjust_is_cached_within_ttl(monkeypatch):
    """`GROUP BY adjust` 聚合在 1335 万行上要数秒，不能在每次扫描都跑。"""
    from datetime import date

    from app.realtime import screener as screener_module

    calls = {"n": 0}

    def fake_resolve(db, period="daily"):
        calls["n"] += 1
        return "qfq", 14685046, date(2026, 9, 11)

    monkeypatch.setattr(screener_module, "resolve_bar_adjust", fake_resolve)
    service = screener_module.ScreenerService()

    first = service._resolve_adjust_cached(None)
    second = service._resolve_adjust_cached(None)
    assert first == second == ("qfq", 14685046, date(2026, 9, 11))
    assert calls["n"] == 1, "TTL 内应复用解析结果"


def test_resolve_adjust_refreshes_after_ttl(monkeypatch):
    from datetime import date

    from app.realtime import screener as screener_module

    calls = {"n": 0}

    def fake_resolve(db, period="daily"):
        calls["n"] += 1
        return "qfq", 100 + calls["n"], date(2026, 9, 11)

    monkeypatch.setattr(screener_module, "resolve_bar_adjust", fake_resolve)
    monkeypatch.setattr(screener_module, "ADJUST_CACHE_TTL_SECONDS", 0.0)
    service = screener_module.ScreenerService()

    service._resolve_adjust_cached(None)
    service._resolve_adjust_cached(None)
    assert calls["n"] == 2, "TTL 过期后必须重新解析（避免长期用错口径）"


@pytest.mark.asyncio
async def test_warm_bars_cache_swallows_errors(monkeypatch):
    """预热失败不能影响服务启动。"""
    from app.realtime.screener import ScreenerService

    service = ScreenerService()

    def boom(lookback):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "_load_series_in_thread", boom)
    elapsed = await service.warm_bars_cache()
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0
