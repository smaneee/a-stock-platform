"""`POST /api/paper/accounts/{id}/settle` 的日期护栏回归测试。

**为什么补这组测试**：`app/api/paper_accounts.py` 里已有两道守卫 ——
未来日期 → **422**、非交易日 → **400** —— 但**此前没有任何测试覆盖它们**
（仓库里 `settle_account` 的用例都在服务层，测的是幂等与 T+1 解冻语义）。
守卫没有回归保护，等于「现在对、以后不一定」。

研发计划 §14.11 把「`/settle` 接受任意日历内交易日（含未来日）」登记为缺口；
2026-09-14 复核确认**代码侧已修**，本轮把这些守卫固化成测试，
并把它同时作为「先解冻 T+1 批次」的防线 —— 因为提前结算正是绕过 T+1 的路径。

服务层的 T+1 语义（结算日 = 建仓日不解冻）已由
`tests/test_paper_state_machine.py::test_settlement_unfreezes_only_prior_days` 覆盖，
本文件不重复，只补**接口层护栏**。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.database.models import TradingDate
from app.database.session import engine
from app.paper_trading.forward import today_cn


def _seed_calendar(days: list[date]) -> None:
    """把交易日写进 **app 引擎**（TestClient 走的就是它，不能用 db_session）。

    app 引擎在 lifespan 里可能已经播过日历，直接 INSERT 会撞
    `UNIQUE constraint failed: trading_calendar.trade_date`（实测踩到），
    因此先查出已存在的日期再补缺。
    """
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("SELECT trade_date FROM trading_calendar").fetchall()
        existing = {str(row[0])[:10] for row in rows}
        missing = [d for d in days if d.isoformat() not in existing]
        if missing:
            conn.execute(
                insert(TradingDate.__table__).values([{"trade_date": d} for d in missing])
            )


@pytest.fixture
def paper_client():
    """建一个空仓账户：无持仓 → settle 不会去打行情源，护栏可独立测试。"""
    from app.main import app

    with TestClient(app) as client:
        created = client.post("/api/paper/accounts", json={"name": "结算护栏", "initial_cash": 100000})
        assert created.status_code in (200, 201), created.text
        account_id = created.json()["id"]
        yield client, account_id


def test_settle_rejects_future_date(paper_client):
    """未来日期必须 422 —— 否则可以提前解冻 T+1 批次。"""
    client, account_id = paper_client
    future = (today_cn() + timedelta(days=5)).isoformat()
    resp = client.post(f"/api/paper/accounts/{account_id}/settle?trading_date={future}")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "晚于今天" in detail
    assert "结算只能发生在已经过去的交易日" in detail


def test_settle_rejects_malformed_date(paper_client):
    """非法日期格式必须 422，而不是 500 或静默按今天处理。

    **实测发现（记录在案）**：`20260914` **不会**被拒 —— Python 的
    `date.fromisoformat()` 自 3.11 起接受 ISO 8601 基本格式（`YYYYMMDD`），
    因此它不是「非法日期」，而是「合法但非文档格式」。接口文档写的是
    `YYYY-MM-DD`，是否要收紧成严格格式属接口风格取舍，本轮只如实记录、不擅自改。
    """
    client, account_id = paper_client
    for bad in ("2026-13-45", "not-a-date", "2026/09/14"):
        resp = client.post(f"/api/paper/accounts/{account_id}/settle?trading_date={bad}")
        assert resp.status_code == 422, f"{bad} -> {resp.status_code}"

    # 基本格式被接受（前提是该日不晚于今天且是交易日 → 否则会是 422/400，
    # 但不该是 500）。这里只断言「不是 500」。
    compact = (today_cn() - timedelta(days=1)).strftime("%Y%m%d")
    resp = client.post(f"/api/paper/accounts/{account_id}/settle?trading_date={compact}")
    assert resp.status_code != 500, resp.text


def test_settle_rejects_non_trading_day(paper_client):
    """日历里没有的日期必须 400（不是静默结算）。

    需要一个**确定不在日历里**的过去日期：不能写死（第一次写成「今天−3 天」，
    结果那天恰好是真实交易日，接口正确返回 200，测试反而错了）。
    因此这里先读日历，再往前找一个不在其中的日期；同时确保日历非空，
    以证明拒绝的是「这个日期」而不是「日历为空」。
    """
    client, account_id = paper_client
    known_day = today_cn() - timedelta(days=10)
    _seed_calendar([known_day])

    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT trade_date FROM trading_calendar").fetchall()
    known = {str(row[0])[:10] for row in rows}

    candidate = today_cn() - timedelta(days=1)
    while candidate.isoformat() in known:
        candidate -= timedelta(days=1)
    assert candidate.isoformat() not in known, "构造不出不在日历里的过去日期"

    resp = client.post(
        f"/api/paper/accounts/{account_id}/settle?trading_date={candidate.isoformat()}"
    )
    assert resp.status_code == 400, f"{candidate} -> {resp.status_code} {resp.text}"
    assert "非交易日" in resp.json()["detail"]


def test_settle_accepts_past_trading_day_and_is_idempotent(paper_client):
    """已过去的交易日可结算，且同一 (账户, 日期) 重复调用返回首次结果。"""
    client, account_id = paper_client
    trading_day = today_cn() - timedelta(days=1)
    _seed_calendar([trading_day])

    first = client.post(
        f"/api/paper/accounts/{account_id}/settle?trading_date={trading_day.isoformat()}"
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["trading_date"] == trading_day.isoformat()
    assert body["account_id"] == account_id
    assert body["idempotent"] is False

    again = client.post(
        f"/api/paper/accounts/{account_id}/settle?trading_date={trading_day.isoformat()}"
    )
    assert again.status_code == 200
    repeat = again.json()
    assert repeat["idempotent"] is True
    # 幂等路径返回同一账户/同一日期与同一资产快照（返回体没有 `id` 字段，
    # 幂等的判据是这三者 + idempotent 标志，不是记录 id）
    assert repeat["account_id"] == body["account_id"]
    assert repeat["trading_date"] == body["trading_date"]
    assert repeat["total_asset"] == body["total_asset"]


def test_settle_rejects_unknown_account():
    """不存在的账户 404（护栏不应把 404 变成 422 或 500）。"""
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/paper/accounts/99999999/settle?trading_date=2026-01-05")
        assert resp.status_code == 404
