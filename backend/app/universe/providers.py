"""全市场证券主数据 Provider 接口。

实现：
- MockUniverseProvider：固定种子 + 预定义样本（默认，CI 友好）
- AkshareUniverseProvider：AKShare 真实数据（生产环境）

所有 Provider 必须遵守：
1) 返回标准 SecurityRecord 列表（与 ORM 字段对应）
2) 失败抛 ProviderError，便于 SyncService 区分成功/失败
3) Provider 自身不带重试，重试由 SyncService 统一管理
4) Provider 不准静默降级：失败就是失败，由调用方决定是否切下一个 provider
5) Provider 必须通过【全市场质量门槛】：
   - 总数 ≥ min_total（生产 3500）
   - SH/SZ/BJ 三交易所都有覆盖
   - 代码 6 位数字格式
   - 重复率 ≤ 1%
   - 关键字段（name / exchange）完整率 ≥ 99%
   任一不达标 → ProviderError("insufficient_market_coverage", ...)，
   18 行 / 缩量 / 异常返回 503，不得落库。
"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from pydantic import BaseModel

from app.time_utils import utc_now

logger = logging.getLogger(__name__)


class SecurityRecord(BaseModel):
    """证券主数据（Provider 返回值，DB 写入前的中间形态）。"""

    symbol: str
    name: str = ""
    exchange: str = ""  # sh / sz / bj
    board: str = "unknown"  # main / gem / star / bj / unknown
    is_st: bool = False
    listing_date: date | None = None
    delisted_date: date | None = None
    trading_status: str = "active"
    sector: str | None = None
    # 数据拉取的时间点（UTC date）。用于 point-in-time 约束：snapshot trading_day
    # 不能晚于 as_of_date，provider 也必须明确告诉调用方"这是何时拉的数据"。
    as_of_date: date | None = None


class ProviderError(Exception):
    """Provider 调用失败的统一错误类型，供 SyncService 决定是否重试。"""

    def __init__(self, source_id: str, message: str):
        super().__init__(f"[{source_id}] {message}")
        self.source_id = source_id


class UniverseProvider(ABC):
    """证券主数据 Provider 抽象接口。"""

    source_id: str = "base"

    @abstractmethod
    async def fetch_all(self) -> list[SecurityRecord]:
        """拉取整个市场的证券主数据。

        失败必须抛 ProviderError，不要返回 []。
        不要静默降级到 Mock — 这是调用方的责任。
        """


# ───────────── 全市场质量门槛配置 ─────────────
# 任一不达标即视为「缩量 / 异常返回」→ ProviderError，503 落库拒绝。
# 数值参考 2024 年 A 股规模：沪深京合计约 5300 只，其中 SH≈2300, SZ≈2900, BJ≈250。
# 阈值给到 3500 / 1000 / 1500 / 100，留 30% 缓冲，防止数据源临时缩量也算成功。
MARKET_COVERAGE_MIN_TOTAL = 3500
MARKET_COVERAGE_MIN_PER_EXCHANGE = {"SH": 1000, "SZ": 1500, "BJ": 100}
MARKET_COVERAGE_MAX_DUPLICATE_RATE = 0.01  # 1%
MARKET_COVERAGE_MIN_NAME_COMPLETENESS = 0.99
MARKET_COVERAGE_MIN_EXCHANGE_COMPLETENESS = 0.99
SYMBOL_PATTERN = re.compile(r"^\d{6}$")  # A 股代码 6 位数字


def validate_market_coverage(
    records: list[SecurityRecord],
    *,
    source_id: str = "akshare",
) -> None:
    """全市场质量门槛校验。

    校验项（任一不达标即 ProviderError）：
    1. 总数 ≥ MARKET_COVERAGE_MIN_TOTAL（3500）
    2. SH/SZ/BJ 三交易所都有覆盖，且每家 ≥ MARKET_COVERAGE_MIN_PER_EXCHANGE
    3. 代码格式（6 位数字）100%
    4. 重复率（输入原始 records 中 symbol 重复）≤ 1%
    5. name 完整率 ≥ 99%
    6. exchange 完整率 ≥ 99%
    """
    if not records:
        raise ProviderError(source_id, "全市场质量门槛：records 为空（不可能零只）")

    total = len(records)
    if total < MARKET_COVERAGE_MIN_TOTAL:
        raise ProviderError(
            source_id,
            f"全市场质量门槛：总数 {total} < {MARKET_COVERAGE_MIN_TOTAL}（数据源缩量）",
        )

    # 代码格式 + 重复率
    invalid_format = 0
    seen: set[str] = set()
    duplicate = 0
    for r in records:
        if not SYMBOL_PATTERN.match(r.symbol or ""):
            invalid_format += 1
            continue
        if r.symbol in seen:
            duplicate += 1
        seen.add(r.symbol)

    if invalid_format > 0:
        raise ProviderError(
            source_id,
            f"全市场质量门槛：{invalid_format}/{total} 行代码不符合 6 位数字格式",
        )

    dup_rate = duplicate / total if total > 0 else 0.0
    if dup_rate > MARKET_COVERAGE_MAX_DUPLICATE_RATE:
        raise ProviderError(
            source_id,
            f"全市场质量门槛：重复率 {dup_rate:.2%} > {MARKET_COVERAGE_MAX_DUPLICATE_RATE:.2%}",
        )

    # 分交易所覆盖
    per_exchange: dict[str, int] = {}
    for r in records:
        ex = (r.exchange or "").upper()
        per_exchange[ex] = per_exchange.get(ex, 0) + 1

    for ex, required in MARKET_COVERAGE_MIN_PER_EXCHANGE.items():
        actual = per_exchange.get(ex, 0)
        if actual < required:
            raise ProviderError(
                source_id,
                f"全市场质量门槛：{ex} 覆盖 {actual} < {required}（交易所缩量）",
            )

    # name 完整率
    name_missing = sum(1 for r in records if not (r.name or "").strip())
    name_complete_rate = 1 - name_missing / total
    if name_complete_rate < MARKET_COVERAGE_MIN_NAME_COMPLETENESS:
        raise ProviderError(
            source_id,
            f"全市场质量门槛：name 完整率 {name_complete_rate:.2%} < "
            f"{MARKET_COVERAGE_MIN_NAME_COMPLETENESS:.2%}（字段缩量）",
        )

    # exchange 完整率
    ex_missing = sum(1 for r in records if not (r.exchange or "").strip())
    ex_complete_rate = 1 - ex_missing / total
    if ex_complete_rate < MARKET_COVERAGE_MIN_EXCHANGE_COMPLETENESS:
        raise ProviderError(
            source_id,
            f"全市场质量门槛：exchange 完整率 {ex_complete_rate:.2%} < "
            f"{MARKET_COVERAGE_MIN_EXCHANGE_COMPLETENESS:.2%}",
        )

    logger.info(
        "全市场质量门槛通过：total=%d, sh=%d, sz=%d, bj=%d, duplicate=%d",
        total,
        per_exchange.get("SH", 0),
        per_exchange.get("SZ", 0),
        per_exchange.get("BJ", 0),
        duplicate,
    )


class MockUniverseProvider(UniverseProvider):
    """用于 CI / 离线测试的固定种子样本数据。

    数据预设：A 股 + 深 A + 创业板 + 北证 + ST + 已退市，共 26 条
    跨越 SH/SZ/BJ 三个交易所，覆盖各种排除场景。

    注意：本 Provider 只用于 CI / 离线 / 单测场景，生产环境必须显式
    通过 UNIVERSE_PROVIDERS=mock 启用并意识到这是测试模式。
    """

    source_id = "mock"

    _SEED: tuple[dict, ...] = (
        # 上证主板（active）
        {"symbol": "600000", "name": "浦发银行", "exchange": "SH", "listing_date": "1999-11-10"},
        {"symbol": "600519", "name": "贵州茅台", "exchange": "SH", "listing_date": "2001-08-27"},
        {"symbol": "600036", "name": "招商银行", "exchange": "SH", "listing_date": "2002-04-09"},
        {"symbol": "601318", "name": "中国平安", "exchange": "SH", "listing_date": "2007-03-01"},
        # 深证主板
        {"symbol": "000001", "name": "平安银行", "exchange": "SZ", "listing_date": "1991-04-03"},
        {"symbol": "000002", "name": "万科A", "exchange": "SZ", "listing_date": "1991-01-29"},
        {"symbol": "000858", "name": "五粮液", "exchange": "SZ", "listing_date": "1998-04-08"},
        {"symbol": "000333", "name": "美的集团", "exchange": "SZ", "listing_date": "2013-09-18"},
        {"symbol": "000651", "name": "格力电器", "exchange": "SZ", "listing_date": "1996-11-18"},
        {"symbol": "000725", "name": "京东方A", "exchange": "SZ", "listing_date": "2001-01-12"},
        {"symbol": "000063", "name": "中兴通讯", "exchange": "SZ", "listing_date": "1997-11-18"},
        {"symbol": "000538", "name": "云南白药", "exchange": "SZ", "listing_date": "1993-12-15"},
        # 创业板
        {"symbol": "300750", "name": "宁德时代", "exchange": "SZ", "listing_date": "2018-06-11"},
        {"symbol": "300059", "name": "东方财富", "exchange": "SZ", "listing_date": "2010-03-19"},
        {"symbol": "300015", "name": "爱尔眼科", "exchange": "SZ", "listing_date": "2009-10-30"},
        {"symbol": "300760", "name": "迈瑞医疗", "exchange": "SZ", "listing_date": "2018-10-16"},
        {"symbol": "300124", "name": "汇川技术", "exchange": "SZ", "listing_date": "2010-09-28"},
        # 北证
        {"symbol": "830799", "name": "艾融软件", "exchange": "BJ", "listing_date": "2015-06-23"},
        {"symbol": "835185", "name": "贝特瑞", "exchange": "BJ", "listing_date": "2015-12-28"},
        {"symbol": "832000", "name": "曙光数创", "exchange": "BJ", "listing_date": "2014-08-25"},
        {"symbol": "836260", "name": "中讯四方", "exchange": "BJ", "listing_date": "2014-04-14"},
        # ST（特殊处理）
        {"symbol": "600018", "name": "ST上港", "exchange": "SH", "is_st": True, "listing_date": "2000-07-19"},
        {"symbol": "000005", "name": "ST星源", "exchange": "SZ", "is_st": True, "listing_date": "1990-12-19"},
        # 长期停牌（trading_status=suspended，列入待排除）
        {"symbol": "300372", "name": "退市大集", "exchange": "SZ", "trading_status": "suspended", "listing_date": "2010-09-21"},
        # 已退市
        {"symbol": "000040", "name": "退市长岭", "exchange": "SZ", "trading_status": "delisted", "listing_date": "1996-08-26", "delisted_date": "2024-06-13"},
        {"symbol": "600087", "name": "退市油轮", "exchange": "SH", "trading_status": "delisted", "listing_date": "1997-06-12", "delisted_date": "2024-05-22"},
    )

    async def fetch_all(self) -> list[SecurityRecord]:
        as_of = utc_now().date()
        records: list[SecurityRecord] = []
        for row in self._SEED:
            code = row["symbol"]
            exchange = row.get("exchange", "")
            records.append(
                SecurityRecord(
                    symbol=code,
                    name=row["name"],
                    exchange=exchange,
                    board=_infer_board(code, exchange),
                    listing_date=date.fromisoformat(row["listing_date"]) if row.get("listing_date") else None,
                    delisted_date=date.fromisoformat(row["delisted_date"]) if row.get("delisted_date") else None,
                    is_st=row.get("is_st", False),
                    trading_status=row.get("trading_status", "active"),
                    sector=row.get("sector"),
                    as_of_date=as_of,
                )
            )
        return records


# ───────────── AKShare 真实实现 ─────────────


def _normalize_exchange(symbol: str) -> str:
    """根据 6 位股票代码推断交易所前缀。

    AKShare 返回的 code 列不带交易所，但根据行业惯例：
      - 6xxxxx / 9xxxxx → SH（含 605/688 科创板、900 B 股）
      - 0xxxxx / 2xxxxx / 30xxxx → SZ（含 000/002 主板、300 创业板）
      - 4xxxxx / 8xxxxx → BJ（北证，代码一般 6 位但 83/87/43 开头）

    不会 100% 精确（依赖 AKShare 实际格式），但是 fallback；优先 trust 真实源字段。
    """
    s = str(symbol).strip()
    if not s:
        return ""
    head = s[0]
    if head in ("6", "9"):
        return "SH"
    if head in ("0", "2", "3"):
        return "SZ"
    if head in ("4", "8"):
        return "BJ"
    return ""


def _infer_board(symbol: str, exchange: str) -> str:
    """根据代码前缀推断板块（不可变字段，snapshot 时一次性拷贝）。

    规则（业界惯例 + AKShare 文档）：
      - SH 600/601/603/605 → main（主板，含 605 主板新股）
      - SH 688            → star（科创板）
      - SH 9              → b_share（B 股）
      - SZ 000/001/002/003 → main（主板，含 003 主板新股）
      - SZ 300/301        → gem（创业板）
      - SZ 15             → b_share（B 股）
      - BJ 8/4            → bj（北交所，83/87/43 等）
    """
    s = str(symbol or "").strip()
    ex = (exchange or "").upper()
    if not s:
        return "unknown"
    head3 = s[:3]
    head2 = s[:2]
    head1 = s[0]
    if ex == "SH":
        if head3.startswith("688"):
            return "star"
        if head1 == "9":
            return "b_share"
        return "main"  # 600/601/603/605
    if ex == "SZ":
        if head3 in ("300", "301"):
            return "gem"
        if head1 == "2":
            return "b_share"
        return "main"  # 000/001/002/003
    if ex == "BJ":
        return "bj"
    return "unknown"


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s in ("nan", "NaT", "NaN", "None"):
        return None
    # 常见格式：YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return date.fromisoformat(s.replace("/", "-")) if fmt != "%Y%m%d" else date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except (ValueError, TypeError):
            continue
    return None


def _is_st_name(name: str) -> bool:
    """根据股票名称判断是否带 ST / *ST 标记。"""
    if not name:
        return False
    n = name.upper()
    return "ST" in n or "*ST" in n or "S*" in n


def _is_delisted_name(name: str) -> bool:
    """根据名称判断是否已退市（A 股惯例是「退」字 + 公司简称）。"""
    if not name:
        return False
    return "退市" in name or name.startswith("退")


class AkshareUniverseProvider(UniverseProvider):
    """AKShare 数据源（生产环境）。

    实现要点：
    1. **线程隔离**：akshare 是同步阻塞库，必须放在线程池里跑，
       否则会阻塞 asyncio 事件循环，让整个 /sync 路由 hang 住。
    2. **超时控制**：调用 akshare.stock_info_a_code_name() 可能因为
       网络抖动或 akshare 服务器限速而卡死，必须有 timeout。
    3. **字段校验**：返回的 dataframe 必须有 code / name 列，缺字段直接抛错。
    4. **空结果保护**：返回 0 行 → ProviderError，不允许落库空 universe。
    5. **重试交给 SyncService**：本 provider 自身不重试。

    真实源失败必须抛 ProviderError，绝不允许返回 [] 让 SyncService "以为成功"。
    """

    source_id = "akshare"

    def __init__(self, *, timeout_seconds: float = 30.0):
        self._timeout_seconds = timeout_seconds

    async def fetch_all(self) -> list[SecurityRecord]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ProviderError(self.source_id, f"无运行中的事件循环: {exc}")

        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_sync),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise ProviderError(
                self.source_id,
                f"AKShare 调用超时（>{self._timeout_seconds}s）",
            )
        except ProviderError:
            # _fetch_sync 已经包装好 ProviderError，原样抛
            raise
        except Exception as exc:
            # akshare 自身异常（网络、解析、字段缺失等） → 统一包成 ProviderError
            raise ProviderError(self.source_id, f"AKShare 调用失败: {exc}")

        # 标注 as_of_date（用于 point-in-time 约束）
        as_of = utc_now().date()
        records = self._records_from_dataframe(df, as_of_date=as_of)

        # ─────────── 全市场质量门槛（必须在落库前硬拦截） ───────────
        # 18 行 / 缩量 / 缺交易所覆盖 / 代码格式异常 → ProviderError，
        # 上层 SyncService 会让 /sync 返回 503，**不会**落库。
        validate_market_coverage(records, source_id=self.source_id)

        return records

    # ───────────── 阻塞调用（线程池内执行） ─────────────

    def _fetch_sync(self):
        """阻塞调用 akshare，捕获其网络/解析异常统一包装为 ProviderError。"""
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise ProviderError(
                self.source_id,
                f"akshare 未安装: {exc}",
            )

        try:
            # 沪深京 A 股主数据（最新一次接口签名）
            df = ak.stock_info_a_code_name()
        except Exception as exc:
            raise ProviderError(self.source_id, f"akshare.stock_info_a_code_name 失败: {exc}")

        if df is None:
            raise ProviderError(self.source_id, "akshare 返回 None dataframe")
        try:
            rows = df.to_dict("records")
        except Exception as exc:
            raise ProviderError(self.source_id, f"dataframe → dict 失败: {exc}")
        if not rows:
            raise ProviderError(self.source_id, "akshare 返回空列表（market 不可达或接口已废弃）")
        return rows

    # ───────────── dataframe → SecurityRecord ─────────────

    def _records_from_dataframe(
        self, rows: list[dict], as_of_date: date | None = None
    ) -> list[SecurityRecord]:
        records: list[SecurityRecord] = []
        seen: set[str] = set()
        skipped_missing_code = 0

        for row in rows:
            # 字段名兼容：akshare 不同版本可能叫 code / symbol / stock_code
            code = row.get("code") or row.get("symbol") or row.get("stock_code")
            name = row.get("name") or row.get("stock_name") or ""

            if not code:
                skipped_missing_code += 1
                continue

            code = str(code).strip()
            if not code or code in seen:
                continue
            seen.add(code)

            exchange = _normalize_exchange(code)
            board = _infer_board(code, exchange)
            listing_date = _parse_date(
                row.get("ipo_date")
                or row.get("listing_date")
                or row.get("issue_date")
            )

            is_st = _is_st_name(str(name))
            is_delisted = _is_delisted_name(str(name))

            trading_status = "delisted" if is_delisted else "active"
            delisted_date = None  # AKShare 不返回精确退市日期，由 ExclusionEngine 用 listing_date/delisted 标记兜底

            records.append(
                SecurityRecord(
                    symbol=code,
                    name=str(name).strip(),
                    exchange=exchange,
                    board=board,
                    listing_date=listing_date,
                    delisted_date=delisted_date,
                    trading_status=trading_status,
                    is_st=is_st,
                    as_of_date=as_of_date,
                )
            )

        if not records:
            raise ProviderError(
                self.source_id,
                f"akshare 返回 {len(rows)} 行但解析后 0 条有效（缺 code 字段或全为空）",
            )

        if skipped_missing_code > 0:
            logger.warning(
                "akshare provider skipped %d rows missing code (kept %d)",
                skipped_missing_code,
                len(records),
            )

        return records


def build_provider_by_name(name: str, **kwargs) -> UniverseProvider:
    """根据 provider 名称构造实例，未知名称 → ProviderError。

    这是 SyncService 启动时校验的入口，防止拼错 provider 名字后静默退回 mock。
    """
    n = (name or "").strip().lower()
    if n == "mock":
        return MockUniverseProvider()
    if n == "akshare":
        return AkshareUniverseProvider(**kwargs)
    raise ProviderError("factory", f"未知的 UNIVERSE_PROVIDER: {name!r}（仅支持 mock / akshare）")


# ───────────── BaoStock 真实实现（point-in-time 主数据源） ─────────────


# BaoStock code 格式：sh.000001 / sz.000001 / bj.830799 → 6 位纯数字
# 完整 4 所映射：sh / sz / szmb（深主板）/ szcn（创业板）/ shmb（上主板）/ shkc（科创）/ bj
_BAOSTOCK_EXCHANGE_FROM_PREFIX: dict[str, str] = {
    "sh": "SH",
    "sz": "SZ",
    "szmb": "SZ",
    "szcn": "SZ",
    "shmb": "SH",
    "shkc": "SH",
    "bj": "BJ",
}


def _baostock_code_to_symbol(code_with_prefix: str) -> tuple[str, str]:
    """把 BaoStock 的 'sh.000001' 拆成 ('600000', 'SH')。

    BaoStock 的 code 字段一般是 6 位数字，前缀是小写交易所/板块代码：
      - sh.000001（SH 主板/科创板/老股）
      - sz.000001（SZ 主板）
      - sz.300750（深创业板）
      - bj.830799（北交所）

    返回 (symbol_6digits, exchange) 或 ("", "") 解析失败。
    """
    s = str(code_with_prefix or "").strip()
    if not s or "." not in s:
        return ("", "")
    prefix, _, raw = s.partition(".")
    raw = raw.strip()
    if not raw.isdigit() or len(raw) != 6:
        return ("", "")
    exchange = _BAOSTOCK_EXCHANGE_FROM_PREFIX.get(prefix.lower(), "")
    return (raw, exchange)


def _baostock_query_trade_dates(start_date: str, end_date: str) -> list[date]:
    """BaoStock 交易日历查询。失败抛 ProviderError。"""
    import baostock as bs  # type: ignore

    rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
    if rs is None or rs.error_code != "0":
        raise ProviderError(
            "baostock",
            f"query_trade_dates 失败: {getattr(rs, 'error_msg', 'unknown')}",
        )
    rows: list[date] = []
    while rs.next():
        record = rs.get_row_data()
        # fields: calendar_date, is_trading_day
        try:
            if len(record) >= 2 and record[1] == "1":
                rows.append(date.fromisoformat(record[0]))
        except (ValueError, IndexError):
            continue
    return rows


class BaoStockUniverseProvider(UniverseProvider):
    """BaoStock 数据源（生产环境，point-in-time 主数据源）。

    关键能力（来源：https://github.com/lzwme/finance-quant-skills/blob/main/skills/baostock/references/api.md）：
    - `query_stock_basic()`：返回 code/code_name/ipoDate/outDate/type/status，
      其中 `status` 字段 0=在市 / 1=退市 / 2=暂停上市，`type` 1=股票 / 2=指数 / 3=其它 /
      4=可转债 / 5=基金等。`ipoDate` 是真正的 point-in-time 上市日期。
    - `query_all_stock(day)`：按指定日期返回 tradeStatus。但 dev 环境实测这个
      接口会无限迭代（实际是一个 iterator of streaming response），生产可用但 CI
      不能用 — 因此本 Provider **不**调用这个接口。
    - `query_trade_dates()`：返回交易日历。

    实现要点：
    1. **as_of_date 来源**：用 query_trade_dates 把 day 归一到最近交易日。
    2. **线程隔离**：baostock.login / query_* 都是同步阻塞 IO，必须走 run_in_executor。
    3. **超时控制**：整体 timeout（默认 60s）；底层 socket timeout = min(timeout, 30)。
    4. **BJ 缺口**：BaoStock `query_stock_basic` 返回的 type=1 集合覆盖沪深京三所
       （实测 SH / SZ / BJ 都齐全），不需要额外补 BJ — 但保留 supplement 参数
       留个扩展点。
    5. **不静默 fallback**：如果 query_stock_basic 返回 0 行 → ProviderError。
    """

    source_id = "baostock"

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        as_of_date: date | None = None,
        bj_supplement_timeout_seconds: float = 15.0,
    ):
        """Args:
        timeout_seconds: BaoStock fetch 总超时
        as_of_date: 指定拉数据的交易日；None → 用最近交易日
        bj_supplement_timeout_seconds: AKShare BJ 子源超时

        关键：BaoStock 实测在 dev 网络下 query_stock_basic 的 next() 会被限速，
        且 type=1 集合内 BJ=0（实际只有 SH/SZ）。所以本 Provider **自动**调用
        AKShareBjSupplementProvider 补 BJ — 不让调用方注入；注入入口仍保留
        以便测试用。
        """
        self._timeout_seconds = timeout_seconds
        self._as_of_date = as_of_date
        self._bj_supplement = AkshareBjSupplementProvider(
            timeout_seconds=bj_supplement_timeout_seconds
        )

    async def fetch_all(self) -> list[SecurityRecord]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ProviderError(self.source_id, f"无运行中的事件循环: {exc}")

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_sync),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise ProviderError(
                self.source_id,
                f"BaoStock 调用超时（>{self._timeout_seconds}s）",
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(self.source_id, f"BaoStock 调用失败: {exc}")

        records: list[SecurityRecord]
        effective_day: date
        records, effective_day = result

        # ─────────── 全市场质量门槛（必须在落库前硬拦截） ───────────
        validate_market_coverage(records, source_id=self.source_id)

        for r in records:
            r.as_of_date = effective_day
        return records

    # ───────────── 阻塞调用（线程池内执行） ─────────────

    def _fetch_sync(self) -> tuple[list[SecurityRecord], date]:
        """同步 BaoStock 调用全流程：login → query_trade_dates + query_stock_basic → 装配。"""
        try:
            import baostock as bs  # type: ignore
        except ImportError as exc:
            raise ProviderError(
                self.source_id,
                f"baostock 未安装: {exc}",
            )

        # socket timeout 防御：底层 socket 没设 timeout 的话 BaoStock 的 next() 阻塞会无界
        import socket
        try:
            socket.setdefaulttimeout(min(self._timeout_seconds, 30.0))
        except Exception:  # noqa: BLE001
            pass

        lg = bs.login()
        if lg is None or lg.error_code != "0":
            raise ProviderError(
                self.source_id,
                f"baostock.login 失败: {getattr(lg, 'error_msg', 'unknown')}",
            )
        try:
            return self._fetch_sync_inner(bs)
        finally:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass

    def _fetch_sync_inner(self, bs) -> tuple[list[SecurityRecord], date]:
        import baostock as bs  # type: ignore

        # 1) 计算 effective_day（最近交易日 ≤ 今天）
        effective_day = self._resolve_effective_day(bs)

        # 2) query_stock_basic() — 主数据源（含 type / status / ipoDate / outDate）
        rs_basic = bs.query_stock_basic()
        if rs_basic is None or rs_basic.error_code != "0":
            raise ProviderError(
                self.source_id,
                f"query_stock_basic 失败: {getattr(rs_basic, 'error_msg', 'unknown')}",
            )
        basic_fields = list(rs_basic.fields)
        basic_rows: list[list[str]] = []
        # 注意：next() 在 dev 环境下可能非常慢，但不像 query_all_stock 那样无限。
        # 给一个保守的最大行数限制（实际 type=1 < 10000）。
        max_rows = 20000
        while len(basic_rows) < max_rows:
            try:
                if not rs_basic.next():
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning("BaoStock query_stock_basic next() 异常: %s", exc)
                break
            basic_rows.append(rs_basic.get_row_data())
        else:
            logger.warning(
                "BaoStock query_stock_basic 超过 %d 行上限，截断（说明接口异常）",
                max_rows,
            )

        if not basic_rows:
            raise ProviderError(
                self.source_id,
                "query_stock_basic 0 行（market 不可达）",
            )

        # 3) 装配：type=1 过滤 + status 映射
        records = self._assemble_records(
            basic_fields=basic_fields,
            basic_rows=basic_rows,
            effective_day=effective_day,
        )

        # 4) 用 AKShare BJ 子源补 BJ（BaoStock type=1 实测不含 BJ）
        if self._bj_supplement is not None:
            bj_records = self._fetch_bj_supplement_sync()
            existing_symbols = {r.symbol for r in records}
            bj_added = [
                r for r in bj_records
                if r.exchange == "BJ" and r.symbol not in existing_symbols
            ]
            if bj_added:
                records.extend(bj_added)
                logger.info(
                    "BaoStock BJ缺口：AKShare 子源补充 %d 条", len(bj_added)
                )

        return records, effective_day

    def _fetch_bj_supplement_sync(self) -> list[SecurityRecord]:
        """同步调用 AKShareBjSupplementProvider（fetch_all 是 async 包了 run_in_executor）。"""
        import asyncio as _asyncio

        return _asyncio.run(self._bj_supplement.fetch_all())

    # ───────────── 装配 helpers ─────────────

    def _assemble_records(
        self,
        *,
        basic_fields: list[str],
        basic_rows: list[list[str]],
        effective_day: date,
    ) -> list[SecurityRecord]:
        """query_stock_basic → SecurityRecord。

        步骤：
        1. type=1 过滤（剔除指数/债券/基金）
        2. status 映射：0=active / 1=delisted / 2=suspended
        3. ipoDate > effective_day → 跳过（当日尚未上市）
        """
        records: list[SecurityRecord] = []
        seen: set[str] = set()
        skipped_non_stock = 0
        skipped_listed_after = 0

        for row in basic_rows:
            rec = dict(zip(basic_fields, row))
            code_raw = str(rec.get("code", "")).strip()
            symbol, exchange = _baostock_code_to_symbol(code_raw)
            if not symbol:
                continue
            if symbol in seen:
                continue
            seen.add(symbol)

            basic_type = str(rec.get("type", "")).strip()
            if basic_type != "1":
                skipped_non_stock += 1
                continue

            ipo_date = _parse_date(rec.get("ipoDate"))
            out_date = _parse_date(rec.get("outDate"))
            status_raw = str(rec.get("status", "")).strip()
            # 0=在市 / 1=退市 / 2=暂停上市
            if status_raw == "1":
                trading_status = "delisted"
            elif status_raw == "2":
                trading_status = "suspended"
            else:
                trading_status = "active"

            # 上市日期晚于 effective_day → 当日尚未上市，不能入选 universe
            if ipo_date is not None and ipo_date > effective_day:
                skipped_listed_after += 1
                continue

            name = str(rec.get("code_name", "")).strip()
            is_st = _is_st_name(name)
            board = _infer_board(symbol, exchange)
            delisted_date = out_date if trading_status == "delisted" else None

            records.append(
                SecurityRecord(
                    symbol=symbol,
                    name=name,
                    exchange=exchange,
                    board=board,
                    listing_date=ipo_date,
                    delisted_date=delisted_date,
                    trading_status=trading_status,
                    is_st=is_st,
                    as_of_date=effective_day,
                )
            )

        if not records:
            raise ProviderError(
                self.source_id,
                f"BaoStock 装配后 0 条 type=1 记录（原始 {len(basic_rows)} 行，"
                f"跳过非股票 {skipped_non_stock}）",
            )

        if skipped_non_stock > 0:
            logger.info(
                "BaoStock 跳过 %d 条非 type=1 记录（指数/债券/基金）",
                skipped_non_stock,
            )
        if skipped_listed_after > 0:
            logger.info(
                "BaoStock 跳过 %d 条 effective_day 后上市的新股", skipped_listed_after
            )
        return records

    def _resolve_effective_day(self, bs_mod) -> date:
        """把 self._as_of_date 归一到 ≤ 该日的最近一个交易日。"""
        if self._as_of_date is not None:
            target = self._as_of_date
        else:
            target = utc_now().date()

        # 留 10 天窗口，避免 BaoStock 当日尚未发布当日清单
        start = (target - timedelta(days=10)).strftime("%Y-%m-%d")
        end = target.strftime("%Y-%m-%d")
        try:
            trading_days = _baostock_query_trade_dates(start, end)
        except ProviderError:
            logger.warning("BaoStock query_trade_dates 失败，使用 target=%s", target)
            return target
        if not trading_days:
            return target
        eligible = [d for d in trading_days if d <= target]
        if eligible:
            return max(eligible)
        return target


# ───────────── AKShare 北交所子源（补 BaoStock BJ 缺口） ─────────────


class AkshareBjSupplementProvider(UniverseProvider):
    """只取北交所清单 + 上市日期的 AKShare 精简子源。

    不走 `stock_info_a_code_name()`（一锅端 SH+SZ+BJ 一处失败全盘失败），
    单独用 `stock_info_bj_name_code()`，避免上交所 SSL 错误拖垮整个同步。

    用途：
    - 作为 BaoStockUniverseProvider 的 supplement_bj_provider 参数注入
    - 单独走 validate_market_coverage 时会失败（只有 BJ，<3500）
    - 因此本 Provider 不强制走质量门槛（由调用方决定是否校验）
    """

    source_id = "akshare_bj"

    def __init__(self, *, timeout_seconds: float = 15.0):
        self._timeout_seconds = timeout_seconds

    async def fetch_all(self) -> list[SecurityRecord]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ProviderError(self.source_id, f"无运行中的事件循环: {exc}")

        try:
            rows = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_sync),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise ProviderError(
                self.source_id,
                f"AKShare BJ 子源超时（>{self._timeout_seconds}s）",
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                self.source_id, f"AKShare BJ 子源失败: {exc}"
            )

        as_of = utc_now().date()
        return self._records_from_rows(rows, as_of_date=as_of)

    def _fetch_sync(self) -> list[dict]:
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise ProviderError(self.source_id, f"akshare 未安装: {exc}")
        try:
            df = ak.stock_info_bj_name_code()
        except Exception as exc:
            raise ProviderError(
                self.source_id, f"stock_info_bj_name_code 失败: {exc}"
            )
        if df is None:
            raise ProviderError(self.source_id, "stock_info_bj_name_code 返回 None")
        try:
            return df.to_dict("records")
        except Exception as exc:
            raise ProviderError(self.source_id, f"df → dict 失败: {exc}")

    def _records_from_rows(
        self, rows: list[dict], as_of_date: date | None = None
    ) -> list[SecurityRecord]:
        records: list[SecurityRecord] = []
        seen: set[str] = set()
        for row in rows:
            # AKShare 北交所字段是中文：`证券代码 / 证券简称 / 上市日期`
            # 早期版本可能叫 code/name — 兼容两者
            code = (
                row.get("证券代码")
                or row.get("code")
                or row.get("symbol")
                or ""
            )
            code = str(code).strip()
            if not code or not SYMBOL_PATTERN.match(code):
                continue
            if code in seen:
                continue
            seen.add(code)
            name = (
                str(row.get("证券简称") or row.get("name") or row.get("code_name") or "")
                .strip()
            )
            ipo_date = _parse_date(
                row.get("上市日期")
                or row.get("ipo_date")
                or row.get("listing_date")
                or row.get("issue_date")
            )
            sector = (
                str(row.get("所属行业") or row.get("sector") or "").strip() or None
            )
            records.append(
                SecurityRecord(
                    symbol=code,
                    name=name,
                    exchange="BJ",
                    board="bj",
                    listing_date=ipo_date,
                    trading_status="active",
                    sector=sector,
                    is_st=_is_st_name(name),
                    as_of_date=as_of_date,
                )
            )
        return records


# ───────────── 更新 build_provider_by_name ─────────────


def build_provider_by_name(name: str, **kwargs) -> UniverseProvider:  # noqa: F811
    """根据 provider 名称构造实例，未知名称 → ProviderError。

    这是 SyncService 启动时校验的入口，防止拼错 provider 名字后静默退回 mock。

    支持：mock / akshare / baostock（+ ak_bj_supplement 注入 BJ 子源）
    """
    n = (name or "").strip().lower()
    if n == "mock":
        return MockUniverseProvider()
    if n == "akshare":
        return AkshareUniverseProvider(**kwargs)
    if n == "baostock":
        return BaoStockUniverseProvider(**kwargs)
    raise ProviderError(
        "factory",
        f"未知的 UNIVERSE_PROVIDER: {name!r}（仅支持 mock / akshare / baostock）",
    )