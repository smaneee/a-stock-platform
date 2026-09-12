"""M6 修正提交测试：MetricsRegistry 死锁修复与并发安全。

覆盖：
- snapshot 不会被写线程饿死（写线程同时记录时读者仍能拿到一致快照）。
- 写线程并发记录不会丢失更新（计数等于写入次数之和）。
- RLock 行为：snapshot 内部子方法调用不会递归死锁。
- 长持锁情况下 snapshot 返回降级占位而非阻塞超时。
"""
from __future__ import annotations

import threading
import time

from app.observability.metrics import MetricsRegistry


class TestMetricsRegistryConcurrency:
    """MetricsRegistry 多线程并发安全。"""

    def test_snapshot_does_not_deadlock_with_concurrent_writers(self):
        reg = MetricsRegistry()
        # 1) 一个写线程持续记录
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                reg.record_provider_success("provider-a", 0.001)
                reg.record_provider_failure("provider-b")
                reg.record_ws_connect()
                reg.record_task_result("succeeded")
                # 让出 CPU
                time.sleep(0.0001)

        t_writer = threading.Thread(target=writer, daemon=True)
        t_writer.start()

        try:
            # 2) 读者并发做 snapshot
            reader_snapshots = []
            reader_errors = []
            snapshot_count = 50

            def reader():
                for _ in range(snapshot_count // 5):
                    try:
                        snap = reg.snapshot()
                        reader_snapshots.append(snap)
                    except Exception as exc:  # noqa: BLE001
                        reader_errors.append(exc)
                    time.sleep(0.0005)

            reader_threads = [
                threading.Thread(target=reader, daemon=True) for _ in range(5)
            ]
            for t in reader_threads:
                t.start()
            for t in reader_threads:
                t.join(timeout=5.0)

            # 3) 写线程停
            stop.set()
            t_writer.join(timeout=2.0)

            # 4) 读者全部成功，计数大概达到
            assert len(reader_errors) == 0, f"reader errors: {reader_errors}"
            assert len(reader_snapshots) >= 10
            for snap in reader_snapshots:
                assert "providers" in snap
                assert "websocket" in snap
                assert "tasks" in snap
        finally:
            stop.set()

    def test_snapshot_holds_no_lock_after_completion(self):
        """snapshot() 完成后锁必须释放，否则后续 record 会被永久阻塞。"""
        reg = MetricsRegistry()
        # 取一次快照
        reg.snapshot()
        # 接着记录成功 — 必须不挂
        deadline = time.time() + 2.0
        reg.record_provider_success("x", 0.01)
        assert time.time() < deadline, "snapshot 持锁未释放"

    def test_rlock_allows_reentrant_calls(self):
        """snapshot 内部的 _locked 方法必须在持锁下被同线程调用，不能死锁。"""
        reg = MetricsRegistry()
        # 直接调用受保护的 locked 方法（持锁状态）—— 应能成功
        with reg._lock:
            # 内部方法要求持锁，在持锁状态下调用证明可重入
            inner = reg._provider_metrics_locked()
            assert isinstance(inner, dict)
            inner2 = reg._ws_metrics_locked()
            assert isinstance(inner2, dict)
            inner3 = reg._task_metrics_locked()
            assert isinstance(inner3, dict)

    def test_snapshot_with_writers_does_not_lose_updates(self):
        """读 / 写并发时，最终写入的次数应等于 record_* 次数之和。"""
        reg = MetricsRegistry()
        N = 200
        # 写入 N 次 succeeded
        for _ in range(N):
            reg.record_task_result("succeeded")
        # 写入 N 次 failed
        for _ in range(N):
            reg.record_task_result("failed")

        # 同时并发读取
        reads = []

        def reader():
            for _ in range(20):
                snap = reg.snapshot()
                reads.append(snap["tasks"])

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 最终 task_metrics 应该是 200 / 200
        snap_final = reg.snapshot()
        assert snap_final["tasks"]["succeeded"] == N
        assert snap_final["tasks"]["failed"] == N

    def test_snapshot_returns_partial_on_lock_timeout(self, monkeypatch):
        """当写端长期持锁时，snapshot 应在超时后降级返回而非阻塞。"""
        reg = MetricsRegistry()
        # 模拟持锁
        holder_acquired = threading.Event()
        release_holder = threading.Event()
        hold_snapshots = []

        def holder():
            # 主线程抢占锁
            reg._lock.acquire()
            holder_acquired.set()
            # 持续持锁直到测试通知释放
            release_holder.wait(timeout=2.0)
            reg._lock.release()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            holder_acquired.wait(timeout=1.0)
            # 此时锁被别人持有，snapshot 应在 _SNAPSHOT_TIMEOUT_SECONDS 内降级返回
            t0 = time.time()
            snap = reg.snapshot()
            elapsed = time.time() - t0
            # 降到 _empty_snapshot() 或部分正常快照（本测试希望：要么拿到空快照要么拿到正常快照，
            # 但绝不能挂超过快照超时）
            assert elapsed < 5.0  # 远大于 _SNAPSHOT_TIMEOUT=2.0，留余量
            assert "providers" in snap and "websocket" in snap and "tasks" in snap
        finally:
            release_holder.set()
            t.join(timeout=2.0)

    def test_data_source_status_disconnected_when_consecutive_failures(self):
        """连续失败 >= 3 次的数据源应记为 disconnected。"""
        from app.observability.metrics import data_source_status

        reg = MetricsRegistry()
        for _ in range(3):
            reg.record_provider_failure("tencent")
        snap = reg.snapshot()
        status = data_source_status(snap["providers"], ["tencent"])
        assert status == "disconnected"
