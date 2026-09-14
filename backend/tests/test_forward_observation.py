"""前向模拟观察计时测试（P1-02）。

研发计划要求：「工程冻结后累计前向模拟交易日；重大成交逻辑修改后重新计时；
不得用历史重放代替前向观察。」这些用例锁定该语义：

* ``start`` 按 freeze_tag 幂等，重复登记不覆盖、不清零；
* ``count_day`` 只认严格晚于起点、不晚于今天、且为交易日的日期；
* 同一日期重复计数返回 counted=False（幂等）；
* 未来日期被拒绝；
* 未登记起点时状态如实返回 registered=False，不伪造天数；
* 达标语义：``days_met`` 只表示观察期够，不代表策略通过。

注意两个测试环境事实（踩过坑）：

1. ``db_session`` 是独立的内存引擎，与 ``app.database.session.engine`` **不是同一个库**；
   给服务层用例播种交易日历必须用 ``db_session`` 自己，否则服务会看到空日历。
2. 日期一律相对 ``today_cn()`` 构造 —— 用写死的未来日期会（并且确实曾经）
   被「未来日一律拒绝」的保护正确地打回。
"""
from __future__ import annotations

from datetime import date, time, timedelta

from sqlalchemy import insert

from app.database.models import ForwardObservation, TradingDate
from app.database.session import engine
from app.paper_trading.forward import (
    CURRENT_FREEZE_TAG,
    ForwardObservationService,
    today_cn,
)


def _seed_calendar(days: list[date], db=None) -> None:
    """把交易日写进指定引擎（默认 app 引擎；服务层用例请传 db_session）。"""
    target = db.get_bind() if db is not None else engine
    with target.begin() as conn:
        conn.execute(insert(TradingDate.__table__).values([{"trade_date": d} for d in days]))


# ───────────── 登记 ─────────────


def test_status_before_registration_is_honest(db_session):
    service = ForwardObservationService(db_session)
    status = service.status()
    assert status["registered"] is False
    assert "历史重放" in status["note"]
    assert "trading_days_counted" not in status


def test_start_is_idempotent_per_freeze_tag(db_session):
    service = ForwardObservationService(db_session)
    anchor = today_cn() - timedelta(days=10)
    first = service.start(started_on=anchor, target_trading_days=60, notes="初次冻结")
    assert first["created"] is True
    assert first["trading_days_counted"] == 0

    second = service.start(started_on=anchor, target_trading_days=60)
    assert second["created"] is False
    assert second["started_on"] == anchor.isoformat()
    assert db_session.query(ForwardObservation).count() == 1


def test_new_freeze_tag_starts_fresh_without_erasing_history(db_session):
    service = ForwardObservationService(db_session)
    anchor_a = today_cn() - timedelta(days=30)
    service.start(freeze_tag="freeze-a", started_on=anchor_a)
    service.count_day(anchor_a + timedelta(days=1), freeze_tag="freeze-a")
    service.start(freeze_tag="freeze-b", started_on=today_cn() - timedelta(days=1))

    a = service.status("freeze-a")
    b = service.status("freeze-b")
    assert a["trading_days_counted"] == 1
    assert b["trading_days_counted"] == 0
    assert db_session.query(ForwardObservation).count() == 2


# ───────────── 计数口径 ─────────────


def test_count_day_requires_a_registered_freeze(db_session):
    service = ForwardObservationService(db_session)
    result = service.count_day(today_cn())
    assert result["counted"] is False
    assert "尚未登记" in result["reason"]


def test_count_day_rejects_future_and_pre_anchor_dates(db_session):
    anchor = today_cn() - timedelta(days=5)
    _seed_calendar([anchor, anchor + timedelta(days=1)], db=db_session)
    service = ForwardObservationService(db_session)
    service.start(started_on=anchor)

    future = today_cn() + timedelta(days=5)
    future_result = service.count_day(future)
    assert future_result["counted"] is False
    assert "未来日" in future_result["reason"]

    assert service.count_day(anchor)["counted"] is False          # 等于起点
    assert service.count_day(anchor - timedelta(days=1))["counted"] is False  # 早于起点


def test_count_day_rejects_non_trading_day(db_session):
    anchor = today_cn() - timedelta(days=5)
    trading_day = today_cn() - timedelta(days=2)
    _seed_calendar([anchor, trading_day], db=db_session)
    service = ForwardObservationService(db_session)
    service.start(started_on=anchor)

    non_trading = today_cn() - timedelta(days=1)
    result = service.count_day(non_trading)
    assert result["counted"] is False
    assert "非交易日" in result["reason"]


# ───────────── 起点当日的语义（2026-09-14 的决策锚点） ─────────────
#
# 决策背景：2026-09-14 中午给模拟盘接入滑点与过户费（成交逻辑变更），按计划重新计时，
# 新标签 `2026-09-14-paper-costs-v2` 的起点登记为**当天**。由于规则是「严格晚于起点」，
# **当天永远不会被计入**，首个可计日顺延到下一个交易日 09-15。
#
# 用户已确认**保持这个保守口径**（从「第一个完整处在新逻辑下的交易日」起算），
# 因此把这两条语义固化成测试 —— 否则以后看到「起点当天没计入」会误判成 bug。


def test_anchor_set_today_can_never_count_today(db_session, monkeypatch):
    """起点 = 当天时，当天永远不计入，且原因是「不晚于起点」而非「未收盘」。

    把收盘时点 mock 成 00:00，排除「尚未收盘」这条更早的分支，
    确保我们测的确实是起点规则本身。
    """
    from app.paper_trading import forward as forward_module

    monkeypatch.setattr(forward_module, "date_close_time", lambda: time(0, 0))
    today = today_cn()
    _seed_calendar([today, today + timedelta(days=1)], db=db_session)

    service = ForwardObservationService(db_session)
    service.start(started_on=today)

    same_day = service.count_day(today)
    assert same_day["counted"] is False
    assert "不晚于起点" in same_day["reason"]

    # 下一个交易日仍是未来日 → 要等到那天才能真正计入
    tomorrow = service.count_day(today + timedelta(days=1))
    assert tomorrow["counted"] is False
    assert "未来日" in tomorrow["reason"]


def test_anchor_yesterday_counts_today_after_close(db_session, monkeypatch):
    """起点 = 昨天时，今天收盘后可计 1 天 —— 这就是 09-15 会发生的路径。"""
    from app.paper_trading import forward as forward_module

    monkeypatch.setattr(forward_module, "date_close_time", lambda: time(0, 0))
    today = today_cn()
    _seed_calendar([today - timedelta(days=1), today], db=db_session)

    service = ForwardObservationService(db_session)
    service.start(started_on=today - timedelta(days=1))

    result = service.count_day(today)
    assert result["counted"] is True
    assert result["trading_days_counted"] == 1
    assert result["last_counted_day"] == today.isoformat()

    # 同日重复 → 幂等
    assert service.count_day(today)["counted"] is False


def test_count_day_increments_once_and_is_idempotent(db_session):
    anchor = today_cn() - timedelta(days=5)
    day1 = today_cn() - timedelta(days=3)
    day2 = today_cn() - timedelta(days=2)
    _seed_calendar([anchor, day1, day2], db=db_session)
    service = ForwardObservationService(db_session)
    service.start(started_on=anchor, account_id=7)

    first = service.count_day(day1, equity=1_000_000.0)
    assert first["counted"] is True
    assert first["trading_days_counted"] == 1
    assert first["last_counted_day"] == day1.isoformat()
    assert first["baseline_equity"] == 1_000_000.0

    repeat = service.count_day(day1)
    assert repeat["counted"] is False
    assert "已计入" in repeat["reason"]
    assert service.status()["trading_days_counted"] == 1

    # 早于最后计数日（回补）也不会计数
    assert service.count_day(anchor)["counted"] is False

    second = service.count_day(day2, equity=999_000.0)
    assert second["counted"] is True
    assert second["trading_days_counted"] == 2
    assert second["current_equity"] == 999_000.0


def test_count_day_ignores_calendar_when_empty(db_session):
    """日历为空时不能把每一天都判成非交易日（否则前向计时永远不动）。"""
    service = ForwardObservationService(db_session)
    service.start(started_on=today_cn() - timedelta(days=5))
    result = service.count_day(today_cn() - timedelta(days=1))
    assert result["counted"] is True


def test_count_day_rejects_same_day_before_close(db_session):
    """未收盘的「今天」不能计入：否则每天开盘前就能把当天算作一个完整观察日。"""
    from app.paper_trading import forward as forward_module

    service = ForwardObservationService(db_session)
    service.start(started_on=today_cn() - timedelta(days=5))

    original = forward_module._CLOSE_TIME
    forward_module._CLOSE_TIME = time(23, 59)  # 模拟「此刻还没收盘」
    try:
        result = service.count_day(today_cn())
    finally:
        forward_module._CLOSE_TIME = original

    assert result["counted"] is False
    assert "尚未收盘" in result["reason"]


def test_target_progress_and_days_met_semantics(db_session):
    service = ForwardObservationService(db_session)
    service.start(started_on=today_cn() - timedelta(days=100), target_trading_days=2)
    row = service.get(CURRENT_FREEZE_TAG)
    assert row is not None
    row.trading_days_counted = 2
    db_session.commit()

    status = service.status()
    assert status["remaining_trading_days"] == 0
    assert status["days_met"] is True
    # 达标≠策略通过：必须带这句提醒
    assert "不代表策略通过" in status["caveat"]


def test_list_all_returns_history(db_session):
    service = ForwardObservationService(db_session)
    service.start(freeze_tag="f1", started_on=today_cn() - timedelta(days=30))
    service.start(freeze_tag="f2", started_on=today_cn() - timedelta(days=10))
    tags = {row.freeze_tag for row in service.list_all()}
    assert tags == {"f1", "f2"}


# ───────────── API 契约 ─────────────


def test_api_forward_observation_flow():
    """接口层用 app 引擎（TestClient 走它），日历也要播到 app 引擎。"""
    from fastapi.testclient import TestClient

    from app.main import app

    anchor = today_cn() - timedelta(days=5)
    trading_day = today_cn() - timedelta(days=2)
    _seed_calendar([anchor, trading_day])

    with TestClient(app) as client:
        # 起点登记前：如实说明未登记
        before = client.get("/api/paper/forward-observation").json()
        assert before["current"]["registered"] is False
        assert before["current_freeze_tag"] == CURRENT_FREEZE_TAG

        start = client.post(
            "/api/paper/forward-observation/start",
            json={
                "freeze_tag": "api-freeze",
                "started_on": anchor.isoformat(),
                "target_trading_days": 60,
            },
        )
        assert start.status_code == 201, start.text
        assert start.json()["created"] is True

        # 幂等
        again = client.post(
            "/api/paper/forward-observation/start",
            json={"freeze_tag": "api-freeze", "started_on": anchor.isoformat()},
        )
        assert again.json()["created"] is False

        # 未在日历里的日期不计数
        non_trading = (today_cn() - timedelta(days=1)).isoformat()
        weekend = client.post(
            f"/api/paper/forward-observation/count?trading_date={non_trading}&freeze_tag=api-freeze"
        )
        assert weekend.status_code == 200, weekend.text
        assert weekend.json()["counted"] is False

        # 交易日计数
        ok = client.post(
            f"/api/paper/forward-observation/count?trading_date={trading_day.isoformat()}&freeze_tag=api-freeze"
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["counted"] is True
        assert ok.json()["trading_days_counted"] == 1

        # 未来日期 → 422（不允许提前累计）
        future = (today_cn() + timedelta(days=10)).isoformat()
        rejected = client.post(
            f"/api/paper/forward-observation/count?trading_date={future}&freeze_tag=api-freeze"
        )
        assert rejected.status_code == 422

        # 非法日期 → 422
        assert client.post(
            "/api/paper/forward-observation/count?trading_date=2026-13-45"
        ).status_code == 422

        listed = client.get("/api/paper/forward-observation").json()
        assert any(item["freeze_tag"] == "api-freeze" for item in listed["all"])
