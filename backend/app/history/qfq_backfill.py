"""全市场前复权（qfq）日线回填。

把 ``historical_bars`` 里 ``adjust='none'`` 的未复权日线，配合通达信除权除息
数据（``get_xdxr_info``）在本地推导出 ``adjust='qfq'`` 的前复权日线，写回同一张
表（表上已有 ``(symbol, period, adjust, trade_date)`` 唯一约束，不需要迁移）。

设计约束
--------
- **幂等**：每只标的按「先删后插」整体重写，重复执行结果完全一致；
- **不碰其它数据**：只写 ``adjust='qfq'`` 的行，不改 ``adjust='none'``，不改其它表；
- **失败不等于「没有事件」**：拉不到 xdxr 时该标的记为失败并跳过写库，绝不用
  「全 1.0 因子」冒充前复权，否则会把不复权数据伪装成复权数据；
- tdxpy 的 API 对象不是线程安全的，每个 worker 线程独占一条连接、线程内串行。

**分析结果仅用于研究，不构成投资建议。**
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import HistoricalBar
from app.database.session import SessionLocal
from app.history.adjust import compute_qfq_factors, parse_xdxr_events
from app.history.service import ADJUST_NONE, ADJUST_QFQ
from app.market_data.tdx_provider import (
    HOST_COOLDOWN_SECONDS,
    MARKET_SH,
    PROBE_SYMBOL,
    TDX_HOSTS,
    TDX_PORT,
    to_tdx_market,
)
from app.time_utils import utc_now

try:  # pragma: no cover - 未安装 tdxpy 时模块仍可导入
    from tdxpy.hq import TdxHq_API  # type: ignore

    _TDX_AVAILABLE = True
except ImportError:
    TdxHq_API = None  # type: ignore
    _TDX_AVAILABLE = False

logger = logging.getLogger(__name__)

# 写库时标记数据来源，便于与 'tdx'（未复权）区分
QFQ_SOURCE = "tdx-qfq"
# 价格保留 6 位小数（A 股最小报价单位 0.01，6 位足够且不放大行体积）
PRICE_DECIMALS = 6
# 一次 xdxr 请求失败后的重试次数（换连接重试）
FETCH_RETRIES = 2

ProgressCallback = Callable[[int, int], None]


def _to_decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), PRICE_DECIMALS)))


def _default_api_factory() -> Any:
    if TdxHq_API is None:
        raise RuntimeError("未安装 tdxpy，无法获取除权除息数据（pip install tdxpy）")
    return TdxHq_API(heartbeat=False)


@dataclass(frozen=True)
class SymbolAdjustResult:
    """单只标的的回填结果。``error`` 非空表示跳过写库。"""

    symbol: str
    bars: int = 0
    events: int = 0
    applied: int = 0
    unresolved: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class QfqBackfillReport:
    """一次全市场回填的汇总结果。"""

    started_at: str
    finished_at: str
    elapsed_seconds: float
    period: str
    source_adjust: str
    target_adjust: str
    symbols_total: int
    symbols_ok: int
    symbols_empty: int
    symbols_failed: int
    rows_written: int
    events_applied: int
    unresolved_symbols: tuple[tuple[str, int], ...] = field(default=())
    failures: tuple[tuple[str, str], ...] = field(default=())

    def as_dict(self, failure_limit: int = 50) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds,
            "period": self.period,
            "source_adjust": self.source_adjust,
            "target_adjust": self.target_adjust,
            "symbols_total": self.symbols_total,
            "symbols_ok": self.symbols_ok,
            "symbols_empty": self.symbols_empty,
            "symbols_failed": self.symbols_failed,
            "rows_written": self.rows_written,
            "events_applied": self.events_applied,
            "unresolved_count": len(self.unresolved_symbols),
            "unresolved_symbols": dict(self.unresolved_symbols[:failure_limit]),
            "failure_count": len(self.failures),
            "failures": dict(self.failures[:failure_limit]),
        }


class _TdxXdxrSession:
    """线程独占的通达信连接，只用来拉除权除息。

    服务端掐断连接是常态，因此每次请求失败都会丢弃连接并在下次请求时重连，
    两次都失败才向调用方抛错（宁可不写，也不能写错）。
    """

    def __init__(
        self,
        api_factory: Callable[[], Any] | None = None,
        hosts: Sequence[str] = TDX_HOSTS,
        timeout: float = 8.0,
    ) -> None:
        self._api_factory = api_factory or _default_api_factory
        self._hosts = tuple(hosts)
        self._timeout = timeout
        self._api: Any | None = None
        self._host: str | None = None
        self._cooldown: dict[str, float] = {}

    @property
    def host(self) -> str | None:
        return self._host

    @property
    def is_open(self) -> bool:
        return self._api is not None

    def _close_quietly(self, api: Any) -> None:
        if api is None:
            return
        try:
            api.disconnect()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响主流程
            logger.debug("关闭通达信连接失败：%s", exc)

    def reset(self) -> None:
        api, self._api = self._api, None
        self._host = None
        self._close_quietly(api)

    def close(self) -> None:
        self.reset()

    def connect(self) -> bool:
        """按候选主机顺序连接并探活；全部失败返回 False。"""
        now = time.monotonic()
        for host in self._hosts:
            if self._cooldown.get(host, 0.0) > now:
                continue
            api = self._api_factory()
            try:
                if not api.connect(host, TDX_PORT, time_out=self._timeout):
                    raise RuntimeError("connect() 返回 False")
                # 实测有主机 TCP 可连却不供数，必须用一次真实请求探活
                if not api.get_security_quotes([(MARKET_SH, PROBE_SYMBOL)]):
                    raise RuntimeError("探活请求返回空数据")
            except Exception as exc:  # noqa: BLE001 - 换下一台继续试
                logger.debug("通达信主机 %s 不可用：%s", host, exc)
                self._close_quietly(api)
                self._cooldown[host] = time.monotonic() + HOST_COOLDOWN_SECONDS
                continue
            self._api, self._host = api, host
            logger.info("前复权回填：已连接通达信 %s", host)
            return True
        return False

    def fetch_xdxr(self, symbol: str) -> list[dict[str, Any]]:
        """拉取除权除息原始记录；无法获取时抛异常（不返回空列表顶替）。"""
        market = to_tdx_market(symbol)
        if market is None:
            raise ValueError(f"无法识别市场：{symbol}")
        last_error: Exception | None = None
        for _ in range(FETCH_RETRIES):
            if self._api is None and not self.connect():
                raise RuntimeError("通达信全部候选主机当前不可用")
            try:
                return list(self._api.get_xdxr_info(market, symbol) or [])
            except Exception as exc:  # noqa: BLE001 - 换连接重试
                last_error = exc
                self.reset()
        raise RuntimeError(f"get_xdxr_info 连续失败：{last_error}")


class QfqBackfillService:
    """把未复权日线换算成前复权日线并写回 ``historical_bars``。"""

    def __init__(
        self,
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        *,
        api_factory: Callable[[], Any] | None = None,
        hosts: Sequence[str] = TDX_HOSTS,
        timeout: float = 8.0,
        workers: int = 4,
        batch_size: int = 120,
    ) -> None:
        if workers < 1:
            raise ValueError("workers 必须 >= 1")
        if batch_size < 1:
            raise ValueError("batch_size 必须 >= 1")
        self._session_factory = session_factory or SessionLocal
        self._api_factory = api_factory
        self._hosts = tuple(hosts)
        self._timeout = timeout
        self._workers = workers
        self._batch_size = batch_size

    # ─────────── 主流程 ───────────

    def run(
        self,
        *,
        symbols: Iterable[str] | None = None,
        period: str = "daily",
        source_adjust: str = ADJUST_NONE,
        target_adjust: str = ADJUST_QFQ,
        progress: ProgressCallback | None = None,
    ) -> QfqBackfillReport:
        """回填指定标的（缺省为本地全部有未复权日线的标的）。"""
        started_perf = time.perf_counter()
        started_at = utc_now().isoformat()
        targets = list(symbols) if symbols is not None else self._list_symbols(period, source_adjust)
        total = len(targets)
        logger.info(
            "前复权回填开始：%d 只标的，%d 线程，%s → %s",
            total, self._workers, source_adjust, target_adjust,
        )

        results: list[SymbolAdjustResult] = []
        if total:
            chunks = [
                targets[index : index + self._batch_size]
                for index in range(0, total, self._batch_size)
            ]
            done = 0
            counter_lock = threading.Lock()
            with ThreadPoolExecutor(
                max_workers=self._workers, thread_name_prefix="qfq"
            ) as pool:
                futures = [
                    pool.submit(
                        self._worker, chunk, period, source_adjust, target_adjust
                    )
                    for chunk in chunks
                ]
                for future in as_completed(futures):
                    try:
                        chunk_results = future.result()
                    except Exception as exc:  # noqa: BLE001 - 单批崩溃不影响其它批
                        logger.exception("前复权回填批次异常")
                        chunk_results = [
                            SymbolAdjustResult(symbol="?", error=f"批次异常：{exc}")
                        ]
                    with counter_lock:
                        results.extend(chunk_results)
                        done += len(chunk_results)
                        snapshot = done
                    if progress is not None:
                        progress(snapshot, total)

        ok = [item for item in results if item.ok and item.bars > 0]
        empty = [item for item in results if item.ok and item.bars == 0]
        failed = [item for item in results if not item.ok]
        report = QfqBackfillReport(
            started_at=started_at,
            finished_at=utc_now().isoformat(),
            elapsed_seconds=round(time.perf_counter() - started_perf, 2),
            period=period,
            source_adjust=source_adjust,
            target_adjust=target_adjust,
            symbols_total=total,
            symbols_ok=len(ok),
            symbols_empty=len(empty),
            symbols_failed=len(failed),
            rows_written=sum(item.bars for item in ok),
            events_applied=sum(item.applied for item in ok),
            unresolved_symbols=tuple(
                (item.symbol, item.unresolved) for item in ok if item.unresolved
            ),
            failures=tuple((item.symbol, item.error) for item in failed),
        )
        logger.info(
            "前复权回填完成：成功 %d / 空数据 %d / 失败 %d，写入 %d 行，用时 %.1fs",
            report.symbols_ok, report.symbols_empty, report.symbols_failed,
            report.rows_written, report.elapsed_seconds,
        )
        return report

    # ─────────── 数据访问 ───────────

    def _list_symbols(self, period: str, adjust: str) -> list[str]:
        with self._session_factory() as db:
            rows = db.execute(
                select(HistoricalBar.symbol)
                .where(
                    HistoricalBar.period == period,
                    HistoricalBar.adjust == adjust,
                )
                .group_by(HistoricalBar.symbol)
                .order_by(HistoricalBar.symbol)
            ).scalars()
            return list(rows.all())

    def _load_bars(
        self, symbol: str, period: str, adjust: str
    ) -> list[tuple[date, Any, Any, Any, Any, Any, Any]]:
        with self._session_factory() as db:
            return list(
                db.execute(
                    select(
                        HistoricalBar.trade_date,
                        HistoricalBar.open,
                        HistoricalBar.high,
                        HistoricalBar.low,
                        HistoricalBar.close,
                        HistoricalBar.volume,
                        HistoricalBar.amount,
                    )
                    .where(
                        HistoricalBar.symbol == symbol,
                        HistoricalBar.period == period,
                        HistoricalBar.adjust == adjust,
                    )
                    .order_by(HistoricalBar.trade_date)
                ).all()
            )

    def _write_bars(
        self,
        symbol: str,
        period: str,
        adjust: str,
        bars: Sequence[tuple[date, Any, Any, Any, Any, Any, Any]],
        factors: Sequence[float],
    ) -> int:
        """整体重写该标的在目标复权口径下的全部日线。"""
        fetched_at = utc_now()
        payload = [
            {
                "symbol": symbol,
                "period": period,
                "adjust": adjust,
                "trade_date": row[0],
                "open": _to_decimal(float(row[1] or 0.0) * factors[index]),
                "high": _to_decimal(float(row[2] or 0.0) * factors[index]),
                "low": _to_decimal(float(row[3] or 0.0) * factors[index]),
                "close": _to_decimal(float(row[4] or 0.0) * factors[index]),
                "volume": float(row[5] or 0.0),
                "amount": float(row[6] or 0.0),
                "source": QFQ_SOURCE,
                "fetched_at": fetched_at,
            }
            for index, row in enumerate(bars)
        ]
        table = HistoricalBar.__table__
        with self._session_factory() as db:
            db.execute(
                delete(table).where(
                    table.c.symbol == symbol,
                    table.c.period == period,
                    table.c.adjust == adjust,
                )
            )
            if payload:
                db.execute(insert(table), payload)
            db.commit()
        return len(payload)

    # ─────────── worker ───────────

    def _worker(
        self,
        chunk: Sequence[str],
        period: str,
        source_adjust: str,
        target_adjust: str,
    ) -> list[SymbolAdjustResult]:
        session = _TdxXdxrSession(self._api_factory, self._hosts, self._timeout)
        try:
            return [
                self._adjust_symbol(
                    session, symbol, period, source_adjust, target_adjust
                )
                for symbol in chunk
            ]
        finally:
            session.close()

    def _adjust_symbol(
        self,
        session: _TdxXdxrSession,
        symbol: str,
        period: str,
        source_adjust: str,
        target_adjust: str,
    ) -> SymbolAdjustResult:
        try:
            bars = self._load_bars(symbol, period, source_adjust)
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整批
            return SymbolAdjustResult(symbol=symbol, error=f"读取日线失败：{exc}")
        if not bars:
            return SymbolAdjustResult(symbol=symbol)
        try:
            raw = session.fetch_xdxr(symbol)
        except Exception as exc:  # noqa: BLE001 - 拿不到除权除息就跳过，不写错数据
            return SymbolAdjustResult(symbol=symbol, error=f"除权除息获取失败：{exc}")

        days = [row[0] for row in bars]
        closes = [float(row[4] or 0.0) for row in bars]
        events = parse_xdxr_events(raw, earliest=days[0])
        series = compute_qfq_factors(days, closes, events)
        try:
            written = self._write_bars(
                symbol, period, target_adjust, bars, series.factors
            )
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整批
            return SymbolAdjustResult(symbol=symbol, error=f"写入失败：{exc}")
        return SymbolAdjustResult(
            symbol=symbol,
            bars=written,
            events=len(events),
            applied=series.applied_events,
            unresolved=len(series.unresolved_events),
        )