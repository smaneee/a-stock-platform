"""历史数据质量检查。

检测重复时间、缺失字段、OHLC 不自洽、负成交量、时间倒序等问题。
不能把不完整数据静默交给回测引擎。
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

    @property
    def clean_count(self) -> int:
        """正常数据条数（不含重复/倒序之外的问题无法精确计数，这里返回近似）。"""
        return self.total

    @property
    def is_clean(self) -> bool:
        return not self.issues

    @property
    def issue_codes(self) -> set[str]:
        return {i.code for i in self.issues}


class DataQualityChecker:
    """历史 K 线质量检查器。"""

    def check(self, bars: list[QuoteData]) -> QualityReport:
        """检查 K 线序列，返回问题清单。"""
        report = QualityReport(total=len(bars))
        if not bars:
            return report

        seen_times: set = set()
        prev_time = None

        for i, bar in enumerate(bars):
            t = bar.market_time
            symbol = bar.symbol

            # 1) 缺失字段：关键价格非法
            if bar.price <= 0 and bar.open <= 0 and bar.high <= 0 and bar.low <= 0:
                report.issues.append(
                    QualityIssue("missing_field", f"{symbol} 第{i}根 K 线 OHLC 全为 0/缺失")
                )

            # 2) OHLC 不自洽
            if bar.high > 0 and bar.low > 0:
                if bar.high < bar.low:
                    report.issues.append(
                        QualityIssue(
                            "ohlc_invalid",
                            f"{symbol} {t} 最高价 {bar.high} < 最低价 {bar.low}",
                        )
                    )
                if bar.open > 0 and (bar.open > bar.high or bar.open < bar.low):
                    report.issues.append(
                        QualityIssue(
                            "ohlc_invalid",
                            f"{symbol} {t} 开盘价 {bar.open} 超出 [低 {bar.low}, 高 {bar.high}]",
                        )
                    )
                if bar.price > 0 and (bar.price > bar.high or bar.price < bar.low):
                    report.issues.append(
                        QualityIssue(
                            "ohlc_invalid",
                            f"{symbol} {t} 收盘价 {bar.price} 超出 [低 {bar.low}, 高 {bar.high}]",
                        )
                    )

            # 3) 负成交量
            if bar.volume < 0:
                report.issues.append(
                    QualityIssue("negative_volume", f"{symbol} {t} 成交量为负 {bar.volume}")
                )

            # 4) 重复时间
            if t is not None:
                if t in seen_times:
                    report.issues.append(
                        QualityIssue("duplicate_time", f"{symbol} {t} 时间重复")
                    )
                seen_times.add(t)

            # 5) 时间倒序
            if prev_time is not None and t is not None and t < prev_time:
                report.issues.append(
                    QualityIssue(
                        "time_reversal",
                        f"{symbol} {t} 早于前一根 {prev_time}",
                    )
                )
            if t is not None:
                prev_time = t

        return report
