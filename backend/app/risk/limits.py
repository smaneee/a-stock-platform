"""风控限额配置。"""
from __future__ import annotations

from dataclasses import dataclass


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
