"""Reviewable selection-to-paper-trading rebalance workflow."""
from __future__ import annotations

from datetime import timedelta
from math import floor

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    PaperAccount,
    PaperPosition,
    PaperRebalancePlan,
    SelectionRun,
)
from app.market_data.base import QuoteData
from app.paper_trading.broker import PaperBroker
from app.paper_trading.portfolio import PortfolioService
from app.selection import SelectionEvaluationService
from app.time_utils import utc_now


class RebalanceError(ValueError):
    """A safe rebalance plan cannot be created or executed."""


class PaperRebalanceService:
    MIN_VALIDATED_RUNS = 3
    PLAN_TTL = timedelta(minutes=15)

    def __init__(self, db: Session):
        self._db = db

    def required_symbols(self, account_id: int, selection_run_id: int) -> list[str]:
        run = self._db.get(SelectionRun, selection_run_id)
        if run is None:
            raise RebalanceError("选股运行不存在")
        if self._db.get(PaperAccount, account_id) is None:
            raise RebalanceError("模拟账户不存在")
        held = self._db.scalars(
            select(PaperPosition.symbol).where(PaperPosition.account_id == account_id)
        ).all()
        return sorted(set(held) | {item.symbol for item in run.candidates})

    def create_plan(
        self,
        account_id: int,
        selection_run_id: int,
        quotes: dict[str, QuoteData],
        *,
        target_investment_ratio: float = 0.8,
        max_symbol_weight: float = 0.2,
        validation_override: bool = False,
    ) -> PaperRebalancePlan:
        if not 0.1 <= target_investment_ratio <= 0.8:
            raise RebalanceError("目标总仓位必须在 10%..80%")
        if not 0.05 <= max_symbol_weight <= 0.2:
            raise RebalanceError("单股权重上限必须在 5%..20%")
        account = self._db.get(PaperAccount, account_id)
        run = self._db.get(SelectionRun, selection_run_id)
        if account is None or run is None:
            raise RebalanceError("模拟账户或选股运行不存在")
        if not run.candidates:
            raise RebalanceError("选股运行没有候选股票")
        summary = SelectionEvaluationService(self._db).summarize(
            strategy_config=run.config_json
        )
        validation_passed = (
            summary.evaluated_runs >= self.MIN_VALIDATED_RUNS
            and summary.mean_forward_return > 0
            and summary.average_rank_ic is not None
            and summary.average_rank_ic > 0
        )
        if not validation_passed and not validation_override:
            raise RebalanceError(
                "历史验证未达标：至少需要 3 个批次，且平均收益和 RankIC 均为正"
            )
        symbols = self.required_symbols(account_id, selection_run_id)
        missing = [
            symbol
            for symbol in symbols
            if symbol not in quotes or quotes[symbol].is_stale or quotes[symbol].price <= 0
        ]
        if missing:
            raise RebalanceError(f"缺少有效实时行情: {', '.join(missing[:5])}")

        snapshot = PortfolioService(self._db).calculate_snapshot(account, quotes)
        candidates = list(run.candidates)
        equal_weight = min(target_investment_ratio / len(candidates), max_symbol_weight)
        target_value = snapshot.total_asset * equal_weight
        current_quantity: dict[str, int] = {}
        positions = self._db.scalars(
            select(PaperPosition).where(PaperPosition.account_id == account_id)
        ).all()
        for position in positions:
            current_quantity[position.symbol] = (
                current_quantity.get(position.symbol, 0) + position.quantity
            )
        target_symbols = {item.symbol for item in candidates}
        orders: list[dict] = []
        for symbol, quantity in sorted(current_quantity.items()):
            if symbol not in target_symbols and quantity > 0:
                sell_quantity = floor(quantity / 100) * 100
                if sell_quantity:
                    orders.append(self._order("SELL", symbol, sell_quantity, quotes[symbol]))
        for candidate in candidates:
            quote = quotes[candidate.symbol]
            # 盘口缺失（东财/AKShare 无五档）时按最新价估算可买数量
            reference = quote.execution_price("BUY")
            desired = floor(target_value / reference / 100) * 100 if reference > 0 else 0
            difference = desired - current_quantity.get(candidate.symbol, 0)
            if difference >= 100:
                orders.append(self._order("BUY", candidate.symbol, floor(difference / 100) * 100, quote))
            elif difference <= -100:
                orders.append(self._order("SELL", candidate.symbol, floor(-difference / 100) * 100, quote))
        orders.sort(key=lambda item: (item["side"] != "SELL", item["symbol"]))
        payload = {
            "selection_trading_day": run.trading_day.isoformat(),
            "total_asset": snapshot.total_asset,
            "validation": {
                "passed": validation_passed,
                "override": validation_override,
                "evaluated_runs": summary.evaluated_runs,
                "mean_forward_return": summary.mean_forward_return,
                "average_rank_ic": summary.average_rank_ic,
            },
            "orders": orders,
        }
        existing = self._db.scalar(
            select(PaperRebalancePlan).where(
                PaperRebalancePlan.account_id == account_id,
                PaperRebalancePlan.selection_run_id == selection_run_id,
            )
        )
        if existing is not None:
            return existing
        plan = PaperRebalancePlan(
            account_id=account_id,
            selection_run_id=selection_run_id,
            status="DRAFT",
            target_investment_ratio=target_investment_ratio,
            max_symbol_weight=max_symbol_weight,
            validation_override=validation_override,
            proposal_json=payload,
        )
        self._db.add(plan)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            winner = self._db.scalar(
                select(PaperRebalancePlan).where(
                    PaperRebalancePlan.account_id == account_id,
                    PaperRebalancePlan.selection_run_id == selection_run_id,
                )
            )
            if winner is None:
                raise
            return winner
        self._db.refresh(plan)
        return plan

    def execute_plan(
        self, plan_id: int, quotes: dict[str, QuoteData]
    ) -> PaperRebalancePlan:
        plan = self._db.get(PaperRebalancePlan, plan_id)
        if plan is None:
            raise RebalanceError("调仓方案不存在")
        if plan.status != "DRAFT":
            raise RebalanceError(f"方案状态 {plan.status} 不可执行")
        if utc_now() - plan.created_at > self.PLAN_TTL:
            plan.status = "EXPIRED"
            self._db.commit()
            raise RebalanceError("调仓方案已超过 15 分钟，请重新生成")
        orders = plan.proposal_json.get("orders", [])
        missing = [item["symbol"] for item in orders if item["symbol"] not in quotes]
        if missing:
            raise RebalanceError("执行时缺少实时行情")
        results = []
        broker = PaperBroker(self._db)
        for item in orders:
            signal_id = f"rebalance:{plan.id}:{item['side']}:{item['symbol']}"
            order, error = broker.place_order(
                account_id=plan.account_id,
                symbol=item["symbol"],
                side=item["side"],
                quantity=item["quantity"],
                price=quotes[item["symbol"]].price,
                quote=quotes[item["symbol"]],
                signal_id=signal_id,
            )
            results.append(
                {
                    "symbol": item["symbol"],
                    "side": item["side"],
                    "quantity": item["quantity"],
                    "status": order.status if order else "REJECTED",
                    "error": error or None,
                }
            )
        plan = self._db.get(PaperRebalancePlan, plan_id)
        succeeded = sum(item["status"] == "FILLED" for item in results)
        plan.status = "EXECUTED" if succeeded == len(results) else "PARTIAL"
        plan.execution_json = {"orders": results, "filled": succeeded, "total": len(results)}
        plan.executed_at = utc_now()
        self._db.commit()
        self._db.refresh(plan)
        return plan

    def cancel_plan(self, plan_id: int) -> PaperRebalancePlan:
        plan = self._db.get(PaperRebalancePlan, plan_id)
        if plan is None:
            raise RebalanceError("调仓方案不存在")
        if plan.status != "DRAFT":
            raise RebalanceError(f"方案状态 {plan.status} 不可取消")
        plan.status = "CANCELLED"
        self._db.commit()
        return plan

    @staticmethod
    def _order(side: str, symbol: str, quantity: int, quote: QuoteData) -> dict:
        price = quote.execution_price(side)
        return {
            "side": side,
            "symbol": symbol,
            "quantity": quantity,
            "indicative_price": price,
            "indicative_value": round(price * quantity, 2),
        }
