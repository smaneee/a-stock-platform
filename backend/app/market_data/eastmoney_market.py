"""东方财富市场级数据：板块（行业/概念/地域）行情、板块成分股与资金流。

与 :mod:`app.market_data.eastmoney_provider` 的区别：Provider 面向「单只股票」的
实时/历史行情，供轮询与回测使用；本模块面向「全市场横截面」数据，供看板与选股
研究使用，因此不进 ProviderManager，而是通过 ``/api/market/*`` 只读接口暴露。

覆盖的东财接口（全部走同一套主机故障转移）：

- 板块行情：``/api/qt/clist/get``，``fs=m:90+t:1|t:2|t:3``（地域/行业/概念）
- 板块成分股：``fs=b:BKxxxx``
- 资金流排行：``clist`` 按 ``fid=f62``（主力净流入）排序，板块与个股同源
- 个股资金流历史：``/api/qt/stock/fflow/daykline/get``

字段口径（``fltt=2`` 时为浮点原值）：

- 板块：f2 指数、f3 涨跌幅、f4 涨跌额、f5 成交量(手)、f6 成交额(元)、f8 换手率、
  f62 主力净流入(元)、f104 上涨家数、f105 下跌家数、f106 平盘家数、
  f128/f140/f136 领涨股名称/代码/涨跌幅
- 资金流：f62 主力、f66 超大单、f72 大单、f78 中单、f84 小单净流入（元），
  对应的 f184/f69/f75/f81/f87 为净占比(%)；主力 = 超大单 + 大单
- 资金流历史：``日期,主力,小单,中单,大单,超大单,主力占比,小单占比,中单占比,
  大单占比,超大单占比,收盘价,涨跌幅,...``

成交量统一按 1 手 = 100 股换算成股，与行情数据源口径一致；资金流单位为元。
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

import httpx
from pydantic import BaseModel

from app.market_data.eastmoney_provider import (
    EASTMONEY_HEADERS,
    EASTMONEY_UT,
    HISTORY_HOSTS,
    QUOTE_HOSTS,
    EastmoneyHostPool,
    eastmoney_request_json,
    em_to_float,
    to_eastmoney_secid,
)

CLIST_PATH = "/api/qt/clist/get"
FFLOW_DAYKLINE_PATH = "/api/qt/stock/fflow/daykline/get"

# 板块类型 → 东财 clist 的 fs 过滤条件
BOARD_KINDS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
    "region": "m:90+t:1",
}
# 沪深京 A 股（不含 B 股/基金/债券）
ALL_A_SHARES_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# 东财 clist 单页上限实测为 100，超过也只会返回 100 条
MAX_PAGE_SIZE = 100

_BOARD_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f12,f14,f62,f104,f105,f106,f128,f136,f140,f184"
)
_MEMBER_FIELDS = "f2,f3,f5,f6,f8,f12,f14,f62,f184"
_FLOW_FIELDS = "f2,f3,f12,f14,f62,f66,f69,f72,f75,f78,f81,f84,f87,f184"


class BoardQuote(BaseModel):
    """板块行情（行业/概念/地域）。"""

    code: str
    name: str
    kind: str
    index_value: float
    change_pct: float
    change_amount: float
    volume: float  # 股
    amount: float  # 元
    amplitude: float
    turnover_rate: float
    main_net_inflow: float  # 元
    main_net_inflow_pct: float
    up_count: int
    down_count: int
    flat_count: int
    leader_symbol: str | None = None
    leader_name: str | None = None
    leader_change_pct: float | None = None


class BoardMember(BaseModel):
    """板块成分股（含当日资金流）。"""

    symbol: str
    name: str
    price: float
    change_pct: float
    volume: float
    amount: float
    turnover_rate: float
    main_net_inflow: float
    main_net_inflow_pct: float


class FundFlowRow(BaseModel):
    """资金流排行行（板块或个股共用，用 kind 区分）。"""

    code: str
    name: str
    kind: str = "stock"
    price: float
    change_pct: float
    main_net_inflow: float
    main_net_inflow_pct: float
    super_large_net_inflow: float
    super_large_net_inflow_pct: float
    large_net_inflow: float
    large_net_inflow_pct: float
    medium_net_inflow: float
    medium_net_inflow_pct: float
    small_net_inflow: float
    small_net_inflow_pct: float


class FundFlowPoint(BaseModel):
    """个股资金流历史中的一个交易日。"""

    trade_date: str
    main_net_inflow: float
    small_net_inflow: float
    medium_net_inflow: float
    large_net_inflow: float
    super_large_net_inflow: float
    main_net_inflow_pct: float
    close_price: float
    change_pct: float


def _rows(payload: dict) -> list[dict]:
    """取出 clist 返回体中的行（部分节点用序号字典代替数组）。"""
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [row for row in rows if isinstance(row, dict)]


def _text(raw: object) -> str:
    value = str(raw or "").strip()
    return "" if value in ("-", "nan") else value


def _int(raw: object) -> int:
    return int(em_to_float(raw))


def parse_board_rows(payload: dict, kind: str) -> list[BoardQuote]:
    """解析板块行情 clist 返回。"""
    boards: list[BoardQuote] = []
    for row in _rows(payload):
        code = _text(row.get("f12"))
        if not code:
            continue
        boards.append(
            BoardQuote(
                code=code,
                name=_text(row.get("f14")),
                kind=kind,
                index_value=em_to_float(row.get("f2")),
                change_pct=em_to_float(row.get("f3")),
                change_amount=em_to_float(row.get("f4")),
                volume=em_to_float(row.get("f5")) * 100,
                amount=em_to_float(row.get("f6")),
                amplitude=em_to_float(row.get("f7")),
                turnover_rate=em_to_float(row.get("f8")),
                main_net_inflow=em_to_float(row.get("f62")),
                main_net_inflow_pct=em_to_float(row.get("f184")),
                up_count=_int(row.get("f104")),
                down_count=_int(row.get("f105")),
                flat_count=_int(row.get("f106")),
                leader_symbol=_text(row.get("f140")) or None,
                leader_name=_text(row.get("f128")) or None,
                leader_change_pct=(
                    em_to_float(row.get("f136"))
                    if _text(row.get("f140"))
                    else None
                ),
            )
        )
    return boards


def parse_board_member_rows(payload: dict) -> list[BoardMember]:
    """解析板块成分股 clist 返回。"""
    members: list[BoardMember] = []
    for row in _rows(payload):
        symbol = _text(row.get("f12"))
        if not symbol:
            continue
        members.append(
            BoardMember(
                symbol=symbol,
                name=_text(row.get("f14")),
                price=em_to_float(row.get("f2")),
                change_pct=em_to_float(row.get("f3")),
                volume=em_to_float(row.get("f5")) * 100,
                amount=em_to_float(row.get("f6")),
                turnover_rate=em_to_float(row.get("f8")),
                main_net_inflow=em_to_float(row.get("f62")),
                main_net_inflow_pct=em_to_float(row.get("f184")),
            )
        )
    return members


def parse_fund_flow_rows(payload: dict, kind: str = "stock") -> list[FundFlowRow]:
    """解析资金流排行 clist 返回（板块与个股字段一致）。"""
    flows: list[FundFlowRow] = []
    for row in _rows(payload):
        code = _text(row.get("f12"))
        if not code:
            continue
        flows.append(
            FundFlowRow(
                code=code,
                name=_text(row.get("f14")),
                kind=kind,
                price=em_to_float(row.get("f2")),
                change_pct=em_to_float(row.get("f3")),
                main_net_inflow=em_to_float(row.get("f62")),
                main_net_inflow_pct=em_to_float(row.get("f184")),
                super_large_net_inflow=em_to_float(row.get("f66")),
                super_large_net_inflow_pct=em_to_float(row.get("f69")),
                large_net_inflow=em_to_float(row.get("f72")),
                large_net_inflow_pct=em_to_float(row.get("f75")),
                medium_net_inflow=em_to_float(row.get("f78")),
                medium_net_inflow_pct=em_to_float(row.get("f81")),
                small_net_inflow=em_to_float(row.get("f84")),
                small_net_inflow_pct=em_to_float(row.get("f87")),
            )
        )
    return flows


def parse_fund_flow_history(payload: dict) -> list[FundFlowPoint]:
    """解析个股资金流历史（fflow/daykline）。

    列顺序：日期,主力,小单,中单,大单,超大单,主力占比,小单占比,中单占比,
    大单占比,超大单占比,收盘价,涨跌幅,...
    """
    data = payload.get("data") or {}
    points: list[FundFlowPoint] = []
    for row in data.get("klines") or []:
        parts = str(row).split(",")
        if len(parts) < 13:
            continue
        trade_date = parts[0].strip()
        try:
            datetime.strptime(trade_date, "%Y-%m-%d")
        except ValueError:
            continue
        points.append(
            FundFlowPoint(
                trade_date=trade_date,
                main_net_inflow=em_to_float(parts[1]),
                small_net_inflow=em_to_float(parts[2]),
                medium_net_inflow=em_to_float(parts[3]),
                large_net_inflow=em_to_float(parts[4]),
                super_large_net_inflow=em_to_float(parts[5]),
                main_net_inflow_pct=em_to_float(parts[6]),
                close_price=em_to_float(parts[11]),
                change_pct=em_to_float(parts[12]),
            )
        )
    return points


def _has_clist_rows(payload: dict) -> bool:
    """clist 的 data 为空说明该主机没有这份数据（或参数不被接受）。"""
    return isinstance(payload.get("data"), dict)


def _has_fflow_rows(payload: dict) -> bool:
    data = payload.get("data")
    return isinstance(data, dict) and bool(data.get("klines"))


class EastmoneyMarketService:
    """东方财富板块/资金流只读服务（供 ``/api/market/*`` 使用）。"""

    def __init__(
        self,
        timeout: float = 8.0,
        max_retries: int = 1,
        hosts: Sequence[str] = QUOTE_HOSTS,
        history_hosts: Sequence[str] = HISTORY_HOSTS,
        page_size: int = MAX_PAGE_SIZE,
    ):
        self._max_retries = max_retries
        self._page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        self._client = httpx.AsyncClient(timeout=timeout, headers=EASTMONEY_HEADERS)
        self._pool = EastmoneyHostPool(hosts)
        self._history_pool = EastmoneyHostPool(history_hosts)

    # ──────── 板块 ────────

    async def list_boards(
        self, kind: str = "industry", limit: int = 50, order: str = "desc"
    ) -> list[BoardQuote]:
        """板块行情列表，默认按涨跌幅降序。"""
        fs = self._board_fs(kind)
        rows = await self._clist(fs, _BOARD_FIELDS, limit=limit, fid="f3", order=order)
        return parse_board_rows({"data": {"diff": rows}}, kind)

    async def board_constituents(
        self, board_code: str, limit: int = 100
    ) -> list[BoardMember]:
        """板块成分股（按涨跌幅降序）。"""
        code = (board_code or "").strip().upper()
        if not code.startswith("BK") or not code[2:].isdigit():
            raise ValueError(f"板块代码不合法: {board_code!r}（应形如 BK0475）")
        rows = await self._clist(
            f"b:{code}", _MEMBER_FIELDS, limit=limit, fid="f3"
        )
        return parse_board_member_rows({"data": {"diff": rows}})

    # ──────── 资金流 ────────

    async def board_fund_flow(
        self, kind: str = "industry", limit: int = 50, order: str = "desc"
    ) -> list[FundFlowRow]:
        """板块资金流排行（默认按主力净流入降序）。"""
        fs = self._board_fs(kind)
        rows = await self._clist(fs, _FLOW_FIELDS, limit=limit, fid="f62", order=order)
        return parse_fund_flow_rows({"data": {"diff": rows}}, kind)

    async def stock_fund_flow_rank(
        self, limit: int = 50, order: str = "desc"
    ) -> list[FundFlowRow]:
        """个股资金流排行（默认按主力净流入降序）。"""
        rows = await self._clist(
            ALL_A_SHARES_FS, _FLOW_FIELDS, limit=limit, fid="f62", order=order
        )
        return parse_fund_flow_rows({"data": {"diff": rows}}, "stock")

    async def stock_fund_flow_history(
        self, symbol: str, days: int = 60
    ) -> list[FundFlowPoint]:
        """个股资金流历史（按交易日升序）。"""
        params = {
            "lmt": str(max(1, min(days, 1000))),
            "klt": "101",
            "secid": to_eastmoney_secid(symbol),
            "fields1": "f1,f2,f3,f7",
            "fields2": (
                "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
            ),
            "ut": EASTMONEY_UT,
        }
        payload = await self._request(
            FFLOW_DAYKLINE_PATH, params, pool=self._history_pool,
            accept=_has_fflow_rows,
        )
        points = parse_fund_flow_history(payload)
        points.sort(key=lambda point: point.trade_date)
        return points

    async def close(self) -> None:
        await self._client.aclose()

    # ──────── 内部实现 ────────

    def _board_fs(self, kind: str) -> str:
        key = (kind or "").strip().lower()
        if key not in BOARD_KINDS:
            raise ValueError(
                f"不支持的板块类型: {kind!r}（可选 {'/'.join(BOARD_KINDS)}）"
            )
        return BOARD_KINDS[key]

    async def _clist(
        self,
        fs: str,
        fields: str,
        *,
        limit: int,
        fid: str,
        order: str = "desc",
    ) -> list[dict]:
        """按需翻页拉取 clist 行（单页上限 100）。"""
        want = max(1, min(limit, 1000))
        collected: list[dict] = []
        page_number = 1
        while len(collected) < want:
            size = min(self._page_size, want - len(collected))
            params = {
                "pn": str(page_number),
                "pz": str(size),
                "po": "1" if order == "desc" else "0",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": fid,
                "fs": fs,
                "fields": fields,
                "ut": EASTMONEY_UT,
            }
            payload = await self._request(CLIST_PATH, params, accept=_has_clist_rows)
            page = _rows(payload)
            if not page:
                break
            collected.extend(page)
            if len(page) < size:
                break
            page_number += 1
        return collected[:want]

    async def _request(
        self,
        path: str,
        params: dict,
        *,
        pool: EastmoneyHostPool | None = None,
        accept=None,
    ) -> dict:
        return await eastmoney_request_json(
            self._client,
            pool or self._pool,
            path,
            params,
            max_retries=self._max_retries,
            accept=accept,
        )
