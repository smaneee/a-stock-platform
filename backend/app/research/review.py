"""研究记录复核：版本差异对比 + 复核触发条件（确定性纯函数，无 IO）。

为什么需要：研究记录（`investment_research_runs`）已经**冻结**了每次研究的假设、分析、
反向估值与指纹，但冻结本身只解决"当时怎么想的"，不解决"**现在需不需要重新研究**"。
本模块补齐后半段：

* :func:`diff_runs` —— 两次研究之间**哪些事实/假设/结论变了**（逐项列出前后值与方向）；
* :func:`evaluate_triggers` —— 用**当前**快照与重算结果判断哪些复核条件已经触发；
* :func:`build_review` —— 合成"是否需要复核"的结论与理由。

纪律：

1. 只比较**已冻结的记录**与**当前重算结果**，不引入任何外部预期；
2. 触发条件是显式阈值，写死在常量里，可被测试固定；
3. 没有上一次记录时如实返回 ``has_previous=False``，不拿"空"当"没变化"；
4. 本模块不判断买不买，只回答"要不要重新看一遍"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: 现价相对基准情景价值的偏离阈值（超过即触发复核）
PRICE_DEVIATION_TRIGGER = 0.20
#: 研究记录超过这么多天未更新即提醒（价格与财报之外的"陈旧"提醒）
STALE_RUN_DAYS = 30
#: 报告期滞后超过这么多天视为数据过期（与 quality.STALE_LIMIT_DAYS 同口径）
STALE_REPORT_DAYS = 270

#: 变化项的重要度（用于前端排序；high 排最前）
IMPORTANCE = {
    "conclusion_key": "high",
    "price": "high",
    "report_date": "high",
    "base_per_share": "high",
    "quality_score": "medium",
    "evidence_confidence": "medium",
    "revenue_growth": "medium",
    "fcf_margin": "medium",
    "discount_rate": "medium",
    "terminal_growth": "medium",
    "net_debt": "medium",
    "support_evidence": "medium",
    "oppose_evidence": "medium",
    "invalidation_conditions": "medium",
    "explanation_status": "low",
    "snapshot_date": "low",
}


@dataclass(frozen=True)
class Change:
    field: str
    label: str
    before: object
    after: object
    direction: str          # up / down / changed / added / removed
    importance: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "label": self.label,
            "before": self.before,
            "after": self.after,
            "direction": self.direction,
            "importance": self.importance,
            "note": self.note,
        }


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _direction(before: object, after: object) -> str:
    b, a = _num(before), _num(after)
    if b is not None and a is not None:
        if a > b:
            return "up"
        if a < b:
            return "down"
        return "changed"
    return "changed"


def _analysis_of(run: dict) -> dict:
    return run.get("analysis") or {}


def _scenario_values(run: dict) -> dict[str, float | None]:
    """各情景每股价值（来自冻结的统一分析）。"""
    out: dict[str, float | None] = {}
    for scenario in ((_analysis_of(run).get("5_scenarios") or {}).get("scenarios") or []):
        label = str(scenario.get("label"))
        out[label] = (scenario.get("result") or {}).get("per_share")
    return out


def _upside_values(run: dict) -> dict[str, float | None]:
    valuation = ((_analysis_of(run).get("3_dimensions") or {}).get("valuation")) or {}
    return dict(valuation.get("upside_vs_price") or {})


def _quality(run: dict) -> dict:
    return ((_analysis_of(run).get("3_dimensions") or {}).get("quality")) or {}


def _conclusion(run: dict) -> dict:
    return _analysis_of(run).get("1_conclusion") or {}


def _open_items(run: dict) -> dict:
    return _analysis_of(run).get("6_open_items") or {}


def _evidence(run: dict, side: str) -> list[str]:
    items = (_analysis_of(run).get("4_evidence") or {}).get(side) or []
    return [str(item.get("evidence")) for item in items]


def _assumptions(run: dict) -> dict:
    """估值假设。记录里是嵌套的 ``{"valuation": {...}}``，这里同时兼容扁平写法。"""
    raw = run.get("assumptions") or {}
    valuation = raw.get("valuation")
    return valuation if isinstance(valuation, dict) else raw


def _scalar_change(field: str, label: str, before: object, after: object,
                   note: str = "") -> Change | None:
    if before == after:
        return None
    return Change(
        field=field,
        label=label,
        before=before,
        after=after,
        direction=_direction(before, after),
        importance=IMPORTANCE.get(field, "low"),
        note=note,
    )


def _set_change(field: str, label: str, before: list[str], after: list[str]) -> Change | None:
    before_set, after_set = set(before), set(after)
    if before_set == after_set:
        return None
    added = sorted(after_set - before_set)
    removed = sorted(before_set - after_set)
    return Change(
        field=field,
        label=label,
        before=removed or None,
        after=added or None,
        direction="changed",
        importance=IMPORTANCE.get(field, "low"),
        note=f"新增 {len(added)} 条、移除 {len(removed)} 条",
    )


#: 逐项比较的标量字段：(字段名, 中文标签, 取值函数)
SCALARS: tuple[tuple[str, str, object], ...] = (
    ("price", "现价", lambda r: r.get("price")),
    ("snapshot_date", "快照日", lambda r: r.get("snapshot_date")),
    ("report_date", "财报报告期", lambda r: r.get("report_date")),
    ("conclusion_key", "结论状态", lambda r: r.get("conclusion_key")),
    ("quality_score", "企业质量分", lambda r: _quality(r).get("score")),
    ("evidence_confidence", "证据置信度", lambda r: _conclusion(r).get("evidence_confidence_score")),
    ("revenue_growth", "营收增长率假设", lambda r: _assumptions(r).get("revenue_growth")),
    ("fcf_margin", "自由现金流率假设", lambda r: _assumptions(r).get("fcf_margin")),
    ("discount_rate", "折现率假设", lambda r: _assumptions(r).get("discount_rate")),
    ("terminal_growth", "永续增长率假设", lambda r: _assumptions(r).get("terminal_growth")),
    ("net_debt", "净负债假设", lambda r: _assumptions(r).get("net_debt")),
    ("explanation_status", "解释层状态", lambda r: r.get("explanation_status")),
)


def diff_runs(previous: dict, current: dict) -> dict:
    """比较两次研究（参数为 ``_detail()`` 形状的字典），返回逐项差异。"""
    changes: list[Change] = []
    for field, label, getter in SCALARS:
        change = _scalar_change(field, label, getter(previous), getter(current))
        if change is not None:
            changes.append(change)

    prev_scenarios, cur_scenarios = _scenario_values(previous), _scenario_values(current)
    for label in sorted(set(prev_scenarios) | set(cur_scenarios)):
        change = _scalar_change(
            "base_per_share" if label == "基准" else f"per_share_{label}",
            f"{label}情景每股价值",
            prev_scenarios.get(label),
            cur_scenarios.get(label),
        )
        if change is not None:
            changes.append(change)

    for side, field, label in (
        ("support", "support_evidence", "支持证据"),
        ("oppose", "oppose_evidence", "反对证据"),
    ):
        change = _set_change(field, label, _evidence(previous, side), _evidence(current, side))
        if change is not None:
            changes.append(change)

    change = _set_change(
        "invalidation_conditions",
        "论点失效条件",
        list(_open_items(previous).get("invalidation_conditions") or []),
        list(_open_items(current).get("invalidation_conditions") or []),
    )
    if change is not None:
        changes.append(change)

    order = {"high": 0, "medium": 1, "low": 2}
    changes.sort(key=lambda c: (order.get(c.importance, 3), c.field))
    high = [c for c in changes if c.importance == "high"]
    return {
        "changed_count": len(changes),
        "changes": [c.to_dict() for c in changes],
        "material_changes": [c.to_dict() for c in high],
        "summary": (
            f"共 {len(changes)} 项变化，其中关键项 {len(high)} 项"
            if changes
            else "与上次研究相比没有变化"
        ),
        "upside_vs_price": {"before": _upside_values(previous), "after": _upside_values(current)},
    }


@dataclass(frozen=True)
class Trigger:
    name: str
    label: str
    fired: bool
    detail: str
    severity: str  # high / medium / low

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "fired": self.fired,
            "detail": self.detail,
            "severity": self.severity,
        }


def evaluate_triggers(
    run: dict,
    *,
    current_snapshot: dict,
    current_analysis: dict,
    today: date,
) -> list[Trigger]:
    """用当前快照与重算结果判断复核条件。``run`` 为冻结记录（``_detail()`` 形状）。"""
    triggers: list[Trigger] = []

    # 1) 新财报到达
    run_report = run.get("report_date")
    cur_report = current_snapshot.get("report_date")
    triggers.append(Trigger(
        name="new_report_period",
        label="新财报报告期",
        fired=bool(run_report and cur_report and run_report != cur_report),
        detail=f"记录时 {run_report or '未知'} → 当前 {cur_report or '未知'}",
        severity="high",
    ))

    # 2) 现价相对基准情景价值偏离
    base_value = None
    for scenario in ((run.get("analysis") or {}).get("5_scenarios") or {}).get("scenarios") or []:
        if scenario.get("label") == "基准":
            base_value = (scenario.get("result") or {}).get("per_share")
    price = current_snapshot.get("price")
    deviation = None
    if base_value and price:
        deviation = price / base_value - 1.0
    triggers.append(Trigger(
        name="price_deviation",
        label=f"现价相对基准价值偏离 ≥ ±{PRICE_DEVIATION_TRIGGER:.0%}",
        fired=bool(deviation is not None and abs(deviation) >= PRICE_DEVIATION_TRIGGER),
        detail=(f"基准价值 {base_value}，现价 {price}，偏离 {deviation:+.1%}"
                if deviation is not None else "缺少基准价值或现价，无法判断"),
        severity="high",
    ))

    # 3) 结论状态变化
    cur_conclusion = (current_analysis.get("1_conclusion") or {}).get("conclusion_key")
    triggers.append(Trigger(
        name="conclusion_changed",
        label="结论状态变化",
        fired=bool(cur_conclusion and cur_conclusion != run.get("conclusion_key")),
        detail=f"{run.get('conclusion_key')} → {cur_conclusion}",
        severity="high",
    ))

    # 4) 财报数据过期
    stale_days = None
    if run_report:
        try:
            stale_days = (today - date.fromisoformat(str(run_report))).days
        except ValueError:
            stale_days = None
    triggers.append(Trigger(
        name="report_expired",
        label=f"报告期滞后 > {STALE_REPORT_DAYS} 天",
        fired=bool(stale_days is not None and stale_days > STALE_REPORT_DAYS),
        detail=(f"滞后 {stale_days} 天" if stale_days is not None else "记录未含报告期"),
        severity="high",
    ))

    # 5) 研究记录陈旧
    created = str(run.get("snapshot_date") or "")
    run_age = None
    try:
        run_age = (today - date.fromisoformat(created)).days if created else None
    except ValueError:
        run_age = None
    triggers.append(Trigger(
        name="run_stale",
        label=f"研究记录超过 {STALE_RUN_DAYS} 天未更新",
        fired=bool(run_age is not None and run_age > STALE_RUN_DAYS),
        detail=(f"记录快照日 {created}，已过 {run_age} 天" if run_age is not None
                else "记录未含快照日"),
        severity="medium",
    ))
    return triggers


def build_review(
    run: dict,
    *,
    previous: dict | None,
    current_snapshot: dict,
    current_analysis: dict,
    today: date,
) -> dict:
    """合成复核结论：是否需要复核、哪些条件触发、与上次记录有哪些差异。"""
    triggers = evaluate_triggers(
        run,
        current_snapshot=current_snapshot,
        current_analysis=current_analysis,
        today=today,
    )
    fired = [t for t in triggers if t.fired]
    diff = diff_runs(previous, run) if previous else None
    if diff is None:
        # 没有上一次记录：只报"本次记录自身"的变化（用当前重算对照冻结记录）
        diff = diff_runs(run, {"analysis": current_analysis, "assumptions": run.get("assumptions"),
                               "price": current_snapshot.get("price"),
                               "report_date": current_snapshot.get("report_date"),
                               "snapshot_date": current_snapshot.get("snapshot_date"),
                               "conclusion_key": (current_analysis.get("1_conclusion") or {}).get("conclusion_key"),
                               "explanation_status": run.get("explanation_status")})
        diff["baseline"] = "no_previous_run"
    return {
        "needs_review": bool(fired),
        "fired_count": len(fired),
        "fired_triggers": [t.to_dict() for t in fired],
        "all_triggers": [t.to_dict() for t in triggers],
        "has_previous_run": previous is not None,
        "diff": diff,
        "note": (
            "触发条件只是'该重新看一遍'的提醒，不代表观点对错；"
            "研究记录是不可变的，新的判断请创建新记录并留下指纹"
        ),
    }
