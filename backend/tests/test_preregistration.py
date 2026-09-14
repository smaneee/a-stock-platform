"""实验预登记与多重比较校正测试（P0-05 的 D10）。

锁定三条纪律：

1. 预登记字段齐全才能冻结；缺字段直接报错（不允许「先跑再说」）；
2. 冻结内容有哈希，事后改判据会被检出；
3. 留出段**只允许揭盲一次**，第二次返回明确拒绝并给出首次时间；
4. 多重比较必须按真实试验次数校正（BH 手算对照 + Bonferroni）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.research.preregistration import (
    Preregistration,
    adjust_pvalues,
    benjamini_hochberg,
    bonferroni,
    canonical_hash,
    summarize_correction,
    validate_preregistration,
)


def _payload(**overrides):
    base = {
        "experiment_id": "radar-v1-holdout",
        "hypothesis": "买点雷达规则在样本外具有正净超额",
        "strategy_version": "radar-2026.09",
        "data_cutoff": "2026-09-11",
        "universe_scope": "沪深京全A（快照 2026-09-11）",
        "primary_horizon_days": 3,
        "primary_benchmark": "equal_weight",
        "costs": {"commission": 0.0003, "stamp_tax": 0.0005, "slippage_bps": 5},
        "risk_budget": {"max_weight_pct": 20.0, "max_total_position_pct": 80.0},
        "decision_criteria": {"net_excess_ci_lower_gt": 0.0, "max_drawdown_lt": 0.15},
        "planned_trials": 48,
        "multiple_comparison": "benjamini_hochberg",
    }
    base.update(overrides)
    return base


# ───────────── 预登记校验 ─────────────


def test_validate_accepts_complete_payload():
    validate_preregistration(_payload())


@pytest.mark.parametrize(
    "missing", ["experiment_id", "hypothesis", "data_cutoff", "decision_criteria", "planned_trials"]
)
def test_validate_rejects_missing_required_fields(missing):
    payload = _payload()
    payload[missing] = None
    with pytest.raises(ValueError, match="缺少必需字段"):
        validate_preregistration(payload)


def test_validate_rejects_bad_multiple_comparison_method():
    with pytest.raises(ValueError, match="multiple_comparison 非法"):
        validate_preregistration(_payload(multiple_comparison="fdr-ish"))


def test_validate_rejects_single_trial_with_correction():
    with pytest.raises(ValueError, match="planned_trials >= 2"):
        validate_preregistration(_payload(planned_trials=1))


def test_validate_rejects_bad_data_cutoff():
    with pytest.raises(ValueError, match="data_cutoff"):
        validate_preregistration(_payload(data_cutoff="2026-13-45"))


def test_validate_requires_excess_criteria():
    with pytest.raises(ValueError, match="超额"):
        validate_preregistration(_payload(decision_criteria={"sharpe_gt": 1.0}))


# ───────────── 冻结与防篡改 ─────────────


def test_freeze_records_hash_and_detects_tampering():
    record = Preregistration.freeze(_payload())
    assert record.integrity_ok() is True
    assert len(record.frozen_hash) == 64

    # 事后把判据改松（例如把「下界 > 0」改成「> -1」）必须被检出
    record.payload["decision_criteria"] = {"net_excess_ci_lower_gt": -1.0}
    assert record.integrity_ok() is False


def test_canonical_hash_ignores_volatile_fields():
    payload = _payload()
    base = canonical_hash(payload)
    payload["unblinded_at"] = "2026-09-14T10:00:00"
    payload["unblind_count"] = 1
    payload["notes_history"] = [{"at": "x", "note": "y"}]
    assert canonical_hash(payload) == base


# ───────────── 一次性揭盲 ─────────────


def test_unblind_allowed_once_then_refused():
    record = Preregistration.freeze(_payload())
    first = record.unblind(now=datetime(2026, 9, 14, 10, 0, 0), note="首次揭盲")
    assert first["allowed"] is True
    assert first["unblinded_at"].startswith("2026-09-14T10:00:00")
    assert first["integrity_ok"] is True

    second = record.unblind(now=datetime(2026, 9, 14, 11, 0, 0))
    assert second["allowed"] is False
    assert "只允许揭盲一次" in second["reason"]
    assert second["first_unblinded_at"] == first["unblinded_at"]
    assert record.unblind_count == 1


def test_save_and_load_roundtrip(tmp_path):
    record = Preregistration.freeze(_payload())
    path = record.save(tmp_path / "p.json")
    loaded = Preregistration.load(path)
    assert loaded.frozen_hash == record.frozen_hash
    assert loaded.integrity_ok() is True
    assert loaded.unblinded_at is None


def test_load_rejects_tampered_file(tmp_path):
    import json

    record = Preregistration.freeze(_payload())
    path = record.save(tmp_path / "p.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["planned_trials"] = 1  # 事后缩小试验次数（等于放宽多重比较）
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希不一致"):
        Preregistration.load(path)


# ───────────── 多重比较校正 ─────────────


def test_benjamini_hochberg_hand_computed_classic_example():
    """经典 15 假设例子：alpha=0.05 时只有最小的 p=0.001 通过。"""
    pvalues = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216,
               0.222, 0.251, 0.269, 0.275, 0.34]
    result = benjamini_hochberg(pvalues, alpha=0.05)
    assert result["m"] == 15
    assert result["rejected_indices"] == [0]
    assert result["critical_p"] == pytest.approx(0.001)


def test_benjamini_hochberg_rejects_more_with_larger_alpha():
    pvalues = [0.001, 0.008, 0.039, 0.041, 0.042]
    tight = benjamini_hochberg(pvalues, alpha=0.01)
    loose = benjamini_hochberg(pvalues, alpha=0.2)
    assert loose["rejected_count"] >= tight["rejected_count"]


def test_qvalues_are_monotone_and_bounded():
    pvalues = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074]
    result = benjamini_hochberg(pvalues, alpha=0.05)
    q = result["qvalues"]
    assert len(q) == len(pvalues)
    assert all(0.0 <= value <= 1.0 for value in q)
    # q 值随原始 p 值单调不减
    ordered = sorted(zip(pvalues, q))
    assert all(ordered[i][1] <= ordered[i + 1][1] + 1e-12 for i in range(len(ordered) - 1))


def test_bonferroni_is_more_conservative_than_bh():
    pvalues = [0.001, 0.01, 0.02, 0.03, 0.04]
    bh = benjamini_hochberg(pvalues, alpha=0.05)
    bf = bonferroni(pvalues, alpha=0.05)
    assert bf["threshold"] == pytest.approx(0.05 / 5)
    assert bf["rejected_count"] <= bh["rejected_count"]


def test_adjust_pvalues_none_flags_missing_correction():
    result = adjust_pvalues([0.01, 0.04, 0.5], method="none")
    assert result["method"] == "none"
    assert "未做多重比较校正" in result["warning"]
    assert summarize_correction(result) == result["warning"]


def test_adjust_pvalues_dispatch_and_errors():
    assert adjust_pvalues([0.01], "benjamini_hochberg")["method"] == "benjamini_hochberg"
    assert adjust_pvalues([0.01], "bonferroni")["method"] == "bonferroni"
    with pytest.raises(ValueError, match="未知的多重比较方法"):
        adjust_pvalues([0.01], "magic")


@pytest.mark.parametrize("bad", [-0.01, 1.5])
def test_adjust_pvalues_validates_range(bad):
    with pytest.raises(ValueError, match="p 值必须在"):
        benjamini_hochberg([bad], alpha=0.05)


def test_adjust_pvalues_rejects_bad_alpha():
    with pytest.raises(ValueError, match="alpha"):
        benjamini_hochberg([0.01], alpha=0.0)


def test_empty_pvalues_are_handled():
    assert benjamini_hochberg([], 0.05)["m"] == 0
    assert bonferroni([], 0.05)["m"] == 0
    assert adjust_pvalues([], "none")["rejected_count"] == 0


def test_summarize_correction_mentions_trial_count():
    result = benjamini_hochberg([0.001, 0.5, 0.6], alpha=0.05)
    text = summarize_correction(result)
    assert "m=3" in text
    assert "1/3" in text


# ───────────── API 契约 ─────────────


def test_api_adjust_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/research/adjust",
            json={"pvalues": [0.001, 0.008, 0.039], "method": "benjamini_hochberg", "alpha": 0.05},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["m"] == 3
    assert "Benjamini-Hochberg" in body["summary"]


def test_api_unblind_is_one_shot(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import research as research_api
    from app.main import app

    monkeypatch.setattr(research_api, "PREREGISTRATION_DIR", tmp_path)
    record = Preregistration.freeze(_payload(experiment_id="api-holdout"))
    record.save(tmp_path / "api-holdout.json")

    with TestClient(app) as client:
        first = client.post("/api/research/preregistrations/api-holdout/unblind")
        assert first.status_code == 200, first.text
        assert first.json()["allowed"] is True

        second = client.post("/api/research/preregistrations/api-holdout/unblind")
        assert second.status_code == 409
        assert second.json()["detail"]["error"] == "already_unblinded"

        listing = client.get("/api/research/preregistrations").json()
        assert listing["count"] == 1
        assert listing["items"][0]["unblind_count"] == 1


def test_api_rejects_path_traversal_ids(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import research as research_api
    from app.main import app

    monkeypatch.setattr(research_api, "PREREGISTRATION_DIR", tmp_path)
    with TestClient(app) as client:
        # Starlette 会先把 ../ 归一化，这里验证特殊字符 id 被拒绝
        resp = client.get("/api/research/preregistrations/bad%20id")
    assert resp.status_code == 422
