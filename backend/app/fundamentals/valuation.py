"""估值引擎（确定性、纯函数、无 IO）。

设计原则（对应本平台的研究纪律）：

1. **假设全部显式**：所有输入由调用方给出，本模块不猜增长率、不猜折现率、不猜净利率。
   每个假设都要在 ``basis`` 里写明它是**事实**（来自财报/公告）、**估计**（分析者判断）
   还是**模型推断**（本平台计算得出），否则无法复核。
2. **口径写在返回值里**：返回值带 ``formula`` 与逐期现金流，前端可以展开计算过程。
3. **允许"算不出来"**：折现率 ≤ 永续增长率、股本 ≤ 0、营收 ≤ 0 等一律抛
   :class:`ValuationError` 并给出中文原因，绝不返回一个看起来像结论的数字。
4. 三情景通过 :func:`scenario_band` 生成，悲观/乐观是**对同一组假设做显式替换**，
   不是把结果乘一个系数 —— 这样每个情景的数字都能单独复核。

模型说明（必须如实告知用户）：本引擎用「营业总收入 × 自由现金流率」折现，
是**两阶段 DCF 的简化形式**，不建模资本开支、营运资本变动与税率明细。
真正的自由现金流需要现金流量表细项（可用东财 ``RPT_DMSK_FN_CASHFLOW`` 取到），
该细化留待数据接入后实现；当前口径下 ``fcf_margin`` 通常用
「经营现金流量净额 / 营业总收入」近似，属于**估计**。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Sequence


class ValuationError(ValueError):
    """假设不成立或数据不足，无法给出估值（不是异常，是正常业务状态）。"""


@dataclass(frozen=True)
class ValuationAssumptions:
    """一组显式估值假设（单位：元 / 小数）。"""

    #: 基期营业总收入（元）。口径必须与 ``basis`` 一致（例如"2026H1 报告期，未年化"）
    revenue: float
    #: 营业总收入年化增长率（小数，0.08 = 8%）
    revenue_growth: float
    #: 自由现金流 / 营业总收入（小数）
    fcf_margin: float
    #: 折现率（小数，近似 WACC）
    discount_rate: float
    #: 永续增长率（小数）
    terminal_growth: float
    #: 总股本（股）
    shares: float
    #: 净负债 = 有息负债 − 现金（元）；净现金为负值
    net_debt: float = 0.0
    #: 显式预测期年数
    years: int = 5
    #: 这组假设的来源说明（事实/估计/模型推断 + 依据）
    basis: str = ""

    def validate(self) -> None:
        if self.revenue <= 0:
            raise ValuationError("基期营业总收入必须为正，无法估值")
        if self.shares <= 0:
            raise ValuationError("总股本必须为正，无法计算每股价值")
        if self.years < 1:
            raise ValuationError("预测期年数至少为 1 年")
        if not -0.5 < self.revenue_growth < 1.0:
            raise ValuationError("增长率超出合理区间 (-50%, 100%)，请检查输入口径")
        if not 0.0 < self.discount_rate < 0.5:
            raise ValuationError("折现率超出合理区间 (0, 50%)")
        if self.discount_rate <= self.terminal_growth:
            raise ValuationError(
                "折现率必须大于永续增长率，否则永续价值发散（当前假设下估值无意义）"
            )
        if self.fcf_margin <= 0:
            raise ValuationError("自由现金流率为非正，不适用本折现模型")


@dataclass(frozen=True)
class ValuationOutput:
    """估值结果（含可展开的计算过程）。"""

    per_share: float
    equity_value: float
    enterprise_value: float
    terminal_value: float
    terminal_value_share: float
    cash_flows: tuple[float, ...]
    assumptions: ValuationAssumptions
    formula: str

    def to_dict(self) -> dict:
        return {
            "per_share": self.per_share,
            "equity_value": self.equity_value,
            "enterprise_value": self.enterprise_value,
            "terminal_value": self.terminal_value,
            "terminal_value_share": self.terminal_value_share,
            "cash_flows": list(self.cash_flows),
            "assumptions": {
                "revenue": self.assumptions.revenue,
                "revenue_growth": self.assumptions.revenue_growth,
                "fcf_margin": self.assumptions.fcf_margin,
                "discount_rate": self.assumptions.discount_rate,
                "terminal_growth": self.assumptions.terminal_growth,
                "shares": self.assumptions.shares,
                "net_debt": self.assumptions.net_debt,
                "years": self.assumptions.years,
                "basis": self.assumptions.basis,
            },
            "formula": self.formula,
        }


FORMULA = (
    "FCF_t = 营业总收入 × (1+g)^t × 自由现金流率；"
    "企业价值 = Σ_{t=1..N} FCF_t/(1+r)^t + [FCF_N×(1+g∞)/(r−g∞)]/(1+r)^N；"
    "股权价值 = 企业价值 − 净负债；每股价值 = 股权价值 ÷ 总股本"
)


def intrinsic_value(assumptions: ValuationAssumptions) -> ValuationOutput:
    """两阶段 DCF：按显式假设给出每股内在价值。"""
    assumptions.validate()
    a = assumptions
    cash_flows = tuple(
        a.revenue * (1.0 + a.revenue_growth) ** year * a.fcf_margin
        for year in range(1, a.years + 1)
    )
    pv_explicit = sum(
        cf / (1.0 + a.discount_rate) ** year
        for year, cf in enumerate(cash_flows, start=1)
    )
    terminal = (
        cash_flows[-1] * (1.0 + a.terminal_growth)
        / (a.discount_rate - a.terminal_growth)
    )
    pv_terminal = terminal / (1.0 + a.discount_rate) ** a.years
    enterprise_value = pv_explicit + pv_terminal
    equity_value = enterprise_value - a.net_debt
    return ValuationOutput(
        per_share=round(equity_value / a.shares, 4),
        equity_value=round(equity_value, 2),
        enterprise_value=round(enterprise_value, 2),
        terminal_value=round(terminal, 2),
        terminal_value_share=round(pv_terminal / enterprise_value, 4)
        if enterprise_value > 0
        else 0.0,
        cash_flows=tuple(round(cf, 2) for cf in cash_flows),
        assumptions=a,
        formula=FORMULA,
    )


@dataclass(frozen=True)
class Scenario:
    """一个情景：名称 + 相对基准假设的显式替换 + 结果。"""

    label: str
    #: 相对基准被替换的假设（键为 :class:`ValuationAssumptions` 字段名）
    overrides: dict
    output: ValuationOutput

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "overrides": self.overrides,
            "result": self.output.to_dict(),
        }


@dataclass(frozen=True)
class ScenarioBand:
    """悲观 / 基准 / 乐观三情景（外加"不适用"原因，若某情景算不出来）。"""

    bear: Scenario | None
    base: Scenario | None
    bull: Scenario | None
    errors: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "scenarios": [
                s.to_dict() for s in (self.bear, self.base, self.bull) if s is not None
            ],
            "errors": self.errors,
            "range": self.range,
        }

    @property
    def range(self) -> list[float] | None:
        values = sorted(
            s.output.per_share for s in (self.bear, self.base, self.bull) if s is not None
        )
        return [values[0], values[-1]] if values else None


def scenario_band(
    base: ValuationAssumptions,
    *,
    bear_overrides: dict,
    bull_overrides: dict,
) -> ScenarioBand:
    """生成三情景。

    ``bear_overrides`` / ``bull_overrides`` 会被**显式记录**在结果里，例如
    ``{"revenue_growth": 0.0, "fcf_margin": 0.08}``；某个情景假设不成立时（如折现率
    被调到低于永续增长率）只记录原因，不编造数字。
    """
    errors: dict[str, str] = {}
    outputs: dict[str, Scenario | None] = {}
    for label, overrides in (
        ("悲观", bear_overrides),
        ("基准", {}),
        ("乐观", bull_overrides),
    ):
        try:
            assumptions = replace(base, **overrides) if overrides else base
            outputs[label] = Scenario(
                label=label, overrides=dict(overrides), output=intrinsic_value(assumptions)
            )
        except ValuationError as exc:
            outputs[label] = None
            errors[label] = str(exc)
        except TypeError as exc:  # overrides 里出现未知字段
            outputs[label] = None
            errors[label] = f"未知假设字段: {exc}"
    return ScenarioBand(
        bear=outputs.get("悲观"),
        base=outputs.get("基准"),
        bull=outputs.get("乐观"),
        errors=errors,
    )


def sensitivity_table(
    base: ValuationAssumptions,
    *,
    growth_values: Sequence[float],
    discount_values: Sequence[float],
) -> dict:
    """敏感性表：行 = 增长率，列 = 折现率，格 = 每股价值（算不出为 ``None``）。

    返回结构自带行列标签与单位，前端可直接渲染；``None`` 会被保留而不是填 0，
    以免把"无意义"读成"价值为零"。
    """
    rows: list[dict] = []
    for growth in growth_values:
        cells: list[float | None] = []
        for discount in discount_values:
            try:
                out = intrinsic_value(
                    replace(base, revenue_growth=growth, discount_rate=discount)
                )
                cells.append(out.per_share)
            except ValuationError:
                cells.append(None)
        rows.append(
            {
                "revenue_growth": growth,
                "discount_rate_cells": cells,
            }
        )
    return {
        "growth_values": list(growth_values),
        "discount_values": list(discount_values),
        "rows": rows,
        "unit": "元/股",
        "note": "空格为 None，表示该组合下假设不成立（通常因折现率不高于永续增长率）",
    }


def upside_ratio(per_share: float | None, price: float | None) -> float | None:
    """相对现价的偏离度（小数）。任一缺失返回 ``None``（不做缺省猜测）。"""
    if per_share is None or price is None or price <= 0:
        return None
    return round(per_share / price - 1.0, 4)


def margin_of_safety(per_share: float | None, price: float | None) -> float | None:
    """安全边际 = (价值 − 价格) / 价值。"""
    if per_share is None or price is None or per_share <= 0:
        return None
    return round((per_share - price) / per_share, 4)


def weighted_value(
    scenarios: Iterable[Scenario], weights: Sequence[float]
) -> float | None:
    """情景加权价值（权重必须显式给出，且要求与情景一一对应）。"""
    items = list(scenarios)
    if not items or len(items) != len(weights):
        return None
    total = sum(weights)
    if total <= 0:
        return None
    return round(sum(s.output.per_share * w for s, w in zip(items, weights)) / total, 4)
