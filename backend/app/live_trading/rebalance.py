"""Real-money rebalance workflow with review and one-time approval."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import date, timedelta
from math import floor

from sqlalchemy.orm import Session

from app.database.models import LiveRebalancePlan, SelectionRun
from app.live_trading.qmt_broker import (
    LiveAccountSnapshot,
    LiveOrderStatus,
    QmtBrokerTimeout,
    QmtLiveBroker,
)
from app.market_data.base import QuoteData
from app.selection import SelectionEvaluationService
from app.time_utils import utc_now


class LiveRebalanceError(ValueError):
    """A live plan failed a mandatory safety condition."""


class LiveRebalanceService:
    ACKNOWLEDGEMENT = "I_UNDERSTAND_REAL_MONEY_WILL_BE_USED"
    APPROVAL_TTL = timedelta(minutes=5)
    MAX_ACCOUNT_ASSET_DRIFT = 0.05
    MAX_SELECTION_AGE_DAYS = 10

    def __init__(
        self, db: Session, account_id: str, *, today: date | None = None
    ):
        self._db = db
        self._today = today or date.today()
        self._account_fingerprint = hashlib.sha256(
            account_id.encode("utf-8")
        ).hexdigest()[:16]

    @property
    def account_fingerprint(self) -> str:
        return self._account_fingerprint

    def required_symbols(
        self, selection_run_id: int, account: LiveAccountSnapshot
    ) -> list[str]:
        run = self._db.get(SelectionRun, selection_run_id)
        if run is None:
            raise LiveRebalanceError("选股运行不存在")
        return sorted(set(account.positions) | {item.symbol for item in run.candidates})

    def create_plan(
        self,
        selection_run_id: int,
        account: LiveAccountSnapshot,
        quotes: dict[str, QuoteData],
        *,
        target_investment_ratio: float = 0.8,
        max_symbol_weight: float = 0.2,
    ) -> LiveRebalancePlan:
        if not 0.1 <= target_investment_ratio <= 0.8:
            raise LiveRebalanceError("目标总仓位必须在 10%..80%")
        if not 0.05 <= max_symbol_weight <= 0.2:
            raise LiveRebalanceError("单股权重上限必须在 5%..20%")
        run = self._db.get(SelectionRun, selection_run_id)
        if run is None or not run.candidates:
            raise LiveRebalanceError("选股运行不存在或没有候选")
        age_days = (self._today - run.trading_day).days
        if age_days < 0 or age_days > self.MAX_SELECTION_AGE_DAYS:
            raise LiveRebalanceError("实盘选股结果必须为最近 10 天且不能来自未来")
        summary = SelectionEvaluationService(self._db).summarize(
            strategy_config=run.config_json
        )
        if (
            summary.evaluated_runs < 3
            or summary.mean_forward_return <= 0
            or summary.average_rank_ic is None
            or summary.average_rank_ic <= 0
        ):
            raise LiveRebalanceError(
                "实盘禁止绕过历史验证：至少 3 个同参数批次，且平均收益和 RankIC 均为正"
            )
        symbols = self.required_symbols(selection_run_id, account)
        self._validate_quotes(symbols, quotes)
        candidates = list(run.candidates)
        equal_weight = min(
            target_investment_ratio / len(candidates), max_symbol_weight
        )
        target_value = account.total_asset * equal_weight
        targets = {item.symbol for item in candidates}
        orders: list[dict] = []
        remaining_buy_budget = max(account.cash, 0) * 0.998
        for symbol, position in sorted(account.positions.items()):
            if symbol not in targets:
                quantity = floor(position.available_quantity / 100) * 100
                if quantity:
                    orders.append(self._order("SELL", symbol, quantity, quotes[symbol]))
        for candidate in candidates:
            quote = quotes[candidate.symbol]
            desired = floor(target_value / quote.ask_price / 100) * 100
            position = account.positions.get(candidate.symbol)
            current = position.quantity if position else 0
            difference = desired - current
            if difference >= 100:
                requested = floor(difference / 100) * 100
                affordable = floor(remaining_buy_budget / quote.ask_price / 100) * 100
                quantity = min(requested, affordable)
                if quantity:
                    orders.append(self._order("BUY", candidate.symbol, quantity, quote))
                    remaining_buy_budget -= quantity * quote.ask_price
            elif difference <= -100 and position:
                quantity = min(
                    floor(-difference / 100) * 100,
                    floor(position.available_quantity / 100) * 100,
                )
                if quantity:
                    orders.append(self._order("SELL", candidate.symbol, quantity, quote))
        orders.sort(key=lambda item: (item["side"] != "SELL", item["symbol"]))
        if not orders:
            raise LiveRebalanceError("当前真实持仓已达到目标，无需生成实盘委托")
        plan = LiveRebalancePlan(
            selection_run_id=selection_run_id,
            account_fingerprint=self._account_fingerprint,
            status="DRAFT",
            account_snapshot_json=self._snapshot_payload(account),
            proposal_json={
                "selection_trading_day": run.trading_day.isoformat(),
                "target_investment_ratio": target_investment_ratio,
                "max_symbol_weight": max_symbol_weight,
                "validation": {
                    "evaluated_runs": summary.evaluated_runs,
                    "mean_forward_return": summary.mean_forward_return,
                    "average_rank_ic": summary.average_rank_ic,
                },
                "orders": orders,
            },
        )
        self._db.add(plan)
        self._db.commit()
        self._db.refresh(plan)
        return plan

    def reconcile(
        self, plan_id: int, broker_orders: list[LiveOrderStatus]
    ) -> LiveRebalancePlan:
        plan = self._get_owned_plan(plan_id)
        if plan.status not in {"SUBMITTED", "PARTIAL"}:
            raise LiveRebalanceError(f"方案状态 {plan.status} 不需要对账")
        execution = dict(plan.execution_json or {})
        existing_orders = list(execution.get("orders") or [])
        if not existing_orders:
            raise LiveRebalanceError("方案没有可对账的委托记录")
        remark = f"live-plan-{plan.id}"
        matched_ids: set[int] = set()
        by_id = {item.order_id: item for item in broker_orders if item.order_id > 0}
        updated = []
        for item in existing_orders:
            broker_order = None
            order_id = item.get("order_id")
            if isinstance(order_id, int) and order_id > 0:
                broker_order = by_id.get(order_id)
            if broker_order is None:
                broker_order = self._match_order_by_remark(
                    item, broker_orders, remark, matched_ids
                )
            if broker_order is not None:
                matched_ids.add(broker_order.order_id)
                updated.append({**item, **self._broker_order_payload(broker_order)})
            else:
                updated.append(item)

        filled = sum(1 for item in updated if item.get("broker_status") == "FILLED")
        active = any(
            item.get("broker_status") in {"SUBMITTED", "PARTIAL_FILLED"}
            for item in updated
        )
        unknown = any(item.get("status") == "UNKNOWN" and "broker_status" not in item for item in updated)
        if filled == len(updated):
            plan.status = "FILLED"
        elif active or unknown:
            plan.status = "SUBMITTED"
        else:
            plan.status = "PARTIAL"
        execution["orders"] = updated
        execution["filled"] = filled
        execution["reconciled_at"] = utc_now().isoformat()
        plan.execution_json = execution
        self._db.commit()
        self._db.refresh(plan)
        return plan

    def approve(self, plan_id: int, acknowledgement: str) -> tuple[LiveRebalancePlan, str]:
        if acknowledgement != self.ACKNOWLEDGEMENT:
            raise LiveRebalanceError("确认文本不匹配，未授权使用真实资金")
        plan = self._get_owned_plan(plan_id)
        if plan.status != "DRAFT":
            raise LiveRebalanceError(f"方案状态 {plan.status} 不可批准")
        token = secrets.token_urlsafe(32)
        plan.approval_token_hash = self._token_hash(token)
        plan.approval_expires_at = utc_now() + self.APPROVAL_TTL
        plan.approved_at = utc_now()
        plan.status = "APPROVED"
        self._db.commit()
        return plan, token

    async def execute(
        self,
        plan_id: int,
        token: str,
        broker: QmtLiveBroker,
        current_account: LiveAccountSnapshot,
        quotes: dict[str, QuoteData],
    ) -> LiveRebalancePlan:
        plan = self._get_owned_plan(plan_id)
        if plan.status != "APPROVED":
            raise LiveRebalanceError(f"方案状态 {plan.status} 不可执行")
        if not plan.approval_expires_at or utc_now() > plan.approval_expires_at:
            plan.status = "EXPIRED"
            plan.approval_token_hash = None
            self._db.commit()
            raise LiveRebalanceError("实盘批准已超过 5 分钟")
        if not token or not hmac.compare_digest(
            self._token_hash(token), plan.approval_token_hash or ""
        ):
            raise LiveRebalanceError("一次性批准令牌无效")
        self._validate_account_unchanged(plan, current_account)
        orders = plan.proposal_json.get("orders", [])
        self._validate_quotes([item["symbol"] for item in orders], quotes)
        plan.status = "EXECUTING"
        plan.approval_token_hash = None
        self._db.commit()

        results = []
        for item in orders:
            quote = quotes[item["symbol"]]
            price = quote.bid_price if item["side"] == "SELL" else quote.ask_price
            try:
                order_id = await broker.place_limit_order(
                    item["symbol"],
                    item["side"],
                    item["quantity"],
                    price,
                    f"live-plan-{plan.id}",
                )
                results.append({**item, "order_id": order_id, "status": "SUBMITTED"})
            except QmtBrokerTimeout as exc:
                results.append({**item, "order_id": None, "status": "UNKNOWN", "error": str(exc)})
                break
            except Exception as exc:
                results.append({**item, "order_id": None, "status": "FAILED", "error": str(exc)})
                break
        plan = self._db.get(LiveRebalancePlan, plan_id)
        submitted = sum(item["status"] == "SUBMITTED" for item in results)
        plan.status = "SUBMITTED" if submitted == len(orders) else "PARTIAL"
        plan.execution_json = {
            "orders": results,
            "submitted": submitted,
            "total": len(orders),
        }
        plan.executed_at = utc_now()
        self._db.commit()
        self._db.refresh(plan)
        return plan

    def _validate_account_unchanged(
        self, plan: LiveRebalancePlan, current: LiveAccountSnapshot
    ) -> None:
        original = plan.account_snapshot_json
        original_total = float(original["total_asset"])
        drift = abs(current.total_asset - original_total) / max(original_total, 1)
        original_positions = {
            symbol: int(value["quantity"])
            for symbol, value in original.get("positions", {}).items()
        }
        original_available = {
            symbol: int(value["available_quantity"])
            for symbol, value in original.get("positions", {}).items()
        }
        current_positions = {
            symbol: value.quantity for symbol, value in current.positions.items()
        }
        current_available = {
            symbol: value.available_quantity for symbol, value in current.positions.items()
        }
        cash_changed = abs(current.cash - float(original["cash"])) > 1.0
        if (
            drift > self.MAX_ACCOUNT_ASSET_DRIFT
            or cash_changed
            or current_positions != original_positions
            or current_available != original_available
        ):
            raise LiveRebalanceError("真实账户资产或持仓已变化，必须重新生成并审核方案")

    def _get_owned_plan(self, plan_id: int) -> LiveRebalancePlan:
        plan = self._db.get(LiveRebalancePlan, plan_id)
        if plan is None or plan.account_fingerprint != self._account_fingerprint:
            raise LiveRebalanceError("实盘方案不存在或账户配置已变化")
        return plan

    @staticmethod
    def _validate_quotes(symbols: list[str], quotes: dict[str, QuoteData]) -> None:
        invalid = [
            symbol
            for symbol in symbols
            if symbol not in quotes
            or quotes[symbol].is_stale
            or quotes[symbol].bid_price <= 0
            or quotes[symbol].ask_price <= 0
        ]
        if invalid:
            raise LiveRebalanceError(f"缺少有效实时盘口: {', '.join(invalid[:5])}")

    @staticmethod
    def _snapshot_payload(account: LiveAccountSnapshot) -> dict:
        return {
            "cash": account.cash,
            "total_asset": account.total_asset,
            "positions": {
                symbol: {
                    "quantity": position.quantity,
                    "available_quantity": position.available_quantity,
                }
                for symbol, position in account.positions.items()
            },
        }

    @staticmethod
    def _order(side: str, symbol: str, quantity: int, quote: QuoteData) -> dict:
        price = quote.bid_price if side == "SELL" else quote.ask_price
        return {
            "side": side,
            "symbol": symbol,
            "quantity": quantity,
            "indicative_price": price,
            "indicative_value": round(price * quantity, 2),
        }

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _match_order_by_remark(
        item: dict,
        broker_orders: list[LiveOrderStatus],
        remark: str,
        matched_ids: set[int],
    ) -> LiveOrderStatus | None:
        for broker_order in broker_orders:
            if broker_order.order_id in matched_ids:
                continue
            if broker_order.remark != remark:
                continue
            if (
                broker_order.symbol == item.get("symbol")
                and broker_order.side == item.get("side")
                and broker_order.quantity == int(item.get("quantity", 0) or 0)
            ):
                return broker_order
        return None

    @staticmethod
    def _broker_order_payload(order: LiveOrderStatus) -> dict:
        return {
            "order_id": order.order_id,
            "broker_status": order.status,
            "broker_raw_status": order.raw_status,
            "broker_status_message": order.status_message,
            "broker_traded_volume": order.traded_volume,
            "broker_traded_price": order.traded_price,
            "broker_remark": order.remark,
        }
