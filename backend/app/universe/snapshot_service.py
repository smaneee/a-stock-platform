"""UniverseSnapshotService — 落库某一交易日股票池快照 + 查询。

设计：
- get_or_create_snapshot(trading_day, source_provider, synced_at, *, as_of_date):
  幂等 + 冲突检测。
  - 若当天已有 snapshot 且 source_provider / total_count / source_synced_at 完全
    一致 → 直接返回旧版（同内容幂等）
  - 若已有 snapshot 但任一字段不同 → 抛 SnapshotConflictError（让调用方显式决定
    revise / skip，不能静默返回旧版）
  - 否则创建新 snapshot + 全部 N 行 UniverseMember

- 单事务约束：本服务的所有方法都只 flush() 不 commit()，由调用方（sync_service
  或 API 层）控制 commit 边界。失败时整个事务回滚，Security 写入也会一起撤销。

- point-in-time 约束：trading_day 不能晚于 as_of_date+1，也不能早于 provider 的
  earliest data date。禁止用当前数据生成过去快照。

- 防 survivorship bias：member 的业务字段（name / exchange / board / sector /
  is_st / listing_date / delisted_date / trading_status）全部从 Security 拷贝，
  不 JOIN 当前 Security 表。Security 修改/删除不影响历史 member。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    Security,  # noqa: F401  ← snapshot 创建时仍需要 Security 拷贝业务字段
    UniverseMember,
    UniverseSnapshot,
)
from app.universe.exclusion import (
    ExclusionEngine,
    find_long_suspension_symbols,
    get_long_suspension_state,
)
from app.time_utils import utc_now

logger = logging.getLogger(__name__)


class SnapshotConflictError(Exception):
    """同 trading_day 已有 snapshot 但内容（source_provider / total_count /
    source_synced_at）发生变化。

    不允许静默返回旧版：调用方必须显式决定 revise / skip / overwrite。
    """


class InvalidTradingDayError(Exception):
    """trading_day 违反 point-in-time 约束（晚于 as_of_date+1 或早于最早数据日）。"""


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
    board: str = "unknown"
    listing_date: date | None = None
    delisted_date: date | None = None
    trading_status: str = "active"
    audit_reason: str | None = None


class UniverseSnapshotService:
    """每个实例绑定一个 Session。

    重要：本服务**不调用 self._db.commit()**。所有写操作都用 flush()，
    事务边界由调用方控制。这是与 sync_service 真正原子的关键。
    """

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
        *,
        as_of_date: date | None = None,
        total_count_hint: int | None = None,
        force_overwrite: bool = False,
    ) -> UniverseSnapshot:
        """幂等 + 冲突检测版 snapshot 创建。

        Args:
            trading_day: 交易日 YYYY-MM-DD
            source_provider: 主数据源（akshare / mock）
            source_synced_at: 主数据拉取时间（UTC）
            as_of_date: Provider 拉数据的截止日（ProviderError 必传；point-in-time 校验用）
            total_count_hint: 拉取记录数（用于冲突检测；None 时不校验 total_count）
            force_overwrite: True → 冲突时静默覆盖；False → 抛 SnapshotConflictError

        Raises:
            InvalidTradingDayError: trading_day > as_of_date+1 或 trading_day 太早
            SnapshotConflictError: 同 day 内容变化且未 force_overwrite
        """
        # ───── point-in-time 校验 ─────
        self._validate_trading_day(trading_day, as_of_date)

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
            # ───── 同 day 冲突检测 ─────
            same_source = existing.source_provider == source_provider
            same_synced = (
                source_synced_at is None
                or existing.source_synced_at == source_synced_at
            )
            same_total = (
                total_count_hint is None
                or existing.total_count == total_count_hint
            )
            if same_source and same_synced and same_total:
                # 内容完全一致 → 幂等返回
                return existing
            if not force_overwrite:
                raise SnapshotConflictError(
                    f"trading_day={trading_day} 已有 snapshot 但内容变化："
                    f"existing=(provider={existing.source_provider}, "
                    f"synced_at={existing.source_synced_at}, total={existing.total_count}) vs "
                    f"new=(provider={source_provider}, synced_at={source_synced_at}, "
                    f"total={total_count_hint})；调用 force_overwrite=True 显式覆盖，"
                    "或调用 delete_snapshot(trading_day) 先清空"
                )
            # force_overwrite：先删旧的，递归创建新的
            logger.warning(
                "snapshot trading_day=%s 强制覆盖：existing=(p=%s, t=%s) vs new=(p=%s, t=%s)",
                trading_day,
                existing.source_provider,
                existing.total_count,
                source_provider,
                total_count_hint,
            )
            self._db.delete(existing)
            self._db.flush()

        # 收集所有当前 Security
        all_securities: list[Security] = list(
            self._db.execute(select(Security)).scalars().all()
        )

        # 计算长期停牌集合（仅当有 history_ingest_batches 覆盖时才判 confirmed）
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
        self._db.flush()  # 拿到 snap.id；不 commit

        included = 0
        excluded = 0
        for rank, dec in enumerate(decisions):
            final_reason = dec.reason

            # 长期停牌的 3 状态：
            #  - confirmed_long_suspension：is_included=False, exclude_reason="long_suspension"
            #  - incomplete_history      ：审计标签，audit_reason 记录，is_included 不变
            #  - provider_unknown        ：审计标签，audit_reason 记录，is_included 不变
            long_susp_state = get_long_suspension_state(
                self._db,
                dec.symbol,
                as_of=trading_day,
                threshold_days=self._threshold,
            )
            if long_susp_state == "confirmed_long_suspension":
                final_reason = "long_suspension"

            # 组合 audit_reason：long_susp 状态 + listing_date 缺失
            audit_bits: list[str] = []
            if long_susp_state != "ok":
                audit_bits.append(long_susp_state)
            # 这里需要 sec 来判断 listing_date；先在循环内取一次
            sec = self._db.get(Security, dec.symbol)
            if sec is None:
                logger.warning("Snapshot skip missing security: %s", dec.symbol)
                continue
            if sec.listing_date is None:
                audit_bits.append("listing_date_unknown")

            is_included = final_reason is None

            member = UniverseMember(
                snapshot_id=snap.id,
                symbol=dec.symbol,
                security_id=sec.symbol,
                is_included=is_included,
                exclude_reason=final_reason,
                sort_rank=rank,
                # ───────── 不可变业务字段（迁移 0009/0010） ─────────
                name=sec.name,
                exchange=sec.exchange,
                board=sec.board,  # 迁移 0010
                sector=sec.sector,
                is_st=sec.is_st,
                listing_date=sec.listing_date,
                delisted_date=sec.delisted_date,
                trading_status=sec.trading_status,
                audit_reason=",".join(audit_bits) if audit_bits else None,
            )
            self._db.add(member)
            if is_included:
                included += 1
            else:
                excluded += 1

        snap.included_count = included
        snap.excluded_count = excluded
        # 注意：这里不 commit。调用方在 sync 顶层统一 commit / rollback
        self._db.flush()
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

        直接读 UniverseMember 不可变字段，不再 JOIN 当前 Security。
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
                board=m.board or "unknown",
                listing_date=m.listing_date,
                delisted_date=m.delisted_date,
                trading_status=m.trading_status or "active",
                audit_reason=m.audit_reason,
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

    # ───────── internal ─────────

    @staticmethod
    def _validate_trading_day(trading_day: date, as_of_date: date | None) -> None:
        """point-in-time 校验：trading_day 不能晚于 as_of_date+1，也不能早于 1990-12-19。

        晚于 as_of_date+1 → 未来日期（伪造未来快照）
        早于 1990-12-19 → 早于 A 股最早交易日
        """
        # 1) trading_day 不能晚于 as_of_date + 1 天（容忍时区 + 收盘后当晚算次日）
        if as_of_date is not None and trading_day > as_of_date + timedelta(days=1):
            raise InvalidTradingDayError(
                f"trading_day={trading_day} 晚于 as_of_date={as_of_date}+1（伪造未来快照）"
            )
        # 2) trading_day 不能晚于今天 + 1（无 as_of_date 时的兜底）
        if as_of_date is None and trading_day > utc_now().date() + timedelta(days=1):
            raise InvalidTradingDayError(
                f"trading_day={trading_day} 晚于今天+1（伪造未来快照）"
            )
        # 3) A 股最早交易日 1990-12-19（上海老八股）
        earliest = date(1990, 12, 19)
        if trading_day < earliest:
            raise InvalidTradingDayError(
                f"trading_day={trading_day} 早于 A 股最早交易日 {earliest}"
            )
