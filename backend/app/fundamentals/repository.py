"""基本面快照的入库与读取（本包**唯一**写库入口）。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import FundamentalSnapshot
from app.fundamentals.eastmoney_fundamentals import FundamentalSnapshotData
from app.fundamentals.statement_details import StatementDetail
from app.market_rules.session_state import now_cst
from app.time_utils import utc_now


def beijing_today() -> date:
    """北京时间的今天（数据源按北京时间发布，不能用 UTC 推断「今天」）。"""
    return now_cst().date()

#: 可写入的数值列（与模型字段一一对应；symbol/name/report_date/industry 单独处理）
NUMERIC_COLUMNS = (
    "price",
    "market_cap",
    "float_market_cap",
    "pe_dynamic",
    "pb",
    "roe",
    "revenue",
    "revenue_yoy",
    "net_profit_parent",
    "net_profit_yoy",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "eps_diluted",
    "bps",
    "equity",
)


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    snapshot_date: date | None = None

    def to_dict(self) -> dict:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "snapshot_date": self.snapshot_date.isoformat() if self.snapshot_date else None,
        }


def upsert_snapshots(
    db: Session,
    snapshots: list[FundamentalSnapshotData],
    *,
    snapshot_date: date | None = None,
    source: str = "eastmoney",
) -> UpsertResult:
    """按 ``(symbol, snapshot_date, source)`` 更新或插入。同日重复抓取不产生重复行。"""
    day = snapshot_date or beijing_today()
    result = UpsertResult(snapshot_date=day)
    for item in snapshots:
        existing = db.execute(
            select(FundamentalSnapshot).where(
                FundamentalSnapshot.symbol == item.symbol,
                FundamentalSnapshot.snapshot_date == day,
                FundamentalSnapshot.source == source,
            )
        ).scalar_one_or_none()
        if existing is None:
            row = FundamentalSnapshot(
                symbol=item.symbol,
                name=item.name or "",
                snapshot_date=day,
                source=source,
                fetched_at=utc_now(),
            )
            _apply(row, item)
            db.add(row)
            result.inserted += 1
        else:
            _apply(existing, item)
            existing.fetched_at = utc_now()
            result.updated += 1
    db.commit()
    return result


def _apply(row: FundamentalSnapshot, item: FundamentalSnapshotData) -> None:
    row.name = item.name or row.name or ""
    row.report_date = item.report_date
    row.industry = item.industry
    for column in NUMERIC_COLUMNS:
        setattr(row, column, getattr(item, column))
    row.warnings = json.dumps(list(item.warnings), ensure_ascii=False)


def latest_snapshot(db: Session, symbol: str) -> FundamentalSnapshot | None:
    return db.execute(
        select(FundamentalSnapshot)
        .where(FundamentalSnapshot.symbol == symbol)
        .order_by(FundamentalSnapshot.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def apply_statement_detail(
    db: Session, row: FundamentalSnapshot, detail: StatementDetail
) -> FundamentalSnapshot:
    """把同报告期的已核实三表字段写入指定快照。"""
    fields = (
        "operating_cash_flow",
        "capital_expenditure",
        "monetary_funds",
        "short_loan",
        "long_loan",
        "bonds_payable",
        "noncurrent_liab_due_year",
        "lease_liabilities",
        "goodwill",
        "statement_equity",
    )
    for field in fields:
        setattr(row, field, getattr(detail, field))
    row.statement_report_date = detail.report_date
    row.statement_source = detail.source
    row.statement_fetched_at = utc_now()
    db.commit()
    db.refresh(row)
    return row


def snapshot_history(db: Session, symbol: str, limit: int = 60) -> list[FundamentalSnapshot]:
    return list(
        db.execute(
            select(FundamentalSnapshot)
            .where(FundamentalSnapshot.symbol == symbol)
            .order_by(FundamentalSnapshot.snapshot_date.desc())
            .limit(limit)
        ).scalars()
    )


def coverage(db: Session) -> dict:
    """覆盖统计：多少标的、最新抓取日、报告期分布（分析前先看这个）。"""
    total = db.execute(select(func.count(FundamentalSnapshot.symbol))).scalar_one()
    latest_day = db.execute(select(func.max(FundamentalSnapshot.snapshot_date))).scalar_one()
    symbols = db.execute(
        select(func.count(func.distinct(FundamentalSnapshot.symbol)))
    ).scalar_one()
    report_rows = db.execute(
        select(FundamentalSnapshot.report_date, func.count())
        .group_by(FundamentalSnapshot.report_date)
        .order_by(func.count().desc())
    ).all()
    industry_rows = db.execute(
        select(FundamentalSnapshot.industry, func.count())
        .group_by(FundamentalSnapshot.industry)
        .order_by(func.count().desc())
        .limit(15)
    ).all()
    warning_rows = db.execute(
        select(func.count())
        .select_from(FundamentalSnapshot)
        .where(FundamentalSnapshot.warnings != "[]")
    ).scalar_one()
    return {
        "rows": total,
        "symbols": symbols,
        "latest_snapshot_date": latest_day.isoformat() if latest_day else None,
        "report_date_distribution": [
            {"report_date": rd.isoformat() if rd else None, "rows": count}
            for rd, count in report_rows
        ],
        "top_industries": [
            {"industry": name or "(未分类)", "rows": count} for name, count in industry_rows
        ],
        "rows_with_warnings": warning_rows,
    }


def industry_peers(
    db: Session, industry: str, snapshot_date: date | None = None, limit: int = 500
) -> list[FundamentalSnapshot]:
    """同行业、同一抓取日的标的（用于行业相对分位）。默认取库中最新抓取日。"""
    day = snapshot_date or db.execute(
        select(func.max(FundamentalSnapshot.snapshot_date))
    ).scalar_one()
    if day is None:
        return []
    return list(
        db.execute(
            select(FundamentalSnapshot)
            .where(
                FundamentalSnapshot.snapshot_date == day,
                FundamentalSnapshot.industry == industry,
            )
            .limit(limit)
        ).scalars()
    )
