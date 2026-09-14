"""策略证据契约测试（P1-01）。

核心目的不是「接口能返回 200」，而是**负结果不能被悄悄隐藏**：

* 证据文件必须包含全部必需条目，且每条都要有可审计的 ``evidence_source``；
* 已知的负结果/不确定结论必须保持其状态，任何「美化」都会让本测试失败；
* 实盘闸门必须保持关闭（买点雷达与情绪因子都未通过样本外门槛）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import evidence as evidence_module
from app.api.evidence import (
    REQUIRED_ITEM_IDS,
    REQUIRED_NEGATIVE_IDS,
    VALID_STATUSES,
    load_evidence,
    summary_of,
)


def test_evidence_file_loads_and_is_complete():
    payload = load_evidence()
    ids = {item["id"] for item in payload["items"]}
    for required in REQUIRED_ITEM_IDS:
        assert required in ids, f"缺少必需证据条目 {required}"


def test_every_item_has_auditable_source_and_valid_status():
    payload = load_evidence()
    for item in payload["items"]:
        assert item["status"] in VALID_STATUSES, item["id"]
        assert item["evidence_source"], item["id"]
        assert item["limitations"], f"{item['id']} 必须写明限制"
        assert item["ui_rule"], f"{item['id']} 必须写明展示约束"


def test_negative_results_cannot_be_hidden():
    """反向不变量：把负结果改成「通过」就会让本用例失败。"""
    payload = load_evidence()
    by_id = {item["id"]: item for item in payload["items"]}

    radar = by_id["buy-point-radar"]
    assert radar["status"] == "failed_oos"
    assert radar["production_ready"] is False
    assert radar["result"]["mean_excess_pct"] < 0
    assert radar["result"]["folds_positive"] < radar["result"]["folds_total"]
    # 本机无法复现这一条必须如实标注
    assert radar["reproducible_on_this_machine"] is False

    sentiment = by_id["sentiment-factor"]
    assert sentiment["status"] == "inconclusive"
    assert sentiment["production_ready"] is False
    assert sentiment["result"]["block_bootstrap_ci_crosses_zero"] is True


def test_live_trading_gate_stays_closed():
    payload = load_evidence()
    gate = payload["production_gate"]
    assert gate["live_trading_enabled"] is False
    assert gate["requirements_for_small_live_pilot"]


def test_summary_counts_negative_items():
    payload = load_evidence()
    summary = summary_of(payload)
    assert summary["total"] == len(payload["items"])
    assert summary["production_ready_count"] == 0
    for item_id in REQUIRED_NEGATIVE_IDS:
        assert item_id in summary["negative_or_uncertain"]


def test_load_evidence_rejects_missing_items(tmp_path):
    source = json.loads(evidence_module.EVIDENCE_PATH.read_text(encoding="utf-8"))
    source["items"] = [item for item in source["items"] if item["id"] != "buy-point-radar"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必需条目"):
        load_evidence(broken)


def test_load_evidence_rejects_invalid_status(tmp_path):
    source = json.loads(evidence_module.EVIDENCE_PATH.read_text(encoding="utf-8"))
    source["items"][0]["status"] = "passed"  # 不在词表里，等价于自造「通过」
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="状态非法"):
        load_evidence(broken)


def test_load_evidence_rejects_missing_source(tmp_path):
    source = json.loads(evidence_module.EVIDENCE_PATH.read_text(encoding="utf-8"))
    source["items"][0]["evidence_source"] = ""
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="证据来源"):
        load_evidence(broken)


def test_api_returns_items_and_summary():
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/evidence")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"]["total"] == len(body["items"])
        assert body["production_gate"]["live_trading_enabled"] is False
        assert body["status_vocabulary"]["failed_oos"]
        live = body["live"].get("buy_point_radar_validation")
        # 活体状态允许失败，但字段必须存在，避免前端误以为「已验证通过」
        assert live is not None


def test_api_summary_endpoint():
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/evidence/summary")
    assert resp.status_code == 200
    assert resp.json()["summary"]["production_ready_count"] == 0


def test_api_returns_503_when_artifact_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_module, "EVIDENCE_PATH", tmp_path / "nope.json")
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/evidence")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "evidence_unavailable"


def test_evidence_file_is_versioned_in_repo():
    """证据文件必须在仓库里（docs/evidence），而不是运行时生成的内存对象。"""
    path = evidence_module.EVIDENCE_PATH
    assert path.exists()
    assert path.parts[-2:] == ("evidence", "strategy-evidence.json")
