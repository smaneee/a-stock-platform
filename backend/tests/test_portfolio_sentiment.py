"""市场情绪闸门测试：阈值、滞后、缺数据、仓位缩放与回测 / 接口集成。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.database.models import LimitUpSentiment
from app.main import app
from app.market_data.base import QuoteData
from app.portfolio.config import PortfolioConfig
from app.portfolio.engine import PortfolioBacktestEngine
from app.portfolio.sentiment import (
    MISSING_DATA_REASON,
    SentimentGate,
    SentimentGateConfig,
    SentimentSnapshot,
)
from app.strategies.base import Signal, Strategy
from app.tasks.portfolio_worker import PortfolioBacktestWorker
from tests.helpers import make_quote

DAY_ONE = date(2026, 1, 5)


def _snapshot(
    day: date,
    *,
    seal_rate: float | None,
    max_streak: int = 3,
    limit_up_count: int = 50,
    broken_board_count: int = 20,
) -> SentimentSnapshot:
    return SentimentSnapshot(
        trade_date=day,
        limit_up_count=limit_up_count,
        broken_board_count=broken_board_count,
        seal_rate=seal_rate,
        broken_rate=None if seal_rate is None else 1 - seal_rate,
        max_streak=max_streak,
    )


def _series(*seal_rates: float | None) -> list[SentimentSnapshot]:
    return [
        _snapshot(DAY_ONE + timedelta(days=index), seal_rate=rate)
        for index, rate in enumerate(seal_rates)
    ]


def make_daily_bars(
    symbol: str,
    n: int,
    base_price: float = 10.0,
    step: float = 0.05,
) -> list[QuoteData]:
    """生成逐日递增的 K 线（每个自然日一根，测试里不做交易日过滤）。"""
    bars = []
    for index in range(n):
        day = DAY_ONE + timedelta(days=index)
        price = base_price + index * step
        bars.append(
            make_quote(
                symbol=symbol,
                price=price,
                open=price - 0.02,
                high=price + 0.1,
                low=price - 0.1,
                previous_close=price - 0.05,
                volume=1_000_000.0,
                market_time=datetime(day.year, day.month, day.day, 9, 30),
            )
        )
    return bars


class ScheduleStrategy(Strategy):
    """按已见 K 线数量触发信号，便于精确构造有闸门 / 无闸门的对照。"""

    name = "schedule"

    def __init__(self, buy_at: int = 5, sell_at: int = 12):
        self.buy_at = buy_at
        self.sell_at = sell_at

    def analyze(self, history: list[QuoteData]) -> Signal | None:
        if not history:
            return None
        latest = history[-1]
        count = len(history)
        direction = None
        if count == self.buy_at:
            direction = "BUY"
        elif count == self.sell_at:
            direction = "SELL"
        if direction is None:
            return None
        return self._build_signal(
            symbol=latest.symbol,
            direction=direction,
            reason="测试",
            price=latest.price,
            source_time=latest.market_time or latest.received_at,
        )


class TestGateDecision:
    def test_lag_uses_previous_sentiment_day(self):
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6),
            _series(0.8, 0.4),
        )

        # 1/6 用 1/5 的情绪（0.8 达标）→ 放行
        first = gate.decision(date(2026, 1, 6))
        assert first.allowed is True
        assert first.sentiment_date == date(2026, 1, 5)
        # 1/7 用 1/6 的情绪（0.4 不达标）→ 拦截
        second = gate.decision(date(2026, 1, 7))
        assert second.allowed is False
        assert second.exposure == 0.0
        assert second.reasons == ("seal_rate_below_min",)

    def test_lag_days_shifts_window_further_back(self):
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6, lag_days=2),
            _series(0.8, 0.4),
        )

        decision = gate.decision(date(2026, 1, 7))

        assert decision.sentiment_date == date(2026, 1, 5)
        assert decision.allowed is True

    def test_missing_data_policy(self):
        series = _series(0.8)
        allow = SentimentGate(SentimentGateConfig(enabled=True, min_seal_rate=0.6), series)
        blocked = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6, on_missing="block"),
            series,
        )

        # 序列首日之前没有情绪可用
        allowed_decision = allow.decision(DAY_ONE)
        assert allowed_decision.allowed is True
        assert allowed_decision.exposure == 1.0
        assert allowed_decision.reasons == (MISSING_DATA_REASON,)
        assert allowed_decision.sentiment_date is None

        blocked_decision = blocked.decision(DAY_ONE)
        assert blocked_decision.allowed is False
        assert blocked_decision.exposure == 0.0
        assert blocked_decision.reasons == (MISSING_DATA_REASON,)

    def test_all_thresholds_report_reasons(self):
        gate = SentimentGate(
            SentimentGateConfig(
                enabled=True,
                min_seal_rate=0.8,
                max_broken_rate=0.1,
                min_max_streak=5,
                min_limit_up_count=100,
            ),
            [
                _snapshot(
                    DAY_ONE,
                    seal_rate=0.5,
                    max_streak=2,
                    limit_up_count=30,
                    broken_board_count=30,
                )
            ],
        )

        decision = gate.decision(date(2026, 1, 6))

        assert decision.allowed is False
        assert set(decision.reasons) == {
            "seal_rate_below_min",
            "broken_rate_above_max",
            "max_streak_below_min",
            "limit_up_count_below_min",
        }

    def test_absent_seal_rate_fails_threshold(self):
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6),
            _series(None),
        )

        decision = gate.decision(date(2026, 1, 6))

        # 分母为 0（既无涨停也无炸板）属于极弱情绪，按不达标处理
        assert decision.allowed is False
        assert "seal_rate_below_min" in decision.reasons

    def test_exposure_scaling_bounds(self):
        weak = _series(0.55)
        scaled = SentimentGate(
            SentimentGateConfig(enabled=True, scale_exposure=True, min_exposure=0.3),
            weak,
        )
        floored = SentimentGate(
            SentimentGateConfig(enabled=True, scale_exposure=True, min_exposure=0.8),
            weak,
        )
        plain = SentimentGate(SentimentGateConfig(enabled=True), weak)

        assert scaled.decision(date(2026, 1, 6)).exposure == pytest.approx(0.55)
        assert floored.decision(date(2026, 1, 6)).exposure == pytest.approx(0.8)
        assert plain.decision(date(2026, 1, 6)).exposure == pytest.approx(1.0)

    def test_empty_series_always_missing(self):
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.1, on_missing="block"),
            [],
        )

        assert len(gate) == 0
        assert gate.first_date is None
        assert gate.decision(date(2026, 1, 6)).allowed is False

    def test_config_validation_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            SentimentGate(SentimentGateConfig(enabled=True, lag_days=0), [])
        with pytest.raises(ValueError):
            SentimentGate(SentimentGateConfig(enabled=True, min_seal_rate=1.5), [])
        with pytest.raises(ValueError):
            SentimentGate(SentimentGateConfig(enabled=True, min_exposure=-0.1), [])
        with pytest.raises(ValueError):
            SentimentGate(SentimentGateConfig(enabled=True, on_missing="maybe"), [])
        with pytest.raises(ValueError):
            SentimentGate(SentimentGateConfig(enabled=True, min_limit_up_count=-1), [])

    def test_has_threshold(self):
        assert SentimentGateConfig().has_threshold() is False
        assert (
            SentimentGateConfig(min_seal_rate=0.6).has_threshold() is True
        )
        assert SentimentGateConfig(min_max_streak=3).has_threshold() is True


class TestEngineIntegration:
    def test_gate_blocks_new_positions(self):
        bars = make_daily_bars("600000", 20)
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6),
            _series(*([0.2] * 20)),
        )

        gated = PortfolioBacktestEngine(
            strategies={"600000": ScheduleStrategy()}, sentiment=gate
        ).run({"600000": bars})
        plain = PortfolioBacktestEngine(
            strategies={"600000": ScheduleStrategy()}
        ).run({"600000": bars})

        assert gated.sentiment is not None
        assert gated.sentiment["config"]["min_seal_rate"] == 0.6
        assert gated.sentiment["blocked_buy_signals"] == 1
        # 只有序列首日（没有更早情绪）放行
        assert gated.sentiment["blocked_days"] == 19
        assert gated.sentiment["allowed_days"] == 1
        assert gated.trade_count == 0
        assert plain.trade_count >= 1
        # 未启用闸门时不产生情绪汇总，回测结果与老版本一致
        assert plain.sentiment is None

    def test_gate_allows_when_sentiment_healthy(self):
        bars = make_daily_bars("600000", 20)
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6),
            _series(*([0.9] * 20)),
        )

        result = PortfolioBacktestEngine(
            strategies={"600000": ScheduleStrategy()}, sentiment=gate
        ).run({"600000": bars})

        assert result.sentiment["blocked_buy_signals"] == 0
        assert result.sentiment["missing_days"] == [DAY_ONE.isoformat()]
        assert result.trade_count >= 1

    def test_sell_is_never_blocked(self):
        bars = make_daily_bars("600000", 20)
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, min_seal_rate=0.6),
            _series(*([0.9] * 8 + [0.2] * 12)),
        )

        result = PortfolioBacktestEngine(
            strategies={"600000": ScheduleStrategy(buy_at=5, sell_at=13)},
            sentiment=gate,
        ).run({"600000": bars})

        sides = [trade["side"] for trade in result.trades]
        assert "BUY" in sides
        assert "SELL" in sides

    def test_position_scales_with_seal_rate(self):
        bars = make_daily_bars("600000", 20)
        config = PortfolioConfig(max_single_position=1.0, max_total_position=1.0)
        gate = SentimentGate(
            SentimentGateConfig(enabled=True, scale_exposure=True, min_exposure=0.1),
            _series(*([0.5] * 20)),
        )

        scaled = PortfolioBacktestEngine(
            strategies={"600000": ScheduleStrategy()}, config=config, sentiment=gate
        ).run({"600000": bars})
        plain = PortfolioBacktestEngine(
            strategies={"600000": ScheduleStrategy()}, config=config
        ).run({"600000": bars})

        assert scaled.sentiment["average_buy_exposure"] == pytest.approx(0.5)
        assert 0 < scaled.total_return < plain.total_return


class TestWorkerGate:
    def test_requires_sentiment_data_when_enabled(self, db_session):
        worker = PortfolioBacktestWorker()

        with pytest.raises(ValueError):
            worker._build_sentiment_gate(
                db_session,
                {"sentiment_gate": {"enabled": True, "min_seal_rate": 0.6}},
                DAY_ONE,
                date(2026, 1, 9),
            )

    def test_builds_gate_from_rows(self, db_session):
        db_session.add(
            LimitUpSentiment(
                trade_date=DAY_ONE,
                limit_up_count=50,
                broken_board_count=10,
                seal_rate=0.8,
                broken_rate=0.2,
                max_streak=4,
            )
        )
        db_session.commit()

        gate = PortfolioBacktestWorker()._build_sentiment_gate(
            db_session,
            {"sentiment_gate": {"enabled": True, "min_seal_rate": 0.6}},
            DAY_ONE,
            date(2026, 1, 9),
        )

        assert gate is not None
        assert len(gate) == 1
        assert gate.decision(date(2026, 1, 6)).allowed is True

    def test_absent_or_disabled_gate_returns_none(self, db_session):
        worker = PortfolioBacktestWorker()
        end = date(2026, 1, 9)

        assert worker._build_sentiment_gate(db_session, {}, DAY_ONE, end) is None
        assert (
            worker._build_sentiment_gate(
                db_session, {"sentiment_gate": {"enabled": False}}, DAY_ONE, end
            )
            is None
        )
        assert (
            worker._build_sentiment_gate(
                db_session, {"sentiment_gate": "oops"}, DAY_ONE, end
            )
            is None
        )


class TestPortfolioBacktestApi:
    url = "/api/portfolio-backtests"
    base = {
        "symbols": ["600000"],
        "strategy_name": "ma_cross",
        "start_time": "2026-01-05T09:30:00",
        "end_time": "2026-02-05T15:00:00",
    }

    def test_persists_gate_config(self):
        response = TestClient(app).post(
            self.url,
            json={
                **self.base,
                "sentiment_gate": {"enabled": True, "min_seal_rate": 0.6},
            },
        )

        assert response.status_code == 201
        gate = response.json()["sentiment_gate"]
        assert gate["enabled"] is True
        assert gate["min_seal_rate"] == pytest.approx(0.6)
        assert gate["lag_days"] == 1
        assert gate["on_missing"] == "allow"

    def test_plain_backtest_has_no_gate(self):
        response = TestClient(app).post(self.url, json=self.base)

        assert response.status_code == 201
        assert response.json()["sentiment_gate"] is None

    def test_rejects_enabled_gate_without_conditions(self):
        response = TestClient(app).post(
            self.url, json={**self.base, "sentiment_gate": {"enabled": True}}
        )

        assert response.status_code == 422

    def test_rejects_lag_days_zero(self):
        response = TestClient(app).post(
            self.url,
            json={
                **self.base,
                "sentiment_gate": {
                    "enabled": True,
                    "lag_days": 0,
                    "min_seal_rate": 0.6,
                },
            },
        )

        assert response.status_code == 422

    def test_rejects_out_of_range_threshold(self):
        response = TestClient(app).post(
            self.url,
            json={
                **self.base,
                "sentiment_gate": {"enabled": True, "min_seal_rate": 1.5},
            },
        )

        assert response.status_code == 422
