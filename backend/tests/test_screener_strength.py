"""连续强度打分（`trigger_strengths`）与排名梯度的测试。

2026-09-14 变更背景（实测）：原打分是 0/1（命中给满权重），8 个条件只有 2^8=256 种组合，
盘中实测前 10 名**全部 86.0 分并列**，排序实际退化为「按 20 日均额排」，
对「哪只更好」几乎不含信息量。现在：
* `score` 语义不变（命中权重和，0~100）；
* 新增 `strength_score`（0~100）= Σ 权重 × 连续强度，**排名以它为主键**。
"""
from __future__ import annotations

from app.realtime.screener import (
    BOLL_PULLBACK_MAX_POS,
    STABLE_VOL_CEILING,
    TRIGGER_WEIGHTS,
    VOLUME_EXPAND_FULL,
    VOLUME_EXPAND_MIN,
    trigger_strengths,
)

#: 一组「全部条件都舒服命中」的基准输入（便于逐条改动）
BASE = {
    "price": 10.30,          # 高于 ma20 3%
    "ma20": 10.0,
    "ma60": 9.80,            # ma20 高于 ma60 约 2%
    "momentum_20": 0.10,     # 20 日动量 10%
    "rsi14": 50.0,           # 健康区正中
    "macd_hist": 0.031,
    "macd_hist_prev": 0.010,  # 改善 0.021 / 10.30 ≈ 0.204% ≥ 0.2% → 记满
    "boll_position": 0.0,    # 贴近下轨
    "volatility_20": 0.0,    # 波动为 0
    "volume_ratio_20": 2.0,  # 量比 2.0
}


def _strengths(**overrides) -> dict[str, float]:
    return trigger_strengths(**{**BASE, **overrides})


def test_all_conditions_at_full_strength_sum_to_100():
    strengths = _strengths()
    assert all(0.0 <= v <= 1.0 for v in strengths.values()), strengths
    total = sum(TRIGGER_WEIGHTS[k] * v for k, v in strengths.items())
    assert total == 100.0


def test_strength_is_zero_when_condition_not_hit():
    """强度为 0 ⇔ 条件未命中 —— 与 `_refine` 里的布尔判定必须一致。"""
    cases = {
        "above_ma20": {"price": 9.99},
        "ma20_above_ma60": {"ma20": 9.79},
        "momentum_positive": {"momentum_20": -0.01},
        "rsi_rebound": {"rsi14": 30.0},
        "macd_hist_turn": {"macd_hist": 0.005, "macd_hist_prev": 0.010},
        "boll_pullback": {"boll_position": 0.80},
        "stable_volatility": {"volatility_20": STABLE_VOL_CEILING + 0.01},
        "volume_expand": {"volume_ratio_20": VOLUME_EXPAND_MIN - 0.01},
    }
    for key, override in cases.items():
        assert _strengths(**override)[key] == 0.0, key


def test_strength_gradient_monotone_for_ma_and_momentum():
    """站上均线幅度越大、动量越大 → 强度越高（单调不减）。"""
    weak = _strengths(price=10.05)["above_ma20"]    # +0.5%
    mid = _strengths(price=10.15)["above_ma20"]     # +1.5%
    strong = _strengths(price=10.30)["above_ma20"]  # +3.0% → 满
    assert 0 < weak < mid < strong
    assert strong == 1.0

    m_weak = _strengths(momentum_20=0.01)["momentum_positive"]
    m_strong = _strengths(momentum_20=0.08)["momentum_positive"]
    assert 0 < m_weak < m_strong < 1.0


def test_rsi_strength_peaks_at_center_and_vanishes_at_bounds():
    """RSI 越靠近 50 越强，35/65 处为 0（"健康区中心更优"）。"""
    assert _strengths(rsi14=50.0)["rsi_rebound"] == 1.0
    assert _strengths(rsi14=35.0)["rsi_rebound"] == 0.0
    assert _strengths(rsi14=65.0)["rsi_rebound"] == 0.0
    near = _strengths(rsi14=45.0)["rsi_rebound"]
    far = _strengths(rsi14=62.0)["rsi_rebound"]
    assert near > far


def test_boll_pullback_stronger_near_lower_band():
    assert _strengths(boll_position=0.0)["boll_pullback"] == 1.0
    assert _strengths(boll_position=BOLL_PULLBACK_MAX_POS)["boll_pullback"] == 0.0
    assert (
        _strengths(boll_position=0.2)["boll_pullback"]
        > _strengths(boll_position=0.5)["boll_pullback"]
    )


def test_volatility_and_volume_ramps():
    assert _strengths(volatility_20=0.0)["stable_volatility"] == 1.0
    assert _strengths(volatility_20=STABLE_VOL_CEILING)["stable_volatility"] == 0.0
    assert _strengths(volume_ratio_20=VOLUME_EXPAND_MIN)["volume_expand"] == 0.0
    assert _strengths(volume_ratio_20=VOLUME_EXPAND_FULL)["volume_expand"] == 1.0
    assert (
        _strengths(volume_ratio_20=1.6)["volume_expand"]
        > _strengths(volume_ratio_20=1.3)["volume_expand"]
    )


def test_missing_indicators_do_not_crash_and_give_zero():
    """布林位置缺失（数据不足）时该条强度为 0，而不是抛异常。"""
    assert _strengths(boll_position=None)["boll_pullback"] == 0.0


def test_same_hit_set_can_produce_different_strength_score():
    """**核心回归**：同样命中 7 条的两个标的，强度分必须能区分开。

    这正是 2026-09-14 修复的问题：原 `score` 会给出同一个 86.0。
    """
    weak = _strengths(price=10.005, momentum_20=0.005, rsi14=63.0, boll_position=0.60)
    strong = _strengths(price=10.06, momentum_20=0.09, rsi14=50.0, boll_position=0.05)
    weak_score = sum(TRIGGER_WEIGHTS[k] * v for k, v in weak.items())
    strong_score = sum(TRIGGER_WEIGHTS[k] * v for k, v in strong.items())
    assert strong_score > weak_score
    # 两者都命中了全部 8 条 → 原口径下都会是 100.0
    assert all(v > 0 for v in weak.values())
    assert all(v > 0 for v in strong.values())


def test_positive_strength_implies_condition_is_hit():
    """**不变式**：强度 > 0 ⇒ 该条件必然命中（不允许"没命中却拿强度分"）。

    用扫点方式验证，而不是逐条写死期望值。
    """
    for price in (9.5, 9.99, 10.0, 10.001, 10.05, 10.30, 10.80):
        if _strengths(price=price)["above_ma20"] > 0:
            assert price >= BASE["ma20"], price
    for ma20 in (9.5, 9.79, 9.80, 9.81, 10.0, 10.30):
        if _strengths(ma20=ma20)["ma20_above_ma60"] > 0:
            assert ma20 >= BASE["ma60"], ma20
    for ratio in (0.5, 1.19, 1.2, 1.21, 1.6, 2.0, 3.0):
        if _strengths(volume_ratio_20=ratio)["volume_expand"] > 0:
            assert ratio >= VOLUME_EXPAND_MIN, ratio
    for vol in (0.0, 0.1, 0.45, 0.46, 0.9):
        if _strengths(volatility_20=vol)["stable_volatility"] > 0:
            assert 0.0 <= vol <= STABLE_VOL_CEILING, vol


def test_boundary_hits_may_carry_zero_strength():
    """阈值边界上「命中但强度为 0」是**已知且允许**的口径。

    `_refine` 按 `price >= ma20` 判定命中并计入 `score`；而斜坡在 `price == ma20`
    处恰好为 0。二者不冲突 —— `strength_score` 只是对命中项再加权，不会凭空加分。
    这里把这个行为固化下来，避免以后有人"顺手修正"成 (0,1] 区间而改变排名口径。
    """
    assert _strengths(price=BASE["ma20"])["above_ma20"] == 0.0     # price == ma20
    assert _strengths(ma20=BASE["ma60"])["ma20_above_ma60"] == 0.0  # ma20 == ma60
    assert _strengths(rsi14=35.0)["rsi_rebound"] == 0.0
    assert _strengths(rsi14=65.0)["rsi_rebound"] == 0.0
    assert _strengths(boll_position=BOLL_PULLBACK_MAX_POS)["boll_pullback"] == 0.0
    assert _strengths(volatility_20=STABLE_VOL_CEILING)["stable_volatility"] == 0.0
    assert _strengths(volume_ratio_20=VOLUME_EXPAND_MIN)["volume_expand"] == 0.0


def test_strength_score_never_exceeds_hit_weight_sum():
    """强度 ≤ 1 ⇒ 强度分 ≤ 命中权重和（排名主键不会超过旧口径的分数）。"""
    strengths = _strengths(price=10.05, rsi14=60.0, boll_position=0.5, volume_ratio_20=1.3)
    hit_sum = sum(TRIGGER_WEIGHTS[k] for k, v in strengths.items() if v > 0)
    strength_sum = sum(TRIGGER_WEIGHTS[k] * v for k, v in strengths.items())
    assert strength_sum <= hit_sum
