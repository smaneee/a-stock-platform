"""指标采集与数据源状态判定。

跟踪数据源成功率/延迟/连续失败/最后成功时间、WebSocket 连接数与丢包数、
回测任务累计结果计数，并据此判定整体数据源状态
（real-time / delayed / disconnected / simulated）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class _ProviderMetrics:
    success: int = 0
    failure: int = 0
    total_latency: float = 0.0
    last_latency: float = 0.0
    consecutive_failures: int = 0
    last_success_at: float | None = None  # epoch 秒
    last_failure_at: float | None = None


class MetricsRegistry:
    """进程内指标注册表（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, _ProviderMetrics] = {}
        self._ws_connections = 0
        self._ws_peak_connections = 0
        self._ws_dropped = 0
        self._task_results: dict[str, int] = {
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        }

    # ──────── 数据源 ────────

    def record_provider_success(self, name: str, latency_s: float) -> None:
        with self._lock:
            p = self._providers.setdefault(name, _ProviderMetrics())
            p.success += 1
            p.total_latency += latency_s
            p.last_latency = latency_s
            p.consecutive_failures = 0
            p.last_success_at = time.time()

    def record_provider_failure(self, name: str) -> None:
        with self._lock:
            p = self._providers.setdefault(name, _ProviderMetrics())
            p.failure += 1
            p.consecutive_failures += 1
            p.last_failure_at = time.time()

    def provider_metrics(self) -> dict[str, dict]:
        with self._lock:
            result = {}
            for name, p in self._providers.items():
                total = p.success + p.failure
                result[name] = {
                    "success": p.success,
                    "failure": p.failure,
                    "success_rate": (p.success / total) if total else 0.0,
                    "avg_latency_ms": round((p.total_latency / p.success) * 1000, 2) if p.success else 0.0,
                    "last_latency_ms": round(p.last_latency * 1000, 2),
                    "consecutive_failures": p.consecutive_failures,
                    "last_success_at": (
                        datetime.fromtimestamp(p.last_success_at, tz=UTC).isoformat()
                        if p.last_success_at
                        else None
                    ),
                    "last_success_epoch": p.last_success_at,
                }
            return result

    # ──────── WebSocket ────────

    def record_ws_connect(self) -> None:
        with self._lock:
            self._ws_connections += 1
            self._ws_peak_connections = max(self._ws_peak_connections, self._ws_connections)

    def record_ws_disconnect(self) -> None:
        with self._lock:
            self._ws_connections = max(0, self._ws_connections - 1)

    def record_ws_drop(self) -> None:
        with self._lock:
            self._ws_dropped += 1

    def ws_metrics(self) -> dict:
        with self._lock:
            return {
                "connections": self._ws_connections,
                "peak_connections": self._ws_peak_connections,
                "dropped_messages": self._ws_dropped,
            }

    # ──────── 回测任务 ────────

    def record_task_result(self, status: str) -> None:
        with self._lock:
            if status in self._task_results:
                self._task_results[status] += 1

    def task_metrics(self) -> dict:
        with self._lock:
            return dict(self._task_results)

    # ──────── 快照 ────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "providers": self.provider_metrics(),
                "websocket": self.ws_metrics(),
                "tasks": self.task_metrics(),
            }


# 全局单例
metrics = MetricsRegistry()


def data_source_status(provider_metrics: dict[str, dict], provider_names: list[str]) -> str:
    """判定整体数据源状态。

    - simulated：唯一可用数据源是 mock
    - disconnected：所有数据源连续失败 >= 3 次
    - delayed：最近成功距今超过 120 秒
    - real-time：其余情况
    """
    if not provider_names:
        return "disconnected"
    if provider_names == ["mock"]:
        return "simulated"

    real_names = [n for n in provider_names if n != "mock"]
    metrics_of_real = [provider_metrics.get(n) for n in real_names if provider_metrics.get(n)]

    # 无任何成功记录 -> 视作未连接（除非刚启动）
    if not metrics_of_real:
        return "disconnected"

    # 所有真实源都在连续失败 -> disconnected
    if all(m["consecutive_failures"] >= 3 for m in metrics_of_real):
        return "disconnected"

    # 最近成功时间超过阈值 -> delayed
    now = time.time()
    latest_success = max(
        (m.get("last_success_epoch") or 0 for m in metrics_of_real),
        default=0,
    )
    if latest_success and now - latest_success > 120:
        return "delayed"

    return "real-time"
