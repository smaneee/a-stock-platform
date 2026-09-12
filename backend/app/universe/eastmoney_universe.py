"""东方财富全市场股票池 Provider。

数据源：``/api/qt/clist/get``（沪深京 A 股一次拉全，单页上限 100，按页并发抓取）。
相比 ``stock_info_a_code_name``（交易所官网口径、只有代码与名称），东财还能同时给出
``f100 所属行业``与 ``f26 上市日期``，因此可以填上 Security.sector。

约束：
- **不支持历史日期快照**。东财只提供「当前」名单，用它伪造历史成员会引入幸存者
  偏差，因此显式拒绝带 ``as_of_date`` 的历史请求，由调用方回退到 BaoStock。
- 需要过滤北交所可转债（``810xxx`` / ``82xxxx`` 会被同一 fs 条件带出来）。
- 拉完必须过 ``validate_market_coverage`` 全市场质量门槛（缩量 / 缺交易所 → 失败）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Sequence

import httpx

from app.market_data.eastmoney_provider import (
    EASTMONEY_HEADERS,
    EASTMONEY_UT,
    QUOTE_HOSTS,
    EastmoneyHostPool,
    eastmoney_request_json,
)
from app.time_utils import utc_now
from app.universe.providers import (
    ProviderError,
    SecurityRecord,
    SYMBOL_PATTERN,
    UniverseProvider,
    _infer_board,
    _is_delisted_name,
    _is_st_name,
    _parse_date,
    validate_market_coverage,
)

logger = logging.getLogger(__name__)

CLIST_PATH = "/api/qt/clist/get"
# 沪深京 A 股（不含 B 股 / 基金 / 债券，但会带出北交所可转债，需再过滤）
ALL_A_SHARES_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_FIELDS = "f12,f13,f14,f26,f100"

# 单页上限实测 100（传更大也只返回 100 行）
_MAX_PAGE_SIZE = 100

_SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301", "302")
# 北交所：920 新号段 + 43/83/87/88 老号段；81/82 是北交所债券，不能算股票
_BJ_PREFIXES = ("92", "43", "83", "87", "88")


def classify_exchange(code: str) -> str:
    """按代码前缀判定交易所；不是 A 股股票代码时返回空串。"""
    if code.startswith(_SH_PREFIXES):
        return "SH"
    if code.startswith(_SZ_PREFIXES):
        return "SZ"
    if code.startswith(_BJ_PREFIXES):
        return "BJ"
    return ""


def is_bond_row(code: str, name: str) -> bool:
    """东财 fs 条件会带出北交所可转债（810xxx，名称含「转债」）。"""
    if code.startswith(("81", "82")):
        return True
    return "债" in name or name.endswith("转")


class EastmoneyUniverseProvider(UniverseProvider):
    """东方财富全市场证券主数据 Provider。"""

    source_id = "eastmoney"

    def __init__(
        self,
        *,
        timeout_seconds: float = 90.0,
        page_size: int = _MAX_PAGE_SIZE,
        max_concurrency: int = 4,
        request_timeout: float = 10.0,
        hosts: Sequence[str] = QUOTE_HOSTS,
        as_of_date: date | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._timeout_seconds = timeout_seconds
        self._page_size = max(1, min(page_size, _MAX_PAGE_SIZE))
        self._max_concurrency = max(1, max_concurrency)
        self._request_timeout = request_timeout
        self._hosts = tuple(hosts)
        self._as_of_date = as_of_date
        # 仅用于测试注入 MockTransport；生产保持 None（走真实网络）
        self._transport = transport

    async def fetch_all(self) -> list[SecurityRecord]:
        today = utc_now().date()
        if self._as_of_date is not None and self._as_of_date != today:
            raise ProviderError(
                self.source_id,
                f"东方财富只提供当前名单，不支持历史快照（请求 {self._as_of_date}）；"
                "历史日期请使用 baostock",
            )
        try:
            records = await asyncio.wait_for(
                self._fetch(), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            raise ProviderError(
                self.source_id,
                f"东方财富股票池超时（>{self._timeout_seconds}s）",
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一包成 ProviderError
            raise ProviderError(self.source_id, f"东方财富股票池失败: {exc}")

        validate_market_coverage(records, source_id=self.source_id, as_of_date=today)
        return records

    # ──────── 内部实现 ────────

    async def _fetch(self) -> list[SecurityRecord]:
        as_of = utc_now().date()
        async with httpx.AsyncClient(
            timeout=self._request_timeout,
            headers=EASTMONEY_HEADERS,
            transport=self._transport,
        ) as client:
            pool = EastmoneyHostPool(self._hosts)
            rows, total = await self._page(client, pool, 1)
            pages = max(1, (total + self._page_size - 1) // self._page_size)
            if pages > 1:
                semaphore = asyncio.Semaphore(self._max_concurrency)

                async def grab(page_number: int) -> list[dict]:
                    async with semaphore:
                        page_rows, _ = await self._page(client, pool, page_number)
                        return page_rows

                for chunk in await asyncio.gather(
                    *[grab(number) for number in range(2, pages + 1)]
                ):
                    rows.extend(chunk)
        logger.info("东方财富股票池拉取 %d 行（%d 页）", len(rows), pages)
        records = self._to_records(rows, as_of)
        if not records:
            raise ProviderError(self.source_id, "东方财富股票池解析后 0 条有效记录")
        return records

    async def _page(
        self, client: httpx.AsyncClient, pool: EastmoneyHostPool, page_number: int
    ) -> tuple[list[dict], int]:
        params = {
            "pn": str(page_number),
            "pz": str(self._page_size),
            "po": "0",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": ALL_A_SHARES_FS,
            "fields": _FIELDS,
            "ut": EASTMONEY_UT,
        }
        payload = await eastmoney_request_json(
            client,
            pool,
            CLIST_PATH,
            params,
            max_retries=2,
            accept=lambda body: isinstance(body.get("data"), dict),
        )
        data = payload.get("data") or {}
        rows = data.get("diff") or []
        if isinstance(rows, dict):  # 部分节点按序号返回字典
            rows = list(rows.values())
        return [row for row in rows if isinstance(row, dict)], int(data.get("total") or 0)

    def _to_records(
        self, rows: list[dict], as_of_date: date | None
    ) -> list[SecurityRecord]:
        records: list[SecurityRecord] = []
        seen: set[str] = set()
        for row in rows:
            code = str(row.get("f12") or "").strip()
            if not SYMBOL_PATTERN.match(code) or code in seen:
                continue
            name = str(row.get("f14") or "").strip()
            if is_bond_row(code, name):
                continue
            exchange = classify_exchange(code)
            if not exchange:
                continue
            seen.add(code)
            sector = str(row.get("f100") or "").strip()
            if sector in ("-", "nan"):
                sector = ""
            records.append(
                SecurityRecord(
                    symbol=code,
                    name=name,
                    exchange=exchange,
                    board=_infer_board(code, exchange),
                    listing_date=_parse_date(row.get("f26")),
                    trading_status="delisted" if _is_delisted_name(name) else "active",
                    is_st=_is_st_name(name),
                    sector=sector or None,
                    as_of_date=as_of_date,
                )
            )
        return records
