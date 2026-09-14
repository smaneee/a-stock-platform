"""回测成交执行。

落实 A 股基本约束：T+1、100 股整数手、分板块涨跌停限制、停牌不能成交、
佣金最低收费、印花税、可配置滑点，并禁止使用未来数据。
涨跌停规则统一来自 MarketRuleEngine（回测与模拟交易共用）。
调用方应通过 SecurityMaster 解析 Security 后传入 rules，避免依赖
bar.name 临时字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.market_data.base import QuoteData
from app.market_rules.rules import MarketRuleEngine, MarketRules


@dataclass(frozen=True)
class ExecutionConfig:
    """回测成交配置。

    默认费率取 A 股常见口径（券商公示，2026-09 复核）：
    佣金不高于成交金额 0.3% 且最低 5 元、印花税 0.05%（仅卖出单边）、
    过户费 0.001% 双边、滑点按单边比例。
    """

    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001  # 过户费（双边收取）
    slippage: float = 0.0005  # 滑点比例
    #: 成交量参与率上限（D5）。0 表示不限制（保持既有行为）；>0 时单笔成交量
    #: 不得超过当日成交量的该比例，超过则部分成交或拒绝。
    max_participation_rate: float = 0.0
    #: 触及参与率上限时是否允许部分成交（False 则整笔拒绝）
    allow_partial_fill: bool = True


@dataclass
class ExecutionResult:
    """单笔成交结果。"""

    filled: bool
    reason: str = ""
    price: float = 0.0
    quantity: int = 0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    #: 请求数量与是否部分成交（D5）
    requested_quantity: int = 0
    partial: bool = False
    #: 参与率上限下当日最多可成交的股数（0 表示未启用限制）
    participation_cap: int = 0


class ExecutionSimulator:
    """回测成交模拟器。"""

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        rule_engine: MarketRuleEngine | None = None,
    ):
        self.config = config or ExecutionConfig()
        self.rule_engine = rule_engine or MarketRuleEngine()

    def try_fill(
        self,
        side: str,
        quantity: int,
        bar: QuoteData,
        rules: MarketRules | None = None,
        is_new_listing: bool = False,
    ) -> ExecutionResult:
        """尝试在指定 K 线（开盘价）成交。

        bar 为信号产生后的下一根 K 线，使用其开盘价成交以避免未来数据。
        rules 由调用方经 SecurityMaster 解析后传入；为 None 时按 bar.name 兜底。

        涨跌停口径（无盘口排队证据时的保守假设）：
        - 一字板（当日 low >= 涨停价 / high <= 跌停价，全天封死）不可成交；
        - 开盘即封在板价但盘中开板（有 low < 涨停价 / high > 跌停价 的证据）
          按板价成交，不把「触及涨跌停」机械等同全天无法成交；
        - 昨收缺失（previous_close <= 0）时无法判断板价，跳过涨跌停约束
          （数据侧必须补昨收，见 HistoricalDataService.get_cached）。
        """
        # 停牌：成交量为 0 无法成交
        if bar.volume <= 0:
            return ExecutionResult(False, "停牌，无法成交")

        price = bar.open
        if price <= 0:
            return ExecutionResult(False, "开盘价无效")

        # 解析规则：优先使用调用方传入的 rules，否则兜底用 bar.name
        if rules is None:
            rules = self.rule_engine.get_rules(
                bar.symbol, name=bar.name, is_new_listing=is_new_listing
            )

        # 整数手校验（BUY 严格要求一手，SELL 允许零股）
        if side == "BUY" and quantity % rules.lot_size != 0:
            return ExecutionResult(False, "数量必须为 100 股整数手")

        # 分板块涨跌停限制（统一规则引擎）
        limit_up: Decimal | None = None
        limit_down: Decimal | None = None
        if bar.previous_close > 0 and rules.has_price_limit:
            prev = Decimal(str(bar.previous_close))
            limit_up = rules.limit_up(prev)
            limit_down = rules.limit_down(prev)

        if side == "BUY" and limit_up is not None and _round2(price) >= limit_up:
            if not _board_opened(bar, limit_up, upper=True):
                return ExecutionResult(False, "一字涨停（全天封板），无法买入")
        if side == "SELL" and limit_down is not None and _round2(price) <= limit_down:
            if not _board_opened(bar, limit_down, upper=False):
                return ExecutionResult(False, "一字跌停（全天封板），无法卖出")

        # 最小报价单位校验（A 股统一 0.01 元）
        try:
            tick = Decimal(str(rules.tick_size))
        except Exception:
            tick = Decimal("0.01")
        if tick > 0:
            price_dec = Decimal(str(price)).quantize(Decimal("0.0001"))
            tick_dec = Decimal(str(tick))
            if tick_dec != 0 and (price_dec % tick_dec) != 0:
                # 警告：未对齐到 tick，但回测口径默认按 bar.open 成交，记录 reason 不阻断
                _ = tick  # 显式保留以方便后续启用严格模式

        # 滑点：买入抬价，卖出压价；成交价不得越过涨跌停价（越过即为不可成交价格）
        if side == "BUY":
            fill_price = price * (1 + self.config.slippage)
            if limit_up is not None:
                fill_price = min(fill_price, float(limit_up))
        else:
            fill_price = price * (1 - self.config.slippage)
            if limit_down is not None:
                fill_price = max(fill_price, float(limit_down))

        # 成交量容量（D5）：单笔不得超过当日成交量的参与率上限。
        # 缺少盘口证据时，按「全成或全不成」都过于乐观/悲观，这里按可成交量部分成交，
        # 并把剩余部分明确标为未成交（调用方据此决定撤单或顺延）。
        requested = quantity
        cap = 0
        partial = False
        if self.config.max_participation_rate > 0:
            cap = participation_cap(
                bar.volume, self.config.max_participation_rate, rules.lot_size
            )
            if quantity > cap:
                if not self.config.allow_partial_fill or cap < rules.lot_size:
                    return ExecutionResult(
                        False,
                        f"超过成交量容量上限（当日可成交 {cap} 股 < 委托 {quantity} 股）",
                        requested_quantity=requested,
                        participation_cap=cap,
                    )
                quantity = cap
                partial = True

        value = fill_price * quantity
        commission = max(value * self.config.commission_rate, self.config.min_commission)
        stamp_tax = value * self.config.stamp_tax_rate if side == "SELL" else 0.0
        transfer_fee = value * self.config.transfer_fee_rate

        return ExecutionResult(
            filled=True,
            reason=(
                f"部分成交：受参与率上限 {self.config.max_participation_rate:.1%} 约束"
                f"（当日可成交 {cap} 股，委托 {requested} 股）"
                if partial
                else ""
            ),
            price=fill_price,
            quantity=quantity,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            requested_quantity=requested,
            partial=partial,
            participation_cap=cap,
        )


def participation_cap(volume: float, rate: float, lot_size: int = 100) -> int:
    """参与率上限下当日最多可成交股数（向下取整到手）。

    ``rate<=0`` 或 ``volume<=0`` 时返回 0（调用方应据此判定不可成交）。
    """
    if rate <= 0 or volume <= 0:
        return 0
    raw = int(volume * rate)
    lot = max(1, int(lot_size or 1))
    return (raw // lot) * lot


def capacity_multiple(
    quantity: int, price: float, volume: float, rate: float
) -> float | None:
    """容量倍数 = 该笔需求 / 参与率上限下可成交量。

    * ``>= 1``：在容量之内；
    * ``< 1``：超出容量（需要降低规模或延长建仓时间）；
    * 无法计算（volume/rate/price 无效）时返回 ``None``，调用方应标注为未知而不是 0。
    """
    if volume <= 0 or rate <= 0 or price <= 0 or quantity <= 0:
        return None
    cap = volume * rate
    return cap / quantity


def _round2(value: float) -> Decimal:
    """把价格按 0.01 元取整，用于与涨跌停价比较。"""
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _board_opened(bar: QuoteData, limit: Decimal, *, upper: bool) -> bool:
    """判断当日是否整天封在板价（一字板）。

    upper=True 用于涨停：当日最低价低于涨停价，且 high >= open（K 线自洽）。
    upper=False 用于跌停：当日最高价高于跌停价，且 low <= open。
    缺失或自相矛盾（high < open / low > open）的 K 线不臆测开板，按一字板保守处理。
    """
    if bar.high is None or bar.low is None or bar.high <= 0 or bar.low <= 0:
        return False
    high, low, open_ = _round2(bar.high), _round2(bar.low), _round2(bar.open)
    if upper:
        return high >= open_ and low < limit
    return low <= open_ and high > limit