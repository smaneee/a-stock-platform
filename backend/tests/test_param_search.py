"""参数寻优引擎的纯函数与缓存视图测试（不碰数据库，秒级完成）。

覆盖三件容易写错、且会直接改变结论的事：
1. 分数就是命中触发器的权重和（与 ``ScreenerService._refine`` 同一公式）；
2. 候选排序与生产 ``(-score, -amount_20, symbol)`` 一致，并正确按 ``min_triggers`` 过滤；
3. 缓存视图把 NaN（算不出收益）排除在基准均值之外，随机对照排序随盐值变化。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.realtime.param_search import (
    HORIZONS,
    KEYS,
    KEY_IX,
    Book,
    build_folds,
    noise_stats,
    rank_pool,
    score_of,
    slice_payload,
)
from app.realtime.screener import TRIGGER_WEIGHTS


def _onehot(triggers: tuple[str, ...]) -> np.ndarray:
    row = np.zeros(len(KEYS), dtype=np.float64)
    for key in triggers:
        row[KEY_IX[key]] = 1.0
    return row


def test_score_of_equals_weight_sum() -> None:
    assert score_of(("above_ma20", "macd_hist_turn"), TRIGGER_WEIGHTS) == 28.0
    assert score_of((), TRIGGER_WEIGHTS) == 0.0
    # 未知条件 / 缺省权重不能悄悄算成非零
    assert score_of(("not_a_trigger",), TRIGGER_WEIGHTS) == 0.0
    assert score_of(("above_ma20", "boll_pullback"), {}) == 0.0


def test_rank_pool_matches_production_ordering() -> None:
    triggers = [
        ("above_ma20",),
        ("above_ma20", "macd_hist_turn"),
        ("macd_hist_turn",),
        (),
    ]
    onehot = np.vstack([_onehot(t) for t in triggers])
    counts = onehot.sum(axis=1)
    amounts = np.array([100.0, 50.0, 200.0, 999.0])
    symbols = np.array([10, 11, 12, 13], dtype=np.int32)

    # 不限命中数：分数降序，同分按成交额降序，空命中排最后
    assert list(rank_pool(onehot, counts, amounts, symbols, TRIGGER_WEIGHTS, 0, 4)) == [1, 2, 0, 3]
    # 只取前 2 名
    assert list(rank_pool(onehot, counts, amounts, symbols, TRIGGER_WEIGHTS, 1, 2)) == [1, 2]
    # 至少命中 2 个条件：只剩第 2 只
    assert list(rank_pool(onehot, counts, amounts, symbols, TRIGGER_WEIGHTS, 2, 4)) == [1]
    # 条件过严时返回空，而不是抛异常
    assert list(rank_pool(onehot, counts, amounts, symbols, TRIGGER_WEIGHTS, 3, 4)) == []


def test_rank_pool_respects_top_n_and_custom_weights() -> None:
    onehot = np.vstack([_onehot(("above_ma20",)), _onehot(("volume_expand",))])
    counts = onehot.sum(axis=1)
    amounts = np.array([10.0, 20.0])
    symbols = np.array([1, 2], dtype=np.int32)
    # 把 volume_expand 调高后它应当排到第一，说明权重确实生效
    weights = {k: 0.0 for k in KEYS}
    weights["above_ma20"] = 1.0
    weights["volume_expand"] = 5.0
    assert list(rank_pool(onehot, counts, amounts, symbols, weights, 1, 1)) == [1]


def _payload() -> dict:
    return {
        "symbols": ["600000", "000001"],
        "adjust": "qfq",
        "universe": 2,
        "snapshot_day": "2026-09-11",
        "days": [
            {
                "date": "2026-01-05",
                "sym": np.array([0, 1], dtype=np.int32),
                "val": np.array([[1.0, 2.0, np.nan, 4.0], [5.0, 6.0, 7.0, 8.0]], dtype=np.float32),
                "pool_trig": [("above_ma20",), None],
                "pool_amt": np.array([100.0, 200.0], dtype=np.float32),
                "bench_sum": [6.0, 8.0, 7.0, 12.0],
                "bench_cnt": [2, 2, 1, 2],
            },
            {
                "date": "2026-01-06",
                "sym": np.array([0, 1], dtype=np.int32),
                "val": np.array([[2.0, 3.0, 4.0, 5.0], [np.nan, np.nan, np.nan, np.nan]], dtype=np.float32),
                "pool_trig": [("macd_hist_turn", "volume_expand"), ("above_ma20",)],
                "pool_amt": np.array([50.0, 60.0], dtype=np.float32),
                "bench_sum": [2.0, 3.0, 4.0, 5.0],
                "bench_cnt": [1, 1, 1, 1],
            },
        ],
    }


def _many_symbols_payload(n: int) -> dict:
    """n 只标的、只有一天的极简缓存，用于验证随机对照排序。"""
    return {
        "symbols": [f"60000{i}" for i in range(n)],
        "adjust": "qfq",
        "universe": n,
        "snapshot_day": "2026-09-11",
        "days": [
            {
                "date": "2026-01-05",
                "sym": np.arange(n, dtype=np.int32),
                "val": np.ones((n, len(HORIZONS)), dtype=np.float32),
                "pool_trig": [("above_ma20",)] * n,
                "pool_amt": np.arange(n, dtype=np.float32),
                "bench_sum": [float(n)] * len(HORIZONS),
                "bench_cnt": [n] * len(HORIZONS),
            }
        ],
    }


def test_book_builds_onehot_and_bench_without_nan() -> None:
    book = Book(_payload())
    assert len(book) == 2
    assert book.dates == ["2026-01-05", "2026-01-06"]

    # 第 1 天：命中个数 [1, 0]（_refine 淘汰的标的计数为 0）
    assert list(book.counts[0]) == [1.0, 0.0]
    assert list(book.counts[1]) == [2.0, 1.0]
    # 收益里的 NaN 必须被剔除，否则基准均值会被污染
    pooled = book.pooled_bench_for([0, 1])
    assert len(pooled[2]) == 2  # 5 日：第 1 天 1 个 + 第 2 天 1 个
    assert pooled[2][0] == pytest.approx(7.0)
    assert [round(v, 4) for v in book.bench_mean[0]] == [3.0, 4.0, 7.0, 6.0]
    assert list(HORIZONS) == [1, 3, 5, 10]


def test_book_resalt_changes_random_order_deterministically() -> None:
    # 只有 2 只时不同盐值可能给出同一顺序，扩到 8 只再验证
    book = Book(_many_symbols_payload(8))
    first = [np.asarray(order).tolist() for order in book.rand_order]
    book.resalt(0)
    assert [np.asarray(order).tolist() for order in book.rand_order] == first
    book.resalt(7)
    assert any(
        np.asarray(order).tolist() != first[i] for i, order in enumerate(book.rand_order)
    )


# ────────────── walk-forward 切折 / 切片 / 噪声带 ──────────────


def test_build_folds_rolls_backwards_and_overlaps_train() -> None:
    # 整段 400 天：每折 160 天（100+10+50），步长 50 → 最多 5 折，留出段逐折前移
    folds = build_folds(400, 100, 10, 50, 50, 5)
    assert folds == [(240, 399), (190, 349), (140, 299), (90, 249), (40, 199)]
    for lo, hi in folds:
        assert hi - lo + 1 == 160  # 折长固定 = train + gap + hold
        assert 0 <= lo <= hi < 400
    # 相邻折：留出段严格不重叠且按 step 前移，训练段则必须互相重叠
    for (lo_a, hi_a), (lo_b, hi_b) in zip(folds, folds[1:]):
        assert hi_b < hi_a - 50 + 1  # 后一折的留出段结束得早于前一折留出段起点
        assert hi_b == hi_a - 50  # 留出段按 step 紧邻前移
        assert lo_b < lo_a  # 训练段整体向前滑动


def test_build_folds_stops_when_window_does_not_fit() -> None:
    # 整段刚好装下一折 → 只出 1 折；装不下 → 空列表（不能返回越界下标）
    assert build_folds(100, 60, 10, 30, 30, 3) == [(0, 99)]
    assert build_folds(99, 60, 10, 30, 30, 3) == []
    assert build_folds(0, 60, 10, 30, 30, 3) == []


def test_build_folds_rejects_invalid_params() -> None:
    for kwargs in (
        {"train": 0}, {"hold": 0}, {"step": 0}, {"max_folds": 0}, {"gap": -1}
    ):
        args = {"train": 60, "gap": 10, "hold": 30, "step": 30, "max_folds": 1, **kwargs}
        with pytest.raises(ValueError):
            build_folds(500, **args)


def test_slice_payload_keeps_metadata_and_rejects_empty() -> None:
    payload = {**_payload(), "days": [{"date": f"2026-01-{d:02d}"} for d in range(1, 6)]}
    sliced = slice_payload(payload, 1, 3)
    assert [rec["date"] for rec in sliced["days"]] == [
        "2026-01-02", "2026-01-03", "2026-01-04"
    ]
    assert sliced["symbols"] == payload["symbols"]
    assert sliced["adjust"] == payload["adjust"]
    assert payload["days"][0]["date"] == "2026-01-01"  # 原缓存不被就地修改
    with pytest.raises(ValueError):
        slice_payload(payload, 5, 6)


def test_noise_stats_distinguishes_no_sample_from_zero() -> None:
    # 没有随机对照样本时必须给 None，不能给 0（0 会被误读成「有对照且不显著」）
    assert noise_stats([]) == {"mean": None, "sd": None, "min": None, "max": None, "n": 0}
    stats = noise_stats([-2.0, 0.0, 2.0])
    assert stats["n"] == 3
    assert stats["mean"] == pytest.approx(0.0)
    assert stats["min"] == pytest.approx(-2.0)
    assert stats["max"] == pytest.approx(2.0)
    assert stats["sd"] == pytest.approx(1.63, abs=0.01)


def test_research_window_override_keeps_production_cap() -> None:
    from app.realtime.param_search import RESEARCH_MAX_EVAL_DAYS
    from app.realtime.validation import MAX_EVAL_DAYS, ValidationConfig

    assert MAX_EVAL_DAYS == 240
    assert RESEARCH_MAX_EVAL_DAYS > MAX_EVAL_DAYS
    # 默认口径（生产 / API）仍然拒绝超过一年的窗口
    with pytest.raises(ValueError):
        ValidationConfig(eval_days=MAX_EVAL_DAYS + 1).validate()
    # 研究工具显式放宽后可以通过，并且 end_day 会被带进配置
    cfg = ValidationConfig(eval_days=1330, end_day=date(2025, 6, 30))
    cfg.validate(max_eval_days=RESEARCH_MAX_EVAL_DAYS)
    assert cfg.end_day == date(2025, 6, 30)
