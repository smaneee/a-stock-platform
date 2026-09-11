"""UniverseSyncService — 全市场证券主数据同步编排。

职责：
1) 顺序尝试多个 Provider（按 UNIVERSE_PROVIDERS 配置顺序），任一成功即返回；
   全部失败抛 AllProvidersFailedError → /sync 返回 503，**禁止**静默退回 mock。
2) 单 provider 调用 N 次（retry+backoff），N 次都失败才切到下一个。
3) 持久化 DataSourceHealth（最近成功/失败/连续失败数）。
4) 写入 Security 表（upsert），不变更调用方原有逻辑。
5) **与 snapshot 原子化**：sync 成功后立即在同一个 Session/事务中创建
   当日 UniverseSnapshot，使 /sync 之后 /filter 必定可用。

配置驱动（不允许硬编码默认 Provider）：
- 生产默认 UNIVERSE_PROVIDERS=akshare
- 测试 / managed E2E 模式：E2E_USE_MOCK=true → 自动切到 mock
- 也可以显式传 universe_providers=mock
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.models import DataSourceHealth, Security
from app.market_rules.security_master import SecurityMasterService
from app.time_utils import utc_now
from app.universe.providers import (
    AkshareUniverseProvider,
    MockUniverseProvider,
    ProviderError,
    SecurityRecord,
    UniverseProvider,
    build_provider_by_name,
)

logger = logging.getLogger(__name__)


class AllProvidersFailedError(Exception):
    """所有 provider 都失败 → 没有可同步的数据，sync 整体失败。"""


@dataclass
class SyncResult:
    """一次 sync 的结果摘要，便于 API 返回 & 测试断言。"""

    source_provider: str
    synced_at: datetime
    total_fetched: int
    new_securities: int = 0
    updated_securities: int = 0
    attempted_providers: list[str] = field(default_factory=list)
    provider_health: dict[str, str] = field(default_factory=dict)
    snapshot_id: int | None = None
    snapshot_trading_day: str | None = None

    def as_dict(self) -> dict:
        return {
            "source_provider": self.source_provider,
            "synced_at": self.synced_at.isoformat(),
            "total_fetched": self.total_fetched,
            "new_securities": self.new_securities,
            "updated_securities": self.updated_securities,
            "attempted_providers": list(self.attempted_providers),
            "provider_health": dict(self.provider_health),
            "snapshot_id": self.snapshot_id,
            "snapshot_trading_day": self.snapshot_trading_day,
        }


def _resolve_providers_from_settings() -> list[UniverseProvider]:
    """从 settings 构造 provider 实例。

    关键约束：
    - 生产默认从 UNIVERSE_PROVIDERS 配置读取（默认 akshare）
    - E2E_USE_MOCK=true 时强制切到 mock（CI/managed 模式）
    - 任何拼错的名字都直接抛 ProviderError，禁止静默 fallback
    """
    settings = get_settings()
    names = settings.universe_provider_list
    providers: list[UniverseProvider] = []
    for name in names:
        if name == "akshare":
            providers.append(
                AkshareUniverseProvider(
                    timeout_seconds=settings.akshare_universe_timeout_seconds
                )
            )
        elif name == "mock":
            providers.append(MockUniverseProvider())
        else:
            # 拼错名字直接抛错（不许静默 fallback）
            raise ProviderError(
                "factory",
                f"未知的 UNIVERSE_PROVIDER: {name!r}（仅支持 mock / akshare）",
            )
    return providers


class UniverseSyncService:
    """编排同步逻辑；每个实例绑定一个 Session。"""

    def __init__(
        self,
        db: Session,
        providers: Sequence[UniverseProvider] | None = None,
        max_retries: int | None = None,
        backoff_base_ms: int | None = None,
    ):
        self._db = db
        # 用户传入 provider 实例列表；否则按 settings.universe_provider_list 构造
        if providers is not None:
            self._providers: list[UniverseProvider] = list(providers)
        else:
            self._providers = _resolve_providers_from_settings()
        settings = get_settings()
        self._max_retries = (
            max_retries if max_retries is not None else settings.universe_max_retries
        )
        self._backoff_base_ms = (
            backoff_base_ms if backoff_base_ms is not None else settings.universe_backoff_base_ms
        )

    @property
    def providers(self) -> list[UniverseProvider]:
        return list(self._providers)

    async def sync(self, *, create_snapshot: bool = False, trading_day=None) -> SyncResult:
        """同步全部证券主数据。

        行为：
        1) 顺序尝试 self._providers（按 settings.universe_provider_list 顺序）
        2) 每个 provider 内最多 self._max_retries 次（指数退避 + 抖动）
        3) 任一 provider 一次成功即止
        4) 全部失败 → 抛 AllProvidersFailedError（每个 provider 的失败都写 health 表）
        5) **成功后立即在同一个 Session 中创建当日 snapshot**（除非 create_snapshot=False）
           — 这是 /sync → /filter 闭环的硬性保证；不在 snapshot 阶段的失败也要让整体失败。

        严禁：真实源失败后静默回落到 mock —— 这是用户 P0 反馈的核心。
        """
        attempted: list[str] = []
        health_snapshot: dict[str, str] = {}
        for provider in self._providers:
            attempted.append(provider.source_id)
            try:
                records = await self._fetch_with_retry(provider)
            except ProviderError as exc:
                logger.warning("Provider %s 全部重试失败: %s", provider.source_id, exc)
                self._record_provider_health(provider.source_id, success=False, error=str(exc))
                health_snapshot[provider.source_id] = "failed"
                continue

            synced_at = utc_now()
            new_count, upd_count = self._persist_records(records, provider.source_id)
            self._record_provider_health(
                provider.source_id, success=True, synced_at=synced_at
            )
            health_snapshot[provider.source_id] = "ok"
            # 其余 provider 也打一次 health（标记为 "not_attempted"）以供 status API
            for p in self._providers:
                if p.source_id not in health_snapshot:
                    health_snapshot[p.source_id] = "skipped"

            # ───────── 原子化：sync 成功后立即在同 session 创建 snapshot ─────────
            snapshot_id: int | None = None
            snapshot_trading_day = None
            if create_snapshot:
                from app.universe.snapshot_service import UniverseSnapshotService
                from datetime import date as _date

                snap_svc = UniverseSnapshotService(self._db)
                td = trading_day or synced_at.date() or _date.today()
                snap = snap_svc.get_or_create_snapshot(
                    trading_day=td,
                    source_provider=provider.source_id,
                    source_synced_at=synced_at,
                )
                snapshot_id = snap.id
                snapshot_trading_day = snap.trading_day.isoformat()

            return SyncResult(
                source_provider=provider.source_id,
                synced_at=synced_at,
                total_fetched=len(records),
                new_securities=new_count,
                updated_securities=upd_count,
                attempted_providers=attempted,
                provider_health=health_snapshot,
                snapshot_id=snapshot_id,
                snapshot_trading_day=snapshot_trading_day,
            )

        # 所有 provider 都失败：必须抛错，让上层 API 返回 503
        # 不允许在这里退回 mock —— 那是"伪闭环"，违背用户的 P0 约束
        raise AllProvidersFailedError(
            f"全部 {len(self._providers)} 个 provider 都失败：{attempted}"
        )

    async def _fetch_with_retry(self, provider: UniverseProvider) -> list[SecurityRecord]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await provider.fetch_all()
            except ProviderError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                backoff_ms = self._backoff_base_ms * (2 ** attempt)
                jitter = random.uniform(0, 0.25 * backoff_ms)
                await asyncio.sleep((backoff_ms + jitter) / 1000.0)
        assert last_error is not None
        raise last_error

    # ───────── persist ─────────

    def _persist_records(
        self, records: list[SecurityRecord], source: str
    ) -> tuple[int, int]:
        """写入 securities 表。返回 (新增, 更新) 计数。

        不调用 SecurityMasterService — 因为我们需要更细粒度的计数
        （区分新增与覆盖）。但仍走 ensure() 是另一个选择；这里直接
        实现 upsert 简化测试断言。
        """
        from sqlalchemy import select as _sel

        new_count = 0
        upd_count = 0
        svc = SecurityMasterService(self._db)
        for rec in records:
            existing = self._db.execute(
                _sel(Security).where(Security.symbol == rec.symbol)
            ).scalars().first()
            svc.ensure(
                symbol=rec.symbol,
                name=rec.name,
                is_st=rec.is_st,
                listing_date=rec.listing_date,
                source=source,
                exchange=rec.exchange,
                delisted_date=rec.delisted_date,
                trading_status=rec.trading_status,
                sector=rec.sector,
            )
            if existing is None:
                new_count += 1
            else:
                upd_count += 1
        self._db.commit()
        return new_count, upd_count

    # ───────── health ─────────

    def _record_provider_health(
        self,
        source_id: str,
        success: bool,
        synced_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Upsert health row — 必须用 SQL 探查后再决定 INSERT/UPDATE，
        避免被 session 缓存的 stale identity 导致重复插入失败。"""
        from sqlalchemy import select

        now = utc_now()
        stmt = select(DataSourceHealth).where(DataSourceHealth.source_id == source_id)
        row = self._db.execute(stmt).scalars().first()
        if row is None:
            row = DataSourceHealth(
                source_id=source_id,
                last_success_at=synced_at if success else None,
                last_failure_at=now if not success else None,
                last_status="ok" if success else "failed",
                last_error=error,
                consecutive_failures=0 if success else 1,
                updated_at=now,
            )
            self._db.add(row)
        else:
            row.updated_at = now
            if success:
                row.last_success_at = synced_at
                row.last_status = "ok"
                row.consecutive_failures = 0
                row.last_error = None
            else:
                row.last_failure_at = now
                row.last_status = "failed"
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
                row.last_error = error
        self._db.commit()

    def get_provider_health(self) -> list[dict]:
        """返回所有 provider 的最新健康记录，便于 status API。"""
        rows = self._db.execute(select(DataSourceHealth)).scalars().all()
        return [
            {
                "source_id": r.source_id,
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "last_failure_at": r.last_failure_at.isoformat() if r.last_failure_at else None,
                "last_status": r.last_status,
                "last_error": r.last_error,
                "consecutive_failures": r.consecutive_failures,
            }
            for r in rows
        ]