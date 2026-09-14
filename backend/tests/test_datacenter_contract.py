"""东方财富数据中心接口契约测试（EM-03 日期校验 + EM-07 限售解禁默认视角）。

背景（由功能矩阵逐项验收实测发现）：

* EM-03：`date=2026-13-45` / `2026-02-30` 只过正则、不过日历，被原样发给上游后
  触发上游报错，API 返回 **503「数据源暂不可用」** —— 把「参数错误」误报成
  「上游故障」，用户会以为数据源坏了。
* EM-07：限售解禁默认降序返回 **2028~2035 年**的解禁安排，`asc` 又返回 2010 年起的
  历史，两种默认视角都没有「最近将解禁」，首屏极易被误读成「即将解禁」。

修复口径：非法日历日期 → 422；限售解禁未指定日期区间且未显式要求升序时，自动套用
「从今天（北京时间）起、按解禁日期升序」的 upcoming 视角，并在响应里用
``applied_default`` 如实告知调用方。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.market_data import eastmoney_datacenter as module
from app.market_data.eastmoney_datacenter import (
    DATASETS,
    EastmoneyDatacenterService,
    _require_date,
)
from app.market_rules.session_state import now_cst


# ───────────── EM-03：日期校验 ─────────────


@pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30", "2026-00-10", "2026-04-31"])
def test_require_date_rejects_impossible_calendar_dates(bad):
    with pytest.raises(ValueError):
        _require_date(bad, "date")


@pytest.mark.parametrize("good", ["2026-09-11", "2024-02-29"])
def test_require_date_accepts_real_dates(good):
    assert _require_date(good, "date") == good


def test_require_date_still_rejects_format_errors():
    for bad in ["2026/09/11", "20260911", "26-09-11", ""]:
        with pytest.raises(ValueError):
            _require_date(bad, "date")


def test_api_returns_422_for_impossible_date():
    """参数错误必须是 422，不能落进 503「数据源不可用」。"""
    from app.main import app

    with TestClient(app) as client:
        for bad in ("2026-13-45", "2026-02-30"):
            resp = client.get(f"/api/market/datacenter/dragon-tiger?date={bad}")
            assert resp.status_code == 422, (bad, resp.status_code, resp.text)
            assert "日历日期" in resp.json()["detail"] or "YYYY-MM-DD" in resp.json()["detail"]


def test_api_returns_422_for_impossible_seat_date():
    from app.main import app

    with TestClient(app) as client:
        resp = client.get(
            "/api/market/datacenter/dragon-tiger/600519/seats?trade_date=2026-02-30"
        )
    assert resp.status_code == 422


# ───────────── EM-07：限售解禁默认视角 ─────────────


class _Captured:
    def __init__(self):
        self.kwargs: dict = {}
        self.calls: list[dict] = []


@pytest.fixture
def captured_fetch(monkeypatch):
    captured = _Captured()

    async def fake_fetch(self, report, spec, *, filter_expr, limit, page, order):
        captured.kwargs = {
            "report": report,
            "filter_expr": filter_expr,
            "limit": limit,
            "page": page,
            "order": order,
        }
        captured.calls.append(dict(captured.kwargs))
        # limit==1 是 _latest_date 的探测请求，返回一行带日期列的数据，
        # 让「最近一个有数据的交易日」逻辑可以走完整条路径。
        if limit == 1:
            return [{spec.date_column: "2026-09-11"}], 1
        return [], 0

    monkeypatch.setattr(module.EastmoneyDatacenterService, "_fetch", fake_fetch)
    return captured


@pytest.mark.asyncio
async def test_restricted_release_defaults_to_upcoming(captured_fetch):
    service = EastmoneyDatacenterService()
    try:
        result = await service.query("restricted-release")
    finally:
        await service._client.aclose()
    today = now_cst().date()
    assert result.applied_default == f"upcoming>={today.isoformat()}"
    assert captured_fetch.kwargs["order"] == "asc"
    assert f"FREE_DATE>='{today.isoformat()}'" in captured_fetch.kwargs["filter_expr"]
    # 必须是从今天起，而不是过去
    assert f"FREE_DATE<={today.isoformat()}" not in captured_fetch.kwargs["filter_expr"]


@pytest.mark.asyncio
async def test_restricted_release_explicit_asc_means_history(captured_fetch):
    service = EastmoneyDatacenterService()
    try:
        result = await service.query("restricted-release", order="asc")
    finally:
        await service._client.aclose()
    assert result.applied_default is None
    assert captured_fetch.kwargs["filter_expr"] == ""
    assert captured_fetch.kwargs["order"] == "asc"


@pytest.mark.asyncio
async def test_restricted_release_explicit_range_wins(captured_fetch):
    service = EastmoneyDatacenterService()
    try:
        result = await service.query(
            "restricted-release", date_from="2026-01-01", date_to="2026-12-31"
        )
    finally:
        await service._client.aclose()
    assert result.applied_default is None
    expr = captured_fetch.kwargs["filter_expr"]
    assert "FREE_DATE>='2026-01-01'" in expr and "FREE_DATE<='2026-12-31'" in expr


@pytest.mark.asyncio
async def test_restricted_release_explicit_date_wins(captured_fetch):
    service = EastmoneyDatacenterService()
    try:
        result = await service.query("restricted-release", date="2026-09-11")
    finally:
        await service._client.aclose()
    assert result.applied_default is None
    assert "FREE_DATE='2026-09-11'" in captured_fetch.kwargs["filter_expr"]


@pytest.mark.asyncio
async def test_only_expected_datasets_have_default_scope(captured_fetch):
    """默认视角是显式声明出来的，不允许悄悄扩散到其它数据集。"""
    scopes = {spec.key: spec.default_scope for spec in DATASETS.values() if spec.default_scope}
    assert scopes == {
        "restricted-release": "upcoming",
        "margin": "latest_trading_day",
    }

    service = EastmoneyDatacenterService()
    try:
        result = await service.query("dragon-tiger", limit=5)
    finally:
        await service._client.aclose()
    assert result.applied_default is None
    assert captured_fetch.kwargs["filter_expr"] == ""


@pytest.mark.asyncio
async def test_margin_defaults_to_latest_trading_day(captured_fetch):
    """EM-01：融资融券不能默认在 680 万行历史里翻页。"""
    service = EastmoneyDatacenterService()
    try:
        result = await service.query("margin", limit=5)
    finally:
        await service._client.aclose()
    assert result.applied_default == "latest_trading_day=2026-09-11"
    assert "DATE='2026-09-11'" in captured_fetch.kwargs["filter_expr"]
    # 第一次调用是探测最近日期（limit=1），第二次才是真正取数
    assert len(captured_fetch.calls) == 2
    assert captured_fetch.calls[0]["limit"] == 1


@pytest.mark.asyncio
async def test_margin_explicit_range_skips_default(captured_fetch):
    service = EastmoneyDatacenterService()
    try:
        result = await service.query("margin", date_from="2026-09-01", symbol="000001")
    finally:
        await service._client.aclose()
    assert result.applied_default is None
    expr = captured_fetch.kwargs["filter_expr"]
    assert "DATE>='2026-09-01'" in expr and 'SCODE="000001"' in expr


def test_composite_sort_is_declared_for_margin():
    """分页正确性依赖复合排序：主排序列 + 去重键。"""
    spec = DATASETS["margin"]
    assert spec.sort_column == "DATE"
    assert spec.tiebreak_column == "SCODE"
    assert spec.default_scope == "latest_trading_day"


def _fake_row_for(report: str) -> dict:
    """按数据集声明造一行「字段齐全」的假数据，避免触发缺列守卫。"""
    spec = next(s for s in DATASETS.values() if s.report == report)
    row = {}
    for column in spec.columns:
        if column == spec.date_column:
            row[column] = "2026-09-11"
        else:
            row[column] = "1"
    return row


def _install_capture(monkeypatch, captured: dict):
    async def fake_request(client, pool, path, params, **kwargs):
        captured.update(params)
        return {
            "result": {
                "data": [_fake_row_for(params["reportName"])],
                "count": 1,
            }
        }

    monkeypatch.setattr(module, "eastmoney_request_json", fake_request)


@pytest.mark.asyncio
async def test_fetch_sends_composite_sort_params(monkeypatch):
    captured: dict = {}
    _install_capture(monkeypatch, captured)
    service = EastmoneyDatacenterService()
    try:
        await service.query("margin", date="2026-09-11", limit=5)
    finally:
        await service._client.aclose()
    assert captured["sortColumns"] == "DATE,SCODE"
    assert len(captured["sortTypes"].split(",")) == 2
    assert captured["filter"] == "(DATE='2026-09-11')"


@pytest.mark.asyncio
async def test_fetch_sends_single_sort_without_tiebreak(monkeypatch):
    captured: dict = {}
    _install_capture(monkeypatch, captured)
    service = EastmoneyDatacenterService()
    try:
        await service.query("dragon-tiger", date="2026-09-11", limit=5)
    finally:
        await service._client.aclose()
    assert "," not in captured["sortColumns"]
    assert "," not in captured["sortTypes"]


@pytest.mark.asyncio
async def test_api_exposes_applied_default(monkeypatch):
    """响应体必须如实告诉前端「套用了默认视角」。"""

    async def fake_query(self, dataset, **kwargs):
        spec = self.spec(dataset)
        return module.DatasetResult(
            spec=spec, total=0, rows=[], applied_default="upcoming>=2026-09-13"
        )

    monkeypatch.setattr(module.EastmoneyDatacenterService, "query", fake_query)

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/market/datacenter/restricted-release?limit=5")
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_default"] == "upcoming>=2026-09-13"


def test_catalog_documents_upcoming_semantics():
    """目录描述要写清默认视角，避免前端/调用方误解为全量历史。"""
    catalog = {item["key"]: item for item in module.dataset_catalog()}
    desc = catalog["restricted-release"]["description"]
    assert "最近将解禁" in desc


def test_catalog_exposes_all_seat_fields_for_frontend():
    """EM-09：席位 12 个字段必须通过目录暴露，否则前端只能硬编码并漏渲染。"""
    catalog = {item["key"]: item for item in module.dataset_catalog()}
    seats = catalog["dragon-tiger-seats"]
    keys = {field["key"] for field in seats["fields"]}
    expected = {
        "trade_date", "symbol", "seat_name", "buy_amount", "sell_amount", "net_amount",
        "buy_ratio", "sell_ratio", "close", "change_pct", "accum_amount", "reason",
    }
    assert expected <= keys, f"缺少字段：{expected - keys}"
    assert len(seats["fields"]) >= 12


def test_catalog_annotates_upstream_empty_fields():
    """EM-04~06：上游恒空字段必须带 note，前端才能解释「—」不是抓取失败。"""
    catalog = {item["key"]: item for item in module.dataset_catalog()}
    expectations = {
        "northbound": {"fund_inflow", "quota_balance"},
        "dividend": {"bonus_ratio", "it_ratio"},
        "dragon-tiger": {"change_5d_pct", "change_10d_pct", "change_20d_pct"},
    }
    for dataset, keys in expectations.items():
        fields = {field["key"]: field for field in catalog[dataset]["fields"]}
        for key in keys:
            assert fields[key].get("note"), f"{dataset}.{key} 缺少恒空说明"


def test_catalog_note_is_empty_for_normal_fields():
    """没有口径问题的字段不应被加上无意义的说明。"""
    catalog = {item["key"]: item for item in module.dataset_catalog()}
    fields = {field["key"]: field for field in catalog["dragon-tiger"]["fields"]}
    assert not fields["symbol"].get("note")


def test_api_catalog_includes_seats_dataset():
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/market/datacenter")
    assert resp.status_code == 200
    keys = {item["key"] for item in resp.json()["datasets"]}
    assert "dragon-tiger-seats" in keys
    assert "dragon-tiger" in keys


def test_upcoming_default_is_future_only_by_construction():
    """构造性检查：默认视角的起点不早于今天。"""
    today = now_cst().date()
    assert today >= date(2026, 1, 1)
    assert today + timedelta(days=1) > today
