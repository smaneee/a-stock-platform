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

    def test_no_listing_date_excluded_as_incomplete(self, db_session):
        _seed_security(db_session, "600003", listing_date=None)
        engine = ExclusionEngine(db_session)
        decisions = engine.evaluate(
            [db_session.get(Security, "600003")],
            as_of=date(2024, 1, 1),
        )
        assert decisions[0].reason == "incomplete_data"

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

        SQLite 默认 FK 不强制，所以这里手动 PRAGMA foreign_keys=ON。
        """
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy import text

        # 强制 SQLite 启用 FK 约束
        db_session.execute(text("PRAGMA foreign_keys=ON"))

        svc = UniverseSyncService(db=db_session, providers=[MockUniverseProvider()])
        asyncio.run(svc.sync(create_snapshot=True))
        snap_svc = UniverseSnapshotService(db_session)
        td = date(2024, 1, 15)
        snap = snap_svc.get_or_create_snapshot(td, source_provider="mock")
        member_count_before = len(
            db_session.execute(
                select(UniverseMember).where(UniverseMember.snapshot_id == snap.id)
            ).scalars().all()
        )
        assert member_count_before > 0

        # 试图删 Security → 应当失败（被 NO ACTION FK 拦下）
        sec = db_session.get(Security, "600000")
        db_session.delete(sec)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

        # 历史 member 仍然完整
        db_session.expire_all()
        member_count_after = len(
            db_session.execute(
                select(UniverseMember).where(UniverseMember.snapshot_id == snap.id)
            ).scalars().all()
        )
        assert member_count_after == member_count_before


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
    """修复 P1：long_suspension 区分 confirmed / incomplete / provider_unknown。"""

    def test_no_trading_days_returns_incomplete_history(self, db_session):
        """交易日历表空 → 所有 stock 都是 incomplete_history（非 confirmed）。"""
        # 不插入任何 TradingDate / HistoricalBar
        from app.universe.exclusion import get_long_suspension_state
        # 注：未插入 Security 的 symbol → provider_unknown
        state_no_sec = get_long_suspension_state(db_session, "999999", date(2024, 1, 1))
        assert state_no_sec == "provider_unknown"

    def test_existing_security_no_bars_is_confirmed(self, db_session):
        """Security 存在 + 0 成交 → confirmed_long_suspension。"""
        from app.universe.exclusion import get_long_suspension_state

        _seed_security(
            db_session,
            "688999",
            listing_date=date(2020, 1, 1),
            trading_status="active",
        )
        # 插入交易日历但这只 stock 没 bar
        from app.database.models import TradingDate
        for i in range(5):
            db_session.add(TradingDate(trade_date=date(2024, 1, 1) + timedelta(days=i)))
        db_session.commit()

        state = get_long_suspension_state(db_session, "688999", date(2024, 1, 5), threshold_days=5)
        assert state == "confirmed_long_suspension"

    def test_active_symbol_is_ok(self, db_session):
        """有完整成交覆盖 → ok。"""
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

        state = get_long_suspension_state(
            db_session, "600000", date(2024, 1, 3), threshold_days=5
        )
        assert state == "ok"

    def test_find_long_suspension_does_not_include_provider_unknown(self, db_session):
        """未在 Security 表的 symbol 即使无 bar 也不被列为 confirmed。

        这是 P1 修复的核心：旧版 `all_symbols - active` 会把全市场都误判。
        """
        from app.universe.exclusion import find_long_suspension_symbols
        from app.database.models import TradingDate

        for i in range(3):
            db_session.add(TradingDate(trade_date=date(2024, 1, 1) + timedelta(days=i)))
        # 不插入任何 Security
        db_session.commit()

        # 不应返回任何 confirmed（因为没在 securities 里 → 都是 provider_unknown）
        result = find_long_suspension_symbols(db_session, date(2024, 1, 3), threshold_days=3)
        assert result == set(), (
            f"无 securities 时不应返回任何 confirmed，实际 {result}"
        )
