"""模拟组合管理。

计算账户总资产、持仓市值、当日盈亏、峰值资产等，
供风控与资产曲线使用。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import AssetRecord, PaperAccount, PaperPosition
from app.market_data.base import QuoteData
from app.risk.risk_manager import AccountSnapshot


class PortfolioService:
    """组合管理服务。"""

    def __init__(self, db: Session):
        self._db = db

    def get_account(self, account_id: int) -> PaperAccount | None:
        return self._db.get(PaperAccount, account_id)

    def get_position(self, account_id: int, symbol: str) -> PaperPosition | None:
        """返回指定账户+证券的聚合持仓视图（按批次汇总）。

        多批次场景下，返回值是一个内存视图对象，其 quantity / available_quantity
        为各批次之和，avg_cost 按持仓成本加权平均，acquisition_date 视为最早
        一个交易日（用于 FIFO 辅助）。不落库。
        """
        positions = self._db.scalars(
            select(PaperPosition).where(
                PaperPosition.account_id == account_id,
                PaperPosition.symbol == symbol,
            )
        ).all()
        if not positions:
            return None

        total_qty = sum(p.quantity for p in positions)
        total_available = sum(p.available_quantity for p in positions)
        if total_qty > 0:
            weighted_cost = sum(
                p.avg_cost * Decimal(p.quantity) for p in positions
            ) / Decimal(total_qty)
        else:
            weighted_cost = Decimal("0")

        # 取最早非空 acquisition_date
        dates = [p.acquisition_date for p in positions if p.acquisition_date is not None]
        earliest = min(dates) if dates else None

        # 构造一个轻量视图（不落库）
        class _AggregatedPosition:
            pass

        agg = _AggregatedPosition()
        agg.id = positions[0].id  # 取最早批次 id 用于日志/回溯
        agg.account_id = account_id
        agg.symbol = symbol
        agg.quantity = total_qty
        agg.available_quantity = total_available
        agg.avg_cost = weighted_cost
        agg.realized_pnl = sum(
            (p.realized_pnl for p in positions),
            Decimal("0"),
        )
        agg.acquisition_date = earliest
        return agg

    def unrealized_pnl(
        self, account_id: int, quotes: dict[str, QuoteData]
    ) -> float:
        """未实现盈亏 = Σ (现价 - 成本) × 数量。"""
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account_id)
        ).all()
        total = 0.0
        for pos in positions:
            quote = quotes.get(pos.symbol)
            price = quote.price if quote else float(pos.avg_cost)
            total += (price - float(pos.avg_cost)) * pos.quantity
        return total

    def calculate_snapshot(
        self,
        account: PaperAccount,
        quotes: dict[str, QuoteData],
    ) -> AccountSnapshot:
        """根据账户、持仓与最新行情计算账户快照。"""
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account.id)
        ).all()

        position_value: dict[str, float] = {}
        total_position_value = 0.0
        for pos in positions:
            quote = quotes.get(pos.symbol)
            price = quote.price if quote else float(pos.avg_cost)
            value = pos.quantity * price
            position_value[pos.symbol] = value
            total_position_value += value

        total_asset = float(account.available_cash) + float(account.frozen_cash) + total_position_value

        # 当日盈亏：用最近一条资产记录作为当日基准（简化处理）
        daily_pnl = self._calc_daily_pnl(account, total_asset)
        peak_asset = self._get_peak_asset(account.id)

        return AccountSnapshot(
            total_asset=total_asset,
            available_cash=float(account.available_cash),
            initial_cash=float(account.initial_cash),
            current_position_value=position_value,
            daily_pnl=daily_pnl,
            peak_asset=peak_asset,
        )

    def record_asset(self, account_id: int, total_asset: float) -> None:
        """记录一条资产曲线数据。"""
        record = AssetRecord(account_id=account_id, total_asset=total_asset)
        self._db.add(record)
        self._db.commit()

    def get_asset_curve(self, account_id: int, limit: int = 1000) -> list[dict]:
        """返回资产曲线。"""
        records = self._db.scalars(
            select(AssetRecord)
            .where(AssetRecord.account_id == account_id)
            .order_by(AssetRecord.recorded_at.asc())
            .limit(limit)
        ).all()
        return [
            {
                "total_asset": float(r.total_asset),
                "recorded_at": r.recorded_at.isoformat(),
            }
            for r in records
        ]

    def _calc_daily_pnl(self, account: PaperAccount, current_total_asset: float) -> float:
        """计算当日盈亏（简化：以账户建立以来的基准比较）。"""
        # 获取最早一条当日资产记录作为基准
        base = self._get_daily_base(account.id)
        if base is None:
            return 0.0
        return current_total_asset - base

    def _get_daily_base(self, account_id: int) -> float | None:
        record = self._db.scalars(
            select(AssetRecord)
            .where(AssetRecord.account_id == account_id)
            .order_by(AssetRecord.recorded_at.asc())
        ).first()
        return float(record.total_asset) if record else None

    def _get_peak_asset(self, account_id: int) -> float:
        records = self._db.scalars(
            select(AssetRecord).where(AssetRecord.account_id == account_id)
        ).all()
        if not records:
            return 0.0
        return max(float(r.total_asset) for r in records)
