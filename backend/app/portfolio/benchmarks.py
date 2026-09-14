"""基准构造（D7）：把「排序能力」与「市场/现金」分开列示。

研发计划 §5.3.5 / §11.7 的要求：

> 同池等权只衡量池内排序能力；另列预先选定市场指数与含现金基准。

因此每个组合回测结果同时给出三条曲线/三个收益数字：

1. ``equity_curve``：策略账户净值（现金 + 持仓市值，含费用与滑点）；
2. ``equal_weight_curve``：**同池等权买入持有**——在完全相同的标的池与日期区间上，
   每只标的以窗口内首个收盘价归一化后等权平均。策略相对它的超额 = **池内排序能力**；
3. ``cash_curve``：**含现金基准**——初始资金原样持有，收益恒为 0。策略相对它的超额
   = 承担风险换来的绝对收益。

三者口径必须一致（同一日期轴、同一初始资金、都不含分红送转），否则比较无意义。
"""
from __future__ import annotations

from datetime import date


def equal_weight_buy_and_hold(
    series_by_symbol: dict[str, dict[date, float]],
    dates: list[date],
    initial_cash: float,
) -> list[float]:
    """同池等权买入持有曲线。

    * 每只标的以其在窗口内**第一个有行情的日期**的收盘价归一化（该日不计涨跌）；
    * 同一日期上对所有「当日有行情」的标的取等权平均；
    * 尚未开始交易（当日无行情）的标的在该日不参与，避免用未来价格回填。

    返回与 ``dates`` 等长的曲线；没有任何可用数据时返回空列表。
    """
    if not dates or not series_by_symbol or initial_cash <= 0:
        return []

    first_close: dict[str, float] = {}
    for symbol, closes in series_by_symbol.items():
        for day in dates:
            price = closes.get(day)
            if price and price > 0:
                first_close[symbol] = price
                break

    curve: list[float] = []
    for day in dates:
        ratios: list[float] = []
        for symbol, closes in series_by_symbol.items():
            base = first_close.get(symbol)
            price = closes.get(day)
            if base and price and price > 0:
                ratios.append(price / base)
        if not ratios:
            # 该日无人有行情（例如全部停牌）：沿用上一点，避免断点
            curve.append(curve[-1] if curve else initial_cash)
            continue
        curve.append(initial_cash * (sum(ratios) / len(ratios)))
    return curve


def cash_curve(dates: list[date], initial_cash: float) -> list[float]:
    """含现金基准：不做任何交易，净值恒为初始资金。"""
    if not dates:
        return []
    return [float(initial_cash) for _ in dates]


def return_of(curve: list[float], initial_cash: float) -> float:
    """曲线对应的区间收益（曲线空或初始资金非正时返回 0）。"""
    if not curve or initial_cash <= 0:
        return 0.0
    return (curve[-1] - initial_cash) / initial_cash


def benchmark_labels(has_market_index: bool) -> dict:
    """基准口径标注：说清每条基准衡量什么，避免把同池等权当成市场基准。"""
    return {
        "equal_weight": "同池等权买入持有：只衡量池内排序能力，不是市场基准",
        "cash": "含现金基准：不承担任何市场风险，收益恒为 0",
        "market_index": (
            "预先选定的市场指数（买入持有）"
            if has_market_index
            else "未设置市场指数基准：结论不能表述为「跑赢市场」"
        ),
        "has_market_index": has_market_index,
    }
