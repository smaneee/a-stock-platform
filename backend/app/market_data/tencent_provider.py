"""腾讯行情数据源。

使用免费轮询接口 http://qt.gtimg.cn 获取 A 股实时行情。
所有网络请求均设置超时、异常捕获、连接池复用、有限重试与指数退避。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

import httpx

from app.market_data.base import MarketDataProvider, QuoteData
from app.time_utils import utc_now

logger = logging.getLogger(__name__)

_QUOTE_URL = "http://qt.gtimg.cn/q="


def to_tencent_symbol(symbol: str) -> str:
    """将 A 股代码转换为腾讯带市场前缀的代码。

    - 6xxxxx -> sh6xxxxx
    - 0xxxxx / 3xxxxx -> sz0xxxxx
    - 8xxxxx / 4xxxxx / 9xxxxx -> bj（北交所）
    """
    symbol = symbol.strip()
    if symbol.startswith(("sh", "sz", "bj")):
        return symbol
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    if symbol.startswith(("0", "3", "1", "2")):
        return f"sz{symbol}"
    return f"bj{symbol}"


def _parse_quote(text: str) -> QuoteData | None:
    """解析腾讯行情返回的原始文本。"""
    try:
        # 格式形如 v_sh600000="1~浦发银行~600000~10.50~...";
        if "=" not in text:
            return None
        _, _, payload = text.partition("=")
        payload = payload.strip().strip('";')
        fields = payload.split("~")
        if len(fields) < 35:
            return None

        symbol = fields[2]
        name = fields[1]
        price = float(fields[3])
        previous_close = float(fields[4])
        open_price = float(fields[5])
        volume_lot = float(fields[6]) if fields[6] else 0.0  # 手
        bid_price = float(fields[9]) if fields[9] else 0.0
        ask_price = float(fields[19]) if fields[19] else 0.0
        high = float(fields[33]) if fields[33] else 0.0
        low = float(fields[34]) if fields[34] else 0.0
        amount_wan = float(fields[37]) if len(fields) > 37 and fields[37] else 0.0  # 万元

        market_time = None
        if len(fields) > 30 and fields[30]:
            try:
                market_time = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
            except ValueError:
                market_time = None

        return QuoteData(
            symbol=symbol,
            name=name,
            price=price,
            open=open_price,
            high=high,
            low=low,
            previous_close=previous_close,
            volume=volume_lot * 100,  # 手转股
            amount=amount_wan * 10_000,  # 万元转元
            bid_price=bid_price,
            ask_price=ask_price,
            source="tencent",
            market_time=market_time,
            received_at=utc_now(),
            is_stale=False,
        )
    except (ValueError, IndexError, TypeError):
        return None


def _validate_quote(quote: QuoteData) -> bool:
    """Provider 级数据合理性校验，防止单数据源被中间人注入。

    与上层 QuoteScheduler._validate 形成两层防护：此处独立校验腾讯原始解析结果，
    上层校验保证跨数据源一致性。
    """
    if not quote.symbol or quote.price <= 0:
        return False
    if quote.high > 0 and quote.low > 0 and quote.high < quote.low:
        return False
    # 单日涨跌幅不应超过 ±20%（A 股 ±10% 正常，±20% 含临时扩幅）
    if quote.previous_close > 0:
        if not (
            0.8 * quote.previous_close
            <= quote.price
            <= 1.2 * quote.previous_close
        ):
            return False
    # 名称不应包含 HTML 注入字符
    name = quote.name or ""
    if any(ch in name for ch in ("<", ">", "&", '"', "'")):
        return False
    return True


class TencentProvider(MarketDataProvider):
    """腾讯免费行情数据源。"""

    name = "tencent"

    def __init__(self, timeout: float = 5.0, max_retries: int = 3):
        self._timeout = timeout
        self._max_retries = max_retries
        # 复用连接池
        self._client = httpx.AsyncClient(timeout=timeout)

    async def get_quote(self, symbol: str) -> QuoteData | None:
        result = await self.get_quotes([symbol])
        return result.get(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteData]:
        if not symbols:
            return {}
        codes = ",".join(to_tencent_symbol(s) for s in symbols)
        url = f"{_QUOTE_URL}{codes}"

        try:
            text = await self._request_with_retry(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("腾讯行情请求失败: %s", exc)
            return {}

        # 返回内容可能包含多行，每行一个股票
        result: dict[str, QuoteData] = {}
        for line in text.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            quote = _parse_quote(line)
            if quote is not None and _validate_quote(quote):
                result[quote.symbol] = quote
        return result

    async def get_history(
        self,
        symbol: str,
        period: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[QuoteData]:
        # 腾讯免费接口不直接提供历史 K 线，交由 AKShare 处理
        return []

    async def subscribe(self, symbols: list[str], callback: Callable[[QuoteData], None]) -> None:
        # 腾讯为轮询模式，不支持主动推送
        return None

    async def health_check(self) -> bool:
        try:
            result = await self.get_quotes(["600000"])
            return bool(result)
        except Exception:  # noqa: BLE001
            return False

    async def _request_with_retry(self, url: str) -> str:
        """带有限重试与指数退避的请求。"""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                # 腾讯接口可能返回 GBK 编码
                resp.encoding = "gbk"
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self._max_retries - 1:
                    wait = 2 ** attempt  # 指数退避：1s, 2s, 4s
                    await asyncio.sleep(wait)
        raise last_exc or RuntimeError("request failed")

    async def close(self) -> None:
        await self._client.aclose()
