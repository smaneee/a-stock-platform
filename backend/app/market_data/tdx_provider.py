"""通达信（TDX）行情数据源。

通过通达信行情服务器的二进制协议（TCP 7709）取实时行情与 K 线。相比东财 /
腾讯的 HTTP 轮询，它的延迟更低、也不容易被单个 IP 的突发限流打断，适合秒级
刷新的看盘场景。实测（2026-09-12，本机到 ``117.34.114.27``）：

- 一次批量取 66 只实时行情：约 51ms
- 一次取 800 根日 K：约 95ms
- 稳态单次调用：约 35ms

对照东财 HTTP 单次 200~500ms，且连续请求会被断连。

口径说明
--------

- ``vol`` 字段单位是「手」，写入 ``QuoteData.volume`` 时乘 100 转成「股」。
- ``amount`` 字段单位是「元」。
- ``servertime`` 只有时分秒，与当天日期拼成 ``market_time``。
- 行情接口不返回股票名称，名称由注入的 ``name_resolver`` 补齐（见下）。
- **不支持北交所**（4 / 8 / 920 开头）：tdxpy 对这些代码直接返回 ``None``，
  因此它们不会出现在结果里，由上层 ``ProviderManager`` 回退到东财。

已知限制
--------

- tdxpy 是同步阻塞库，所有调用都放进线程池并串行化，不阻塞事件循环。
- 实测多台候选服务器虽然 TCP 能连通，却对行情请求返回空列表（只服务特定
  客户），因此连上后必须**探活**，失败的主机进入冷却再换下一台。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any, Callable, Sequence

from app.market_data.base import MarketDataProvider, QuoteData
from app.market_data.indices import IndexMeta, resolve_index
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

try:
    from tdxpy.hq import TdxHq_API  # type: ignore

    _TDX_AVAILABLE = True
except ImportError:  # pragma: no cover - 未安装 tdxpy 时优雅降级
    TdxHq_API = None  # type: ignore
    _TDX_AVAILABLE = False

# 协议固定值，与 tdxpy.constants.TDXParams 中同名常量一致；
# 这里重复一份是为了在未安装 tdxpy 时模块仍可导入。
MARKET_SZ = 0
MARKET_SH = 1
# 北交所（含新三板）在通达信协议里的市场号：**请求要传 3**，服务端回包里的
# market 字段是 2。实测 (3, "920819") 与 [(1, "600519"), (3, "920819")] 混合
# 批量都能一次返回，因此北交所不再需要回退到东财。
MARKET_BJ = 3
# 与 TDXParams.KLINE_TYPE_* 一致
KLINE_TYPE_BY_PERIOD: dict[str, int] = {
    "daily": 9,
    "weekly": 5,
    "monthly": 6,
    "60m": 3,
    "30m": 2,
    "15m": 1,
    "5m": 0,
    "1m": 8,
}

TDX_PORT = 7709
# 一次批量请求的协议上限
MAX_SYMBOLS_PER_CALL = 80
# 一次 K 线请求的协议上限
MAX_KLINE_COUNT = 800
# 分页上限，防止时钟异常时无限翻页
MAX_KLINE_PAGES = 40
# 1 手 = 100 股；通达信的成交量字段单位是「手」
LOT_SIZE = 100
# 成交量/成交额的有效下限：低于它的都是协议解码出的非规格化脏值，按 0 处理
VOLUME_EPSILON = 1e-6
# 通达信 K 线成交量字段的单位随周期而变（实测，见 tests/test_tdx_provider.py）：
# 日/周/月/季/年线给「手」，分钟线（1/5/15/30/60 分钟）直接给「股」。
# 出口统一换算成「股」，与 QuoteData 的约定保持一致。
LOT_VOLUME_CATEGORIES = frozenset({4, 5, 6, 9, 10, 11})

# 候选服务器：实测只有第一台能稳定供数，其余作为热备
TDX_HOSTS: tuple[str, ...] = (
    "117.34.114.27",
    "180.153.18.170",
    "124.71.187.122",
    "115.238.56.198",
    "60.191.117.167",
    "218.75.126.9",
)
# 探活用的基准标的（贵州茅台，沪市主板，不可能停牌）
PROBE_SYMBOL = "600519"
# 连接成功后的复用时长；到期后重新探活，避免服务器静默失效
HOST_TTL_SECONDS = 300.0
# 探活失败后的冷却时长
HOST_COOLDOWN_SECONDS = 120.0


def to_tdx_market(symbol: str) -> int | None:
    """A 股代码转通达信市场号；无法识别时返回 None。"""
    raw = (symbol or "").strip().lower()
    prefix = ""
    if raw.startswith(("sh", "sz", "bj")):
        prefix, raw = raw[:2], raw[2:]
    if not raw.isdigit():
        return None
    if prefix == "bj":
        return MARKET_BJ
    if prefix == "sh":
        return MARKET_SH
    if prefix == "sz":
        return MARKET_SZ
    # 北交所：4xxxxx / 8xxxxx 老号段，920xxx 新号段
    if raw.startswith(("4", "8")) or raw.startswith("92"):
        return MARKET_BJ
    if raw.startswith(("6", "5", "900")):
        return MARKET_SH
    return MARKET_SZ


def _number(raw: Any) -> float:
    """转 float；缺失或非数字一律按 0 处理。"""
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _volume_number(raw: Any) -> float:
    """成交量/成交额专用转换：把协议里的非规格化脏值归零。

    通达信的这两个字段走「协议浮点」解码，服务端把字段填 0 时会解出 2^-127
    量级的非规格化小数（实测 5.877e-39，等于 0 却又不等于 0），直接落库或
    返回给前端都会变成脏数据。
    """
    value = _number(raw)
    return 0.0 if abs(value) < VOLUME_EPSILON else value


def _parse_server_time(raw: Any) -> datetime | None:
    """``"15:17:46.110"`` 拼上当天日期；解析失败返回 None。"""
    text = "" if raw is None else str(raw).strip()
    if len(text) < 8:
        return None
    try:
        parsed = datetime.strptime(text[:8], "%H:%M:%S")
    except ValueError:
        return None
    return datetime.combine(datetime.now().date(), parsed.time())


def _bar_time(row: dict) -> datetime | None:
    """K 线行转 datetime；字段缺失返回 None。"""
    try:
        return datetime(
            int(_number(row.get("year"))),
            int(_number(row.get("month"))),
            int(_number(row.get("day"))),
            int(_number(row.get("hour"))),
            int(_number(row.get("minute"))),
        )
    except (TypeError, ValueError):
        return None


def _bar_volume(raw: Any, kline_type: int) -> float:
    """K 线成交量换算成「股」。

    通达信日/周/月/季/年线的成交量字段是「手」，分钟线（1/5/15/30/60 分钟）
    却直接是「股」；统一乘算后出口口径才是「股」。指数 K 线的量纲同样由协议
    决定，这里不做额外缩放。
    """
    value = _volume_number(raw)
    if kline_type in LOT_VOLUME_CATEGORIES:
        return value * LOT_SIZE
    return value


def validate_quote(quote: QuoteData) -> bool:
    """数据合理性校验，规则与腾讯 / 东财数据源保持一致。"""
    if not quote.symbol or quote.price <= 0:
        return False
    if quote.high > 0 and quote.low > 0 and quote.high < quote.low:
        return False
    if quote.previous_close > 0:
        if not (
            0.8 * quote.previous_close <= quote.price <= 1.2 * quote.previous_close
        ):
            return False
    name = quote.name or ""
    if any(ch in name for ch in ("<", ">", "&", '"', "'")):
        return False
    return True


def parse_quote_row(row: dict, name: str = "") -> QuoteData | None:
    """把 tdxpy 的实时行情行转成 QuoteData；不合格返回 None。"""
    symbol = str(row.get("code") or "").strip()
    if not symbol:
        return None
    quote = QuoteData(
        symbol=symbol,
        name=name,
        price=_number(row.get("price")),
        open=_number(row.get("open")),
        high=_number(row.get("high")),
        low=_number(row.get("low")),
        previous_close=_number(row.get("last_close")),
        volume=_volume_number(row.get("vol")) * LOT_SIZE,
        amount=_volume_number(row.get("amount")),
        bid_price=_number(row.get("bid1")),
        ask_price=_number(row.get("ask1")),
        source="tdx",
        market_time=_parse_server_time(row.get("servertime")),
        received_at=utc_now(),
        is_stale=False,
    )
    return quote if validate_quote(quote) else None


def _default_api_factory():
    """延迟导入 tdxpy，未安装时给出可读错误。"""
    if TdxHq_API is None:
        raise RuntimeError("未安装 tdxpy，无法使用通达信数据源（pip install tdxpy）")
    return TdxHq_API(heartbeat=False)


class TdxProvider(MarketDataProvider):
    """通达信行情数据源：同步阻塞的 tdxpy 包装为异步。"""

    name = "tdx"
    upstream = "tdx"

    def __init__(
        self,
        hosts: Sequence[str] = TDX_HOSTS,
        timeout: float = 3.0,
        name_resolver: Callable[[Sequence[str]], dict[str, str]] | None = None,
        api_factory: Callable[[], Any] | None = None,
    ):
        self._hosts = tuple(hosts)
        self._timeout = timeout
        # tdxpy 行情接口不返回名称；由上层注入解析器（通常读本地股票主数据）
        self._name_resolver = name_resolver
        self._api_factory = api_factory or _default_api_factory
        self._api: Any | None = None
        self._host: str | None = None
        self._connected_at = 0.0
        self._cooldown: dict[str, float] = {}
        # tdxpy 的 API 对象不是线程安全的，所有调用串行化
        self._lock = asyncio.Lock()
        if not _TDX_AVAILABLE and api_factory is None:
            logger.warning("未安装 tdxpy，通达信数据源不可用（pip install tdxpy）")

    @staticmethod
    def _disconnect_blocking(api: Any) -> None:
        if api is None:
            return
        try:
            api.disconnect()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响后续流程
            logger.warning("关闭通达信连接失败：%s", exc)

    def _drop_connection_blocking(self) -> None:
        api, self._api = self._api, None
        self._host = None
        self._connected_at = 0.0
        self._disconnect_blocking(api)

    def _open_blocking(self, host: str) -> Any | None:
        """连上 host 并探活；失败返回 None（调用方负责冷却）。"""
        api = self._api_factory()
        try:
            if not api.connect(host, TDX_PORT, time_out=self._timeout):
                raise RuntimeError("connect() 返回 False")
            # 实测多台服务器 TCP 可连却不供数，必须用一次真实请求探活
            if not api.get_security_quotes([(MARKET_SH, PROBE_SYMBOL)]):
                raise RuntimeError("探活请求返回空数据（该服务器不对外供数）")
            return api
        except Exception as exc:  # noqa: BLE001 - 换下一台继续尝试
            logger.warning("通达信主机 %s 不可用：%s", host, exc)
            self._disconnect_blocking(api)
            return None

    def _ensure_api_blocking(self) -> Any | None:
        now = time.monotonic()
        if self._api is not None and now - self._connected_at < HOST_TTL_SECONDS:
            return self._api
        if self._api is not None:
            host = self._host
            self._drop_connection_blocking()
            if host is not None:
                api = self._open_blocking(host)
                if api is not None:
                    self._api, self._host, self._connected_at = api, host, now
                    return api
                self._cooldown[host] = now + HOST_COOLDOWN_SECONDS
        for host in self._hosts:
            if self._cooldown.get(host, 0.0) > now:
                continue
            api = self._open_blocking(host)
            if api is None:
                self._cooldown[host] = now + HOST_COOLDOWN_SECONDS
                continue
            self._api, self._host, self._connected_at = api, host, now
            logger.info("通达信数据源已连接到 %s", host)
            return api
        logger.warning("通达信数据源：全部候选主机当前不可用")
        return None

    async def _call(self, operation: Callable[[Any], Any], default: Any) -> Any:
        """在锁内确保有可用连接，再执行 operation(api)；任何失败都返回 default。"""
        async with self._lock:
            try:
                api = await asyncio.to_thread(self._ensure_api_blocking)
            except Exception as exc:  # noqa: BLE001
                logger.warning("通达信建立连接异常：%s", exc)
                return default
            if api is None:
                return default
            try:
                return await asyncio.to_thread(operation, api)
            except Exception as exc:  # noqa: BLE001
                logger.warning("通达信请求失败(%s)：%s", self._host, exc)
            # 服务端掐断连接是常态（WinError 10038 / 接收数据异常）。此时若直接
            # 返回 default，上层会把它当成「这只标的没有数据」，掉进慢速回退链；
            # 换一条新连接重试一次通常就能拿到数据。
            await asyncio.to_thread(self._drop_connection_blocking)
            try:
                api = await asyncio.to_thread(self._ensure_api_blocking)
                if api is None:
                    return default
                return await asyncio.to_thread(operation, api)
            except Exception as retry_exc:  # noqa: BLE001
                logger.warning("通达信重试仍失败(%s)：%s", self._host, retry_exc)
                await asyncio.to_thread(self._drop_connection_blocking)
                return default

    def _attach_names(self, quotes: dict[str, QuoteData]) -> None:
        """用注入的解析器一次性补齐名称（tdxpy 行情接口不含名称）。"""
        if self._name_resolver is None:
            return
        try:
            names = self._name_resolver(list(quotes)) or {}
        except Exception as exc:  # noqa: BLE001 - 名称缺失不影响行情可用性
            logger.warning("补齐通达信行情名称失败：%s", exc)
            return
        for symbol, quote in quotes.items():
            name = names.get(symbol)
            if name:
                quote.name = name

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol.strip())

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols:
            return {}
        grouped: dict[int, list[str]] = {}
        requested: dict[str, str] = {}
        for symbol in symbols:
            market = to_tdx_market(symbol)
            if market is None:
                continue
            raw = symbol.strip().lower()
            if raw.startswith(("sh", "sz", "bj")):
                raw = raw[2:]
            grouped.setdefault(market, []).append(raw)
            requested[raw] = symbol.strip()
        if not grouped:
            return {}

        pairs = [(market, code) for market, codes in grouped.items() for code in codes]
        result: dict[str, QuoteData] = {}
        for index in range(0, len(pairs), MAX_SYMBOLS_PER_CALL):
            chunk = pairs[index : index + MAX_SYMBOLS_PER_CALL]
            rows = await self._call(
                lambda api, chunk=chunk: api.get_security_quotes(chunk), []
            )
            for row in rows or []:
                code = str(row.get("code") or "").strip()
                symbol = requested.get(code, code)
                quote = parse_quote_row(row)
                if quote is None:
                    continue
                if quote.symbol != symbol:
                    quote = quote.model_copy(update={"symbol": symbol})
                result[symbol] = quote
        self._attach_names(result)
        return result

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        # 指数必须先分流：指数走 get_index_bars，个股走 get_security_bars，
        # 两个接口的请求格式不同，混用会取到字段错位的乱码数据。
        index_meta = resolve_index(symbol)
        if index_meta is not None:
            return await self._get_index_history(
                symbol, index_meta, period, start_time, end_time
            )
        market = to_tdx_market(symbol)
        if market is None:
            logger.info("通达信不支持 %s 的历史行情，交由其它数据源", symbol)
            return []
        kline_type = KLINE_TYPE_BY_PERIOD.get(period)
        if kline_type is None:
            logger.warning("通达信数据源不支持 period=%s", period)
            return []
        raw = symbol.strip().lower()
        if raw.startswith(("sh", "sz", "bj")):
            raw = raw[2:]

        pages: list[list[dict]] = []
        for page in range(MAX_KLINE_PAGES):
            offset = page * MAX_KLINE_COUNT
            chunk = await self._call(
                lambda api, offset=offset: api.get_security_bars(
                    kline_type, market, raw, offset, MAX_KLINE_COUNT
                ),
                [],
            )
            if not chunk:
                break
            rows = [dict(row) for row in chunk]
            pages.append(rows)
            oldest = _bar_time(rows[0])
            if oldest is not None and oldest <= start_time:
                break
            if len(rows) < MAX_KLINE_COUNT:
                break

        # 每页内部按时间升序，页之间是从新到旧，因此倒序拼接得到完整升序序列
        ordered = [row for page in reversed(pages) for row in page]
        return self._rows_to_quotes(symbol, ordered, start_time, end_time, kline_type)

    async def _get_index_history(
        self,
        symbol: str,
        meta: IndexMeta,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        """基准指数历史 K 线（走通达信 ``get_index_bars``）。"""
        kline_type = KLINE_TYPE_BY_PERIOD.get(period)
        if kline_type is None:
            logger.warning("通达信数据源不支持 period=%s", period)
            return []

        pages: list[list[dict]] = []
        for page in range(MAX_KLINE_PAGES):
            offset = page * MAX_KLINE_COUNT
            chunk = await self._call(
                lambda api, offset=offset: api.get_index_bars(
                    kline_type,
                    meta.tdx_market,
                    meta.tdx_code,
                    offset,
                    MAX_KLINE_COUNT,
                ),
                [],
            )
            if not chunk:
                break
            rows = [dict(row) for row in chunk]
            pages.append(rows)
            oldest = _bar_time(rows[0])
            if oldest is not None and oldest <= start_time:
                break
            if len(rows) < MAX_KLINE_COUNT:
                break

        ordered = [row for page in reversed(pages) for row in page]
        return self._rows_to_quotes(symbol, ordered, start_time, end_time, kline_type)

    @staticmethod
    def _rows_to_quotes(
        symbol: str,
        ordered: list[dict],
        start_time: datetime,
        end_time: datetime,
        kline_type: int,
    ) -> list[QuoteData]:
        """把通达信原始 K 线行按时间升序转成 QuoteData（区间外丢弃）。

        ``kline_type`` 决定成交量字段的量纲（日线是「手」，分钟线是「股」），
        由 ``_bar_volume`` 统一换算成「股」。
        """
        quotes: list[QuoteData] = []
        previous_close = 0.0
        for row in ordered:
            when = _bar_time(row)
            if when is None:
                continue
            close = _number(row.get("close"))
            if start_time <= when <= end_time:
                quotes.append(
                    QuoteData(
                        symbol=symbol,
                        name="",
                        price=close,
                        open=_number(row.get("open")),
                        high=_number(row.get("high")),
                        low=_number(row.get("low")),
                        previous_close=previous_close,
                        volume=_bar_volume(row.get("vol"), kline_type),
                        amount=_volume_number(row.get("amount")),
                        source="tdx",
                        market_time=when,
                        received_at=utc_now(),
                        is_stale=False,
                    )
                )
            previous_close = close or previous_close
        return quotes

    async def subscribe(
        self, symbols: list[str], callback: Callable[[QuoteData], None]
    ) -> None:
        """通达信为轮询模式，不支持主动推送。"""
        return None

    async def health_check(self) -> bool:
        try:
            quotes = await self.get_quotes([PROBE_SYMBOL])
            return PROBE_SYMBOL in quotes
        except Exception as exc:  # noqa: BLE001
            logger.warning("通达信健康检查失败：%s", exc)
            return False

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._drop_connection_blocking)
