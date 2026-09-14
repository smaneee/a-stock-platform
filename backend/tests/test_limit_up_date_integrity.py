"""P0-03 / EM-02 回归测试：涨停情绪池的日期真实性。

背景（实测缺陷，由东方财富功能矩阵验收发现）：

* 旧实现把**请求参数**里的 ``trade_date`` 直接回显到响应体，于是请求
  ``trade_date=2099-01-01`` 会拿到 2026-09-11 的最新快照却标记成 2099-01-01
  （40/40 条与 2026-09-11 完全相同）——这是典型的未来函数风险；
* ``POST /api/market/limit-up/capture`` 若接受未来日期，会把未来交易日写进
  情绪曲线，直接污染回测的市场温度因子。

修复后的口径：

1. 响应体的 ``trade_date`` **以上游实际返回的 ``qdate`` 为准**，请求日期与上游
   日期不一致时记 WARNING 并采用上游日期；
2. 请求未来日期一律 **422**，且不发起上游请求、不写库。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.market_data import eastmoney_limit_up as module
from app.market_data.eastmoney_limit_up import EastmoneyLimitUpService


def _fake_response(qdate: int):
    async def _request_json(client, host, path, params, **kwargs):
        return {"data": {"qdate": qdate, "tc": 1, "pool": []}}

    return _request_json


@pytest.mark.asyncio
async def test_response_date_follows_upstream_not_request(monkeypatch):
    """请求 2099 年也必须回上游真实日期。"""
    monkeypatch.setattr(module, "eastmoney_request_json", _fake_response(20260911))
    service = EastmoneyLimitUpService()
    try:
        result = await service.query("limit-up", trade_date=date(2099, 1, 1))
    finally:
        await service._client.aclose()
    assert result.trade_date == "2026-09-11"


@pytest.mark.asyncio
async def test_response_date_falls_back_to_request_when_upstream_missing(monkeypatch):
    """上游没给 qdate 时才退回请求日期（此时不构成冒名顶替）。"""
    monkeypatch.setattr(module, "eastmoney_request_json", _fake_response(0))
    service = EastmoneyLimitUpService()
    try:
        result = await service.query("limit-up", trade_date=date(2026, 9, 11))
    finally:
        await service._client.aclose()
    assert result.trade_date == "2026-09-11"


def test_limit_up_pool_rejects_future_trade_date():
    from fastapi.testclient import TestClient

    from app.main import app

    future = date(2099, 1, 1).isoformat()
    with TestClient(app) as client:
        resp = client.get(f"/api/market/limit-up/limit-up?trade_date={future}")
    assert resp.status_code == 422
    assert "未来日期" in resp.json()["detail"] or "晚于今天" in resp.json()["detail"]


def test_limit_up_capture_rejects_future_trade_date():
    """落库接口绝不能写未来交易日。"""
    from fastapi.testclient import TestClient

    from app.main import app

    future = date(2099, 1, 1).isoformat()
    with TestClient(app) as client:
        resp = client.post(
            "/api/market/limit-up/capture",
            json={"trade_date": future, "backfill_days": 0},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "拒绝抓取" in detail or "晚于今天" in detail


def test_unknown_pool_still_422():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/market/limit-up/not-a-pool")
    assert resp.status_code == 422
