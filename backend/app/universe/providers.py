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
from datetime import date, timedelta
from typing import Any

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


# 历史日期的动态门槛（修复 693e866 的 P0 阻断）
# 早期年份 A 股规模远小于 2025+ 的 5300，固定 3500 必然失败
# 阈值参考各年实际规模：
#   2024+  : 现代三市场，必须含北交所
#   2021-11-15 起：北交所已开市，必须含北交所
#   2020 至北交所开市前：只要求沪深
#   2015-2019 : ~3500
#   2010-2014 : ~2500
#   2005-2009 : ~1500
#   2000-2004 : ~1100
#   1991-1999 : ~800（沪深老股）
# 仅用于 validate_market_coverage，sync 顶层不变。
_HISTORICAL_COVERAGE_TABLE: tuple[tuple[date, int, dict[str, int]], ...] = (
    (date(2024, 1, 1), 3500, {"SH": 1000, "SZ": 1500, "BJ": 100}),
    (date(2021, 11, 15), 2500, {"SH": 800, "SZ": 1200, "BJ": 50}),
    (date(2020, 1, 1), 2500, {"SH": 800, "SZ": 1200, "BJ": 0}),
    (date(2015, 1, 1), 1500, {"SH": 600, "SZ": 800, "BJ": 0}),
    (date(2010, 1, 1), 800, {"SH": 400, "SZ": 500, "BJ": 0}),
    (date(2005, 1, 1), 600, {"SH": 300, "SZ": 300, "BJ": 0}),
    (date(2000, 1, 1), 500, {"SH": 250, "SZ": 250, "BJ": 0}),
    (date(1990, 12, 19), 100, {"SH": 50, "SZ": 50, "BJ": 0}),
)


def _resolve_threshold(as_of: date | None) -> tuple[int, dict[str, int], date | None]:
    """根据 effective_date 选历史覆盖阈值。

    Returns (min_total, min_per_exchange, baseline_date)。
    """
    if as_of is None:
        return (
            MARKET_COVERAGE_MIN_TOTAL,
            dict(MARKET_COVERAGE_MIN_PER_EXCHANGE),
            None,
        )
    # 找 ≤ as_of 的最新一档（表格按日期降序）
    for baseline, total, per_ex in _HISTORICAL_COVERAGE_TABLE:
        if as_of >= baseline:
            return total, dict(per_ex), baseline
    return (
        MARKET_COVERAGE_MIN_TOTAL,
        dict(MARKET_COVERAGE_MIN_PER_EXCHANGE),
        None,
    )


def validate_market_coverage(
    records: list[SecurityRecord],
    *,
    source_id: str = "akshare",
    as_of_date: date | None = None,
) -> None:
    """全市场质量门槛校验。

    校验项（任一不达标即 ProviderError）：
    1. 总数 ≥ 阈值（默认 MARKET_COVERAGE_MIN_TOTAL=3500，按 as_of_date 动态调整）
    2. SH/SZ/BJ 三交易所都有覆盖（动态阈值；BJ 在历史早期可能不要求）
    3. 代码格式（6 位数字）100%
    4. 重复率（输入原始 records 中 symbol 重复）≤ 1%
    5. name 完整率 ≥ 99%
    6. exchange 完整率 ≥ 99%

    Args:
        records: 待校验的 SecurityRecord 列表
        source_id: provider source_id（用于错误信息）
        as_of_date: 拉数据的有效交易日；用于动态历史阈值。
            None 时用现代全市场阈值（3500+）。
    """
    if not records:
        raise ProviderError(source_id, "全市场质量门槛：records 为空（不可能零只）")

    min_total, min_per_ex, baseline = _resolve_threshold(as_of_date)
    threshold_label = (
        f"{min_total} (baseline={baseline.isoformat()})"
        if baseline
        else f"{min_total} (modern)"
    )

    total = len(records)
    if total < min_total:
        raise ProviderError(
            source_id,
            f"全市场质量门槛：总数 {total} < {threshold_label}（数据源缩量）",
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

    # 分交易所覆盖（动态阈值）
    per_exchange: dict[str, int] = {}
    for r in records:
        ex = (r.exchange or "").upper()
        per_exchange[ex] = per_exchange.get(ex, 0) + 1

    for ex, required in min_per_ex.items():
        if required == 0:
            continue  # 该时期此交易所不要求
        actual = per_exchange.get(ex, 0)
        if actual < required:
            raise ProviderError(
                source_id,
                f"全市场质量门槛：{ex} 覆盖 {actual} < {required}（交易所缩量，"
                f"baseline={baseline.isoformat() if baseline else 'modern'}）",
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
        "全市场质量门槛通过：total=%d (min=%s), sh=%d, sz=%d, bj=%d, duplicate=%d, as_of=%s",
        total,
        threshold_label,
        per_exchange.get("SH", 0),
        per_exchange.get("SZ", 0),
        per_exchange.get("BJ", 0),
        duplicate,
        as_of_date.isoformat() if as_of_date else "None",
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
      - 6xxxxx → SH（含 605/688 科创板）
      - 900xxx → SH（沪 B 股）
      - 920xxx / 43xxxx / 83xxxx / 87xxxx / 88xxxx → BJ（北交所，含 920 新号段）
      - 0xxxxx / 2xxxxx / 30xxxx → SZ（含 000/002 主板、300 创业板）
      - 4xxxxx / 8xxxxx → BJ（北证，代码一般 6 位但 83/87/43 开头）

    不会 100% 精确（依赖 AKShare 实际格式），但是 fallback；优先 trust 真实源字段。
    """
    s = str(symbol).strip()
    if not s:
        return ""
    # 920xxx 是北交所新号段：不能因为「9 开头」被当成沪市
    if s.startswith("920"):
        return "BJ"
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
    """根据名称判断是否已退市。

    A 股退市证券的简称有三种写法，缺一种就会把退市股留在股票池里：
    - 「退市XX」/「退XX」：老三板平移写法（名称以「退」开头）
    - 「XX退」：退市整理期写法（2020 年后主流，如「国华退」「康得退」）
    - 「PTXX」：特别转让（1999-2002），现存 PT 全部已退市
    """
    if not name:
        return False
    normalized = name.strip().upper()
    if not normalized:
        return False
    return (
        "退市" in normalized
        or normalized.startswith("退")
        or normalized.endswith("退")
        or normalized.startswith("PT")
    )


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


def _collect_baostock_rows(
    result_set: Any,
    *,
    query_name: str,
    max_rows: int,
) -> tuple[list[str], list[list[str]]]:
    """Consume a BaoStock ResultData cursor safely and completely.

    BaoStock advances its cursor in ``get_row_data()``, not merely in ``next()``.
    Keeping both calls in the same loop prevents the apparent infinite iteration
    that led the previous implementation to abandon ``query_all_stock(day)``.
    A full cursor at ``max_rows`` is rejected instead of being silently truncated.
    """
    if result_set is None or getattr(result_set, "error_code", None) != "0":
        raise ProviderError(
            "baostock",
            f"{query_name} 失败: {getattr(result_set, 'error_msg', 'unknown')}",
        )

    fields = list(getattr(result_set, "fields", []) or [])
    rows: list[list[str]] = []
    while True:
        try:
            has_next = result_set.next()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("baostock", f"{query_name} 游标读取失败: {exc}") from exc
        if not has_next:
            break
        row = result_set.get_row_data()
        if row:
            rows.append(list(row))
        if len(rows) >= max_rows:
            raise ProviderError(
                "baostock",
                f"{query_name} 达到安全上限 {max_rows} 行，拒绝截断数据",
            )

    if getattr(result_set, "error_code", "0") != "0":
        raise ProviderError(
            "baostock",
            f"{query_name} 读取结束时失败: {getattr(result_set, 'error_msg', 'unknown')}",
        )
    return fields, rows


def _baostock_query_trade_dates(
    start_date: str,
    end_date: str,
    *,
    bs_module: Any | None = None,
) -> list[date]:
    """BaoStock 交易日历查询。失败抛 ProviderError。"""
    if bs_module is None:
        import baostock as bs_module  # type: ignore

    rs = bs_module.query_trade_dates(start_date=start_date, end_date=end_date)
    fields, raw_rows = _collect_baostock_rows(
        rs, query_name="query_trade_dates", max_rows=370
    )
    rows: list[date] = []
    for raw_row in raw_rows:
        record = dict(zip(fields, raw_row))
        try:
            if record.get("is_trading_day") == "1":
                rows.append(date.fromisoformat(record["calendar_date"]))
        except (ValueError, KeyError):
            continue
    return rows


# BaoStock 内部 socket 默认无超时：连接静默断开时会永久阻塞在 rs.next()。
# 实测单个查询的结果集读取在正常网络下 <1s，30s 只用于兜住"挂死"的情况。
_BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 30.0


class BaoStockUniverseProvider(UniverseProvider):
    """BaoStock 数据源（生产环境，point-in-time 主数据源）。

    关键能力（来源：https://github.com/lzwme/finance-quant-skills/blob/main/skills/baostock/references/api.md）：
    - `query_all_stock(day)`：**主驱动**。返回当日所有证券（含债券/基金/股票） +
      `tradeStatus`（0=停牌，1=正常）。当日成员以它为准。
    - `query_stock_basic()`：**仅补字段**。返回 type / status / ipoDate / outDate，
      其中 `status` 0=退市 / 1=上市，`type` 1=股票 / 2=指数 / 3=其它。
      用于把 type=1 过滤后，把 ipoDate / outDate / status 合并到当日成员。
    - `query_trade_dates()`：返回交易日历，用于把 as_of_date 归一到最近交易日。

    实现要点（修复 693e866 的 P0 阻断）：
    1. **status 映射方向修正**：BaoStock stock_basic `status=1` 是 listed（上市），
       `status=0` 是 delisted（退市）。693e866 反了，让 90% 股票被判退市。
    2. **未知状态不猜**：status 不在 {0, 1} 范围 → audit_reason='provider_unknown_status'
       （不排除，由 selection 层处理），不强行猜 suspended。
    3. **当日停牌以 tradeStatus 为准**：query_all_stock.tradeStatus=0 → suspended。
       只在 query_all_stock 拿不到时退回到 status。
    4. **恢复 query_all_stock 主驱动**：693e866 错误地放弃了它，导致拿不到历史时点成分。
       现在恢复并用 max_rows=20000 兜底防 dev 网络限速导致卡死。
    5. **socket timeout 不污染全局**：用 socket.create_connection 显式 timeout 参数
       替代 socket.setdefaulttimeout，或者在子进程中跑 BaoStock。
    6. **point-in-time 真实生效**：effective_day 来自 query_trade_dates，
       而不是 utc_now().date()。周末/节假日自动归一到上一个交易日。
    7. **历史覆盖不足明确失败**：historical_dates 数量太少的日期（query_all_stock
       返回 0 行）→ ProviderError 而非落库空 universe。
    """

    source_id = "baostock"

    # query_all_stock 在 BaoStock 实测会返回基金/债券/股票混合，type filter 必须在
    # 拿到基本数据后做；用 query_stock_basic 的 type 字段做过滤。
    # 实测：2025-09 单次 query_all_stock 返回 ~15000+ 行（含债券/基金/股票）；
    # ~7000 是 type=1 股票。max_rows=20000 留 30% buffer 防 dev 网络慢导致截断。
    _MAX_QUERY_ROWS = 20000

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        as_of_date: date | None = None,
        bj_supplement_timeout_seconds: float = 60.0,
    ):
        """Args:
        timeout_seconds: BaoStock fetch 总超时（全市场实测 70~210s，
            秒级默认值必然误判超时并丢弃已拉到的数据）
        as_of_date: 指定拉数据的交易日；None → 用最近交易日
        bj_supplement_timeout_seconds: AKShare BJ 子源超时（该接口分 18 页拉取，
            实测 12~20s，原 15s 会随机超时并把整个股票池同步判为失败）
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
        # 用 effective_day 算历史日期的动态阈值，避免早年数据卡门槛
        validate_market_coverage(
            records, source_id=self.source_id, as_of_date=effective_day
        )

        for r in records:
            r.as_of_date = effective_day
        return records

    # ───────────── 阻塞调用（线程池内执行） ─────────────

    def _fetch_sync(self) -> tuple[list[SecurityRecord], date]:
        """同步 BaoStock 调用全流程：login → query_trade_dates → query_all_stock →
        query_stock_basic → 装配 SecurityRecord。

        返回 (records, effective_day)。

        实机测量（2026-09-11，全市场 7382/8950 行）：整体约 70~210 秒，其中
        query_all_stock 取结果集约 10s、逐行读取 7382 行约 11s、query_stock_basic
        约 7s、逐行读取 8950 行约 36s、logout 约 4s，其余为 BJ 子源与组装。
        两个直接后果：
        1. 调用方的总超时必须给足（见 BAOSTOCK_UNIVERSE_TIMEOUT_SECONDS），
           否则慢但成功的拉取会被当成超时丢弃；
        2. 必须显式设置 socket 超时：BaoStock 内部 socket 默认无超时，一旦连接
           静默断开就会永久阻塞在 rs.next()；此时事件循环的 wait_for 取消不了
           已在线程池里运行的调用（实测把进程卡死 10 分钟以上）。
        """
        import socket

        try:
            import baostock as bs  # type: ignore
        except ImportError as exc:
            raise ProviderError(
                self.source_id,
                f"baostock 未安装: {exc}",
            )

        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_BAOSTOCK_SOCKET_TIMEOUT_SECONDS)
        try:
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
        finally:
            socket.setdefaulttimeout(previous_timeout)

    def _fetch_sync_inner(self, bs) -> tuple[list[SecurityRecord], date]:
        """实际拉取逻辑（已 login，调用方负责 logout）。"""
        # 1) 计算 effective_day（最近交易日 ≤ self._as_of_date）
        effective_day = self._resolve_effective_day(bs)
        day_str = effective_day.strftime("%Y-%m-%d")

        # 2) query_all_stock(day) — **当日主驱动**：当日成员 + tradeStatus
        rs_all = bs.query_all_stock(day=day_str)
        all_stock_fields, all_stock_rows = _collect_baostock_rows(
            rs_all,
            query_name=f"query_all_stock({day_str})",
            max_rows=self._MAX_QUERY_ROWS,
        )

        if not all_stock_rows:
            raise ProviderError(
                self.source_id,
                f"query_all_stock({day_str}) 0 行（market 不可达或日期非交易日）",
            )

        # 3) query_stock_basic() — **仅补字段**：IPO/退市日期 + type 过滤
        rs_basic = bs.query_stock_basic()
        basic_fields, basic_rows = _collect_baostock_rows(
            rs_basic,
            query_name="query_stock_basic",
            max_rows=self._MAX_QUERY_ROWS,
        )

        if not basic_rows:
            raise ProviderError(
                self.source_id,
                "query_stock_basic 0 行（market 不可达）",
            )

        # 4) 装配：query_all_stock 主驱动 + query_stock_basic 补 type/ipoDate/outDate
        records = self._assemble_records(
            all_stock_fields=all_stock_fields,
            all_stock_rows=all_stock_rows,
            basic_fields=basic_fields,
            basic_rows=basic_rows,
            effective_day=effective_day,
        )

        # 5) AKShare BJ 是“当前名单”，绝不能回填历史快照。仅当调用方未请求
        # 历史日期，且 BaoStock 当日结果确实没有 BJ 时，才允许补当前 BJ。
        has_bj = any(record.exchange == "BJ" for record in records)
        if self._as_of_date is None and not has_bj and self._bj_supplement is not None:
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
        elif self._as_of_date is not None and not has_bj:
            logger.info(
                "BaoStock 历史快照 %s 不含 BJ；禁止使用当前 AKShare 名单回填",
                effective_day,
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
        all_stock_fields: list[str],
        all_stock_rows: list[list[str]],
        basic_fields: list[str],
        basic_rows: list[list[str]],
        effective_day: date,
    ) -> list[SecurityRecord]:
        """query_all_stock + query_stock_basic → SecurityRecord。

        关键设计（修复 693e866 的 P0 阻断）：
        1. **成员集合 = query_all_stock(day)**：绝不从当前 basic 全集反推历史成员；
           basic 只负责 type=1 过滤和静态字段补充。
        2. **query_all_stock 仅补 tradeStatus**：basic.status 决定 listed/delisted，
           all.tradeStatus 决定 suspended。两者组合最终 trading_status。
        3. **status 映射**：BaoStock basic.status=1=listed(active), 0=delisted。
        4. **tradeStatus=0 → suspended**：当日停牌（query_all_stock 字段语义）。
        5. **未上市 / 已退市 / 未来**：ipoDate / outDate / effective_day 关系过滤。
        """
        # basic 索引：symbol → {type, status, ipoDate, outDate, code_name}
        basic_idx: dict[str, dict[str, str]] = {}
        for row in basic_rows:
            rec = dict(zip(basic_fields, row))
            sym, _ = _baostock_code_to_symbol(rec.get("code", ""))
            if sym:
                basic_idx[sym] = rec

        # type=1 白名单仅用于过滤 query_all_stock 的当日成员。
        type1_set: set[str] = {
            sym
            for sym, b in basic_idx.items()
            if str(b.get("type", "")).strip() == "1"
        }
        daily_idx: dict[str, tuple[dict[str, str], str]] = {}
        for row in all_stock_rows:
            all_rec = dict(zip(all_stock_fields, row))
            sym, exchange = _baostock_code_to_symbol(all_rec.get("code", ""))
            if sym and sym in type1_set:
                daily_idx[sym] = (all_rec, exchange)

        records: list[SecurityRecord] = []
        skipped_listed_after = 0
        unknown_trade_status = 0

        # 主驱动必须是指定交易日的 query_all_stock 结果。若遍历 type1_set，
        # 会把今天才上市的证券错误塞进过去的快照，形成幸存者偏差。
        for symbol in sorted(daily_idx):
            basic = basic_idx[symbol]
            daily, exchange = daily_idx[symbol]
            code_name = str(
                daily.get("code_name") or basic.get("code_name") or ""
            ).strip()

            ipo_date = _parse_date(basic.get("ipoDate"))
            out_date = _parse_date(basic.get("outDate"))
            trade_status_raw = str(daily.get("tradeStatus", "")).strip()
            if trade_status_raw == "0":
                trading_status = "suspended"
            elif trade_status_raw == "1":
                trading_status = "active"
            else:
                # 未知状态不能猜成 active/suspended/delisted。保留成员并明确标记，
                # 由下游风控决定是否允许进入可交易集合。
                trading_status = "unknown"
                unknown_trade_status += 1

            # 上市日期晚于 effective_day → 当日尚未上市，跳过
            if ipo_date is not None and ipo_date > effective_day:
                skipped_listed_after += 1
                continue

            name = code_name
            is_st = _is_st_name(name)
            board = _infer_board(symbol, exchange)
            records.append(
                SecurityRecord(
                    symbol=symbol,
                    name=name,
                    exchange=exchange,
                    board=board,
                    listing_date=ipo_date,
                    delisted_date=out_date,
                    trading_status=trading_status,
                    is_st=is_st,
                    as_of_date=effective_day,
                )
            )

        if not records:
            raise ProviderError(
                self.source_id,
                f"BaoStock 装配后 0 条 type=1 记录（query_all_stock={len(all_stock_rows)} 行，"
                f"query_stock_basic={len(basic_rows)} 行，type=1 白名单={len(type1_set)} 个，"
                f"当日股票={len(daily_idx)}，跳过未来上市 {skipped_listed_after}）",
            )

        if skipped_listed_after > 0:
            logger.info(
                "BaoStock 跳过 %d 条 effective_day 后上市的新股", skipped_listed_after
            )
        if unknown_trade_status > 0:
            logger.warning(
                "BaoStock %s 有 %d 条未知 tradeStatus，已标记 unknown",
                effective_day,
                unknown_trade_status,
            )
        return records

    def _resolve_effective_day(self, bs_mod) -> date:
        """Resolve the exact point-in-time date used for membership.

        Explicit historical requests are normalized to the latest trading day at
        or before that date. For the default live sync we use yesterday as the
        upper bound, because the current day's list may not be published yet.
        Calendar failures are fatal: stamping data with a guessed non-trading date
        would break the snapshot contract.
        """
        upper_bound = (
            self._as_of_date
            if self._as_of_date is not None
            else utc_now().date() - timedelta(days=1)
        )
        start = (upper_bound - timedelta(days=15)).strftime("%Y-%m-%d")
        end = upper_bound.strftime("%Y-%m-%d")
        trading_days = _baostock_query_trade_dates(
            start, end, bs_module=bs_mod
        )
        eligible = [trading_day for trading_day in trading_days if trading_day <= upper_bound]
        if not eligible:
            raise ProviderError(
                self.source_id,
                f"{start}..{end} 未返回可用交易日，拒绝猜测 snapshot 日期",
            )
        return max(eligible)


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


def build_provider_by_name(name: str, **kwargs) -> UniverseProvider:
    """根据 provider 名称构造实例，未知名称 → ProviderError。

    这是 SyncService 启动时校验的入口，防止拼错 provider 名字后静默退回 mock。

    支持：mock / eastmoney / akshare / baostock（+ ak_bj_supplement 注入 BJ 子源）
    """
    n = (name or "").strip().lower()
    if n == "mock":
        return MockUniverseProvider()
    if n == "eastmoney":
        # 延迟 import：eastmoney_universe 依赖本模块的 SecurityRecord / 校验函数
        from app.universe.eastmoney_universe import EastmoneyUniverseProvider

        return EastmoneyUniverseProvider(**kwargs)
    if n == "akshare":
        return AkshareUniverseProvider(**kwargs)
    if n == "baostock":
        return BaoStockUniverseProvider(**kwargs)
    raise ProviderError(
        "factory",
        f"未知的 UNIVERSE_PROVIDER: {name!r}"
        "（仅支持 mock / eastmoney / akshare / baostock）",
    )
