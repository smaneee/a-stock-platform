"""模拟账户日终结算。

每日收盘后执行：T+1 解冻、记录当日资产快照、计算当日盈亏基准。
结算过程对每个账户独立提交，单个账户失败不影响其他账户。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import AssetRecord, PaperAccount, PaperPosition
from app.market_data.base import QuoteData

logger = logging.getLogger(__name__)


class DailySettlement:
    """日终结算服务。"""

    def __init__(self, db: Session):
        self._db = db

    def settle_account(
        self,
        account: PaperAccount,
        quotes: dict[str, QuoteData],
    ) -> dict:
        """结算单个账户：T+1 解冻 + 记录资产快照。返回结算摘要。"""
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()

        # T+1 解冻
        for position in positions:
            position.available_quantity = position.quantity

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

        record = AssetRecord(account_id=account.id, total_asset=total_asset)
        self._db.add(record)
        self._db.commit()

        return {
            "account_id": account.id,
            "total_asset": total_asset,
            "frozen_cash": float(account.frozen_cash),
            "positions_settled": len(positions),
        }

    def settle_all(self, quotes: dict[str, QuoteData]) -> list[dict]:
        """结算所有账户，返回每个账户的结算摘要。"""
        accounts = self._db.scalars(select(PaperAccount)).all()
        results: list[dict] = []
        for account in accounts:
            try:
                results.append(self.settle_account(account, quotes))
            except Exception as exc:  # noqa: BLE001
                self._db.rollback()
                logger.error("账户 %s 结算失败: %s", account.id, exc)
        return results
