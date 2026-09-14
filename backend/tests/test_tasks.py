"""后台回测任务 worker 测试。"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.database.models import Backtest
from app.tasks.status import (
    CANCELLABLE_STATES,
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TERMINAL_STATES,
)
from app.tasks.worker import BacktestWorker


def _factory(session):
    """从测试 session 构造 sessionmaker，供 worker 使用。"""
    return sessionmaker(bind=session.bind, autoflush=False, autocommit=False)


def _make_backtest(db, status=QUEUED, **kwargs):
    bt = Backtest(
        symbol=kwargs.get("symbol", "600000"),
        strategy_name=kwargs.get("strategy_name", "ma_cross"),
        start_time=kwargs.get("start_time", datetime(2026, 1, 1)),
        end_time=kwargs.get("end_time", datetime(2026, 1, 10)),
        initial_cash=kwargs.get("initial_cash", 100000),
        status=status,
        idempotency_key=kwargs.get("idempotency_key"),
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


# ──────── 状态常量 ────────


def test_status_constants():
    assert TERMINAL_STATES == {SUCCEEDED, FAILED, CANCELLED}
    assert CANCELLABLE_STATES == {QUEUED, RUNNING}


# ──────── 恢复遗留任务 ────────


def test_recover_stale_tasks(db_session):
    bt = _make_backtest(db_session, status=RUNNING)
    worker = BacktestWorker(session_factory=_factory(db_session))
    recovered = worker._recover_stale_tasks()
    assert recovered == 1
    db_session.refresh(bt)
    assert bt.status == QUEUED
    assert bt.started_at is None


def test_recover_no_stale(db_session):
    _make_backtest(db_session, status=QUEUED)
    _make_backtest(db_session, status=SUCCEEDED)
    worker = BacktestWorker(session_factory=_factory(db_session))
    assert worker._recover_stale_tasks() == 0


# ──────── 认领任务 ────────


def test_claim_next(db_session):
    bt = _make_backtest(db_session, status=QUEUED)
    worker = BacktestWorker(session_factory=_factory(db_session))
    claimed_id = worker._claim_next()
    assert claimed_id == bt.id
    db_session.refresh(bt)
    assert bt.status == RUNNING
    assert bt.started_at is not None


def test_claim_next_empty(db_session):
    worker = BacktestWorker(session_factory=_factory(db_session))
    assert worker._claim_next() is None


def test_claim_next_picks_oldest(db_session):
    bt1 = _make_backtest(db_session, status=QUEUED)
    bt2 = _make_backtest(db_session, status=QUEUED)
    worker = BacktestWorker(session_factory=_factory(db_session))
    assert worker._claim_next() == min(bt1.id, bt2.id)


# ──────── 标记失败 ────────


def test_mark_failed(db_session):
    bt = _make_backtest(db_session, status=RUNNING)
    worker = BacktestWorker(session_factory=_factory(db_session))
    worker._mark_failed(bt.id, "测试错误")
    db_session.refresh(bt)
    assert bt.status == FAILED
    assert "测试错误" in (bt.error_message or "")
    assert bt.finished_at is not None


def test_mark_failed_skips_cancelled(db_session):
    """已取消的任务不应被覆盖为 failed。"""
    bt = _make_backtest(db_session, status=CANCELLED)
    worker = BacktestWorker(session_factory=_factory(db_session))
    worker._mark_failed(bt.id, "测试错误")
    db_session.refresh(bt)
    assert bt.status == CANCELLED


# ──────── 执行（async） ────────


@pytest.mark.asyncio
async def test_execute_success(db_session, monkeypatch):
    """完整执行流程：queued→running→succeeded。"""
    bt = _make_backtest(db_session, status=RUNNING)

    # mock 历史数据服务，返回足够的 K 线
    from app.market_data.base import QuoteData
    from app.time_utils import utc_now

    fake_bars = []
    for i in range(60):
        fake_bars.append(
            QuoteData(
                symbol="600000", name="600000", price=10 + i * 0.01,
                open=10 + i * 0.01, high=10.2 + i * 0.01, low=9.8 + i * 0.01,
                previous_close=9.99, volume=1000, amount=10000, source="mock",
                market_time=utc_now(), is_stale=False,
            )
        )

    async def fake_get_history(self, symbol, start, end, period="daily", adjust="none", sync_if_incomplete=True):
        from app.history.service import HistoryResult
        from app.history.quality import QualityReport
        return HistoryResult(bars=fake_bars, source="cache", data_updated_at=None, is_complete=True, quality=QualityReport(total=len(fake_bars)))

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )

    worker = BacktestWorker(session_factory=_factory(db_session))
    await worker._execute(bt.id)

    db_session.refresh(bt)
    assert bt.status == SUCCEEDED
    assert bt.progress == 100
    assert bt.result is not None
    assert bt.finished_at is not None


@pytest.mark.asyncio
async def test_execute_no_history_marks_failed(db_session, monkeypatch):
    """无历史数据时标记 failed。"""
    bt = _make_backtest(db_session, status=RUNNING)

    async def fake_get_history(self, symbol, start, end, period="daily", adjust="none", sync_if_incomplete=True):
        from app.history.service import HistoryResult
        from app.history.quality import QualityReport
        return HistoryResult(bars=[], source="cache", data_updated_at=None, is_complete=False, quality=QualityReport(total=0))

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )

    worker = BacktestWorker(session_factory=_factory(db_session))
    await worker._execute(bt.id)

    db_session.refresh(bt)
    assert bt.status == FAILED
    assert "历史数据" in (bt.error_message or "")


# ──────── D6：单标的回测也必须「前复权收益 + 未复权涨跌停昨收」 ────────


def _daily_bars(n: int = 60, base: float = 10.0):
    """构造有真实交易日的日线（不是 60 根同一个日期）。"""
    from datetime import timedelta

    from app.market_data.base import QuoteData

    start = datetime(2026, 1, 5)
    out = []
    for i in range(n):
        price = base + i * 0.01
        out.append(
            QuoteData(
                symbol="600000",
                name="测试",
                price=price,
                open=price,
                high=price,
                low=price,
                previous_close=price - 0.01,
                volume=1_000_000,
                amount=10_000_000,
                source="mock",
                market_time=start + timedelta(days=i),
            )
        )
    return out


def _patch_history(monkeypatch, qfq_bars, none_bars, captured):
    async def fake_get_history(self, symbol, start, end, **kwargs):
        from app.history.quality import QualityReport
        from app.history.service import HistoryResult

        adjust = kwargs.get("adjust", "none")
        captured.append(adjust)
        bars = none_bars if adjust == "none" else qfq_bars
        return HistoryResult(
            bars=bars,
            source="cache",
            data_updated_at=None,
            is_complete=True,
            quality=QualityReport(total=len(bars)),
        )

    monkeypatch.setattr(
        "app.history.service.HistoricalDataService.get_history", fake_get_history
    )


@pytest.mark.asyncio
async def test_execute_uses_qfq_with_unadjusted_limit_reference(db_session, monkeypatch):
    """单标的回测：策略/收益取 qfq，另取 none 仅用于还原涨跌停昨收。"""
    captured: list[str] = []
    _patch_history(monkeypatch, _daily_bars(), _daily_bars(), captured)
    bt = _make_backtest(db_session, status=RUNNING)

    worker = BacktestWorker(session_factory=_factory(db_session))
    await worker._execute(bt.id)

    db_session.refresh(bt)
    assert bt.status == SUCCEEDED
    assert "qfq" in captured, f"策略/收益口径必须是前复权，实际请求={captured}"
    assert "none" in captured, f"涨跌停昨收必须另取未复权序列，实际请求={captured}"

    payload = __import__("json").loads(bt.result)
    assert payload["bars_adjust"] == "qfq"
    assert payload["bars_adjust_label"] == "前复权"
    assert payload["limit_reference"] == "unadjusted_previous_close"
    assert payload["limit_reference_missing"] == 0
    assert payload["limit_reference_complete"] is True
    assert "limit_reference_note" not in payload


@pytest.mark.asyncio
async def test_execute_reverts_to_unadjusted_when_configured(db_session, monkeypatch):
    """一键回退：backtest_bars_adjust=none 时只请求未复权（改造前行为）。"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backtest_bars_adjust", "none", raising=False)
    captured: list[str] = []
    _patch_history(monkeypatch, _daily_bars(), _daily_bars(), captured)
    bt = _make_backtest(db_session, status=RUNNING)

    worker = BacktestWorker(session_factory=_factory(db_session))
    await worker._execute(bt.id)

    db_session.refresh(bt)
    assert bt.status == SUCCEEDED
    assert captured == ["none"], f"回退后只应请求一次未复权，实际={captured}"
    payload = __import__("json").loads(bt.result)
    assert payload["bars_adjust"] == "none"
    assert payload["bars_adjust_label"] == "不复权"
    assert payload["limit_reference"] == "none"


@pytest.mark.asyncio
async def test_execute_reports_limit_reference_gap(db_session, monkeypatch):
    """未复权序列缺最后一天时，必须把缺口写进结果，而不是静默跳过涨跌停。"""
    captured: list[str] = []
    _patch_history(monkeypatch, _daily_bars(), _daily_bars()[:-1], captured)
    bt = _make_backtest(db_session, status=RUNNING)

    worker = BacktestWorker(session_factory=_factory(db_session))
    await worker._execute(bt.id)

    db_session.refresh(bt)
    assert bt.status == SUCCEEDED
    payload = __import__("json").loads(bt.result)
    assert payload["limit_reference_missing"] == 1
    assert payload["limit_reference_complete"] is False
    assert "涨跌停判断已跳过" in payload["limit_reference_note"]


def test_backtest_result_meta_reports_configured_adjust():
    """`BacktestResult.to_dict()` 必须带口径标注，且随配置变化。"""
    from app.backtest.engine import BacktestResult

    qfq = BacktestResult(equity_curve=[100.0], bars_adjust="qfq").to_dict()
    assert qfq["bars_adjust"] == "qfq"
    assert qfq["bars_adjust_label"] == "前复权"
    assert qfq["limit_reference"] == "unadjusted_previous_close"
    assert "除权除息跳空已在本地还原" in qfq["return_convention_note"]

    default = BacktestResult(equity_curve=[100.0]).to_dict()
    assert default["bars_adjust"] == "none"
    assert default["limit_reference"] == "none"
