"""企业质量：指标计算 + 分行业打分 + **证据置信度**（确定性纯函数，无 IO）。

三条硬规则（来自用户对系统的要求）：

1. **按行业选指标、权重可配置且有解释**：金融股没有"毛利率"可言，公用事业的高负债
   是常态、消费股的高负债才是风险。所以打分用**行业画像**（:class:`QualityProfile`），
   画像由显式关键词映射选择，选择理由随结果一起返回（``profile_reason``）。
2. **投资吸引力与证据置信度分开**：:func:`assess_quality` 给出"质量分"（吸引力的一部分），
   :func:`evidence_confidence` 单独给出"证据置信度"。**质量分不是上涨概率**，两者不得相乘
   包装成单一"买入分"。
3. **允许证据不足**：缺失指标不填 0（填 0 等于断言"很差"），而是从加权里剔除并把覆盖率
   记入置信度；覆盖率过低时直接给 ``insufficient_evidence=True``，由上层输出"暂不行动"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# ── 指标定义 ────────────────────────────────────────────────────────────────
#
# key → (中文名, 单位, 方向, 来源分类)
# 方向 +1 表示越大越好，-1 表示越小越好；来源分类说明这个指标是**事实**（财报直接给出）
# 还是**模型推断**（由财报数字计算得出），用于在输出里区分事实/估计/推断。
METRIC_SPECS: dict[str, tuple[str, str, int, str]] = {
    "roe": ("净资产收益率", "%", 1, "事实"),
    "gross_margin": ("毛利率", "%", 1, "事实"),
    "net_margin": ("销售净利率", "%", 1, "事实"),
    "debt_ratio": ("资产负债率", "%", -1, "事实"),
    "revenue_yoy": ("营业总收入同比", "%", 1, "事实"),
    "profit_yoy": ("归母净利润同比", "%", 1, "事实"),
    "ocf_to_profit": ("经营现金流/净利润", "倍", 1, "模型推断"),
    "goodwill_to_equity": ("商誉/净资产", "%", -1, "模型推断"),
}


@dataclass(frozen=True)
class QualityInputs:
    """质量打分的输入。全部为 ``None`` 表示上游没拿到，绝不用 0 代替。"""

    roe: float | None = None
    gross_margin: float | None = None
    net_margin: float | None = None
    debt_ratio: float | None = None
    revenue_yoy: float | None = None
    profit_yoy: float | None = None
    #: 经营现金流量净额 / 归母净利润（倍）。净利润为正而该值长期 < 0.5 是盈利质量警示
    ocf_to_profit: float | None = None
    #: 商誉 / 股东权益（%）
    goodwill_to_equity: float | None = None
    #: 财报报告期（数据时点）
    report_date: date | None = None
    #: 本次分析日期（用于计算报告期滞后天数）
    as_of: date | None = None
    industry: str | None = None
    currency: str = "CNY"
    source: str = ""

    def values(self) -> dict[str, float | None]:
        return {key: getattr(self, key) for key in METRIC_SPECS}


#: 报告期滞后超过该天数即视为**过期**，上层应停止给出判断而不是"降权后照常推荐"
STALE_LIMIT_DAYS = 270
#: 覆盖率低于该比例视为证据不足
MIN_COVERAGE = 0.5


@dataclass(frozen=True)
class QualityProfile:
    """行业画像：权重 + 好坏阈值。权重之和不必为 1（会按实际可用指标重新归一）。"""

    key: str
    label: str
    #: 指标 → 权重
    weights: dict[str, float]
    #: 指标 → (差, 好)：线性斜坡端点，方向由 ``METRIC_SPECS`` 决定
    thresholds: dict[str, tuple[float, float]]
    excluded: tuple[str, ...] = ()

    def explain(self) -> str:
        parts = [f"{METRIC_SPECS[k][0]} {self.weights[k]:.0%}" for k in self.weights]
        text = f"{self.label}画像：" + "、".join(parts)
        if self.excluded:
            names = "、".join(METRIC_SPECS[k][0] for k in self.excluded if k in METRIC_SPECS)
            text += f"；本画像**不使用** {names}（该行业口径下无意义或无区分度）"
        return text


GENERAL = QualityProfile(
    key="general",
    label="通用（制造/消费/服务）",
    weights={
        "roe": 0.24,
        "gross_margin": 0.14,
        "net_margin": 0.10,
        "debt_ratio": 0.14,
        "revenue_yoy": 0.14,
        "profit_yoy": 0.10,
        "ocf_to_profit": 0.10,
        "goodwill_to_equity": 0.04,
    },
    thresholds={
        "roe": (3.0, 15.0),
        "gross_margin": (10.0, 40.0),
        "net_margin": (1.0, 15.0),
        "debt_ratio": (75.0, 40.0),
        "revenue_yoy": (-10.0, 15.0),
        "profit_yoy": (-20.0, 20.0),
        "ocf_to_profit": (0.3, 1.0),
        "goodwill_to_equity": (30.0, 5.0),
    },
)

FINANCIAL = QualityProfile(
    key="financial",
    label="金融（银行/保险/证券）",
    weights={"roe": 0.34, "net_margin": 0.18, "revenue_yoy": 0.18, "profit_yoy": 0.18, "ocf_to_profit": 0.12},
    thresholds={
        "roe": (5.0, 14.0),
        "net_margin": (10.0, 35.0),
        "revenue_yoy": (-5.0, 12.0),
        "profit_yoy": (-10.0, 15.0),
        "ocf_to_profit": (0.2, 0.9),
    },
    excluded=("gross_margin", "debt_ratio", "goodwill_to_equity"),
)

UTILITY = QualityProfile(
    key="utility",
    label="公用事业/基础设施",
    weights={
        "roe": 0.20,
        "net_margin": 0.14,
        "debt_ratio": 0.22,
        "revenue_yoy": 0.14,
        "profit_yoy": 0.14,
        "ocf_to_profit": 0.12,
        "goodwill_to_equity": 0.04,
    },
    thresholds={
        "roe": (3.0, 12.0),
        "net_margin": (3.0, 20.0),
        "debt_ratio": (80.0, 55.0),
        "revenue_yoy": (-5.0, 10.0),
        "profit_yoy": (-10.0, 15.0),
        "ocf_to_profit": (0.5, 1.2),
        "goodwill_to_equity": (30.0, 5.0),
    },
    excluded=("gross_margin",),
)

GROWTH = QualityProfile(
    key="growth",
    label="科技/医药成长",
    weights={
        "roe": 0.14,
        "gross_margin": 0.22,
        "net_margin": 0.10,
        "debt_ratio": 0.08,
        "revenue_yoy": 0.22,
        "profit_yoy": 0.16,
        "ocf_to_profit": 0.08,
    },
    thresholds={
        "roe": (3.0, 18.0),
        "gross_margin": (20.0, 60.0),
        "net_margin": (1.0, 25.0),
        "debt_ratio": (70.0, 35.0),
        "revenue_yoy": (0.0, 30.0),
        "profit_yoy": (-10.0, 40.0),
        "ocf_to_profit": (0.3, 1.0),
    },
    excluded=("goodwill_to_equity",),
)

PROFILES: dict[str, QualityProfile] = {
    p.key: p for p in (GENERAL, FINANCIAL, UTILITY, GROWTH)
}

#: 行业关键词 → 画像。顺序敏感：先匹配到的先返回（所以先放更具体的金融/公用事业）。
#: 关键词来自东财行情接口的 ``f100`` 所属行业（如"白色家电""银行""电力行业"）。
PROFILE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("financial", ("银行", "保险", "证券", "多元金融", "信托")),
    ("utility", ("电力", "燃气", "水务", "公用事业", "港口", "高速公路", "机场", "铁路", "航空")),
    ("growth", ("软件", "半导体", "芯片", "生物", "医药", "医疗", "疫苗", "新能源",
                "电池", "光伏", "通信", "电子", "计算机", "互联网", "军工", "航天")),
)


def profile_for(industry: str | None) -> tuple[QualityProfile, str]:
    """按行业选画像，并返回**选择理由**（未识别时如实说明）。"""
    if not industry:
        return GENERAL, "行业字段缺失，按通用画像处理（这是置信度折扣项）"
    for key, keywords in PROFILE_KEYWORDS:
        for keyword in keywords:
            if keyword in industry:
                return PROFILES[key], f"行业「{industry}」命中关键词「{keyword}」→ {PROFILES[key].label}"
    return GENERAL, f"行业「{industry}」未命中任何专用画像 → 通用画像"


def _ramp(value: float, low: float, high: float, direction: int) -> float:
    """线性斜坡到 0~100。``direction=-1`` 时 low>high（越小越好）。"""
    if direction > 0:
        span = high - low
        ratio = 0.0 if span == 0 else (value - low) / span
    else:
        span = low - high
        ratio = 0.0 if span == 0 else (low - value) / span
    return max(0.0, min(1.0, ratio)) * 100.0


@dataclass(frozen=True)
class QualityAssessment:
    score: float | None
    grade: str
    profile_key: str
    profile_label: str
    profile_reason: str
    profile_explain: str
    subscores: dict[str, dict] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    coverage: float = 0.0
    insufficient_evidence: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "profile_key": self.profile_key,
            "profile_label": self.profile_label,
            "profile_reason": self.profile_reason,
            "profile_explain": self.profile_explain,
            "subscores": self.subscores,
            "missing": list(self.missing),
            "coverage": self.coverage,
            "insufficient_evidence": self.insufficient_evidence,
            "notes": list(self.notes),
        }


def _grade(score: float | None, insufficient: bool) -> str:
    if insufficient or score is None:
        return "证据不足"
    if score >= 75:
        return "较强"
    if score >= 55:
        return "中性偏强"
    if score >= 40:
        return "中性"
    return "偏弱"


def assess_quality(inputs: QualityInputs) -> QualityAssessment:
    """按行业画像给企业质量打分（0~100）。缺失指标被剔除，不填 0。"""
    profile, reason = profile_for(inputs.industry)
    values = inputs.values()
    subscores: dict[str, dict] = {}
    weighted = 0.0
    weight_sum = 0.0
    relevant = [k for k in profile.weights if k not in profile.excluded]

    for key in relevant:
        spec = METRIC_SPECS[key]
        value = values.get(key)
        low, high = profile.thresholds[key]
        weight = profile.weights[key]
        if value is None:
            subscores[key] = {
                "label": spec[0],
                "unit": spec[1],
                "origin": spec[3],
                "value": None,
                "score": None,
                "weight": weight,
                "direction": "越大越好" if spec[2] > 0 else "越小越好",
                "note": "上游缺失，未计入加权（不计为 0 分）",
            }
            continue
        sub = _ramp(value, low, high, spec[2])
        weighted += sub * weight
        weight_sum += weight
        subscores[key] = {
            "label": spec[0],
            "unit": spec[1],
            "origin": spec[3],
            "value": value,
            "score": round(sub, 1),
            "weight": weight,
            "threshold_poor": low,
            "threshold_good": high,
            "direction": "越大越好" if spec[2] > 0 else "越小越好",
        }

    missing = tuple(k for k in relevant if values.get(k) is None)
    coverage = 1.0 - len(missing) / len(relevant) if relevant else 0.0
    score = round(weighted / weight_sum, 1) if weight_sum > 0 else None
    insufficient = coverage < MIN_COVERAGE or score is None

    notes: list[str] = []
    if profile.excluded:
        excluded_names = "、".join(METRIC_SPECS[k][0] for k in profile.excluded)
        notes.append(f"按 {profile.label} 口径，{excluded_names} 不参与打分")
    if missing:
        notes.append("缺失指标：" + "、".join(METRIC_SPECS[k][0] for k in missing))
    if inputs.ocf_to_profit is not None and inputs.profit_yoy is not None:
        if inputs.ocf_to_profit < 0.5 and (inputs.profit_yoy or 0) > 20:
            notes.append("净利润高增长但经营现金流覆盖不足（<0.5 倍）→ 盈利质量存疑")
    if insufficient:
        notes.append("可用指标不足或全部缺失 → 判定「证据不足」，不给出质量结论")
    notes.append("质量分衡量的是财务特征，**不是上涨概率**，与「证据置信度」是两个独立维度")
    return QualityAssessment(
        score=score if not insufficient else None,
        grade=_grade(score, insufficient),
        profile_key=profile.key,
        profile_label=profile.label,
        profile_reason=reason,
        profile_explain=profile.explain(),
        subscores=subscores,
        missing=missing,
        coverage=round(coverage, 3),
        insufficient_evidence=insufficient,
        notes=tuple(notes),
    )


# ── 证据置信度（与上面的"吸引力"完全独立） ─────────────────────────────────


@dataclass(frozen=True)
class ConfidenceAssessment:
    score: float
    label: str
    expired: bool
    components: dict[str, float]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "label": self.label,
            "expired": self.expired,
            "components": self.components,
            "reasons": list(self.reasons),
            "note": "置信度衡量「本次判断的数据基础有多可靠」，**不是上涨概率**，也不参与排名加权",
        }


#: 报告期滞后天数 → 时效分（线性到 0）
FRESHNESS_FULL_DAYS = 120


def evidence_confidence(
    *,
    coverage: float,
    report_date: date | None,
    as_of: date | None,
    industry_identified: bool,
    has_statement_detail: bool = False,
    sources: int = 1,
    stale_limit_days: int = STALE_LIMIT_DAYS,
) -> ConfidenceAssessment:
    """证据置信度（0~100）。

    四个分量：①指标覆盖率 ②报告期时效 ③行业画像是否识别 ④数据源/明细丰富度。
    报告期滞后超过 ``stale_limit_days`` 时 ``expired=True``，上层必须停止给出判断
    （而不是"降权后照常推荐"）。
    """
    reasons: list[str] = []
    coverage_score = max(0.0, min(1.0, coverage)) * 100.0 * 0.40

    stale_days: int | None = None
    if report_date is None or as_of is None:
        stale_score = 0.0
        reasons.append("报告期或分析日期缺失 → 时效分记 0")
    else:
        stale_days = (as_of - report_date).days
        if stale_days <= FRESHNESS_FULL_DAYS:
            stale_score = 100.0 * 0.30
        else:
            ratio = max(0.0, min(1.0, (stale_limit_days - stale_days) /
                                (stale_limit_days - FRESHNESS_FULL_DAYS)))
            stale_score = ratio * 100.0 * 0.30
            reasons.append(f"报告期滞后 {stale_days} 天，时效分按比例衰减")

    industry_score = (100.0 if industry_identified else 45.0) * 0.20
    if not industry_identified:
        reasons.append("行业未识别 → 只能套用通用画像，指标选择与阈值未必适用")

    richness = 0.5 + (0.3 if has_statement_detail else 0.0) + min(sources - 1, 1) * 0.2
    richness_score = max(0.0, min(1.0, richness)) * 100.0 * 0.10
    if not has_statement_detail:
        reasons.append("仅用到行情接口的汇总财务字段，未接入三表明细（资本开支/有息负债不可得）")

    total = round(coverage_score + stale_score + industry_score + richness_score, 1)
    expired = stale_days is not None and stale_days > stale_limit_days
    if expired:
        reasons.append(f"报告期滞后 {stale_days} 天 > {stale_limit_days} 天 → **数据过期，停止判断**")
    label = "过期" if expired else ("较高" if total >= 75 else "中等" if total >= 50 else "偏低")
    return ConfidenceAssessment(
        score=total,
        label=label,
        expired=expired,
        components={
            "coverage": round(coverage_score, 1),
            "freshness": round(stale_score, 1),
            "industry": round(industry_score, 1),
            "richness": round(richness_score, 1),
            "stale_days": -1 if stale_days is None else float(stale_days),
        },
        reasons=tuple(reasons),
    )
