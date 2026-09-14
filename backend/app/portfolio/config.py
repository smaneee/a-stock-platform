"""组合回测配置。"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.backtest.execution import ExecutionConfig


@dataclass(frozen=True)
class PortfolioConfig:
    """组合回测配置。

    仓位限制用于风控：单标的不得超过 max_single_position，总仓位不得超过
    max_total_position（预留现金）。成本与滑点复用 ExecutionConfig。
    """

    initial_cash: float = 1_000_000.0
    max_single_position: float = 0.20  # 单只股票最大仓位（占总资产比例）
    max_total_position: float = 0.95  # 总仓位上限
    benchmark_symbol: str | None = None  # 基准指数代码（仅用于展示标注）
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk_free_rate: float = 0.0  # 无风险利率（年化，用于 alpha/夏普）
    #: 本次回测实际使用的日线复权口径（D6）。只用于在结果里如实标注口径，
    #: 引擎本身不据此取数（取数在 portfolio_worker 里按同一配置完成）。
    bars_adjust: str = "none"
