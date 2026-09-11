"""全市场证券主数据 Provider 接口。

实现：
- MockUniverseProvider：固定种子 + 预定义样本（默认，CI 友好）
- AkshareUniverseProvider：AKShare 真实数据（生产环境）

所有 Provider 必须遵守：
1) 返回标准 SecurityRecord 列表（与 ORM 字段对应）
2) 失败抛 ProviderError，便于 SyncService 区分成功/失败
3) Provider 自身不带重试，重试由 SyncService 统一管理
4) Provider 不准静默降级：失败就是失败，由调用方决定是否切下一个 provider
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SecurityRecord(BaseModel):
    """证券主数据（Provider 返回值，DB 写入前的中间形态）。"""

    symbol: str
    name: str = ""
    exchange: str = ""  # sh / sz / bj
    board: str = "unknown"
    is_st: bool = False
    listing_date: date | None = None
    delisted_date: date | None = None
    trading_status: str = "active"
    sector: str | None = None


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
        records: list[SecurityRecord] = []
        for row in self._SEED:
            records.append(
                SecurityRecord(
                    symbol=row["symbol"],
                    name=row["name"],
                    exchange=row.get("exchange", ""),
                    listing_date=date.fromisoformat(row["listing_date"]) if row.get("listing_date") else None,
                    delisted_date=date.fromisoformat(row["delisted_date"]) if row.get("delisted_date") else None,
                    is_st=row.get("is_st", False),
                    trading_status=row.get("trading_status", "active"),
                    sector=row.get("sector"),
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

        return self._records_from_dataframe(df)

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

    def _records_from_dataframe(self, rows: list[dict]) -> list[SecurityRecord]:
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
                    listing_date=listing_date,
                    delisted_date=delisted_date,
                    trading_status=trading_status,
                    is_st=is_st,
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