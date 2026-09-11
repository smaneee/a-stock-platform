"""可观测性指标测试。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.observability.metrics import MetricsRegistry, data_source_status


@pytest.fixture
def registry():
    """每个测试用独立的注册表，避免全局状态泄漏。"""
    return MetricsRegistry()


def test_provider_success_and_failure(registry):
    registry.record_provider_success("tencent", 0.05)
    registry.record_provider_success("tencent", 0.03)
    registry.record_provider_failure("tencent")

    m = registry.provider_metrics()["tencent"]
    assert m["success"] == 2
    assert m["failure"] == 1
    assert m["success_rate"] == pytest.approx(2 / 3)
    assert m["avg_latency_ms"] == pytest.approx(40.0)
    assert m["consecutive_failures"] == 1


def test_consecutive_failures_reset_on_success(registry):
    registry.record_provider_failure("tencent")
    registry.record_provider_failure("tencent")
    assert registry.provider_metrics()["tencent"]["consecutive_failures"] == 2

    registry.record_provider_success("tencent", 0.01)
    assert registry.provider_metrics()["tencent"]["consecutive_failures"] == 0


def test_ws_metrics(registry):
    registry.record_ws_connect()
    registry.record_ws_connect()
    registry.record_ws_drop()
    registry.record_ws_disconnect()

    m = registry.ws_metrics()
    assert m["connections"] == 1
    assert m["peak_connections"] == 2
    assert m["dropped_messages"] == 1


def test_task_metrics(registry):
    registry.record_task_result("succeeded")
    registry.record_task_result("succeeded")
    registry.record_task_result("failed")
    m = registry.task_metrics()
    assert m["succeeded"] == 2
    assert m["failed"] == 1


def test_data_source_status_simulated():
    assert data_source_status({}, ["mock"]) == "simulated"


def test_data_source_status_disconnected_no_names():
    assert data_source_status({}, []) == "disconnected"


def test_data_source_status_disconnected_all_failing():
    m = {"tencent": {"consecutive_failures": 3, "last_success_epoch": None}}
    assert data_source_status(m, ["tencent"]) == "disconnected"


def test_data_source_status_no_records_yet_is_delayed_not_disconnected():
    """刚启动 / 自选股为空（轮询器没打过请求）时不能报"数据断开"。

    回归：此前 `not metrics_of_real` 分支直接返回 disconnected，导致新装环境
    页面顶栏常驻红色"数据断开"，而实际上真实数据源一个都没失败过。
    """
    assert data_source_status({}, ["tencent", "akshare"]) == "delayed"
    assert data_source_status({}, ["tencent", "akshare", "mock"]) == "delayed"


def test_data_source_status_delayed():
    now = time.time()
    m = {"tencent": {"consecutive_failures": 0, "last_success_epoch": now - 300}}
    assert data_source_status(m, ["tencent"]) == "delayed"


def test_data_source_status_real_time():
    now = time.time()
    m = {"tencent": {"consecutive_failures": 0, "last_success_epoch": now}}
    assert data_source_status(m, ["tencent"]) == "real-time"


def test_metrics_endpoint():
    with TestClient(app) as client:
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "data_status" in data
        assert "providers" in data
        assert "websocket" in data
        assert "tasks" in data
        # mock 数据源 -> simulated
        assert data["data_status"] == "simulated"
