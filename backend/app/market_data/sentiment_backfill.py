"""离线回算历史涨停情绪（封板率 / 连板高度 / 连板梯队）。

东财涨停板池（push2ex）只提供**当日**榜单，``LimitUpSentimentStore.backfill``
需要逐日打网络且可回溯窗口很短，"用涨停情绪因子做回测"一直缺少历史序列。
本模块改用本地 ``historical_bars``（不复权日线）配合
:class:`~app.market_rules.rules.MarketRuleEngine` 的涨跌停价规则，离线回算整段
历史并写入 ``limit_up_sentiment``（``source='derived'``）。

计算口径
--------
- 涨停（封板）：``收盘价 >= 涨停价``；涨停价 = 上一有效交易日收盘价 ×(1+涨跌幅
  限制)，四舍五入到 0.01 元（ST 5%、沪深主板 10%、创业板/科创板 20%、
  北交所 30%）。
- 炸板：``最高价 >= 涨停价`` 但收盘未封住。
- 跌停：``收盘价 <= 跌停价``。
- 连板：在该标的自身的日线序列中连续封板，且相邻两根日线在交易日历上相邻
  （长期停牌会打断连板，与"连续涨停交易日"口径一致）。
- ``first_board_count`` / ``streak_N_count`` 按上面的连板数分档。

与东财实时口径的差异（使用时务必注意）
--------------------------------------
- 日线没有封单金额与首次封板时间，``total_seal_amount`` 与
  ``total_limit_up_amount`` 记 0；``strong_count`` / ``sub_new_count`` 依赖
  分钟级涨幅与上市日期口径，也记 0。
- 除权除息日交易所会用除权价重算涨跌停基准，本地不复权日线仍按前收盘计算，
  因此这类日子只会**漏记**涨停、不会误记（约每只股票每年 1~2 天）。
- 上市初期无涨跌停的交易日按 ``NO_LIMIT_DAYS`` 跳过。
- 默认 ``include_st=False``：东财涨停板池**不含** ST / *ST，因此回算也剔除 ST，
  否则两条曲线在"东财实抓区间"与"回算区间"的接缝处会出现台阶。实测（2026-09-11）
  回算结果恰好是东财榜单的严格超集，多出的 7 只全部是 ST。
  注意 ``securities.is_st`` 是**当前** ST 状态，会被套用到整段历史，这是近似。
- 样本面 ``coverage_symbols`` 只统计**真正参与判定**的标的（当日与上一有效交易日
  都有日线），所以本地日线覆盖的第一天为 0、整日不写入。封板率 / 炸板率这类比率
  在样本面稳定时可横向比较；涨停**家数**是绝对量，会随样本面变化，因此样本面低于
  ``coverage_floor`` 的交易日整日不写入。
- 本地日线起点之前发生的涨停无法还原，所以起点附近（约头 12 个交易日）的连板高度
  可能偏低，往后收敛。

本模块只读日线、只写 ``limit_up_sentiment``，不触碰任何实时抓取链路。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import HistoricalBar, LimitUpSentiment, Security
from app.market_rules.calendar import TradingCalendar
from app.market_rules.rules import NO_LIMIT_DAYS, MarketRuleEngine
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

# 写入 ``limit_up_sentiment.source`` 的标记，用于与东财实时榜单区分
DERIVED_SOURCE = "derived"

#: 回算口径的算法版本。日线 high/close 只能近似「触板未封板」，
#: 无法还原封板次数、封板时间与盘口队列 —— 版本号让这种口径限制可追溯。
DERIVED_ALGORITHM_VERSION = "derived-daily-high-close-v1"

_PERIOD = "daily"
_ADJUST = "none"

# 样本面低于该值的交易日整日不写入（绝对家数与比率都会严重失真）
DEFAULT_COVERAGE_FLOOR = 1000

# 回算窗口每天要往前多看一段，才能拿到"上一有效收盘价"以及进入窗口时的连板数。
# 120 个自然日远超最长休市（春节约 11 天），也足以覆盖任何现实中的连板长度。
_PREV_CONTEXT_DAYS = 120

# 每次从库里取多少个标的的日线，控制峰值内存
_SYMBOL_CHUNK = 400

_QUANTUM = Decimal("0.01")


def _quantize(value: object) -> Decimal:
    """把价格规整到 0.01 元；None 或非法值返回 0。"""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value)).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError):
        return Decimal("0")


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    """把标的列表切成定长分片，避免一次加载整市场的日线。"""
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


@dataclass
class _DayAccumulator:
    """单个交易日的回算累加器。"""

    limit_up_count: int = 0
    limit_down_count: int = 0
    broken_board_count: int = 0
    coverage_symbols: int = 0
    # 连板数 -> 只数；键为 1（首板）、2、3 …
    streak_histogram: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DerivedSentiment:
    """回算出的单个交易日情绪因子。"""

    trade_date: date
    limit_up_count: int
    limit_down_count: int
    broken_board_count: int
    coverage_symbols: int
    max_streak: int
    first_board_count: int
    streak_2_count: int
    streak_3_count: int
    streak_4_count: int
    streak_5plus_count: int

    @property
    def seal_denominator(self) -> int:
        """封板率 / 炸板率的共同分母。"""
        return self.limit_up_count + self.broken_board_count

    @property
    def seal_rate(self) -> float | None:
        """封板率 = 涨停家数 / (涨停家数 + 炸板家数)；分母为 0 时 None。"""
        denominator = self.seal_denominator
        return self.limit_up_count / denominator if denominator else None

    @property
    def broken_rate(self) -> float | None:
        """炸板率 = 炸板家数 / (涨停家数 + 炸板家数)；分母为 0 时 None。"""
        denominator = self.seal_denominator
        return self.broken_board_count / denominator if denominator else None

    def to_dict(self) -> dict[str, object]:
        """序列化为接口返回结构。"""
        return {
            "trade_date": self.trade_date.isoformat(),
            "limit_up_count": self.limit_up_count,
            "limit_down_count": self.limit_down_count,
            "broken_board_count": self.broken_board_count,
            "coverage_symbols": self.coverage_symbols,
            "seal_rate": self.seal_rate,
            "broken_rate": self.broken_rate,
            "max_streak": self.max_streak,
            "first_board_count": self.first_board_count,
            "streak_2_count": self.streak_2_count,
            "streak_3_count": self.streak_3_count,
            "streak_4_count": self.streak_4_count,
            "streak_5plus_count": self.streak_5plus_count,
        }


@dataclass(frozen=True)
class ComputationResult:
    """一次纯计算（不落库）的结果。"""

    start: date
    end: date
    symbols: int
    bars_scanned: int
    items: list[DerivedSentiment]
    low_coverage_days: list[date]


@dataclass(frozen=True)
class BackfillReport:
    """回算落库的执行报告。"""

    start: date
    end: date
    symbols: int
    bars_scanned: int
    inserted: int
    updated: int
    skipped_existing: int
    skipped_low_coverage: int
    deleted_stale: int
    coverage_floor: int
    include_st: bool
    overwrite_derived: bool
    dry_run: bool
    items: list[DerivedSentiment]
    low_coverage_days: list[date]
    stale_days: list[date]

    def to_dict(self) -> dict[str, object]:
        """序列化为接口返回结构（不含逐日明细）。"""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": self.symbols,
            "bars_scanned": self.bars_scanned,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_existing": self.skipped_existing,
            "skipped_low_coverage": self.skipped_low_coverage,
            "deleted_stale": self.deleted_stale,
            "coverage_floor": self.coverage_floor,
            "include_st": self.include_st,
            "overwrite_derived": self.overwrite_derived,
            "dry_run": self.dry_run,
            "days": len(self.items),
            "items": [item.to_dict() for item in self.items],
            "low_coverage_days": [day.isoformat() for day in self.low_coverage_days],
            "stale_days": [day.isoformat() for day in self.stale_days],
        }


class SentimentBackfillService:
    """用本地不复权日线回算历史涨停情绪。"""

    def __init__(
        self,
        db: Session,
        *,
        rule_engine: MarketRuleEngine | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._db = db
        self._rules = rule_engine or MarketRuleEngine()
        self._calendar = calendar or TradingCalendar(db)
        self._next_day: dict[date, date] = {}

    # ---------------- 对外接口 ----------------

    def bar_date_bounds(self) -> tuple[date, date] | None:
        """本地不复权日线覆盖的最早 / 最晚交易日；无日线时返回 None。"""
        base = (
            select(HistoricalBar.trade_date)
            .where(HistoricalBar.period == _PERIOD)
            .where(HistoricalBar.adjust == _ADJUST)
        )
        first = self._db.scalars(base.order_by(HistoricalBar.trade_date).limit(1)).first()
        if first is None:
            return None
        last = self._db.scalars(
            base.order_by(HistoricalBar.trade_date.desc()).limit(1)
        ).first()
        return first, last

    def compute(
        self,
        start: date,
        end: date,
        *,
        coverage_floor: int = DEFAULT_COVERAGE_FLOOR,
        include_st: bool = False,
    ) -> ComputationResult:
        """按交易日回算情绪因子，不写库。样本面不足的交易日单独列出。

        ``include_st=False``（默认）剔除 ST / *ST，与东财涨停板池口径一致。
        """
        if start > end:
            raise ValueError("start 不能晚于 end")

        symbols = self._symbols_with_bars(start, end)
        if not symbols:
            return ComputationResult(start, end, 0, 0, [], [])

        self._prepare_calendar(start, end)
        securities = self._security_index(symbols)

        accumulators: dict[date, _DayAccumulator] = {}
        scanned = 0
        for chunk in _chunked(symbols, _SYMBOL_CHUNK):
            rows = self._load_bars(chunk, start, end)
            current: str | None = None
            bucket: list[tuple[date, object, object]] = []
            for symbol, trade_date, high, close in rows:
                if symbol != current:
                    if current is not None:
                        scanned += len(bucket)
                        self._scan_symbol(
                            current,
                            bucket,
                            securities.get(current),
                            start,
                            end,
                            accumulators,
                            include_st=include_st,
                        )
                    current = symbol
                    bucket = []
                bucket.append((trade_date, high, close))
            if current is not None:
                scanned += len(bucket)
                self._scan_symbol(
                    current,
                    bucket,
                    securities.get(current),
                    start,
                    end,
                    accumulators,
                    include_st=include_st,
                )

        items: list[DerivedSentiment] = []
        low_coverage: list[date] = []
        for trade_date in sorted(accumulators):
            accumulator = accumulators[trade_date]
            if accumulator.coverage_symbols < coverage_floor:
                low_coverage.append(trade_date)
                continue
            items.append(self._to_derived(trade_date, accumulator))
        logger.info(
            "涨停情绪回算完成：%s ~ %s，标的 %d 只，日线 %d 根，有效交易日 %d 天，样本不足 %d 天",
            start,
            end,
            len(symbols),
            scanned,
            len(items),
            len(low_coverage),
        )
        return ComputationResult(start, end, len(symbols), scanned, items, low_coverage)

    def backfill(
        self,
        start: date | None = None,
        end: date | None = None,
        *,
        coverage_floor: int = DEFAULT_COVERAGE_FLOOR,
        include_st: bool = False,
        overwrite_derived: bool = False,
        dry_run: bool = False,
    ) -> BackfillReport:
        """回算并写库；已存在东财实时榜单的交易日默认跳过。"""
        bounds = self.bar_date_bounds()
        if bounds is None:
            raise ValueError("本地没有不复权日线，无法回算涨停情绪")
        resolved_start = start or bounds[0]
        resolved_end = end or bounds[1]
        if resolved_start > resolved_end:
            raise ValueError("start 不能晚于 end")

        computation = self.compute(
            resolved_start,
            resolved_end,
            coverage_floor=coverage_floor,
            include_st=include_st,
        )

        plan: list[tuple[str, DerivedSentiment]] = []
        for item in computation.items:
            existing = self._db.get(LimitUpSentiment, item.trade_date)
            if existing is None:
                plan.append(("insert", item))
            elif existing.source == DERIVED_SOURCE and overwrite_derived:
                plan.append(("update", item))
            else:
                # 东财实时榜单口径（含封单金额 / 首封时间）优先，不覆盖
                plan.append(("skip", item))

        stale_days = self._stale_derived_days(
            resolved_start,
            resolved_end,
            {item.trade_date for item in computation.items},
        )

        if not dry_run:
            for action, item in plan:
                if action == "insert":
                    self._db.add(self._build_row(item))
                elif action == "update":
                    self._apply_row(self._db.get(LimitUpSentiment, item.trade_date), item)
            for trade_date in stale_days:
                row = self._db.get(LimitUpSentiment, trade_date)
                if row is not None:
                    self._db.delete(row)
            self._db.commit()

        inserted = sum(1 for action, _ in plan if action == "insert")
        updated = sum(1 for action, _ in plan if action == "update")
        skipped = sum(1 for action, _ in plan if action == "skip")
        report = BackfillReport(
            start=resolved_start,
            end=resolved_end,
            symbols=computation.symbols,
            bars_scanned=computation.bars_scanned,
            inserted=inserted,
            updated=updated,
            skipped_existing=skipped,
            skipped_low_coverage=len(computation.low_coverage_days),
            deleted_stale=len(stale_days),
            coverage_floor=coverage_floor,
            include_st=include_st,
            overwrite_derived=overwrite_derived,
            dry_run=dry_run,
            items=computation.items,
            low_coverage_days=computation.low_coverage_days,
            stale_days=stale_days,
        )
        logger.info(
            "涨停情绪回算落库：新增 %d，更新 %d，跳过 %d，清理陈旧 %d，dry_run=%s",
            inserted,
            updated,
            skipped,
            len(stale_days),
            dry_run,
        )
        return report

    # ---------------- 内部实现 ----------------

    def _stale_derived_days(
        self, start: date, end: date, produced: set[date]
    ) -> list[date]:
        """范围内已有回算行、但本次没有产出的交易日。

        口径调整（例如抬高 ``coverage_floor``）或本地日线被裁剪后会命中，需要
        删掉才能保证"库里的回算结果 == 本次口径算出来的结果"。
        """
        stmt = (
            select(LimitUpSentiment.trade_date)
            .where(LimitUpSentiment.source == DERIVED_SOURCE)
            .where(LimitUpSentiment.trade_date >= start)
            .where(LimitUpSentiment.trade_date <= end)
        )
        if produced:
            stmt = stmt.where(LimitUpSentiment.trade_date.not_in(produced))
        return sorted(self._db.scalars(stmt).all())

    def _symbols_with_bars(self, start: date, end: date) -> list[str]:
        """回算窗口内有日线的标的（含回看段，用于取上一收盘价）。"""
        stmt = (
            select(HistoricalBar.symbol)
            .where(HistoricalBar.period == _PERIOD)
            .where(HistoricalBar.adjust == _ADJUST)
            .where(
                HistoricalBar.trade_date >= start - timedelta(days=_PREV_CONTEXT_DAYS)
            )
            .where(HistoricalBar.trade_date <= end)
            .distinct()
            .order_by(HistoricalBar.symbol)
        )
        return list(self._db.scalars(stmt).all())

    def _load_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[tuple[str, date, object, object]]:
        """按 (标的, 交易日) 顺序取一批标的的日线，只取判定所需字段。"""
        stmt = (
            select(
                HistoricalBar.symbol,
                HistoricalBar.trade_date,
                HistoricalBar.high,
                HistoricalBar.close,
            )
            .where(HistoricalBar.period == _PERIOD)
            .where(HistoricalBar.adjust == _ADJUST)
            .where(HistoricalBar.symbol.in_(list(symbols)))
            .where(
                HistoricalBar.trade_date >= start - timedelta(days=_PREV_CONTEXT_DAYS)
            )
            .where(HistoricalBar.trade_date <= end)
            .order_by(HistoricalBar.symbol, HistoricalBar.trade_date)
        )
        return list(self._db.execute(stmt).all())

    def _security_index(
        self, symbols: Sequence[str]
    ) -> dict[str, tuple[str, bool | None, date | None]]:
        """symbol -> (名称, 是否 ST, 上市日)，用于判定涨跌幅限制与新股期。"""
        index: dict[str, tuple[str, bool | None, date | None]] = {}
        for chunk in _chunked(symbols, 900):
            rows = self._db.execute(
                select(Security.symbol, Security.name, Security.is_st, Security.listing_date)
                .where(Security.symbol.in_(list(chunk)))
            ).all()
            for symbol, name, is_st, listing_date in rows:
                index[symbol] = (name or "", is_st, listing_date)
        return index

    def _prepare_calendar(self, start: date, end: date) -> None:
        """预计算"下一个交易日"，用于判定连板是否被停牌打断。"""
        lo = start - timedelta(days=_PREV_CONTEXT_DAYS)
        days = sorted(self._calendar.trading_days_in_range(lo, end))
        self._next_day = {
            days[index]: days[index + 1] for index in range(len(days) - 1)
        }

    def _scan_symbol(
        self,
        symbol: str,
        bars: Sequence[tuple[date, object, object]],
        security: tuple[str, bool | None, date | None] | None,
        start: date,
        end: date,
        accumulators: dict[date, _DayAccumulator],
        *,
        include_st: bool = False,
    ) -> None:
        """扫一只标的的日线序列，累计每日封板 / 炸板 / 连板。"""
        name, is_st, listing_date = security or ("", None, None)
        if is_st and not include_st:
            # 与东财涨停板池口径对齐：ST / *ST 整只不计入样本
            return
        no_limit_days = NO_LIMIT_DAYS.get(self._rules.classify(symbol), 0)
        previous_close: Decimal | None = None
        previous_date: date | None = None
        streak = 0

        for trade_date, high, close in bars:
            close_q = _quantize(close)
            if close_q <= 0:
                # 脏数据：不参与统计，也不作为后续交易日的"上一收盘价"
                continue

            if previous_close is not None:
                in_window = start <= trade_date <= end
                is_new_listing = (
                    listing_date is not None
                    and no_limit_days > 0
                    and 0 <= (trade_date - listing_date).days < no_limit_days
                )
                rules = self._rules.get_rules(
                    symbol, name=name, is_st=is_st, is_new_listing=is_new_listing
                )
                limit_up = rules.limit_up(previous_close)
                limit_down = rules.limit_down(previous_close)
                sealed = limit_up is not None and close_q >= limit_up
                if sealed:
                    continuous = (
                        previous_date is not None
                        and self._next_day.get(previous_date) == trade_date
                    )
                    streak = streak + 1 if continuous else 1
                else:
                    streak = 0
                if in_window:
                    bucket = self._bucket(accumulators, trade_date)
                    # 只有拿到上一收盘价、真正参与判定的标的才计入样本面，
                    # 否则序列首日会被算成"零涨停"
                    bucket.coverage_symbols += 1
                    if sealed:
                        bucket.limit_up_count += 1
                    elif limit_up is not None and _quantize(high) >= limit_up:
                        bucket.broken_board_count += 1
                    if limit_down is not None and close_q <= limit_down:
                        bucket.limit_down_count += 1
                    if streak > 0:
                        bucket.streak_histogram[streak] = (
                            bucket.streak_histogram.get(streak, 0) + 1
                        )

            previous_close = close_q
            previous_date = trade_date

    @staticmethod
    def _bucket(
        accumulators: dict[date, _DayAccumulator], trade_date: date
    ) -> _DayAccumulator:
        bucket = accumulators.get(trade_date)
        if bucket is None:
            bucket = _DayAccumulator()
            accumulators[trade_date] = bucket
        return bucket

    @staticmethod
    def _to_derived(
        trade_date: date, accumulator: _DayAccumulator
    ) -> DerivedSentiment:
        histogram = accumulator.streak_histogram
        return DerivedSentiment(
            trade_date=trade_date,
            limit_up_count=accumulator.limit_up_count,
            limit_down_count=accumulator.limit_down_count,
            broken_board_count=accumulator.broken_board_count,
            coverage_symbols=accumulator.coverage_symbols,
            max_streak=max(histogram, default=0),
            first_board_count=histogram.get(1, 0),
            streak_2_count=histogram.get(2, 0),
            streak_3_count=histogram.get(3, 0),
            streak_4_count=histogram.get(4, 0),
            streak_5plus_count=sum(
                count for streak, count in histogram.items() if streak >= 5
            ),
        )

    @staticmethod
    def _apply_row(row: LimitUpSentiment, item: DerivedSentiment) -> None:
        row.limit_up_count = item.limit_up_count
        row.limit_down_count = item.limit_down_count
        row.broken_board_count = item.broken_board_count
        row.strong_count = 0
        row.sub_new_count = 0
        row.seal_rate = item.seal_rate
        row.broken_rate = item.broken_rate
        row.coverage_symbols = item.coverage_symbols
        row.max_streak = item.max_streak
        row.first_board_count = item.first_board_count
        row.streak_2_count = item.streak_2_count
        row.streak_3_count = item.streak_3_count
        row.streak_4_count = item.streak_4_count
        row.streak_5plus_count = item.streak_5plus_count
        row.total_seal_amount = 0.0
        row.total_limit_up_amount = 0.0
        row.source = DERIVED_SOURCE
        row.algorithm_version = DERIVED_ALGORITHM_VERSION
        row.captured_at = utc_now()

    @classmethod
    def _build_row(cls, item: DerivedSentiment) -> LimitUpSentiment:
        row = LimitUpSentiment(trade_date=item.trade_date)
        cls._apply_row(row, item)
        return row


def load_sentiment_series(
    db: Session, start: date, end: date
) -> dict[date, LimitUpSentiment]:
    """读取 [start, end] 的涨停情绪序列，按交易日索引。"""
    rows = db.scalars(
        select(LimitUpSentiment)
        .where(LimitUpSentiment.trade_date >= start)
        .where(LimitUpSentiment.trade_date <= end)
        .order_by(LimitUpSentiment.trade_date)
    ).all()
    return {row.trade_date: row for row in rows}
