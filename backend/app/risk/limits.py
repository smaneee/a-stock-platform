"""风控限额配置。"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from app.config import Settings


@dataclass(frozen=True)
class RiskLimits:
    """模拟交易风控限额（比例均为小数，0.20 表示 20%）。"""

    max_position_per_symbol: float = 0.20  # 单只股票最大仓位
    max_total_position: float = 0.80  # 总仓位上限
    max_daily_loss: float = 0.03  # 单日最大亏损
    max_total_drawdown: float = 0.10  # 总回撤禁止新增仓位阈值
    min_commission: float = 5.0  # 最低佣金（元）
    commission_rate: float = 0.0003  # 佣金费率
    stamp_tax_rate: float = 0.0005  # 印花税（仅卖出）


DEFAULT_LIMITS = RiskLimits()

# Settings 字段名 = RISK_ + RiskLimits 字段名，保持单一映射避免两处漂移
_SETTINGS_FIELD_PREFIX = "risk_"


def limits_from_settings(settings: "Settings | Any") -> RiskLimits:
    """从应用配置构建风控限额。

    阈值全部来自环境变量/.env（RISK_*），便于按个人风险偏好调整；
    Settings 缺字段时回退到 RiskLimits 默认值。
    """
    values: dict[str, float] = {}
    for item in fields(RiskLimits):
        raw: Any = getattr(settings, _SETTINGS_FIELD_PREFIX + item.name, None)
        if raw is None:
            continue
        values[item.name] = float(raw)
    return RiskLimits(**values)
