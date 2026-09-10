"""历史行情本地化服务。

从 AKShare 获取历史 K 线，保存到本地数据库缓存，支持：
- 增量同步（不重复下载已有数据）
- 前复权 / 后复权 / 不复权，并在数据中保存复权标识
- 网络失败回退到缓存，并明确提示数据更新时间
- 连接/读取超时、有限重试与指数退避
- 大批量分块处理
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

    def __init__(self, db: Session, checker: DataQualityChecker | None = None):
        self._db = db
        self._checker = checker or DataQualityChecker()

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

        - 网络失败时返回缓存数据（source="cache"），并提示更新时间。
        - sync_if_incomplete=False 时只读缓存，不触发同步。
        """
        adjust = self._normalize_adjust(adjust)
        cached = self.get_cached(symbol, start, end, period, adjust)

        if cached:
            quality = self._checker.check(cached)
            updated_at = max((b.received_at for b in cached), default=None)
            # 缓存覆盖完整区间则直接返回
            if self._covers(cached, start, end):
                return HistoryResult(
                    bars=cached,
                    source="cache",
                    data_updated_at=updated_at,
                    is_complete=True,
                    quality=quality,
                )

        if not sync_if_incomplete:
            # 只读模式：返回已有缓存并标记不完整
            quality = self._checker.check(cached) if cached else QualityReport(total=0)
            updated_at = max((b.received_at for b in cached), default=None) if cached else None
            return HistoryResult(
                bars=cached,
                source="cache",
                data_updated_at=updated_at,
                is_complete=False,
                quality=quality,
            )

        # 尝试增量同步
        try:
            added = await self.sync(symbol, start, end, period, adjust)
        except Exception as exc:  # noqa: BLE001
            logger.warning("历史数据同步失败，回退缓存: %s", exc)
            added = 0

        merged = self.get_cached(symbol, start, end, period, adjust)
        quality = self._checker.check(merged)
        updated_at = max((b.received_at for b in merged), default=None) if merged else None
        source = "akshare" if added > 0 else "cache"
        if added > 0 and cached:
            source = "mixed"

        return HistoryResult(
            bars=merged,
            source=source,
            data_updated_at=updated_at,
            is_complete=self._covers(merged, start, end),
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

    # ──────── 内部方法 ────────

    async def _fetch_with_retry(
        self, symbol: str, adjust: str, start: date, end: date
    ) -> list[QuoteData]:
        """带超时与指数退避重试的 AKShare 拉取。"""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        _akshare_fetch, symbol, adjust, start, end
                    ),
                    timeout=_FETCH_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BASE_SECONDS * (2 ** attempt)
                    logger.warning(
                        "AKShare 拉取 %s 第 %d 次失败，%.1f 秒后重试: %s",
                        symbol, attempt + 1, wait, exc,
                    )
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

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
