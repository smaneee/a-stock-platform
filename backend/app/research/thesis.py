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

import json
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
#:
#: 2026-09-15 实测两次教训：
#: ① 最初没有要求标注证据 id → ``opposing_evidence_ids`` 恒为空；
#: ② 改成"句末标注 id"的自然语言要求后，真实模型**依然没标**（提示词约束不保证合规）。
#: 因此现在改成**结构化契约**：只返回 JSON，字段固定；服务端 schema 校验 + 证据 id 存在性校验，
#: 不合格就重试一次，仍不合格则卡片显式标记 ``opposing_incomplete``（而不是静默留空）。
CRITIC_PROMPT = (
    "你是独立反方投资委员。不要迎合既有结论；只根据证据包判断，指出会导致永久损失、"
    "估值失真或论点失效的理由。\n"
    "**输出格式（必须严格遵守）**：只输出一个 JSON 对象，不要写任何解释性文字或 Markdown 代码块，结构为：\n"
    '{"opposing": [{"claim": "一句话反对意见", "evidence_id": "fact:roe", '
    '"why_it_matters": "这条证据如何影响判断"}], "cannot_answer": ["证据不足无法判断的问题"]}\n'
    "要求：\n"
    "1. opposing 至少 3 条，至多 6 条；\n"
    "2. 每条 evidence_id 必须取自证据包里真实存在的 id（形如 fact:xxx 或 calc:xxx），"
    "不得编造、不得留空；\n"
    "3. 找不到证据支持的意见不要写进 opposing，放进 cannot_answer；\n"
    "4. 数字只能照抄证据包里的原值，不得换算或估算。"
)

#: 反方输出不合格时的追加提醒（只在重试时使用一次）
CRITIC_RETRY_SUFFIX = (
    "\n【上一次输出不合格】必须只输出规定结构的 JSON；opposing 至少 3 条；"
    "每条都要带真实存在的 evidence_id。"
)


class OpposingOpinion(BaseModel):
    """反方的一条反对意见（结构化契约的最小单元）。"""

    model_config = ConfigDict(extra="ignore")

    claim: str = Field(min_length=1, description="一句话反对意见")
    evidence_id: str = Field(min_length=1, description="依据的证据 id，必须真实存在")
    why_it_matters: str = Field(default="", description="这条证据如何影响判断")


class CriticReport(BaseModel):
    """反方审查的结构化输出。字段固定，多写会被忽略，缺 opposing 视为不合格。"""

    model_config = ConfigDict(extra="ignore")

    opposing: list[OpposingOpinion] = Field(default_factory=list)
    cannot_answer: list[str] = Field(default_factory=list)


#: opposing 至少要有这么多条，否则判为不合格
MIN_OPPOSING = 3


def extract_json_object(text: str) -> tuple[dict | None, str | None]:
    """从模型输出里抠出第一个 JSON 对象。

    允许外面包着解释性文字或 ```json 代码块（模型经常这样），但不做"猜字段"式兼容：
    抠不出 JSON 就返回错误原因，由调用方决定重试或标不合格。
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, "输出为空"
    if stripped.startswith("```"):
        # 去掉 ```json / ``` 包裹
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, "输出里没有 JSON 对象"
    candidate = stripped[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc.msg}"
    if not isinstance(payload, dict):
        return None, "JSON 顶层必须是对象"
    return payload, None


def parse_critic_output(text: str) -> tuple[CriticReport | None, str | None]:
    """把反方输出解析成 :class:`CriticReport`；失败返回 (None, 原因)。"""
    payload, error = extract_json_object(text)
    if payload is None:
        return None, error
    try:
        report = CriticReport.model_validate(payload)
    except ValidationError as exc:
        return None, f"字段校验失败：{exc.error_count()} 处"
    if len(report.opposing) < MIN_OPPOSING:
        return None, f"opposing 只有 {len(report.opposing)} 条，少于 {MIN_OPPOSING} 条"
    return report, None


def bind_critic_output(
    report: CriticReport | None, pack: EvidencePack, *, error: str | None = None
) -> dict:
    """把反方结构化输出**绑定到证据包**：逐条校验 evidence_id 是否存在。

    返回可直接放进决策卡的字典；任何一条引用了不存在的 id，整份反方意见判为不合格
    （``opposing_incomplete=True``）—— 允许"证据不足"进 cannot_answer，
    不允许"有结论但找不到依据"。
    """
    index = set(_evidence_index(pack))
    opinions: list[dict] = []
    invalid: list[str] = []
    if report is not None:
        for item in report.opposing:
            ok = item.evidence_id in index
            if not ok:
                invalid.append(item.evidence_id)
            opinions.append({
                "claim": item.claim,
                "evidence_id": item.evidence_id,
                "why_it_matters": item.why_it_matters,
                "evidence_exists": ok,
            })
    complete = bool(report is not None and opinions and not invalid)
    return {
        "opinions": opinions,
        "opposing_evidence_ids": [item["evidence_id"] for item in opinions if item["evidence_exists"]],
        "cannot_answer": list(report.cannot_answer) if report is not None else [],
        "invalid_evidence_ids": invalid,
        "format_error": error,
        "opposing_incomplete": not complete,
        "note": (
            "反方意见按结构化契约校验：每条必须有真实存在的证据 id；"
            "不合格时整份判为不完整，不作为增仓依据"
        ),
    }


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
    #: 反方结构化意见（claim + evidence_id + 影响机制），每条带 evidence_exists 校验结果
    opposing_opinions: list[dict] = Field(default_factory=list)
    #: 反方"证据不足无法判断"的问题清单
    cannot_answer: list[str] = Field(default_factory=list)
    #: 反方整份是否不合格（没输出 / JSON 不合法 / 引用了不存在的 id）
    opposing_incomplete: bool = True
    #: 同时被支持方与反方引用的证据 id（不是缺陷，是"同一证据两面看"）
    shared_evidence_ids: list[str] = Field(default_factory=list)
    critic_report: dict = Field(default_factory=dict)
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


#: id 里被模型不小心插入空白的两种位置（2026-09-15 实测：模型写 "calc:dcf_ 悲观"，
#: 结果被抽成不存在的 "calc:dcf_"，整张卡的门禁因此失败）。这里只做**空白规整**，
#: 不补齐、不猜测：规整后仍然对不上的 id 依旧判为无效。
_ID_SPACE_AFTER_PREFIX = re.compile(r"\b(fact|calc):\s+")
_ID_SPACE_AFTER_UNDERSCORE = re.compile(r"_\s+([A-Za-z0-9\u4e00-\u9fff])")


def normalize_evidence_text(text: str) -> str:
    """把 id 前后的多余空白去掉（只处理此两类空白，不改动其它内容）。"""
    cleaned = _ID_SPACE_AFTER_PREFIX.sub(r"\1:", text or "")
    return _ID_SPACE_AFTER_UNDERSCORE.sub(r"_\1", cleaned)


def extract_evidence_ids(*texts: str) -> list[str]:
    """从文字里抓出所有证据 id（保持出现顺序、去重；先做空白规整）。"""
    seen: list[str] = []
    for text in texts:
        for match in EVIDENCE_ID_RE.findall(normalize_evidence_text(text)):
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
        invalid_ids = list(card.citation_report.get("invalid_ids") or [])
        if invalid_ids:
            blockers.append("解释引用了不存在的证据 id：" + "、".join(invalid_ids))
        unverified = list(card.citation_report.get("unverified_numbers") or [])
        if unverified:
            blockers.append("解释里出现证据包外的数字：" + "、".join(unverified))

    # 反方审查必须通过结构化契约：没有可核对的反对意见，就等于"没人替我们找错"，
    # 这种状态不允许解锁增仓（与模型调用失败同一条规则）。
    if card.opposing_incomplete:
        reason = card.critic_report.get("format_error") or "反对意见缺少可核对的证据 id"
        blockers.append(f"反方审查未通过结构化契约（{reason}）")
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
    critic_binding: dict | None = None,
    model_usage: dict | None = None,
    fetched_at: str | None = None,
    text_origin: str = "deterministic_skeleton",
) -> ThesisCard:
    """组装研究决策卡。模型文字可选；引用校验不通过时卡片仍然生成，但标记为无效。"""
    index = _evidence_index(pack)
    strategy = classify_strategy(analysis, strategy_type)
    report_date = (analysis.get("2_data_asof") or {}).get("report_date")
    snapshot_date = str(fetched_at or "")

    # 支持证据只认模型真正引用过的 id：**不用"证据包前三条"顶替**，
    # 否则那些 id 会在去重时把反方真正引用的同一条证据挤掉（2026-09-15 实测缺陷）。
    supporting = extract_evidence_ids(model_text)
    binding = critic_binding or {}
    # 优先使用结构化契约绑定出来的 id；没有绑定时退回文字里抓到的 id
    opposing = list(binding.get("opposing_evidence_ids") or [])
    if not opposing:
        opposing = extract_evidence_ids(variant_text)
    # 同一条证据**可以同时被两方引用**（例如负债率既支持"便宜"也提示风险）。
    # 2026-09-15 实测缺陷：原先强行去重，导致主审引用了 17 条证据时把反方 6 条全部挤掉，
    # 卡片出现"反方契约完整但反对证据 id 为 0"的自相矛盾。现在保留两边，
    # 只把交集单独列出来让人看到"同一证据被两方同时引用"。
    shared_evidence_ids = [item for item in opposing if item in supporting]

    id_report = validate_evidence_ids(supporting + opposing, pack)
    number_report = validate_citations(model_text + "\n" + variant_text, pack)
    # 反方意见必须能回指至少一条证据；否则它只是没有依据的修辞，不能进入
    # 冻结卡片或作为增仓依据。确定性骨架没有反方文字时不触发此规则。
    if binding:
        opposing_binding_missing = bool(binding.get("opposing_incomplete", True))
    else:
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
    if binding:
        if binding.get("format_error"):
            gaps.append(f"反方输出不符合结构化契约：{binding['format_error']}")
        if binding.get("invalid_evidence_ids"):
            gaps.append(
                "反方引用了不存在的证据 id："
                + "、".join(binding["invalid_evidence_ids"])
            )
        if not binding.get("opinions"):
            gaps.append("反方未给出结构化反对意见（opposing 为空）")
    elif not opposing and variant_text.strip():
        gaps.append(
            "反方文字里找不到可回指的 fact:/calc: 证据 id（未走结构化契约时无法核对依据）"
        )
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
        opposing_opinions=list(binding.get("opinions") or []),
        cannot_answer=list(binding.get("cannot_answer") or []),
        opposing_incomplete=bool(binding.get("opposing_incomplete", not opposing)),
        shared_evidence_ids=shared_evidence_ids,
        critic_report=dict(binding),
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
