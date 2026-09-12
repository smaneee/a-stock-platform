"""东方财富涨停板行情（``push2ex.eastmoney.com``）情绪池。

东财「涨停板行情」（``quote.eastmoney.com/ztb/detail``）按交易日给出 5 个横截面
情绪池，是判断市场热度（封板率、连板高度、炸板率）的基础数据：

- 涨停股池：当日封住涨停的个股（封板资金、首次/最后封板时间、连板数）
- 跌停股池：当日封住跌停的个股（封单资金、连续跌停天数、开板次数）
- 炸板股池：盘中触及涨停但收盘未封住的个股（涨停价、炸板次数、振幅）
- 强势股池：涨幅居前且走势强劲的个股（是否新高、量比、涨速）
- 次新股池：上市不久的个股（上市日期、是否新高）

实现要点：

- **声明式字段映射**：每个池声明「输出名 → 上游字段 + 解析方式」，上游改名或删列
  时 :func:`_parse_pool` 立即抛错，而不是静默返回一堆 0。
- **价格口径**：push2ex 的价格字段是「元 × 1000」（``13880`` = 13.88 元），
  统一除以 1000 还原为元；已逐只与腾讯行情核对（000993 13.88 / 002161 8.04 /
  002790 9.31）。上市首日等「无涨跌幅限制」场景上游会给出 ``1e9`` 这类占位值，
  超出 ``MAX_PLAUSIBLE_PRICE`` 时统一返回 ``None``，避免显示成 1000000.00 元。
- **时间口径**：``fbt`` / ``lbt`` 是 HHMMSS 整数（``92500`` → ``09:25:00``）。
- **涨停统计**：``zttj`` 是 ``{"days": 3, "ct": 3}`` 对象，输出为「3天3板」。
- **主机故障转移**：复用 :class:`EastmoneyHostPool`，与行情 / 数据中心口径一致。

已知局限：``date`` 参数经实测被上游忽略（传 20260909 / 20260910 / 20260912 均返回
同一交易日），因此本模块只提供「最近交易日」快照，并把上游回传的 ``qdate`` 作为
``LimitUpResult.trade_date`` 返回，不声称支持历史查询。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from app.market_data.eastmoney_provider import (
    EASTMONEY_HEADERS,
    EASTMONEY_UT,
    EastmoneyHostPool,
    eastmoney_request_json,
    em_to_float,
)

# push2ex 只有一台主机；放进主机池是为了复用冷却与故障转移逻辑
LIMIT_UP_HOSTS = ("push2ex.eastmoney.com",)
LIMIT_UP_HEADERS = {
    **EASTMONEY_HEADERS,
    "Referer": "https://quote.eastmoney.com/ztb/detail",
}
# 东财涨停板页固定参数
LIMIT_UP_DPT = "wz.ztzt"
# 单页上限（上游对超大 pagesize 会截断）
POOL_PAGE_SIZE_MAX = 200
# push2ex 价格字段 = 元 × 1000
PRICE_SCALE = 1000.0
# 新股上市首日等「无涨跌幅限制」场景，上游用 1e9 之类的占位值表示「没有涨停价」，
# 直接当价格显示会变成 1000000.00 元；A 股单股价格不可能达到 1 万元，据此判为占位。
MAX_PLAUSIBLE_PRICE = 10_000.0
# A 股交易日以北京时间为准
BEIJING = timezone(timedelta(hours=8))

_YES_NO = {"1": "是", "0": "否", "是": "是", "否": "否"}


class EastmoneyLimitUpError(RuntimeError):
    """东方财富涨停板行情不可用，或返回了与声明不符的数据。"""


def _text(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def _number(raw: Any) -> float:
    return em_to_float(raw)


def _optional_number(raw: Any) -> float | None:
    """保留「无数据」语义的数值（如亏损股没有动态市盈率）。"""
    if raw is None or raw == "" or raw == "-":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _price(raw: Any) -> float | None:
    """push2ex 的价格字段是「元 × 1000」；无涨跌幅限制的占位值返回 None。"""
    value = em_to_float(raw) / PRICE_SCALE
    if value <= 0 or value >= MAX_PLAUSIBLE_PRICE:
        return None
    return value


def _integer(raw: Any) -> int:
    return int(em_to_float(raw))


def _ymd(raw: Any) -> str:
    """``20260911`` → ``2026-09-11``；无值返回空串。"""
    text = _text(raw)
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if len(text) >= 10 else text


def _clock(raw: Any) -> str:
    """``92500`` → ``09:25:00``；0 或缺失返回空串。"""
    value = int(em_to_float(raw))
    if value <= 0:
        return ""
    text = f"{value:06d}"
    return f"{text[:2]}:{text[2:4]}:{text[4:]}"


def _limit_up_stat(raw: Any) -> str:
    """``{"days": 3, "ct": 3}`` → ``3天3板``；无连板记录返回空串。"""
    if isinstance(raw, dict):
        days = int(em_to_float(raw.get("days")))
        count = int(em_to_float(raw.get("ct")))
        if days <= 0 or count <= 0:
            return ""
        return f"{days}天{count}板"
    return _text(raw)


def _yes_no(raw: Any) -> str:
    value = _text(raw)
    return _YES_NO.get(value, value)


_PARSERS: dict[str, Callable[[Any], Any]] = {
    "text": _text,
    "num": _number,
    "opt_num": _optional_number,
    "price": _price,
    "int": _integer,
    "ymd": _ymd,
    "clock": _clock,
    "zttj": _limit_up_stat,
    "yesno": _yes_no,
}


@dataclass(frozen=True)
class PoolField:
    """池中的一列：输出名、上游字段、解析方式与中文表头。"""

    key: str
    column: str
    kind: str = "num"
    title: str = ""

    def parse(self, row: dict) -> Any:
        parser = _PARSERS.get(self.kind)
        if parser is None:  # pragma: no cover - 声明错误在测试中兜住
            raise EastmoneyLimitUpError(f"未知字段类型: {self.kind}")
        return parser(row.get(self.column))


@dataclass(frozen=True)
class LimitUpPool:
    """一个情绪池的声明。"""

    key: str
    label: str
    path: str
    fields: tuple[PoolField, ...]
    sort_field: str
    sort_order: str = "desc"
    description: str = ""

    @property
    def columns(self) -> tuple[str, ...]:
        """上游必须提供的列（多个输出字段可共用一列，只校验一次）。"""
        return tuple(dict.fromkeys(item.column for item in self.fields))


# ──────── 池声明 ────────

LIMIT_UP = LimitUpPool(
    key="limit-up",
    label="涨停股池",
    description="当日封住涨停的个股：封板资金、首次/最后封板时间、连板数与炸板次数。",
    path="/getTopicZTPool",
    sort_field="fbt",
    sort_order="asc",
    fields=(
        PoolField("symbol", "c", "text", "股票代码"),
        PoolField("name", "n", "text", "股票名称"),
        PoolField("price", "p", "price", "最新价(元)"),
        PoolField("change_pct", "zdp", "num", "涨跌幅(%)"),
        PoolField("amount", "amount", "num", "成交额(元)"),
        PoolField("turnover_rate", "hs", "num", "换手率(%)"),
        PoolField("seal_amount", "fund", "num", "封板资金(元)"),
        PoolField("first_seal_time", "fbt", "clock", "首次封板时间"),
        PoolField("last_seal_time", "lbt", "clock", "最后封板时间"),
        PoolField("broken_times", "zbc", "int", "炸板次数"),
        PoolField("limit_up_stat", "zttj", "zttj", "涨停统计"),
        PoolField("boards", "lbc", "int", "连板数"),
        PoolField("float_market_cap", "ltsz", "num", "流通市值(元)"),
        PoolField("total_market_cap", "tshare", "num", "总市值(元)"),
        PoolField("industry", "hybk", "text", "所属行业"),
    ),
)

LIMIT_DOWN = LimitUpPool(
    key="limit-down",
    label="跌停股池",
    description="当日封住跌停的个股：封单资金、板上成交额、连续跌停天数与开板次数。",
    path="/getTopicDTPool",
    sort_field="fund",
    fields=(
        PoolField("symbol", "c", "text", "股票代码"),
        PoolField("name", "n", "text", "股票名称"),
        PoolField("price", "p", "price", "最新价(元)"),
        PoolField("change_pct", "zdp", "num", "涨跌幅(%)"),
        PoolField("amount", "amount", "num", "成交额(元)"),
        PoolField("turnover_rate", "hs", "num", "换手率(%)"),
        PoolField("dynamic_pe", "pe", "opt_num", "动态市盈率"),
        PoolField("seal_amount", "fund", "num", "封单资金(元)"),
        PoolField("board_amount", "fba", "num", "板上成交额(元)"),
        PoolField("last_seal_time", "lbt", "clock", "最后封板时间"),
        PoolField("limit_down_days", "days", "int", "连续跌停(天)"),
        PoolField("open_times", "oc", "int", "开板次数"),
        PoolField("float_market_cap", "ltsz", "num", "流通市值(元)"),
        PoolField("total_market_cap", "tshare", "num", "总市值(元)"),
        PoolField("industry", "hybk", "text", "所属行业"),
    ),
)

BROKEN_BOARD = LimitUpPool(
    key="broken-board",
    label="炸板股池",
    description="盘中触及涨停但收盘未封住的个股：涨停价、炸板次数、振幅与涨速。",
    path="/getTopicZBPool",
    sort_field="fbt",
    sort_order="asc",
    fields=(
        PoolField("symbol", "c", "text", "股票代码"),
        PoolField("name", "n", "text", "股票名称"),
        PoolField("price", "p", "price", "最新价(元)"),
        PoolField("change_pct", "zdp", "num", "涨跌幅(%)"),
        PoolField("limit_up_price", "ztp", "price", "涨停价(元)"),
        PoolField("amount", "amount", "num", "成交额(元)"),
        PoolField("turnover_rate", "hs", "num", "换手率(%)"),
        PoolField("first_seal_time", "fbt", "clock", "首次封板时间"),
        PoolField("broken_times", "zbc", "int", "炸板次数"),
        PoolField("amplitude", "zf", "num", "振幅(%)"),
        PoolField("speed", "zs", "num", "涨速(%)"),
        PoolField("limit_up_stat", "zttj", "zttj", "涨停统计"),
        PoolField("float_market_cap", "ltsz", "num", "流通市值(元)"),
        PoolField("total_market_cap", "tshare", "num", "总市值(元)"),
        PoolField("industry", "hybk", "text", "所属行业"),
    ),
)

STRONG = LimitUpPool(
    key="strong",
    label="强势股池",
    description="涨幅居前且走势强劲的个股：是否涨停、是否新高、量比与涨速。",
    path="/getTopicQSPool",
    sort_field="zdp",
    fields=(
        PoolField("symbol", "c", "text", "股票代码"),
        PoolField("name", "n", "text", "股票名称"),
        PoolField("price", "p", "price", "最新价(元)"),
        PoolField("change_pct", "zdp", "num", "涨跌幅(%)"),
        PoolField("limit_up_price", "ztp", "price", "涨停价(元)"),
        PoolField("is_limit_up", "ztf", "yesno", "是否涨停"),
        PoolField("amount", "amount", "num", "成交额(元)"),
        PoolField("turnover_rate", "hs", "num", "换手率(%)"),
        PoolField("is_new_high", "nh", "yesno", "是否新高"),
        PoolField("volume_ratio", "lb", "opt_num", "量比"),
        PoolField("speed", "zs", "num", "涨速(%)"),
        PoolField("limit_up_stat", "zttj", "zttj", "涨停统计"),
        PoolField("float_market_cap", "ltsz", "num", "流通市值(元)"),
        PoolField("total_market_cap", "tshare", "num", "总市值(元)"),
        PoolField("industry", "hybk", "text", "所属行业"),
    ),
)

SUB_NEW = LimitUpPool(
    key="sub-new",
    label="次新股池",
    description="上市不久的个股：上市日期、是否涨停、是否新高与换手率。",
    path="/getTopicCXPool",
    sort_field="zdp",
    fields=(
        PoolField("symbol", "c", "text", "股票代码"),
        PoolField("name", "n", "text", "股票名称"),
        PoolField("price", "p", "price", "最新价(元)"),
        PoolField("change_pct", "zdp", "num", "涨跌幅(%)"),
        PoolField("limit_up_price", "ztp", "price", "涨停价(元)"),
        PoolField("is_limit_up", "ztf", "yesno", "是否涨停"),
        PoolField("amount", "amount", "num", "成交额(元)"),
        PoolField("turnover_rate", "hs", "num", "换手率(%)"),
        PoolField("is_new_high", "nh", "yesno", "是否新高"),
        PoolField("listed_date", "ipod", "ymd", "上市日期"),
        PoolField("limit_up_stat", "zttj", "zttj", "涨停统计"),
        PoolField("float_market_cap", "ltsz", "num", "流通市值(元)"),
        PoolField("total_market_cap", "tshare", "num", "总市值(元)"),
        PoolField("industry", "hybk", "text", "所属行业"),
    ),
)

POOLS: dict[str, LimitUpPool] = {
    pool.key: pool
    for pool in (LIMIT_UP, LIMIT_DOWN, BROKEN_BOARD, STRONG, SUB_NEW)
}


@dataclass(frozen=True)
class LimitUpResult:
    """一次情绪池查询的结果。"""

    pool: LimitUpPool
    trade_date: str
    total: int
    page: int
    items: list[dict[str, Any]]


def _parse_pool(pool: LimitUpPool, rows: list[dict]) -> list[dict[str, Any]]:
    """按声明构造行；上游改名/删列时立即报错，避免静默返回空字段。"""
    if not rows:
        return []
    missing = [column for column in pool.columns if column not in rows[0]]
    if missing:
        raise EastmoneyLimitUpError(
            f"{pool.path} 缺少预期字段: {', '.join(missing)}（东财字段可能已调整）"
        )
    return [{item.key: item.parse(row) for item in pool.fields} for row in rows]


def _accepted(payload: dict) -> bool:
    """``rc=0`` 表示该主机给出了可用结果；其余状态换一台主机再试。"""
    return payload.get("rc") in (0, "0")


def pool_catalog() -> list[dict[str, Any]]:
    """池自描述信息，供前端渲染表头与说明。"""
    return [
        {
            "key": pool.key,
            "label": pool.label,
            "description": pool.description,
            "fields": [
                {"key": item.key, "title": item.title, "kind": item.kind}
                for item in pool.fields
            ],
        }
        for pool in POOLS.values()
    ]


def _beijing_today() -> str:
    return datetime.now(BEIJING).strftime("%Y%m%d")


class EastmoneyLimitUpService:
    """东方财富涨停板行情只读服务（供 ``/api/market/limit-up*`` 使用）。"""

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 1,
        hosts: tuple[str, ...] = LIMIT_UP_HOSTS,
    ):
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout, headers=LIMIT_UP_HEADERS)
        self._pool = EastmoneyHostPool(hosts)

    @staticmethod
    def keys() -> list[str]:
        return list(POOLS)

    @staticmethod
    def pool(pool: str) -> LimitUpPool:
        spec = POOLS.get((pool or "").strip().lower())
        if spec is None:
            raise ValueError(f"未知情绪池: {pool!r}（可选 {', '.join(POOLS)}）")
        return spec

    async def query(
        self,
        pool: str,
        *,
        limit: int = 50,
        page: int = 1,
        order: str | None = None,
    ) -> LimitUpResult:
        """查询一个情绪池的「最近交易日」快照（按该池推荐字段排序）。"""
        spec = self.pool(pool)
        page = max(1, int(page))
        limit = max(1, min(int(limit), POOL_PAGE_SIZE_MAX))
        direction = order if order in ("asc", "desc") else spec.sort_order
        params = {
            "ut": EASTMONEY_UT,
            "dpt": LIMIT_UP_DPT,
            # 上游分页从 0 开始
            "Pageindex": str(page - 1),
            "pagesize": str(limit),
            "sort": f"{spec.sort_field}:{direction}",
            "date": _beijing_today(),
        }
        payload = await eastmoney_request_json(
            self._client,
            self._pool,
            spec.path,
            params,
            max_retries=self._max_retries,
            accept=_accepted,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            # 上游在无数据时返回 data=null，属于正常结果
            return LimitUpResult(spec, "", 0, page, [])
        rows = data.get("pool") or []
        if not isinstance(rows, list):
            raise EastmoneyLimitUpError(f"{spec.path} 返回了非预期的数据结构")
        total = data.get("tc")
        return LimitUpResult(
            pool=spec,
            trade_date=_ymd(data.get("qdate")),
            total=total if isinstance(total, int) else len(rows),
            page=page,
            items=_parse_pool(spec, rows),
        )

    async def close(self) -> None:
        await self._client.aclose()
