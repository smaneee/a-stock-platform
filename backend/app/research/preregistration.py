"""实验预登记与多重比较校正（P0-05 的 D10）。

为什么需要（研发计划 §5.3.1 / §11.6）：

> 所有策略登记唯一版本：代码提交、参数、数据截止日、股票池快照、复权口径、费用、
> 滑点、结果文件。
> 预登记准入判据。在新留出揭盲前固定主基准、主持有期、成本、风险预算、试验数量及
> 多重比较处理。
> 参数实验预登记，最终未看留出只揭盲一次。

D10 缺口：`param_search.py` 每次运行都会打印留出段结果（留出可反复查看），且
`validation.py` 自述未做多重比较校正 —— 等于没有「只看一次」的纪律，也没有把
「试了多少次」计入显著性。

本模块提供三件事：

1. **预登记载体**：一份冻结产物（含内容哈希），字段齐全才允许登记；
2. **一次性揭盲**：同一实验只允许揭盲一次，第二次调用被明确拒绝并给出首次时间与哈希；
3. **多重比较校正**：Benjamini-Hochberg（FDR）与 Bonferroni，用真实试验次数校正阈值。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 允许的多重比较方法（"none" 只用于对照，登记时会给出警告）
MULTIPLE_COMPARISON_METHODS = ("benjamini_hochberg", "bonferroni", "none")

#: 冻结后不允许再改的字段（改了就等于事后挑选判据）
REQUIRED_FIELDS = (
    "experiment_id",
    "hypothesis",
    "strategy_version",
    "data_cutoff",
    "universe_scope",
    "primary_horizon_days",
    "primary_benchmark",
    "costs",
    "risk_budget",
    "decision_criteria",
    "planned_trials",
    "multiple_comparison",
)

#: 不参与内容哈希的字段（登记后允许变化，或属于派生字段）
VOLATILE_FIELDS = (
    "frozen_hash",
    "unblinded_at",
    "unblind_count",
    "notes_history",
    "integrity_ok",
)


def canonical_hash(payload: dict[str, Any]) -> str:
    """冻结内容哈希：去掉易变字段后按规范化 JSON 计算 SHA-256。"""
    frozen = {k: v for k, v in payload.items() if k not in VOLATILE_FIELDS}
    blob = json.dumps(frozen, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_preregistration(payload: dict[str, Any]) -> None:
    """校验预登记字段完整性；不完整直接报错，不允许「先跑再说」。"""
    missing = [name for name in REQUIRED_FIELDS if not payload.get(name)]
    if missing:
        raise ValueError(f"预登记缺少必需字段：{', '.join(missing)}")
    if payload["multiple_comparison"] not in MULTIPLE_COMPARISON_METHODS:
        raise ValueError(
            f"multiple_comparison 非法：{payload['multiple_comparison']!r}"
            f"（可选 {', '.join(MULTIPLE_COMPARISON_METHODS)}）"
        )
    trials = payload["planned_trials"]
    if not isinstance(trials, int) or trials < 1:
        raise ValueError("planned_trials 必须是 >=1 的整数（实验次数要计入多重比较）")
    if payload["multiple_comparison"] != "none" and trials < 2:
        raise ValueError("planned_trials >= 2 时才能声明多重比较方法")
    try:
        date.fromisoformat(str(payload["data_cutoff"])[:10])
    except ValueError as exc:
        raise ValueError(f"data_cutoff 必须是有效日期：{payload['data_cutoff']!r}") from exc
    criteria = payload["decision_criteria"]
    if not isinstance(criteria, dict) or not criteria:
        raise ValueError("decision_criteria 必须是非空对象（预先写死准入判据）")
    # 判据里必须写明「净超额置信区间下界」这类可判定条件，避免事后解释
    if not any("excess" in key or "超额" in key for key in criteria):
        raise ValueError("decision_criteria 必须包含超额收益相关的准入判据")


@dataclass
class Preregistration:
    """一份冻结的实验预登记。"""

    payload: dict[str, Any]
    frozen_hash: str
    unblinded_at: str | None = None
    unblind_count: int = 0
    notes_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def freeze(cls, payload: dict[str, Any], now: datetime | None = None) -> "Preregistration":
        validate_preregistration(payload)
        data = dict(payload)
        return cls(
            payload=data,
            frozen_hash=canonical_hash(data),
        )

    @property
    def experiment_id(self) -> str:
        return str(self.payload["experiment_id"])

    def integrity_ok(self) -> bool:
        """冻结内容是否被改过（哈希不匹配即视为事后改判据）。"""
        return canonical_hash(self.payload) == self.frozen_hash

    def unblind(self, now: datetime | None = None, note: str = "") -> dict[str, Any]:
        """一次性揭盲：第二次调用被拒绝。"""
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        if self.unblind_count > 0:
            return {
                "allowed": False,
                "reason": (
                    f"实验 {self.experiment_id} 已于 {self.unblinded_at} 揭盲；"
                    "留出段只允许揭盲一次，重复查看会使样本外结论失效"
                ),
                "unblind_count": self.unblind_count,
                "frozen_hash": self.frozen_hash,
                "first_unblinded_at": self.unblinded_at,
            }
        self.unblind_count = 1
        self.unblinded_at = stamp
        self.notes_history.append({"at": stamp, "note": note or "首次揭盲"})
        return {
            "allowed": True,
            "experiment_id": self.experiment_id,
            "unblinded_at": stamp,
            "frozen_hash": self.frozen_hash,
            "planned_trials": self.payload.get("planned_trials"),
            "multiple_comparison": self.payload.get("multiple_comparison"),
            "integrity_ok": self.integrity_ok(),
        }

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.payload)
        data.update(
            {
                "frozen_hash": self.frozen_hash,
                "unblinded_at": self.unblinded_at,
                "unblind_count": self.unblind_count,
                "notes_history": self.notes_history,
                "integrity_ok": self.integrity_ok(),
            }
        )
        return data

    # ──────── 持久化（版本化 JSON） ────────

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "Preregistration":
        raw = json.loads(path.read_text(encoding="utf-8"))
        record = cls(
            payload={k: v for k, v in raw.items() if k not in VOLATILE_FIELDS},
            frozen_hash=str(raw.get("frozen_hash", "")),
            unblinded_at=raw.get("unblinded_at"),
            unblind_count=int(raw.get("unblind_count") or 0),
            notes_history=list(raw.get("notes_history") or []),
        )
        if not record.integrity_ok():
            raise ValueError(
                f"预登记 {record.experiment_id} 的冻结内容与哈希不一致：判据可能被事后修改"
            )
        return record


# ───────────── 多重比较校正 ─────────────


def benjamini_hochberg(
    pvalues: Sequence[float], alpha: float = 0.05
) -> dict[str, Any]:
    """Benjamini-Hochberg FDR 校正。

    返回被拒绝的原始下标、临界 p 值、以及每个假设的 q 值（单调化后的调整 p 值）。
    手算对照（m=15, alpha=0.05）：只有最小的 p=0.001 会通过
    （0.001 <= 1/15*0.05=0.00333，而 0.008 > 2/15*0.05=0.00667）。
    """
    m = len(pvalues)
    if m == 0:
        return {"method": "benjamini_hochberg", "alpha": alpha, "m": 0,
                "rejected_indices": [], "qvalues": [], "critical_p": None}
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须在 (0,1) 区间")
    for p in pvalues:
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"p 值必须在 [0,1]：{p!r}")

    order = sorted(range(m), key=lambda i: pvalues[i])
    sorted_p = [pvalues[i] for i in order]

    critical: float | None = None
    k_max = 0
    for rank, p in enumerate(sorted_p, start=1):
        if p <= rank / m * alpha:
            k_max = rank
            critical = p
    rejected_sorted = set(range(1, k_max + 1))

    # q 值：从大到小取 min(m/rank * p)
    q_sorted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        value = sorted_p[rank - 1] * m / rank
        running = min(running, value)
        q_sorted[rank - 1] = min(1.0, running)

    qvalues = [0.0] * m
    rejected: list[int] = []
    for rank, original_index in enumerate(order, start=1):
        qvalues[original_index] = q_sorted[rank - 1]
        if rank in rejected_sorted:
            rejected.append(original_index)

    return {
        "method": "benjamini_hochberg",
        "alpha": alpha,
        "m": m,
        "rejected_indices": sorted(rejected),
        "rejected_count": len(rejected),
        "critical_p": critical,
        "qvalues": qvalues,
        "note": (
            "按真实试验次数 m 校正：未通过校正的结果不得表述为显著"
            if m > 1
            else "只登记了 1 次试验；仍须声明未做多重比较校正"
        ),
    }


def bonferroni(pvalues: Sequence[float], alpha: float = 0.05) -> dict[str, Any]:
    """Bonferroni 校正：阈值 alpha/m（比 BH 更保守）。"""
    m = len(pvalues)
    if m == 0:
        return {"method": "bonferroni", "alpha": alpha, "m": 0,
                "rejected_indices": [], "adjusted": [], "critical_p": None}
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须在 (0,1) 区间")
    threshold = alpha / m
    rejected = [i for i, p in enumerate(pvalues) if p <= threshold]
    return {
        "method": "bonferroni",
        "alpha": alpha,
        "m": m,
        "threshold": threshold,
        "critical_p": max((pvalues[i] for i in rejected), default=None),
        "rejected_indices": rejected,
        "rejected_count": len(rejected),
        "adjusted": [min(1.0, p * m) for p in pvalues],
    }


def adjust_pvalues(
    pvalues: Sequence[float], method: str = "benjamini_hochberg", alpha: float = 0.05
) -> dict[str, Any]:
    """按登记的方法做多重比较校正；method="none" 会明确标注未校正。"""
    if method == "benjamini_hochberg":
        return benjamini_hochberg(pvalues, alpha)
    if method == "bonferroni":
        return bonferroni(pvalues, alpha)
    if method == "none":
        return {
            "method": "none",
            "alpha": alpha,
            "m": len(pvalues),
            "rejected_indices": [i for i, p in enumerate(pvalues) if p <= alpha],
            "rejected_count": sum(1 for p in pvalues if p <= alpha),
            "warning": "未做多重比较校正：多次试验会抬高假阳性率，结论只能作为探索性结果",
        }
    raise ValueError(f"未知的多重比较方法：{method!r}")


def summarize_correction(result: dict[str, Any]) -> str:
    """把校正结果转成一句可直接放进报告的话。"""
    method = result.get("method")
    if method == "none":
        return str(result.get("warning", "未做多重比较校正"))
    m = result.get("m", 0)
    rejected = result.get("rejected_count", 0)
    name = {"benjamini_hochberg": "Benjamini-Hochberg FDR", "bonferroni": "Bonferroni"}.get(
        str(method), str(method)
    )
    return (
        f"{name} 校正（m={m} 次试验，alpha={result.get('alpha')}）："
        f"{rejected}/{m} 个假设通过校正"
    )
