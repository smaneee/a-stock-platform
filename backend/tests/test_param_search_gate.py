"""D10：`scripts/param_search.py` 的留出段预登记门禁。

原缺口（final-report §6.14「未接线」）：脚本每次运行都会打印留出段结果，
留出可以反复查看 —— 等于没有「只看一次」的纪律。本测试锁定修复后的行为：

1. 不提供 `--preregistration` 时，留出段既不打印也不落盘；
2. 提供预登记时，揭盲时间**立刻写回**文件（先落盘再跑）；
3. 同一份预登记第二次运行被拒绝，只跑训练段；
4. 找不到 / 哈希不符的预登记直接拒绝（返回码 2）。
"""
from __future__ import annotations

import importlib.util
import json
import pickle
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "param_search.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("param_search_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ps():
    return _load_script()


def _valid_prereg(experiment_id: str = "unit-test-gate") -> dict:
    return {
        "experiment_id": experiment_id,
        "hypothesis": "单元测试用的假设",
        "strategy_version": "test-v1",
        "data_cutoff": "2026-09-11",
        "universe_scope": "沪深300（测试）",
        "primary_horizon_days": 3,
        "primary_benchmark": "same_pool_equal_weight",
        "costs": {"commission_rate": 0.0003, "slippage": 0.0005},
        "risk_budget": {"max_position": 0.95},
        "decision_criteria": {"net_excess_ci_lower_gt_0": True},
        "planned_trials": 48,
        "multiple_comparison": "benjamini_hochberg",
    }


# ───────────── 纯函数：抹掉留出段 ─────────────


def test_redact_holdout_blanks_every_holdout_field(ps):
    report = {
        "results": [
            {"name": "a", "train": {"3": {"t": 2.0}}, "hold": {"3": {"t": 5.0}}},
            {"name": "b", "train": {"3": {"t": 1.0}}, "hold": {"3": {"t": 3.0}}},
        ],
        "hold_single_factor": {"momentum": {"3": {"t": 1.1}}},
        "count_buckets_hold": [{"n": 10, "excess_pct": 0.5}],
        "factor_diagnostics": [
            {"key": "momentum", "hit_rate_hold": 0.2, "hold": {"spread_pct": 0.4}}
        ],
        "top_bottom_spread": {"等权": {"train": {"t": 1.0}, "hold": {"t": 2.0}}},
        "best_on_train": {"name": "a", "hold": {"3": {"t": 5.0}}, "hold_all_horizons": "x"},
        "noise_band": {"hold_t_mean": 0.1, "hold_t_values": [1, 2], "train_t_mean": 0.2},
        "hold_span": ["2026-06-01", "2026-08-31"],
        "train_span": ["2026-01-01", "2026-05-31"],
    }
    out = ps.redact_holdout(report)

    assert out["holdout_blinded"] is True
    assert "封存" in out["holdout_blind_reason"]
    # 所有留出结果都被替换为占位符
    for row in out["results"]:
        assert row["hold"] == ps.BLINDED
    assert out["hold_single_factor"] == {"momentum": ps.BLINDED}
    assert out["count_buckets_hold"] == ps.BLINDED
    assert out["factor_diagnostics"][0]["hold"] == ps.BLINDED
    assert out["factor_diagnostics"][0]["hit_rate_hold"] == ps.BLINDED
    assert out["top_bottom_spread"]["等权"]["hold"] == ps.BLINDED
    assert out["best_on_train"]["hold"] == ps.BLINDED
    assert out["best_on_train"]["hold_all_horizons"] == ps.BLINDED
    assert out["noise_band"]["hold_t_mean"] == ps.BLINDED
    assert out["noise_band"]["hold_t_values"] == ps.BLINDED
    # 训练段与窗口元信息必须原样保留（否则训练也无法复核）
    assert out["results"][0]["train"] == {"3": {"t": 2.0}}
    assert out["train_span"] == ["2026-01-01", "2026-05-31"]
    assert out["hold_span"] == ["2026-06-01", "2026-08-31"]
    assert out["noise_band"]["train_t_mean"] == 0.2


def test_redact_holdout_is_idempotent(ps):
    report = {"results": [{"train": {}, "hold": {"3": 1}}], "count_buckets_hold": 1}
    once = ps.redact_holdout(json.loads(json.dumps(report)))
    twice = ps.redact_holdout(once)
    assert twice["results"][0]["hold"] == ps.BLINDED


# ───────────── 多重比较提示 ─────────────


def test_multi_comparison_uses_planned_trials_as_m(ps):
    from app.realtime.param_search import H_PRIMARY

    def row(t: float) -> dict:
        return {"train": {H_PRIMARY: types.SimpleNamespace(excess_t_stat=t)}}

    # 5 行结果，但预登记声明了 48 次试验 → m 必须取 48（不能只按实际行数）
    note = ps._multi_comparison_note([row(3.0), row(0.1), row(0.2), row(0.3), row(0.4)], 48)
    assert note["m"] == 48
    assert note["method"] == "benjamini_hochberg"
    assert note["observed_trials"] == 5
    assert note["nominal_significant"] == 1
    # 48 次试验下，名义 p≈0.0027 也过不了 BH 阈值 0.05/48≈0.00104
    assert note["rejected_count"] == 0
    assert "正态近似" in note["note"]


def test_multi_comparison_without_planned_trials_uses_row_count(ps):
    from app.realtime.param_search import H_PRIMARY

    rows = [
        {"train": {H_PRIMARY: types.SimpleNamespace(excess_t_stat=t)}}
        for t in (4.0, 0.5, 0.6)
    ]
    note = ps._multi_comparison_note(rows, None)
    assert note["m"] == 3
    assert note["rejected_count"] == 1


# ───────────── 预登记解析 ─────────────


def test_resolve_preregistration_finds_real_artifact(ps):
    path = ps._resolve_preregistration("radar-v2-future-holdout")
    assert path is not None and path.is_file()
    assert path.parent == ps.PREREG_DIR


def test_resolve_preregistration_returns_none_when_missing(ps, capsys):
    assert ps._resolve_preregistration("no-such-experiment-xyz") is None
    assert "拒绝揭盲" in capsys.readouterr().out


# ───────────── 端到端：揭盲只允许一次 ─────────────


@pytest.fixture
def stub_pipeline(ps, monkeypatch):
    """把 Book / run_search / build_cache 全部替身化，只考察门禁行为。"""
    calls: dict = {"run_search": 0}

    monkeypatch.setattr(ps, "build_cache", lambda *a, **k: {"stub": True})
    monkeypatch.setattr(ps, "Book", lambda payload: object())

    def fake_run_search(book, train_days, hold_days, log=print, **kwargs):
        calls["run_search"] += 1
        calls["allow_holdout"] = kwargs.get("allow_holdout")
        calls["planned_trials"] = kwargs.get("planned_trials")
        return {}

    monkeypatch.setattr(ps, "run_search", fake_run_search)
    return calls


def test_cli_gates_holdout_once(ps, tmp_path, monkeypatch, stub_pipeline, capsys):
    prereg_path = tmp_path / "gate-unit.json"
    from app.research.preregistration import Preregistration

    Preregistration.freeze(_valid_prereg()).save(prereg_path)
    out_path = tmp_path / "report.json"

    # 第一次：应揭盲，并把揭盲状态立刻写回文件
    monkeypatch.setattr(
        sys, "argv",
        ["param_search.py", "--reuse", "--cache", str(tmp_path / "c.pkl"),
         "--out", str(out_path), "--preregistration", str(prereg_path)],
    )
    (tmp_path / "c.pkl").write_bytes(pickle.dumps({"stub": True}))  # --reuse 且存在 → 跳过 build_cache
    assert ps.main() == 0
    first = capsys.readouterr().out
    assert "已揭盲" in first
    assert stub_pipeline["allow_holdout"] is True
    assert stub_pipeline["planned_trials"] == 48

    saved = json.loads(prereg_path.read_text(encoding="utf-8"))
    assert saved["unblind_count"] == 1
    assert saved["unblinded_at"]

    # 写出的报告必须带预登记信息与「已揭盲」标记
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["holdout_visible"] is True
    assert report["preregistration"]["experiment_id"] == "unit-test-gate"
    assert report["preregistration"]["frozen_hash"] == saved["frozen_hash"]
    assert report["preregistration"]["planned_trials"] == 48

    # 第二次：必须拒绝揭盲，且只跑训练段
    stub_pipeline["allow_holdout"] = None
    assert ps.main() == 0
    second = capsys.readouterr().out
    assert "拒绝揭盲" in second
    assert "只运行训练段" in second
    assert stub_pipeline["allow_holdout"] is False
    # 拒绝时不得覆盖文件里的首次揭盲记录
    assert json.loads(prereg_path.read_text(encoding="utf-8"))["unblinded_at"] == saved["unblinded_at"]


def test_cli_without_preregistration_blinds_holdout(ps, tmp_path, monkeypatch, stub_pipeline, capsys):
    (tmp_path / "c.pkl").write_bytes(pickle.dumps({"stub": True}))
    out_path = tmp_path / "blinded.json"
    monkeypatch.setattr(
        sys, "argv",
        ["param_search.py", "--reuse", "--cache", str(tmp_path / "c.pkl"),
         "--out", str(out_path)],
    )
    assert ps.main() == 0
    printed = capsys.readouterr().out
    assert "留出段封存" in printed
    assert stub_pipeline["allow_holdout"] is False
    assert stub_pipeline["planned_trials"] is None
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["holdout_visible"] is False
    assert report["preregistration"]["provided"] is False
    assert report["preregistration"].get("unblind_allowed") is not True


def test_cli_rejects_tampered_preregistration(ps, tmp_path, monkeypatch, capsys):
    prereg_path = tmp_path / "tampered.json"
    payload = _valid_prereg("tampered")
    from app.research.preregistration import Preregistration

    record = Preregistration.freeze(payload)
    data = record.to_dict()
    data["decision_criteria"] = {"net_excess_ci_lower_gt_neg1": True}  # 事后放宽判据
    prereg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv",
        ["param_search.py", "--preregistration", str(prereg_path)],
    )
    assert ps.main() == 2
    assert "拒绝揭盲" in capsys.readouterr().out


def test_cli_rejects_unknown_preregistration(ps, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv",
        ["param_search.py", "--preregistration", "definitely-not-registered"],
    )
    assert ps.main() == 2
    assert "找不到预登记" in capsys.readouterr().out
