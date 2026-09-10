"""M4 修正提交测试：历史缓存完整性 + 缺口合并 + QualityReport 维度。"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.database.models import HistoricalBar, TradingDate
from app.history.quality import DataQualityChecker, QualityReport
from app.history.service import (
    HistoricalDataService,
    merge_gap_ranges,
)
from app.market_data.base import QuoteData
from app.market_rules.calendar import TradingCalendar
from app.time_utils import utc_now


def _mk_bar(symbol="600000", trade_date="2026-01-05", price=10.0):
    """构造一根干净的 K 线（用于测试）。"""
    return QuoteData(
        symbol=symbol,
        name=symbol,
        price=price,
        open=price - 0.1,
        high=price + 0.2,
        low=price - 0.2,
        previous_close=0.0,
        volume=1000.0,
        amount=100000.0,
        source="akshare",
        market_time=datetime.strptime(trade_date, "%Y-%m-%d"),
    )


def _seed_calendar(db, dates):
    """向交易日历插入交易日。"""
    for d in dates:
        db.add(TradingDate(trade_date=d))
    db.commit()


def _seed_bars(db, dates, symbol="600000"):
    """向 historical_bars 插入 K 线。"""
    for d in dates:
        db.add(
            HistoricalBar(
                symbol=symbol,
                period="daily",
                adjust="none",
                trade_date=d,
                open=10.0,
                high=10.5,
                low=9.5,
                close=10.2,
                volume=1000.0,
                amount=100000.0,
                source="akshare",
                fetched_at=utc_now(),
            )
        )
    db.commit()


# ──────── merge_gap_ranges ────────


class TestMergeGapRanges:
    """缺失日期列表合并为连续下载区间。"""

    def test_empty_returns_empty(self):
        assert merge_gap_ranges([]) == []

    def test_single_date(self):
        assert merge_gap_ranges([date(2024, 6, 10)]) == [
            (date(2024, 6, 10), date(2024, 6, 10))
        ]

    def test_consecutive_merged(self):
        # 相邻日期应该合并为一段
        dates = [date(2024, 6, 10), date(2024, 6, 11), date(2024, 6, 12)]
        assert merge_gap_ranges(dates) == [(date(2024, 6, 10), date(2024, 6, 12))]

    def test_discrete_split(self):
        # 跨度 > 5 天则拆段
        dates = [date(2024, 6, 10), date(2024, 6, 11), date(2024, 6, 20)]
        result = merge_gap_ranges(dates)
        assert len(result) == 2
        assert result[0] == (date(2024, 6, 10), date(2024, 6, 11))
        assert result[1] == (date(2024, 6, 20), date(2024, 6, 20))

    def test_max_gap_param(self):
        dates = [date(2024, 6, 10), date(2024, 6, 13)]  # 3 天跨度
        # 默认 max_gap_days=5：合并
        assert len(merge_gap_ranges(dates)) == 1
        # 强制 max_gap_days=2：拆分
        assert len(merge_gap_ranges(dates, max_gap_days=2)) == 2


# ──────── QualityReport 维度 ────────


class TestQualityReportDimensions:
    """expected_count / actual_count / clean_count / rejected_count / missing_dates。"""

    def test_clean_count_not_always_equal_total(self):
        """clean_count 不等于 total 时也能正确表达。"""
        bars = [
            _mk_bar("600000", "2026-01-05"),
            # 缺失字段：全 0
            QuoteData(
                symbol="600000", name="600000", price=0, open=0,
                high=0, low=0, previous_close=0, volume=0, amount=0,
                source="akshare", market_time=datetime.strptime("2026-01-06", "%Y-%m-%d"),
            ),
        ]
        report = DataQualityChecker().check(bars)
        assert report.total == 2
        assert report.rejected_count >= 1
        assert report.clean_count == 1
        assert report.rejected_count != 0
        # 关键断言：clean_count != total
        assert report.clean_count != report.total

    def test_to_dict_has_all_dimensions(self):
        bars = [_mk_bar("600000", "2026-01-05")]
        report = DataQualityChecker().check(bars)
        d = report.to_dict()
        for key in (
            "total", "clean_count", "rejected_count",
            "expected_count", "actual_count", "missing_dates",
            "is_complete", "is_clean", "issue_codes",
        ):
            assert key in d


class TestQualityReportCompleteness:
    """与 TradingCalendar 协作，按交易日历统计完整性。"""

    def test_actual_matches_expected_when_complete(self, db_session):
        # 假设日历有 3 个交易日（2026-01-05/06/07）
        cal_dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
        _seed_calendar(db_session, cal_dates)

        bars = [_mk_bar("600000", d.isoformat()) for d in cal_dates]
        report = DataQualityChecker().check(
            bars, TradingCalendar(db_session),
            expected_start=date(2026, 1, 5),
            expected_end=date(2026, 1, 7),
        )
        assert report.expected_count == 3
        assert report.actual_count == 3
        assert report.missing_dates == []
        assert report.is_complete is True

    def test_missing_dates_detected(self, db_session):
        # 日历 5 个交易日，缓存只 3 根
        cal_dates = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)]
        _seed_calendar(db_session, cal_dates)
        bars = [
            _mk_bar("600000", "2026-01-05"),
            _mk_bar("600000", "2026-01-07"),
            _mk_bar("600000", "2026-01-09"),
        ]
        report = DataQualityChecker().check(
            bars, TradingCalendar(db_session),
            expected_start=date(2026, 1, 5),
            expected_end=date(2026, 1, 9),
        )
        assert report.expected_count == 5
        assert report.actual_count == 3
        assert set(report.missing_dates) == {date(2026, 1, 6), date(2026, 1, 8)}
        assert report.is_complete is False


# ──────── HistoricalDataService 完整性行为 ────────


class TestHistoryCompleteness:
    """get_history 返回的 QualityReport 含 expected_count / missing_dates 等。"""

    @pytest.mark.asyncio
    async def test_get_history_returns_complete_report(self, db_session):
        # 日历 3 天，缓存全部覆盖
        cal_dates = [date(2026, 1, d) for d in (5, 6, 7)]
        _seed_calendar(db_session, cal_dates)
        _seed_bars(db_session, cal_dates)

        svc = HistoricalDataService(db_session)
        result = await svc.get_history(
            "600000",
            datetime(2026, 1, 5),
            datetime(2026, 1, 7),
        )
        assert result.is_complete is True
        assert result.quality.expected_count == 3
        assert result.quality.actual_count == 3
        assert result.quality.missing_dates == []

    @pytest.mark.asyncio
    async def test_get_history_reports_missing(self, db_session):
        # 日历 5 天，缓存只 3 根（缺失 06、08）
        cal_dates = [date(2026, 1, d) for d in (5, 6, 7, 8, 9)]
        _seed_calendar(db_session, cal_dates)
        _seed_bars(db_session, [date(2026, 1, 5), date(2026, 1, 7), date(2026, 1, 9)])

        svc = HistoricalDataService(db_session)
        result = await svc.get_history(
            "600000",
            datetime(2026, 1, 5),
            datetime(2026, 1, 9),
            sync_if_incomplete=False,
        )
        assert result.is_complete is False
        assert result.quality.expected_count == 5
        assert result.quality.actual_count == 3
        assert {d for d in result.quality.missing_dates} == {
            date(2026, 1, 6),
            date(2026, 1, 8),
        }

    @pytest.mark.asyncio
    async def test_sync_triggers_download_when_calendar_has_gaps(
        self, db_session, monkeypatch
    ):
        """日历有缺口但缓存空时，触发网络拉取。"""
        cal_dates = [date(2026, 1, d) for d in (5, 6)]
        _seed_calendar(db_session, cal_dates)

        fake_bars = [_mk_bar("600000", d.isoformat()) for d in cal_dates]
        monkeypatch.setattr(
            "app.history.service._akshare_fetch",
            lambda symbol, adjust, start, end: fake_bars,
        )
        svc = HistoricalDataService(db_session)
        result = await svc.get_history(
            "600000", datetime(2026, 1, 5), datetime(2026, 1, 6)
        )
        assert result.source in ("akshare", "mixed")
        assert result.is_complete is True
        assert result.quality.expected_count == 2
        assert result.quality.actual_count == 2

    @pytest.mark.asyncio
    async def test_no_calendar_falls_back_to_endpoint_check(
        self, db_session, monkeypatch
    ):
        """日历为空时不阻塞，回退到端点完整性判断。"""
        fake_bars = [_mk_bar("600000", "2026-01-05"), _mk_bar("600000", "2026-01-06")]
        monkeypatch.setattr(
            "app.history.service._akshare_fetch",
            lambda symbol, adjust, start, end: fake_bars,
        )
        svc = HistoricalDataService(db_session)
        result = await svc.get_history(
            "600000", datetime(2026, 1, 5), datetime(2026, 1, 6)
        )
        assert result.is_complete is True
        assert result.quality.expected_count == 0  # 日历空就 0
        assert result.source in ("akshare", "mixed")
