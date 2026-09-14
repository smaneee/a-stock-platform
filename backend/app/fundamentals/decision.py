"""投资论点与结论组装（确定性规则，不生成数字、不写"荐股文案"）。

对应「每次分析统一输出 7 项」：

1. 一句话结论 + 适用期限
2. 数据截至时间与完整性
3. 企业质量 / 估值 / 市场预期 / 组合风险
4. 最重要的 3 条支持证据与 3 条反对证据（区分**事实 / 估计 / 模型推断**）
5. 悲观 / 基准 / 乐观情景及对应假设
6. 尚未验证的信息、论点失效条件、下次复核触发条件
7. 分析依据、数据来源与**可展开的计算过程**

三条实现纪律：

* **不承诺收益**：结论文案取自固定集合，且带 ``wording_guard`` 校验（禁止"稳赚/必涨/保证"等）。
* **允许"暂不行动"**：数据过期、证据不足、缺少估值假设时，结论就是「暂不行动」并说明缺什么。
* **仓位不由评分决定**：:func:`position_ceiling` 必须有用户显式给出的风险承受能力与组合约束，
  否则返回 ``None`` + 原因；评分只影响"是否值得进一步研究"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.fundamentals.quality import (
    ConfidenceAssessment,
    METRIC_SPECS,
    QualityAssessment,
    profile_for,
)
from app.fundamentals.valuation import (
    ScenarioBand,
    margin_of_safety,
    upside_ratio,
)

#: 禁止出现在任何输出里的措辞（收益承诺/确定性暗示）
FORBIDDEN_PHRASES = (
    "稳赚",
    "必涨",
    "必赚",
    "保证收益",
    "包赚",
    "一定涨",
    "肯定涨",
    "无风险套利",
    "推荐买入",
    "建议买入",
)

#: 一句话结论的封闭集合
CONCLUSIONS = {
    "insufficient": "证据不足：暂不行动（先补齐数据）",
    "expired": "数据过期：停止判断（等待新报告期）",
    "model_not_applicable": (
        "估值模型不适用于该行业：暂不给价值判断（需改用剩余收益/股息折现等框架）"
    ),
    "need_valuation": "质量与置信度可评估，但缺少显式估值假设：暂不给价值判断",
    "overvalued": "估值高于价值区间上沿：吸引力有限，等待更好价格或更强证据",
    "fair": "估值落在价值区间内：可继续研究，但无安全边际",
    "attractive": "估值低于价值区间下沿且置信度可用：可纳入研究观察名单（非买入建议）",
}

#: 结论键的公开顺序（供前端与测试引用，避免硬编码字符串）
CONCLUSION_KEYS: tuple[str, ...] = tuple(CONCLUSIONS)

DISCLAIMER = "本结论由确定性规则生成，仅用于研究，不构成投资建议，也不承诺任何收益。"


@dataclass(frozen=True)
class PortfolioContext:
    """组合与个人约束。**全部必须由用户显式给出**，系统不猜风险偏好。"""

    #: 用户可承受的单笔最大回撤（小数，0.2 = 20%）
    max_loss_per_trade: float | None = None
    #: 单票权重上限（小数）
    max_symbol_weight: float | None = None
    #: 该行业现有仓位权重（小数）
    industry_weight: float | None = None
    #: 行业权重上限（小数）
    max_industry_weight: float | None = None
    #: 投资期限（例如 "3 年以上"）
    horizon: str = ""
    #: 是否需要这笔资金在短期内使用（流动性约束）
    liquidity_needed_soon: bool = False

    def missing(self) -> list[str]:
        gaps = []
        if self.max_symbol_weight is None:
            gaps.append("单票权重上限")
        if self.max_industry_weight is None or self.industry_weight is None:
            gaps.append("行业集中度约束")
        if self.max_loss_per_trade is None:
            gaps.append("可承受的单笔最大回撤")
        if not self.horizon:
            gaps.append("投资期限")
        return gaps


@dataclass(frozen=True)
class DecisionInputs:
    symbol: str
    name: str
    quality: QualityAssessment
    confidence: ConfidenceAssessment
    price: float | None = None
    report_date: date | None = None
    fetched_at: str | None = None
    industry: str | None = None
    currency: str = "CNY"
    source: str = ""
    valuation: ScenarioBand | None = None
    #: 估值假设的来源说明（事实/估计/模型推断），无估值时为空
    valuation_basis: str = ""
    portfolio: PortfolioContext | None = None
    #: 组合相对收益/相关性等外部输入（未提供时如实标注"未接入"）
    portfolio_notes: tuple[str, ...] = field(default_factory=tuple)
    #: 与基本面快照同报告期的三表派生值；None 表示尚未接入或报告期不一致。
    statement_detail: dict | None = None

    def missing_inputs(self) -> list[str]:
        gaps: list[str] = []
        if self.price is None:
            gaps.append("现价")
        if self.report_date is None:
            gaps.append("财报报告期")
        if self.valuation is None:
            gaps.append("显式估值假设（增长率/自由现金流率/折现率/永续增长率/股本）")
        if self.portfolio is None:
            gaps.append("组合与个人约束（风险承受能力/期限/集中度）")
        else:
            gaps.extend(self.portfolio.missing())
        return gaps


def model_applicability(profile_key: str) -> dict:
    """估值模型**适用性**提示（按行业画像）。

    为什么必须有：2026-09-14 实测把「收入 × 自由现金流率」折现套到光大银行（601818）
    上，得到基准价值 46.42 元 vs 现价 3.05 元 —— 相差 15 倍。这不是计算错误，而是
    **模型用错了对象**：银行没有"营业总收入×现金流率"这种自由现金流口径，应改用
    剩余收益（ROE−PB）、股息折现或分部估值。系统必须主动把这个限制说出来。
    """
    table = {
        "general": {
            "applicable": True,
            "model": "收入 × 自由现金流率 的两阶段折现",
            "caveat": "适用于经营现金流相对稳定的企业；重资产或强周期行业需自行核查资本开支与营运资本",
        },
        "financial": {
            "applicable": False,
            "model": "收入 × 自由现金流率 的两阶段折现",
            "caveat": (
                "**不适用于银行/保险/证券**：这类企业应使用剩余收益（ROE−PB 框架）、"
                "股息折现或分部估值。用收入折现会给出数量级错误的结果，本结果只能当演示"
            ),
        },
        "utility": {
            "applicable": True,
            "model": "收入 × 自由现金流率 的两阶段折现",
            "caveat": "可用，但折现率应反映受监管现金流的稳定性，并计入资本开支与债务结构",
        },
        "growth": {
            "applicable": True,
            "model": "收入 × 自由现金流率 的两阶段折现",
            "caveat": (
                "高增长企业的价值几乎全部来自永续假设 → 必须同时看悲观情景与敏感性表，"
                "并把自由现金流率当作最不确定的输入"
            ),
        },
    }
    return table.get(profile_key, table["general"])


def position_ceiling(
    inputs: DecisionInputs, *, stop_distance: float | None
) -> dict:
    """仓位上限（**不是建议仓位**）。必须由用户约束推导，评分不参与。

    规则：``单票上限 = min(用户单票权重上限, 风险承受 ÷ 止损距离, 行业剩余额度)``；
    任何输入缺失即返回 ``None`` 并说明缺什么。
    """
    ctx = inputs.portfolio
    if ctx is None:
        return {
            "ceiling_pct": None,
            "reason": "未提供组合与个人约束 → 不给仓位上限（评分不决定仓位）",
            "components": {},
        }
    gaps = ctx.missing()
    if gaps:
        return {
            "ceiling_pct": None,
            "reason": "缺少：" + "、".join(gaps),
            "components": {},
        }
    components: dict[str, float] = {"用户单票上限": float(ctx.max_symbol_weight)}
    risk_cap = None
    if stop_distance and stop_distance > 0:
        risk_cap = float(ctx.max_loss_per_trade) / float(stop_distance)
        components["风险承受约束"] = round(risk_cap, 4)
    industry_left = None
    if ctx.max_industry_weight is not None and ctx.industry_weight is not None:
        industry_left = max(0.0, float(ctx.max_industry_weight) - float(ctx.industry_weight))
        components["行业剩余额度"] = round(industry_left, 4)
    caps = [c for c in (ctx.max_symbol_weight, risk_cap, industry_left) if c is not None]
    ceiling = min(caps) if caps else None
    return {
        "ceiling_pct": round(ceiling * 100.0, 2) if ceiling is not None else None,
        "reason": "取各约束最小值；这是上限，不是建议仓位，也不由评分决定",
        "components": components,
        "liquidity_penalty": "需短期动用资金 → 应先排除该标的" if ctx.liquidity_needed_soon else None,
    }


def _evidence(
    subscores: dict[str, dict], quality_ok: bool
) -> tuple[list[dict], list[dict]]:
    """从子分数里挑出最强/最弱的证据（每条都带数值、口径与来源分类）。"""
    scored = [
        {"metric": key, **value}
        for key, value in subscores.items()
        if value.get("score") is not None
    ]
    scored.sort(key=lambda item: item["score"], reverse=True)
    support = [
        {
            "evidence": f"{item['label']} {item['value']}{item['unit']}"
                        f"（阈值 {item['threshold_poor']}~{item['threshold_good']}{item['unit']}，"
                        f"得分 {item['score']}）",
            "origin": item["origin"],
            "source": "财报/行情快照",
        }
        for item in scored[:3]
    ]
    oppose = [
        {
            "evidence": f"{item['label']} {item['value']}{item['unit']}"
                        f"（低于画像良好阈值 {item['threshold_good']}{item['unit']}，"
                        f"得分 {item['score']}）",
            "origin": item["origin"],
            "source": "财报/行情快照",
        }
        for item in scored[::-1][:3]
    ]
    if not quality_ok:
        support, oppose = [], []
    return support, oppose


def _invalidation_conditions(inputs: DecisionInputs) -> list[str]:
    """论点失效条件：由当前指标与画像阈值确定性推导，可复核。"""
    conditions: list[str] = []
    subs = inputs.quality.subscores
    roe = subs.get("roe", {})
    if roe.get("threshold_poor") is not None:
        conditions.append(
            f"净资产收益率跌破 {roe['threshold_poor']}%（当前 {roe['value']}%）→ 论点失效"
        )
    debt = subs.get("debt_ratio", {})
    if debt.get("threshold_poor") is not None:
        conditions.append(
            f"资产负债率升破 {debt['threshold_poor']}%（当前 {debt['value']}%）→ 论点失效"
        )
    profit = subs.get("profit_yoy", {})
    if profit.get("value") is not None and profit["value"] > 0:
        conditions.append("归母净利润同比转负（当前为正）→ 需重新评估")
    ocf = subs.get("ocf_to_profit", {})
    if ocf.get("value") is not None and ocf["value"] >= 1.0:
        conditions.append("经营现金流/净利润 跌破 0.5 倍 → 盈利质量论点失效")
    if inputs.valuation and inputs.valuation.base is not None and inputs.price:
        conditions.append(
            f"现价高于基准情景价值 {inputs.valuation.base.output.per_share} 元 → 安全边际消失"
        )
    if not conditions:
        conditions.append("关键指标缺失，论点本身不成立（先补数据）")
    return conditions


def _review_triggers(inputs: DecisionInputs) -> list[str]:
    triggers = ["下一期定期报告发布后（报告期字段变化时自动失效重算）"]
    if inputs.price is None:
        triggers.append("取到现价后（当前缺现价）")
    else:
        triggers.append("现价相对基准情景价值偏离超过 ±20%")
    triggers.append("行业分类或画像变化（决定使用哪套指标阈值）")
    triggers.append("组合约束变化（风险承受能力、期限、集中度上限）")
    return triggers


def _wording_guard(payload: dict) -> list[str]:
    """扫描输出里的禁用措辞（自检，违规会在响应里如实报出来）。"""
    text = str(payload)
    return [phrase for phrase in FORBIDDEN_PHRASES if phrase in text]


def build_analysis(inputs: DecisionInputs) -> dict:
    """生成 7 项统一输出。缺失输入不猜，直接写进"尚未验证/缺少输入"。"""
    quality = inputs.quality
    confidence = inputs.confidence
    gaps = inputs.missing_inputs()

    valuation_upside: dict[str, float | None] = {}
    if inputs.valuation is not None and inputs.price:
        for scenario in (inputs.valuation.bear, inputs.valuation.base, inputs.valuation.bull):
            if scenario is not None:
                valuation_upside[scenario.label] = upside_ratio(
                    scenario.output.per_share, inputs.price
                )

    if confidence.expired:
        conclusion_key = "expired"
    elif quality.insufficient_evidence:
        conclusion_key = "insufficient"
    elif inputs.valuation is None:
        conclusion_key = "need_valuation"
    elif not model_applicability(profile_for(inputs.industry)[0].key)["applicable"]:
        # 实测教训：收入折现套在银行上得到 46 元 vs 现价 3 元（差 15 倍）。
        # 模型不适用时**不允许**输出"低估/值得关注"这类结论。
        conclusion_key = "model_not_applicable"
    else:
        base_upside = valuation_upside.get("基准")
        mos = margin_of_safety(
            inputs.valuation.base.output.per_share if inputs.valuation.base else None,
            inputs.price,
        )
        if base_upside is None:
            conclusion_key = "need_valuation"
        elif base_upside < -0.10:
            conclusion_key = "overvalued"
        elif mos is not None and mos >= 0.20:
            conclusion_key = "attractive"
        else:
            conclusion_key = "fair"

    support, oppose = _evidence(quality.subscores, not quality.insufficient_evidence)
    if inputs.valuation is not None:
        for label, value in valuation_upside.items():
            if value is None:
                continue
            item = {
                "evidence": f"{label}情景价值对应现价偏离 {value:+.1%}",
                "origin": "模型推断（依赖于假设，非事实）",
                "source": "本平台 DCF 引擎",
            }
            (support if value > 0 else oppose).append(item)
    support = support[:3]
    oppose = oppose[:3]

    scoring_ceiling = position_ceiling(inputs, stop_distance=None)
    unverified = [
        "商业模式与竞争优势（需人工阅读年报/公告，系统无法自动断言）",
        "管理层资本配置质量（需人工判断）",
        "卖方一致预期与价格隐含假设 —— 未接入",
        "公司行为（分红、增发、回购）对每股口径的影响 —— 未接入",
    ]
    if inputs.statement_detail is None:
        unverified.insert(
            2,
            "三表明细（经营现金流、资本开支、有息负债、商誉）尚未按本报告期刷新",
        )
    leverage = "未接入（需资产负债表有息负债明细）"
    if inputs.statement_detail is not None:
        net_debt = inputs.statement_detail.get("identified_net_debt")
        leverage = (
            f"已识别净负债 {net_debt} 元；"
            "口径不含上游未披露的其他有息负债"
            if net_debt is not None else "三表已刷新，但净负债组件不足"
        )

    payload = {
        "symbol": inputs.symbol,
        "name": inputs.name,
        "1_conclusion": {
            "conclusion": CONCLUSIONS[conclusion_key],
            "conclusion_key": conclusion_key,
            "horizon": inputs.portfolio.horizon if inputs.portfolio and inputs.portfolio.horizon
                       else "未指定（长投与短交规则不通用，本分析不覆盖短线交易规则）",
            "attractiveness_quality_score": quality.score,
            "evidence_confidence_score": confidence.score,
            "separation_note": (
                "质量分（吸引力）与证据置信度是两个独立维度，均**不是上涨概率**，"
                "不得相乘包装成单一「买入分」"
            ),
        },
        "2_data_asof": {
            "report_date": inputs.report_date.isoformat() if inputs.report_date else None,
            "fetched_at": inputs.fetched_at,
            "price": inputs.price,
            "currency": inputs.currency,
            "source": inputs.source,
            "staleness_days": confidence.components.get("stale_days"),
            "expired": confidence.expired,
            "completeness": {
                "metric_coverage": quality.coverage,
                "missing_metrics": list(quality.missing),
                "missing_inputs": gaps,
            },
            "confidence_reasons": list(confidence.reasons),
        },
        "3_dimensions": {
            "quality": quality.to_dict(),
            "valuation": {
                "available": inputs.valuation is not None,
                "basis": inputs.valuation_basis,
                "upside_vs_price": valuation_upside,
                "applicability": model_applicability(
                    profile_for(inputs.industry)[0].key
                ),
                "reason": None if inputs.valuation is not None
                          else "未提供显式估值假设 → 不给价值区间（禁止用单一指标代替估值）",
            },
            "market_expectation": {
                "available": False,
                "reason": (
                    "未接入卖方一致预期/隐含假设数据 → 无法区分「公司表现好」与「超过市场预期」，"
                    "该项标记为未接入而不是估计"
                ),
            },
            "portfolio_risk": {
                "position_ceiling": scoring_ceiling,
                "permanent_loss_risk": "未量化（缺少历史最大回撤与破产概率口径）",
                "liquidity": "未量化（需成交额/换手与持仓规模）",
                "leverage": leverage,
                "notes": list(inputs.portfolio_notes),
            },
        },
        "4_evidence": {
            "support": support,
            "oppose": oppose,
            "origin_legend": {
                "事实": "财报/公告直接给出",
                "估计": "分析者输入",
                "模型推断": "本平台由事实计算得出",
            },
            "guarantee": "支持与反对证据数量不足 3 条时如实少于 3 条，不凑数",
        },
        "5_scenarios": inputs.valuation.to_dict() if inputs.valuation is not None else {
            "scenarios": [],
            "reason": "缺少显式假设：悲观/基准/乐观三情景必须由使用者给出增长率、自由现金流率、"
                      "折现率、永续增长率与股本，系统不代填",
        },
        "6_open_items": {
            "unverified": unverified,
            "invalidation_conditions": _invalidation_conditions(inputs),
            "review_triggers": _review_triggers(inputs),
            "horizon_discipline": (
                "投资期限必须在建仓时锁定；**禁止因为亏损而把交易改称长期投资**。"
                "本分析不包含短线交易规则（短线规则见实时选股模块，两者不混用）"
            ),
        },
        "7_provenance": {
            "formulas": {
                "durability_metrics": "ROE / 毛利率 / 净利率 / 资产负债率 / 同比，均取自报告期口径",
                "annualization": "报告期年化倍数：一季×4、半年×2、三季×4/3、年报×1（模型推断，未计季节性）",
                "dcf": "见 /api/fundamentals/{symbol}/valuation 返回的 formula 字段",
                "margin_of_safety": "(价值 − 价格) ÷ 价值",
                "free_cash_flow": "经营现金流量净额 − 购建长期资产支付的现金",
                "identified_net_debt": "已识别有息负债合计 − 货币资金",
            },
            "model_version": "fundamentals-decision-v1",
            "inputs_snapshot": {
                "symbol": inputs.symbol,
                "industry": inputs.industry,
                "price": inputs.price,
                "report_date": inputs.report_date.isoformat() if inputs.report_date else None,
                "valuation_basis": inputs.valuation_basis,
                "portfolio_context": (
                    "已提供" if inputs.portfolio is not None else "未提供"
                ),
                "statement_detail": "已提供且报告期一致"
                if inputs.statement_detail is not None else "未提供",
            },
            "reproduce": "同样的输入（快照 + 假设）必然得到同样的结论：本模块为纯函数",
        },
        "disclaimer": DISCLAIMER,
    }
    payload["wording_guard"] = _wording_guard(payload)
    return payload
