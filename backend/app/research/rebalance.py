"""Deterministic research-to-paper allocation draft.

The output is deliberately a *draft*: it never writes an order and never bypasses the
existing paper/live trading approval gates.  Every cap is derived from a user supplied
constraint or an observable portfolio/liquidity input.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor, sqrt


@dataclass(frozen=True)
class ResearchDraftInputs:
    conclusion_key: str
    symbol: str
    price: float
    total_asset: float
    current_quantity: int
    current_weight: float
    industry: str | None
    industry_weight: float | None
    invested_weight: float
    average_amount_20: float | None
    max_correlation: float | None
    correlation_observations: int
    held_peer_count: int


@dataclass(frozen=True)
class ResearchDraftConstraints:
    max_symbol_weight: float
    max_industry_weight: float
    max_loss_per_trade: float
    stop_distance: float
    max_portfolio_drawdown: float
    max_correlation: float
    max_liquidity_participation: float


def pearson(left: list[float], right: list[float]) -> float | None:
    """Return Pearson correlation, or ``None`` when it cannot be supported."""
    if len(left) != len(right) or len(left) < 2:
        return None
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True)
    )
    variance_left = sum((value - mean_left) ** 2 for value in left)
    variance_right = sum((value - mean_right) ** 2 for value in right)
    denominator = sqrt(variance_left * variance_right)
    return covariance / denominator if denominator > 0 else None


def aligned_return_correlation(
    target: dict[str, float], peer: dict[str, float]
) -> tuple[float | None, int]:
    """Align close prices by date, then correlate one-period returns."""
    dates = sorted(set(target) & set(peer))
    if len(dates) < 3:
        return None, 0
    target_returns: list[float] = []
    peer_returns: list[float] = []
    for previous, current in zip(dates, dates[1:]):
        target_previous = target[previous]
        peer_previous = peer[previous]
        if target_previous <= 0 or peer_previous <= 0:
            continue
        target_returns.append(target[current] / target_previous - 1)
        peer_returns.append(peer[current] / peer_previous - 1)
    return pearson(target_returns, peer_returns), len(target_returns)


def build_research_draft(
    inputs: ResearchDraftInputs, constraints: ResearchDraftConstraints
) -> dict:
    """Build a conservative BUY-only allocation draft with auditable caps."""
    can_increase = inputs.conclusion_key == "attractive"
    caps: dict[str, float] = {
        "current_weight": max(0.0, inputs.current_weight),
        "max_symbol_weight": constraints.max_symbol_weight,
        "single_trade_loss_budget": constraints.max_loss_per_trade
        / constraints.stop_distance,
    }
    checks: list[dict] = []

    if inputs.industry and inputs.industry_weight is not None:
        caps["industry_headroom"] = inputs.current_weight + max(
            0.0, constraints.max_industry_weight - inputs.industry_weight
        )
        checks.append(
            {
                "name": "industry_exposure",
                "status": "pass" if caps["industry_headroom"] > inputs.current_weight else "block",
                "detail": (
                    f"{inputs.industry} 当前权重 {inputs.industry_weight:.2%}，"
                    f"上限 {constraints.max_industry_weight:.2%}"
                ),
            }
        )
    else:
        caps["industry_headroom"] = inputs.current_weight
        checks.append(
            {
                "name": "industry_exposure",
                "status": "block",
                "detail": "行业或现有行业权重缺失，禁止增加仓位",
            }
        )

    current_risk = inputs.invested_weight * constraints.stop_distance
    drawdown_headroom = max(0.0, constraints.max_portfolio_drawdown - current_risk)
    caps["portfolio_drawdown_budget"] = inputs.current_weight + (
        drawdown_headroom / constraints.stop_distance
    )
    checks.append(
        {
            "name": "drawdown_budget",
            "status": "pass" if drawdown_headroom > 0 else "block",
            "detail": (
                f"按统一止损距离估算已用风险 {current_risk:.2%}，"
                f"组合预算 {constraints.max_portfolio_drawdown:.2%}"
            ),
        }
    )

    if inputs.average_amount_20 and inputs.average_amount_20 > 0:
        max_order_value = inputs.average_amount_20 * constraints.max_liquidity_participation
        caps["liquidity"] = inputs.current_weight + max_order_value / inputs.total_asset
        checks.append(
            {
                "name": "liquidity",
                "status": "pass",
                "detail": (
                    f"20日平均成交额 {inputs.average_amount_20:.0f} 元，"
                    f"单次草案不超过 {constraints.max_liquidity_participation:.2%}"
                ),
            }
        )
    else:
        caps["liquidity"] = inputs.current_weight
        checks.append(
            {"name": "liquidity", "status": "block", "detail": "缺少20日成交额，禁止增加仓位"}
        )

    if inputs.held_peer_count == 0:
        checks.append(
            {"name": "correlation", "status": "pass", "detail": "组合内没有其他持仓，无相关性集中问题"}
        )
    elif inputs.max_correlation is None or inputs.correlation_observations < 20:
        caps["correlation"] = inputs.current_weight
        checks.append(
            {"name": "correlation", "status": "block", "detail": "共同收益样本不足20期，禁止增加仓位"}
        )
    elif inputs.max_correlation > constraints.max_correlation:
        caps["correlation"] = inputs.current_weight
        checks.append(
            {
                "name": "correlation",
                "status": "block",
                "detail": (
                    f"与现有持仓最高相关系数 {inputs.max_correlation:.2f}，"
                    f"超过上限 {constraints.max_correlation:.2f}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "correlation",
                "status": "pass",
                "detail": (
                    f"与现有持仓最高相关系数 {inputs.max_correlation:.2f}，"
                    f"共同样本 {inputs.correlation_observations} 期"
                ),
            }
        )

    if not can_increase:
        caps["research_conclusion"] = inputs.current_weight
        checks.append(
            {
                "name": "research_conclusion",
                "status": "block",
                "detail": f"结论 {inputs.conclusion_key} 不允许增加仓位；仅 attractive 可生成增持草案",
            }
        )
    else:
        checks.append(
            {
                "name": "research_conclusion",
                "status": "pass",
                "detail": "结论为 attractive，只代表允许计算草案，不代表建议买入",
            }
        )

    target_weight = min(value for key, value in caps.items() if key != "current_weight")
    target_weight = max(inputs.current_weight, target_weight)
    # 浮点运算可能把理论上的 10_000 股表示成 9_999.999999；先在“股数”
    # 量级补极小容差，再按 A 股 100 股整数手向下取整。
    raw_target_quantity = inputs.total_asset * target_weight / inputs.price
    target_quantity = floor((raw_target_quantity + 1e-7) / 100) * 100
    buy_quantity = max(0, target_quantity - inputs.current_quantity)
    buy_quantity = floor(buy_quantity / 100) * 100
    proposed_order = None
    if buy_quantity >= 100:
        proposed_order = {
            "side": "BUY",
            "symbol": inputs.symbol,
            "quantity": buy_quantity,
            "indicative_price": inputs.price,
            "indicative_value": round(buy_quantity * inputs.price, 2),
        }
    blocked = any(item["status"] == "block" for item in checks)
    return {
        "action": "INCREASE_DRAFT" if proposed_order else ("BLOCKED" if blocked else "HOLD"),
        "current_weight": round(inputs.current_weight, 6),
        "target_weight": round(target_weight, 6),
        "caps": {key: round(value, 6) for key, value in caps.items()},
        "checks": checks,
        "proposed_order": proposed_order,
        "execution_allowed": False,
        "note": "只读调仓草案；未写入模拟订单。执行仍须走模拟盘草案与人工确认门禁。",
    }
