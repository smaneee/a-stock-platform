"""东方财富（Eastmoney）行情数据源。

覆盖三类数据：

1. 实时行情：``/api/qt/ulist.np/get``（批量，``fltt=2`` 直接返回浮点价）
2. 日/周/月/分钟 K 线：``/api/qt/stock/kline/get``（klt=101/102/103/1/5/15/30/60）
3. 分时：``/api/qt/stock/trends2/get``（1 分钟，近 N 日）

**主机故障转移**：东财按「主机 + IP」维度限流，被限流的域名会直接断开连接
（``RemoteProtocolError``），而同一时刻其它域名（如 ``push2delay``）仍然可用。
因此所有请求都经 :class:`EastmoneyHostPool`：按主机顺序尝试，失败的主机立即
切换并进入冷却，避免单台主机被拉黑后整条行情链路不可用。

``upstream = "eastmoney"`` 标记本数据源与 AKShare 的东财通道同源，管理器在同一次
请求内不会对同一上游重复请求（历史入库时避免每个 symbol 多打一次被限流的东财）。

字段口径（与东财行情页一致，``fltt=2`` 时价格与涨跌幅已是浮点）：

- 实时：f12 代码、f14 名称、f2 最新价、f15 最高、f16 最低、f17 今开、f18 昨收、
  f5 成交量（手）、f6 成交额（元）、f124 行情时间戳
- K 线：日期,开盘,收盘,最高,最低,成交量(手),成交额(元),振幅,涨跌幅,涨跌额,换手率
- 分时：时间,开盘,收盘,最高,最低,成交量(手),成交额(元),均价

批量行情接口不含五档盘口，因此 ``bid_price`` / ``ask_price`` 恒为 0；需要盘口的
模拟盘 / 实盘调仓会在盘口缺失时按最新价兜底。

成交量统一按 1 手 = 100 股换算成股，与腾讯数据源口径保持一致。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Iterable, Sequence

import httpx

from app.market_data.base import MarketDataProvider, QuoteData
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

# 行情/板块/资金流共用的公开 token（东财行情页固定使用，缺失会被边缘节点直接断开）
EASTMONEY_UT = "7eea3edcaed734bea9cbfc24409ed989"

EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

# 主机池：首选官方域名，备选延迟域名（同一套接口与字段，被限流时的兜底通道）
QUOTE_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")
HISTORY_HOSTS = ("push2his.eastmoney.com", "push2delay.eastmoney.com")

QUOTE_PATH = "/api/qt/ulist.np/get"
KLINE_PATH = "/api/qt/stock/kline/get"
TRENDS_PATH = "/api/qt/stock/trends2/get"

# 注意：批量列表接口（ulist.np/get）没有五档盘口字段（f19/f20/f38/f39 在这套
# 字段命名里是序号/总市值/流通市值，不是买卖一价），因此不请求盘口，
# bid_price / ask_price 统一返回 0，由上层按最新价兜底（见 paper/live rebalance）。
QUOTE_FIELDS = "f12,f14,f2,f5,f6,f15,f16,f17,f18,f86,f124"

# 日/周/月用 klt，分钟线用东财的分钟周期编号
KLT_BY_PERIOD = {
    "daily": "101",
    "weekly": "102",
    "monthly": "103",
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
}
MINUTE_PERIODS = {"1m", "5m", "15m", "30m", "60m"}
# 1 分钟线只有分时接口，一次最多给近几天
TREND_DAYS = 5
LOT_SIZE = 100

# 东财限流后会持续断连一段时间。连续失败到阈值就熔断，让 ProviderManager 立刻
# 回退到腾讯，而不是每 3 秒的轮询都去撞一次被限流的接口。
_FAILURE_THRESHOLD = 3
_DISABLE_SECONDS = 300.0


class EastmoneyHostPool:
    """东财行情主机的健康感知轮换池。

    东财按「主机 + IP」维度限流：某台边缘节点被限流后只是该域名不可达，
    其它域名仍然可用。这里记录每台主机的冷却时间，失败的主机先跳过，
    成功后把它提到队首，避免每次请求都先去撞不可达的主机。
    """

    def __init__(
        self, hosts: Sequence[str], cooldown_seconds: float = 120.0
    ) -> None:
        if not hosts:
            raise ValueError("EastmoneyHostPool 至少需要一台主机")
        self._hosts = list(hosts)
        self._cooldown = cooldown_seconds
        self._down_until: dict[str, datetime] = {}
        self._preferred = 0

    @property
    def hosts(self) -> list[str]:
        return list(self._hosts)

    def ordered(self) -> list[str]:
        """按「上次成功的主机优先」返回主机顺序，冷却中的主机排到最后。"""
        rotated = self._hosts[self._preferred :] + self._hosts[: self._preferred]
        now = datetime.now()
        alive = [
            host
            for host in rotated
            if self._down_until.get(host) is None or self._down_until[host] <= now
        ]
        # 全部处于冷却期时仍按原顺序尝试，否则会永久放弃整条链路
        return alive or rotated

    def mark_down(self, host: str) -> None:
        already_down = self._down_until.get(host) is not None
        self._down_until[host] = datetime.now() + timedelta(seconds=self._cooldown)
        if self._hosts[self._preferred] == host:
            for offset in range(1, len(self._hosts) + 1):
                candidate_index = (self._preferred + offset) % len(self._hosts)
                if self._hosts[candidate_index] != host:
                    self._preferred = candidate_index
                    break
        if not already_down:
            logger.warning(
                "东方财富主机 %s 暂不可用，冷却 %.0f 秒后重试（已切换到备用主机）",
                host,
                self._cooldown,
            )

    def mark_up(self, host: str) -> None:
        self._down_until.pop(host, None)
        if host in self._hosts:
            self._preferred = self._hosts.index(host)


def to_eastmoney_secid(symbol: str) -> str:
    """A 股代码转东财 secid（市场.代码）。

    沪市（6/5/900 开头，含科创板与沪市基金）为 1，深市与北交所为 0。
    北交所新号段 920xxx 属于市场 0，不能按“9 开头是沪 B”处理。
    """
    raw = symbol.strip().lower()
    market: int | None = None
    for prefix, code in (("sh", 1), ("sz", 0), ("bj", 0)):
        if raw.startswith(prefix):
            raw, market = raw[len(prefix) :], code
            break
    if market is None:
        market = 1 if raw.startswith(("6", "5", "900")) else 0
    return f"{market}.{raw}"


def em_to_float(raw: object) -> float:
    """东财用 - 表示停牌/无数据，这里统一转 0。"""
    if raw is None or raw == "" or raw == "-":
        return 0.0
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_market_time(row: dict) -> datetime | None:
    """解析行情更新时间（f124 / f86 为 unix 秒）。"""
    now = datetime.now()
    for key in ("f124", "f86"):
        ts = em_to_float(row.get(key))
        if ts <= 0:
            continue
        try:
            when = datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            continue
        # 只接受合理区间，避免把序号之类的字段误当时间
        if now - timedelta(days=30) <= when <= now + timedelta(days=1):
            return when
    return None


def validate_quote(quote: QuoteData) -> bool:
    """Provider 级数据合理性校验，规则与腾讯数据源一致。

    与上层 QuoteScheduler / ProviderManager 形成多层防护，防止单源被注入脏数据。
    """
    if not quote.symbol or quote.price <= 0:
        return False
    if quote.high > 0 and quote.low > 0 and quote.high < quote.low:
        return False
    if quote.previous_close > 0:
        if not (
            0.8 * quote.previous_close
            <= quote.price
            <= 1.2 * quote.previous_close
        ):
            return False
    name = quote.name or ""
    if any(ch in name for ch in ("<", ">", "&", '"', "'")):
        return False
    return True


def parse_quote_rows(payload: dict) -> dict[str, QuoteData]:
    """解析 ``ulist.np/get`` 返回的批量行情。"""
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    if isinstance(rows, dict):  # 部分节点按序号返回字典
        rows = list(rows.values())

    result: dict[str, QuoteData] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("f12") or "").strip()
        if not symbol:
            continue
        quote = QuoteData(
            symbol=symbol,
            name=str(row.get("f14") or "").strip(),
            price=em_to_float(row.get("f2")),
            open=em_to_float(row.get("f17")),
            high=em_to_float(row.get("f15")),
            low=em_to_float(row.get("f16")),
            previous_close=em_to_float(row.get("f18")),
            volume=em_to_float(row.get("f5")) * LOT_SIZE,
            amount=em_to_float(row.get("f6")),
            # 批量接口不含五档；上层（模拟/实盘调仓）在盘口缺失时按最新价兜底
            bid_price=0.0,
            ask_price=0.0,
            source="eastmoney",
            market_time=_parse_market_time(row),
            received_at=utc_now(),
            is_stale=False,
        )
        if validate_quote(quote):
            result[symbol] = quote
    return result


def parse_history_rows(
    payload: dict, symbol: str, period: str = "daily"
) -> list[QuoteData]:
    """解析 K 线（klines）或分时（trends）返回。"""
    data = payload.get("data") or {}
    rows = data.get("klines")
    if rows is None:
        rows = data.get("trends") or []
    source_name = str(data.get("name") or "").strip() or symbol

    bars: list[QuoteData] = []
    previous_close = 0.0
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 7:
            continue
        when = _parse_bar_time(parts[0])
        if when is None:
            continue
        open_price = em_to_float(parts[1])
        close_price = em_to_float(parts[2])
        high = em_to_float(parts[3])
        low = em_to_float(parts[4])
        if close_price <= 0 and open_price <= 0:
            continue
        bars.append(
            QuoteData(
                symbol=symbol,
                name=source_name,
                price=close_price,
                open=open_price,
                high=high,
                low=low,
                previous_close=previous_close,
                volume=em_to_float(parts[5]) * LOT_SIZE,
                amount=em_to_float(parts[6]),
                bid_price=0.0,
                ask_price=0.0,
                source="eastmoney",
                market_time=when,
                received_at=utc_now(),
                is_stale=False,
            )
        )
        previous_close = close_price
    return bars


def kline_available(payload: dict) -> bool:
    """该节点是否真的提供历史 K 线。

    延迟节点（push2delay）没有历史库：rc=0 但 dktotal=0、klines 为空。
    把这种情况判定为「这台主机给不了这份数据」，继续换下一台主机。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    if data.get("klines"):
        return True
    return bool(data.get("dktotal"))


def _parse_bar_time(raw: str) -> datetime | None:
    """解析 K 线/分时的时间列（2026-08-03 或 2026-08-03 09:30）。"""
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def eastmoney_request_json(
    client: httpx.AsyncClient,
    pool: EastmoneyHostPool,
    path: str,
    params: dict,
    *,
    max_retries: int = 1,
    accept: Callable[[dict], bool] | None = None,
) -> dict:
    """按主机池顺序请求东财 JSON 接口；单台主机不可达时立即切换备用主机。

    Args:
        client: 复用的 httpx 客户端（带东财请求头）
        pool: 主机池，决定尝试顺序并记录主机冷却
        path: 接口路径（如 ``/api/qt/ulist.np/get``）
        params: 查询参数
        max_retries: **单台主机**的尝试次数，跨主机故障转移不计入重试次数
        accept: 判定「该主机是否真的提供了这份数据」；不满足时同样切换主机
            （例如延迟节点没有历史 K 线库，返回 rc=0 但 klines 为空）

    Raises:
        Exception: 所有主机都失败时抛出最后一次异常
    """
    last_exc: Exception | None = None
    for host in pool.ordered():
        for attempt in range(max_retries):
            try:
                resp = await client.get(f"https://{host}{path}", params=params)
                resp.raise_for_status()
                payload = resp.json()
            except httpx.TransportError as exc:
                # 连接被重置/超时 → 这台主机被限流，立即换下一台
                last_exc = exc
                pool.mark_down(host)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 < max_retries:
                    await asyncio.sleep(0.3 * (attempt + 1))
                continue
            if accept is not None and not accept(payload):
                last_exc = RuntimeError(f"{host} 未返回所需数据")
                pool.mark_down(host)
                break
            pool.mark_up(host)
            return payload
    raise last_exc or RuntimeError("eastmoney request failed")


class EastmoneyProvider(MarketDataProvider):
    """东方财富行情数据源（实时 + 历史）。"""

    name = "eastmoney"
    upstream = "eastmoney"

    def __init__(
        self,
        timeout: float = 5.0,
        max_retries: int = 1,
        batch_size: int = 50,
        quote_hosts: Sequence[str] = QUOTE_HOSTS,
        history_hosts: Sequence[str] = HISTORY_HOSTS,
    ):
        self._timeout = timeout
        # 东财限流严格：单台主机重试越多越容易被持续拒绝，保持低次数
        self._max_retries = max_retries
        self._batch_size = batch_size
        self._client = httpx.AsyncClient(timeout=timeout, headers=EASTMONEY_HEADERS)
        self._quote_pool = EastmoneyHostPool(quote_hosts)
        self._history_pool = EastmoneyHostPool(history_hosts)
        self._consecutive_failures = 0
        self._disabled_until: datetime | None = None

    # ──────── 熔断 ────────

    def _is_disabled(self) -> bool:
        """是否处于熔断期（到期自动恢复）。"""
        if self._disabled_until is None:
            return False
        if datetime.now() >= self._disabled_until:
            self._disabled_until = None
            self._consecutive_failures = 0
            logger.info("东方财富数据源熔断到期，恢复请求")
            return False
        return True

    def _record_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= _FAILURE_THRESHOLD
            and self._disabled_until is None
        ):
            self._disabled_until = datetime.now() + timedelta(
                seconds=_DISABLE_SECONDS
            )
            logger.warning(
                "东方财富连续 %d 次失败（%s），暂停使用 %.0f 秒后自动重试",
                self._consecutive_failures,
                reason,
                _DISABLE_SECONDS,
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._disabled_until = None

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols:
            return {}
        if self._is_disabled():
            logger.info("东方财富数据源处于熔断期，本次行情请求直接跳过")
            return {}
        result: dict[str, QuoteData] = {}
        for chunk in _chunked(symbols, self._batch_size):
            params = {
                "fltt": "2",
                "invt": "2",
                "secids": ",".join(to_eastmoney_secid(s) for s in chunk),
                "fields": QUOTE_FIELDS,
                "ut": EASTMONEY_UT,
            }
            try:
                payload = await self._request_json(
                    self._quote_pool, QUOTE_PATH, params
                )
            except Exception as exc:  # noqa: BLE001 - 数据源异常交给上层回退
                logger.warning("东方财富批量行情失败: %s", exc)
                self._record_failure(str(exc))
                return {}
            result.update(parse_quote_rows(payload))
        if result:
            self._record_success()
        return result

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        if self._is_disabled():
            logger.info("东方财富数据源处于熔断期，本次历史请求直接跳过")
            return []
        secid = to_eastmoney_secid(symbol)
        accept: Callable[[dict], bool] | None = None
        if period == "1m":
            path = TRENDS_PATH
            params = {
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ut": EASTMONEY_UT,
                "ndays": str(TREND_DAYS),
                "iscr": "0",
                "secid": secid,
            }
        else:
            klt = KLT_BY_PERIOD.get(period)
            if klt is None:
                logger.warning("东方财富数据源不支持 period=%s", period)
                return []
            intraday = period in MINUTE_PERIODS
            path = KLINE_PATH
            params = {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "ut": EASTMONEY_UT,
                "klt": klt,
                "fqt": "0",
                "secid": secid,
                # 分钟线不支持区间参数，取全量后本地过滤
                "beg": "0" if intraday else start_time.strftime("%Y%m%d"),
                "end": "20500000" if intraday else end_time.strftime("%Y%m%d"),
            }
            accept = kline_available

        try:
            payload = await self._request_json(
                self._history_pool, path, params, accept=accept
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("东方财富历史行情 %s(%s) 失败: %s", symbol, period, exc)
            self._record_failure(str(exc))
            return []

        self._record_success()
        bars = parse_history_rows(payload, symbol, period)
        return [
            bar
            for bar in bars
            if bar.market_time is not None and start_time <= bar.market_time <= end_time
        ]

    async def subscribe(
        self, symbols: list[str], callback: Callable[[QuoteData], None]
    ) -> None:
        # 东财为轮询模式，不支持主动推送
        return None

    async def health_check(self) -> bool:
        try:
            return bool(await self.get_quotes(["600000"]))
        except Exception:  # noqa: BLE001
            return False

    async def _request_json(
        self,
        pool: EastmoneyHostPool,
        path: str,
        params: dict,
        accept: Callable[[dict], bool] | None = None,
    ) -> dict:
        """使用本 Provider 的客户端与重试预算请求（见 eastmoney_request_json）。"""
        return await eastmoney_request_json(
            self._client,
            pool,
            path,
            params,
            max_retries=self._max_retries,
            accept=accept,
        )

    async def close(self) -> None:
        await self._client.aclose()
