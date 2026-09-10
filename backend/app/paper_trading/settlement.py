"""模拟账户日终结算。

每日收盘后执行：T+1 解冻、记录当日资产快照、计算当日盈亏基准。
结算过程对每个账户独立提交，单个账户失败不影响其他账户。

幂等性：同一 (account_id, trading_date) 多次结算只生效一次（落库前先
检查 DailySettlementRecord 是否已存在）；返回首次结算的快照保证可重入。
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    AssetRecord,
    DailySettlementRecord,
    PaperAccount,
    PaperPosition,
)
from app.market_data.base import QuoteData
from app.market_rules.calendar import TradingCalendar

logger = logging.getLogger(__name__)

# A 股收盘时间（北京时间）
_CN_TZ = ZoneInfo("Asia/Shanghai")
_CLOSE_HOUR_LOCAL = 15
_CLOSE_MINUTE_LOCAL = 30


def now_in_cn(now_utc=None) -> date:
    """返回当前北京时间所在自然日。测试时可注入 utc 时间。"""
    from datetime import datetime, timezone

    base = now_utc or datetime.now(timezone.utc)
    return base.astimezone(_CN_TZ).date()


def is_market_closed_now(now_utc=None) -> bool:
    """判断当前北京时间是否已过 15:30 收盘。"""
    from datetime import datetime, time, timezone

    base = now_utc or datetime.now(timezone.utc)
    cn_now = base.astimezone(_CN_TZ)
    return cn_now.time() >= time(_CLOSE_HOUR_LOCAL, _CLOSE_MINUTE_LOCAL)


class DailySettlement:
    """日终结算服务。"""

    def __init__(self, db: Session):
        self._db = db

    def settle_account(
        self,
        account: PaperAccount,
        quotes: dict[str, QuoteData],
        trading_date: date | None = None,
        force: bool = False,
    ) -> dict:
        """结算单个账户：T+1 解冻 + 记录资产快照。

        - trading_date: 交易日；缺省使用交易日历上一个已收盘的交易日。
        - force: True 时强制重算（无视幂等记录）。
        """
        td = trading_date or self._default_trading_date(account)
        # 幂等：同一 (account_id, trading_date) 已结算过则直接返回
        if not force:
            existing = self._db.scalars(
                select(DailySettlementRecord).where(
                    DailySettlementRecord.account_id == account.id,
                    DailySettlementRecord.trading_date == td,
                )
            ).first()
            if existing is not None:
                return {
                    "account_id": account.id,
                    "trading_date": td.isoformat(),
                    "total_asset": float(existing.total_asset),
                    "frozen_cash": float(account.frozen_cash),
                    "positions_settled": existing.positions_settled,
                    "idempotent": True,
                }

        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()

        # T+1 解冻：所有批次 available_quantity 恢复为 quantity
        positions_settled = 0
        for position in positions:
            if position.available_quantity != position.quantity:
                position.available_quantity = position.quantity
                positions_settled += 1

        # 计算总资产（现金 + 冻结 + 持仓市值）
        position_value = 0.0
        for position in positions:
            quote = quotes.get(position.symbol)
            price = quote.price if quote else float(position.avg_cost)
            position_value += float(position.quantity) * price

        total_asset = (
            float(account.available_cash)
            + float(account.frozen_cash)
            + position_value
        )

        # 写入幂等结算记录（如已存在则更新 total_asset / positions_settled）
        record = self._db.scalars(
            select(DailySettlementRecord).where(
                DailySettlementRecord.account_id == account.id,
                DailySettlementRecord.trading_date == td,
            )
        ).first()
        if record is None:
            record = DailySettlementRecord(
                account_id=account.id,
                trading_date=td,
                total_asset=Decimal(str(total_asset)),
                positions_settled=positions_settled,
            )
            self._db.add(record)
        else:
            record.total_asset = Decimal(str(total_asset))
            record.positions_settled = positions_settled

        record_asset = AssetRecord(
            account_id=account.id, total_asset=Decimal(str(total_asset))
        )
        self._db.add(record_asset)
        self._db.commit()

        return {
            "account_id": account.id,
            "trading_date": td.isoformat(),
            "total_asset": total_asset,
            "frozen_cash": float(account.frozen_cash),
            "positions_settled": positions_settled,
            "idempotent": False,
        }

    def settle_all(
        self,
        quotes: dict[str, QuoteData],
        trading_date: date | None = None,
    ) -> list[dict]:
        """结算所有账户，返回每个账户的结算摘要。"""
        accounts = self._db.scalars(select(PaperAccount)).all()
        results: list[dict] = []
        for account in accounts:
            try:
                results.append(
                    self.settle_account(account, quotes, trading_date=trading_date)
                )
            except Exception as exc:  # noqa: BLE001
                self._db.rollback()
                logger.error("账户 %s 结算失败: %s", account.id, exc)
        return results

    def _default_trading_date(self, account: PaperAccount) -> date:
        """自动选择交易日：当前北京时间已过 15:30 → 当日；否则 → 上一交易日。"""
        from datetime import datetime, timezone

        utc_now = datetime.now(timezone.utc)
        cn_today = now_in_cn(utc_now)
        calendar = TradingCalendar(self._db)
        if is_market_closed_now(utc_now):
            # 已收盘：当日若为交易日则用当日，否则用上一个交易日
            if calendar.is_trading_day(cn_today):
                return cn_today
            try:
                return calendar.prev_trading_day(cn_today)
            except ValueError:
                return cn_today
        # 未收盘：用上一个交易日（结算上一日）
        try:
            return calendar.prev_trading_day(cn_today)
        except ValueError:
            return cn_today
