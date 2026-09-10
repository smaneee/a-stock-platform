"""全市场证券主数据 Provider 接口。

实现：
- MockUniverseProvider：固定种子 + 预定义样本（默认，CI 友好）
- AkshareUniverseProvider：AKShare 真实数据（生产环境）

所有 Provider 必须遵守：
1) 返回标准 SecurityRecord 列表（与 ORM 字段对应）
2) 失败抛 ProviderError，便于 SyncService 区分成功/失败
3) Provider 自身不带重试，重试由 SyncService 统一管理
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Iterable

from pydantic import BaseModel, Field

from app.time_utils import utc_now


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
        """


class MockUniverseProvider(UniverseProvider):
    """用于 CI / 离线测试的固定种子样本数据。

    数据预设：A 股 + 深 A + 创业板 + 北证 + ST + 已退市，共 26 条
    跨越 SH/SZ/BJ 三个交易所，覆盖各种排除场景。
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


class AkshareUniverseProvider(UniverseProvider):
    """AKShare 数据源（生产）。

    注：CI 不依赖 AKShare 网络可达；managed e2e 默认不启用此 provider。
    网络失败 → ProviderError("akshare", "...") 让 SyncService 退到 mock。
    """

    source_id = "akshare"

    async def fetch_all(self) -> list[SecurityRecord]:
        # 网络调用在生产由真实 AKShare 实现；这里故意抛错表示离线不可用
        # 真实接入是在 SyncService 选真实 provider 时才走这条路径
        raise ProviderError(
            self.source_id,
            "AKShare provider 是 placeholder，需要在生产环境真正实现",
        )
