"""研究报告解释层（DeepSeek）：只解释确定性计算结果，不做计算、不写库、不下单。

单用户本机定位：一个薄适配器 + 引用校验 + 状态如实上报，不做企业级的配额/审计/多租户设施。
"""
from app.explain.deepseek import (
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    STATUS_REJECTED,
    DeepSeekExplainer,
    EvidencePack,
    ExplainerConfig,
    build_evidence_pack,
    mask_secret,
    validate_citations,
)

__all__ = [
    "STATUS_ERROR",
    "STATUS_NOT_CONFIGURED",
    "STATUS_OK",
    "STATUS_REJECTED",
    "DeepSeekExplainer",
    "EvidencePack",
    "ExplainerConfig",
    "build_evidence_pack",
    "mask_secret",
    "validate_citations",
]
