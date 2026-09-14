"""买点雷达样本外验证（walk-forward replay）测试。

覆盖四块：
1. 纯函数：成本、评估窗口下标、净收益公式、统计量、对照组抽样；
2. 单日结算：次日停牌 / 零成交量 / 开盘即涨停 → 按买不到处理；
3. 整链路：合成日线上跑完整验证，检查报告结构与免责声明；
4. 无未来函数：改动信号日之后的价格不得改变该日的候选，但必须改变收益。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    HistoricalBar,
    UniverseMember,
    UniverseSnapshot,
)
from app.database.session import Base
from app.market_rules.rules import MarketRuleEngine
from app.realtime.screener import ScreenerConfig, ScreenerUnavailable
from app.realtime.validation import (
    CostModel,
    RadarValidationService,
    RadarValidator,
    ValidationConfig,
    _Bars,
    _max_drawdown_pct,
    _t_stat,
)

DAY0 = date(2026, 6, 1)
BARS = 70
# _eval_indices(70, horizons=(1,3), eval_days=2) → 信号日下标 64、65
SIGNAL_INDEX = 64
# eval_days=5 时，_eval_indices(70, (1,3), 5) → 61..65，最后一个信号日就是它
LAST_SIGNAL_INDEX = 65


# ─────────────────── 脚手架 ───────────────────


def _make_session_factory():
    """独立内存 SQLite（不启用 FK pragma，因此无需再伪造 Securities 行）。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _bars(
    symbol: str,
    closes: list[float],
    volumes: list[float] | None = None,
    opens: list[float] | None = None,
) -> _Bars:
    """造一段连续日线（开=前收，高=收×1.01，低=收×0.99）。"""
    days = [DAY0 + timedelta(days=index) for index in range(len(closes))]
    if opens is None:
        opens = [
            closes[index - 1] if index else closes[0] for index in range(len(closes))
        ]
    vols = list(volumes) if volumes is not None else [1_000_000.0] * len(closes)
    return _Bars(
        symbol=symbol,
        days=days,
        day_index=list(range(len(closes))),
        opens=list(opens),
        highs=[value * 1.01 for value in closes],
        lows=[value * 0.99 for value in closes],
        closes=list(closes),
        volumes=vols,
        amounts=[value * vol for value, vol in zip(closes, vols)],
    )


def _profile(symbol: str, count: int = BARS) -> list[float]:
    """按标的给一段温和走势（单日涨跌幅远小于 ±7%，不会被硬性过滤掉）。"""
    base = {
        "600001": 0.004,
        "600002": 0.003,
        "600003": 0.002,
        "600004": 0.0005,
        "600005": 0.0015,
        "600006": -0.002,
    }[symbol]
    return [round(10.0 + base * index, 4) for index in range(count)]


def _seed(session_factory, symbols: list[str], *, future_scale: float = 1.0) -> None:
    """落一份「当前」股票池快照 + 日线；future_scale 只放大信号日之后的价格。"""
    session = session_factory()
    try:
        snapshot = UniverseSnapshot(
            trading_day=DAY0 + timedelta(days=BARS - 1),
            total_count=len(symbols),
            included_count=len(symbols),
            excluded_count=0,
            source_provider="test",
        )
        session.add(snapshot)
        session.flush()
        for symbol in symbols:
            session.add(
                UniverseMember(
                    snapshot_id=snapshot.id,
                    symbol=symbol,
                    security_id=symbol,
                    is_included=True,
                    name=f"测试{symbol[-2:]}",
                    exchange="SH",
                    board="main",
                    is_st=False,
                    trading_status="active",
                )
            )
            closes = _profile(symbol)
            for index, close in enumerate(closes):
                if index and index <= LAST_SIGNAL_INDEX:
                    open_ = closes[index - 1]
                else:
                    open_ = close
                # 只在最后一个信号日「之后」放大价格：信号日之前（含信号日当天）的数据
                # 必须逐字段一致，否则改变的是打分输入而不是未来信息
                scaled_close = close * future_scale if index > LAST_SIGNAL_INDEX else close
                volume = 10_000_000.0 + index * 1_000.0
                session.add(
                    HistoricalBar(
                        symbol=symbol,
                        period="daily",
                        adjust="none",
                        trade_date=DAY0 + timedelta(days=index),
                        open=open_,
                        high=scaled_close * 1.01,
                        low=scaled_close * 0.99,
                        close=scaled_close,
                        volume=volume,
                        amount=scaled_close * volume,
                    )
                )
        session.commit()
    finally:
        session.close()


def _config(**overrides) -> ValidationConfig:
    payload = {
        "eval_days": 5,
        "horizons": (1, 3),
        "primary_horizon": 1,
        "screener": ScreenerConfig(top_n=3, min_triggers=1),
    }
    payload.update(overrides)
    return ValidationConfig(**payload)


def _meta(symbol: str) -> dict[str, object]:
    return {
        "name": f"测试{symbol[-2:]}",
        "exchange": "SH",
        "board": "main",
        "is_st": False,
    }


# ─────────────────── 1. 纯函数 ───────────────────


def test_cost_model_rates():
    costs = CostModel(commission_rate=0.0003, stamp_tax_rate=0.0005, slippage_bps=5.0)
    assert costs.buy_cost == pytest.approx(0.0003 + 0.0005)
    assert costs.sell_cost == pytest.approx(0.0003 + 0.0005 + 0.0005)
    assert costs.round_trip == pytest.approx(0.0021)
    assert costs.as_dict()["round_trip_pct"] == pytest.approx(0.21)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"eval_days": 3},
        {"eval_days": 500},
        {"horizons": ()},
        {"horizons": (1, 0)},
        {"horizons": (1, 3, 3)},
        {"primary_horizon": 7},
        {"control": "bogus"},
        {"costs": CostModel(slippage_bps=-1)},
        {"screener": ScreenerConfig(top_n=0)},
    ],
)
def test_config_validate_rejects_bad_values(kwargs):
    config = _config(**kwargs)
    with pytest.raises(ValueError):
        config.validate()


def test_config_validate_accepts_defaults():
    ValidationConfig().validate()
    _config().validate()


def test_eval_indices_leaves_room_for_lookback_and_horizon():
    cfg = _config()
    indices = RadarValidator._eval_indices(BARS, cfg)
    assert indices == list(range(SIGNAL_INDEX - 3, SIGNAL_INDEX + 2))
    assert len(indices) == cfg.eval_days
    # 末尾必须留出最长持有期：最后一天不能既当信号日又当出场日
    assert indices[-1] + 1 + max(cfg.horizons) <= BARS - 1


def test_net_return_applies_round_trip_cost():
    costs = CostModel()
    bucket = _buckets_for("600001", [10.0, 10.0, 11.0])
    value = RadarValidator._net_return(bucket, entry_pos=1, exit_pos=2, costs=costs)
    expected = ((11.0 * (1 - costs.sell_cost)) / (10.0 * (1 + costs.buy_cost)) - 1) * 100
    assert value == pytest.approx(expected)
    assert value < 10.0  # 毛收益 10%，净收益必须更低


def test_net_return_returns_none_for_bad_prices():
    costs = CostModel()
    # 开盘价（= 前一日收盘）为 0 → 视为无效
    bucket = _buckets_for("600001", [0.0, 0.0, 11.0])
    assert RadarValidator._net_return(bucket, 1, 2, costs) is None


def _buckets_for(symbol: str, closes: list[float]) -> _Bars:
    return _bars(symbol, closes)


def test_t_stat_and_drawdown():
    assert _t_stat([1.0]) == 0.0
    assert _t_stat([1.0, 1.0, 1.0]) == 0.0  # 零方差
    # 样本 [1, 3]：均值 2、样本标准差 √2、t = 2 / (√2/√2) = 2
    assert _t_stat([1.0, 3.0]) == pytest.approx(2.0)
    assert _max_drawdown_pct([]) == 0.0
    assert _max_drawdown_pct([10.0, -10.0]) == pytest.approx(-10.0)
    assert _max_drawdown_pct([-50.0, 100.0]) == pytest.approx(-50.0)


def test_control_sample_is_deterministic_and_sized():
    items = [
        type("Item", (), {"symbol": f"60{i:04d}", "amount_20": float(i)})()
        for i in range(20)
    ]
    day = DAY0 + timedelta(days=SIGNAL_INDEX)
    first = RadarValidator._control_sample(items, day, 5)
    second = RadarValidator._control_sample(items, day, 5)
    assert [item.symbol for item in first] == [item.symbol for item in second]
    assert len(first) == 5
    assert len({item.symbol for item in first}) == 5
    # 换一天必须换一批（否则对照组就不是抽样而是固定名单）
    other = RadarValidator._control_sample(items, day + timedelta(days=1), 5)
    assert [item.symbol for item in other] != [item.symbol for item in first]


def test_trading_days_backfills_global_index():
    first = _buckets_for("600001", [10.0, 10.1, 10.2])
    second = _buckets_for("600002", [20.0, 20.1])
    bucket_map = {"600001": first, "600002": second}
    days = RadarValidator._trading_days(bucket_map)
    assert len(days) == 3
    assert first.day_index == [0, 1, 2]
    assert second.day_index == [0, 1]
    assert first.position(2) == 2
    assert second.position(2) is None
    assert first.position(0) == 0

# ─────────────────── 2. 单日结算 ───────────────────


def test_opened_limit_up_detects_limit_open():
    rules = MarketRuleEngine()
    meta_map = {"600000": _meta("600000")}
    # 昨收 10.00 → 涨停价 11.00
    at_limit = _bars("600000", [10.0, 11.0, 11.0], opens=[10.0, 11.0, 11.0])
    assert RadarValidator._opened_limit_up(at_limit, 0, 1, rules, meta_map, None) is True
    below = _bars("600000", [10.0, 10.5, 10.6], opens=[10.0, 10.4, 10.5])
    assert RadarValidator._opened_limit_up(below, 0, 1, rules, meta_map, None) is False


def test_outcome_blocks_when_next_day_is_missing():
    cfg = _config()
    bucket = _buckets_for("600001", [10.0, 10.1, 10.2, 10.3])
    bucket.day_index[1] = 9  # 次日停牌：下一根 K 线不是紧接着的交易日
    values, blocked, missing = RadarValidator()._outcome(
        bucket, 0, cfg, MarketRuleEngine(), None, {"600001": _meta("600001")}
    )
    assert blocked == 1
    assert missing == 0
    assert all(value is None for value in values.values())


def test_outcome_blocks_zero_volume():
    cfg = _config()
    bucket = _bars("600001", [10.0, 10.1, 10.2, 10.3], volumes=[1e6, 0.0, 1e6, 1e6])
    values, blocked, missing = RadarValidator()._outcome(
        bucket, 0, cfg, MarketRuleEngine(), None, {"600001": _meta("600001")}
    )
    assert (blocked, missing) == (1, 0)
    assert all(value is None for value in values.values())


def test_outcome_computes_each_horizon():
    cfg = _config()
    bucket = _buckets_for("600001", [10.0, 10.0, 10.5, 11.0, 11.5])
    values, blocked, missing = RadarValidator()._outcome(
        bucket, 0, cfg, MarketRuleEngine(), None, {"600001": _meta("600001")}
    )
    assert (blocked, missing) == (0, 0)
    assert set(values) == {1, 3}
    assert all(value is not None for value in values.values())
    assert values[3] > values[1]


# ─────────────────── 3. 整链路 ───────────────────


def test_run_produces_structured_report():
    factory = _make_session_factory()
    symbols = ["600001", "600002", "600003", "600004", "600005", "600006"]
    _seed(factory, symbols)

    report = RadarValidator(factory).run(_config())

    assert report.universe_size == 6
    assert report.evaluation_days == 5
    assert [item.horizon for item in report.horizons] == [1, 3]
    assert report.first_signal_day == DAY0 + timedelta(days=SIGNAL_INDEX - 3)
    assert report.control == "none"
    assert report.bars_last_day == DAY0 + timedelta(days=BARS - 1)
    assert report.config.costs.round_trip > 0
    assert len(report.daily) == 5
    # 每日候选都带了代码，且都在股票池里
    for record in report.daily:
        assert set(record.symbols) <= set(symbols)
    assert all(item.observations >= 0 for item in report.horizons)
    # 免责声明与偏差说明必须在
    assert report.notes[-1] == "分析结果仅用于研究，不构成投资建议"
    assert any("幸存者偏差" in text for text in report.caveats)
    assert any("不复权" in text for text in report.caveats)
    assert any("窗口重叠" in text for text in report.caveats)
    # none 模式才有分层统计
    assert report.score_buckets


def test_run_raises_when_universe_is_empty():
    factory = _make_session_factory()
    with pytest.raises(ScreenerUnavailable):
        RadarValidator(factory).run(_config())


def test_run_raises_when_history_is_too_short():
    factory = _make_session_factory()
    symbols = ["600001", "600002"]
    _seed(factory, symbols)
    session = factory()
    try:
        session.query(HistoricalBar).filter(
            HistoricalBar.trade_date > DAY0 + timedelta(days=40)
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
    with pytest.raises(ScreenerUnavailable):
        RadarValidator(factory).run(_config())


def test_control_modes_skip_score_buckets():
    factory = _make_session_factory()
    symbols = ["600001", "600002", "600003", "600004"]
    _seed(factory, symbols)
    for mode in ("random", "worst"):
        report = RadarValidator(factory).run(_config(control=mode))
        assert report.control == mode
        assert report.score_buckets == ()
        assert any("对照模式" in text for text in report.notes)


def test_future_prices_do_not_change_the_picks_but_change_the_return():
    """无未来函数：只改信号日之后的价格，候选名单必须不变，收益必须变。"""
    symbols = ["600001", "600002", "600003", "600004", "600005", "600006"]
    baseline_factory = _make_session_factory()
    _seed(baseline_factory, symbols)
    scaled_factory = _make_session_factory()
    _seed(scaled_factory, symbols, future_scale=2.0)

    baseline = RadarValidator(baseline_factory).run(_config())
    scaled = RadarValidator(scaled_factory).run(_config())

    assert len(baseline.daily) == len(scaled.daily)
    for left, right in zip(baseline.daily, scaled.daily):
        assert left.signal_day == right.signal_day
        assert left.symbols == right.symbols
        assert left.eligible == right.eligible
    # 未来价格被放大 → 出场价变了 → 至少有一个评估日的收益不同
    assert any(
        (left.net_pct or 0.0) != (right.net_pct or 0.0)
        for left, right in zip(baseline.daily, scaled.daily)
    )


# ─────────────────── 4. 服务与接口 ───────────────────


class _StubReport:
    generated_at = "2026-09-12T00:00:00+00:00"


class _StubValidator:
    """替身验证器：不进数据库，只验证调度与状态机。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[ValidationConfig] = []
        self._fail = fail

    def run(self, config, progress=None):
        self.calls.append(config)
        if progress is not None:
            progress(1, config.eval_days)
        if self._fail:
            raise RuntimeError("boom")
        return _StubReport()


@pytest.mark.asyncio
async def test_service_runs_validator_and_caches_report():
    validator = _StubValidator()
    service = RadarValidationService(validator=validator)
    status = await service.start(_config())
    assert status["state"] == "running"
    report = await service.wait()
    assert report is not None
    assert validator.calls[0].eval_days == 5
    final = service.status()
    assert final["state"] == "done"
    assert final["has_report"] is True
    assert final["report_generated_at"] == _StubReport.generated_at
    assert final["error"] is None
    assert final["finished_at"] is not None


@pytest.mark.asyncio
async def test_service_records_failure_without_raising():
    service = RadarValidationService(validator=_StubValidator(fail=True))
    await service.start(_config())
    report = await service.wait()
    assert report is None
    status = service.status()
    assert status["state"] == "failed"
    assert "boom" in str(status["error"])


@pytest.mark.asyncio
async def test_service_start_is_idempotent_while_running():
    service = RadarValidationService(validator=_StubValidator())
    await service.start(_config())
    second = await service.start(_config(eval_days=5))
    assert second["state"] in {"running", "done"}
    await service.wait()


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


class _FakeApiService:
    """接口层替身：记录 start 收到的配置，不触发真实计算。"""

    def __init__(self) -> None:
        self.configs: list[ValidationConfig] = []

    @property
    def report(self):
        return None

    def status(self):
        return {
            "state": "running",
            "progress": {"done": 0, "total": 60},
            "started_at": None,
            "finished_at": None,
            "error": None,
            "has_report": False,
            "report_generated_at": None,
        }

    async def start(self, config: ValidationConfig):
        self.configs.append(config)
        return self.status()


def test_api_validation_get_returns_idle_without_report():
    from app.api.deps import get_radar_validation_service
    from app.main import app

    service = RadarValidationService(validator=_StubValidator())
    app.dependency_overrides[get_radar_validation_service] = lambda: service
    try:
        response = _client().get("/api/realtime/picks/validation")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"]["state"] == "idle"
        assert payload["report"] is None
    finally:
        app.dependency_overrides.pop(get_radar_validation_service, None)


def test_api_validation_post_accepts_config_and_returns_202():
    from app.api.deps import get_radar_validation_service
    from app.main import app

    service = _FakeApiService()
    app.dependency_overrides[get_radar_validation_service] = lambda: service
    try:
        response = _client().post(
            "/api/realtime/picks/validation",
            params={
                "eval_days": 12,
                "horizons": "1,5",
                "primary_horizon": 5,
                "control": "random",
                "commission_rate": 0.0002,
            },
        )
        assert response.status_code == 202
        assert service.configs[0].eval_days == 12
        assert service.configs[0].horizons == (1, 5)
        assert service.configs[0].primary_horizon == 5
        assert service.configs[0].control == "random"
        assert service.configs[0].costs.commission_rate == pytest.approx(0.0002)
    finally:
        app.dependency_overrides.pop(get_radar_validation_service, None)


@pytest.mark.parametrize(
    "params",
    [
        {"horizons": "abc"},
        {"horizons": "1,"},
        {"eval_days": 3},
        {"control": "bogus"},
        {"primary_horizon": 7},
        {"top_n": 0},
        {"slippage_bps": -1},
    ],
)
def test_api_validation_post_rejects_invalid_params(params):
    from app.api.deps import get_radar_validation_service
    from app.main import app

    service = _FakeApiService()
    app.dependency_overrides[get_radar_validation_service] = lambda: service
    try:
        response = _client().post("/api/realtime/picks/validation", params=params)
        assert response.status_code == 422
        assert service.configs == []
    finally:
        app.dependency_overrides.pop(get_radar_validation_service, None)
