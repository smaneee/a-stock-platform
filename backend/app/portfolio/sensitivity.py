"""成本/滑点敏感性分析。

在基础配置上对佣金率与滑点做网格化扰动，重新运行组合回测，
输出各档参数下的总收益/最大回撤/夏普等指标，用于评估策略对交易成本的稳健性。
"""
from __future__ import annotations

from dataclasses import replace

from app.market_data.base import QuoteData
from app.portfolio.config import PortfolioConfig
from app.portfolio.engine import PortfolioBacktestEngine


def _run_with_config(
    engine: PortfolioBacktestEngine,
    histories: dict[str, list[QuoteData]],
    config: PortfolioConfig,
) -> dict:
    """用指定配置克隆引擎并运行，返回精简指标。"""
    clone = PortfolioBacktestEngine(
        strategies=engine.strategies,
        weights=engine.weights,
        config=config,
        benchmark=engine.benchmark,
    )
    r = clone.run(histories)
    return {
        "total_return": r.total_return,
        "annual_return": r.annual_return,
        "max_drawdown": r.max_drawdown,
        "sharpe_ratio": r.sharpe_ratio,
        "trade_count": r.trade_count,
        "turnover": r.turnover,
    }


def run_sensitivity(
    engine: PortfolioBacktestEngine,
    histories: dict[str, list[QuoteData]],
    commission_rates: list[float] | None = None,
    slippages: list[float] | None = None,
) -> list[dict]:
    """对佣金率与滑点做网格敏感性分析，返回各档指标列表。

    每档记录使用的参数与对应指标。默认佣金率 [0.0003, 0.001, 0.003]，
    默认滑点 [0.0005, 0.001, 0.002]。
    """
    commission_rates = commission_rates or [0.0003, 0.001, 0.003]
    slippages = slippages or [0.0005, 0.001, 0.002]
    base_exec = engine.config.execution
    results: list[dict] = []

    # 单独扰动佣金率（滑点保持基准）
    for rate in commission_rates:
        exec_cfg = replace(base_exec, commission_rate=rate)
        config = replace(engine.config, execution=exec_cfg)
        metrics = _run_with_config(engine, histories, config)
        results.append({"commission_rate": rate, "slippage": base_exec.slippage, **metrics})

    # 单独扰动滑点（佣金率保持基准）
    for slip in slippages:
        exec_cfg = replace(base_exec, slippage=slip)
        config = replace(engine.config, execution=exec_cfg)
        metrics = _run_with_config(engine, histories, config)
        results.append({"commission_rate": base_exec.commission_rate, "slippage": slip, **metrics})

    return results
