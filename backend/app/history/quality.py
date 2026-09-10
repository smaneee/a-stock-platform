"""历史数据质量检查。

检测重复时间、缺失字段、OHLC 不自洽、负成交量、时间倒序等问题，
并能基于交易日历统计缓存完整性。

报告维度：
- total / clean_count / rejected_count：实际处理数 vs 有效数 vs 脏数据
- expected_count / actual_count：基于交易日历的完整性
- missing_dates：相对于交易日历的缺口日期列表
- issues：所有具体问题（含 detail）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.market_data.base import QuoteData


@dataclass
class QualityIssue:
    """单条质量问题。"""

    code: str  # duplicate_time / missing_field / ohlc_invalid / negative_volume / time_reversal
    detail: str


@dataclass
class QualityReport:
    """质量检查报告。"""

    total: int = 0
    issues: list[QualityIssue] = field(default_factory=list)
    clean_count: int = 0
    rejected_count: int = 0
    expected_count: int = 0
    actual_count: int = 0
    missing_dates: list = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.issues

    @property
    def is_complete(self) -> bool:
        """基于交易日历：expected_count 与 actual_count 一致，且无缺失日期。"""
        return (
            self.expected_count == 0
            or (self.actual_count >= self.expected_count and not self.missing_dates)
        )

    @property
    def issue_codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "clean_count": self.clean_count,
            "rejected_count": self.rejected_count,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "missing_dates": [d.isoformat() for d in self.missing_dates],
            "is_complete": self.is_complete,
            "is_clean": self.is_clean,
            "issue_codes": sorted(self.issue_codes),
            "issues": [{"code": i.code, "detail": i.detail} for i in self.issues[:50]],
        }


class DataQualityChecker:
    """历史 K 线质量检查器。

    与交易日历配合可以统计缓存完整性（expected_count / actual_count / missing_dates）。
    """

    def check(
        self,
        bars: list[QuoteData],
        trading_calendar: "TradingCalendarLike | None" = None,
        expected_start: "date | None" = None,
        expected_end: "date | None" = None,
    ) -> QualityReport:
        """检查 K 线序列。

        - bars: 候选 K 线（落库前可作为过滤依据；落库后用于复核）。
        - trading_calendar: 实现 .iter_trading_days(start, end) 的对象；缺省不计算预期 / 缺口。
        - expected_start / expected_end: 期望覆盖区间。缺省则从 bars 推断。
        """
        report = QualityReport(total=len(bars))
        if not bars:
            # 即使 bars 为空，也要按日历计算缺失
            if trading_calendar is not None and expected_start and expected_end:
                expected = trading_calendar.trading_days_in_range(
                    expected_start, expected_end
                )
                report.expected_count = len(expected)
                report.actual_count = 0
                report.missing_dates = sorted(expected)
            return report

        seen_times: set = set()
        prev_time = None
        clean = 0
        rejected_indexes: set[int] = set()

        for i, bar in enumerate(bars):
            t = bar.market_time
            symbol = bar.symbol
            is_bar_good = True

            # 1) 缺失字段
            if bar.price <= 0 and bar.open <= 0 and bar.high <= 0 and bar.low <= 0:
                report.issues.append(
                    QualityIssue(
                        "missing_field",
                        f"{symbol} 第{i}根 K 线 OHLC 全为 0/缺失",
                    )
                )
                is_bar_good = False

            # 2) OHLC 不自洽
            if bar.high > 0 and bar.low > 0:
                if bar.high < bar.low:
                    report.issues.append(
                        QualityIssue(
                            "ohlc_invalid",
                            f"{symbol} {t} 最高价 {bar.high} < 最低价 {bar.low}",
                        )
                    )
                    is_bar_good = False
                if bar.open > 0 and (bar.open > bar.high or bar.open < bar.low):
                    report.issues.append(
                        QualityIssue(
                            "ohlc_invalid",
                            f"{symbol} {t} 开盘价超出 [低, 高]",
                        )
                    )
                    is_bar_good = False
                if bar.price > 0 and (bar.price > bar.high or bar.price < bar.low):
                    report.issues.append(
                        QualityIssue(
                            "ohlc_invalid",
                            f"{symbol} {t} 收盘价超出 [低, 高]",
                        )
                    )
                    is_bar_good = False

            # 3) 负成交量
            if bar.volume < 0:
                report.issues.append(
                    QualityIssue("negative_volume", f"{symbol} {t} 成交量为负")
                )
                is_bar_good = False

            # 4) 重复时间
            if t is not None:
                if t in seen_times:
                    report.issues.append(
                        QualityIssue("duplicate_time", f"{symbol} {t} 时间重复")
                    )
                    is_bar_good = False
                seen_times.add(t)

            # 5) 时间倒序
            if prev_time is not None and t is not None and t < prev_time:
                report.issues.append(
                    QualityIssue("time_reversal", f"{symbol} {t} 早于前一根 {prev_time}")
                )
                is_bar_good = False
            if t is not None:
                prev_time = t

            if is_bar_good:
                clean += 1
            else:
                rejected_indexes.add(i)

        report.clean_count = clean
        report.rejected_count = len(rejected_indexes)

        # 与交易日历对齐计算完整性
        if trading_calendar is not None:
            actual_dates = {
                b.market_time.date() for b in bars if b.market_time is not None
            }
            if expected_start is None or expected_end is None:
                if actual_dates:
                    expected_start = min(actual_dates)
                    expected_end = max(actual_dates)
            if expected_start and expected_end:
                expected = trading_calendar.trading_days_in_range(
                    expected_start, expected_end
                )
                report.expected_count = len(expected)
                report.actual_count = len(actual_dates)
                report.missing_dates = sorted(expected - actual_dates)

        return report


# 类型提示：避免循环 import
from typing import Protocol


class TradingCalendarLike(Protocol):
    """与 DataQualityChecker 配合的最小交易日历接口。

    真实实现位于 app.market_rules.calendar.TradingCalendar，
    这里仅描述 duck-type 接口。
    """

    def trading_days_in_range(self, start: "date", end: "date") -> "set":
        ...
