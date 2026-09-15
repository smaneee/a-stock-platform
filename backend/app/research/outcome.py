"""复盘归因：把"这次要不要重新研究"进一步区分为**为什么**（S4 / 方案 §三）。

方案 §三要求：「复盘先检查原论点是否成立，再检查收益；区分**判断错误、估值过高、仓位问题、
执行偏差和随机波动**」。复核接口此前只回答"是否需要重新研究"与"哪些字段变了"，
本模块把变化**归因**到上面五类，避免把"价格噪声"当成"论点失效"，也避免把"论点被证伪"
轻描淡写成"正常波动"。

判定顺序（先因后果，第一命中即返回，并给出理由与依据字段）：

============================  ==========================================================
归因                          触发条件（全部为确定性规则）
============================  ============================================================
``thesis_invalidated``        结构化失效条件命中：当前指标已越过阈值（例：ROE < 画像下限）
``fact_changed``              财报报告期变化，或质量/现金流等**事实类**指标发生实质变化
``valuation_drift``           事实未变，只有价格/估值相关项变化（含基准价值、偏离度）
``position_constraint``       当前权重超出用户给定的单票上限（仓位问题，而不是判断问题）
``execution_gap``             计划数量与实际成交数量不一致（执行偏差；需传入两者）
``noise``                     无实质变化且价格波动小于阈值 → 随机波动
``inconclusive``              上述都不成立（数据不足或变化无法归类）
============================  ==========================================================

纪律：只做**归因**，不预测涨跌；每条结论都带 ``evidence``（依据的字段与数值），可人工复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 价格波动小于该幅度且无实质变化 → 视为随机波动
NOISE_PRICE_BAND = 0.02

#: 触发失效条件比对时允许的浮点容差
THRESHOLD_EPSILON = 1e-9

OUTCOME_LABELS = {
    "thesis_invalidated": "判断错误：论点失效条件已命中",
    "fact_changed": "事实变化：财报/经营数据出现实质变化",
    "valuation_drift": "估值变化：事实未变，价格已偏离原假设",
    "position_constraint": "仓位问题：当前权重超出用户给定的约束",
    "execution_gap": "执行偏差：计划与实际成交不一致",
    "noise": "随机波动：无实质变化",
    "inconclusive": "无法归因：数据不足或变化无法归类",
}


@dataclass
class Outcome:
    key: str
    label: str
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    #: 是否属于"论点/判断"层面的问题（True）还是"价格/执行"层面（False）
    thesis_level: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "thesis_level": self.thesis_level,
        }


def _metric_value(analysis: dict, metric_key: str) -> float | None:
    subs = ((analysis.get("3_dimensions") or {}).get("quality") or {}).get("subscores") or {}
    item = subs.get(metric_key) or {}
    value = item.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def hit_invalidation_conditions(
    structured_conditions: list[dict], current_analysis: dict
) -> list[dict]:
    """检查结构化失效条件是否已被当前数据命中（这是"论点失效"的硬证据）。"""
    hits: list[dict] = []
    for condition in structured_conditions or []:
        metric = condition.get("metric")
        operator = condition.get("operator")
        threshold = condition.get("threshold")
        if metric == "price_vs_base_value":
            # 价格类条件需要现价与基准价值，由调用方在 evidence 里给出，这里跳过
            continue
        if not metric or operator not in ("<", "<=", ">", ">=") or threshold is None:
            continue
        current = _metric_value(current_analysis, metric)
        if current is None:
            continue
        hit = {
            "<": current < threshold - THRESHOLD_EPSILON,
            "<=": current <= threshold + THRESHOLD_EPSILON,
            ">": current > threshold + THRESHOLD_EPSILON,
            ">=": current >= threshold - THRESHOLD_EPSILON,
        }[operator]
        if hit:
            hits.append({
                "metric": metric,
                "label": condition.get("label"),
                "threshold": threshold,
                "current": current,
                "operator": operator,
                "text": condition.get("text"),
                "source": condition.get("source"),
            })
    return hits


def classify_review_outcome(
    *,
    run: dict,
    current_snapshot: dict,
    current_analysis: dict,
    changes: list[dict] | None = None,
    structured_conditions: list[dict] | None = None,
    position_weight: float | None = None,
    max_symbol_weight: float | None = None,
    planned_quantity: float | None = None,
    executed_quantity: float | None = None,
) -> Outcome:
    """把一次复核**归因**到七类之一（第一命中即返回，理由与依据都写清）。"""
    changes = list(changes or [])
    run_price = run.get("price")
    now_price = current_snapshot.get("price")
    price_change = None
    if isinstance(run_price, (int, float)) and isinstance(now_price, (int, float)) and run_price:
        price_change = now_price / run_price - 1.0

    # ① 论点失效条件命中 → 判断错误（最严重，优先）
    hits = hit_invalidation_conditions(structured_conditions or [], current_analysis)
    if hits:
        return Outcome(
            key="thesis_invalidated",
            label=OUTCOME_LABELS["thesis_invalidated"],
            reasons=[
                f"{item['label'] or item['metric']} 当前 {item['current']} "
                f"{item['operator']} 阈值 {item['threshold']}（{item['source']}）"
                for item in hits
            ],
            evidence={"hits": hits, "price_change": price_change},
            thesis_level=True,
        )

    # ② 财报报告期变化或事实类指标变化 → 事实变化
    fact_fields = {"report_date", "roe", "debt_ratio", "profit_yoy", "ocf_to_profit",
                   "revenue_yoy", "gross_margin", "net_margin", "goodwill", "free_cash_flow"}
    fact_changes = [c for c in changes if c.get("field") in fact_fields]
    if fact_changes:
        return Outcome(
            key="fact_changed",
            label=OUTCOME_LABELS["fact_changed"],
            reasons=[
                f"{c.get('label') or c.get('field')}：{c.get('before')} → {c.get('after')}"
                for c in fact_changes
            ],
            evidence={"changes": fact_changes, "price_change": price_change},
            thesis_level=True,
        )

    # ③ 只有价格/估值相关项变化 → 估值变化（事实没变）
    valuation_fields = {"price", "base_per_share", "conclusion_key", "valuation_method"}
    valuation_changes = [
        c for c in changes
        if c.get("field") in valuation_fields or str(c.get("field", "")).startswith("per_share_")
    ]
    if valuation_changes and not fact_changes:
        return Outcome(
            key="valuation_drift",
            label=OUTCOME_LABELS["valuation_drift"],
            reasons=[
                f"{c.get('label') or c.get('field')}：{c.get('before')} → {c.get('after')}"
                for c in valuation_changes
            ],
            evidence={"changes": valuation_changes, "price_change": price_change},
            thesis_level=False,
        )

    # ④ 仓位超出用户约束 → 仓位问题（不是判断问题）
    if (
        position_weight is not None
        and max_symbol_weight is not None
        and position_weight > max_symbol_weight
    ):
        return Outcome(
            key="position_constraint",
            label=OUTCOME_LABELS["position_constraint"],
            reasons=[
                f"当前权重 {position_weight:.2%} 超出用户单票上限 {max_symbol_weight:.2%}"
            ],
            evidence={
                "position_weight": position_weight,
                "max_symbol_weight": max_symbol_weight,
                "price_change": price_change,
            },
            thesis_level=False,
        )

    # ⑤ 计划与实际成交不一致 → 执行偏差
    if (
        planned_quantity is not None
        and executed_quantity is not None
        and planned_quantity != executed_quantity
    ):
        return Outcome(
            key="execution_gap",
            label=OUTCOME_LABELS["execution_gap"],
            reasons=[f"计划 {planned_quantity} 股，实际成交 {executed_quantity} 股"],
            evidence={
                "planned_quantity": planned_quantity,
                "executed_quantity": executed_quantity,
                "price_change": price_change,
            },
            thesis_level=False,
        )

    # ⑥ 无变化且价格波动小 → 随机波动
    if not changes and (price_change is None or abs(price_change) < NOISE_PRICE_BAND):
        return Outcome(
            key="noise",
            label=OUTCOME_LABELS["noise"],
            reasons=[
                f"无字段变化，价格变动 {price_change:+.2%}" if price_change is not None
                else "无字段变化，且缺少可比现价"
            ],
            evidence={"changes": [], "price_change": price_change},
            thesis_level=False,
        )

    # ⑦ 兜底
    return Outcome(
        key="inconclusive",
        label=OUTCOME_LABELS["inconclusive"],
        reasons=["变化存在但无法归入事实/估值/仓位/执行任一类，建议人工复核"],
        evidence={"changes": changes, "price_change": price_change},
        thesis_level=False,
    )
