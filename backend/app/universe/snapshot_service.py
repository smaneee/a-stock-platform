"""UniverseSnapshotService — 落库某一交易日股票池快照 + 查询。

设计：
- get_or_create_snapshot(trading_day, source_provider, synced_at): 幂等。
  若当天已有 snapshot → 直接返回；否则基于当前 Security 列表 + ExclusionEngine
  创建新 snapshot + 全部 N 行 UniverseMember（包含与排除都落库）。
- get_membership(trading_day, *, include=bool, exchange, exclude_reasons):
  按交易日查成员；支持过滤。这是 selection / paper trading 调用的 API。
- list_snapshots(limit): 元信息列表，供 API 列出历史。

注意：
- 严格按 trading_day 查，禁止调用「今天」/「current snapshot」。
  即使要最新，也要明确传 trading_day=今天；这是 anti-survivorship-bias 强制约束。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.database.models import (
    Security,  # noqa: F401  ← snapshot 创建时仍需要 Security 拷贝业务字段
    UniverseMember,
    UniverseSnapshot,
)
from app.universe.exclusion import (
    ExclusionEngine,
    classify_long_suspension_status,
    find_long_suspension_symbols,
    get_long_suspension_state,
)
from app.time_utils import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberView:
    """API 返回 / 选股查询使用的成员视图。

    所有字段直接来自 UniverseMember 行（snapshot 时拷贝的不可变字段），
    不再 JOIN 当前 Security 表 — 这是 point-in-time 的硬性保证。
    """

    symbol: str
    name: str
    exchange: str
    is_st: bool
    is_included: bool
    exclude_reason: str | None
    sort_rank: int
    listing_date: date | None = None
    delisted_date: date | None = None
    trading_status: str = "active"


class UniverseSnapshotService:
    """每个实例绑定一个 Session。"""

    def __init__(
        self,
        db: Session,
        include_st: bool = True,
        long_suspension_threshold: int = 30,
    ):
        self._db = db
        self._include_st = include_st
        self._threshold = long_suspension_threshold

    # ───────── create / lookup ─────────

    def get_or_create_snapshot(
        self,
        trading_day: date,
        source_provider: str,
        source_synced_at: datetime | None = None,
    ) -> UniverseSnapshot:
        """幂等：若该 trading_day 已有 snapshot 则返回；否则新建。

        新建时按当前 Security 表的状态做一次全面评估，N 行 N 列。
        """
        existing = (
            self._db.execute(
                select(UniverseSnapshot).where(
                    UniverseSnapshot.trading_day == trading_day
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing

        # 收集所有当前 Security
        all_securities: list[Security] = list(
            self._db.execute(select(Security)).scalars().all()
        )

        # 计算长期停牌集合（合并进 excluded）
        long_susp = find_long_suspension_symbols(
            self._db, as_of=trading_day, threshold_days=self._threshold
        )

        engine = ExclusionEngine(self._db, include_st=self._include_st)
        decisions = engine.evaluate(all_securities, as_of=trading_day)

        snap = UniverseSnapshot(
            trading_day=trading_day,
            total_count=len(all_securities),
            included_count=0,  # 后填
            excluded_count=0,  # 后填
            source_provider=source_provider,
            source_synced_at=source_synced_at,
        )
        self._db.add(snap)
        self._db.flush()  # 拿到 snap.id

        included = 0
        excluded = 0
        for rank, dec in enumerate(decisions):
            # 默认从 ExclusionEngine 取值（delisted/suspended/...）
            final_reason = dec.reason

            # 长期停牌的 3 状态区分（迁移 0009 + 修复 P1）：
            #  - confirmed_long_suspension：is_included=False, exclude_reason="long_suspension"
            #  - incomplete_history      ：审计标签，存到 audit_reason，is_included 保持默认
            #  - provider_unknown        ：审计标签，存到 audit_reason，is_included 保持默认
            #  - ok（无异常）           ：is_included=True,  exclude_reason=None
            long_susp_state = get_long_suspension_state(
                self._db,
                dec.symbol,
                as_of=trading_day,
                threshold_days=self._threshold,
            )
            if long_susp_state == "confirmed_long_suspension":
                # 仅 confirmed 才改变 is_included
                final_reason = "long_suspension"
            # incomplete_history / provider_unknown 是审计标签（不再决定 is_included）
            # 它们会让 UniverseMember.audit_reason 写入（通过 get_membership 暴露），
            # selection / paper trading 可按需进一步过滤

            is_included = final_reason is None

            # 关联 Security — 必须存在（sync 必须先跑过）
            sec = self._db.get(Security, dec.symbol)
            if sec is None:
                # 防御：snapshot 时间点 security 突然不存在（极端 race）
                # 这种情况下 attach 一个 placeholder 不可行，跳过并记日志
                logger.warning("Snapshot skip missing security: %s", dec.symbol)
                continue

            member = UniverseMember(
                snapshot_id=snap.id,
                symbol=dec.symbol,
                security_id=sec.symbol,
                is_included=is_included,
                exclude_reason=final_reason,
                sort_rank=rank,
                # ───────── 不可变业务字段（迁移 0009） ─────────
                # 在 snapshot 时一次性从 Security 拷贝；后续 Security 表的
                # 修改或删除不影响这一行的业务字段。
                name=sec.name,
                exchange=sec.exchange,
                sector=sec.sector,
                is_st=sec.is_st,
                listing_date=sec.listing_date,
                delisted_date=sec.delisted_date,
                trading_status=sec.trading_status,
                # 审计标签：记录 long_suspension 状态（区分 3 种）
                audit_reason=long_susp_state if long_susp_state != "ok" else None,
            )
            self._db.add(member)
            if is_included:
                included += 1
            else:
                excluded += 1

        snap.included_count = included
        snap.excluded_count = excluded
        self._db.commit()
        self._db.refresh(snap)
        return snap

    # ───────── query ─────────

    def list_snapshots(self, limit: int = 50) -> list[dict]:
        """按 trading_day 倒序列出全部历史快照。"""
        rows = (
            self._db.execute(
                select(UniverseSnapshot)
                .order_by(UniverseSnapshot.trading_day.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "trading_day": r.trading_day.isoformat(),
                "total_count": r.total_count,
                "included_count": r.included_count,
                "excluded_count": r.excluded_count,
                "source_provider": r.source_provider,
                "source_synced_at": r.source_synced_at.isoformat()
                if r.source_synced_at
                else None,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    def get_membership(
        self,
        trading_day: date,
        *,
        include_only: bool | None = True,
        exchange: str | None = None,
        exclude_reasons: Sequence[str] | None = None,
        limit: int = 5000,
    ) -> list[MemberView]:
        """按 trading_day 查成员。strict as_of 防幸存者偏差。

        Args:
            include_only:
                - True  → 只返回包含（is_included=True）
                - False → 只返回排除
                - None  → 全部
            exchange: 过滤 SH / SZ / BJ（None 不过滤）
            exclude_reasons: 对 is_included=False 的成员再按 exclude_reason 过滤
        """
        snap = (
            self._db.execute(
                select(UniverseSnapshot).where(
                    UniverseSnapshot.trading_day == trading_day
                )
            )
            .scalars()
            .first()
        )
        if snap is None:
            return []

        # 直接读 UniverseMember 不可变字段，不再 JOIN 当前 Security。
        # 这是迁移 0009 引入的核心修复：防 survivorship bias 漂移。
        stmt = select(UniverseMember).where(UniverseMember.snapshot_id == snap.id)
        if include_only is True:
            stmt = stmt.where(UniverseMember.is_included.is_(True))
        elif include_only is False:
            stmt = stmt.where(UniverseMember.is_included.is_(False))
        if exchange:
            stmt = stmt.where(UniverseMember.exchange == exchange)
        if exclude_reasons:
            stmt = stmt.where(UniverseMember.exclude_reason.in_(exclude_reasons))

        stmt = stmt.order_by(UniverseMember.sort_rank).limit(limit)
        rows = self._db.execute(stmt).scalars().all()
        return [
            MemberView(
                symbol=m.symbol,
                name=m.name or "",
                exchange=m.exchange or "",
                is_st=bool(m.is_st) if m.is_st is not None else False,
                is_included=m.is_included,
                exclude_reason=m.exclude_reason,
                sort_rank=m.sort_rank,
                listing_date=m.listing_date,
                delisted_date=m.delisted_date,
                trading_status=m.trading_status or "active",
            )
            for m in rows
        ]

    def latest_snapshot_date(self) -> date | None:
        """最近一个 snapshot 的交易日期（用于 selection API 默认值）。"""
        row = self._db.execute(
            select(UniverseSnapshot.trading_day)
            .order_by(UniverseSnapshot.trading_day.desc())
            .limit(1)
        ).scalar_one_or_none()
        return row
