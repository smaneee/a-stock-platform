"""组合回测后台 worker 测试：恢复、认领、执行与完整性校验。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from app.database.models import PortfolioBacktest
from app.history.quality import QualityReport
from app.history.service import HistoryResult
from app.market_data.base import QuoteData
from app.tasks.portfolio_worker import PortfolioBacktestWorker
from app.tasks.status import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED

from tests.helpers import make_quote

START = datetime(2026, 1, 5)
END = datetime(2026, 3, 5)  # 与 60 根连续日线的首尾对齐


def _factory(session):
    return sessionmaker(bind=session.bind, autoflush=False, autocommit=False)


def _bars(symbol: str = "600000", n: int = 60) -> list[QuoteData]:
    bars = []
    for i in range(n):
        day = date(2026, 1, 5) + timedelta(days=i)
        price = 10.0 + i * 0.05
        bars.append(
            make_quote(
                symbol=symbol,
                price=price,
                open=price - 0.02,
                high=price + 0.1,
                low=price - 0.1,
                previous_close=price - 0.05,
                market_time=datetime(day.year, day.month, day.day, 9, 30),
            )
        )
    return bars


def _result(
    symbol: str = "600000",
    *,
    bars=None,
    complete: bool = True,
    missing=None,
) -> HistoryResult:
    bars = _bars(symbol) if bars is None else bars
    return HistoryResult(
        bars=bars,
        source="cache",
        data_updated_at=None,
        is_complete=complete,
        quality=QualityReport(total=len(bars), missing_dates=list(missing or [])),
    )


def _make_task(db, status=RUNNING, **kwargs) -> PortfolioBacktest:
    task = PortfolioBacktest(
        symbols=kwargs.get("symbols", json.dumps(["600000"])),
        weights=kwargs.get("weights"),
        benchmark_symbol=kwargs.get("benchmark_symbol"),
        strategy_name=kwargs.get("strategy_name", "ma_cross"),
        start_time=kwargs.get("start_time", START),
        end_time=kwargs.get("end_time", END),
        initial_cash=kwargs.get("initial_cash", 100000),
        config_json=kwargs.get("config_json"),
        status=status,
        idempotency_key=kwargs.get("idempotency_key"),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _patch_history(monkeypatch, mapping) -> None:
    async def fake_get_history(self, symbol, start, end, **kwargs):
        return mapping[symbol]

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )


def _worker(db) -> PortfolioBacktestWorker:
    return PortfolioBacktestWorker(session_factory=_factory(db))


def test_recover_stale_tasks(db_session):
    task = _make_task(db_session, status=RUNNING)
    assert _worker(db_session)._recover_stale_tasks() == 1
    db_session.refresh(task)
    assert task.status == QUEUED
    assert task.started_at is None


def test_recover_ignores_non_running(db_session):
    _make_task(db_session, status=QUEUED)
    assert _worker(db_session)._recover_stale_tasks() == 0


def test_claim_next_takes_oldest(db_session):
    first = _make_task(db_session, status=QUEUED)
    _make_task(db_session, status=QUEUED)
    assert _worker(db_session)._claim_next() == first.id
    db_session.refresh(first)
    assert first.status == RUNNING
    assert first.started_at is not None


def test_claim_next_returns_none_when_empty(db_session):
    assert _worker(db_session)._claim_next() is None


def test_mark_failed_skips_cancelled(db_session):
    task = _make_task(db_session, status=CANCELLED)
    _worker(db_session)._mark_failed(task.id, "boom")
    db_session.refresh(task)
    assert task.status == CANCELLED
    assert task.error_message is None


def test_mark_failed_truncates_error(db_session):
    task = _make_task(db_session, status=RUNNING)
    _worker(db_session)._mark_failed(task.id, "x" * 600)
    db_session.refresh(task)
    assert task.status == FAILED
    assert len(task.error_message) == 500


async def test_execute_returns_early_when_not_running(db_session):
    task = _make_task(db_session, status=QUEUED)
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == QUEUED
    assert task.result is None


async def test_execute_success(db_session, monkeypatch):
    task = _make_task(db_session, status=RUNNING)
    _patch_history(monkeypatch, {"600000": _result()})
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED
    assert task.progress == 100
    assert task.finished_at is not None
    payload = json.loads(task.result)
    assert payload["requested_symbols"] == ["600000"]
    assert payload["executed_symbols"] == ["600000"]
    assert payload["excluded_symbols"] == []


async def test_execute_incomplete_marks_failed(db_session, monkeypatch):
    task = _make_task(db_session, status=RUNNING)
    _patch_history(monkeypatch, {"600000": _result(bars=[], complete=False)})
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == FAILED
    assert "完整性校验失败" in (task.error_message or "")
    payload = json.loads(task.result)
    assert payload["excluded_symbols"] == ["600000"]
    assert payload["exclusion_reasons"]["600000"]["reason"] == "empty_bars"


async def test_execute_allow_partial_keeps_complete_symbols(db_session, monkeypatch):
    task = _make_task(
        db_session,
        status=RUNNING,
        symbols=json.dumps(["600000", "000001"]),
        config_json=json.dumps({"allow_partial": True}),
    )
    _patch_history(
        monkeypatch,
        {"600000": _result(), "000001": _result(bars=[], complete=False)},
    )
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED
    payload = json.loads(task.result)
    assert payload["executed_symbols"] == ["600000"]
    assert payload["excluded_symbols"] == ["000001"]


async def test_execute_with_benchmark(db_session, monkeypatch):
    task = _make_task(db_session, status=RUNNING, benchmark_symbol="000300")
    _patch_history(monkeypatch, {"600000": _result(), "000300": _result("000300")})
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == SUCCEEDED
    payload = json.loads(task.result)
    assert payload["benchmark_curve"]  # 基准曲线已生成
    assert payload["benchmark_return"] is not None


async def test_execute_with_incomplete_benchmark_marks_failed(db_session, monkeypatch):
    task = _make_task(db_session, status=RUNNING, benchmark_symbol="000300")
    _patch_history(
        monkeypatch,
        {"600000": _result(), "000300": _result("000300", bars=[], complete=False)},
    )
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == FAILED
    assert "benchmark_incomplete" in (task.error_message or "")


async def test_execute_unknown_strategy_marks_failed(db_session, monkeypatch):
    task = _make_task(db_session, status=RUNNING, strategy_name="no_such_strategy")
    _patch_history(monkeypatch, {"600000": _result()})
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == FAILED
    assert "策略不存在" in (task.error_message or "")


def test_check_history_completeness_branches():
    check = PortfolioBacktestWorker._check_history_completeness
    assert check("600000", None, START, END)["reason"] == "no_result"
    assert check("600000", _result(bars=[], complete=False), START, END)["reason"] == "empty_bars"
    assert check("600000", _result(complete=False), START, END)["reason"] == "incomplete"
    no_dates = _result(bars=[QuoteData(symbol="600000", price=10.0, market_time=None)])
    assert check("600000", no_dates, START, END)["reason"] == "no_dates"
    assert check("600000", _result(), START, END) is None
    assert (
        check("600000", _result(), START + timedelta(days=10), END)["reason"]
        == "range_mismatch"
    )
    assert (
        check("600000", _result(), START, END + timedelta(days=120))["reason"]
        == "range_too_short"
    )
    assert (
        check("600000", _result(missing=[date(2026, 1, 10)]), START, END)["reason"]
        == "missing_dates"
    )


async def test_start_stop_lifecycle(db_session):
    worker = _worker(db_session)
    assert worker.is_running is False
    await worker.start()
    assert worker.is_running is True
    await worker.start()  # 重复启动幂等
    assert worker.is_running is True
    await worker.stop()
    assert worker.is_running is False


async def test_process_next_claims_and_executes(db_session, monkeypatch):
    task = _make_task(db_session, status=QUEUED)
    worker = _worker(db_session)
    executed: list[int] = []

    async def fake_execute(task_id):
        executed.append(task_id)

    monkeypatch.setattr(worker, "_execute", fake_execute)
    assert await worker._process_next() is True
    assert executed == [task.id]
    assert await worker._process_next() is False


async def test_poll_loop_survives_errors(db_session, monkeypatch):
    from app.tasks import portfolio_worker as mod

    monkeypatch.setattr(mod, "_POLL_INTERVAL_SECONDS", 0.01)
    worker = _worker(db_session)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        worker._running = False
        return False

    monkeypatch.setattr(worker, "_process_next", flaky)
    worker._running = True
    await worker._poll_loop()
    assert calls["n"] == 2  # 异常后继续轮询，不退出


def test_module_level_recover_returns_zero_on_clean_db():
    from app.tasks.portfolio_worker import recover_stale_portfolio_tasks

    assert recover_stale_portfolio_tasks() == 0


async def test_execute_stops_when_cancelled_during_fetch(db_session, monkeypatch):
    """历史拉取期间任务被取消：不再继续，也不写回结果。"""
    task = _make_task(db_session, status=RUNNING)

    async def cancel_then_return(self, symbol, start, end, **kwargs):
        row = self._db.get(PortfolioBacktest, task.id)
        row.status = CANCELLED
        self._db.commit()
        return _result()

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", cancel_then_return
    )
    await _worker(db_session)._execute(task.id)
    db_session.refresh(task)
    assert task.status == CANCELLED
    assert task.result is None
