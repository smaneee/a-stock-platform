"""P0-01 情绪因子验证器 v2 —— 回归测试。

覆盖任务书要求的四类用例（外加口径守卫）：

A. 日期乱序输入 → 修正后结果与预先排好序的输入一致
B. 重复日期 → 明确报错（不得静默取一条）
C. 含并列值 → 与 scipy 一致；无 scipy 时与手算用例一致
D. 未来函数检测 → 「信号日晚于/等于收益起算日」必须被拒绝并告警

跑法::

    backend\\.venv-311\\Scripts\\python.exe -m pytest work\\ps_search\\tests -q
"""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from sent_factor_core import (
    DuplicateTradeDateError,
    InsufficientSampleError,
    LookAheadError,
    SentFactorError,
    SourceMixError,
    assert_no_lookahead,
    assert_single_source,
    average_rank,
    block_bootstrap_mean_ci,
    block_bootstrap_spearman_ci,
    cross_check_spearman,
    ensure_sorted_unique_dates,
    hac_mean_ci,
    lookahead_violations,
    make_splits,
    monotonic_dates,
    sensitivity_table,
    spearman_sensitivity,
    split_by_source,
    split_integrity_report,
)
from sent_factor_v2 import (
    build_forward_returns,
    daily_market_series,
)

CAL = [f"2026-01-{d:02d}" for d in range(1, 32)] + [f"2026-02-{d:02d}" for d in range(1, 29)]


def make_bars(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "trade_date", "open", "close"])


# ---------------------------------------------------------------------------
# A. 日期乱序 → 与排好序一致
# ---------------------------------------------------------------------------


def test_ensure_sorted_unique_dates_sorts_and_preserves_payload():
    rows = [
        {"trade_date": "2026-01-05", "source": "derived", "seal_rate": 0.5},
        {"trade_date": "2026-01-02", "source": "derived", "seal_rate": 0.4},
        {"trade_date": "2026-01-03", "source": "derived", "seal_rate": 0.45},
    ]
    ordered = ensure_sorted_unique_dates(rows)
    assert [r["trade_date"] for r in ordered] == ["2026-01-02", "2026-01-03", "2026-01-05"]
    assert monotonic_dates(ordered)
    assert ordered[0]["seal_rate"] == 0.4


def test_unsorted_market_series_equals_sorted_market_series():
    """乱序的逐股明细聚合结果必须与已排序输入完全一致。"""
    rows = []
    # 三天、三只股票，价格刻意不同，保证结果对顺序敏感
    prices = {
        "2026-02-02": (10.0, 10.2),
        "2026-02-03": (10.2, 10.6),
        "2026-02-04": (10.6, 10.1),
        "2026-02-05": (10.1, 10.9),
    }
    for symbol, bump in (("AAA", 1.0), ("BBB", 1.02), ("CCC", 0.99)):
        for day, (open_, close) in prices.items():
            rows.append((symbol, day, open_ * bump, close * bump))

    sorted_bars = make_bars(rows).sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    rng = random.Random(20260913)
    order = list(range(len(rows)))
    rng.shuffle(order)
    shuffled = make_bars([rows[i] for i in order])
    # 再整体轮转一次，确保「返回顺序」与交易日顺序完全脱钩
    shuffled = pd.concat([shuffled.iloc[5:], shuffled.iloc[:5]], ignore_index=True)

    a = build_forward_returns(sorted_bars, CAL, 1)
    b = build_forward_returns(shuffled, CAL, 1)

    sa = daily_market_series(a).sort_values("sig_date").reset_index(drop=True)
    sb = daily_market_series(b).sort_values("sig_date").reset_index(drop=True)

    pd.testing.assert_frame_equal(sa, sb)
    assert list(sa["sig_date"]) == sorted(sa["sig_date"])


def test_forward_returns_exact_alignment_t_plus_1_open_to_t_plus_k_close():
    """k=1: t+1 开盘买入、t+1 收盘卖出；k=2: t+1 开盘 → t+2 收盘。"""
    bars = make_bars(
        [
            ("AAA", "2026-01-01", 10.0, 10.5),
            ("AAA", "2026-01-02", 11.0, 11.5),
            ("AAA", "2026-01-03", 12.0, 12.6),
        ]
    )
    k1 = build_forward_returns(bars, CAL, 1).set_index("sig_date")
    assert k1.loc["2026-01-01", "entry_date"] == "2026-01-02"
    assert k1.loc["2026-01-01", "exit_date"] == "2026-01-02"
    assert k1.loc["2026-01-01", "ret"] == pytest.approx(11.5 / 11.0 - 1.0)

    k2 = build_forward_returns(bars, CAL, 2).set_index("sig_date")
    assert k2.loc["2026-01-01", "entry_date"] == "2026-01-02"
    assert k2.loc["2026-01-01", "exit_date"] == "2026-01-03"
    assert k2.loc["2026-01-01", "ret"] == pytest.approx(12.6 / 11.0 - 1.0)
    # 收盘起算口径单独一列，不与开盘口径混用
    assert k2.loc["2026-01-01", "ret_cc"] == pytest.approx(12.6 / 11.5 - 1.0)


def test_every_row_has_entry_strictly_after_signal():
    bars = make_bars(
        [
            ("AAA", day, 10.0 + i, 10.1 + i)
            for i, day in enumerate(CAL[:10])
        ]
    )
    for k in (1, 3, 5):
        out = build_forward_returns(bars, CAL, k)
        assert (out["entry_date"] > out["sig_date"]).all()
        assert (out["exit_date"] > out["sig_date"]).all()
        # 真实数据路径上的硬断言不应抛错
        assert_no_lookahead(
            out["sig_date"].tolist(), out["entry_date"].tolist(), exit_dates=out["exit_date"].tolist()
        )


# ---------------------------------------------------------------------------
# B. 重复日期 → 明确报错
# ---------------------------------------------------------------------------


def test_duplicate_trade_date_raises():
    rows = [
        {"trade_date": "2026-01-02", "source": "derived", "seal_rate": 0.5},
        {"trade_date": "2026-01-02", "source": "derived", "seal_rate": 0.7},
    ]
    with pytest.raises(DuplicateTradeDateError) as err:
        ensure_sorted_unique_dates(rows)
    assert "2026-01-02" in str(err.value)


def test_duplicate_detection_is_per_source():
    rows = [
        {"trade_date": "2026-01-02", "source": "derived", "seal_rate": 0.5},
        {"trade_date": "2026-01-02", "source": "eastmoney", "seal_rate": 0.6},
    ]
    ordered = ensure_sorted_unique_dates(rows)
    assert len(ordered) == 2
    with pytest.raises(DuplicateTradeDateError):
        ensure_sorted_unique_dates(
            rows + [{"trade_date": "2026-01-02", "source": "eastmoney", "seal_rate": 0.61}]
        )


def test_duplicate_can_be_tolerated_explicitly():
    rows = [
        {"trade_date": "2026-01-02", "source": "derived"},
        {"trade_date": "2026-01-02", "source": "derived"},
    ]
    assert len(ensure_sorted_unique_dates(rows, require_unique=False)) == 2


def test_duplicate_symbol_day_in_bars_is_deduplicated_not_double_counted():
    bars = make_bars(
        [
            ("AAA", "2026-01-01", 10.0, 10.5),
            ("AAA", "2026-01-02", 11.0, 11.5),
            ("AAA", "2026-01-02", 11.0, 11.5),  # 同一 (symbol, day) 重复
        ]
    )
    out = build_forward_returns(bars, CAL, 1)
    assert len(out) == 1
    day = daily_market_series(out)
    assert int(day.iloc[0]["n_symbols"]) == 1


# ---------------------------------------------------------------------------
# C. 并列值 → 平均秩；与 scipy 交叉核对
# ---------------------------------------------------------------------------


def test_average_rank_hand_computed_case():
    # 值 [10, 20, 20, 40] 的平均秩应为 [1, 2.5, 2.5, 4]
    assert list(average_rank([10, 20, 20, 40])) == [1.0, 2.5, 2.5, 4.0]
    # 全部并列 → 全部取中间秩
    assert list(average_rank([7, 7, 7])) == [2.0, 2.0, 2.0]
    # 无并列的普通情形
    assert list(average_rank([3, 1, 2])) == [3.0, 1.0, 2.0]


def test_average_rank_matches_scipy_rankdata():
    scipy_stats = pytest.importorskip("scipy.stats")
    values = [1.0, 2.0, 2.0, 2.0, 5.0, -3.0, 5.0]
    assert list(average_rank(values)) == list(scipy_stats.rankdata(values, method="average"))


def test_spearman_with_ties_matches_scipy_and_hand_value():
    scipy_stats = pytest.importorskip("scipy.stats")
    x = [0.1, 0.1, 0.4, 0.4, 0.9, 0.2, 0.2, 0.7]
    y = [1.0, 1.0, -0.5, 2.0, 0.3, 0.3, 1.2, -1.0]

    check = cross_check_spearman(x, y, tol=1e-9)
    assert check["scipy_available"] is True
    assert check["ok"] is True, check
    assert check["abs_diff"] <= 1e-9

    # 手算：平均秩上的皮尔逊相关
    rx = average_rank(x)
    ry = average_rank(y)
    hand = float(np.corrcoef(rx, ry)[0, 1])
    assert check["self_value"] == pytest.approx(hand, abs=1e-12)
    assert check["self_value"] == pytest.approx(
        float(scipy_stats.spearmanr(x, y).statistic), abs=1e-12
    )

    # 旧「两次 argsort」的实现会在并列值上给出不同结果，这里显式固化差异存在
    naive = float(
        np.corrcoef(np.argsort(np.argsort(x)), np.argsort(np.argsort(y)))[0, 1]
    )
    assert abs(naive - check["self_value"]) > 1e-6


def test_spearman_no_ties_matches_scipy():
    scipy_stats = pytest.importorskip("scipy.stats")
    x = [3.0, 1.0, 4.0, 1.5, 5.0]
    y = [2.0, 5.0, 1.0, 4.0, 3.0]
    check = cross_check_spearman(x, y)
    assert check["ok"] is True
    assert check["self_value"] == pytest.approx(
        float(scipy_stats.spearmanr(x, y).statistic), abs=1e-12
    )


def test_spearman_cross_check_reports_when_scipy_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("scipy"):
            raise ImportError("scipy disabled for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    check = cross_check_spearman([1, 2, 3, 4], [4, 3, 2, 1])
    assert check["scipy_available"] is False
    assert check["ok"] is None
    assert check["self_value"] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# D. 未来函数检测
# ---------------------------------------------------------------------------


def test_lookahead_same_day_entry_is_rejected():
    with pytest.raises(LookAheadError):
        assert_no_lookahead(["2026-01-05"], ["2026-01-05"])


def test_lookahead_earlier_entry_is_rejected_and_reported():
    violations = lookahead_violations(
        ["2026-01-05", "2026-01-06"], ["2026-01-02", "2026-01-07"]
    )
    assert violations == [("2026-01-05", "2026-01-02")]
    with pytest.raises(LookAheadError):
        assert_no_lookahead(["2026-01-05", "2026-01-06"], ["2026-01-02", "2026-01-07"])


def test_lookahead_exit_before_entry_is_rejected():
    with pytest.raises(LookAheadError):
        assert_no_lookahead(["2026-01-05"], ["2026-01-06"], exit_dates=["2026-01-05"])


def test_no_lookahead_passes_for_correct_alignment():
    assert_no_lookahead(
        ["2026-01-05", "2026-01-06"],
        ["2026-01-06", "2026-01-07"],
        exit_dates=["2026-01-08", "2026-01-09"],
    )


def test_length_mismatch_is_an_error():
    with pytest.raises(SentFactorError):
        assert_no_lookahead(["2026-01-05"], ["2026-01-06", "2026-01-07"])


# ---------------------------------------------------------------------------
# 来源分离
# ---------------------------------------------------------------------------


def test_split_by_source_never_mixes_sources():
    rows = [
        {"trade_date": "2026-01-05", "source": "derived", "seal_rate": 0.5},
        {"trade_date": "2026-01-06", "source": "eastmoney", "seal_rate": 0.6},
        {"trade_date": "2026-01-02", "source": "derived", "seal_rate": 0.4},
    ]
    buckets = split_by_source(rows)
    assert set(buckets) == {"derived", "eastmoney"}
    assert [r["trade_date"] for r in buckets["derived"]] == ["2026-01-02", "2026-01-05"]
    assert [r["trade_date"] for r in buckets["eastmoney"]] == ["2026-01-06"]


def test_assert_single_source_rejects_pooling():
    with pytest.raises(SourceMixError):
        assert_single_source(
            [
                {"trade_date": "2026-01-05", "source": "derived"},
                {"trade_date": "2026-01-06", "source": "eastmoney"},
            ]
        )
    assert assert_single_source([{"trade_date": "2026-01-05", "source": "derived"}]) == "derived"


# ---------------------------------------------------------------------------
# 重叠收益：HAC / 块自助法
# ---------------------------------------------------------------------------


def test_bartlett_hac_lags_zero_equals_iid_standard_error():
    rng = np.random.default_rng(7)
    x = rng.normal(0.001, 0.01, size=200)
    res = hac_mean_ci(x, lags=0)
    expected_se = float(np.std(x, ddof=0)) / np.sqrt(len(x))
    assert res["se"] == pytest.approx(expected_se, rel=1e-12)
    assert res["ci_low"] < res["mean"] < res["ci_high"]


def test_hac_ci_widens_with_positive_autocorrelation():
    rng = np.random.default_rng(11)
    shocks = rng.normal(0.0, 0.01, size=400)
    x = np.empty_like(shocks)
    x[0] = shocks[0]
    for i in range(1, len(shocks)):
        x[i] = 0.8 * x[i - 1] + shocks[i]
    iid = hac_mean_ci(x, lags=0)
    hac = hac_mean_ci(x, lags=10)
    assert hac["se"] > iid["se"]


def test_block_bootstrap_is_deterministic_and_wider_than_iid_for_dependent_series():
    rng = np.random.default_rng(3)
    shocks = rng.normal(0.0, 0.01, size=300)
    x = np.empty_like(shocks)
    x[0] = shocks[0]
    for i in range(1, len(shocks)):
        x[i] = 0.8 * x[i - 1] + shocks[i]

    a = block_bootstrap_mean_ci(x, block=10, n_boot=1500, seed=42)
    b = block_bootstrap_mean_ci(x, block=10, n_boot=1500, seed=42)
    assert a == b  # 固定种子 → 可复现
    iid = block_bootstrap_mean_ci(x, block=1, n_boot=1500, seed=42)
    assert (a["ci_high"] - a["ci_low"]) > (iid["ci_high"] - iid["ci_low"])
    assert a["ci_low"] < a["mean"] < a["ci_high"]


def test_sensitivity_table_registers_lags_and_blocks():
    rng = np.random.default_rng(5)
    x = rng.normal(0.0005, 0.01, size=180)
    table = sensitivity_table(x, lags=(0, 1, 5), blocks=(1, 5, 20), n_boot=300)
    assert [item["lags"] for item in table["hac"]] == [0, 1, 5]
    assert [item["block"] for item in table["bootstrap"]] == [1, 5, 20]
    assert isinstance(table["stable_sign"], bool)
    assert isinstance(table["significant_at_95"], bool)


def test_hac_requires_minimum_sample():
    with pytest.raises(InsufficientSampleError):
        hac_mean_ci([0.01, 0.02], lags=1)


def test_block_bootstrap_spearman_is_deterministic_and_contains_point():
    rng = np.random.default_rng(17)
    x = rng.normal(size=200)
    y = 0.3 * x + rng.normal(size=200)
    a = block_bootstrap_spearman_ci(x, y, block=5, n_boot=600, seed=7)
    b = block_bootstrap_spearman_ci(x, y, block=5, n_boot=600, seed=7)
    assert a == b
    assert a["ci_low"] <= a["point"] <= a["ci_high"]
    assert a["point"] == pytest.approx(float(np.corrcoef(average_rank(x), average_rank(y))[0, 1]))


def test_block_bootstrap_spearman_widens_under_overlapping_windows():
    """强序列依赖下，块自助区间必须比 i.i.d. 块（block=1）更宽。"""
    rng = np.random.default_rng(23)
    x = np.cumsum(rng.normal(size=220))
    y = np.cumsum(rng.normal(size=220))
    iid = block_bootstrap_spearman_ci(x, y, block=1, n_boot=800, seed=11)
    blocked = block_bootstrap_spearman_ci(x, y, block=10, n_boot=800, seed=11)
    assert (blocked["ci_high"] - blocked["ci_low"]) > (iid["ci_high"] - iid["ci_low"])
    assert blocked["ci_low"] <= blocked["point"] <= blocked["ci_high"]


def test_spearman_sensitivity_flags_instability():
    rng = np.random.default_rng(29)
    x = rng.normal(size=150)
    y = rng.normal(size=150)  # 独立 → 真相关为 0
    table = spearman_sensitivity(x, y, blocks=(1, 5, 10, 20), n_boot=300)
    assert table["robust_excludes_zero"] is False
    assert [r["block"] for r in table["intervals"]] == [1, 5, 10, 20]
    assert table["any_includes_zero"] is True
    assert table["usable_intervals"] == 4


def test_block_bootstrap_spearman_requires_minimum_sample():
    with pytest.raises(InsufficientSampleError):
        block_bootstrap_spearman_ci([1.0, 2.0], [1.0, 2.0], block=1)


def test_block_bootstrap_flags_degenerate_block_length():
    """块长 >= 样本长度时只能取到整段，区间退化为一个点，必须被标记。"""
    x = [1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0]
    y = [2.0, 1.0, 4.0, 3.0, 6.0, 5.0, 2.0, 1.0]
    degen = block_bootstrap_spearman_ci(x, y, block=len(x), n_boot=50, seed=1)
    assert degen["degenerate"] is True
    assert degen["excludes_zero"] is False
    assert degen["ci_low"] == degen["ci_high"]

    table = spearman_sensitivity(x, y, blocks=(1, 2, len(x)), n_boot=100)
    assert table["usable_intervals"] == 2

    only_degenerate = spearman_sensitivity(x, y, blocks=(len(x),), n_boot=50)
    assert only_degenerate["usable_intervals"] == 0
    assert only_degenerate["robust_excludes_zero"] is False


# ---------------------------------------------------------------------------
# 样本划分
# ---------------------------------------------------------------------------


def test_splits_are_disjoint_and_embargo_covers_label_end():
    dates = [f"2026-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    horizon = 5
    plan = make_splits(dates, horizon=horizon, train_frac=0.5, select_frac=0.25, min_holdout=30)
    label_end = {}
    for i, day in enumerate(dates):
        label_end[day] = dates[min(i + horizon, len(dates) - 1)]
    report = split_integrity_report(plan, label_end)
    assert report["all_ok"] is True, report
    assert plan.embargo.n == horizon
    assert set(plan.train.dates) & set(plan.select.dates) == set()
    assert set(plan.select.dates) & set(plan.holdout.dates) == set()


def test_splits_reject_insufficient_sample():
    dates = [f"2026-01-{d:02d}" for d in range(1, 29)]
    with pytest.raises(InsufficientSampleError):
        make_splits(dates, horizon=5, train_frac=0.5, select_frac=0.25, min_holdout=30)


def test_splits_reject_unsorted_dates():
    with pytest.raises(SentFactorError):
        make_splits(["2026-01-03", "2026-01-01", "2026-01-02"], horizon=1)


# ---------------------------------------------------------------------------
# 端到端：合成情绪 + 合成行情走完整条修正管线
# ---------------------------------------------------------------------------


def synthetic_panel(n_days: int = 260, n_symbols: int = 40, seed: int = 1):
    rng = np.random.default_rng(seed)
    cal = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)][:n_days]
    rows = []
    for s in range(n_symbols):
        price = 10.0 + s * 0.1
        for day in cal:
            open_ = price * (1 + rng.normal(0, 0.005))
            close = open_ * (1 + rng.normal(0, 0.01))
            rows.append((f"S{s:03d}", day, round(open_, 4), round(close, 4)))
            price = close
    sentiment = pd.DataFrame(
        {
            "trade_date": cal,
            "seal_rate": rng.uniform(0.3, 0.9, size=n_days),
            "broken_rate": rng.uniform(0.1, 0.7, size=n_days),
            "max_streak": rng.integers(1, 12, size=n_days),
            "limit_up_count": rng.integers(20, 180, size=n_days),
        }
    )
    return make_bars(rows), cal, sentiment


def test_end_to_end_pipeline_is_order_invariant_and_lookahead_free():
    bars, cal, sentiment = synthetic_panel()
    shuffled = bars.sample(frac=1.0, random_state=99).reset_index(drop=True)

    market_a = daily_market_series(build_forward_returns(bars, cal, 1))
    market_b = daily_market_series(build_forward_returns(shuffled, cal, 1))
    pd.testing.assert_frame_equal(
        market_a.sort_values("sig_date").reset_index(drop=True),
        market_b.sort_values("sig_date").reset_index(drop=True),
    )

    merged = sentiment.merge(market_a, left_on="trade_date", right_on="sig_date", how="inner")
    merged = merged.sort_values("trade_date").reset_index(drop=True)
    assert len(merged) >= 200

    plan = make_splits(merged["trade_date"].tolist(), horizon=1, min_holdout=40)
    check = cross_check_spearman(merged["seal_rate"].tolist(), merged["mean"].tolist())
    assert check["ok"] is True

    train = merged[merged["trade_date"].isin(plan.train.dates)]
    threshold = float(train["seal_rate"].median())
    merged["w"] = (merged["seal_rate"] >= threshold).astype(float)
    # 关键：信号日 t 只能用 t 收盘信息，建仓必须在 t+1
    assert_no_lookahead(
        merged["trade_date"].tolist(), merged["entry_date"].tolist(), exit_dates=merged["exit_date"].tolist()
    )
    spread = ((2 * merged["w"] - 1) * merged["mean"]).to_numpy()
    table = sensitivity_table(spread, lags=(0, 1, 5), blocks=(1, 5, 20), n_boot=400)
    assert table["n"] == len(spread)


def test_threshold_from_train_does_not_change_when_holdout_changes():
    """训练段冻结阈值的性质：改动留出段不能影响阈值。"""
    _, _, sentiment = synthetic_panel(seed=5)
    dates = sentiment["trade_date"].tolist()
    plan = make_splits(dates, horizon=1, min_holdout=40)
    train = sentiment[sentiment["trade_date"].isin(plan.train.dates)]
    thr1 = float(train["seal_rate"].median())

    tampered = sentiment.copy()
    holdout_mask = tampered["trade_date"].isin(plan.holdout.dates)
    tampered.loc[holdout_mask, "seal_rate"] = 0.999
    train2 = tampered[tampered["trade_date"].isin(plan.train.dates)]
    assert float(train2["seal_rate"].median()) == pytest.approx(thr1)
