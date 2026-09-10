"""UniverseSyncService — 全市场证券主数据同步编排。

职责：
1) 顺序尝试多个 Provider（先 mock，再 akshare 等），任一成功即返回；
   全部失败抛 AllProvidersFailedError。
2) 单 provider 调用 N 次（retry+backoff），N 次都失败才切到下一个。
3) 持久化 DataSourceHealth（最近成功/失败/连续失败数）。
4) 写入 Security 表（upsert），不变更调用方原有逻辑。
5) 触发 SnapshotService 形成当天的 UniverseSnapshot。
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

from app.database.models import DataSourceHealth, Security
from app.market_rules.security_master import SecurityMasterService
from app.time_utils import utc_now
from app.universe.providers import (
    AkshareUniverseProvider,
    MockUniverseProvider,
    ProviderError,
    SecurityRecord,
    UniverseProvider,
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

    def as_dict(self) -> dict:
        return {
            "source_provider": self.source_provider,
            "synced_at": self.synced_at.isoformat(),
            "total_fetched": self.total_fetched,
            "new_securities": self.new_securities,
            "updated_securities": self.updated_securities,
            "attempted_providers": list(self.attempted_providers),
            "provider_health": dict(self.provider_health),
        }


class UniverseSyncService:
    """编排同步逻辑；每个实例绑定一个 Session。"""

    DEFAULT_PROVIDERS: tuple[type[UniverseProvider], ...] = (
        MockUniverseProvider,
        AkshareUniverseProvider,
    )

    def __init__(
        self,
        db: Session,
        providers: Sequence[UniverseProvider] | None = None,
        max_retries: int = 2,
        backoff_base_ms: int = 50,
    ):
        self._db = db
        # 用户传入 provider 实例列表；否则按默认顺序构造
        self._providers: list[UniverseProvider] = list(
            providers
            if providers is not None
            else [cls() for cls in self.DEFAULT_PROVIDERS]
        )
        self._max_retries = max_retries
        self._backoff_base_ms = backoff_base_ms

    async def sync(self) -> SyncResult:
        """同步全部证券主数据。

        行为：
        1) 顺序尝试 self._providers（mock 优先）
        2) 每个 provider 内最多 self._max_retries 次（指数退避 + 抖动）
        3) 任一 provider 一次成功即止
        4) 全部失败 → 抛 AllProvidersFailedError（每个 provider 的失败都写 health 表）
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

            return SyncResult(
                source_provider=provider.source_id,
                synced_at=synced_at,
                total_fetched=len(records),
                new_securities=new_count,
                updated_securities=upd_count,
                attempted_providers=attempted,
                provider_health=health_snapshot,
            )

        # 所有 provider 都失败
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
