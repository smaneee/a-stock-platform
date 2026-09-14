"""投资决策辅助：数据获取 / 质量与估值计算 / 风险约束 / 文字解释**分层**。

模块划分（对应"把数据获取、指标计算、估值、组合风控和文字解释拆分为独立模块"）：

* :mod:`app.fundamentals.eastmoney_fundamentals` —— 数据获取（全市场基本面/估值快照，只读解析）
* :mod:`app.fundamentals.quality` —— 企业质量指标、分行业画像打分、**证据置信度**（纯函数）
* :mod:`app.fundamentals.valuation` —— 三情景 DCF 与敏感性（纯函数）
* :mod:`app.fundamentals.repository` —— 快照入库/读取（唯一写库入口）
* :mod:`app.fundamentals.decision` —— 投资论点与 7 项统一输出（确定性规则，含措辞自检）
* :mod:`app.api.fundamentals` —— 对外接口

**本包不含任何下单能力**，也不使用大模型生成数字：所有数值由确定性代码算出，
文字解释只能引用这些数字，不得臆造（解释层另见决策输出模块）。
"""
from app.fundamentals.decision import (
    CONCLUSION_KEYS,
    DecisionInputs,
    PortfolioContext,
    build_analysis,
    position_ceiling,
)
from app.fundamentals.eastmoney_fundamentals import (
    FundamentalSnapshotData,
    annualization_factor,
    fetch_market_fundamentals,
    parse_fundamentals,
)
from app.fundamentals.quality import (
    ConfidenceAssessment,
    QualityAssessment,
    QualityInputs,
    assess_quality,
    evidence_confidence,
    profile_for,
)
from app.fundamentals.valuation import (
    ScenarioBand,
    ValuationAssumptions,
    ValuationError,
    ValuationOutput,
    implied_revenue_growth,
    intrinsic_value,
    margin_of_safety,
    scenario_band,
    sensitivity_table,
    upside_ratio,
)

__all__ = [
    "CONCLUSION_KEYS",
    "ConfidenceAssessment",
    "DecisionInputs",
    "FundamentalSnapshotData",
    "PortfolioContext",
    "QualityAssessment",
    "QualityInputs",
    "ScenarioBand",
    "ValuationAssumptions",
    "ValuationError",
    "ValuationOutput",
    "annualization_factor",
    "assess_quality",
    "build_analysis",
    "evidence_confidence",
    "fetch_market_fundamentals",
    "implied_revenue_growth",
    "intrinsic_value",
    "margin_of_safety",
    "parse_fundamentals",
    "position_ceiling",
    "profile_for",
    "scenario_band",
    "sensitivity_table",
    "upside_ratio",
]
