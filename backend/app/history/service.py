"""历史行情本地化服务。

从 AKShare 获取历史 K 线，保存到本地数据库缓存，支持：
- 增量同步（不重复下载已有数据）
- 前复权 / 后复权 / 不复权，并在数据中保存复权标识
- 网络失败回退到缓存，并明确提示数据更新时间
- 连接/读取超时、有限重试与指数退避
- 大批量分块处理
- 缓存完整性基于交易日历校验（expected_count / actual_count / missing_dates）
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import HistoricalBar
from app.history.quality import DataQualityChecker, QualityReport
from app.market_data.base import QuoteData
from app.market_rules.calendar import TradingCalendar
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

# 复权方式常量
ADJUST_NONE = "none"  # 不复权
ADJUST_QFQ = "qfq"  # 前复权
ADJUST_HFQ = "hfq"  # 后复权

_VALID_ADJUST = {ADJUST_NONE, ADJUST_QFQ, ADJUST_HFQ}

# 网络请求参数
_FETCH_TIMEOUT_SECONDS = 30.0  # 单次请求超时
_MAX_RETRIES = 3  # 重试上限
_RETRY_BASE_SECONDS = 2.0  # 退避基数（2^attempt 秒）

# 缺口合并容差：相邻缺失交易日跨度 ≤ 该天数即视为同一段。
# A 股最长连续休市（春节 / 国庆）在交易日之间约 11 天，取 30 天可把
# 「一次回补整年」拆成的 4 段合并为 1 段，单只标的网络调用从 4 次降到 1 次；
# 真正零散的长跨度缺口（> 30 天）仍会拆分，保持增量回补的粒度。
_GAP_MERGE_MAX_DAYS = 30

# BaoStock 内部 socket 没有超时，必须显式设置，否则 next() 可能无限阻塞
_BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 20.0


@dataclass
class HistoryResult:
    """历史数据获取结果。"""

    bars: list[QuoteData]
    source: str  # cache / akshare / mixed
    data_updated_at: datetime | None  # 缓存数据最近更新时间
    is_complete: bool  # 缓存是否完整覆盖请求区间
    quality: QualityReport


class HistoricalDataService:
    """历史行情本地化服务。"""

    def __init__(
        self,
        db: Session,
        checker: DataQualityChecker | None = None,
        provider_manager=None,
    ):
        self._db = db
        self._checker = checker or DataQualityChecker()
        # 显式注入 ProviderManager 时优先使用（含 mock 路径），否则直接调 AKShare
        self._provider_manager = provider_manager

    async def get_history(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        period: str = "daily",
        adjust: str = ADJUST_NONE,
        sync_if_incomplete: bool = True,
    ) -> HistoryResult:
        """获取历史数据：优先本地缓存，缺失则增量同步。

        - 完整性校验基于交易日历：返回 expected_count / actual_count / missing_dates。
        - 网络失败时返回缓存数据（source="cache"），并提示更新时间。
        - sync_if_incomplete=False 时只读缓存，不触发同步。
        - 当本地交易日历为空时退化为端点判断（避免冷启动瞬间被静默）。
        """
        adjust = self._normalize_adjust(adjust)
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = end.date() if isinstance(end, datetime) else end
        calendar = TradingCalendar(self._db)
        calendar_available = not calendar.is_empty()
        cached = self.get_cached(symbol, start, end, period, adjust)

        # 完整性检查：基于交易日历（若日历为空则用端点判断）
        if calendar_available:
            quality = self._checker.check(
                cached, calendar, expected_start=start_date, expected_end=end_date
            )
            is_complete_by_calendar = quality.is_complete
        else:
            quality = self._checker.check(cached)
            is_complete_by_calendar = self._covers(cached, start, end)

        if is_complete_by_calendar and cached:
            updated_at = max((b.received_at for b in cached), default=None)
            return HistoryResult(
                bars=cached,
                source="cache",
                data_updated_at=updated_at,
                is_complete=True,
                quality=quality,
            )

        if not sync_if_incomplete:
            updated_at = max((b.received_at for b in cached), default=None) if cached else None
            return HistoryResult(
                bars=cached,
                source="cache",
                data_updated_at=updated_at,
                is_complete=False,
                quality=quality,
            )

        # 增量同步：有日历则按缺口逐段下载；无日历则直接拉整个区间
        try:
            if calendar_available:
                added = await self._sync_with_calendar(
                    symbol, period, adjust, calendar, start_date, end_date
                )
            else:
                added = await self.sync(symbol, start, end, period, adjust)
        except Exception as exc:  # noqa: BLE001
            logger.warning("历史数据同步失败，回退缓存: %s", exc)
            added = 0

        merged = self.get_cached(symbol, start, end, period, adjust)
        if calendar_available:
            quality = self._checker.check(
                merged, calendar, expected_start=start_date, expected_end=end_date
            )
            is_complete_final = quality.is_complete
        else:
            quality = self._checker.check(merged)
            is_complete_final = self._covers(merged, start, end)
        updated_at = max((b.received_at for b in merged), default=None) if merged else None
        source = "akshare" if added > 0 else "cache"
        if added > 0 and cached:
            source = "mixed"

        return HistoryResult(
            bars=merged,
            source=source,
            data_updated_at=updated_at,
            is_complete=is_complete_final,
            quality=quality,
        )

    def get_cached(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        period: str = "daily",
        adjust: str = ADJUST_NONE,
    ) -> list[QuoteData]:
        """从本地数据库读取缓存（按日期升序）。"""
        adjust = self._normalize_adjust(adjust)
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = end.date() if isinstance(end, datetime) else end

        rows = self._db.scalars(
            select(HistoricalBar)
            .where(
                HistoricalBar.symbol == symbol,
                HistoricalBar.period == period,
                HistoricalBar.adjust == adjust,
                HistoricalBar.trade_date >= start_date,
                HistoricalBar.trade_date <= end_date,
            )
            .order_by(HistoricalBar.trade_date)
        ).all()

        return [self._bar_to_quote(r) for r in rows]

    async def sync(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        period: str = "daily",
        adjust: str = ADJUST_NONE,
    ) -> int:
        """从 AKShare 增量同步历史数据，返回新增条数。"""
        adjust = self._normalize_adjust(adjust)
        if period != "daily":
            # 第一版只支持日线本地化，分钟线走实时管道
            logger.warning("历史数据服务当前仅支持日线同步，收到 period=%s", period)
            return 0

        start_date = start.date() if isinstance(start, datetime) else start
        end_date = end.date() if isinstance(end, datetime) else end

        # 增量：先看已有缓存覆盖范围，只拉缺失区间
        existing_dates = self._existing_dates(symbol, period, adjust)
        missing_start, missing_end = self._missing_range(
            start_date, end_date, existing_dates
        )
        if missing_start is None:
            return 0  # 已完整覆盖

        bars = await self._fetch_with_retry(symbol, adjust, missing_start, missing_end)
        if not bars:
            return 0

        # 质量检查：记录问题（供上层观察），并过滤脏数据不落库
        quality = self._checker.check(bars)
        if not quality.is_clean:
            logger.warning(
                "历史数据同步 %s 发现 %d 类质量问题: %s",
                symbol, len(quality.issue_codes), sorted(quality.issue_codes),
            )

        good = _filter_valid_bars(bars)

        added = 0
        for bar in good:
            trade_date = (bar.market_time or bar.received_at).date()
            if trade_date in existing_dates:
                continue
            self._db.add(
                HistoricalBar(
                    symbol=symbol,
                    period=period,
                    adjust=adjust,
                    trade_date=trade_date,
                    open=Decimal(str(bar.open)),
                    high=Decimal(str(bar.high)),
                    low=Decimal(str(bar.low)),
                    close=Decimal(str(bar.price)),
                    volume=bar.volume,
                    amount=bar.amount,
                    source=bar.source or "akshare",
                    fetched_at=utc_now(),
                )
            )
            added += 1

        self._db.commit()
        logger.info("历史数据同步 %s (%s) 新增 %d 条", symbol, adjust, added)
        return added

    async def _sync_with_calendar(
        self,
        symbol: str,
        period: str,
        adjust: str,
        calendar: TradingCalendar,
        start_date: date,
        end_date: date,
    ) -> int:
        """基于交易日历定位缺口，合并连续区间，逐段下载。

        返回新增条数。失败时返回 0，由调用方决定是否回退缓存。
        """
        if period != "daily":
            logger.warning("历史数据服务当前仅支持日线同步，收到 period=%s", period)
            return 0

        existing_dates = self._existing_dates(symbol, period, adjust)
        expected = calendar.trading_days_in_range(start_date, end_date)
        missing = sorted(expected - existing_dates)
        if not missing:
            return 0

        gap_ranges = merge_gap_ranges(missing, _GAP_MERGE_MAX_DAYS)
        total_added = 0
        for gap_start, gap_end in gap_ranges:
            bars = await self._fetch_with_retry(symbol, adjust, gap_start, gap_end)
            if not bars:
                continue

            quality = self._checker.check(bars)
            if not quality.is_clean:
                logger.warning(
                    "历史数据 %s 区间 %s~%s 发现 %d 类质量问题",
                    symbol, gap_start, gap_end, len(quality.issue_codes),
                )

            good = _filter_valid_bars(bars)
            added = 0
            for bar in good:
                trade_date = (bar.market_time or bar.received_at).date()
                if trade_date in existing_dates:
                    continue
                self._db.add(
                    HistoricalBar(
                        symbol=symbol,
                        period=period,
                        adjust=adjust,
                        trade_date=trade_date,
                        open=Decimal(str(bar.open)),
                        high=Decimal(str(bar.high)),
                        low=Decimal(str(bar.low)),
                        close=Decimal(str(bar.price)),
                        volume=bar.volume,
                        amount=bar.amount,
                        source=bar.source or "akshare",
                        fetched_at=utc_now(),
                    )
                )
                added += 1
                existing_dates.add(trade_date)
            self._db.commit()
            total_added += added
            logger.info(
                "历史数据同步 %s 区间 %s~%s 新增 %d 条", symbol, gap_start, gap_end, added
            )

        return total_added

    # ──────── 内部方法 ────────

    def _provider_covered_sources(self) -> tuple[str, ...]:
        """返回 ProviderManager 已覆盖的回退链源名，避免重复请求同一上游。

        MARKET_PROVIDERS 含 akshare 或 eastmoney 时，manager 内部已经打过东方财富
        kline 接口（stock_zh_a_hist / push2his），回退链不必再打一次——该通道实测经常
        RemoteDisconnected，重复调用会让全市场入库白白翻倍耗时。
        """
        return provider_covered_sources(self._provider_manager)

    async def _fetch_with_retry(
        self, symbol: str, adjust: str, start: date, end: date
    ) -> list[QuoteData]:
        """带超时与指数退避重试的拉取。

        每次尝试先走 ProviderManager（mock / tencent / akshare / qmt），
        再走直连多源回退链（EastMoney → Sina → BaoStock）。任意一个源有数据
        即返回；全部源都报错时抛出最后一次异常，让调用方回退本地缓存。
        """
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        last_exc: Exception | None = None
        saw_empty = False

        for attempt in range(_MAX_RETRIES):
            attempt_raised = False
            if self._provider_manager is not None:
                try:
                    bars = await asyncio.wait_for(
                        self._provider_manager.get_history(
                            symbol, "daily", start_dt, end_dt
                        ),
                        timeout=_FETCH_TIMEOUT_SECONDS,
                    )
                    if bars:
                        return bars
                    saw_empty = True
                except Exception as exc:  # noqa: BLE001
                    attempt_raised = True
                    last_exc = exc
                    logger.warning(
                        "ProviderManager 拉取 %s 第 %d 次失败: %s",
                        symbol, attempt + 1, exc,
                    )
            try:
                bars = await asyncio.wait_for(
                    asyncio.to_thread(
                        _fetch_from_sources,
                        symbol,
                        adjust,
                        start,
                        end,
                        self._provider_covered_sources(),
                    ),
                    timeout=_FETCH_TIMEOUT_SECONDS,
                )
                if bars:
                    return bars
                saw_empty = True
            except Exception as exc:  # noqa: BLE001
                attempt_raised = True
                last_exc = exc
                logger.warning(
                    "多源历史回退拉取 %s 第 %d 次失败: %s",
                    symbol, attempt + 1, exc,
                )
            # 所有源都明确返回空 = 该标的确实没有数据（停牌 / 退市 / 未上市），
            # 这不是瞬时故障；继续退避重试只会白白拖慢整批入库。
            if saw_empty and not attempt_raised:
                break
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_BASE_SECONDS * (2 ** attempt))

        # 全部源都抛异常才向上报错；只是"没数据"（停牌/退市）返回空列表
        if last_exc is not None and not saw_empty:
            raise last_exc
        return []

    def _existing_dates(self, symbol: str, period: str, adjust: str) -> set[date]:
        rows = self._db.scalars(
            select(HistoricalBar.trade_date).where(
                HistoricalBar.symbol == symbol,
                HistoricalBar.period == period,
                HistoricalBar.adjust == adjust,
            )
        ).all()
        return set(rows)

    @staticmethod
    def _missing_range(
        start: date, end: date, existing: set[date]
    ) -> tuple[date | None, date | None]:
        """计算缺失区间。已完整覆盖返回 (None, None)。"""
        if not existing:
            return start, end
        # 简单策略：若区间端点都在已有数据中，则视为覆盖
        # 更精细的中间缺口检测由调用方按需补拉
        if start in existing and end in existing:
            return None, None
        return start, end

    @staticmethod
    def _covers(bars: list[QuoteData], start: datetime, end: datetime) -> bool:
        """判断缓存是否覆盖请求区间（端点都在即可视为完整）。"""
        if not bars:
            return False
        dates = {b.market_time.date() for b in bars if b.market_time}
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = end.date() if isinstance(end, datetime) else end
        return start_date in dates and end_date in dates

    @staticmethod
    def _normalize_adjust(adjust: str) -> str:
        adjust = (adjust or ADJUST_NONE).lower()
        if adjust not in _VALID_ADJUST:
            return ADJUST_NONE
        return adjust

    @staticmethod
    def _bar_to_quote(row: HistoricalBar) -> QuoteData:
        return QuoteData(
            symbol=row.symbol,
            name=row.symbol,
            price=float(row.close or 0),
            open=float(row.open or 0),
            high=float(row.high or 0),
            low=float(row.low or 0),
            previous_close=0.0,
            volume=float(row.volume or 0),
            amount=float(row.amount or 0),
            bid_price=0.0,
            ask_price=0.0,
            source=row.source or "akshare",
            market_time=datetime.combine(row.trade_date, datetime.min.time()),
            received_at=row.fetched_at or utc_now(),
            is_stale=False,
        )


def _akshare_fetch(symbol: str, adjust: str, start: date, end: date) -> list[QuoteData]:
    """AKShare 同步阻塞调用（在线程池中执行）。"""
    import akshare as ak  # type: ignore

    ak_adjust = "" if adjust == ADJUST_NONE else adjust
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust=ak_adjust,
    )
    if df is None or df.empty:
        return []

    bars: list[QuoteData] = []
    for _, row in df.iterrows():
        bars.append(
            QuoteData(
                symbol=symbol,
                name=symbol,
                price=float(row.get("收盘", 0) or 0),
                open=float(row.get("开盘", 0) or 0),
                high=float(row.get("最高", 0) or 0),
                low=float(row.get("最低", 0) or 0),
                previous_close=0.0,
                volume=float(row.get("成交量", 0) or 0),
                amount=float(row.get("成交额", 0) or 0),
                bid_price=0.0,
                ask_price=0.0,
                source="akshare",
                market_time=datetime.combine(
                    _parse_date(str(row.get("日期"))), datetime.min.time()
                ),
                received_at=utc_now(),
                is_stale=False,
            )
        )
    return bars


def _exchange_prefix(symbol: str) -> str:
    """symbol → 小写交易所前缀（sh / sz / bj），无法判定时抛错。"""
    head = (symbol or "")[:1]
    if head == "6":
        return "sh"
    if head in ("0", "3"):
        return "sz"
    if head in ("4", "8", "9"):
        return "bj"
    raise ValueError(f"无法从 {symbol!r} 推断交易所前缀")


def _coerce_day(raw: object) -> date:
    """把 date / datetime / pandas Timestamp / 字符串统一成 date。"""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return _parse_date(str(raw))


def _make_bar(
    symbol: str,
    source: str,
    day: date,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    amount: float,
) -> QuoteData:
    """构造历史行情（各数据源共用，字段口径保持一致）。"""
    return QuoteData(
        symbol=symbol,
        name=symbol,
        price=close,
        open=open_price,
        high=high,
        low=low,
        previous_close=0.0,
        volume=volume,
        amount=amount,
        bid_price=0.0,
        ask_price=0.0,
        source=source,
        market_time=datetime.combine(day, datetime.min.time()),
        received_at=utc_now(),
        is_stale=False,
    )


def _akshare_sina_fetch(
    symbol: str, adjust: str, start: date, end: date
) -> list[QuoteData]:
    """新浪日线（akshare.stock_zh_a_daily）：EastMoney 不可达时的第一备选。"""
    import akshare as ak  # type: ignore

    prefix = _exchange_prefix(symbol)
    df = ak.stock_zh_a_daily(
        symbol=f"{prefix}{symbol}",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="" if adjust == ADJUST_NONE else adjust,
    )
    if df is None or df.empty:
        return []

    bars: list[QuoteData] = []
    for _, row in df.iterrows():
        bars.append(
            _make_bar(
                symbol,
                "akshare_sina",
                _coerce_day(row.get("date")),
                float(row.get("open") or 0),
                float(row.get("high") or 0),
                float(row.get("low") or 0),
                float(row.get("close") or 0),
                float(row.get("volume") or 0),
                float(row.get("amount") or 0),
            )
        )
    return bars


_BAOSTOCK_ADJUSTFLAG = {ADJUST_NONE: "3", ADJUST_QFQ: "2", ADJUST_HFQ: "1"}


def _baostock_fetch(symbol: str, adjust: str, start: date, end: date) -> list[QuoteData]:
    """BaoStock 日线（query_history_k_data_plus）：EastMoney/Sina 都不可用时的兜底。

    BaoStock 不覆盖北交所（实测 bj.* 返回 10004011），北交所标的只能靠
    AKShare/Sina；两者都失败时该股票会被记为失败并进入覆盖证据。
    """
    import socket

    import baostock as bs  # type: ignore

    prefix = _exchange_prefix(symbol)
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_BAOSTOCK_SOCKET_TIMEOUT_SECONDS)
    try:
        login = bs.login()
        if getattr(login, "error_code", "1") != "0":
            raise RuntimeError(
                f"baostock 登录失败: {getattr(login, 'error_msg', 'unknown')}"
            )
        try:
            rs = bs.query_history_k_data_plus(
                f"{prefix}.{symbol}",
                "date,open,high,low,close,volume,amount",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag=_BAOSTOCK_ADJUSTFLAG.get(adjust, "3"),
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock 查询失败: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    finally:
        socket.setdefaulttimeout(previous_timeout)

    bars: list[QuoteData] = []
    for row in rows:
        # fields: date,open,high,low,close,volume,amount；停牌日价格为空字符串
        if len(row) < 7 or not row[1] or not row[4]:
            continue
        bars.append(
            _make_bar(
                symbol,
                "baostock",
                _parse_date(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5] or 0),
                float(row[6] or 0),
            )
        )
    return bars


def _fetch_from_sources(
    symbol: str,
    adjust: str,
    start: date,
    end: date,
    skip: tuple[str, ...] = (),
) -> list[QuoteData]:
    """按回退链顺序取第一个有数据的源。

    任一源抛错不终止整体流程；只有"所有源都抛错"才算失败（抛 RuntimeError），
    "所有源都返回空"视为该股票无数据（停牌/退市），返回空列表。

    skip 里的源会被跳过（调用方已经用 ProviderManager 打过同一上游）。
    """
    errors: list[str] = []
    saw_empty = False
    for name, fetcher in (
        ("akshare", _akshare_fetch),
        ("akshare_sina", _akshare_sina_fetch),
        ("baostock", _baostock_fetch),
    ):
        if name in skip:
            continue
        try:
            bars = fetcher(symbol, adjust, start, end)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            logger.warning("历史源 %s 拉取 %s 失败: %s", name, symbol, exc)
            continue
        if bars:
            if name != "akshare":
                logger.info("历史源 %s 命中 %s（%d 条）", name, symbol, len(bars))
            return bars
        saw_empty = True
    if errors and not saw_empty:
        raise RuntimeError("; ".join(errors))
    return []


def provider_covered_sources(provider_manager) -> tuple[str, ...]:
    """返回 ProviderManager 已覆盖的回退链源名，避免重复请求同一上游。

    MARKET_PROVIDERS 含 akshare 或 eastmoney 时，manager 内部已经打过东方财富
    kline 接口（stock_zh_a_hist / push2his），回退链不必再打一次——该通道实测经常
    RemoteDisconnected，重复调用会让全市场入库白白翻倍耗时。
    """
    if provider_manager is None:
        return ()
    names = {
        getattr(provider, "name", "")
        for provider in getattr(provider_manager, "providers", [])
    }
    return ("akshare",) if {"akshare", "eastmoney"} & names else ()


async def fetch_history_from_sources(
    symbol: str,
    start: date,
    end: date,
    adjust: str = ADJUST_NONE,
    skip: tuple[str, ...] = (),
) -> list[QuoteData]:
    """只读地走多源回退链取**日线**，不写数据库。

    供只读接口（如指标计算）在 ProviderManager 拿不到数据时兜底，覆盖沪 / 深 /
    北交所全部标的；与入库路径共用同一套源，口径一致。
    全部源都报错时抛异常，由调用方决定如何降级。
    """
    return await asyncio.to_thread(
        _fetch_from_sources, symbol, adjust, start, end, skip
    )


def _parse_date(raw: str) -> date:
    """解析 AKShare 返回的日期字符串。"""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {raw}")


def _filter_valid_bars(bars: list[QuoteData]) -> list[QuoteData]:
    """过滤脏数据：负成交量、OHLC 不自洽、价格缺失，并按时间去重。

    不把不完整数据静默交给回测引擎——这里在落库前拦截。
    """
    seen: set = set()
    valid: list[QuoteData] = []
    for b in bars:
        # 负成交量
        if b.volume < 0:
            continue
        # OHLC 不自洽（最高 < 最低）
        if b.high > 0 and b.low > 0 and b.high < b.low:
            continue
        # 关键价格全缺失
        if b.price <= 0 and b.open <= 0 and b.high <= 0 and b.low <= 0:
            continue
        # 按时间去重（保留首次出现）
        t = b.market_time
        if t is not None:
            if t in seen:
                continue
            seen.add(t)
        valid.append(b)
    return valid


def merge_gap_ranges(
    missing_dates: list[date], max_gap_days: int = 5
) -> list[tuple[date, date]]:
    """将缺失日期列表合并为连续下载区间。

    - 相邻缺失日期跨度 ≤ max_gap_days 天 → 合并到同一区间
    - 跨度 > max_gap_days 天 → 拆分为两段，避免一次拉太多浪费带宽
    """
    if not missing_dates:
        return []
    sorted_dates = sorted(missing_dates)
    ranges: list[tuple[date, date]] = []
    seg_start = sorted_dates[0]
    seg_end = sorted_dates[0]
    for d in sorted_dates[1:]:
        gap = (d - seg_end).days
        if gap <= max_gap_days:
            seg_end = d
        else:
            ranges.append((seg_start, seg_end))
            seg_start = d
            seg_end = d
    ranges.append((seg_start, seg_end))
    return ranges
