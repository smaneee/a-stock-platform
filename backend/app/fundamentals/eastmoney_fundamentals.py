"""全市场基本面/估值快照：解析与采集（东财行情列表接口）。

## 字段是怎么确认的（不是凭印象）

2026-09-14 用 **akshare 的独立财务摘要**（``stock_financial_abstract("000333")``，
数据源与东财行情接口不同）逐项交叉核对美的集团（000333）2026-06-30 报告期：

============================  ==================  ==================  ==============
东财字段                       含义                东财值              akshare/核算值
============================  ==================  ==================  ==============
``f40``                       营业总收入          261052379000        261052379000.0
``f45``                       归母净利润          26446037000         26446037000.0
``f37``                       净资产收益率(ROE)   11.33               11.33
``f49``                       毛利率(%)           25.2557645483       25.255764
``f57``                       资产负债率(%)       64.8900151015       64.890015
``f112``                      摊薄每股收益        3.466361973         3.466361
``f113``                      每股净资产          27.816806308        27.816806
``f129``                      销售净利率(%)       10.2225117134       10.222511
``f135``                      股东权益合计        225792941000        225792941000.0
``f20``                       总市值              663904738662        = 87.02(现价) × 7.6295e9 股本
``f23``                       市净率              3.13                = 87.02 / 27.816806
``f9``                        市盈率(动态)        12.55               = 663904738662 / (26446037000×2)
``f41``                       营收同比(%)         3.4561222865        = 261052379000/252331494000−1
``f46``                       净利润同比(%)       1.661997971068      = 26446037000/26013690000−1
``f221``                      报告期              20260630            —
============================  ==================  ==================  ==============

**未做独立验证的字段一律不采集**（例如 ``f132``/``f127``/``f173`` 等，数值对不上任何
已知口径）。宁可少一个指标，不可多一个猜测。

## 工程约束（实测）

* 分页硬上限 **100 行/页**（``pz=200`` 实测只回 100 行，``total=5913``）→ 全市场约 60 页；
* ``push2.eastmoney.com`` 会直接断连（``RemoteProtocolError``），备用域
  ``push2delay.eastmoney.com`` 可用 —— 因此复用 ``provider`` 里的主机池与轮换逻辑；
* 本机注册表配了 HTTP 代理，必须 ``trust_env=False``，否则拿到空响应体。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

import httpx

from app.market_data.eastmoney_provider import (
    EASTMONEY_HEADERS,
    EASTMONEY_UT,
    QUOTE_HOSTS,
)

logger = logging.getLogger(__name__)

#: 全市场板块过滤（沪深主板 + 创业板 + 科创板 + 北交所），与 universe 口径一致
MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
LIST_PATH = "/api/qt/clist/get"
#: 实测单页上限 100 行
MAX_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 80

#: 输出字段名 → (东财字段, 解析方式)。解析方式: num=必填数值, opt_num=可空数值,
#: text=文本, day=YYYYMMDD 日期
FIELD_MAP: dict[str, tuple[str, str]] = {
    "symbol": ("f12", "text"),
    "name": ("f14", "text"),
    "price": ("f2", "num"),
    "market_cap": ("f20", "num"),
    "float_market_cap": ("f21", "num"),
    "pe_dynamic": ("f9", "opt_num"),
    "pb": ("f23", "opt_num"),
    "roe": ("f37", "opt_num"),
    "revenue": ("f40", "opt_num"),
    "revenue_yoy": ("f41", "opt_num"),
    "net_profit_parent": ("f45", "opt_num"),
    "net_profit_yoy": ("f46", "opt_num"),
    "gross_margin": ("f49", "opt_num"),
    "debt_ratio": ("f57", "opt_num"),
    "industry": ("f100", "text"),
    "eps_diluted": ("f112", "opt_num"),
    "bps": ("f113", "opt_num"),
    "net_margin": ("f129", "opt_num"),
    "equity": ("f135", "opt_num"),
    "report_date": ("f221", "day"),
}

#: 请求字段串（去重后按 f 编号排序，便于比对抓包）
REQUEST_FIELDS = ",".join(
    sorted({em for em, _ in FIELD_MAP.values()}, key=lambda k: int(k[1:]))
)

#: 报告期 → 年化倍数（一季度×4、半年×2、三季度×4/3、年报×1）
ANNUALIZE_FACTOR: dict[int, float] = {3: 4.0, 6: 2.0, 9: 4.0 / 3.0, 12: 1.0}

#: 动态市盈率自洽校验的容许相对误差
PE_TOLERANCE = 0.05


def _to_float(raw: object) -> float | None:
    if raw is None or raw == "-" or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def _to_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def annualization_factor(report_date: date | None) -> float | None:
    """报告期 → 年化倍数。**这是模型推断**：假定年内经营均匀，实际有季节性。"""
    if report_date is None:
        return None
    return ANNUALIZE_FACTOR.get(report_date.month)


@dataclass(frozen=True)
class FundamentalSnapshotData:
    """一行基本面快照（原始采集值 + 明确标注的派生值）。"""

    symbol: str
    name: str
    price: float
    market_cap: float
    float_market_cap: float | None
    pe_dynamic: float | None
    pb: float | None
    roe: float | None
    revenue: float | None
    revenue_yoy: float | None
    net_profit_parent: float | None
    net_profit_yoy: float | None
    gross_margin: float | None
    debt_ratio: float | None
    industry: str | None
    eps_diluted: float | None
    bps: float | None
    net_margin: float | None
    equity: float | None
    report_date: date | None
    source: str = "eastmoney"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # ── 派生（模型推断，口径写在方法名与注释里） ──────────────────────────
    @property
    def shares_outstanding(self) -> float | None:
        """总股本 = 总市值 / 现价（与行情接口自洽，实测 000333 → 7.6295e9 股）。"""
        if self.market_cap > 0 and self.price > 0:
            return self.market_cap / self.price
        return None

    @property
    def annualized_revenue(self) -> float | None:
        factor = annualization_factor(self.report_date)
        if self.revenue is None or factor is None:
            return None
        return self.revenue * factor

    @property
    def annualized_net_profit(self) -> float | None:
        factor = annualization_factor(self.report_date)
        if self.net_profit_parent is None or factor is None:
            return None
        return self.net_profit_parent * factor

    @property
    def pe_from_statements(self) -> float | None:
        """用「总市值 ÷ 年化归母净利润」自算市盈率，用来校验 ``f9``。"""
        profit = self.annualized_net_profit
        if profit is None or profit <= 0:
            return None
        return self.market_cap / profit

    @property
    def pb_from_statements(self) -> float | None:
        if self.bps is None or self.bps <= 0:
            return None
        return self.price / self.bps

    @property
    def ps_annualized(self) -> float | None:
        """市销率（年化口径）。**模型推断**：需年化处理，否则季度口径会高估。"""
        revenue = self.annualized_revenue
        if revenue is None or revenue <= 0:
            return None
        return self.market_cap / revenue

    @property
    def goodwill_to_equity(self) -> float | None:
        """商誉/净资产。行情接口不提供商誉 → 恒为 ``None``，需三表明细（见模块文档）。"""
        return None

    @property
    def ocf_to_profit(self) -> float | None:
        """经营现金流/净利润。行情接口不提供现金流 → 恒为 ``None``，需三表明细。"""
        return None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": self.price,
            "market_cap": self.market_cap,
            "float_market_cap": self.float_market_cap,
            "pe_dynamic": self.pe_dynamic,
            "pb": self.pb,
            "roe": self.roe,
            "revenue": self.revenue,
            "revenue_yoy": self.revenue_yoy,
            "net_profit_parent": self.net_profit_parent,
            "net_profit_yoy": self.net_profit_yoy,
            "gross_margin": self.gross_margin,
            "debt_ratio": self.debt_ratio,
            "industry": self.industry,
            "eps_diluted": self.eps_diluted,
            "bps": self.bps,
            "net_margin": self.net_margin,
            "equity": self.equity,
            "report_date": self.report_date.isoformat() if self.report_date else None,
            "source": self.source,
            "warnings": list(self.warnings),
        }


def _consistency_warnings(row: dict) -> tuple[str, ...]:
    """自洽性告警：上游字段互相矛盾时**不静默取用**，而是记录告警。"""
    warnings: list[str] = []
    pe = row.get("pe_dynamic")
    profit = row.get("net_profit_parent")
    cap = row.get("market_cap")
    factor = annualization_factor(row.get("report_date"))
    if pe and pe > 0 and profit and profit > 0 and cap and factor:
        implied = cap / (profit * factor)
        if abs(implied - pe) / pe > PE_TOLERANCE:
            warnings.append(
                f"动态市盈率自洽性偏差过大：接口 {pe:.2f} vs 自算 {implied:.2f}"
                f"（市值÷年化归母净利润），已按接口值使用但需人工复核"
            )
    if row.get("revenue") is None or row.get("net_profit_parent") is None:
        warnings.append("缺少营收或净利润 → 估值/质量模块将按「指标缺失」处理")
    if row.get("report_date") is None:
        warnings.append("缺少报告期 → 无法判断数据时效，置信度按最低处理")
    return tuple(warnings)


def parse_fundamentals(rows: list[dict]) -> list[FundamentalSnapshotData]:
    """把接口原始行解析成快照对象。**跳过**没有代码/现价的行，不构造空壳。"""
    parsed: list[FundamentalSnapshotData] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        values: dict[str, object] = {}
        for out_name, (em_key, kind) in FIELD_MAP.items():
            value = raw.get(em_key)
            if kind == "num":
                values[out_name] = _to_float(value)
            elif kind == "opt_num":
                values[out_name] = _to_float(value)
            elif kind == "day":
                values[out_name] = _to_date(value)
            else:
                text = str(value).strip() if value is not None else ""
                values[out_name] = text or None

        symbol = values.get("symbol")
        price = values.get("price")
        cap = values.get("market_cap")
        if not symbol or not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit():
            continue
        if price is None or price <= 0 or cap is None or cap <= 0:
            continue

        values["price"] = float(price)
        values["market_cap"] = float(cap)
        parsed.append(
            FundamentalSnapshotData(
                **{k: values.get(k) for k in FIELD_MAP if k not in ("price", "market_cap")},
                price=float(price),
                market_cap=float(cap),
                warnings=_consistency_warnings(values),
            )
        )
    return parsed


@dataclass
class FetchStats:
    pages_fetched: int = 0
    rows_raw: int = 0
    rows_parsed: int = 0
    #: 原始行里被跳过的数量（无代码/无现价/无市值），用于如实报告覆盖率缺口
    rows_skipped: int = 0
    total_reported: int | None = None
    host: str | None = None
    #: 累计重试次数（>0 说明上游不稳定，需在报告里体现）
    retries: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float | None:
        """解析行数 / 上游报告总数。低于 1 的原因可能是页面失败，也可能是行被有意跳过。"""
        if not self.total_reported:
            return None
        return round(self.rows_parsed / self.total_reported, 4)

    @property
    def retrieval_ratio(self) -> float | None:
        """取回行数 / 上游报告总数 —— 衡量"有没有取全"（跳过不算取漏）。"""
        if not self.total_reported:
            return None
        return round(self.rows_raw / self.total_reported, 4)

    def to_dict(self) -> dict:
        return {
            "pages_fetched": self.pages_fetched,
            "rows_raw": self.rows_raw,
            "rows_parsed": self.rows_parsed,
            "rows_skipped": self.rows_skipped,
            "total_reported": self.total_reported,
            "coverage_ratio": self.coverage_ratio,
            "retrieval_ratio": self.retrieval_ratio,
            "host": self.host,
            "retries": self.retries,
            "failures": list(self.failures),
            # complete 指的是**取回**是否完整（无失败页且取回行数≥上游总数）。
            # 被有意跳过的行（无现价/无市值，如停牌）不计入"取漏"。
            "complete": not self.failures
            and (self.total_reported is None or self.rows_raw >= self.total_reported),
        }


async def fetch_market_fundamentals(
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = MAX_PAGE_SIZE,
    pause_seconds: float = 0.15,
    page_retries: int = 3,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[FundamentalSnapshotData], FetchStats]:
    """按页拉取全市场基本面快照（只读）。返回（快照列表, 统计）。

    * 单页上限 100（实测），所以全市场约 60 页；
    * **失败即重试**：2026-09-14 实测第 44 页出现 ``ReadError``（备用域瞬时断连），
      若直接放弃会只拿到 4087/5913 行 —— 因此每页带退避重试，且每个主机都值得重试
      （前一轮失败的域过几秒往往恢复）；
    * 仍失败则该页记入 ``failures`` 并停止：宁可少抓，也不返回"半份数据 + 完整"的假象。
      调用方必须检查 ``stats.failures`` 与 ``total_reported``。
    """
    stats = FetchStats()
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=30.0, headers=EASTMONEY_HEADERS, trust_env=False
        )
    collected: list[FundamentalSnapshotData] = []
    try:
        for page in range(1, max_pages + 1):
            payload = None
            last_error = ""
            for attempt in range(1, page_retries + 1):
                for host in QUOTE_HOSTS:
                    try:
                        resp = await client.get(
                            f"https://{host}{LIST_PATH}",
                            params={
                                "pn": str(page),
                                "pz": str(page_size),
                                "po": "1",
                                "np": "1",
                                "fltt": "2",
                                "invt": "2",
                                "ut": EASTMONEY_UT,
                                "fid": "f12",
                                "fs": MARKET_FS,
                                "fields": REQUEST_FIELDS,
                            },
                        )
                        body = resp.json()
                        if not (body.get("data") or {}).get("diff"):
                            last_error = f"{host}: 返回空 diff"
                            continue
                        payload = body
                        stats.host = host
                        stats.retries += attempt - 1
                        break
                    except Exception as exc:  # noqa: BLE001 - 换主机/重试，不吞信息
                        last_error = f"{host}: {type(exc).__name__}: {exc}"
                        logger.warning("基本面快照分页失败 %s", last_error)
                if payload is not None:
                    break
                if attempt < page_retries:
                    await asyncio.sleep(min(2.0 * attempt, 5.0))
            if payload is None:
                stats.failures.append(
                    f"page {page} 在 {page_retries} 轮重试后仍失败: {last_error}"
                )
                break

            data = payload.get("data") or {}
            rows = data.get("diff") or []
            if stats.total_reported is None and data.get("total") is not None:
                stats.total_reported = int(data["total"])
            stats.pages_fetched += 1
            stats.rows_raw += len(rows)
            parsed = parse_fundamentals(rows)
            stats.rows_parsed += len(parsed)
            stats.rows_skipped += len(rows) - len(parsed)
            collected.extend(parsed)
            if len(rows) < page_size:
                break
            if pause_seconds:
                await asyncio.sleep(pause_seconds)
    finally:
        if owned:
            await client.aclose()
    return collected, stats
