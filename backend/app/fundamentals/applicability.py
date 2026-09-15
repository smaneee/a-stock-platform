"""估值方法适用性检查（S2）。

方案 §二：「按行业选择估值方法。银行、保险、周期企业、负自由现金流企业不应无条件套用
同一 DCF。第一版可以明确返回"方法不适用"，再逐步增加经验证的模型。」
方案 §四 S2 验收：「不适用的行业模型明确拒绝」。

本模块把"能不能用这套估值"变成**可测试的判定**，判据只用**已经入库的真实数据**
（行业、自由现金流、净负债、净资产、总资产），不引入任何猜测：

======================  ==================================================  ========
判据                     结论                                                 强度
======================  ==================================================  ========
金融行业（银行/保险等）   收入折现**不适用**，应改用剩余收益/股息折现              拒绝
自由现金流 ≤ 0           折现模型无意义（分母是负的），应改用资产/清算视角          拒绝
净资产 ≤ 0               每股价值无意义（资不抵债）                              拒绝
强周期行业               **可用但需正常化盈利**，不能把高景气利润长期外推          警示
净负债/净资产 > 100%     高杠杆会放大股权价值波动，需单独看债务期限                警示
数据缺失（FCF/净资产）    无法判断 → 返回 unknown，交给上层按"缺数据"处理           未知
======================  ==================================================  ========

注意区分"拒绝"与"警示"：拒绝表示**不输出该模型的结论**；警示表示结论仍可输出，
但必须把限制写在同一条响应里，供使用者判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 强周期行业关键词（东财 f100 行业名）。命中即要求正常化盈利口径。
CYCLICAL_KEYWORDS: tuple[str, ...] = (
    "钢铁", "煤炭", "有色", "金属", "化工", "化学", "石油", "化纤", "橡胶", "塑料",
    "航运", "港口水运", "水泥", "建材", "玻璃", "船舶", "工程机械", "房地产开发",
    "养殖", "饲料", "农业", "造纸", "纺织",
)

#: 净负债/净资产超过该比例视为高杠杆（警示，不是拒绝）
HIGH_LEVERAGE_RATIO = 1.0


@dataclass(frozen=True)
class ApplicabilityInputs:
    """判定所需数据。全部来自已入库快照/报表；缺失写 ``None``，**不用 0 顶替**。"""

    industry: str | None = None
    market: str | None = None                      # 主板/创业板等，仅用于说明
    free_cash_flow: float | None = None            # 报告期经营现金流 − 资本开支
    net_debt: float | None = None                  # 有息负债 − 货币资金（负值为净现金）
    equity: float | None = None                    # 股东权益（净资产）
    total_assets: float | None = None
    net_profit: float | None = None                # 报告期归母净利润
    report_date: str | None = None


@dataclass
class ApplicabilityReport:
    applicable: bool
    verdict: str                                   # ok / warning / rejected / unknown
    model: str = "收入 × 自由现金流率 的两阶段折现"
    rejected_reason: str | None = None
    requires_normalization: bool = False
    caveats: list[str] = field(default_factory=list)
    data_used: dict = field(default_factory=dict)
    missing_data: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "applicable": self.applicable,
            "verdict": self.verdict,
            "model": self.model,
            "rejected_reason": self.rejected_reason,
            "requires_normalization": self.requires_normalization,
            "caveats": list(self.caveats),
            "data_used": dict(self.data_used),
            "missing_data": list(self.missing_data),
            "rule_note": (
                "拒绝=不输出该模型结论；警示=结论仍输出但限制必须同时展示；"
                "未知=数据不足，按缺数据处理"
            ),
        }


def _is_financial(industry: str | None) -> bool:
    if not industry:
        return False
    return any(
        keyword in industry
        for keyword in ("银行", "保险", "证券", "多元金融", "信托")
    )


def _is_cyclical(industry: str | None) -> bool:
    if not industry:
        return False
    return any(keyword in industry for keyword in CYCLICAL_KEYWORDS)


def assess_applicability(inputs: ApplicabilityInputs) -> ApplicabilityReport:
    """判定当前标的是否适用「收入 × 自由现金流率」两阶段折现。"""
    data_used: dict = {
        "industry": inputs.industry,
        "report_date": inputs.report_date,
        "free_cash_flow": inputs.free_cash_flow,
        "net_debt": inputs.net_debt,
        "equity": inputs.equity,
    }
    missing: list[str] = []
    if inputs.free_cash_flow is None:
        missing.append("自由现金流（需现金流量表）")
    if inputs.equity is None:
        missing.append("股东权益（需资产负债表）")
    if inputs.net_debt is None:
        missing.append("净负债（需有息负债与货币资金）")

    # ① 金融行业：收入折现不适用（这是口径问题，不是数据不足）
    if _is_financial(inputs.industry):
        return ApplicabilityReport(
            applicable=False,
            verdict="rejected",
            rejected_reason=(
                f"行业「{inputs.industry}」属金融业：没有「营业总收入 × 自由现金流率」"
                "这种现金流口径，应改用剩余收益（ROE−PB）、股息折现或分部估值"
            ),
            caveats=["用收入折现会给数量级错误的结果，已按方案 §二 明确拒绝输出结论"],
            data_used=data_used,
            missing_data=missing,
        )

    # ② 净资产为负：资不抵债，每股价值无意义
    if inputs.equity is not None and inputs.equity <= 0:
        return ApplicabilityReport(
            applicable=False,
            verdict="rejected",
            rejected_reason=f"股东权益为 {inputs.equity:.0f} 元（≤0，资不抵债）：每股价值无意义",
            caveats=["应改用资产处置/清算视角，而不是持续经营折现"],
            data_used=data_used,
            missing_data=missing,
        )

    # ③ 自由现金流为负：折现模型的分子为负 → 拒绝
    if inputs.free_cash_flow is not None and inputs.free_cash_flow <= 0:
        return ApplicabilityReport(
            applicable=False,
            verdict="rejected",
            rejected_reason=(
                f"报告期自由现金流为 {inputs.free_cash_flow:.0f} 元（≤0）："
                "折现模型的现金流为负，任何增长率都只会放大负值，结论无意义"
            ),
            caveats=["应改用资产价值、重置成本或清算视角；若认为现金流将转正，需先给出证据"],
            data_used=data_used,
            missing_data=missing,
        )

    # ④ 关键数据缺失：不拒绝也不放行，如实返回 unknown
    if inputs.free_cash_flow is None or inputs.equity is None:
        return ApplicabilityReport(
            applicable=False,
            verdict="unknown",
            rejected_reason=None,
            caveats=["缺少判定所需数据 → 不给适用性结论（缺数据就是缺数据）"],
            data_used=data_used,
            missing_data=missing,
        )

    caveats: list[str] = []
    requires_normalization = False

    # ⑤ 强周期行业：可用，但必须正常化盈利
    if _is_cyclical(inputs.industry):
        requires_normalization = True
        caveats.append(
            f"行业「{inputs.industry}」属强周期：高景气期的利润与现金流不能长期外推，"
            "需用正常化（跨周期平均）盈利重算，本次结论按报告期口径给出"
        )

    # ⑥ 高杠杆警示
    if inputs.net_debt is not None and inputs.equity > 0:
        ratio = inputs.net_debt / inputs.equity
        data_used["net_debt_to_equity"] = round(ratio, 4)
        if ratio > HIGH_LEVERAGE_RATIO:
            caveats.append(
                f"净负债/净资产 = {ratio:.0%}（> {HIGH_LEVERAGE_RATIO:.0%}）："
                "高杠杆会放大股权价值对经营波动的敏感度，需同时检查债务期限结构"
            )

    # ⑦ 亏损企业（净利润 ≤ 0）：不拒绝，但提醒盈利口径
    if inputs.net_profit is not None and inputs.net_profit <= 0:
        caveats.append(
            f"报告期归母净利润为 {inputs.net_profit:.0f} 元（≤0）：估值不依赖当期利润，"
            "但盈利转正的时间与幅度是核心不确定项"
        )

    return ApplicabilityReport(
        applicable=True,
        verdict="warning" if caveats else "ok",
        rejected_reason=None,
        requires_normalization=requires_normalization,
        caveats=caveats,
        data_used=data_used,
        missing_data=missing,
    )
