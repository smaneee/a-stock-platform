"""东方财富数据中心（data.eastmoney.com）横截面数据集。

行情走 ``push2*/api/qt/*``、板块与资金流走 ``clist``，此外东财还提供一套结构化
「数据中心」报表：龙虎榜、大宗交易、融资融券、沪深港通、机构调研、股东户数、
限售解禁、业绩预告、分红送配。它们共用同一入口
``datacenter-web.eastmoney.com/api/data/v1/get``，用 ``reportName`` 选择报表、
用 ``filter`` 传过滤表达式。

实现要点：

- **声明式字段映射**：每个数据集声明「输出字段名 → 东财列名 + 解析方式」，
  运行时按声明构造行。上游若改名或删列，``_parse_rows`` 会立刻抛错，而不是
  静默返回一堆 0。
- **过滤条件白名单**：``date`` / ``symbol`` 先经严格正则校验再拼进东财的 filter
  表达式，不把用户输入原样送进远端查询串。
- **空结果不是错误**：东财用 ``code=9201`` 表示「数据为空」，此时按空列表返回；
  其它失败码（9501 参数错误、9701 数据繁忙等）按错误处理并切换主机。
- **主机故障转移**：``datacenter-web`` 与 ``datacenter`` 两台主机互为备份，
  复用行情侧的 :class:`EastmoneyHostPool`。

单位口径（均已核对，全部换算成「元」返回）：

- 龙虎榜 / 大宗交易：买、卖、净额均为元（``BILLBOARD_NET_AMT = 买入 - 卖出``）。
- 融资融券：``RZYE``/``RQYE`` 等均为元（贵州茅台融资余额约 1.7e10 元）。
- 沪深港通 ``RPT_MUTUAL_DEAL_HISTORY``：该报表的金额列是**百万元**（``DEAL_AMT``
  沪股通 142256.09 ≈ 1422.6 亿元，且十大成交股合计约 171 亿元），因此统一乘 1e6
  后返回；``HOLD_MARKET_CAP`` 本身是元，不再换算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

from app.market_data.eastmoney_provider import (
    EASTMONEY_HEADERS,
    EastmoneyHostPool,
    eastmoney_request_json,
    em_to_float,
)

DATA_HOSTS = ("datacenter-web.eastmoney.com", "datacenter.eastmoney.com")
DATA_PATH = "/api/data/v1/get"
DATA_HEADERS = {**EASTMONEY_HEADERS, "Referer": "https://data.eastmoney.com/"}

# 东财数据中心单页上限
MAX_PAGE_SIZE = 500
# 状态码 9201 = 数据为空（正常结果，不是错误）
EMPTY_RESULT_CODE = 9201

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL_RE = re.compile(r"^\d{6}$")


class EastmoneyDataError(RuntimeError):
    """东方财富数据中心不可用，或返回了与声明不符的数据。"""


def _text(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _optional_number(raw: Any) -> float | None:
    """保留「无数据」语义的数值（涨跌幅/占比等可能为 null）。"""
    if raw is None or raw == "" or raw == "-":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _number(raw: Any) -> float:
    return em_to_float(raw)


def _million_to_yuan(raw: Any) -> float:
    """东财沪深港通报表的金额以百万元计，统一换算成元。"""
    return em_to_float(raw) * 1_000_000


def _optional_million_to_yuan(raw: Any) -> float | None:
    value = _optional_number(raw)
    return None if value is None else value * 1_000_000


def _integer(raw: Any) -> int:
    return int(em_to_float(raw))


def _day(raw: Any) -> str:
    """``2026-09-11 00:00:00`` → ``2026-09-11``；无值返回空串。"""
    value = _text(raw)
    return value[:10] if len(value) >= 10 else value


_PARSERS: dict[str, Callable[[Any], Any]] = {
    "text": _text,
    "num": _number,
    "opt_num": _optional_number,
    "million": _million_to_yuan,
    "opt_million": _optional_million_to_yuan,
    "int": _integer,
    "date": _day,
}


@dataclass(frozen=True)
class Field:
    """数据集中的一列：输出名、东财列名、解析方式与中文表头。"""

    key: str
    column: str
    kind: str = "num"
    title: str = ""
    labels: dict[str, str] | None = None

    def parse(self, row: dict) -> Any:
        raw = row.get(self.column)
        if self.labels:
            value = _text(raw)
            return self.labels.get(value, value)
        parser = _PARSERS.get(self.kind)
        if parser is None:  # pragma: no cover - 声明错误在测试中兜住
            raise EastmoneyDataError(f"未知字段类型: {self.kind}")
        return parser(raw)


@dataclass(frozen=True)
class DatasetSpec:
    """一个东财数据中心报表的声明。"""

    key: str
    label: str
    report: str
    fields: tuple[Field, ...]
    sort_column: str
    sort_order: str = "-1"
    date_column: str | None = None
    symbol_column: str | None = None
    description: str = ""

    @property
    def columns(self) -> tuple[str, ...]:
        """要向东财请求的列（多个输出字段可共用一列，只请求一次）。"""
        return tuple(dict.fromkeys(item.column for item in self.fields))

    @property
    def supports_date(self) -> bool:
        return self.date_column is not None

    @property
    def supports_symbol(self) -> bool:
        return self.symbol_column is not None


# ──────── 数据集声明 ────────

DRAGON_TIGER = DatasetSpec(
    key="dragon-tiger",
    label="龙虎榜",
    description="每日沪深京龙虎榜上榜个股：上榜原因、买卖金额与净额、成交占比及上榜后涨跌幅。",
    report="RPT_DAILYBILLBOARD_DETAILSNEW",
    date_column="TRADE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="TRADE_DATE",
    fields=(
        Field("trade_date", "TRADE_DATE", "date", title="交易日期"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("close", "CLOSE_PRICE", title="收盘价(元)"),
        Field("change_pct", "CHANGE_RATE", title="涨跌幅(%)"),
        Field("turnover_rate", "TURNOVERRATE", title="换手率(%)"),
        Field("market", "TRADE_MARKET", "text", title="交易市场"),
        Field("billboard_buy_amount", "BILLBOARD_BUY_AMT", title="龙虎榜买入额(元)"),
        Field("billboard_sell_amount", "BILLBOARD_SELL_AMT", title="龙虎榜卖出额(元)"),
        Field("billboard_net_amount", "BILLBOARD_NET_AMT", title="龙虎榜净额(元)"),
        Field("deal_amount", "BILLBOARD_DEAL_AMT", title="龙虎榜成交额(元)"),
        Field("accum_amount", "ACCUM_AMOUNT", title="当日总成交额(元)"),
        Field("deal_amount_ratio", "DEAL_AMOUNT_RATIO", "opt_num", title="龙虎榜成交占比(%)"),
        Field("net_amount_ratio", "DEAL_NET_RATIO", "opt_num", title="龙虎榜净额占比(%)"),
        Field("change_1d_pct", "D1_CLOSE_ADJCHRATE", "opt_num", title="上榜后1日涨跌幅(%)"),
        Field("change_5d_pct", "D5_CLOSE_ADJCHRATE", "opt_num", title="上榜后5日涨跌幅(%)"),
        Field("change_10d_pct", "D10_CLOSE_ADJCHRATE", "opt_num", title="上榜后10日涨跌幅(%)"),
        Field("change_20d_pct", "D20_CLOSE_ADJCHRATE", "opt_num", title="上榜后20日涨跌幅(%)"),
        Field("reason", "EXPLANATION", "text", title="上榜原因"),
        Field("interpretation", "EXPLAIN", "text", title="上榜解读"),
    ),
)

# 买入/卖出侧字段集一致，只有 reportName 不同，由服务按 side 选择
DRAGON_TIGER_SEATS = DatasetSpec(
    key="dragon-tiger-seats",
    label="龙虎榜席位",
    description="龙虎榜买卖席位明细（营业部与机构专用席位）。",
    report="RPT_BILLBOARD_DAILYDETAILSBUY",
    date_column="TRADE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="TRADE_DATE",
    fields=(
        Field("trade_date", "TRADE_DATE", "date", title="交易日期"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("seat_name", "OPERATEDEPT_NAME", "text", title="席位名称"),
        Field("buy_amount", "BUY", title="买入额(元)"),
        Field("sell_amount", "SELL", title="卖出额(元)"),
        Field("net_amount", "NET", title="净额(元)"),
        Field("buy_ratio", "TOTAL_BUYRIO", "opt_num", title="买入占比"),
        Field("sell_ratio", "TOTAL_SELLRIO", "opt_num", title="卖出占比"),
        Field("close", "CLOSE_PRICE", title="收盘价(元)"),
        Field("change_pct", "CHANGE_RATE", title="涨跌幅(%)"),
        Field("accum_amount", "ACCUM_AMOUNT", title="当日总成交额(元)"),
        Field("reason", "EXPLANATION", "text", title="上榜原因"),
    ),
)

BLOCK_TRADE = DatasetSpec(
    key="block-trade",
    label="大宗交易",
    description="大宗交易明细：成交价、折溢率、成交量额与买卖营业部。",
    report="RPT_DATA_BLOCKTRADE",
    date_column="TRADE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="TRADE_DATE",
    fields=(
        Field("trade_date", "TRADE_DATE", "date", title="交易日期"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("deal_price", "DEAL_PRICE", title="成交价(元)"),
        Field("premium_ratio", "PREMIUM_RATIO", "opt_num", title="折溢率(%)"),
        Field("discount_ratio", "DISCOUNT_RATIO", "opt_num", title="折价率(%)"),
        Field("deal_volume", "DEAL_VOLUME", title="成交量(股)"),
        Field("deal_amount", "DEAL_AMT", title="成交额(元)"),
        Field("close", "CLOSE_PRICE", title="收盘价(元)"),
        Field("change_pct", "CHANGE_RATE", "opt_num", title="涨跌幅(%)"),
        Field("trade_ratio", "TURNOVER_RATE", "opt_num", title="成交占比(%)"),
        Field("free_shares_ratio", "FREE_SHARES_RATIO", "opt_num", title="占流通股比(%)"),
        Field("buyer", "BUYER_NAME", "text", title="买方营业部"),
        Field("seller", "SELLER_NAME", "text", title="卖方营业部"),
    ),
)

MARGIN = DatasetSpec(
    key="margin",
    label="融资融券",
    description="融资融券个股明细：融资余额/买入/偿还、融券余额/卖出及占流通市值比。",
    report="RPTA_WEB_RZRQ_GGMX",
    date_column="DATE",
    symbol_column="SCODE",
    sort_column="DATE",
    fields=(
        Field("trade_date", "DATE", "date", title="交易日期"),
        Field("symbol", "SCODE", "text", title="股票代码"),
        Field("name", "SECNAME", "text", title="股票名称"),
        Field("market", "MARKET", "text", title="市场"),
        Field("close", "SPJ", title="收盘价(元)"),
        Field("change_pct", "ZDF", "opt_num", title="涨跌幅(%)"),
        Field("financing_balance", "RZYE", title="融资余额(元)"),
        Field("financing_buy", "RZMRE", title="融资买入额(元)"),
        Field("financing_repay", "RZCHE", title="融资偿还额(元)"),
        Field("financing_net_buy", "RZJME", title="融资净买入(元)"),
        Field("financing_balance_pct", "RZYEZB", "opt_num", title="融资余额占流通市值(%)"),
        Field("short_balance", "RQYE", title="融券余额(元)"),
        Field("short_volume", "RQYL", title="融券余量(股)"),
        Field("short_sell_volume", "RQMCL", title="融券卖出量(股)"),
        Field("short_net_sell", "RQJMG", title="融券净卖出(股)"),
        Field("margin_balance", "RZRQYE", title="融资融券余额(元)"),
        Field("margin_balance_diff", "RZRQYECZ", title="融资融券余额差值(元)"),
    ),
)

# RPT_MUTUAL_DEAL_HISTORY 的通道代码（实测：005 = 001 + 003，006 = 002 + 004）
MUTUAL_TYPE_LABELS = {
    "001": "沪股通",
    "002": "港股通(沪)",
    "003": "深股通",
    "004": "港股通(深)",
    "005": "北向资金合计",
    "006": "南向资金合计",
}

NORTHBOUND = DatasetSpec(
    key="northbound",
    label="沪深港通资金",
    description="沪深港通各通道每日成交与资金数据；金额已按实测口径（东财原始单位为百万元）换算成元。",
    report="RPT_MUTUAL_DEAL_HISTORY",
    date_column="TRADE_DATE",
    symbol_column=None,
    sort_column="TRADE_DATE",
    fields=(
        Field("trade_date", "TRADE_DATE", "date", title="交易日期"),
        Field("mutual_type", "MUTUAL_TYPE", "text", title="通道代码"),
        Field("channel", "MUTUAL_TYPE", "text", title="通道", labels=MUTUAL_TYPE_LABELS),
        Field("deal_amount", "DEAL_AMT", "opt_million", title="成交额(元)"),
        Field("buy_amount", "BUY_AMT", "opt_million", title="买入成交额(元)"),
        Field("sell_amount", "SELL_AMT", "opt_million", title="卖出成交额(元)"),
        Field("net_deal_amount", "NET_DEAL_AMT", "opt_million", title="成交净买额(元)"),
        Field("fund_inflow", "FUND_INFLOW", "opt_million", title="资金净流入(元)"),
        Field("accum_deal_amount", "ACCUM_DEAL_AMT", "opt_million", title="累计成交额(元)"),
        Field("deal_num", "DEAL_NUM", "opt_num", title="成交笔数"),
        Field("hold_market_cap", "HOLD_MARKET_CAP", "opt_num", title="持股市值(元)"),
        Field("quota_balance", "QUOTA_BALANCE", "opt_num", title="额度余额"),
        Field("quota_balance_text", "QUOTA_BALANCE_TEXT", "text", title="额度状态"),
        Field("index_close", "INDEX_CLOSE_PRICE", "opt_num", title="指数收盘"),
        Field("index_change_pct", "INDEX_CHANGE_RATE", "opt_num", title="指数涨跌幅(%)"),
        Field("lead_symbol", "LEAD_STOCKS_CODE", "text", title="领涨股代码"),
        Field("lead_name", "LEAD_STOCKS_NAME", "text", title="领涨股名称"),
        Field("lead_change_pct", "LS_CHANGE_RATE", "opt_num", title="领涨股涨跌幅(%)"),
    ),
)

ORG_SURVEY = DatasetSpec(
    key="org-survey",
    label="机构调研",
    description="上市公司接待机构调研记录：调研日期、方式、地点、接待对象与参与人员。",
    report="RPT_ORG_SURVEYNEW",
    date_column="NOTICE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="NOTICE_DATE",
    fields=(
        Field("notice_date", "NOTICE_DATE", "date", title="公告日期"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("receive_start_date", "RECEIVE_START_DATE", "date", title="调研开始日期"),
        Field("receive_end_date", "RECEIVE_END_DATE", "date", title="调研结束日期"),
        Field("receive_way", "RECEIVE_WAY_EXPLAIN", "text", title="调研方式"),
        Field("receive_place", "RECEIVE_PLACE", "text", title="调研地点"),
        Field("receive_object", "RECEIVE_OBJECT", "text", title="接待对象"),
        Field("org_type", "ORG_TYPE", "text", title="机构类型"),
        Field("investigators", "INVESTIGATORS", "text", title="调研人员"),
        Field("receptionist", "RECEPTIONIST", "text", title="接待人员"),
        Field("receive_count", "NUM", "int", title="接待机构数"),
    ),
)

HOLDER_NUMBER = DatasetSpec(
    key="holder-number",
    label="股东户数",
    description="最新一期股东户数及环比变化、户均持股与户均市值。",
    report="RPT_HOLDERNUMLATEST",
    date_column="END_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="HOLDER_NUM",
    fields=(
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("end_date", "END_DATE", "date", title="截止日期"),
        Field("holder_num", "HOLDER_NUM", "int", title="股东户数"),
        Field("pre_holder_num", "PRE_HOLDER_NUM", "int", title="上期股东户数"),
        Field("holder_num_change", "HOLDER_NUM_CHANGE", "int", title="股东户数变化"),
        Field("holder_num_ratio", "HOLDER_NUM_RATIO", "opt_num", title="股东户数变化率(%)"),
        Field("interval_change_pct", "INTERVAL_CHRATE", "opt_num", title="区间涨跌幅(%)"),
        Field("avg_hold_num", "AVG_HOLD_NUM", "opt_num", title="户均持股(股)"),
        Field("avg_market_cap", "AVG_MARKET_CAP", "opt_num", title="户均市值(元)"),
        Field("close", "CLOSE_PRICE", "opt_num", title="收盘价(元)"),
        Field("change_reason", "CHANGE_REASON", "text", title="变动原因"),
    ),
)

RESTRICTED_RELEASE = DatasetSpec(
    key="restricted-release",
    label="限售解禁",
    description="限售股解禁安排（含未来日期，建议配合 date_from 当解禁日历使用）。",
    report="RPT_LIFT_STAGE",
    date_column="FREE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="FREE_DATE",
    fields=(
        Field("free_date", "FREE_DATE", "date", title="解禁日期"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("free_shares_type", "FREE_SHARES_TYPE", "text", title="解禁股份类型"),
        Field("current_free_shares", "CURRENT_FREE_SHARES", "num", title="本次解禁股数"),
        Field("lift_market_cap", "LIFT_MARKET_CAP", "opt_num", title="解禁市值(元)"),
        Field("free_shares", "FREE_SHARES", "opt_num", title="解禁后流通股(股)"),
        Field("free_ratio", "FREE_RATIO", "opt_num", title="解禁股占流通股(%)"),
        Field("total_ratio", "TOTALSHARES_RATIO", "opt_num", title="解禁股占总股本(%)"),
        Field("holder_num", "BATCH_HOLDER_NUM", "int", title="解禁股东数"),
        Field("market_type", "MARKET_TYPE", "text", title="板块"),
    ),
)

EARNINGS_FORECAST = DatasetSpec(
    key="earnings-forecast",
    label="业绩预告",
    description="业绩预告：预告类型、预计净利润区间、同比增幅与变动原因。",
    report="RPT_PUBLIC_OP_NEWPREDICT",
    date_column="NOTICE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="NOTICE_DATE",
    fields=(
        Field("notice_date", "NOTICE_DATE", "date", title="公告日期"),
        Field("report_date", "REPORT_DATE", "date", title="报告期"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("predict_finance", "PREDICT_FINANCE", "text", title="预告指标"),
        Field("predict_type", "PREDICT_TYPE", "text", title="预告类型"),
        Field("predict_amount_lower", "PREDICT_AMT_LOWER", "opt_num", title="预计下限(元)"),
        Field("predict_amount_upper", "PREDICT_AMT_UPPER", "opt_num", title="预计上限(元)"),
        Field("forecast_jz", "FORECAST_JZ", "opt_num", title="预计中值(元)"),
        Field("increase_jz", "INCREASE_JZ", "opt_num", title="同比增幅中值(%)"),
        Field("add_amp_lower", "ADD_AMP_LOWER", "opt_num", title="同比增幅下限(%)"),
        Field("add_amp_upper", "ADD_AMP_UPPER", "opt_num", title="同比增幅上限(%)"),
        Field("preyear_same_period", "PREYEAR_SAME_PERIOD", "opt_num", title="上年同期(元)"),
        Field("content", "PREDICT_CONTENT", "text", title="预告内容"),
        Field("reason", "CHANGE_REASON_EXPLAIN", "text", title="变动原因"),
    ),
)

DIVIDEND = DatasetSpec(
    key="dividend",
    label="分红送配",
    description="分红送转方案：进度、预案、每股派息、送转比例与除权除息日。",
    report="RPT_SHAREBONUS_DET",
    date_column="NOTICE_DATE",
    symbol_column="SECURITY_CODE",
    sort_column="NOTICE_DATE",
    fields=(
        Field("notice_date", "NOTICE_DATE", "date", title="公告日期"),
        Field("plan_notice_date", "PLAN_NOTICE_DATE", "date", title="预案公告日"),
        Field("symbol", "SECURITY_CODE", "text", title="股票代码"),
        Field("name", "SECURITY_NAME_ABBR", "text", title="股票名称"),
        Field("report_date", "REPORT_DATE", "date", title="报告期"),
        Field("progress", "ASSIGN_PROGRESS", "text", title="分配进度"),
        Field("plan", "IMPL_PLAN_PROFILE", "text", title="分红方案"),
        Field("pretax_bonus", "PRETAX_BONUS_RMB", "opt_num", title="每10股派息(元,含税)"),
        Field("bonus_ratio", "BONUS_RATIO", "opt_num", title="送股比例"),
        Field("it_ratio", "IT_RATIO", "opt_num", title="转增比例"),
        Field("dividend_ratio", "DIVIDENT_RATIO", "opt_num", title="股息率(%)"),
        Field("equity_record_date", "EQUITY_RECORD_DATE", "date", title="股权登记日"),
        Field("ex_dividend_date", "EX_DIVIDEND_DATE", "date", title="除权除息日"),
        Field("basic_eps", "BASIC_EPS", "opt_num", title="每股收益(元)"),
        Field("bvps", "BVPS", "opt_num", title="每股净资产(元)"),
        Field("pnp_yoy_ratio", "PNP_YOY_RATIO", "opt_num", title="净利润同比(%)"),
    ),
)


DATASETS: dict[str, DatasetSpec] = {
    spec.key: spec
    for spec in (
        DRAGON_TIGER,
        BLOCK_TRADE,
        MARGIN,
        NORTHBOUND,
        ORG_SURVEY,
        HOLDER_NUMBER,
        RESTRICTED_RELEASE,
        EARNINGS_FORECAST,
        DIVIDEND,
    )
}

# 龙虎榜席位：买入/卖出两侧用各自报表，字段集一致
SEAT_REPORTS = {
    "buy": "RPT_BILLBOARD_DAILYDETAILSBUY",
    "sell": "RPT_BILLBOARD_DAILYDETAILSSELL",
}


@dataclass(frozen=True)
class DatasetResult:
    """一次数据集查询的结果（total 为东财给出的命中总数，可能大于本页行数）。"""

    spec: DatasetSpec
    total: int
    rows: list[dict[str, Any]]


def _require_date(value: str, label: str) -> str:
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        raise ValueError(f"{label} 需要 YYYY-MM-DD 格式，收到 {value!r}")
    return text


def _require_symbol(value: str) -> str:
    text = (value or "").strip()
    if not _SYMBOL_RE.match(text):
        raise ValueError(f"股票代码需要 6 位数字，收到 {value!r}")
    return text


def _parse_rows(spec: DatasetSpec, rows: Sequence[dict]) -> list[dict[str, Any]]:
    """按声明构造行；上游改名/删列时立即报错，避免静默返回空字段。"""
    if not rows:
        return []
    missing = [column for column in spec.columns if column not in rows[0]]
    if missing:
        raise EastmoneyDataError(
            f"{spec.report} 缺少预期列: {', '.join(missing)}（东财字段可能已调整）"
        )
    return [{item.key: item.parse(row) for item in spec.fields} for row in rows]


def _accepted(payload: dict) -> bool:
    """该主机是否给出了可用结果。

    东财用 ``code=9201`` 表示「查询成功但数据为空」，属于正常结果；其余
    非 ``success`` 状态（如 9701 数据繁忙）应当换一台主机再试。
    """
    return payload.get("success") is True or payload.get("code") == EMPTY_RESULT_CODE


def _sort_type(order: str | None, default: str) -> str:
    if order == "asc":
        return "1"
    if order == "desc":
        return "-1"
    return default


def dataset_catalog() -> list[dict[str, Any]]:
    """数据集自描述信息，供前端渲染表头与参数提示。"""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "supports_date": spec.supports_date,
            "supports_symbol": spec.supports_symbol,
            "fields": [
                {"key": item.key, "title": item.title, "kind": item.kind}
                for item in spec.fields
            ],
        }
        for spec in DATASETS.values()
    ]


class EastmoneyDatacenterService:
    """东方财富数据中心只读服务（供 ``/api/market/datacenter*`` 使用）。"""

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 1,
        hosts: Sequence[str] = DATA_HOSTS,
    ):
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout, headers=DATA_HEADERS)
        self._pool = EastmoneyHostPool(hosts)

    @staticmethod
    def keys() -> list[str]:
        return list(DATASETS)

    @staticmethod
    def spec(dataset: str) -> DatasetSpec:
        spec = DATASETS.get((dataset or "").strip().lower())
        if spec is None:
            raise ValueError(
                f"未知数据集: {dataset!r}（可选 {', '.join(DATASETS)}）"
            )
        return spec

    async def query(
        self,
        dataset: str,
        *,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
        page: int = 1,
        order: str | None = None,
    ) -> DatasetResult:
        """查询一个数据集（默认按该报表的推荐排序，取一页）。"""
        spec = self.spec(dataset)
        filter_expr = self._build_filter(spec, date, date_from, date_to, symbol)
        rows, total = await self._fetch(
            spec.report,
            spec,
            filter_expr=filter_expr,
            limit=limit,
            page=page,
            order=order,
        )
        return DatasetResult(spec=spec, total=total, rows=_parse_rows(spec, rows))

    async def dragon_tiger_seats(
        self,
        symbol: str,
        *,
        trade_date: str | None = None,
        limit: int = 50,
        order: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """龙虎榜买入/卖出席位明细。"""
        spec = DRAGON_TIGER_SEATS
        parts = [f'({spec.symbol_column}="{_require_symbol(symbol)}")']
        if trade_date:
            parts.append(f"({spec.date_column}='{_require_date(trade_date, 'trade_date')}')")
        filter_expr = "".join(parts)
        seats: dict[str, list[dict[str, Any]]] = {}
        for side, report in SEAT_REPORTS.items():
            rows, _ = await self._fetch(
                report,
                spec,
                filter_expr=filter_expr,
                limit=limit,
                page=1,
                order=order,
            )
            seats[side] = _parse_rows(spec, rows)
        return seats

    async def close(self) -> None:
        await self._client.aclose()

    # ──────── 内部实现 ────────

    @staticmethod
    def _build_filter(
        spec: DatasetSpec,
        date: str | None,
        date_from: str | None,
        date_to: str | None,
        symbol: str | None,
    ) -> str:
        parts: list[str] = []
        if date or date_from or date_to:
            if spec.date_column is None:
                raise ValueError(f"数据集 {spec.key} 不支持按日期过滤")
            column = spec.date_column
            if date:
                parts.append(f"({column}='{_require_date(date, 'date')}')")
            if date_from:
                parts.append(f"({column}>='{_require_date(date_from, 'date_from')}')")
            if date_to:
                parts.append(f"({column}<='{_require_date(date_to, 'date_to')}')")
        if symbol:
            if spec.symbol_column is None:
                raise ValueError(f"数据集 {spec.key} 不支持按股票代码过滤")
            parts.append(f'({spec.symbol_column}="{_require_symbol(symbol)}")')
        return "".join(parts)

    async def _fetch(
        self,
        report: str,
        spec: DatasetSpec,
        *,
        filter_expr: str,
        limit: int,
        page: int,
        order: str | None,
    ) -> tuple[list[dict], int]:
        params = {
            "reportName": report,
            "columns": ",".join(spec.columns),
            "source": "WEB",
            "client": "WEB",
            "pageNumber": str(max(1, int(page))),
            "pageSize": str(max(1, min(limit, MAX_PAGE_SIZE))),
            "sortColumns": spec.sort_column,
            "sortTypes": _sort_type(order, spec.sort_order),
        }
        if filter_expr:
            params["filter"] = filter_expr
        payload = await eastmoney_request_json(
            self._client,
            self._pool,
            DATA_PATH,
            params,
            max_retries=self._max_retries,
            accept=_accepted,
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            # 9201：查询正常但没有数据
            return [], 0
        rows = result.get("data") or []
        if not isinstance(rows, list):
            raise EastmoneyDataError(f"{report} 返回了非预期的数据结构")
        total = result.get("count")
        return rows, total if isinstance(total, int) else len(rows)
