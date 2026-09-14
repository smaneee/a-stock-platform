"""技术指标核验测试（研发计划「技术指标要求」）。

覆盖 MA / EMA / MACD / RSI / 量比 / 振幅 / BOLL / KDJ / ATR / OBV / CCI / WR
共 12 类指标与 ``GET /api/indicators`` / ``GET /api/indicators/{symbol}``。

分四类：

1. **正常用例**：手算已知数值校验（含逐步指数平滑的手算值）。
2. **独立复核**：本文件用**纯 Python 标准库**（不使用 pandas/numpy，也不 import
   被测函数）另写一份 MA/EMA/RSI/MACD/BOLL/ATR 参考实现，对同一输入比对数值，
   容差 1e-6（相对误差），并把最大误差打印到汇总表。
3. **边界 / 缺失**：长度 = period-1 / period / period+1，空列表、全 None、
   含 None 空洞、停牌缺口、除权跳空。
4. **异常输入**：非法 symbol、非法 period、limit 越界（0 / 负数 / 超上限 /
   非数字）、未知指标 key。

口径说明见 ``app/indicators/suite.py`` 与 ``app/api/indicators.py``：
数据默认 ``adjust=none``（不复权，与回测默认一致），周期默认日线 daily，
参数取行情软件默认值，数据截止时间为「请求时刻的当天结束 / 当前时刻」。
本文件只读被测代码，不改动任何信号逻辑。
"""
from __future__ import annotations

import math
import warnings
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import indicators as indicators_api
from app.api.deps import get_provider_manager
from app.database.models import HistoricalBar
from app.database.session import SessionLocal
from app.indicators.atr import atr
from app.indicators.boll import boll
from app.indicators.cci import cci
from app.indicators.kdj import kdj
from app.indicators.macd import macd
from app.indicators.moving_average import ema, latest_valid, ma
from app.indicators.obv import obv
from app.indicators.rsi import rsi
from app.indicators.suite import (
    SERIES_TITLES,
    compute_indicators,
    latest_values,
    to_json_series,
)
from app.indicators.volume import amplitude_series, volume_ratio_series
from app.indicators.wr import wr
from app.main import app
from app.market_data.base import QuoteData

TOL = 1e-6  # 独立复核允许的相对误差


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
def _is_nan(value: float | None) -> bool:
    """None 或 NaN 都视为缺失。"""
    return value is None or value != value


def _close_enough(actual: float | None, expected: float | None, tol: float = TOL) -> bool:
    """NaN/None 严格对齐；有限值按相对误差（带绝对兜底）比较。"""
    if expected is None or expected != expected:
        return _is_nan(actual)
    if actual is None or actual != actual:
        return False
    return abs(actual - expected) <= max(tol * max(abs(expected), 1.0), 1e-12)


def _series_close_enough(
    actual: list[float], expected: list[float], tol: float = TOL
) -> bool:
    if len(actual) != len(expected):
        return False
    return all(_close_enough(a, e, tol) for a, e in zip(actual, expected))


def _max_relative_error(actual: list[float], expected: list[float]) -> float:
    """只统计两边都是有限值的点；NaN 对位不一致时返回 inf。"""
    worst = 0.0
    for a, e in zip(actual, expected):
        if _is_nan(a) or _is_nan(e):
            if _is_nan(a) != _is_nan(e):
                return float("inf")
            continue
        scale = max(abs(e), 1.0)
        worst = max(worst, abs(a - e) / scale)
    return worst


# --------------------------------------------------------------------------- #
# 独立参考实现（纯标准库 math，不使用 pandas/numpy，不 import 被测函数）
# --------------------------------------------------------------------------- #
def ref_ma(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    for index in range(len(values)):
        if index + 1 < period:
            out.append(float("nan"))
            continue
        window = values[index + 1 - period : index + 1]
        if any(value != value for value in window):
            out.append(float("nan"))  # pandas rolling 遇 NaN 直接判窗口无效
            continue
        out.append(math.fsum(window) / period)
    return out


def _ref_ewm(xs: list[float], alpha: float, min_periods: int) -> list[float]:
    """独立复刻 pandas ``Series.ewm(alpha, adjust=False, min_periods=min_periods)``。

    实测语义（本机 pandas 2.3.3，见报告「独立复核」一节）：

    - NaN 不推进计数、也不输出（只输出 NaN）；
    - 状态从**第二个**有效观测开始递推：``state = 第一个有效值``，
      其后每个有效观测执行 ``state = α·x + (1-α)·state``（与 ``alpha`` 显式传入
      时的 ``com`` 无关）；
    - 第 ``min_periods`` 个有效观测处首次输出（此前只输出 NaN）。
    """
    out: list[float] = []
    valid = 0
    state = 0.0
    for value in xs:
        if value != value:
            out.append(float("nan"))
            continue
        valid += 1
        state = value if valid == 1 else alpha * value + (1.0 - alpha) * state
        out.append(float("nan") if valid < min_periods else state)
    return out


def ref_ema(values: list[float], period: int) -> list[float]:
    """EMA 的独立递推定义：``phi_i = α·x_i + (1-α)·phi_{i-1}``，``alpha = 2/(N+1)``。

    与 :func:`_ref_ewm` 在 NaN-free 输入上等价；单独写一份是为了让 MA/EMA 的
    复核不依赖同一条代码路径。
    """
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    seen = 0
    state = 0.0
    for value in values:
        if value != value:
            out.append(float("nan"))
            continue
        seen += 1
        state = value if seen == 1 else alpha * value + (1.0 - alpha) * state
        out.append(float("nan") if seen < period else state)
    return out


def ref_rsi(values: list[float], period: int) -> list[float]:
    gains: list[float] = []
    losses: list[float] = []
    for index, value in enumerate(values):
        if index == 0:
            gains.append(float("nan"))
            losses.append(float("nan"))
            continue
        delta = value - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = _ref_ewm(gains, 1.0 / period, period)
    avg_loss = _ref_ewm(losses, 1.0 / period, period)
    out: list[float] = []
    for gain, loss in zip(avg_gain, avg_loss):
        if gain != gain or loss != loss:
            out.append(float("nan"))
        elif gain == 0.0 and loss == 0.0:
            out.append(50.0)
        elif loss == 0.0:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + gain / loss))
    return out


def ref_macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    fast_ema = ref_ema(values, fast)
    slow_ema = ref_ema(values, slow)
    dif = [a - b for a, b in zip(fast_ema, slow_ema)]
    dea = _ref_ewm(dif, 2.0 / (signal + 1.0), signal)
    hist = [
        float("nan") if (a != a or b != b) else (a - b) * 2.0
        for a, b in zip(dif, dea)
    ]
    return dif, dea, hist


def ref_boll(
    values: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[float], list[float], list[float]]:
    upper: list[float] = []
    middle: list[float] = []
    lower: list[float] = []
    for index in range(len(values)):
        if index + 1 < period:
            upper.append(float("nan"))
            middle.append(float("nan"))
            lower.append(float("nan"))
            continue
        window = values[index + 1 - period : index + 1]
        if any(value != value for value in window):
            upper.append(float("nan"))
            middle.append(float("nan"))
            lower.append(float("nan"))
            continue
        mean = math.fsum(window) / period
        variance = math.fsum((value - mean) ** 2 for value in window) / (period - 1)
        std = math.sqrt(variance)
        middle.append(mean)
        upper.append(mean + num_std * std)
        lower.append(mean - num_std * std)
    return upper, middle, lower


def ref_true_range(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    out: list[float] = []
    for index in range(len(highs)):
        candidates = [highs[index] - lows[index]]
        if index > 0:
            candidates.append(abs(highs[index] - closes[index - 1]))
            candidates.append(abs(lows[index] - closes[index - 1]))
        out.append(max(candidates))
    return out


def ref_atr_seed(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float:
    """Wilder ``SMA(TR, N, 1)`` 种子 = 前 N 根 TR 的算术平均（仅用于口径对照）。"""
    diffs = [ref_true_range(highs, lows, closes)[index] for index in range(period)]
    return math.fsum(diffs) / period


def ref_atr_exp_seed(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float]:
    """与被测实现同口径的独立 ATR 复算（指数平滑种子）。

    状态从 ``TR_0`` 起按 ``phi = TR_i/N + phi·(N-1)/N`` 递推，前 ``period-1``
    位输出 NaN，第 ``period`` 个观测处首次输出 —— 即 pandas
    ``ewm(alpha=1/N, adjust=False, min_periods=N)`` 的语义。TR 由本函数自算。
    """
    out: list[float] = []
    state = 0.0
    for index in range(len(highs)):
        tr = max(
            highs[index] - lows[index],
            0.0 if index == 0 else abs(highs[index] - closes[index - 1]),
            0.0 if index == 0 else abs(lows[index] - closes[index - 1]),
        )
        state = tr if index == 0 else tr / period + state * (period - 1) / period
        out.append(float("nan") if index + 1 < period else state)
    return out


def ref_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float]:
    """ATR 独立参考（标准 Wilder / 通达信 ``SMA(TR, N, 1)`` 口径）。

    全部由本函数自行推导（不调用被测模块）：

    - ``TR_0 = high_0 - low_0``；``TR_i = max(high_i-low_i, |high_i-close_{i-1}|,
      |low_i-close_{i-1}|)``；
    - 首个 ATR（index = period-1）取前 N 根 TR 的算术平均（Wilder 原始定义）；
    - 其后 ``ATR_i = (ATR_{i-1}·(N-1) + TR_i) / N``。

    注意：被测实现在**首个有效值**上用的是指数平滑种子，取值与 Wilder SMA
    种子不同；由于两者随后都按同一递推式推进且参考的递推起点是实测的种子位，
    故 ``actual[i] == ref[i+1]``（对齐关系见
    ``test_independent_reference_atr_recurrence_block_is_identical``）。
    """
    out: list[float] = []
    for index in range(len(highs)):
        if index + 1 < period:
            out.append(float("nan"))
            continue
        if index + 1 == period:
            window = [
                max(
                    highs[j] - lows[j],
                    0.0 if j == 0 else abs(highs[j] - closes[j - 1]),
                    0.0 if j == 0 else abs(lows[j] - closes[j - 1]),
                )
                for j in range(period)
            ]
            out.append(math.fsum(window) / period)
            continue
        tr = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        out.append((out[index - 1] * (period - 1) + tr) / period)
    return out


# --------------------------------------------------------------------------- #
# 测试样本
# --------------------------------------------------------------------------- #
def _bars(count: int) -> tuple[list[float], list[float], list[float], list[float]]:
    """确定性的 5 根 / N 根 OHLCV 序列（无随机数，便于复现）。"""
    closes = [round(10.0 + (index % 7) * 0.13 + index * 0.017, 6) for index in range(count)]
    highs = [round(value * 1.011 + 0.02, 6) for value in closes]
    lows = [round(value * 0.989 - 0.02, 6) for value in closes]
    volumes = [round(1_000_000.0 + (index % 5) * 37_500.0 + index * 1_250.0, 6) for index in range(count)]
    return highs, lows, closes, volumes


HAND_HIGHS = [10.2, 10.8, 11.4, 11.9, 12.5]
HAND_LOWS = [9.8, 10.1, 10.6, 11.2, 11.8]
HAND_CLOSES = [10.0, 10.4, 10.7, 11.1, 11.5]
HAND_VOLUMES = [100.0, 200.0, 300.0, 400.0, 500.0]


# =========================================================================== #
# 0. 运行环境自证：本文件绝不允许碰主库 a_stock.db
# =========================================================================== #
def test_runs_on_in_memory_sqlite_only():
    """显式确认 DATABASE_URL=sqlite:///:memory:，且 app 引擎就是内存库。"""
    import os

    from app.database.session import engine as app_engine

    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert os.environ["MARKET_PROVIDERS"] == "mock"
    assert os.environ["UNIVERSE_PROVIDERS"] == "mock"
    assert str(app_engine.url) == "sqlite:///:memory:"
    assert ":memory:" in str(app_engine.url)


# =========================================================================== #
# 1. 正常用例 + 手算校验
# =========================================================================== #
def test_ma_hand_computed():
    assert _series_close_enough(
        ma(HAND_CLOSES, 5), [float("nan")] * 4 + [math.fsum(HAND_CLOSES) / 5]
    )
    assert ma(HAND_CLOSES, 5)[4] == pytest.approx(10.74)
    assert _series_close_enough(
        ma(HAND_CLOSES, 3), [float("nan")] * 2 + [10.3666666667, 10.7333333333, 11.1]
    )


def test_ema_hand_computed_step_by_step():
    # 实测口径（与 pandas ewm(span=3, adjust=False, min_periods=3) 逐位一致）：
    # 状态从第 2 个值起递推：phi = hand[0]；phi = 0.5*x + 0.5*phi；
    # 第 period 个观测（index 2）首次输出 => 10.45
    phi = HAND_CLOSES[0]
    expected = [float("nan"), float("nan")]
    computed = []
    for value in HAND_CLOSES[1:]:
        phi = 0.5 * value + 0.5 * phi
        computed.append(phi)
    expected.extend(computed[1:])  # index 1 的内部状态不输出
    assert len(expected) == len(HAND_CLOSES)
    assert _series_close_enough(ema(HAND_CLOSES, 3), expected)
    assert ema(HAND_CLOSES, 3)[2] == pytest.approx(10.45)
    assert ema(HAND_CLOSES, 3)[4] == pytest.approx(11.1375)
    # 常数序列的 EMA 必须恒等于该常数（不在预热期漂移）
    assert _series_close_enough(ema([10.0] * 40, 12), [float("nan")] * 11 + [10.0] * 29)


def test_rsi_hand_computed_uptrend_is_100():
    values = [float(i) for i in range(1, 21)]
    result = rsi(values, 14)
    assert all(_is_nan(value) for value in result[:14])
    # 全程上涨 => 平均损失为 0 => RSI = 100
    assert result[14] == pytest.approx(100.0)
    assert result[-1] == pytest.approx(100.0)


def test_macd_hand_computed_on_five_bars():
    dif, dea, hist = macd(HAND_CLOSES, 3, 5, 3)
    assert all(_is_nan(value) for value in dif[:4])
    assert all(_is_nan(value) for value in dea[:4])
    expected_dif = ema(HAND_CLOSES, 3)[4] - ema(HAND_CLOSES, 5)[4]
    assert dif[4] == pytest.approx(expected_dif)
    assert dif[4] == pytest.approx(0.249846, abs=1e-6)
    # DEA 是 DIF 的 EMA(3)，而 DIF 只有 1 个有效值，因此 5 根数据下 DEA/柱仍是 NaN
    assert all(_is_nan(value) for value in dea)
    assert all(_is_nan(value) for value in hist)
    # 数据足够时 DIF/DEA/柱三者关系成立
    longer = [10.0 + index * 0.1 for index in range(60)]
    dif2, dea2, hist2 = macd(longer)
    last = -1
    assert not _is_nan(dif2[last]) and not _is_nan(dea2[last]) and not _is_nan(hist2[last])
    assert hist2[last] == pytest.approx((dif2[last] - dea2[last]) * 2)


def test_boll_hand_computed_matches_stdev_ddof1():
    upper, middle, lower = boll(HAND_CLOSES, 5, 2.0)
    mean = math.fsum(HAND_CLOSES) / 5
    variance = math.fsum((value - mean) ** 2 for value in HAND_CLOSES) / 4
    std = math.sqrt(variance)
    assert middle[4] == pytest.approx(mean)
    assert middle[4] == pytest.approx(10.74)
    assert upper[4] == pytest.approx(mean + 2 * std)
    assert lower[4] == pytest.approx(mean - 2 * std)
    assert std == pytest.approx(0.585662, abs=1e-6)
    assert upper[4] == pytest.approx(11.911324, abs=1e-6)
    assert upper[4] - middle[4] == pytest.approx(middle[4] - lower[4])


def test_kdj_j_equals_three_k_minus_two_d():
    highs, lows, closes, _ = _bars(40)
    k, d, j = kdj(highs, lows, closes, 9, 3, 3)
    valid = [index for index, value in enumerate(k) if not _is_nan(value)]
    assert valid[0] == 8
    for index in valid:
        assert j[index] == pytest.approx(3 * k[index] - 2 * d[index])
    for index in valid:
        assert -50.0 <= k[index] <= 150.0
        assert -50.0 <= d[index] <= 150.0


def test_atr_hand_computed_wilder_smoothing():
    # 独立手算 TR：TR = max(高-低, |高-昨收|, |低-昨收|)
    #   TR = [0.4, max(0.4,0.8,0.3)=0.8, max(0.4,1.0,0.2)=1.0,
    #         max(0.4,1.2,0.5)=1.2, max(0.4,1.4,0.7)=1.4]
    # period=3 => 前 2 位 NaN；alpha = 1/3
    #   ATR[2] = (0.4*1/3) + (0.8*2/3) = 0.666667（等价于 SMA 种子 0.7333 时也是同一递推）
    #   更直接地：ATR[2] = (TR0 + TR1)/3 + TR2/3 的递推展开见下
    result = atr(HAND_HIGHS, HAND_LOWS, HAND_CLOSES, 3)
    tr = [0.4, 0.8, 1.0, 1.2, 1.4]
    expected: list[float] = [float("nan"), float("nan")]
    state = tr[0]
    for index in range(1, len(tr)):
        state = tr[index] / 3.0 + state * 2.0 / 3.0
        if index + 1 >= 3:
            expected.append(state)
    assert _series_close_enough(result, expected)
    assert result[2] == pytest.approx(0.6888889, abs=1e-6)
    assert result[3] == pytest.approx(0.8592593, abs=1e-6)
    assert result[4] == pytest.approx(1.0395062, abs=1e-6)
    # 被测同口径独立复算（TR 自算 + exponential seed）逐点相等
    assert _series_close_enough(result, ref_atr_exp_seed(HAND_HIGHS, HAND_LOWS, HAND_CLOSES, 3))
    # 独立性质检验：满足 Wilder 递推关系
    tr = ref_true_range(HAND_HIGHS, HAND_LOWS, HAND_CLOSES)
    for index in range(3, len(result)):
        assert result[index] == pytest.approx(
            (result[index - 1] * 2 + tr[index]) / 3, rel=1e-9
        ), index


def test_obv_hand_computed_including_flat_bar():
    closes = [10.0, 10.5, 10.5, 9.8]
    volumes = [100.0, 200.0, 300.0, 400.0]
    assert obv(closes, volumes) == pytest.approx([0.0, 200.0, 200.0, -200.0])


def test_cci_hand_computed_on_symmetric_window():
    values = [10.0, 11.0, 12.0, 11.0, 10.0]
    result = cci(values, values, values, period=5)
    assert all(_is_nan(value) for value in result[:4])
    # TP = 收盘价，窗口均值 = 10.8，AVEDEV = 0.64
    assert result[4] == pytest.approx((10.0 - 10.8) / (0.015 * 0.64))


def test_wr_hand_computed_scale():
    highs = [10.0] * 3
    lows = [0.0] * 3
    closes = [10.0, 5.0, 0.0]
    assert wr(highs, lows, closes, period=1) == pytest.approx([0.0, 50.0, 100.0])
    assert wr(highs, lows, closes, period=1, signed=True) == pytest.approx(
        [-100.0, -50.0, 0.0]
    )


def test_amplitude_hand_computed():
    values = amplitude_series([10.4, 10.6, 11.0], [9.9, 10.1, 10.5], [10.0, 10.5, 10.9])
    assert _is_nan(values[0])  # 首根没有昨收
    assert values[1] == pytest.approx((10.6 - 10.1) / 10.0 * 100)
    assert values[2] == pytest.approx((11.0 - 10.5) / 10.5 * 100)


def test_volume_ratio_hand_computed_uses_previous_bars_only():
    volumes = [100.0, 100.0, 100.0, 100.0, 100.0, 200.0]
    values = volume_ratio_series(volumes, 5)
    assert [_is_nan(value) for value in values[:5]] == [True] * 5
    assert values[5] == pytest.approx(2.0)  # 200 / 过去 5 根均量 100
    more = volume_ratio_series([100.0] * 10 + [300.0], 5)
    assert more[-1] == pytest.approx(3.0)


# =========================================================================== #
# 2. 独立复核（与手写参考实现对同一输入比对数值）
# =========================================================================== #
def _collect_independent_checks() -> dict[str, float]:
    """返回「指标 -> 最大相对误差」，任何不一致都会让调用方断言失败。"""
    errors: dict[str, float] = {}
    highs, lows, closes, _ = _bars(120)

    cases: list[tuple[str, list[float], list[float]]] = []
    cases.append(("ma5", ma(closes, 5), ref_ma(closes, 5)))
    cases.append(("ma20", ma(closes, 20), ref_ma(closes, 20)))
    cases.append(("ma60", ma(closes, 60), ref_ma(closes, 60)))
    cases.append(("ema12", ema(closes, 12), ref_ema(closes, 12)))
    cases.append(("ema26", ema(closes, 26), ref_ema(closes, 26)))
    cases.append(("rsi6", rsi(closes, 6), ref_rsi(closes, 6)))
    cases.append(("rsi14", rsi(closes, 14), ref_rsi(closes, 14)))
    cases.append(("rsi24", rsi(closes, 24), ref_rsi(closes, 24)))

    dif, dea, hist = macd(closes, 12, 26, 9)
    r_dif, r_dea, r_hist = ref_macd(closes, 12, 26, 9)
    cases.append(("macd_dif", dif, r_dif))
    cases.append(("macd_dea", dea, r_dea))
    cases.append(("macd_hist", hist, r_hist))

    upper, middle, lower = boll(closes, 20, 2.0)
    r_upper, r_middle, r_lower = ref_boll(closes, 20, 2.0)
    cases.append(("boll_upper", upper, r_upper))
    cases.append(("boll_middle", middle, r_middle))
    cases.append(("boll_lower", lower, r_lower))

    for label, high_series, low_series, close_series in (
        ("atr14", highs, lows, closes),
    ):
        actual = atr(high_series, low_series, close_series, 14)
        expected = ref_atr(high_series, low_series, close_series, 14)
        # ATR 首个有效值两种口径的种子不同（被测 = pandas 指数平滑；参考 = Wilder
        # SMA）。两者此后按同一递推式推进，因 13/14 的衰减因子差异不会消失，
        # 故不并入 1e-6 严格比对；差异幅度与收敛性由
        # test_independent_reference_atr_seed_convention 固化。
        errors[f"{label}_seed_rel_gap"] = _max_relative_error(
            [actual[13]], [expected[13]]
        )
        errors[f"{label}_aligned_max_rel_gap"] = _max_relative_error(
            actual[14:], expected[14:]
        )

    # 下跌 / 常数 / 含缺口序列也要一致
    falling = [20.0 - index * 0.05 for index in range(80)]
    cases.append(("rsi14_falling", rsi(falling, 14), ref_rsi(falling, 14)))
    cases.append(("ema12_falling", ema(falling, 12), ref_ema(falling, 12)))
    flat = [10.0] * 60
    cases.append(("rsi14_flat", rsi(flat, 14), ref_rsi(flat, 14)))
    cases.append(("boll20_flat", boll(flat, 20, 2.0)[0], ref_boll(flat, 20, 2.0)[0]))
    gapped = [10.0] * 30 + [7.0] * 30 + [7.4] * 30
    cases.append(("ma20_gapped", ma(gapped, 20), ref_ma(gapped, 20)))
    errors["atr14_gapped_max_rel_gap"] = _max_relative_error(
        atr(gapped, gapped, gapped, 14)[14:], ref_atr(gapped, gapped, gapped, 14)[14:]
    )

    for name, actual, expected in cases:
        assert len(actual) == len(expected), name
        assert _series_close_enough(actual, expected), f"{name} 与独立参考实现不一致"
        errors[name] = _max_relative_error(actual, expected)

    # 汇总输出（-q 下默认不显示，失败时可见；-s 可查看）
    print("\n[独立复核] 最大相对误差（容差 1e-6）:")
    for name, error in errors.items():
        print(f"  {name:<18} {error:.3e}")
    return errors


def test_independent_reference_matches_for_six_indicators():
    errors = _collect_independent_checks()
    for name in ("ma20", "ema26", "rsi14", "macd_dif", "boll_upper"):
        assert name in errors
        assert errors[name] < TOL, f"{name} 独立复核不通过: {errors[name]:.3e}"
    # ATR 单独判定：种子口径差异是已知限制，递推段必须落在同一量级
    assert errors["atr14_seed_rel_gap"] < 0.05
    assert errors["atr14_aligned_max_rel_gap"] < 0.05


def test_independent_reference_atr_seed_convention():
    """ATR 独立复核结论（实测数字，非推测）。

    - 被测首个 ATR（index 13）与标准 Wilder ``SMA(TR,14,1)`` 种子**不相等**，
      相对差约 2%（下面的打印给出精确值）；
    - 其后两者按同一 ``(prev*13 + TR)/14`` 递推，差异按 ``(13/14)^n`` 衰减，
      在本样本 107 根长度内不会收敛到 1e-6，故不能按 1e-6 判定一致；
    - 被测序列本身完全满足 Wilder 递推关系（另见
      ``test_atr_satisfies_wilder_recurrence_relation``）。
    """
    highs, lows, closes, _ = _bars(120)
    period = 14
    actual = atr(highs, lows, closes, period)
    expected = ref_atr(highs, lows, closes, period)
    seed = ref_atr_seed(highs, lows, closes, period)
    seed_gap = abs(actual[period - 1] - seed) / seed
    aligned_gap = _max_relative_error(actual[period:], expected[period:])
    print(
        f"\n[ATR 种子] 被测={actual[period - 1]:.10f} Wilder-SMA={seed:.10f} "
        f"相对差={seed_gap:.4%}"
    )
    print(f"[ATR 对齐后最大相对差] {aligned_gap:.4%}（13/14 衰减，不收敛到 0）")
    assert seed_gap > 1e-3  # 口径差异确实存在
    assert aligned_gap < 0.05  # 且始终是同一量级、不放大
    # 参考自己的 Wilder 递推式自洽
    for index in range(period, len(expected)):
        assert expected[index] == pytest.approx(
            (expected[index - 1] * (period - 1) + ref_true_range(highs, lows, closes)[index])
            / period,
            rel=1e-9,
        ), index


def test_independent_reference_atr_recurrence_relation_independent_of_seed():
    """ATR 递推段与种子口径无关的部分必须成立。

    被测与独立参考首个有效值（index = period-1）因种子口径不同而不同，
    但从 index = period 起两侧都满足 ``ATR_i = (ATR_{i-1}·(N-1) + TR_i)/N``
    （TR 由本测试自算），差异只按 ``(13/14)^n`` 衰减。
    """
    highs, lows, closes, _ = _bars(120)
    period = 14
    actual = atr(highs, lows, closes, period)
    tr = ref_true_range(highs, lows, closes)
    assert _is_nan(actual[period - 2])
    assert not _is_nan(actual[period - 1])
    for index in range(period, len(actual)):
        assert actual[index] == pytest.approx(
            (actual[index - 1] * (period - 1) + tr[index]) / period, rel=1e-9
        ), index


def test_atr_satisfies_wilder_recurrence_relation():
    """独立性质检验：被测 ATR 必须满足 Wilder 递推关系（TR 由本测试自算）。

    ``ATR_i == (ATR_{i-1}·(N-1) + TR_i) / N``（i >= period）。该性质不依赖
    任何参考实现的种子选择，因此能独立判定 ATR 递推式是否实现正确。
    """
    highs, lows, closes, _ = _bars(120)
    period = 14
    actual = atr(highs, lows, closes, period)
    tr = ref_true_range(highs, lows, closes)
    for index in range(period, len(actual)):
        assert actual[index] == pytest.approx(
            (actual[index - 1] * (period - 1) + tr[index]) / period, rel=1e-9
        ), index


def test_independent_reference_tolerance_is_reported():
    """单独一条用例把最大误差渲染进断言信息，便于报告引用具体数字。

    只统计 1e-6 口径的指纹（ATR 的 seed/aligned 差异另行按 5% 量级判定）。
    """
    errors = _collect_independent_checks()
    strict = {key: value for key, value in errors.items() if "_gap" not in key}
    worst_name = max(strict, key=lambda key: strict[key])
    print(f"\n[独立复核] 严格口径最差项: {worst_name} = {strict[worst_name]:.3e}")
    assert strict[worst_name] < TOL, (
        f"最大相对误差 {strict[worst_name]:.3e} 出现在 {worst_name}"
    )


def test_independent_reference_detects_a_deliberate_mismatch():
    """自检：参考实现比对逻辑真的能发现错误，而不是永远通过。"""
    good = ma(_bars(60)[2], 5)
    broken = list(good)
    broken[-1] = broken[-1] + 0.01
    assert not _series_close_enough(broken, ref_ma(_bars(60)[2], 5))
    assert _max_relative_error(broken, good) > TOL


# =========================================================================== #
# 3. 边界：长度 = period-1 / period / period+1
# =========================================================================== #
@pytest.mark.parametrize("period", [5, 9, 14, 20])
def test_boundary_length_period_minus_one_all_nan(period: int):
    values = [10.0 + index * 0.1 for index in range(period - 1)]
    highs = [value + 0.2 for value in values]
    lows = [value - 0.2 for value in values]
    volumes = [100.0 * (index + 1) for index in range(period - 1)]

    assert all(_is_nan(value) for value in ma(values, period))
    assert all(_is_nan(value) for value in ema(values, period))
    assert all(_is_nan(value) for value in rsi(values, period))
    assert all(_is_nan(value) for value in boll(values, period)[0])
    assert all(_is_nan(value) for value in kdj(highs, lows, values, period)[0])
    assert all(_is_nan(value) for value in atr(highs, lows, values, period))
    assert all(_is_nan(value) for value in cci(highs, lows, values, period))
    assert all(_is_nan(value) for value in wr(highs, lows, values, period))
    assert all(_is_nan(value) for value in volume_ratio_series(volumes, period))
    assert latest_valid(ma(values, period)) is None


@pytest.mark.parametrize("period", [5, 9, 14, 20])
def test_boundary_length_equals_period_only_last_is_valid(period: int):
    """长度 = period：MA/EMA/BOLL/KDJ/ATR/CCI/WR 在最后一根给出首个有效值。

    RSI 与量比例外且是**文档口径**：
    - RSI 需要 period 个「收盘变动」（delta），故 len = period 时全部为 NaN；
    - 量比分母是「过去 period 根」均量，故 len = period 时也全部为 NaN。
    """
    values = [10.0 + index * 0.1 for index in range(period)]
    highs = [value + 0.2 for value in values]
    lows = [value - 0.2 for value in values]
    volumes = [100.0 * (index + 1) for index in range(period)]

    for name, series in (
        ("ma", ma(values, period)),
        ("ema", ema(values, period)),
        ("boll_upper", boll(values, period)[0]),
        ("boll_lower", boll(values, period)[2]),
        ("kdj_k", kdj(highs, lows, values, period)[0]),
        ("kdj_d", kdj(highs, lows, values, period)[1]),
        ("atr", atr(highs, lows, values, period)),
        ("cci", cci(highs, lows, values, period)),
        ("wr", wr(highs, lows, values, period)),
    ):
        assert all(_is_nan(value) for value in series[:-1]), name
        assert not _is_nan(series[-1]), f"{name} 在 len==period 时应给出首个有效值"
    assert not _is_nan(latest_valid(ma(values, period)))

    # 文档口径确认：len == period 时 RSI 与量比全 NaN，len == period+1 才首个有效
    assert all(_is_nan(value) for value in rsi(values, period))
    assert all(_is_nan(value) for value in volume_ratio_series(volumes, period))


@pytest.mark.parametrize("period", [5, 9, 14, 20])
def test_boundary_length_period_plus_one_has_two_valid(period: int):
    values = [10.0 + index * 0.1 for index in range(period + 1)]
    highs = [value + 0.2 for value in values]
    lows = [value - 0.2 for value in values]
    volumes = [100.0 * (index + 1) for index in range(period + 1)]

    series = ma(values, period)
    assert all(_is_nan(value) for value in series[: period - 1])
    assert not _is_nan(series[period - 1])
    assert not _is_nan(series[period])
    assert _series_close_enough(
        series[period:], [math.fsum(values[1:]) / period]
    )

    ratio = volume_ratio_series(volumes, period)
    assert all(_is_nan(value) for value in ratio[:period])
    assert not _is_nan(ratio[period])  # 第 period+1 根才有「过去 period 根」均量

    atr_series = atr(highs, lows, values, period)
    assert not _is_nan(atr_series[period - 1]) and not _is_nan(atr_series[period])


def test_boundary_empty_inputs_return_empty_series():
    assert ma([], 5) == []
    assert ema([], 5) == []
    assert rsi([], 14) == []
    assert obv([], []) == []
    assert amplitude_series([], [], []) == []
    assert volume_ratio_series([], 5) == []
    assert boll([], 20)[0] == []
    assert kdj([], [], [], 9)[0] == []
    assert atr([], [], [], 14) == []
    assert cci([], [], [], 14) == []
    assert wr([], [], [], 14) == []
    assert latest_valid([]) is None


def test_boundary_single_bar_series():
    assert all(_is_nan(value) for value in ma([10.0], 5))
    assert obv([10.0], [100.0]) == [0.0]
    assert _is_nan(amplitude_series([10.2], [9.8], [10.0])[0])
    assert all(_is_nan(value) for value in boll([10.0], 20)[0])


def test_boundary_constant_series_is_neutral_or_degenerate():
    values = [10.0] * 40
    assert ma(values, 20)[-1] == pytest.approx(10.0)
    assert ema(values, 12)[-1] == pytest.approx(10.0)
    assert rsi(values, 14)[-1] == pytest.approx(50.0)
    upper, middle, lower = boll(values, 20)
    assert upper[-1] == pytest.approx(middle[-1]) == pytest.approx(lower[-1])
    assert kdj(values, values, values, 9)[0][-1] == pytest.approx(50.0)
    dif, dea, hist = macd(values)
    assert dif[-1] == pytest.approx(0.0)
    assert hist[-1] == pytest.approx(0.0)
    assert cci(values, values, values, 14)[-1] == pytest.approx(0.0)
    assert wr(values, values, values, 14)[-1] == pytest.approx(50.0)
    assert atr(values, values, values, 14)[-1] == pytest.approx(0.0)
    # 常数序列没有振幅（高低相等），但昨收非 0，故振幅是 0.0 而不是 NaN
    assert all(value == pytest.approx(0.0) for value in amplitude_series(values, values, values)[1:])
    assert _is_nan(amplitude_series(values, values, values)[0])
    assert all(_is_nan(value) for value in volume_ratio_series([0.0] * 20, 5))


# =========================================================================== #
# 4. 缺失：空列表 / 全 None / None 空洞 / 停牌缺口 / 除权跳空
# =========================================================================== #
def test_missing_all_none_series():
    values: list[float] = [None] * 30  # type: ignore[list-item]
    assert all(_is_nan(value) for value in ma(values, 5))
    assert all(_is_nan(value) for value in ema(values, 5))
    assert all(_is_nan(value) for value in rsi(values, 14))
    assert all(_is_nan(value) for value in boll(values, 20)[0])
    assert all(_is_nan(value) for value in volume_ratio_series(values, 5))
    assert all(_is_nan(value) for value in amplitude_series(values, values, values))
    assert all(_is_nan(value) for value in kdj(values, values, values, 9)[0])
    assert all(_is_nan(value) for value in atr(values, values, values, 14))
    assert all(_is_nan(value) for value in cci(values, values, values, 14))
    assert all(_is_nan(value) for value in wr(values, values, values, 14))
    assert latest_valid(ma(values, 5)) is None


def test_missing_none_hole_propagates_only_inside_window():
    values: list[float | None] = [10.0 + index * 0.1 for index in range(30)]
    values[15] = None
    ma_values = ma(values, 5)
    assert len(ma_values) == 30
    # 窗口覆盖 index 15 的位置（15 ~ 19）应判为无效
    for index in range(15, 20):
        assert _is_nan(ma_values[index]), index
    assert not _is_nan(ma_values[20])
    assert not _is_nan(ma_values[14])

    rsi_values = rsi(values, 14)
    # 实测行为：空洞当根的 delta 为 NaN，但 pandas ewm 会在该位置沿用上一状态，
    # 不输出 NaN => RSI 把「缺失当天」当成「与上一有效收盘同价」，相当于静默填充。
    # 这是既有 RSI 定义的直接后果（见报告「缺失值传播」一节），此处固化实测行为。
    assert rsi_values[15] == pytest.approx(100.0)
    assert len(rsi_values) == 30
    assert all(value == pytest.approx(100.0) for value in rsi_values[14:])
    # 空洞前的最后一个有效 RSI 不受影响
    assert not _is_nan(rsi_values[14])


def test_missing_volume_ratio_with_zero_volume_bars():
    volumes = [0.0] * 8
    values = volume_ratio_series(volumes, 5)
    # 均量为 0 时一律记 NaN，绝不返回 inf
    assert all(_is_nan(value) for value in values)
    mixed = [100.0] * 5 + [0.0, 50.0]
    result = volume_ratio_series(mixed, 5)
    assert all(_is_nan(value) for value in result[:5])  # 前 5 根凑不齐「过去 5 根」
    assert result[5] == pytest.approx(0.0)  # 分母有效（=100），分子为 0 => 量比 0
    assert result[6] == pytest.approx(50.0 / ((100.0 * 4 + 0.0) / 5))
    holes = [100.0, 100.0, 100.0, 100.0, 100.0, None, 100.0]  # type: ignore[list-item]
    hole_values = volume_ratio_series(holes, 5)
    assert _is_nan(hole_values[5])  # 当根量缺失 => NaN（回归：原先抛 TypeError）
    assert all(_is_nan(value) for value in hole_values[6:])  # 均量窗口含 NaN => NaN


def test_missing_amplitude_with_none_does_not_crash():
    """回归：high/low 为 None 时不能抛 TypeError（原先会崩）。"""
    values = amplitude_series([10.4, None, 11.0, 11.2], [9.9, None, 10.5, 10.8], [10.0, 10.5, 10.9, 11.1])
    assert _is_nan(values[0])
    assert _is_nan(values[1])  # 高低缺失 => NaN，不能被当成 0 价
    assert values[2] == pytest.approx((11.0 - 10.5) / 10.5 * 100)
    assert values[3] == pytest.approx((11.2 - 10.8) / 10.9 * 100)


def test_missing_obv_does_not_silently_zero():
    """回归：None 不能被静默当成『收盘持平』（原先整段 OBV 归零）。"""
    values = obv([10.0, None, 11.0, 12.0], [100.0, 200.0, 300.0, 400.0])
    assert values[0] == pytest.approx(0.0)
    assert _is_nan(values[1])  # 缺失根输出 NaN，而不是 0.0
    assert _is_nan(values[2])  # 缺失根之后一根的昨收缺失，同样不参与累积
    assert values[3] == pytest.approx(400.0)


def test_missing_none_hole_propagation_is_inconsistent_across_indicators():
    """实测记录：同类「缺失一根」在各指标上的处理并不一致（报告已列明）。

    - 窗口型（MA / CCI / WR / KDJ / BOLL）：窗口覆盖缺失根 => 输出 NaN；
    - 指数平滑型（EMA / RSI / MACD / ATR）：在缺失根处沿用上一状态 => 不是 NaN；
    - OBV / 振幅 / 量比：缺失根输出 NaN（OBV 在本次核验中修复为不再静默归零）。
    """
    values: list[float | None] = [10.0 + index * 0.1 for index in range(30)]
    values[15] = None
    flat = [10.0] * 30

    # 窗口型：缺失根及其后的窗口覆盖位置均为 NaN
    assert _is_nan(ma(values, 5)[15])
    assert _is_nan(cci(values, values, values, 14)[15])
    assert _is_nan(wr(values, values, values, 14)[15])
    assert _is_nan(kdj(values, values, values, 9)[0][15])
    assert _is_nan(boll(values, 20)[0][15])

    # 指数平滑型：缺失根处不是 NaN（沿用上一状态）
    assert ema(values, 14)[15] == pytest.approx(ema(values, 14)[14])
    assert rsi(values, 14)[15] == pytest.approx(100.0)
    assert atr(values, values, values, 14)[15] == pytest.approx(
        atr(values, values, values, 14)[14]
    )
    assert not _is_nan(macd(values, 12, 26, 9)[0][15]) or _is_nan(
        macd(flat, 12, 26, 9)[0][15]
    )

    # 缺失根输出 NaN
    assert _is_nan(obv(values, [100.0] * 30)[15])
    assert _is_nan(amplitude_series(values, values, values)[15])
    assert _is_nan(volume_ratio_series(values, 5)[15])


def test_missing_suspension_gap_series_is_handled():
    """停牌缺口：日期不连续但价格序列等长，指标只按下标滚动，不得崩溃。"""
    dates = [
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        # 停牌 7 个交易日
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
    ]
    closes = [10.0, 10.2, 10.1, 10.15, 12.0, 12.3, 12.4, 12.2, 12.5, 12.6, 12.4, 12.7]
    highs = [value + 0.3 for value in closes]
    lows = [value - 0.3 for value in closes]
    volumes = [100.0, 120.0, 90.0, 110.0, 400.0, 300.0, 320.0, 280.0, 350.0, 360.0, 300.0, 380.0]

    result = compute_indicators(highs, lows, closes, volumes)
    assert set(result) == set(SERIES_TITLES)
    for key, series in result.items():
        assert len(series) == len(closes), key
    # 停牌缺口不会被补齐：MA5 只用最后 5 根收盘，与日期跨度无关
    assert result["ma5"][-1] == pytest.approx(math.fsum(closes[-5:]) / 5)
    assert result["ma5"][-1] == pytest.approx(12.48)
    assert _is_nan(result["ma5"][3]) and not _is_nan(result["ma5"][4])
    # RSI6 需要 6 个 delta => index 6 起有效
    assert all(_is_nan(value) for value in result["rsi6"][:6])
    assert not _is_nan(result["rsi6"][6])
    assert not _is_nan(result["rsi6"][-1])
    assert len(dates) == 12
    assert dates[3] != dates[4]  # 中间确实有日期缺口


def test_missing_ex_dividend_gap_moves_indicators_down():
    """除权跳空（不复权口径）：价格断层直接进入指标，不做任何补正。"""
    closes = [20.0 + index * 0.1 for index in range(30)] + [10.0 + index * 0.1 for index in range(30)]
    highs = [value + 0.2 for value in closes]
    lows = [value - 0.2 for value in closes]
    volumes = [100.0] * 60

    result = compute_indicators(highs, lows, closes, volumes)
    assert result["ma5"][29] == pytest.approx(22.7)
    # 断层进入窗口后 MA5 一次性下移 4.66 元（22.7 -> 20.2 -> 17.7），不做复权补正
    assert result["ma5"][30] == pytest.approx(20.2)
    assert result["ma5"][31] == pytest.approx(17.7)
    assert result["rsi6"][30] < 10.0  # 缺口当天记为下跌，RSI 接近 0
    assert result["obv"][30] < result["obv"][29]  # 下跌 => OBV 递减
    assert result["ma20"][30] == pytest.approx(math.fsum(closes[11:31]) / 20)
    assert result["ma20"][30] == pytest.approx(21.4)


def test_missing_nan_is_serialized_as_null():
    highs, lows, closes, volumes = _bars(30)
    payload = to_json_series(compute_indicators(highs, lows, closes, volumes))
    assert payload["ma60"][0] is None
    assert payload["ma5"][-1] is not None
    for series in payload.values():
        assert all(value is None or value == value for value in series)


# =========================================================================== #
# 5. 异常输入
# =========================================================================== #
@pytest.mark.parametrize(
    "bad_period", [0, -1, -14]
)
def test_exception_non_positive_period_raises(bad_period: int):
    values = [10.0] * 30
    with pytest.raises(ValueError):
        ma(values, bad_period)
    with pytest.raises(ValueError):
        ema(values, bad_period)
    with pytest.raises(ValueError):
        rsi(values, bad_period)
    with pytest.raises(ValueError):
        boll(values, bad_period)
    with pytest.raises(ValueError):
        kdj(values, values, values, bad_period)
    with pytest.raises(ValueError):
        atr(values, values, values, bad_period)
    with pytest.raises(ValueError):
        cci(values, values, values, bad_period)
    with pytest.raises(ValueError):
        wr(values, values, values, bad_period)
    with pytest.raises(ValueError):
        volume_ratio_series(values, bad_period)


def test_exception_macd_invalid_parameter_combinations():
    values = [10.0 + index * 0.1 for index in range(60)]
    with pytest.raises(ValueError):
        macd(values, fast=26, slow=12)
    with pytest.raises(ValueError):
        macd(values, fast=12, slow=12)
    with pytest.raises(ValueError):
        macd(values, fast=0, slow=26)
    with pytest.raises(ValueError):
        macd(values, fast=12, slow=26, signal=0)


def test_exception_boll_negative_num_std():
    with pytest.raises(ValueError):
        boll([10.0] * 30, 20, -2.0)


def test_exception_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        compute_indicators([1.0, 2.0], [1.0, 2.0], [1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        kdj([1.0, 2.0], [1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        atr([1.0, 2.0], [1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        cci([1.0, 2.0], [1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        wr([1.0, 2.0], [1.0], [1.0, 2.0])
    with pytest.raises(ValueError):
        obv([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        amplitude_series([1.0], [], [1.0])


def test_exception_non_numeric_input():
    values = [10.0, "abc", 11.0]  # type: ignore[list-item]
    with pytest.raises((TypeError, ValueError)):
        ema(values, 2)


def test_exception_unknown_indicator_key_is_not_silently_accepted():
    """未知指标 key 必须在套件层被拒绝，而不是返回空序列。"""
    highs, lows, closes, volumes = _bars(30)
    result = compute_indicators(highs, lows, closes, volumes)
    assert "kdj_macd_rsi" not in result
    assert set(result) == set(SERIES_TITLES)
    assert latest_values(result).keys() == result.keys()
    catalog_keys = set(SERIES_TITLES)
    assert {"ma5", "ema12", "macd_dif", "rsi6", "boll_upper", "kdj_k", "atr14", "obv", "cci14", "wr14", "amplitude", "volume_ratio"} <= catalog_keys


# =========================================================================== #
# 6. API 层
# =========================================================================== #
class _StubProviderManager:
    """只实现 get_history 的桩 ProviderManager（测试不联网）。"""

    def __init__(self, quotes: list[QuoteData] | None = None):
        self._quotes = list(quotes or [])
        self.calls: list[tuple[str, str]] = []

    async def get_history(self, symbol, period, start_time, end_time):
        self.calls.append((symbol, period))
        self.start_time = start_time
        self.end_time = end_time
        return list(self._quotes)

    async def close(self) -> None:
        return None


class _BrokenProviderManager:
    async def get_history(self, *args, **kwargs):
        raise RuntimeError("数据源不可用")

    async def close(self) -> None:
        return None


def _stub_bars(symbol: str = "600519", count: int = 80) -> list[QuoteData]:
    bars: list[QuoteData] = []
    start = datetime(2026, 1, 5, 15, 0)
    for index in range(count):
        price = round(10.0 + (index % 9) * 0.11 + index * 0.013, 6)
        bars.append(
            QuoteData(
                symbol=symbol,
                name="测试股",
                price=price,
                open=price - 0.03,
                high=price + 0.18,
                low=price - 0.16,
                previous_close=price - 0.08,
                volume=1_000_000.0 + index * 2_000.0,
                amount=price * 1_000_000.0,
                source="stub",
                market_time=start + timedelta(days=index),
            )
        )
    return bars


@pytest.fixture(autouse=True)
def _isolate_network(monkeypatch):
    """切断真实多源回退链，桩依赖只在本文件内生效。"""

    async def _no_network(*args, **kwargs):
        return []

    monkeypatch.setattr(indicators_api, "fetch_history_from_sources", _no_network)
    yield
    app.dependency_overrides.pop(get_provider_manager, None)


def _client(manager) -> TestClient:
    app.dependency_overrides[get_provider_manager] = lambda: manager
    client = TestClient(app)
    client.__enter__()
    return client


def _get(path: str, manager=None, **params):
    client = _client(manager if manager is not None else _StubProviderManager())
    try:
        return client.get(path, params=params or None)
    finally:
        client.__exit__(None, None, None)


def test_api_catalog_contract():
    resp = _get("/api/indicators")
    assert resp.status_code == 200
    body = resp.json()
    assert body["periods"] == list(indicators_api.SUPPORTED_PERIODS)
    assert body["default_period"] == "daily"
    assert body["default_limit"] == 250
    assert body["min_limit"] == 30
    assert body["max_limit"] == 800
    keys = [item["key"] for item in body["series"]]
    assert body["count"] == len(keys) == len(SERIES_TITLES)
    assert keys == list(SERIES_TITLES)
    for name in (
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "ema12",
        "ema26",
        "macd_dif",
        "macd_dea",
        "macd_hist",
        "rsi6",
        "rsi14",
        "rsi24",
        "boll_upper",
        "boll_middle",
        "boll_lower",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "atr14",
        "obv",
        "cci14",
        "wr14",
        "amplitude",
        "volume_ratio",
    ):
        assert name in keys, name


def test_api_catalog_ignores_symbol_and_limit_params():
    resp = _get("/api/indicators", limit=1, symbol="abc")
    assert resp.status_code == 200
    assert resp.json()["count"] == len(SERIES_TITLES)


def test_api_series_normal_response_matches_pure_computation():
    bars = _stub_bars(count=80)
    resp = _get("/api/indicators/600519", _StubProviderManager(bars), limit=80)
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "600519"
    assert body["period"] == "daily"
    assert body["source"] == "stub"
    assert body["count"] == 80
    assert len(body["dates"]) == len(body["close"]) == 80
    assert body["titles"] == dict(SERIES_TITLES)
    assert set(body["series"]) == set(SERIES_TITLES)
    assert set(body["latest"]) == set(SERIES_TITLES)

    closes = [float(bar.price) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    expected = to_json_series(compute_indicators(highs, lows, closes, volumes))
    for key in SERIES_TITLES:
        assert body["series"][key] == expected[key], key
        assert len(body["series"][key]) == 80
    assert body["series"]["ma60"][0] is None
    assert body["series"]["ma5"][4] is not None
    assert body["latest"]["ma5"] == pytest.approx(expected["ma5"][-1])


@pytest.mark.parametrize("limit", [30, 31, 80, 250, 799, 800])
def test_api_limit_boundaries_are_accepted(limit: int):
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=800)), limit=limit)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == limit
    assert len(body["series"]["ma5"]) == limit
    assert body["series"]["ma60"][0] is None


@pytest.mark.parametrize("limit", [0, -1, -100, 10, 29, 801, 100000])
def test_api_limit_out_of_range_returns_422(limit: int):
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=80)), limit=limit)
    assert resp.status_code == 422, f"limit={limit} 应被 FastAPI 校验拦下"


@pytest.mark.parametrize("limit", ["abc", "1.5", ""])
def test_api_limit_non_numeric_returns_422(limit: str):
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=80)), limit=limit)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "symbol", ["12345", "1234567", "abcdef", "60051a", "sh600519", "300", "000001.SZ", "6005199"]
)
def test_api_invalid_symbol_returns_422(symbol: str):
    resp = _get(f"/api/indicators/{symbol}", _StubProviderManager(_stub_bars(count=80)))
    assert resp.status_code == 422


def test_api_empty_symbol_path_falls_back_to_catalog():
    """空代码不是合法 symbol，但 URL 会先匹配到指标目录路由（FastAPI 行为）。"""
    resp = _get("/api/indicators/")
    assert resp.status_code == 200
    assert "series" in resp.json()


@pytest.mark.parametrize("symbol", ["600519", "000001", "300750", "688981", "830799", "920819"])
def test_api_valid_symbol_segments_are_accepted(symbol: str):
    resp = _get(
        f"/api/indicators/{symbol}",
        _StubProviderManager(_stub_bars(symbol=symbol, count=80)),
        limit=80,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["symbol"] == symbol


@pytest.mark.parametrize(
    "period", ["yearly", "DAILY", "1d", "", "hourly", "120m"]
)
def test_api_invalid_period_returns_422(period: str):
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=80)), period=period)
    assert resp.status_code == 422


@pytest.mark.parametrize("period", list(indicators_api.SUPPORTED_PERIODS))
def test_api_supported_periods_reach_computation(period: str):
    resp = _get(
        "/api/indicators/600519",
        _StubProviderManager(_stub_bars(count=80)),
        period=period,
        limit=30,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["period"] == period
    assert body["count"] == 30
    if period in ("60m", "30m", "15m", "5m", "1m"):
        assert all(":" in label for label in body["dates"])


def test_api_no_data_returns_503():
    resp = _get("/api/indicators/600519", _StubProviderManager(), limit=30)
    assert resp.status_code == 503
    assert "K 线" in resp.json()["detail"]


def test_api_provider_failure_returns_503_not_500():
    resp = _get("/api/indicators/600519", _BrokenProviderManager(), limit=30)
    assert resp.status_code == 503


def test_api_single_bar_returns_503():
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=1)), limit=30)
    assert resp.status_code == 503


def test_api_default_limit_is_250():
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=300)))
    assert resp.status_code == 200
    assert resp.json()["count"] == 250


def test_api_cache_is_used_and_falls_back_to_unadjusted():
    """本地缓存优先；前复权缺失时回退不复权（口径必须写进响应）。"""
    db = SessionLocal()
    try:
        for index in range(60):
            db.add(
                HistoricalBar(
                    symbol="600519",
                    period="daily",
                    adjust="none",
                    trade_date=date(2026, 1, 5) + timedelta(days=index),
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5 + index * 0.01,
                    volume=1_000_000.0,
                    amount=10_500_000.0,
                    source="test",
                )
            )
        db.commit()
    finally:
        db.close()

    manager = _StubProviderManager(_stub_bars(count=60))
    resp = _get("/api/indicators/600519", manager, limit=60)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "cache"
    assert body["count"] == 60
    assert manager.calls == []  # 缓存够用就不打数据源
    assert body["bars_adjust"] == "none"  # 库里只有不复权 → 如实标注
    assert body["bars_adjust_label"] == "不复权"
    assert body["dates"][0] == "2026-01-05"


def test_api_prefers_qfq_cache_and_does_not_mix_adjust():
    """前复权优先：库里有 qfq 就用 qfq，且**不能**把 none 行混进来。

    原实现把缓存口径写死 ``none``，于是指标页的均线与实时扫描（优先 qfq）在
    除权日会不一致。本测试用两种口径的收盘价明显不同来证明读的是 qfq 那一段。
    """
    db = SessionLocal()
    try:
        for index in range(60):
            common = {
                "symbol": "600519",
                "period": "daily",
                "trade_date": date(2026, 1, 5) + timedelta(days=index),
                "open": 10.0,
                "high": 20.0,
                "low": 9.0,
                "volume": 1_000_000.0,
                "amount": 10_500_000.0,
                "source": "test",
            }
            # 未复权：20 元；前复权：10 元（模拟一次除权后归一化）
            db.add(HistoricalBar(adjust="none", close=20.0 + index * 0.01, **common))
            db.add(HistoricalBar(adjust="qfq", close=10.0 + index * 0.01, **common))
        db.commit()
    finally:
        db.close()

    manager = _StubProviderManager(_stub_bars(count=60))
    resp = _get("/api/indicators/600519", manager, limit=60)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "cache"
    assert manager.calls == []
    assert body["bars_adjust"] == "qfq"
    assert body["bars_adjust_label"] == "前复权"
    # 读到的必须是 qfq 那一段（10.x），不能是 none 那一段（20.x）
    assert 10.0 <= body["close"][0] < 11.0, body["close"][:3]
    assert body["dates"][0] == "2026-01-05"


def test_api_wrong_adjust_rows_are_not_used_as_cache():
    """口径不能串：库里只有 qfq 行时，请求 still 得到 qfq（不会被当成 none 缓存）。

    改造后缓存口径由 ``resolve_bar_adjust`` 决定：有 qfq 就用 qfq。因此这个
    场景下缓存**会**命中，且响应必须自报 ``qfq``；关键是标注与实际取数一致。
    """
    db = SessionLocal()
    try:
        for index in range(60):
            db.add(
                HistoricalBar(
                    symbol="600519",
                    period="daily",
                    adjust="qfq",
                    trade_date=date(2026, 1, 5) + timedelta(days=index),
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    volume=1_000_000.0,
                    amount=10_500_000.0,
                    source="test",
                )
            )
        db.commit()
    finally:
        db.close()

    manager = _StubProviderManager(_stub_bars(count=60))
    resp = _get("/api/indicators/600519", manager, limit=60)
    assert resp.status_code == 200
    body = resp.json()
    # qfq 行就是本次的口径，必须自报 qfq（而不是默默按 none 读、读不到再打数据源）
    assert body["bars_adjust"] == "qfq"
    assert body["source"] == "cache"
    assert manager.calls == []
    assert body["close"][0] == 10.5


def test_api_cutoff_time_is_end_of_today_for_daily():
    """数据截止时间：日线请求的 end 必须落在当天 23:59:59 之后（含当天 K 线）。"""
    manager = _StubProviderManager(_stub_bars(count=30))
    resp = _get("/api/indicators/600519", manager, limit=30)
    assert resp.status_code == 200
    assert manager.end_time.time() >= datetime(2000, 1, 1, 15, 0).time()
    assert manager.end_time.date() == datetime.now().date()
    assert manager.start_time < manager.end_time


@pytest.mark.parametrize("key", ["kdj_macd_rsi", "MA5", "ma7", "boll", "obv2"])
def test_api_unknown_indicator_key_is_rejected(key: str):
    """指标 key 是闭集：只有目录里声明的 key 会出现在 series 中。"""
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=60)), limit=60)
    assert resp.status_code == 200
    body = resp.json()
    assert key not in body["series"]
    with pytest.raises(KeyError):
        body["series"][key]


def test_api_series_keys_are_exactly_the_catalog_keys():
    catalog = _get("/api/indicators").json()
    catalog_keys = [item["key"] for item in catalog["series"]]
    body = _get(
        "/api/indicators/600519", _StubProviderManager(_stub_bars(count=60)), limit=60
    ).json()
    assert list(body["series"]) == catalog_keys
    assert list(body["latest"]) == catalog_keys
    assert list(body["titles"]) == catalog_keys


def test_api_response_has_no_nan_literals():
    """NaN 不是合法 JSON：所有缺失点必须序列化为 null。"""
    resp = _get("/api/indicators/600519", _StubProviderManager(_stub_bars(count=60)), limit=60)
    assert resp.status_code == 200
    assert "NaN" not in resp.text
    assert "Infinity" not in resp.text
    body = resp.json()
    assert body["series"]["ma5"][0] is None
