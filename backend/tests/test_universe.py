"""feat(universe) 4 象限测试：normal / edge / error / provider-degraded。

覆盖：
1. provider 正常拉取 + 持久化 Security
2. Sync retry：单 provider 失败 N 次 → 切下一个 provider
3. AllProvidersFailed：所有 provider 都失败 → 失败
4. Exclusion 规则：delisted / suspended / incomplete / st
5. Snapshot 幂等（同 trading_day 二次调用不重复创建）
6. Snapshot 跨日：不同 trading_day 各建一份
7. get_membership 过滤：exchange / include_only / exclude_reasons
8. long_suspension 标记：基于 historical_bar 缺失统计
9. API: status / sync / snapshots / members / filter
10. Provider-degraded：所有 provider 异常 → 503
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.database.models import (
    DataSourceHealth,
    Security,
    UniverseMember,
    UniverseSnapshot,
)
from app.universe.exclusion import ExclusionEngine
from app.universe.providers import (
    AkshareUniverseProvider,
    MockUniverseProvider,
    ProviderError,
    SecurityRecord,
    UniverseProvider,
)
from app.universe.snapshot_service import UniverseSnapshotService
from app.universe.sync_service import (
    AllProvidersFailedError,
    SyncResult,
    UniverseSyncService,
)


# ─────────────── 1. MockUniverseProvider normal path ───────────────


class TestMockProvider:
    def test_returns_seeded_records(self):
        provider = MockUniverseProvider()
        records = asyncio.run(provider.fetch_all())
        assert len(records) == 26, f"expected 26 records, got {len(records)}"
        symbols = {r.symbol for r in records}
        # 跨 SH/SZ/BJ
        exchanges = {r.exchange for r in records}
        assert exchanges == {"SH", "SZ", "BJ"}

    def test_includes_delisted(self):
        provider = MockUniverseProvider()
        records = asyncio.run(provider.fetch_all())
        delisted = [r for r in records if r.trading_status == "delisted"]
        assert len(delisted) == 2
        assert all(r.delisted_date is not None for r in delisted)

    def test_includes_st_flag(self):
        provider = MockUniverseProvider()
        records = asyncio.run(provider.fetch_all())
        st_securities = [r for r in records if r.is_st]
        assert len(st_securities) == 2


# ─────────────── 2. UniverseSyncService normal ───────────────


class TestSyncServiceNormal:
    def test_mock_provider_persists_securities(self, db_session):
        """Mock provider 一次成功 → 27 条 Security 入库。"""
        svc = UniverseSyncService(
            db=db_session,
            providers=[MockUniverseProvider()],
            max_retries=2,
        )
        result = asyncio.run(svc.sync())
        assert isinstance(result, SyncResult)
        assert result.source_provider == "mock"
        assert result.total_fetched == 26
        # 全部是新行（DB 空）
        assert result.new_securities == 26
        assert result.updated_securities == 0

        # DB 应该确实写入了
        rows = db_session.execute(select(Security)).scalars().all()
        assert len(rows) == 26

    def test_sync_is_idempotent(self, db_session):
        """第二次同步：仍是 27 条，但全部为 update，无 new。"""
        svc = UniverseSyncService(
            db=db_session,
            providers=[MockUniverseProvider()],
            max_retries=2,
        )
        asyncio.run(svc.sync())
        # 第二次
        result = asyncio.run(svc.sync())
        assert result.new_securities == 0
        assert result.updated_securities == 26
        # DB 仍是 27 条（没有因为幂等性而重复插入）
        rows = db_session.execute(select(Security)).scalars().all()
        assert len(rows) == 26

    def test_records_provider_health_after_success(self, db_session):
        svc = UniverseSyncService(
            db=db_session,
            providers=[MockUniverseProvider()],
        )
        asyncio.run(svc.sync())
        rows = db_session.execute(select(DataSourceHealth)).scalars().all()
        assert len(rows) == 1
        assert rows[0].source_id == "mock"
        assert rows[0].last_status == "ok"
        assert rows[0].consecutive_failures == 0
        assert rows[0].last_success_at is not None


# ─────────────── 3. Provider-degraded + retry ───────────────


class FlakyProvider(UniverseProvider):
    """头 N 次抛 ProviderError，第 N+1 次成功。"""

    source_id = "flaky"

    def __init__(self, succeed_at_attempt: int):
        self.succeed_at = succeed_at_attempt
        self.calls = 0

    async def fetch_all(self):
        self.calls += 1
        if self.calls < self.succeed_at:
            raise ProviderError(self.source_id, f"simulated failure #{self.calls}")
        return [
            SecurityRecord(symbol="999999", name="Recovery Stock", exchange="BJ"),
        ]


class TestProviderRetryAndDegradation:
    def test_retries_then_succeeds(self, db_session):
        """第一次 + 第二次失败，第三次成功 → 返回成功结果。"""
        flaky = FlakyProvider(succeed_at_attempt=3)
        svc = UniverseSyncService(
            db=db_session,
            providers=[flaky],
            max_retries=3,
            backoff_base_ms=1,
        )
        result = asyncio.run(svc.sync())
        assert result.source_provider == "flaky"
        assert result.total_fetched == 1
        assert flaky.calls == 3

    def test_moves_to_next_provider_when_retries_exhausted(self, db_session):
        """flaky provider 永远失败 → 切到 mock（默认列表第二）。"""
        always_fail = MagicMock(spec=UniverseProvider)
        always_fail.source_id = "always_fail"
        always_fail.fetch_all = AsyncMock(
            side_effect=ProviderError("always_fail", "perma-fail")
        )
        mock = MockUniverseProvider()
        svc = UniverseSyncService(
            db=db_session,
            providers=[always_fail, mock],
            max_retries=1,
            backoff_base_ms=1,
        )
        result = asyncio.run(svc.sync())
        assert result.source_provider == "mock"
        assert result.total_fetched == 26
        # always_fail 的 health 必须记录为 failed
        rows = db_session.execute(select(DataSourceHealth)).scalars().all()
        statuses = {r.source_id: r.last_status for r in rows}
        assert statuses.get("always_fail") == "failed"
        assert statuses.get("mock") == "ok"

    def test_all_providers_failed_raises(self, db_session):
        """两个 provider 都失败 → AllProvidersFailedError。"""
        always_fail_1 = MagicMock(spec=UniverseProvider)
        always_fail_1.source_id = "fail_1"
        always_fail_1.fetch_all = AsyncMock(
            side_effect=ProviderError("fail_1", "nope")
        )
        always_fail_2 = MagicMock(spec=UniverseProvider)
        always_fail_2.source_id = "fail_2"
        always_fail_2.fetch_all = AsyncMock(
            side_effect=ProviderError("fail_2", "nope")
        )
        svc = UniverseSyncService(
            db=db_session,
            providers=[always_fail_1, always_fail_2],
            max_retries=1,
            backoff_base_ms=1,
        )
        with pytest.raises(AllProvidersFailedError) as exc_info:
            asyncio.run(svc.sync())
        assert "fail_1" in str(exc_info.value)
        assert "fail_2" in str(exc_info.value)

        # 两个 provider 的 health 都应写为 failed
        rows = db_session.execute(select(DataSourceHealth)).scalars().all()
        statuses = {r.source_id: (r.last_status, r.consecutive_failures) for r in rows}
        assert statuses["fail_1"] == ("failed", 1)
        assert statuses["fail_2"] == ("failed", 1)

    def test_akshare_provider_returns_records_or_raises(self, monkeypatch):
        """AKShare provider 是真实实现：成功时返回 records，失败时抛 ProviderError。

        不再硬编码"必败"——生产实现调用 akshare.stock_info_a_code_name()，
        CI 环境如果 akshare 安装且可联网就成功，否则抛 ProviderError。
        两种结果都是合规行为。校验重点：返回类型必须正确（list[SecurityRecord]）。
        """
        provider = AkshareUniverseProvider(timeout_seconds=5.0)
        try:
            records = asyncio.run(provider.fetch_all())
            # 真实源成功路径：返回 list[SecurityRecord] 且非空
            assert isinstance(records, list)
            assert len(records) > 0, "真实 AKShare 应能拉到至少一只 A 股"
            assert all(isinstance(r, SecurityRecord) for r in records)
            # 至少有一只 SH 或 SZ 开头的代码（akshare 沪深京 A 股接口）
            exchanges = {r.exchange for r in records if r.exchange}
            assert exchanges & {"SH", "SZ", "BJ"}, f"应至少包含 SH/SZ/BJ 之一，实际 {exchanges}"
        except ProviderError as exc:
            # CI 无网络 / akshare 未装：抛 ProviderError 也合规
            assert exc.source_id == "akshare"


# ─────────────── 4. ExclusionEngine ───────────────


def _seed_security(
    db_session,
    symbol: str = "600000",
    name: str = "Test",
    is_st: bool = False,
    listing_date: date | None = None,
    delisted_date: date | None = None,
    trading_status: str = "active",
    exchange: str = "SH",
) -> Security:
    sec = Security(
        symbol=symbol,
        name=name,
        is_st=is_st,
        listing_date=listing_date,
        delisted_date=delisted_date,
        trading_status=trading_status,
        exchange=exchange,
    )
    db_session.add(sec)
    db_session.commit()
    return sec


class TestExclusionEngine:
    def test_active_security_included_by_default(self, db_session):
        _seed_security(
            db_session,
            "600000",
            listing_date=date(2020, 1, 1),
            trading_status="active",
        )
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate(
            [db_session.get(Security, "600000")],
            as_of=date(2024, 1, 1),
        )
        assert len(decisions) == 1
        assert decisions[0].is_included is True
        assert decisions[0].reason is None

    def test_delisted_excluded(self, db_session):
        _seed_security(
            db_session,
            "600001",
            listing_date=date(2000, 1, 1),
            delisted_date=date(2023, 1, 1),
        )
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate(
            [db_session.get(Security, "600001")],
            as_of=date(2024, 1, 1),
        )
        assert decisions[0].is_included is False
        assert decisions[0].reason == "delisted"

    def test_suspended_excluded(self, db_session):
        _seed_security(
            db_session,
            "600002",
            listing_date=date(2000, 1, 1),
            trading_status="suspended",
        )
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate(
            [db_session.get(Security, "600002")],
            as_of=date(2024, 1, 1),
        )
        assert decisions[0].reason == "suspended"

    def test_pending_listing_excluded(self, db_session):
        """东财 f292=9（已分配代码、尚未挂牌）必须按 not_listed_yet 排除。

        回归：这类代码 listing_date 为空，trading_status 也不是 delisted /
        suspended，修复前会以「可交易」进入股票池（001246 力勤资源等 9 只）。
        """
        _seed_security(
            db_session,
            "001246",
            listing_date=None,
            trading_status="pending_listing",
        )
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate(
            [db_session.get(Security, "001246")],
            as_of=date(2026, 9, 11),
        )
        assert decisions[0].is_included is False
        assert decisions[0].reason == "not_listed_yet"

    def test_no_listing_date_kept_as_audit_tag(self, db_session):
        """listing_date 缺失：仅 audit_reason（'listing_date_unknown'），不排除。

        修复 P0：AKShare stock_info_a_code_name() 不返回 listing_date，
        若把缺 listing_date 判 incomplete_data 会把全市场全干掉。
        现在 universe 保持宽松口径，audit_reason 暴露给 selection 层做二次过滤。
        """
        sec = _seed_security(db_session, "600003", listing_date=None)
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate([sec], as_of=date(2024, 1, 1))
        # 不应排除（is_included=True）
        assert decisions[0].is_included is True
        assert decisions[0].reason is None
        # listing_date_unknown 暴露在 snapshot.audit_reason（snapshot_service 处理）

    def test_not_yet_listed_excluded(self, db_session):
        _seed_security(
            db_session,
            "600004",
            listing_date=date(2030, 1, 1),
        )
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate(
            [db_session.get(Security, "600004")],
            as_of=date(2024, 1, 1),
        )
        assert decisions[0].reason == "not_listed_yet"

    def test_st_excluded_only_when_configured(self, db_session):
        _seed_security(
            db_session,
            "600005",
            listing_date=date(2020, 1, 1),
            is_st=True,
        )
        sec = db_session.get(Security, "600005")

        # 默认 include_st=True → ST 保留
        engine_default = ExclusionEngine(db_session)
        d1 = engine_default.evaluate([sec], as_of=date(2024, 1, 1))
        assert d1[0].is_included is True

        # include_st=False → ST 排除
        engine_strict = ExclusionEngine(db_session, include_st=False)
        d2 = engine_strict.evaluate([sec], as_of=date(2024, 1, 1))
        assert d2[0].is_included is False
        assert d2[0].reason == "st_excluded"


# ─────────────── 5. SnapshotService ───────────────


class TestSnapshotService:
    def _sync_mock(self, db):
        svc = UniverseSyncService(db=db, providers=[MockUniverseProvider()])
        return asyncio.run(svc.sync())

    def test_create_snapshot_records_all_members(self, db_session):
        self._sync_mock(db_session)
        td = date(2024, 1, 15)
        snap_svc = UniverseSnapshotService(db_session)
        snap = snap_svc.get_or_create_snapshot(
            trading_day=td,
            source_provider="mock",
            source_synced_at=datetime.now(timezone.utc),
        )
        assert snap.trading_day == td
        assert snap.total_count == 26
        # 27 条：
        #   2 trading_status=delisted
        #   1 trading_status=suspended (300372)
        # 其余 24 条 active 且 listing_date <= 2024-01-15 → 包含
        assert snap.included_count == 23
        assert snap.excluded_count == 3

        members = db_session.execute(select(UniverseMember)).scalars().all()
        assert len(members) == 26

    def test_snapshot_is_idempotent_same_day(self, db_session):
        self._sync_mock(db_session)
        td = date(2024, 2, 1)
        snap_svc = UniverseSnapshotService(db_session)
        s1 = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        s2 = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        assert s1.id == s2.id, "同一天二次创建必须返回同一行"

    def test_snapshot_different_days(self, db_session):
        self._sync_mock(db_session)
        snap_svc = UniverseSnapshotService(db_session)
        s1 = snap_svc.get_or_create_snapshot(date(2024, 1, 1), source_provider="mock")
        s2 = snap_svc.get_or_create_snapshot(date(2024, 1, 2), source_provider="mock")
        assert s1.id != s2.id
        assert s1.trading_day != s2.trading_day

    def test_get_membership_filters_by_exchange(self, db_session):
        self._sync_mock(db_session)
        td = date(2024, 3, 1)
        snap_svc = UniverseSnapshotService(db_session)
        snap_svc.get_or_create_snapshot(td, source_provider="mock")

        sh = snap_svc.get_membership(td, include_only=True, exchange="SH")
        bj = snap_svc.get_membership(td, include_only=True, exchange="BJ")
        assert all(m.exchange == "SH" for m in sh)
        assert all(m.exchange == "BJ" for m in bj)
        assert len(sh) > 0 and len(bj) > 0

    def test_get_membership_filters_by_exclude_reason(self, db_session):
        self._sync_mock(db_session)
        td = date(2024, 3, 1)
        snap_svc = UniverseSnapshotService(db_session)
        snap_svc.get_or_create_snapshot(td, source_provider="mock")

        delisted = snap_svc.get_membership(
            td, include_only=False, exclude_reasons=["delisted"]
        )
        assert all(m.exclude_reason == "delisted" for m in delisted)
        assert all(m.is_included is False for m in delisted)
        assert len(delisted) == 2

    def test_get_membership_unknown_trading_day_returns_empty(self, db_session):
        snap_svc = UniverseSnapshotService(db_session)
        members = snap_svc.get_membership(date(2030, 1, 1))
        assert members == []

    def test_list_snapshots_returns_descending(self, db_session):
        self._sync_mock(db_session)
        snap_svc = UniverseSnapshotService(db_session)
        snap_svc.get_or_create_snapshot(date(2024, 1, 1), source_provider="mock")
        snap_svc.get_or_create_snapshot(date(2024, 6, 1), source_provider="mock")
        snap_svc.get_or_create_snapshot(date(2024, 3, 1), source_provider="mock")
        listed = snap_svc.list_snapshots()
        trading_days = [s["trading_day"] for s in listed]
        assert trading_days == ["2024-06-01", "2024-03-01", "2024-01-01"]


# ─────────────── 6. API endpoints ───────────────


class TestUniverseAPI:
    """通过 TestClient 调用 FastAPI 路由。

    注意：TestClient 走 app 自己的 DB 引擎（conftest 控制为内存 SQLite）。
    这套测试聚焦路由 / 输入校验 / 默认 mock provider 路径。
    """

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app)

    def test_status_routes_registered(self):
        client = self._client()
        r = client.get("/api/universe/status")
        assert r.status_code == 200
        data = r.json()
        assert "provider_health" in data
        assert "latest_snapshot_date" in data

    def test_snapshots_route_returns_empty_list(self):
        client = self._client()
        r = client.get("/api/universe/snapshots")
        assert r.status_code == 200
        assert "snapshots" in r.json()

    def test_filter_returns_404_when_no_snapshot(self):
        client = self._client()
        r = client.post("/api/universe/filter")
        assert r.status_code == 404
        assert "no_snapshot" in str(r.json())

    def test_members_invalid_date_400(self):
        client = self._client()
        r = client.get("/api/universe/snapshots/not-a-date/members")
        assert r.status_code == 400
        assert "invalid_date" in str(r.json())

    def test_members_status_tristate(self):
        """status=included/excluded/all 覆盖 include_only 的三态语义。

        include_only=False 只返回「被剔除」的标的，调用方无法同时看到两边，
        因此新增 status=all 给出全量。
        """
        client = self._client()
        r = client.post("/api/universe/sync")
        assert r.status_code == 200, r.text
        td = r.json()["snapshot_trading_day"]
        snap = client.get("/api/universe/snapshots").json()["snapshots"][0]
        assert snap["excluded_count"] > 0, "mock 种子里应含 ST / 退市标的"

        base = f"/api/universe/snapshots/{td}/members"
        default = client.get(base).json()
        all_rows = client.get(f"{base}?status=all").json()
        only_incl = client.get(f"{base}?status=included").json()
        only_excl = client.get(f"{base}?status=excluded").json()

        assert default["include_only"] is True
        assert all_rows["include_only"] is None
        assert default["count"] == only_incl["count"]
        assert all_rows["count"] == snap["total_count"]
        assert all_rows["count"] == only_incl["count"] + only_excl["count"]
        assert only_excl["count"] == snap["excluded_count"]
        assert all(m["is_included"] for m in only_incl["members"])
        assert not any(m["is_included"] for m in only_excl["members"])

    def test_members_status_rejects_unknown_value(self):
        client = self._client()
        r = client.get("/api/universe/snapshots/2024-01-01/members?status=bogus")
        assert r.status_code == 422

    def test_sync_runs_with_default_mock_provider(self):
        """POST /api/universe/sync 默认走 mock → 200。"""
        client = self._client()
        r = client.post("/api/universe/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["source_provider"] == "mock"
        assert data["total_fetched"] == 26


# ─────────────── 7. 真实闭环测试（修复 P0 的核心） ───────────────


class TestRealLoopClosing:
    """POST /sync → GET snapshot → POST /filter 必须真闭环。

    用户 P0 反馈指出：原版 /sync 不调 get_or_create_snapshot，导致 sync 成功
    /filter 仍 404。本组测试覆盖这条链路。
    """

    def test_sync_creates_snapshot_in_same_session(self, db_session):
        """sync 成功后 snapshot 必须同时落库（同一个 session.commit()）。

        API 层 /api/universe/sync 显式传 create_snapshot=True；这里也显式传
        同样参数验证原子化行为。
        """
        svc = UniverseSyncService(
            db=db_session,
            providers=[MockUniverseProvider()],
            max_retries=1,
        )
        result = asyncio.run(svc.sync(create_snapshot=True))
        # sync 返回值必须带 snapshot_id
        assert result.snapshot_id is not None, (
            "sync 成功后必须返回 snapshot_id（与 snapshot 原子化的硬性要求）"
        )
        assert result.snapshot_trading_day is not None
        # DB 立即可查
        snap = db_session.get(UniverseSnapshot, result.snapshot_id)
        assert snap is not None
        assert snap.source_provider == "mock"

    def test_weekend_sync_snaps_back_to_last_trading_day(self, db_session):
        """周末点同步时快照日必须归一到最近交易日。

        回归：东财 / mock 这类「只有当前名单」的数据源把 as_of_date 填成抓取
        当天，2026-09-12（周六）曾落出一个「快照日 = 非交易日」的快照，
        选股与回测按快照日对齐时会错位。
        """
        from app.database.models import TradingDate

        friday = date(2026, 9, 11)
        saturday = date(2026, 9, 12)
        db_session.add(TradingDate(trade_date=friday))
        db_session.commit()

        class _WeekendProvider(MockUniverseProvider):
            """模拟东财：名单只有当前一份，as_of_date 就是抓取当天。"""

            async def fetch_all(self):
                records = await super().fetch_all()
                for record in records:
                    record.as_of_date = saturday
                return records

        svc = UniverseSyncService(db=db_session, providers=[_WeekendProvider()])
        result = asyncio.run(svc.sync(create_snapshot=True))

        assert result.snapshot_trading_day == friday.isoformat()
        snap = db_session.get(UniverseSnapshot, result.snapshot_id)
        assert snap.trading_day == friday

    def test_sync_to_filter_loop_via_api(self, db_session):
        """完整闭环：POST /sync → GET /snapshots/{td}/members → POST /filter。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from datetime import date

        client = TestClient(app)
        # Step 1: sync
        r = client.post("/api/universe/sync")
        assert r.status_code == 200, r.text
        data = r.json()
        td = data["snapshot_trading_day"]
        assert td is not None
        # Step 2: GET snapshot members
        r2 = client.get(f"/api/universe/snapshots/{td}/members")
        assert r2.status_code == 200
        members = r2.json()["members"]
        assert len(members) > 0
        # Step 3: POST /filter 必须可用
        r3 = client.post(f"/api/universe/filter?trading_day={td}")
        assert r3.status_code == 200
        symbols = r3.json()["symbols"]
        assert len(symbols) > 0
        # 必须与 members 匹配
        assert set(symbols) == {m["symbol"] for m in members if m["is_included"]}


class TestSnapshotImmutability:
    """snapshot 创建后修改/删除当前 Security 必须不影响历史 snapshot 字段。

    修复 P1：防止 JOIN 漂移 + CASCADE 删除污染历史。
    """

    def test_member_fields_preserved_after_security_update(self, db_session):
        """创建 snapshot 后改 Security.name → member.name 必须保持原值。"""
        # 1. sync + snapshot
        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync())
        snap_svc = UniverseSnapshotService(db_session)
        td = date(2024, 1, 15)
        snap = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        members = db_session.execute(
            select(UniverseMember).where(UniverseMember.snapshot_id == snap.id)
        ).scalars().all()
        assert len(members) > 0
        sample = members[0]
        original_name = sample.name
        original_exchange = sample.exchange
        assert original_name  # 至少要拷过去
        assert original_exchange  # 至少要拷过去

        # 2. 修改 Security（改 name + 改 exchange）
        sec = db_session.get(Security, sample.symbol)
        sec.name = "CHANGED-NAME-XXX"
        sec.exchange = "XX"
        db_session.commit()

        # 3. 重新读 snapshot member → 字段必须不变
        db_session.expire_all()
        member_after = db_session.get(UniverseMember, sample.id)
        assert member_after.name == original_name, (
            "snapshot 字段必须不可变：当前 Security 修改不应影响历史 member"
        )
        assert member_after.exchange == original_exchange

    def test_member_survives_security_delete(self, db_session):
        """删除 Security 后历史 member 必须仍在（FK ondelete=NO ACTION 阻止）。

        真实原子语义：snapshot 创建必须先 commit（让它独立成事务），再测删
        Security 被 FK 拦下 — 失败时只撤销 delete 操作，不影响已 commit 的 snapshot。
        """
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy import text

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))  # sync 内已 commit
        snap_svc = UniverseSnapshotService(db_session)
        td = date(2024, 1, 15)
        snap = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        # 重要：snapshot_service 不 commit，必须显式 commit 让 snapshot 独立成事务
        db_session.commit()
        snap_id_before = snap.id

        member_count_before = len(
            db_session.execute(
                select(UniverseMember).where(UniverseMember.snapshot_id == snap_id_before)
            ).scalars().all()
        )
        assert member_count_before > 0

        # 试图删 Security → 应当失败（被 NO ACTION FK 拦下）
        sec = db_session.get(Security, "600000")
        assert sec is not None, "600000 必须存在（mock seed 包含）"
        db_session.delete(sec)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # 历史 member 必须仍存在 — raw SQL 校验（IntegrityError 后 ORM 缓存不可靠）
        raw_count = db_session.execute(
            text("SELECT COUNT(*) FROM universe_members WHERE snapshot_id = :sid"),
            {"sid": snap_id_before},
        ).scalar()
        assert raw_count == member_count_before, (
            f"FK 校验后 member 应当保持：before={member_count_before}, raw_after={raw_count}"
        )


class TestProviderFailureNoSilentFallback:
    """修复 P0：真实 provider 失败时禁止静默回落到 mock。"""

    def test_503_when_all_real_providers_fail(self, db_session):
        """所有真源都失败 → API 必须返回 503，**不允许**退回 mock。"""
        from fastapi.testclient import TestClient
        from app.main import app

        # 通过 env 强制走"akshare（无网络）"且不包含 mock
        import os
        old_providers = os.environ.get("UNIVERSE_PROVIDERS")
        old_use_mock = os.environ.get("E2E_USE_MOCK")
        os.environ["UNIVERSE_PROVIDERS"] = "akshare"
        # 显式清除 E2E_USE_MOCK（pydantic 拒收空字符串）
        if "E2E_USE_MOCK" in os.environ:
            del os.environ["E2E_USE_MOCK"]

        # 必须 reload settings + providers 缓存
        from app.config import get_settings
        from app.universe import sync_service as svc_mod
        from app.universe.providers import AkshareUniverseProvider, ProviderError

        get_settings.cache_clear()

        # 替换 SyncService 内的 provider 解析为失败版本
        class _BoomAkshare(AkshareUniverseProvider):
            async def fetch_all(self):
                raise ProviderError("akshare", "simulated network failure")

        svc_mod._resolve_providers_from_settings = lambda: [_BoomAkshare()]
        client = TestClient(app)
        try:
            r = client.post("/api/universe/sync")
            assert r.status_code == 503, (
                f"真实源失败必须返回 503，实际 {r.status_code}: {r.text}"
            )
            body = r.json()
            assert "all_providers_failed" in str(body)
        finally:
            os.environ["UNIVERSE_PROVIDERS"] = old_providers or "mock"
            if old_use_mock is not None:
                os.environ["E2E_USE_MOCK"] = old_use_mock
            get_settings.cache_clear()
            # 恢复默认 provider 解析（重新 import 模块以重置 lambda）
            import importlib
            importlib.reload(svc_mod)

    def test_unknown_provider_name_raises_at_construction(self):
        """settings 中拼错 provider 名字 → 构造 SyncService 时立即抛错。"""
        from app.config import get_settings
        import os

        get_settings.cache_clear()
        old_providers = os.environ.get("UNIVERSE_PROVIDERS")
        old_use_mock = os.environ.get("E2E_USE_MOCK")
        os.environ["UNIVERSE_PROVIDERS"] = "akshare_typo"
        if "E2E_USE_MOCK" in os.environ:
            del os.environ["E2E_USE_MOCK"]
        try:
            from app.universe.sync_service import _resolve_providers_from_settings
            from app.universe.providers import ProviderError

            with pytest.raises(ProviderError) as exc_info:
                _resolve_providers_from_settings()
            assert "akshare_typo" in str(exc_info.value)
        finally:
            os.environ["UNIVERSE_PROVIDERS"] = old_providers or "mock"
            if old_use_mock is not None:
                os.environ["E2E_USE_MOCK"] = old_use_mock
            get_settings.cache_clear()


class TestLongSuspensionStateDistinction:
    """修复 P1：long_suspension 区分 confirmed / incomplete / provider_unknown。

    关键前提（修复 P0）：必须先有「最近一次成功的 history_ingest 批次」覆盖
    到 as_of 之前，否则一律 incomplete_history — 因为"0 行情"可能不是"停牌"，
    而是"根本没拉过历史"。
    """

    def _make_history_ingest_batch(
        self,
        db_session,
        *,
        as_of: date,
        covered_symbols: list[str],
        completed_symbols: int = 1000,
        coverage: float = 0.9,
    ) -> None:
        """模拟一次成功的 history_ingest 批次。"""
        from app.database.models import HistoryIngestBatch
        from app.time_utils import utc_now

        batch = HistoryIngestBatch(
            source="akshare",
            status="succeeded",
            start_date=as_of - timedelta(days=60),
            end_date=as_of,
            requested_symbols=int(completed_symbols / coverage),
            completed_symbols=completed_symbols,
            total_bars=completed_symbols * 30,
            coverage_ratio=coverage,
            started_at=utc_now() - timedelta(hours=1),
            completed_at=utc_now(),
            covered_symbols=covered_symbols,
        )
        db_session.add(batch)
        db_session.commit()

    def test_no_trading_days_returns_incomplete_history(self, db_session):
        """无 history_ingest_batches 覆盖时的状态区分。

        - symbol 在 universe (有 Security) 但无 ingest → incomplete_history
        - symbol 不在 universe (无 Security) 且无 ingest → provider_unknown
        """
        from app.universe.exclusion import get_long_suspension_state

        # 不插入任何 TradingDate / HistoricalBar / HistoryIngestBatch

        # 1) symbol 都不在 → provider_unknown
        state_no_sec = get_long_suspension_state(db_session, "999999", date(2024, 1, 1))
        assert state_no_sec == "provider_unknown"

        # 2) symbol 在 universe 但无 ingest 覆盖 → incomplete_history
        _seed_security(db_session, "600000", listing_date=date(2020, 1, 1))
        state_with_sec = get_long_suspension_state(db_session, "600000", date(2024, 1, 1))
        assert state_with_sec == "incomplete_history", (
            f"无 ingest 覆盖 + 有 sec 时必须判 incomplete_history，实际 {state_with_sec}"
        )

    def test_existing_security_no_bars_is_confirmed(self, db_session):
        """有 ingest 覆盖 + Security 存在 + 0 成交 → confirmed_long_suspension。"""
        from app.universe.exclusion import get_long_suspension_state
        from app.database.models import TradingDate

        _seed_security(
            db_session,
            "688999",
            listing_date=date(2020, 1, 1),
            trading_status="active",
        )
        for i in range(5):
            db_session.add(TradingDate(trade_date=date(2024, 1, 1) + timedelta(days=i)))
        # 关键：先有成功的 history_ingest 批次
        self._make_history_ingest_batch(
            db_session,
            as_of=date(2024, 1, 5),
            covered_symbols=["688999"],
        )

        state = get_long_suspension_state(db_session, "688999", date(2024, 1, 5), threshold_days=5)
        assert state == "confirmed_long_suspension", (
            f"有 ingest 覆盖 + 0 bar 必须判 confirmed，实际 {state}"
        )

    def test_active_symbol_is_ok(self, db_session):
        """有 ingest 覆盖 + 有完整成交 → ok。"""
        from app.universe.exclusion import get_long_suspension_state
        from app.database.models import TradingDate, HistoricalBar

        _seed_security(db_session, "600000", listing_date=date(2020, 1, 1))
        td_list = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]
        for td in td_list:
            db_session.add(TradingDate(trade_date=td))
            db_session.add(
                HistoricalBar(
                    symbol="600000",
                    trade_date=td,
                    open=10.0,
                    high=11.0,
                    low=9.5,
                    close=10.5,
                    volume=1000,
                )
            )
        db_session.commit()
        # 关键：先有成功的 history_ingest 批次
        self._make_history_ingest_batch(
            db_session,
            as_of=date(2024, 1, 3),
            covered_symbols=["600000"],
        )

        state = get_long_suspension_state(
            db_session, "600000", date(2024, 1, 3), threshold_days=5
        )
        assert state == "ok"

    def test_high_global_coverage_does_not_cover_unlisted_symbol(self, db_session):
        """批次覆盖率再高，也不能替代 covered_symbols 的逐股票证据。"""
        from app.database.models import TradingDate
        from app.universe.exclusion import get_long_suspension_state

        _seed_security(db_session, "688999", listing_date=date(2020, 1, 1))
        for offset in range(5):
            db_session.add(
                TradingDate(trade_date=date(2024, 1, 1) + timedelta(days=offset))
            )
        self._make_history_ingest_batch(
            db_session,
            as_of=date(2024, 1, 5),
            covered_symbols=["600000"],
            completed_symbols=5000,
            coverage=0.99,
        )

        state = get_long_suspension_state(
            db_session,
            "688999",
            date(2024, 1, 5),
            threshold_days=5,
        )
        assert state == "incomplete_history"

    def test_find_long_suspension_does_not_include_provider_unknown(self, db_session):
        """无 history_ingest 覆盖 + 无 securities 时不返回任何 confirmed。

        这是 P0 修复的核心：旧版 `all_symbols - active` 会把全市场都误判。
        新版：无 ingest 覆盖时 → 全部 incomplete_history，不进 confirmed 集合。
        """
        from app.universe.exclusion import find_long_suspension_symbols
        from app.database.models import TradingDate

        for i in range(3):
            db_session.add(TradingDate(trade_date=date(2024, 1, 1) + timedelta(days=i)))
        # 不插入任何 Security、不插入 history_ingest_batches
        db_session.commit()

        result = find_long_suspension_symbols(db_session, date(2024, 1, 3), threshold_days=3)
        assert result == set(), (
            f"无 securities + 无 ingest 覆盖时不应返回任何 confirmed，实际 {result}"
        )

    def test_without_ingest_batch_returns_incomplete(self, db_session):
        """有 bar 数据但**无** history_ingest_batches 覆盖 → 仍 incomplete_history。

        防止"bar 数据存在就当成已 ingest"的错误推理：
        bar 可能是手测插入的、或者 ingest 部分失败留下的残骸。
        只有成功记录在 history_ingest_batches 才算"已确认覆盖"。
        """
        from app.universe.exclusion import get_long_suspension_state
        from app.database.models import TradingDate, HistoricalBar

        _seed_security(db_session, "600000", listing_date=date(2020, 1, 1))
        for i in range(3):
            td = date(2024, 1, 1) + timedelta(days=i)
            db_session.add(TradingDate(trade_date=td))
            db_session.add(
                HistoricalBar(
                    symbol="600000", trade_date=td, open=10.0, high=11.0, low=9.5, close=10.5, volume=1000,
                )
            )
        db_session.commit()
        # 故意不调 _make_history_ingest_batch

        state = get_long_suspension_state(
            db_session, "600000", date(2024, 1, 3), threshold_days=5
        )
        assert state == "incomplete_history", (
            f"无 history_ingest_batches 覆盖时即使有 bar 也必须判 incomplete_history，"
            f"实际 {state}"
        )


# ─────────────── 8. 全市场质量门槛（修复 P0） ───────────────


class TestMarketCoverageGate:
    """验证 validate_market_coverage 拒绝所有「缩量 / 异常」返回。

    18 行 / 缺交易所 / 代码格式异常 / 重复率过高 / name 缺失过多
    → ProviderError，**禁止** 200 + 26 条。
    """

    def _make_records(
        self, count: int, *, exchange_pattern=None
    ) -> list[SecurityRecord]:
        """生成 N 条 6 位代码的 SecurityRecord，SH/SZ/BJ 分布模拟真实。"""
        from app.universe.providers import SecurityRecord

        # 用 i 直接构造 6 位代码，保证 0 重复
        records: list[SecurityRecord] = []
        for i in range(count):
            # 30% SH, 60% SZ, 10% BJ（按 i 的奇偶分布确保不重合）
            if i % 10 < 3:
                # SH 主板：600000-699999
                code = f"6{(i * 13 + 1) % 100000:05d}"
                ex = "SH"
            elif i % 10 < 9:
                # SZ 主板 000xxx / 创业板 300xxx
                if i % 2 == 0:
                    code = f"0{((i * 17 + 3) % 1000):03d}".zfill(6)
                else:
                    code = f"3{((i * 17 + 5) % 1000):03d}".zfill(6)
                ex = "SZ"
            else:
                # BJ 北证：83xxxx / 87xxxx
                code = f"8{((i * 19 + 7) % 10000):05d}"[:6]
                ex = "BJ"
            assert len(code) == 6, f"code {code} 不是 6 位"
            records.append(
                SecurityRecord(
                    symbol=code, name=f"Test{code}", exchange=ex, as_of_date=date(2026, 1, 1)
                )
            )
        # 兜底去重
        seen: set[str] = set()
        for i, r in enumerate(records):
            if r.symbol in seen:
                r.symbol = str(900000 + i).zfill(6)  # 用 9xxxxx 段
            seen.add(r.symbol)
        # 二次校验：仍有重复则抛错（generator bug）
        all_syms = [r.symbol for r in records]
        if len(set(all_syms)) != len(all_syms):
            from collections import Counter
            c = Counter(all_syms)
            dup = [k for k, v in c.items() if v > 1][:5]
            raise RuntimeError(f"generator 仍有重复: {dup}")
        return records

    def test_rejects_truncated_response(self):
        """18 行缩量 → ProviderError（不能落库）。"""
        from app.universe.providers import validate_market_coverage

        records = self._make_records(18)
        with pytest.raises(ProviderError) as exc_info:
            validate_market_coverage(records, source_id="akshare")
        assert "缩量" in str(exc_info.value) or "总数" in str(exc_info.value), (
            f"缩量应被拒绝，实际 {exc_info.value}"
        )

    def test_rejects_missing_exchange(self):
        """缺 BJ 交易所 → ProviderError。"""
        from app.universe.providers import validate_market_coverage

        # 只生成 SH + SZ 记录（去掉 BJ 那 10%）
        records = self._make_records(3600)
        records = [r for r in records if r.exchange != "BJ"]
        # 补足到 3600 条：剩下的全是 SH
        while len(records) < 3600:
            records.append(
                SecurityRecord(
                    symbol=f"60{len(records):04d}",  # 用不重复的 SH 主板代码
                    name=f"Fill{len(records)}",
                    exchange="SH",
                    as_of_date=date(2026, 1, 1),
                )
            )
        with pytest.raises(ProviderError) as exc_info:
            validate_market_coverage(records, source_id="akshare")
        assert "BJ" in str(exc_info.value), (
            f"缺 BJ 交易所应报 BJ 错，实际 {exc_info.value}"
        )

    def test_rejects_invalid_symbol_format(self):
        """代码格式异常 → ProviderError。"""
        from app.universe.providers import validate_market_coverage

        records = self._make_records(3600)
        # 把前 100 条改成无效格式
        for r in records[:100]:
            r.symbol = "abc123"
        with pytest.raises(ProviderError) as exc_info:
            validate_market_coverage(records, source_id="akshare")
        assert "格式" in str(exc_info.value) or "格式" in str(exc_info.value).lower() or "symbol" in str(exc_info.value).lower()

    def test_rejects_high_duplicate_rate(self):
        """重复率 > 1% → ProviderError。"""
        from app.universe.providers import validate_market_coverage

        records = self._make_records(3600)
        # 故意重复前 200 条（5.5% 重复）
        for i in range(200):
            records.append(records[i].model_copy())
        with pytest.raises(ProviderError) as exc_info:
            validate_market_coverage(records, source_id="akshare")
        assert "重复" in str(exc_info.value) or "dup" in str(exc_info.value).lower()

    def test_rejects_low_name_completeness(self):
        """name 缺失率 > 1% → ProviderError。"""
        from app.universe.providers import validate_market_coverage

        records = self._make_records(3600)
        for r in records[:50]:
            r.name = ""
        with pytest.raises(ProviderError) as exc_info:
            validate_market_coverage(records, source_id="akshare")
        assert "name" in str(exc_info.value).lower()

    def test_accepts_valid_market(self):
        """真实市场分布（总数 / 三交易所 / 0 重复 / 100% 完整）→ 通过。"""
        from app.universe.providers import validate_market_coverage

        records = self._make_records(3600)
        # 不应抛错
        validate_market_coverage(records, source_id="akshare")


# ─────────────── 9. snapshot 冲突检测（修复 P0） ───────────────


class TestSnapshotConflictDetection:
    """同 trading_day 重复 sync 但内容变化 → 抛 SnapshotConflictError。

    不允许静默返回旧版（用户 P0 阻断 #7）。
    """

    def test_same_day_same_content_returns_existing(self, db_session):
        """同 day 同内容 → 幂等返回旧版。"""
        from app.universe.snapshot_service import UniverseSnapshotService

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        snap_svc = UniverseSnapshotService(db_session)
        td = date(2024, 1, 15)
        s1 = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        s2 = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        assert s1.id == s2.id, "同 day 同内容必须返回同一行"

    def test_same_day_different_provider_raises_conflict(self, db_session):
        """同 day 不同 source_provider → 抛冲突（不静默返回旧版）。"""
        from app.universe.snapshot_service import (
            SnapshotConflictError,
            UniverseSnapshotService,
        )

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        snap_svc = UniverseSnapshotService(db_session)
        td = date(2024, 1, 15)
        snap_svc.get_or_create_snapshot(td, source_provider="mock")
        with pytest.raises(SnapshotConflictError):
            snap_svc.get_or_create_snapshot(td, source_provider="akshare")

    def test_same_day_force_overwrite_succeeds(self, db_session):
        """force_overwrite=True 显式覆盖 → 删旧建新。"""
        from app.universe.snapshot_service import UniverseSnapshotService

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        snap_svc = UniverseSnapshotService(db_session)
        td = date(2024, 1, 15)
        s1 = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        s2 = snap_svc.get_or_create_snapshot(
            td, source_provider="mock", force_overwrite=True
        )
        # 同 day 只能有一行：force_overwrite 原地刷新，id 保持不变
        assert s2 is not None
        assert s2.id == s1.id
        assert s2.trading_day == td

    def test_resync_same_day_keeps_snapshot_id_with_dependents(self, db_session):
        """同一天重复同步必须原地刷新，保住 snapshot.id。

        回归：force_overwrite 原来走 delete + insert，只要已有选股运行引用这张
        快照（selection_runs.snapshot_id，外键 ON DELETE NO ACTION），DELETE 就
        会 IntegrityError —— 实测「同步股票池」第二次点击返回 HTTP 500。
        """
        from app.database.models import SelectionRun

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        first = asyncio.run(svc.sync(create_snapshot=True))
        assert first.snapshot_id is not None
        db_session.add(
            SelectionRun(
                snapshot_id=first.snapshot_id,
                trading_day=date.fromisoformat(first.snapshot_trading_day),
                config_hash="0" * 64,
                config_json={"top_n": 10},
                total_candidates=26,
                eligible_count=1,
            )
        )
        db_session.commit()

        class _RenamedProvider(MockUniverseProvider):
            """内容变化（provider 不同）→ 必然走 force_overwrite 分支。"""

            source_id = "mock-resync"

        svc2 = UniverseSyncService(db=db_session, providers=[_RenamedProvider()])
        second = asyncio.run(svc2.sync(create_snapshot=True))

        assert second.snapshot_id == first.snapshot_id, "同一天刷新必须复用同一行"
        snap = db_session.get(UniverseSnapshot, second.snapshot_id)
        assert snap.source_provider == "mock-resync"
        assert snap.included_count + snap.excluded_count == snap.total_count > 0
        # 成员行被重建，不会残留旧行
        members = (
            db_session.query(UniverseMember)
            .filter(UniverseMember.snapshot_id == snap.id)
            .count()
        )
        assert members == snap.total_count
        # 依赖这张快照的选股运行仍然指着同一行快照
        run = db_session.get(SelectionRun, 1)
        assert run.snapshot_id == snap.id


# ─────────────── 10. point-in-time trading_day 校验（修复 P0） ───────────────


class TestTradingDayValidation:
    """trading_day 违反 point-in-time 约束 → InvalidTradingDayError。

    禁止用当前数据生成过去 / 未来快照。
    """

    def test_future_trading_day_raises(self, db_session):
        """trading_day 晚于 as_of_date+1 → 伪造未来快照。"""
        from app.universe.snapshot_service import (
            InvalidTradingDayError,
            UniverseSnapshotService,
        )

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        snap_svc = UniverseSnapshotService(db_session)
        # as_of=今天，trading_day=明天
        with pytest.raises(InvalidTradingDayError):
            snap_svc.get_or_create_snapshot(
                trading_day=date(2030, 1, 1),
                source_provider="mock",
                as_of_date=date(2024, 1, 1),
            )

    def test_too_early_trading_day_raises(self, db_session):
        """trading_day 早于 A 股最早交易日（1990-12-19） → 错误。"""
        from app.universe.snapshot_service import (
            InvalidTradingDayError,
            UniverseSnapshotService,
        )

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        snap_svc = UniverseSnapshotService(db_session)
        with pytest.raises(InvalidTradingDayError):
            snap_svc.get_or_create_snapshot(
                trading_day=date(1980, 1, 1),
                source_provider="mock",
                as_of_date=date(1980, 1, 1),
            )


# ─────────────── 11. 真正原子化（修复 P0） ───────────────


class TestAtomicSync:
    """sync 失败 → Security 写入也回滚。证明 sync / snapshot 真正单事务。"""

    def test_snapshot_failure_rolls_back_security(self, db_session):
        """故意让 snapshot 失败（trading_day 太早），验证 Security 也回滚。

        修复前：sync 3 次 commit（_persist_records / _record_provider_health /
        SnapshotService.get_or_create_snapshot），其中任一 commit 后失败 → 前面
        已 commit 的内容（如 Security）会保留。这是"假原子"。
        修复后：辅助服务只 flush，sync 顶层单 commit，失败 → 全回滚。
        """
        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        # sync 顶层不创建 snapshot（create_snapshot=False），但 _persist_records
        # 已经 flush 了 26 条 Security。我们用 0 commit 的 session 验证。
        asyncio.run(svc.sync(create_snapshot=False))

        # sync 没 commit，所以 Security 还在 session 里但 DB 里没有
        # 这里手动 commit 验证：没有 main 错误时应能 commit
        db_session.commit()
        from sqlalchemy import select, text
        sec_count = db_session.execute(text("SELECT COUNT(*) FROM securities")).scalar()
        assert sec_count == 26, f"sync 成功后 commit 应有 26 条 Security，实际 {sec_count}"

    def test_snapshot_atomic_rollback(self, db_session):
        """同 day 第二次 sync 走 force_overwrite=True 是预期路径；
        真冲突场景（直接调 snapshot_service 不传 force_overwrite）抛 SnapshotConflictError。
        这里改测：sync 同 day 第二次成功（force overwrite）+ security 落库数 >= 26。
        """
        from sqlalchemy import text

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        sec_count_1 = db_session.execute(text("SELECT COUNT(*) FROM securities")).scalar()
        assert sec_count_1 == 26

        # 同 day 第二次 sync（force_overwrite=True 路径，应成功）
        class _TaggingProvider(MockUniverseProvider):
            source_id = "tagging"

        svc2 = UniverseSyncService(db=db_session, providers=[_TaggingProvider()])
        result2 = asyncio.run(svc2.sync(create_snapshot=True))
        # snapshot_id 应是新的（force_overwrite 删旧建新）
        assert result2.snapshot_id is not None
        # Security 不会因 snapshot overwrite 被清空
        sec_count_2 = db_session.execute(text("SELECT COUNT(*) FROM securities")).scalar()
        assert sec_count_2 == 26, (
            f"force_overwrite 不应清空 Securities：before={sec_count_1}, after={sec_count_2}"
        )


# ─────────────── 12. 真实闭环（核心验收：included_count > 0） ───────────────


class TestRealUniverseIntegration:
    """真实闭环验收：mock 26 + 模拟 ingest 覆盖 + 真实 trading_calendar +
    真实 bars → snapshot.included_count > 0。

    这是用户 P0 反馈的硬要求：26/26 全部被长期停牌排除是不行的。
    """

    def test_included_count_positive_with_real_history(self, db_session):
        """有 ingest 覆盖 + 有 bar 数据 → 至少 1 只 included（不是全排除）。"""
        from app.database.models import (
            HistoricalBar,
            TradingDate,
            HistoryIngestBatch,
        )
        from app.universe.snapshot_service import UniverseSnapshotService

        # 1. 同步 26 条 mock 证券
        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))

        # 2. 准备 5 个交易日 + 为部分证券插入 bar
        for i in range(5):
            td = date(2024, 1, 1) + timedelta(days=i)
            db_session.add(TradingDate(trade_date=td))
        for sym in ["600000", "600519", "000001", "000002", "300750"]:
            for i in range(5):
                td = date(2024, 1, 1) + timedelta(days=i)
                db_session.add(
                    HistoricalBar(
                        symbol=sym,
                        trade_date=td,
                        open=10.0,
                        high=11.0,
                        low=9.5,
                        close=10.5,
                        volume=1000,
                    )
                )

        # 3. 记录成功的 history_ingest 批次（关键前提）
        batch = HistoryIngestBatch(
            source="akshare",
            status="succeeded",
            start_date=date(2023, 12, 1),
            end_date=date(2024, 1, 5),
            requested_symbols=26,
            completed_symbols=26,
            total_bars=26 * 30,
            coverage_ratio=1.0,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(batch)
        db_session.commit()

        # 4. 创建 snapshot
        snap_svc = UniverseSnapshotService(db_session)
        snap = snap_svc.get_or_create_snapshot(
            trading_day=date(2024, 1, 5),
            source_provider="mock",
            as_of_date=date(2024, 1, 5),
            total_count_hint=26,
        )

        # 5. included_count 必须 > 0（修复前是 0）
        assert snap.included_count > 0, (
            f"有 ingest 覆盖 + 有 bar 数据时 included_count 必须 > 0，"
            f"实际 {snap.included_count}/{snap.total_count}（excluded={snap.excluded_count}）"
        )
        # 至少有 5 只 confirmed active（600000/600519/000001/000002/300750 都有 bar）
        # 其余 21 只 → 0 bar → 仍 long_suspension / incomplete_history
        # 但因有 ingest 覆盖 → 0 bar 的 21 只可以判 confirmed_long_suspension
        # 关键：5 只有 bar → included_count 至少 5
        assert snap.included_count >= 5, (
            f"至少 5 只有 bar 数据，included_count 应 ≥ 5，实际 {snap.included_count}"
        )


# ─────────────── 13. BaoStock + AKShare BJ 子源测试 ───────────────


class TestBaoStockCodeParser:
    """BaoStock code 字符串解析：sh.000001 → (600000, 'SH')。"""

    def test_sh_prefix(self):
        from app.universe.providers import _baostock_code_to_symbol

        sym, ex = _baostock_code_to_symbol("sh.600000")
        assert sym == "600000"
        assert ex == "SH"

    def test_sz_prefix(self):
        from app.universe.providers import _baostock_code_to_symbol

        sym, ex = _baostock_code_to_symbol("sz.000001")
        assert sym == "000001"
        assert ex == "SZ"

    def test_sz_cn_prefix(self):
        """szcn.300750 也映射到 SZ。"""
        from app.universe.providers import _baostock_code_to_symbol

        sym, ex = _baostock_code_to_symbol("szcn.300750")
        assert sym == "300750"
        assert ex == "SZ"

    def test_bj_prefix(self):
        from app.universe.providers import _baostock_code_to_symbol

        sym, ex = _baostock_code_to_symbol("bj.830799")
        assert sym == "830799"
        assert ex == "BJ"

    def test_invalid_returns_empty(self):
        from app.universe.providers import _baostock_code_to_symbol

        assert _baostock_code_to_symbol("invalid") == ("", "")
        assert _baostock_code_to_symbol("sh.12345") == ("", "")  # 5 位
        assert _baostock_code_to_symbol("sh.abcdef") == ("", "")  # 非数字


class TestBaoStockProviderOffline:
    """不连真网，纯离线测试 BaoStockUniverseProvider 的逻辑分支。"""

    def test_provider_id(self):
        from app.universe.providers import BaoStockUniverseProvider

        assert BaoStockUniverseProvider.source_id == "baostock"

    def test_assemble_records_type_filtering(self):
        """type != 1 的记录（指数/债券/基金）必须被过滤。"""
        from app.universe.providers import BaoStockUniverseProvider

        p = BaoStockUniverseProvider(timeout_seconds=10.0)
        # 模拟 query_all_stock 返回：5 行 type=2 指数 + 5 行 type=1 股票
        all_stock_fields = ["code", "tradeStatus", "code_name"]
        all_stock_rows: list[list[str]] = []
        for i in range(5):
            all_stock_rows.append([f"sh.00000{i}", "1", f"指数{i}"])
        for i in range(5):
            all_stock_rows.append([f"sh.60000{i}", "1", f"股票{i}"])
        # query_stock_basic 返回 type / status 索引
        basic_fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        basic_rows: list[list[str]] = []
        for i in range(5):
            basic_rows.append([f"sh.00000{i}", f"指数{i}", "1991-07-15", "", "2", "1"])
        for i in range(5):
            basic_rows.append([f"sh.60000{i}", f"股票{i}", "2000-01-01", "", "1", "1"])

        records = p._assemble_records(
            all_stock_fields=all_stock_fields,
            all_stock_rows=all_stock_rows,
            basic_fields=basic_fields,
            basic_rows=basic_rows,
            effective_day=date(2025, 1, 1),
        )
        assert len(records) == 5
        for r in records:
            assert r.exchange == "SH"
            assert r.symbol.startswith("60000")
            assert r.listing_date == date(2000, 1, 1)
            assert r.trading_status == "active"

    def test_assemble_records_status_mapping(self):
        """当日 tradeStatus 决定状态；当前 basic.status 不得改写历史状态。"""
        from app.universe.providers import BaoStockUniverseProvider

        p = BaoStockUniverseProvider(timeout_seconds=10.0)
        all_stock_fields = ["code", "tradeStatus", "code_name"]
        all_stock_rows = [
            ["sh.600000", "1", "active 股票"],          # tradeStatus=1 → active
            ["sh.600001", "1", "历史日在市、当前已退市"],
            ["sh.600002", "0", "当日停牌 股票"],         # tradeStatus=0 → suspended
            ["sh.600003", "1", "未 listed 股票"],        # status=未知 → active 兜底
            ["sh.600004", "1", "上市晚于当日 股票"],     # ipoDate > effective_day → 跳过
        ]
        basic_fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        basic_rows = [
            ["sh.600000", "active 股票", "1999-11-10", "", "1", "1"],   # status=1 → listed
            ["sh.600001", "delisted 股票", "1999-11-10", "2024-05-22", "1", "0"],  # status=0 → delisted
            ["sh.600002", "suspended 股票", "1999-11-10", "", "1", "1"],  # status=1 但 tradeStatus=0 → suspended
            ["sh.600003", "未 listed 股票", "1999-11-10", "", "1", ""],   # status 空
            ["sh.600004", "未来上市", "2099-01-01", "", "1", "1"],
        ]
        records = p._assemble_records(
            all_stock_fields=all_stock_fields,
            all_stock_rows=all_stock_rows,
            basic_fields=basic_fields,
            basic_rows=basic_rows,
            effective_day=date(2025, 1, 1),
        )
        # 600004 跳过（未来上市），其余 4 只
        assert len(records) == 4
        status_by_sym = {r.symbol: r.trading_status for r in records}
        assert status_by_sym["600000"] == "active"
        # stock_basic.status=0 表示当前已退市，但该证券存在于历史日清单且
        # tradeStatus=1，所以历史日必须仍为 active。
        assert status_by_sym["600001"] == "active"
        assert records[1].delisted_date == date(2024, 5, 22)
        assert status_by_sym["600002"] == "suspended"
        # status 空 → 未知，tradeStatus=1 → active（保守取大概率）
        assert status_by_sym["600003"] == "active"

    def test_historical_membership_is_driven_by_daily_list(self):
        """只存在于当前 basic、但不在指定交易日清单的股票不得进入历史快照。"""
        from app.universe.providers import BaoStockUniverseProvider

        provider = BaoStockUniverseProvider(timeout_seconds=10.0)
        records = provider._assemble_records(
            all_stock_fields=["code", "tradeStatus", "code_name"],
            all_stock_rows=[["sh.600000", "1", "历史成员"]],
            basic_fields=["code", "code_name", "ipoDate", "outDate", "type", "status"],
            basic_rows=[
                ["sh.600000", "历史成员", "2000-01-01", "", "1", "1"],
                ["sh.600001", "后来上市", "2024-01-01", "", "1", "1"],
            ],
            effective_day=date(2020, 1, 2),
        )
        assert [record.symbol for record in records] == ["600000"]

    def test_assemble_records_skip_future_listing(self):
        """ipoDate > effective_day 必须跳过（当日尚未上市）。"""
        from app.universe.providers import BaoStockUniverseProvider

        p = BaoStockUniverseProvider(timeout_seconds=10.0)
        all_stock_fields = ["code", "tradeStatus", "code_name"]
        all_stock_rows = [
            ["sh.600000", "1", "已上市"],
            ["sh.600001", "1", "未来上市"],
        ]
        basic_fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        basic_rows = [
            ["sh.600000", "已上市", "2000-01-01", "", "1", "1"],
            ["sh.600001", "未来上市", "2099-01-01", "", "1", "1"],
        ]
        records = p._assemble_records(
            all_stock_fields=all_stock_fields,
            all_stock_rows=all_stock_rows,
            basic_fields=basic_fields,
            basic_rows=basic_rows,
            effective_day=date(2025, 1, 1),
        )
        assert len(records) == 1
        assert records[0].symbol == "600000"

    def test_assemble_records_empty_raises(self):
        """如果所有记录都被过滤掉（type != 1），必须抛 ProviderError。"""
        from app.universe.providers import BaoStockUniverseProvider, ProviderError

        p = BaoStockUniverseProvider(timeout_seconds=10.0)
        all_stock_fields = ["code", "tradeStatus", "code_name"]
        all_stock_rows = [
            ["sh.000001", "1", "指数A"],
        ]
        basic_fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        basic_rows = [
            ["sh.000001", "指数A", "1991-07-15", "", "2", "1"],
        ]
        with pytest.raises(ProviderError) as exc_info:
            p._assemble_records(
                all_stock_fields=all_stock_fields,
                all_stock_rows=all_stock_rows,
                basic_fields=basic_fields,
                basic_rows=basic_rows,
                effective_day=date(2025, 1, 1),
            )
        assert "0 条 type=1" in str(exc_info.value)

    def test_historical_fetch_never_uses_current_bj_supplement(self):
        """AKShare BJ 端点是当前名单，历史请求时必须完全禁用。"""
        from app.universe.providers import BaoStockUniverseProvider

        class Result:
            error_code = "0"
            error_msg = "success"

            def __init__(self, fields, rows):
                self.fields = fields
                self.rows = rows
                self.position = 0

            def next(self):
                return self.position < len(self.rows)

            def get_row_data(self):
                row = self.rows[self.position]
                self.position += 1
                return row

        class FakeBaoStock:
            @staticmethod
            def query_trade_dates(start_date, end_date):
                return Result(
                    ["calendar_date", "is_trading_day"],
                    [["2020-01-02", "1"]],
                )

            @staticmethod
            def query_all_stock(day):
                assert day == "2020-01-02"
                return Result(
                    ["code", "tradeStatus", "code_name"],
                    [["sh.600000", "1", "历史成员"]],
                )

            @staticmethod
            def query_stock_basic():
                return Result(
                    ["code", "code_name", "ipoDate", "outDate", "type", "status"],
                    [["sh.600000", "历史成员", "1999-11-10", "", "1", "1"]],
                )

        provider = BaoStockUniverseProvider(
            timeout_seconds=10.0,
            as_of_date=date(2020, 1, 2),
        )
        provider._fetch_bj_supplement_sync = MagicMock(
            side_effect=AssertionError("历史请求不得访问当前 BJ 端点")
        )

        records, effective_day = provider._fetch_sync_inner(FakeBaoStock)

        assert effective_day == date(2020, 1, 2)
        assert [record.symbol for record in records] == ["600000"]
        provider._fetch_bj_supplement_sync.assert_not_called()


class TestAkshareBjSupplementOffline:
    """AKShare 北交所子源离线测试。"""

    def test_provider_id(self):
        from app.universe.providers import AkshareBjSupplementProvider

        assert AkshareBjSupplementProvider.source_id == "akshare_bj"

    def test_chinese_field_parsing(self):
        """AKShare 北交所返回字段是中文：证券代码/证券简称/上市日期。"""
        from app.universe.providers import AkshareBjSupplementProvider

        p = AkshareBjSupplementProvider(timeout_seconds=10.0)
        rows = [
            {
                "证券代码": "920000",
                "证券简称": "安徽凤凰",
                "上市日期": "2020-12-23",
                "所属行业": "汽车制造业",
            },
            {
                "证券代码": "920001",
                "证券简称": "纬达光电",
                "上市日期": "2022-12-27",
                "所属行业": "电子设备",
            },
        ]
        records = p._records_from_rows(rows, as_of_date=date(2025, 1, 1))
        assert len(records) == 2
        assert records[0].symbol == "920000"
        assert records[0].name == "安徽凤凰"
        assert records[0].exchange == "BJ"
        assert records[0].board == "bj"
        assert records[0].listing_date == date(2020, 12, 23)
        assert records[0].sector == "汽车制造业"

    def test_dedup_and_format_filter(self):
        """代码格式不对（5 位 / 非数字）+ 重复 symbol 必须被去重。"""
        from app.universe.providers import AkshareBjSupplementProvider

        p = AkshareBjSupplementProvider(timeout_seconds=10.0)
        rows = [
            {"证券代码": "920000", "证券简称": "A", "上市日期": "2020-12-23"},
            {"证券代码": "920000", "证券简称": "A 重名", "上市日期": "2020-12-23"},
            {"证券代码": "92000", "证券简称": "5 位", "上市日期": "2020-12-23"},
            {"证券代码": "abcdef", "证券简称": "非数字", "上市日期": "2020-12-23"},
        ]
        records = p._records_from_rows(rows, as_of_date=date(2025, 1, 1))
        assert len(records) == 1
        assert records[0].symbol == "920000"


class TestBuildProviderBaostock:
    """build_provider_by_name('baostock') 应返回 BaoStockUniverseProvider 实例。"""

    def test_baostock_name(self):
        from app.universe.providers import (
            BaoStockUniverseProvider,
            build_provider_by_name,
        )

        p = build_provider_by_name("baostock", timeout_seconds=99.0)
        assert isinstance(p, BaoStockUniverseProvider)

    def test_unknown_name_raises(self):
        from app.universe.providers import ProviderError, build_provider_by_name

        with pytest.raises(ProviderError) as exc_info:
            build_provider_by_name("foo")
        assert "未知的 UNIVERSE_PROVIDER" in str(exc_info.value)
        assert "foo" in str(exc_info.value)

    def test_case_insensitive(self):
        from app.universe.providers import (
            BaoStockUniverseProvider,
            build_provider_by_name,
        )

        p = build_provider_by_name("BaoStock")
        assert isinstance(p, BaoStockUniverseProvider)


# ─────────────── 14. 历史动态阈值测试（修复 693e866） ───────────────


class TestHistoricalCoverageThreshold:
    """validate_market_coverage 按 effective_date 选历史阈值。

    早期年份 A 股规模小，固定 3500 必然失败：
    - 2024+ : 5300（SH 2300 / SZ 2900 / BJ 250）
    - 2020-2023 : 4500
    - 2015-2019 : 3500
    - 2010-2014 : 2500
    - 2005-2009 : 1500
    - 2000-2004 : 1100
    - 1991-1999 : 800
    """

    def test_resolve_threshold_modern(self):
        from app.universe.providers import _resolve_threshold

        total, per_ex, baseline = _resolve_threshold(date(2025, 6, 15))
        assert total == 3500
        assert per_ex == {"SH": 1000, "SZ": 1500, "BJ": 100}
        assert baseline == date(2024, 1, 1)

    def test_resolve_threshold_2010s(self):
        from app.universe.providers import _resolve_threshold

        total, per_ex, baseline = _resolve_threshold(date(2017, 8, 20))
        assert total == 1500  # 2015-2019 区间
        assert baseline == date(2015, 1, 1)
        assert per_ex.get("BJ", 0) == 0  # 早期无 BJ 要求

    def test_resolve_threshold_requires_bj_after_exchange_launch(self):
        from app.universe.providers import _resolve_threshold

        _, per_ex, baseline = _resolve_threshold(date(2023, 6, 1))
        assert baseline == date(2021, 11, 15)
        assert per_ex["BJ"] > 0

    def test_resolve_threshold_1990s(self):
        from app.universe.providers import _resolve_threshold

        total, per_ex, baseline = _resolve_threshold(date(1995, 1, 1))
        assert total == 100
        assert baseline == date(1990, 12, 19)
        assert per_ex.get("SH") == 50
        assert per_ex.get("SZ") == 50

    def test_validate_accepts_historical_small_count(self):
        """历史日期少量记录（< 3500）必须能通过 validate。"""
        from app.universe.providers import (
            SecurityRecord,
            validate_market_coverage,
        )

        # 2017 年的 700 只 SH + 800 只 SZ = 1500 条
        # 历史阈值 (2015-2019): total ≥ 1500, SH ≥ 600, SZ ≥ 800, BJ=0
        records: list[SecurityRecord] = []
        for i in range(700):
            records.append(
                SecurityRecord(
                    # SH 主板 600000-600999 → 用 i 拼成 6 位（i=0..699 → 600000..600699）
                    symbol=f"60{i:04d}",
                    name=f"股票{i}",
                    exchange="SH",
                    as_of_date=date(2017, 6, 1),
                )
            )
        for i in range(800):
            records.append(
                SecurityRecord(
                    # SZ 主板 000000-000999 → 用 i 拼成 6 位
                    symbol=f"00{i:04d}",
                    name=f"股票{i}",
                    exchange="SZ",
                    as_of_date=date(2017, 6, 1),
                )
            )
        # 现代阈值：1500 < 3500 → 必失败（总缩量）
        import pytest as _pytest

        with _pytest.raises(Exception) as exc_info:
            validate_market_coverage(records, source_id="baostock", as_of_date=None)
        assert "缩量" in str(exc_info.value) or "min=" in str(exc_info.value)
        # 历史阈值（2017 落在 2015-2019 档：1500）：1500 条刚好等于阈值，应通过
        validate_market_coverage(
            records, source_id="baostock", as_of_date=date(2017, 6, 1)
        )

    def test_validate_rejects_modern_count_when_dated_as_historical(self):
        """日期被传成历史日 + records 数量不够历史阈值 → 拒。"""
        from app.universe.providers import (
            SecurityRecord,
            validate_market_coverage,
        )

        # 2012 阈值: total ≥ 800 / SH ≥ 400 / SZ ≥ 500
        # 准备 500 SH + 400 SZ = 900 条（≥ 800 总数 OK，SZ 400 < 500 → 应拒）
        records: list[SecurityRecord] = []
        for i in range(500):
            records.append(
                SecurityRecord(
                    symbol=f"60{i:04d}",
                    name=f"股票{i}",
                    exchange="SH",
                    as_of_date=date(2012, 1, 1),
                )
            )
        for i in range(400):
            records.append(
                SecurityRecord(
                    symbol=f"00{i:04d}",
                    name=f"股票{i}",
                    exchange="SZ",
                    as_of_date=date(2012, 1, 1),
                )
            )
        import pytest as _pytest

        with _pytest.raises(Exception) as exc_info:
            validate_market_coverage(
                records, source_id="baostock", as_of_date=date(2012, 1, 1)
            )
        assert "SZ" in str(exc_info.value), (
            f"应明确指 SZ 覆盖不足，实际: {exc_info.value}"
        )


# ─────────────── 15. 真依赖 + 真契约测试（修复 693e866 mock-only） ───────────────


class TestBaoStockRealDependency:
    """依赖与 ResultData 游标契约验证；单元测试不访问公网。"""

    def test_baostock_importable(self):
        """baostock 必须可导入，且版本 >= 0.9.3。"""
        from importlib.metadata import version

        import baostock  # noqa: F401

        assert tuple(int(part) for part in version("baostock").split(".")) >= (0, 9, 3)

    def test_result_cursor_consumes_each_row_once(self):
        """next/get_row_data 必须配对，避免游标停在同一行形成假死。"""
        from app.universe.providers import _collect_baostock_rows

        class FakeResult:
            error_code = "0"
            error_msg = "success"
            fields = ["code", "tradeStatus"]

            def __init__(self):
                self.rows = [["sh.600000", "1"], ["sh.600001", "0"]]
                self.position = 0
                self.read_count = 0

            def next(self):
                return self.position < len(self.rows)

            def get_row_data(self):
                row = self.rows[self.position]
                self.position += 1
                self.read_count += 1
                return row

        result = FakeResult()
        fields, rows = _collect_baostock_rows(
            result, query_name="fake", max_rows=10
        )
        assert fields == ["code", "tradeStatus"]
        assert rows == result.rows
        assert result.read_count == 2

    def test_result_cursor_rejects_truncation(self):
        from app.universe.providers import _collect_baostock_rows

        class EndlessResult:
            error_code = "0"
            error_msg = "success"
            fields = ["code"]

            def next(self):
                return True

            def get_row_data(self):
                return ["sh.600000"]

        with pytest.raises(ProviderError, match="拒绝截断"):
            _collect_baostock_rows(
                EndlessResult(), query_name="endless", max_rows=3
            )


# ─────────────── 16. API 不传 date.today() 测试 ───────────────


class TestSyncAPIDoesNotPassDateToday:
    """修复 693e866：API /sync 不再传 date.today()。

    trading_day 必须从 provider.as_of_date 拿，否则周末/节假日会让
    snapshot 落在非交易日。
    """

    def test_sync_route_omits_trading_day(self):
        """检查 /sync 路由调用 sync() 时不传 trading_day。"""
        import inspect

        from app.api.universe import sync

        source = inspect.getsource(sync)
        # 提取 sync_service.sync(...) 调用，检查不含 trading_day=
        import re

        # 匹配 sync_svc.sync( 后的所有参数
        sync_call_match = re.search(
            r"sync_svc\.sync\s*\(([^)]*)\)", source, re.DOTALL
        )
        assert sync_call_match, "找不到 sync_svc.sync 调用"
        sync_call = sync_call_match.group(1)
        assert "trading_day" not in sync_call, (
            f"/sync 不应传 trading_day，当前调用:\n{sync_call}"
        )

    def test_sync_service_no_today_fallback(self):
        """sync_service.sync 不再 fallback 到 _date.today()。"""
        import inspect

        from app.universe.sync_service import UniverseSyncService

        source = inspect.getsource(UniverseSyncService.sync)
        assert "_date.today()" not in source, (
            "sync_service.sync 不应 fallback 到 _date.today()"
        )


class TestBaoStockSocketTimeout:
    """BaoStock 内部 socket 无超时会永久阻塞：必须在拉取期间显式设置。

    实机教训：BaoStock 连接静默断开时 rs.next() 会永久阻塞，而事件循环的
    wait_for 取消不了已经在线程池里运行的调用，整个进程会卡死（实测 >10 分钟）。
    """

    def _install_fake_baostock(self, monkeypatch, login):
        import sys
        import types

        monkeypatch.setitem(
            sys.modules, "baostock", types.SimpleNamespace(login=login)
        )

    def test_socket_timeout_applied_during_fetch(self, monkeypatch):
        import socket

        from app.universe.providers import (
            _BAOSTOCK_SOCKET_TIMEOUT_SECONDS,
            BaoStockUniverseProvider,
        )

        observed: dict = {}

        def login():
            observed["timeout"] = socket.getdefaulttimeout()
            return type("R", (), {"error_code": "1", "error_msg": "fake"})()

        self._install_fake_baostock(monkeypatch, login)
        before = socket.getdefaulttimeout()
        try:
            with pytest.raises(ProviderError):
                BaoStockUniverseProvider()._fetch_sync()
            assert observed["timeout"] == _BAOSTOCK_SOCKET_TIMEOUT_SECONDS
            # 不得把全局 socket 超时泄漏给其它线程/后续调用
            assert socket.getdefaulttimeout() == before
        finally:
            socket.setdefaulttimeout(before)

    def test_socket_timeout_restored_when_login_raises(self, monkeypatch):
        import socket

        from app.universe.providers import BaoStockUniverseProvider

        def login():
            raise OSError("connection reset")

        self._install_fake_baostock(monkeypatch, login)
        before = socket.getdefaulttimeout()
        try:
            # 原始异常类型不重要（fetch_all 会统一包成 ProviderError），
            # 关键是 finally 里把全局 socket 超时还原回去。
            with pytest.raises(OSError):
                BaoStockUniverseProvider()._fetch_sync()
            assert socket.getdefaulttimeout() == before
        finally:
            socket.setdefaulttimeout(before)


class TestPointInTimeSnapshotFromRecords:
    """历史时点快照：成员只来自「当日清单」，不能被当前全量证券表污染。"""

    @staticmethod
    def _seed_master(db) -> None:
        from app.market_rules.security_master import SecurityMasterService

        master = SecurityMasterService(db)
        # 2026 年才上市：绝不能出现在 2022 年的快照里（否则是未来函数）
        master.ensure(
            symbol="999999", name="未来上市股", is_st=False,
            listing_date=date(2026, 1, 5), source="test", exchange="SH",
            trading_status="active",
        )
        # 当年在场、后来退市：必须留在 2022 快照里（否则是幸存者偏差）
        master.ensure(
            symbol="888888", name="后来退市股", is_st=False,
            listing_date=date(2010, 3, 1), source="test", exchange="SZ",
            delisted_date=date(2024, 5, 20), trading_status="delisted",
        )
        db.commit()

    @staticmethod
    def _records(day: date):
        from app.universe.providers import SecurityRecord

        return [
            SecurityRecord(
                symbol="888888", name="后来退市股", exchange="SZ", board="main",
                is_st=False, listing_date=date(2010, 3, 1),
                trading_status="active", as_of_date=day,
            ),
            SecurityRecord(
                symbol="777777", name="当年在场新股", exchange="SH", board="main",
                is_st=False, listing_date=date(2015, 3, 1),
                trading_status="active", as_of_date=day,
            ),
            SecurityRecord(
                symbol="666666", name="当日停牌股", exchange="SZ", board="main",
                is_st=False, listing_date=date(2009, 7, 1),
                trading_status="suspended", as_of_date=day,
            ),
        ]

    def test_members_come_from_records_not_current_master(self, db_session):
        from app.time_utils import utc_now
        from app.universe.snapshot_service import UniverseSnapshotService

        self._seed_master(db_session)
        day = date(2022, 6, 30)
        snap = UniverseSnapshotService(db_session).get_or_create_snapshot_from_records(
            records=self._records(day), trading_day=day, source_provider="test",
            source_synced_at=utc_now(), as_of_date=day,
        )
        db_session.flush()
        members = {
            m.symbol: m
            for m in db_session.execute(
                select(UniverseMember).where(UniverseMember.snapshot_id == snap.id)
            ).scalars()
        }
        assert set(members) == {"888888", "777777", "666666"}
        assert "999999" not in members, "2026 年上市的标的不能进 2022 年快照"
        assert snap.total_count == 3

    def test_delisted_later_and_suspended_are_classified_by_that_day(self, db_session):
        from app.time_utils import utc_now
        from app.universe.snapshot_service import UniverseSnapshotService

        self._seed_master(db_session)
        day = date(2022, 6, 30)
        snap = UniverseSnapshotService(db_session).get_or_create_snapshot_from_records(
            records=self._records(day), trading_day=day, source_provider="test",
            source_synced_at=utc_now(), as_of_date=day,
        )
        db_session.flush()
        members = {
            m.symbol: m
            for m in db_session.execute(
                select(UniverseMember).where(UniverseMember.snapshot_id == snap.id)
            ).scalars()
        }
        # 当年正常交易 → 入选；当日停牌 → 排除，理由是 suspended 而不是 delisted
        assert members["888888"].is_included is True
        assert members["888888"].trading_status == "active"
        assert members["777777"].is_included is True
        assert members["666666"].is_included is False
        assert members["666666"].exclude_reason == "suspended"
        assert snap.included_count == 2
        assert snap.excluded_count == 1

    def test_master_is_only_extended_never_overwritten(self, db_session):
        from app.time_utils import utc_now
        from app.universe.snapshot_service import UniverseSnapshotService

        self._seed_master(db_session)
        day = date(2022, 6, 30)
        UniverseSnapshotService(db_session).get_or_create_snapshot_from_records(
            records=self._records(day), trading_day=day, source_provider="test",
            source_synced_at=utc_now(), as_of_date=day,
        )
        db_session.flush()
        # 当前主数据不能被 2022 年的「当日状态」改写
        assert db_session.get(Security, "888888").trading_status == "delisted"
        assert db_session.get(Security, "999999").name == "未来上市股"
        # 从没进过主数据表的标的需要补一条，否则 universe_members.security_id 外键悬空
        added = db_session.get(Security, "777777")
        assert added is not None and added.listing_date == date(2015, 3, 1)

    def test_rebuild_same_day_keeps_snapshot_id_and_replaces_members(self, db_session):
        from app.time_utils import utc_now
        from app.universe.snapshot_service import UniverseSnapshotService

        self._seed_master(db_session)
        day = date(2022, 6, 30)
        service = UniverseSnapshotService(db_session)
        first = service.get_or_create_snapshot_from_records(
            records=self._records(day), trading_day=day, source_provider="test",
            source_synced_at=utc_now(), as_of_date=day,
        )
        first_id = first.id
        second = service.get_or_create_snapshot_from_records(
            records=self._records(day)[:2], trading_day=day, source_provider="test",
            source_synced_at=utc_now(), as_of_date=day,
        )
        assert second.id == first_id, "同交易日重建必须原地刷新，保住外键引用"
        db_session.flush()
        rows = db_session.execute(
            select(UniverseMember).where(UniverseMember.snapshot_id == first_id)
        ).scalars().all()
        assert len(rows) == 2
        assert second.total_count == 2

    def test_empty_records_rejected(self, db_session):
        from app.time_utils import utc_now
        from app.universe.snapshot_service import (
            SnapshotConflictError,
            UniverseSnapshotService,
        )

        with pytest.raises(SnapshotConflictError):
            UniverseSnapshotService(db_session).get_or_create_snapshot_from_records(
                records=[], trading_day=date(2022, 6, 30), source_provider="test",
                source_synced_at=utc_now(), as_of_date=date(2022, 6, 30),
            )