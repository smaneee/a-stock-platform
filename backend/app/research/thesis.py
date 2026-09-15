"""结构化研究决策卡（S1：证据绑定与结构化论点）。

方案要求「不接受自由文本代替结构校验」，因此这里用 Pydantic 定义**唯一**的决策卡结构，
字段与 ``docs/DeepSeek-投资策略思考升级执行方案.md`` §二「建议结构化输出」一致：

``strategy_type / horizon / as_of / thesis / variant_view / supporting_evidence_ids /
opposing_evidence_ids / assumptions / valuation_method / scenario_result_ids /
invalidation_conditions / review_triggers / missing_data / decision / confidence_basis /
model_version / prompt_version``

设计要点（都可被测试固定）：

1. **卡片骨架由代码生成**：策略类型、期限、四个维度、假设、情景结果、失效条件、复核触发、
   缺失数据、模型版本全部来自确定性计算；模型只负责 ``thesis``（论点）与 ``variant_view``
   （预期差）两段**文字**，且这两段文字必须通过证据 id 与数字双重校验，否则整卡降级。
2. **引用必须存在**：``supporting_evidence_ids`` / ``opposing_evidence_ids`` 里的每个 id
   都要能在冻结证据包里找到；引用不存在的 id → 卡片标记 ``citations_valid=False``，
   并且**不允许**作为增仓依据（见 :func:`position_gate`）。
3. **缺失数据就是缺失**：``missing_data`` 显式列出缺哪些字段；关键字段缺失时
   :func:`position_gate` 直接拦掉增仓，不用"其它指标看起来不错"来抵。
4. **不随措辞变化**：卡片里的数值全部取自证据包，模型措辞变化不会改变卡片数值（有测试固定）。
"""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.explain.deepseek import (  # noqa: F401 - EvidencePack 作为类型来源
    NUMBER_RE,
    EvidencePack,
    validate_citations,
)

#: 研究策略类型（方案 §一.1：不得用长期估值结论为短线亏损事后辩护）
class StrategyType(str, Enum):
    QUALITY_VALUE = "quality_value"          # 长期质量价值
    CYCLICAL_RECOVERY = "cyclical_recovery"  # 周期修复
    EVENT_DRIVEN = "event_driven"            # 事件研究
    TREND = "trend"                          # 趋势研究


STRATEGY_LABELS: dict[StrategyType, str] = {
    StrategyType.QUALITY_VALUE: "长期质量价值",
    StrategyType.CYCLICAL_RECOVERY: "周期修复",
    StrategyType.EVENT_DRIVEN: "事件研究",
    StrategyType.TREND: "趋势研究",
}

#: 各策略类型的默认持有期限与收益来源（写死可测试，不随行情漂移）
STRATEGY_HORIZON: dict[StrategyType, str] = {
    StrategyType.QUALITY_VALUE: "3 年以上",
    StrategyType.CYCLICAL_RECOVERY: "1~2 年（跟随周期）",
    StrategyType.EVENT_DRIVEN: "事件窗口（不超过 6 个月）",
    StrategyType.TREND: "数周~数月（趋势破坏即退出）",
}
STRATEGY_RETURN_SOURCE: dict[StrategyType, str] = {
    StrategyType.QUALITY_VALUE: "企业盈利增长与估值修复",
    StrategyType.CYCLICAL_RECOVERY: "周期底部盈利正常化",
    StrategyType.EVENT_DRIVEN: "事件兑现带来的预期修正",
    StrategyType.TREND: "价格动量与资金行为（**不依赖基本面估值**）",
}


class Decision(str, Enum):
    """允许的结论状态（方案 §一.8：允许"资料不足/观察/等待验证"）。"""

    INSUFFICIENT_DATA = "insufficient_data"      # 资料不足
    WATCH = "watch"                              # 观察
    AWAIT_VALIDATION = "await_validation"        # 等待验证
    RESEARCH_CANDIDATE = "research_candidate"    # 研究候选
    NOT_APPLICABLE = "not_applicable"            # 现有方法不适用


DECISION_LABELS: dict[Decision, str] = {
    Decision.INSUFFICIENT_DATA: "资料不足：暂不行动",
    Decision.WATCH: "观察：先跟踪再看",
    Decision.AWAIT_VALIDATION: "等待关键验证",
    Decision.RESEARCH_CANDIDATE: "研究候选（非买入建议）",
    Decision.NOT_APPLICABLE: "现有估值方法不适用",
}

#: 结论状态 → 是否需要复核（等待验证/观察/资料不足都要回来看）
NEEDS_REVIEW: dict[Decision, bool] = {
    Decision.INSUFFICIENT_DATA: True,
    Decision.WATCH: True,
    Decision.AWAIT_VALIDATION: True,
    Decision.RESEARCH_CANDIDATE: True,
    Decision.NOT_APPLICABLE: True,
}

#: 增仓门禁视为**关键**的缺失项（命中任意一条即不允许增仓）
CRITICAL_MISSING_KEYS = (
    "现价",
    "财报报告期",
    "显式估值假设（增长率/自由现金流率/折现率/永续增长率/股本）",
)

#: 证据 id 形如 fact:roe / calc:dcf_基准
EVIDENCE_ID_RE = re.compile(r"\b(?:fact|calc):[A-Za-z0-9_\u4e00-\u9fff]+")

PROMPT_VERSION = "investment-thesis-s1"
MODEL_VERSION = "fundamentals-decision-v1"

#: 反方审查提示词（两处共用，避免口径漂移）。
#: 2026-09-15 实测教训：原提示词没有要求标注证据 id，导致 `opposing_evidence_ids` 恒为空，
#: 卡片里"反对证据"一栏只能空着。现在把"每条反对意见必须带真实存在的 id"写成硬性格式要求。
CRITIC_PROMPT = (
    "你是独立反方投资委员。不要迎合既有结论；优先找出会导致永久损失、估值失真或论点"
    "失效的证据，并明确当前证据无法回答什么。\n"
    "硬性格式要求（不满足即视为不合格）：至少写出 3 条反对意见，**每条都必须在句末标注"
    "它所依据的证据 id**（形如 fact:roe 或 calc:dcf_基准），id 只能取证据包里真实存在的；"
    "没有证据可依的意见必须写明「证据不足，无法判断」，不得凭空给结论。"
)


class EvidenceRef(BaseModel):
    """一条证据的溯源信息（方案 §二：保存 id、来源、口径、单位、报告期、公告/抓取时间）。"""

    model_config = ConfigDict(extra="ignore")

    evidence_id: str
    kind: str = Field(description="fact=财报/行情直接给出；calc=平台计算得出")
    label: str | None = None
    value: float | None = None
    unit: str | None = None
    source: str | None = None
    period: str | None = Field(default=None, description="报告期（数据时点）")
    published_at: str | None = Field(default=None, description="公告时间（若可得）")
    fetched_at: str | None = Field(default=None, description="抓取时间")
    available_at: str | None = Field(default=None, description="可用截止时间（时点管理用）")


class ThesisCard(BaseModel):
    """研究决策卡：一次研究的**结构化**结论。数值全部来自确定性计算。"""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str = ""
    strategy_type: StrategyType
    strategy_label: str
    horizon: str
    return_source: str
    as_of: str = Field(description="数据截止（报告期 + 抓取时间）")
    #: 模型生成的两段文字（可为空：模型不可用时卡片骨架仍然完整）
    thesis: str = ""
    variant_view: str = ""
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    opposing_evidence_ids: list[str] = Field(default_factory=list)
    assumptions: dict = Field(default_factory=dict)
    valuation_method: dict = Field(default_factory=dict)
    scenario_result_ids: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    review_triggers: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    decision: Decision
    decision_label: str
    confidence_basis: dict = Field(default_factory=dict)
    model_version: str = MODEL_VERSION
    prompt_version: str = PROMPT_VERSION
    #: 引用与数字校验结果（引用不存在即为 False）
    citations_valid: bool = True
    citation_report: dict = Field(default_factory=dict)
    #: 结构性缺口（例如反方意见没标 id）——显式列出，不靠"反证为空"让人猜
    gaps: list[str] = Field(default_factory=list)
    #: 模型调用成本与耗时（方案 §四 S1 验收要求记录）
    model_usage: dict = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    text_origin: str = "deterministic_skeleton"


def _evidence_index(pack: EvidencePack) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in pack.facts:
        index[str(item.get("id"))] = {**item, "kind": "fact"}
    for item in pack.calculations:
        index[str(item.get("id"))] = {**item, "kind": "calc"}
    return index


def extract_evidence_ids(*texts: str) -> list[str]:
    """从文字里抓出所有证据 id（保持出现顺序、去重）。"""
    seen: list[str] = []
    for text in texts:
        for match in EVIDENCE_ID_RE.findall(text or ""):
            if match not in seen:
                seen.append(match)
    return seen


def evidence_refs_for(ids: list[str], index: dict[str, dict], *, period: str | None,
                      fetched_at: str | None) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for evidence_id in ids:
        item = index.get(evidence_id)
        if item is None:
            continue
        refs.append(
            EvidenceRef(
                evidence_id=evidence_id,
                kind=str(item.get("kind") or "fact"),
                label=item.get("label"),
                value=item.get("value") if isinstance(item.get("value"), (int, float)) else None,
                unit=item.get("unit"),
                source=item.get("source"),
                period=period,
                published_at=None,
                fetched_at=fetched_at,
                available_at=None,
            )
        )
    return refs


def validate_evidence_ids(ids: list[str], pack: EvidencePack) -> dict:
    """校验证据 id 是否存在（方案 §四 S1 验收：引用不存在即失败）。"""
    known = set(_evidence_index(pack))
    invalid = [evidence_id for evidence_id in ids if evidence_id not in known]
    return {
        "checked_ids": len(ids),
        "known_ids": len(known),
        "invalid_ids": invalid,
        "passed": not invalid,
    }


def classify_strategy(analysis: dict, requested: StrategyType | None = None) -> StrategyType:
    """判断研究类型。默认长期质量价值；由用户显式覆盖时以用户为准。

    刻意**不自动推断**成趋势/周期：方案 §一.1 要求策略类型必须显式声明，
    自动猜测会让"用长期估值结论替短线亏损辩护"重新变得容易。
    """
    return requested or StrategyType.QUALITY_VALUE


def decide(analysis: dict) -> Decision:
    """把统一分析的结论键映射到允许的**研究状态**（不强迫给出买入意见）。"""
    key = str((analysis.get("1_conclusion") or {}).get("conclusion_key") or "")
    return {
        "insufficient": Decision.INSUFFICIENT_DATA,
        "expired": Decision.INSUFFICIENT_DATA,
        "model_not_applicable": Decision.NOT_APPLICABLE,
        "need_valuation": Decision.AWAIT_VALIDATION,
        "fair": Decision.WATCH,
        "attractive": Decision.RESEARCH_CANDIDATE,
        "overvalued": Decision.WATCH,
    }.get(key, Decision.WATCH)


def position_gate(card: ThesisCard, *, current_weight: float | None = None) -> dict:
    """**增仓门禁**：关键数据缺失 / 引用无效 / 方法不适用 / 资料不足时，不允许增仓。

    方案 §四 S1 验收：「关键数据缺失不能通过增仓门禁」；§四 S4 验收：
    「模型失败不会解锁增仓」。这里给出可测试的确定性规则 —— 任一阻断项命中即
    ``allowed=False``，并逐条说明原因（不合并成一句"风险"）。
    """
    blockers: list[str] = []
    if not card.citations_valid:
        blockers.append(
            "解释引用了不存在的证据 id："
            + "、".join(card.citation_report.get("invalid_ids") or [])
        )
    critical_missing = [item for item in card.missing_data if item in CRITICAL_MISSING_KEYS]
    if critical_missing:
        blockers.append("关键数据缺失：" + "、".join(critical_missing))
    if card.decision in (Decision.INSUFFICIENT_DATA, Decision.NOT_APPLICABLE):
        blockers.append(f"研究状态为「{card.decision_label}」")
    if not card.valuation_method.get("applicable", False):
        blockers.append(
            "估值方法不适用于该行业：" + str(card.valuation_method.get("caveat") or "")
        )
    if card.text_origin.startswith("model") and not card.citations_valid:
        blockers.append("模型输出未通过引用校验，不得作为增仓依据")
    return {
        "allowed": not blockers,
        "blockers": blockers,
        "note": (
            "门禁只看数据与校验结果，不看模型语气；缺数据时不会因为「其它指标看起来不错」而放行"
        ),
        "current_weight": current_weight,
    }


def build_thesis_card(
    analysis: dict,
    pack: EvidencePack,
    *,
    strategy_type: StrategyType | None = None,
    horizon: str | None = None,
    model_text: str = "",
    variant_text: str = "",
    model_usage: dict | None = None,
    fetched_at: str | None = None,
    text_origin: str = "deterministic_skeleton",
) -> ThesisCard:
    """组装研究决策卡。模型文字可选；引用校验不通过时卡片仍然生成，但标记为无效。"""
    index = _evidence_index(pack)
    strategy = classify_strategy(analysis, strategy_type)
    report_date = (analysis.get("2_data_asof") or {}).get("report_date")
    snapshot_date = str(fetched_at or "")

    supporting = extract_evidence_ids(model_text) or [
        item["id"] for item in pack.facts[:3]
    ]
    opposing = extract_evidence_ids(variant_text)
    # 支持/反对证据 id 去重且不重叠
    opposing = [item for item in opposing if item not in supporting]

    id_report = validate_evidence_ids(supporting + opposing, pack)
    number_report = validate_citations(model_text + "\n" + variant_text, pack)
    # 反方意见必须能回指至少一条证据；否则它只是没有依据的修辞，不能进入
    # 冻结卡片或作为增仓依据。确定性骨架没有反方文字时不触发此规则。
    opposing_binding_missing = bool(variant_text.strip()) and not opposing
    citations_valid = bool(
        id_report["passed"] and number_report["passed"] and not opposing_binding_missing
    )

    quality = ((analysis.get("3_dimensions") or {}).get("quality")) or {}
    valuation = ((analysis.get("3_dimensions") or {}).get("valuation")) or {}
    completeness = (analysis.get("2_data_asof") or {}).get("completeness") or {}
    open_items = analysis.get("6_open_items") or {}

    scenario_ids = [
        f"calc:dcf_{scenario.get('label')}"
        for scenario in ((analysis.get("5_scenarios") or {}).get("scenarios") or [])
        if f"calc:dcf_{scenario.get('label')}" in index
    ]

    gaps: list[str] = []
    if not opposing and variant_text.strip():
        gaps.append("反方意见未标注任何证据 id（不合格）：无法核对反对意见是否有依据")
    elif not variant_text.strip():
        gaps.append("未生成独立反方意见（未调用反方审查或调用失败）")
    if not supporting:
        gaps.append("未标注任何支持证据 id")
    if not scenario_ids:
        gaps.append("没有可引用的情景计算结果（未提供估值假设）")

    return ThesisCard(
        symbol=str(analysis.get("symbol") or pack.symbol),
        name=str(analysis.get("name") or pack.name),
        strategy_type=strategy,
        strategy_label=STRATEGY_LABELS[strategy],
        horizon=horizon or STRATEGY_HORIZON[strategy],
        return_source=STRATEGY_RETURN_SOURCE[strategy],
        as_of=f"报告期 {report_date or '未知'}｜抓取 {snapshot_date or '未知'}",
        thesis=model_text,
        variant_view=variant_text,
        supporting_evidence_ids=supporting,
        opposing_evidence_ids=opposing,
        assumptions=(analysis.get("7_provenance") or {}).get("inputs_snapshot") or {},
        valuation_method={
            "available": valuation.get("available", False),
            "applicable": (valuation.get("applicability") or {}).get("applicable", True),
            "model": (valuation.get("applicability") or {}).get("model"),
            "caveat": (valuation.get("applicability") or {}).get("caveat"),
            "basis": valuation.get("basis") or "",
        },
        scenario_result_ids=scenario_ids,
        invalidation_conditions=list(open_items.get("invalidation_conditions") or []),
        review_triggers=list(open_items.get("review_triggers") or []),
        missing_data=list(completeness.get("missing_inputs") or []),
        decision=decide(analysis),
        decision_label=DECISION_LABELS[decide(analysis)],
        confidence_basis={
            "quality_score": quality.get("score"),
            "quality_coverage": quality.get("coverage"),
            "evidence_confidence": (analysis.get("1_conclusion") or {}).get(
                "evidence_confidence_score"
            ),
            "not_a_probability": "质量分与置信度都不是上涨概率",
        },
        citations_valid=citations_valid,
        citation_report={
            "ids": id_report,
            "numbers": number_report,
            "invalid_ids": id_report["invalid_ids"],
            "unverified_numbers": number_report["unverified_numbers"],
            "opposing_binding_missing": opposing_binding_missing,
        },
        model_usage=dict(model_usage or {}),
        gaps=gaps,
        evidence_refs=evidence_refs_for(
            list(dict.fromkeys(supporting + opposing)),
            index,
            period=report_date,
            fetched_at=fetched_at,
        ),
        text_origin=text_origin,
    )


def card_from_payload(payload: dict) -> ThesisCard:
    """从 JSON 还原决策卡（接口与冻结记录用）。结构不合法直接抛错，不做兼容猜测。"""
    return ThesisCard.model_validate(payload)
