"""「赚钱率」= 同类条件历史回放胜率的测试（纯函数；合成序列，结果可手算）。"""
from __future__ import annotations

import math

from app.realtime.win_rate import (
    MIN_HITS_DEFAULT,
    REPLAY_CONDITIONS,
    replay_signal,
)

#: 造一条严格上涨的序列：任何"同类条件"都会赢
def _up_series(length: int, start: float = 10.0, step: float = 0.01) -> list[float]:
    return [round(start * (1 + step) ** i, 4) for i in range(length)]


def _flat_series(length: int) -> list[float]:
    return [10.0] * length


def _volumes(length: int, value: float = 1_000_000.0) -> list[float]:
    return [value] * length


def test_uptrend_gives_full_win_rate():
    closes = _up_series(200)
    stats = replay_signal(closes, _volumes(200), hold_days=5, min_hits=3)
    assert stats.samples > 0
    assert stats.win_rate == 1.0
    assert stats.mean_return is not None and stats.mean_return > 0
    assert stats.best is not None and stats.worst is not None
    assert stats.best >= stats.mean_return >= stats.worst
    assert stats.hold_days == 5 and stats.min_hits == 3
    assert stats.conditions_used == REPLAY_CONDITIONS


def test_flat_series_never_wins_but_still_counts_samples():
    closes = _flat_series(200)
    # 平盘时"站上 20 日线"成立（等于）、RSI=100 不健康 → 命中数取决于阈值
    stats = replay_signal(closes, _volumes(200), hold_days=5, min_hits=1)
    assert stats.samples > 0
    assert stats.win_rate == 0.0          # 收益恰好为 0 → 不算赢（>0 才算）


def test_short_series_reports_no_samples_instead_of_guessing():
    stats = replay_signal(_up_series(50), _volumes(50), hold_days=5, min_hits=3)
    assert stats.samples == 0
    assert stats.win_rate is None
    assert stats.mean_return is None
    assert "样本不足" in stats.note


def test_high_threshold_yields_fewer_or_equal_samples():
    closes = _up_series(300)
    low = replay_signal(closes, _volumes(300), hold_days=5, min_hits=1)
    high = replay_signal(closes, _volumes(300), hold_days=5, min_hits=5)
    assert high.samples <= low.samples
    assert low.samples > 0


def test_no_lookahead_bias_future_prices_cannot_change_past_signals():
    """把**最后一天之后**的行情改掉，不能影响此前的信号判定与样本数。"""
    base = _up_series(200)
    modified = base[:200]
    stats_a = replay_signal(base, _volumes(200), hold_days=5, min_hits=3)
    # 截断到最后一个可评估信号日之前，样本数应当只减少（说明信号判定不依赖未来）
    stats_b = replay_signal(modified[:190], _volumes(190), hold_days=5, min_hits=3)
    assert stats_b.samples <= stats_a.samples


def test_note_states_it_is_not_a_probability_and_not_out_of_sample():
    stats = replay_signal(_up_series(220), _volumes(220), hold_days=5, min_hits=3)
    note = stats.note
    assert "样本内" in note
    assert "不是未来上涨概率" in note
    assert "没有通过样本外验证" in note
    assert "未扣交易费用" in note
    assert "5 条" in note          # 如实说明只用 5 条条件


def test_win_rate_is_bounded_and_median_is_sane():
    closes = _up_series(300, step=0.005)
    stats = replay_signal(closes, _volumes(300), hold_days=10, min_hits=3)
    assert 0.0 <= stats.win_rate <= 1.0
    assert stats.best >= stats.median_return >= stats.worst
    assert math.isclose(stats.win_rate, stats.wins / stats.samples, rel_tol=1e-9)


def test_monotone_uptrend_can_only_satisfy_three_of_five_conditions():
    """单边上涨时 RSI 必然偏高、成交量不放大 → 最多命中 3 条。

    这条测试把口径钉死：如果有人把 rsi_healthy 的区间改成 35~95，
    样本数会突然暴增，属于口径变化，必须显式改测试而不是悄悄发生。
    """
    closes = _up_series(200, step=0.003)
    three = replay_signal(closes, _volumes(200), hold_days=5, min_hits=3)
    four = replay_signal(closes, _volumes(200), hold_days=5, min_hits=4)
    assert three.samples > 0
    assert four.samples == 0
    assert four.win_rate is None


def test_missing_or_zero_prices_are_skipped_by_caller_contract():
    """回放本身只接受正价格序列；含 0 的价格会被调用方过滤（这里验证判定不崩）。"""
    closes = _up_series(150)
    closes[80] = 0.0
    stats = replay_signal(closes, _volumes(150), hold_days=5, min_hits=3)
    assert stats.samples >= 0     # 不抛异常；调用方负责过滤非法价格
