"""「赚钱率前五」接口测试：排序、样本门槛、口径声明（用假选股服务 + 真实日线表）。

这两条 500 就是被这类测试挡住的：`ScreenerService.screen` 不存在、
`ScreenerResult` 没有 `session` 字段 —— 只测纯函数不会发现接口层写错名字。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_screener_service
from app.database.models import HistoricalBar
from app.database.session import get_db

UP_SYMBOL = "000001"
FLAT_SYMBOL = "000002"
TINY_SYMBOL = "000003"


def _bars(day_count: int, start: float, step: float) -> list[tuple[date, float, float]]:
    base = date(2026, 9, 11)
    rows = []
    for index in range(day_count):
        day = base - timedelta(days=day_count - 1 - index)
        price = round(start * (1 + step) ** index, 4)
        rows.append((day, price, 1_000_000.0))
    return rows


def _seed(db, symbol: str, rows: list[tuple[date, float, float]]) -> None:
    for day, close, volume in rows:
        db.add(
            HistoricalBar(
                symbol=symbol, period="daily", adjust="qfq", trade_date=day,
                open=close, high=close, low=close, close=close, volume=volume,
                amount=close * volume, source="test",
            )
        )
    db.commit()


def _pick(symbol: str, rank: int, strength: float) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol, name=f"测试{symbol}", rank=rank, price=10.0, change_pct=1.0,
        strength_score=strength, score=86.0, live=True,
    )


class _FakeScreener:
    """只实现端点真正用到的字段；字段名写错就会 AttributeError → 测试失败。"""

    def __init__(self, picks) -> None:
        self._picks = picks

    async def run(self, config=None):  # noqa: ANN001 - 与真实签名保持兼容
        return SimpleNamespace(
            picks=self._picks,
            generated_at_cst="2026-09-15T11:20:00+08:00",
            session_day=date(2026, 9, 15),
            signal_day=date(2026, 9, 15),
            live=True,
            bars_last_day=date(2026, 9, 11),
            coverage_ratio=1.0,
            coverage_ok=True,
            bars_adjust="qfq",
            scan_seconds=12.5,
        )


@pytest.fixture
def client(db_session):
    from app.main import app

    # 上涨序列 → 回放胜率 100%；平盘 → 0%；很短 → 样本不足
    _seed(db_session, UP_SYMBOL, _bars(200, 10.0, 0.004))
    _seed(db_session, FLAT_SYMBOL, _bars(200, 10.0, 0.0))
    _seed(db_session, TINY_SYMBOL, _bars(40, 10.0, 0.004))
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_screener_service] = lambda: _FakeScreener(
        [_pick(UP_SYMBOL, 1, 45.0), _pick(FLAT_SYMBOL, 2, 44.0), _pick(TINY_SYMBOL, 3, 43.0)]
    )
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_screener_service, None)


def test_endpoint_ranks_by_replay_win_rate_and_reports_scope(client):
    resp = client.get(
        "/api/realtime/win-rate",
        params={"top_n": 5, "pool_size": 5, "hold_days": 5, "min_hits": 3, "lookback": 200},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 上涨序列必须排在平盘序列之前
    assert body["evaluated"] >= 2
    ranked = [row["symbol"] for row in body["top"]]
    assert ranked.index(UP_SYMBOL) < ranked.index(FLAT_SYMBOL)
    assert body["top"][0]["win_rate"] == 1.0
    flat = next(row for row in body["top"] if row["symbol"] == FLAT_SYMBOL)
    assert flat["win_rate"] == 0.0
    assert body["top"][0]["board_rank"] == 1

    # 样本不足的标的必须进 skipped，而不是给个数字凑数
    assert any(item["symbol"] == TINY_SYMBOL for item in body["skipped"])
    assert all(row["symbol"] != TINY_SYMBOL for row in body["top"])

    # 口径与免责声明必须随响应返回
    assert body["bars_adjust"] == "qfq"
    assert body["market_session"]["signal_day"] == "2026-09-15"
    assert body["market_session"]["live"] is True
    assert "不是未来上涨概率" in body["disclaimer"]
    assert "未扣交易费用" in body["disclaimer"]
    assert body["definitions"]["conditions_used"]
    assert body["min_samples"] == 5


def test_endpoint_respects_top_n_and_empty_pool(client):
    resp = client.get(
        "/api/realtime/win-rate", params={"top_n": 1, "pool_size": 5, "min_hits": 3}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["top"]) <= 1

    from app.main import app

    app.dependency_overrides[get_screener_service] = lambda: _FakeScreener([])
    empty_resp = client.get("/api/realtime/win-rate", params={"top_n": 5, "pool_size": 5})
    assert empty_resp.status_code == 200, empty_resp.text
    empty = empty_resp.json()
    assert empty["top"] == []
    assert empty["pool_size"] == 0
