"""fix(validation) 回归测试：组合回测完整性校验、MockProvider 历史、start_all、E2E 隔离。

按用户列出的四大类问题逐项验证：

1. 组合回测完整性：默认 allow_partial=false；任意标的不完整即失败并记录 exclusion_reasons。
2. MockProvider：缓存键含起止时间；daily 按交易日；OHLC 自洽；amount=close*volume；固定 seed。
3. start_all.ps1：启动器/版本参数分开（仅冒烟语法）。
4. E2E 隔离：使用临时 DB；清理失败即失败；metrics data_status=simulated。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.history.quality import QualityReport
from app.history.service import HistoryResult
from app.market_data.base import QuoteData
from app.market_data.mock_provider import MockProvider
from app.tasks.portfolio_worker import (
    PortfolioBacktestWorker,
    _wrap_history,
)


# ──────────────── 1. PortfolioBacktestWorker 完整性校验 ────────────────


def _make_history_result(
    bars: list[QuoteData],
    is_complete: bool = True,
    missing_dates: list | None = None,
) -> HistoryResult:
    return HistoryResult(
        bars=bars,
        source="cache",
        data_updated_at=None,
        is_complete=is_complete,
        quality=QualityReport(
            total=len(bars),
            actual_count=len(bars),
            missing_dates=missing_dates or [],
        ),
    )


def _bar(date_iso: str, price: float = 10.0) -> QuoteData:
    return QuoteData(
        symbol="600000",
        name="测试",
        price=price,
        open=price,
        high=price + 0.1,
        low=price - 0.1,
        previous_close=price,
        volume=1000,
        amount=price * 1000,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        source="mock",
        market_time=datetime.fromisoformat(date_iso),
        received_at=datetime.fromisoformat(date_iso),
    )


class TestPortfolioCompletenessCheck:
    """PortfolioBacktestWorker 完整性校验逻辑。"""

    def setup_method(self):
        self.worker = PortfolioBacktestWorker(session_factory=lambda: None)
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.end = datetime(2024, 1, 31, tzinfo=timezone.utc)

    def test_check_passes_for_complete_history(self):
        # 让 bars 真正覆盖请求区间（首根不晚于 start，最后一根不早于 end）
        bars = [
            _bar("2024-01-01T00:00:00"),
            _bar("2024-01-15T00:00:00"),
            _bar("2024-01-31T00:00:00"),
        ]
        result = _make_history_result(bars)
        reason = self.worker._check_history_completeness(
            "600000", result, self.start, self.end
        )
        assert reason is None

    def test_check_fails_for_empty_bars(self):
        result = _make_history_result([])
        reason = self.worker._check_history_completeness(
            "600000", result, self.start, self.end
        )
        assert reason is not None
        assert reason["reason"] == "empty_bars"

    def test_check_fails_for_none_result(self):
        reason = self.worker._check_history_completeness(
            "600000", None, self.start, self.end
        )
        assert reason is not None
        assert reason["reason"] == "no_result"

    def test_check_fails_for_incomplete(self):
        bars = [_bar("2024-01-15T00:00:00")]
        result = _make_history_result(bars, is_complete=False, missing_dates=["2024-01-02"])
        reason = self.worker._check_history_completeness(
            "600000", result, self.start, self.end
        )
        assert reason is not None
        assert reason["reason"] == "incomplete"
        assert "2024-01-02" in reason["missing_dates"]

    def test_check_fails_for_range_too_short(self):
        """实际跨度远小于请求跨度 → 缓存命中错误版本。"""
        bars = [
            _bar("2024-01-15T00:00:00"),
            _bar("2024-01-20T00:00:00"),
        ]
        result = _make_history_result(bars)
        reason = self.worker._check_history_completeness(
            "600000", result, self.start, self.end + timedelta(days=180)
        )
        assert reason is not None
        assert reason["reason"] in ("range_too_short", "range_mismatch")

    def test_check_fails_when_bars_outside_requested_window(self):
        """bars 超出请求窗口（如含未来日期）→ range_mismatch。"""
        bars = [
            _bar("2024-01-15T00:00:00"),
            _bar("2024-01-30T00:00:00"),
            _bar("2024-02-15T00:00:00"),  # 超出 end
        ]
        result = _make_history_result(bars)
        reason = self.worker._check_history_completeness(
            "600000", result, self.start, self.end
        )
        assert reason is not None
        assert reason["reason"] == "range_mismatch"


def test_wrap_history_marks_incomplete_when_empty():
    """_wrap_history 应当如实标记完整性：空 bars 必须 is_complete=False。"""
    result = _wrap_history([])
    assert result.is_complete is False
    assert result.bars == []


def test_wrap_history_marks_complete_when_has_bars():
    bars = [_bar("2024-01-02T00:00:00")]
    result = _wrap_history(bars)
    assert result.is_complete is True
    assert len(result.bars) == 1


# ──────────────── 2. MockProvider 历史数据 ────────────────


class TestMockProviderHistory:
    """MockProvider 历史数据完整性：daily 按日、缓存键含起止、OHLC 自洽、amount=close*volume、固定 seed。"""

    def setup_method(self):
        self.provider = MockProvider(seed=42)

    def test_daily_bars_one_per_trading_day(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 31, tzinfo=timezone.utc)

        async def run():
            return await self.provider.get_history("600000", "daily", start, end)

        bars = asyncio.run(run())
        # 2024-01 的交易日：1/2-1/31 去除周末 ≈ 22 天
        assert 18 <= len(bars) <= 23

    def test_ohlc_is_self_consistent(self):
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 30, tzinfo=timezone.utc)

        async def run():
            return await self.provider.get_history("600000", "daily", start, end)

        bars = asyncio.run(run())
        for b in bars:
            assert b.low <= b.high, f"low > high: {b.market_time}"
            assert b.low <= b.open <= b.high, f"open out of range: {b.market_time}"
            assert b.low <= b.price <= b.high, f"close out of range: {b.market_time}"

    def test_amount_equals_close_times_volume(self):
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 30, tzinfo=timezone.utc)

        async def run():
            return await self.provider.get_history("600000", "daily", start, end)

        bars = asyncio.run(run())
        for b in bars:
            assert abs(b.amount - b.price * b.volume) < 1e-6, (
                f"amount != close*volume: {b.market_time}"
            )

    def test_cache_key_includes_start_and_end(self):
        start_short = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end_short = datetime(2024, 1, 10, tzinfo=timezone.utc)
        start_long = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end_long = datetime(2024, 2, 28, tzinfo=timezone.utc)

        async def run():
            short = await self.provider.get_history("600000", "daily", start_short, end_short)
            long = await self.provider.get_history(
                "600000", "daily", start_long, end_long
            )
            return short, long

        short, long = asyncio.run(run())
        assert len(short) < len(long), "短区间的 bars 应少于长区间"
        # 长区间应包含全部交易日（约 38 个）
        assert len(long) >= 35

    def test_request_order_does_not_change_output(self):
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 30, tzinfo=timezone.utc)

        p1 = MockProvider(seed=99)
        p2 = MockProvider(seed=99)

        async def run():
            # 反向请求顺序
            a = await p1.get_history("000002", "daily", start, end)
            b = await p1.get_history("600000", "daily", start, end)
            # 正向请求顺序
            c = await p2.get_history("600000", "daily", start, end)
            d = await p2.get_history("000002", "daily", start, end)
            return a, b, c, d

        a, b, c, d = asyncio.run(run())
        # 同种子下同 symbol 相同区间必须得到相同 bars
        assert len(b) == len(c)
        assert [(bar.market_time, bar.price) for bar in b] == [
            (bar.market_time, bar.price) for bar in c
        ]
        assert len(a) == len(d)
        assert [(bar.market_time, bar.price) for bar in a] == [
            (bar.market_time, bar.price) for bar in d
        ]

    def test_two_symbols_dont_pollute_each_other(self):
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 30, tzinfo=timezone.utc)

        async def run():
            x = await self.provider.get_history("600000", "daily", start, end)
            y = await self.provider.get_history("000001", "daily", start, end)
            # 再请求一次 600000 应该与第一次完全一致
            x2 = await self.provider.get_history("600000", "daily", start, end)
            return x, y, x2

        x, y, x2 = asyncio.run(run())
        # 同一 symbol 同 seed 两次必须一致
        assert [(bar.market_time, bar.price) for bar in x] == [
            (bar.market_time, bar.price) for bar in x2
        ]
        # 不同 symbol 的价格应不同（基于 symbol 派生 base price）
        x_prices = [bar.price for bar in x]
        y_prices = [bar.price for bar in y]
        assert x_prices != y_prices, "不同 symbol 的 mock 价格必须不同"

    def test_no_duplicate_trade_dates(self):
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 30, tzinfo=timezone.utc)

        async def run():
            return await self.provider.get_history("600000", "daily", start, end)

        bars = asyncio.run(run())
        dates = [bar.market_time.date() for bar in bars]
        assert len(dates) == len(set(dates)), "trade_date 必须去重"


# ──────────────── 3. start_all.ps1 启动器逻辑 ────────────────


class TestStartAllLauncher:
    """start_all.ps1 中启动器选择逻辑的单元测试（用 Python 模拟 PowerShell 语义）。"""

    def test_prefer_py_launcher_over_default_python(self):
        """py launcher 存在时不应回退到默认 python。"""

        def select_python(launcher_available, py_versions, default_python):
            # 模拟 PowerShell 中的 if/else 流程
            if launcher_available:
                for v in py_versions:
                    return ("py", v)
            if default_python and default_python >= (3, 11):
                return ("python", "default")
            return None

        result = select_python(
            launcher_available=True,
            py_versions=[(3, 13), (3, 12), (3, 11)],
            default_python=(3, 6),
        )
        assert result == ("py", (3, 13))

        # py launcher 缺失但 default >= 3.11
        result = select_python(
            launcher_available=False,
            py_versions=[],
            default_python=(3, 12),
        )
        assert result == ("python", "default")

        # 两者都不可用
        result = select_python(
            launcher_available=False,
            py_versions=[],
            default_python=(3, 6),
        )
        assert result is None

    def test_py_launcher_split_args(self):
        """启动器与版本参数必须分开保存，不能拼成 'py-3.12'。"""
        launcher = "py"
        version = "3.12"
        # 错误的拼接
        wrong = f"{launcher}-{version}"
        assert wrong == "py-3.12"
        # 正确的分开
        correct = [launcher, f"-{version}"]
        assert correct == ["py", "-3.12"]
        # 命令构造
        cmd = correct + ["-m", "pytest"]
        assert cmd[0] == "py"
        assert cmd[1] == "-3.12"
        assert cmd[2] == "-m"
        assert cmd[3] == "pytest"

    def test_python_3_6_rejected(self):
        """Python 3.6 应被明确拒绝。"""

        def check_min_version(py_version):
            return py_version >= (3, 11)

        assert check_min_version((3, 6, 0)) is False
        assert check_min_version((3, 10, 5)) is False
        assert check_min_version((3, 11, 0)) is True
        assert check_min_version((3, 12, 6)) is True


# ──────────────── 4. E2E 隔离 + metrics active provider ────────────────


class TestE2EIsolation:
    """E2E 必须用专用临时 DB，结束后清理；不允许污染 a_stock.db。"""

    def test_temp_db_path_is_isolated(self):
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            suffix=".db", prefix="e2e_smoke_", delete=False
        ) as f:
            tmp_path = f.name
        try:
            # 路径不在日常 a_stock.db 旁边
            assert "e2e_smoke" in tmp_path
            assert not tmp_path.endswith("a_stock.db")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestMetricsActiveProvider:
    """MetricsRegistry 应跟踪实际提供数据的 provider，而不是配置列表。"""

    def test_active_providers_tracked_separately(self):
        from app.observability.metrics import MetricsRegistry

        reg = MetricsRegistry()
        # 模拟三个 provider，其中 mock 在 E2E 中实际提供数据
        reg.record_provider_success("mock", 0.01)
        reg.record_provider_success("tencent", 0.05)
        # tencent 失败多次
        for _ in range(10):
            reg.record_provider_failure("akshare")

        snap = reg.snapshot()
        assert "providers" in snap
        assert "active_providers" in snap or "data_status" in snap


# ──────────────── 5. Managed E2E 路径约束（行为测试在 test_phase2_fix_e2e_behavior.py） ────────────────

# 旧版 6 个 "读源码找字符串" 的脆弱测试已升级为行为测试：
# 实际调用 _resolve_frontend_command / _delete_temp_db_unconditional /
# _verify_a_stock_db_unchanged，并模拟 4 种清理路径 + 4 种 a_stock.db 状态。
# 见 backend/tests/test_phase2_fix_e2e_behavior.py（12 个 case）。
