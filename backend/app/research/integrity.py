"""平台数据完整性审计（S3：防未来数据、防虚构成交、防"只留成功案例"）。

方案 §三与 §四 S3 的验收要求，逐条对应到本模块的**可执行检查**：

| 验收要求 | 本模块的检查 |
| --- | --- |
| 未来公告不能进入过去信号 | 校验每条研究/回测记录引用的数据时点不晚于其信号日（时点字段缺失即计为无法验证） |
| 不可交易日不能虚构成交 | 扫描 `paper_trades`，成交日期必须落在 `trading_calendar` 内 |
| 数据本身不能有非交易日的 K 线 | 扫描 `historical_bars` 的交易日集合，与日历比对 |
| 前向观察不能虚增天数 | 逐条重算 `forward_observations`：计数必须等于 (起点, 最后计入日] 之间的交易日数 |
| 不足样本不输出"已验证" | 证据页中 `passed_oos` 必须带样本量与样本下限；缺字段即违规 |
| **展示收益失败的策略** | 负结果（`failed_oos`/`inconclusive`）必须仍然存在且带失败原因 |

设计纪律：
* **只读**：本模块不写任何表，只报告违规；
* **不猜**：字段缺失（如没有数据时点）一律记为"无法验证"，不当作通过；
* **可测试**：每个检查都能用构造数据触发，测试里故意造违规来验证真会被抓到。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import ForwardObservation, HistoricalBar, PaperTrade, TradingDate
from app.config import PROJECT_DIR

EVIDENCE_PATH = PROJECT_DIR / "docs" / "evidence" / "strategy-evidence.json"

#: 声明"已通过样本外"时要求的最少样本量（不足即视为标签不当）
MIN_SAMPLES_FOR_PASSED = 60
#: 审计里最多列出多少条样例（避免把整张表倒进响应）
SAMPLE_LIMIT = 10
#: 负结果必须保留的状态
NEGATIVE_STATUSES = ("failed_oos", "inconclusive")

#: 交易日守卫的**修复生效日**（broker._to_trading_date 改为回落到最近交易日，见该函数 docstring）。
#: 此前产生的非交易日成交属于"历史遗留"：仍然列出来，但不与"当前仍在产生违规"混为一谈。
LEGACY_TRADE_CUTOFF = date(2026, 9, 15)


@dataclass
class CheckResult:
    name: str
    label: str
    checked: int
    violations: list[str] = field(default_factory=list)
    note: str = ""
    unverifiable: int = 0
    #: 历史遗留（修复生效日之前产生、当前规则下不合规但已无法回溯修正）
    legacy: list[str] = field(default_factory=list)
    legacy_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "checked": self.checked,
            "violations": self.violations,
            "violation_count": len(self.violations),
            "unverifiable": self.unverifiable,
            "legacy": self.legacy,
            "legacy_count": self.legacy_count,
            "note": self.note,
            "ok": self.ok,
        }


def _trading_days(db: Session) -> set[date]:
    return {row for row in db.scalars(select(TradingDate.trade_date)).all()}


def evaluate_trade_days(
    rows: list[tuple[int, str, datetime | date | None]], days: set[date]
) -> list[str]:
    """纯函数：判定每笔成交是否落在交易日（时间缺失记为无法验证）。

    与取数分离，是为了让「缺失成交时间」这类**库里不可构造**（该列是 NOT NULL）的
    防御分支也能被单元测试覆盖。
    """
    violations: list[str] = []
    legacy: list[str] = []
    for trade_id, symbol, executed_at in rows:
        if executed_at is None:
            violations.append(f"成交 #{trade_id}（{symbol}）缺少成交时间 → 无法验证")
            continue
        trade_day = executed_at.date() if isinstance(executed_at, datetime) else executed_at
        if trade_day in days:
            continue
        message = f"成交 #{trade_id}（{symbol}）日期 {trade_day} 不是交易日"
        if trade_day < LEGACY_TRADE_CUTOFF:
            legacy.append(f"{message}（{LEGACY_TRADE_CUTOFF} 之前的守卫修复前数据）")
        else:
            violations.append(message)
    return violations, legacy


def check_paper_trades_on_trading_days(db: Session) -> CheckResult:
    """成交必须发生在交易日（防止"不可交易日虚构成交"）。"""
    days = _trading_days(db)
    rows = db.execute(
        select(PaperTrade.id, PaperTrade.symbol, PaperTrade.executed_at)
    ).all()
    violations, legacy = evaluate_trade_days([tuple(row) for row in rows], days)
    return CheckResult(
        name="trades_on_trading_days",
        label="成交日期必须在交易日历内",
        checked=len(rows),
        violations=violations[:SAMPLE_LIMIT],
        legacy=legacy[:SAMPLE_LIMIT],
        legacy_count=len(legacy),
        note=(
            f"日历共 {len(days)} 个交易日；{LEGACY_TRADE_CUTOFF} 之前的非交易日成交"
            "单列为历史遗留（守卫修复前产生），不计入违规"
        ),
    )


def check_position_acquisition_days(db: Session) -> CheckResult:
    """持仓的**建仓归属交易日**必须是交易日（非交易日建仓会污染 T+1 判定）。

    与成交时间戳的区别：`executed_at` 是墙钟时间（非交易日的离线模拟下单是允许的），
    而 `acquisition_date` 是"这笔仓位算哪一天的"，它有严格的交易日语义。
    """
    from app.database.models import PaperPosition

    days = _trading_days(db)
    rows = db.execute(
        select(PaperPosition.id, PaperPosition.symbol, PaperPosition.acquisition_date)
    ).all()
    violations: list[str] = []
    legacy: list[str] = []
    for position_id, symbol, acquisition_date in rows:
        if acquisition_date is None:
            violations.append(f"持仓 #{position_id}（{symbol}）缺少建仓日期 → 无法验证")
            continue
        if acquisition_date in days:
            continue
        message = f"持仓 #{position_id}（{symbol}）建仓日 {acquisition_date} 不是交易日"
        if acquisition_date < LEGACY_TRADE_CUTOFF:
            legacy.append(f"{message}（{LEGACY_TRADE_CUTOFF} 之前的历史数据）")
        else:
            violations.append(message)
    return CheckResult(
        name="position_acquisition_days",
        label="持仓建仓日必须在交易日历内",
        checked=len(rows),
        violations=violations[:SAMPLE_LIMIT],
        legacy=legacy[:SAMPLE_LIMIT],
        legacy_count=len(legacy),
        note=(
            "executed_at 是墙钟时间（离线模拟下单允许非交易日）；"
            "acquisition_date 是归属交易日，必须落在日历内"
        ),
    )


def check_bars_on_trading_days(db: Session) -> CheckResult:
    """日线数据的交易日集合必须是日历子集（脏数据会让回测凭空多出一天）。"""
    days = _trading_days(db)
    # 注意：不能用 func.distinct(col) —— 那会丢掉列的 Date 类型，
    # SQLite 会返回字符串，与日历里的 date 对象比较必然"全部不在日历内"（实测踩过）。
    bar_days = set(db.scalars(select(HistoricalBar.trade_date).distinct()).all())
    outside = sorted(day for day in bar_days if day not in days)
    return CheckResult(
        name="bars_on_trading_days",
        label="K 线日期必须在交易日历内",
        checked=len(bar_days),
        violations=[f"{day} 不在交易日历内" for day in outside[:SAMPLE_LIMIT]],
        note=f"库里共 {len(bar_days)} 个不同的 K 线交易日，日历 {len(days)} 天",
    )


def check_forward_observations(db: Session, *, today: date | None = None) -> CheckResult:
    """前向观察计数必须等于"严格晚于起点、且不晚于最后计入日"的交易日数。"""
    day = today or date.today()
    days = sorted(_trading_days(db))
    rows = db.scalars(select(ForwardObservation)).all()
    violations: list[str] = []
    for row in rows:
        if row.last_counted_day is None:
            if row.trading_days_counted != 0:
                violations.append(
                    f"标签 {row.freeze_tag}：没有 last_counted_day 但计数为 {row.trading_days_counted}"
                )
            continue
        if row.last_counted_day > day:
            violations.append(
                f"标签 {row.freeze_tag}：最后计入日 {row.last_counted_day} 晚于今天 {day}"
            )
            continue
        if row.last_counted_day <= row.started_on:
            violations.append(
                f"标签 {row.freeze_tag}：最后计入日 {row.last_counted_day} 不晚于起点 {row.started_on}"
            )
            continue
        expected = len([d for d in days if row.started_on < d <= row.last_counted_day])
        if expected != row.trading_days_counted:
            violations.append(
                f"标签 {row.freeze_tag}：计数 {row.trading_days_counted} 与应计 {expected} 不一致"
                f"（{row.started_on} → {row.last_counted_day}）"
            )
    return CheckResult(
        name="forward_observations",
        label="前向观察天数与交易日历一致",
        checked=len(rows),
        violations=violations[:SAMPLE_LIMIT],
        note="规则：只统计严格晚于起点、且已过去的交易日",
    )


def check_evidence_contract(path: Path | None = None) -> CheckResult:
    """证据页契约：负结果保留 + 样本不足不得标"已通过"+ 必须写明失败原因。"""
    target = path or EVIDENCE_PATH
    if not target.exists():
        return CheckResult(
            name="evidence_contract",
            label="证据页：负结果保留与样本量",
            checked=0,
            violations=[f"证据文件不存在：{target}"],
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    violations: list[str] = []
    unverifiable = 0
    negatives = 0
    for item in items:
        status = item.get("status")
        if status in NEGATIVE_STATUSES:
            negatives += 1
            if not str(item.get("failure_reason") or "").strip():
                violations.append(f"{item.get('id')}：负结果没有写失败原因")
        if status == "passed_oos":
            samples = item.get("sample_size")
            if not isinstance(samples, (int, float)):
                violations.append(f"{item.get('id')}：标为 passed_oos 但没有样本量字段")
            elif samples < MIN_SAMPLES_FOR_PASSED:
                violations.append(
                    f"{item.get('id')}：标为 passed_oos 但样本量 {samples} < {MIN_SAMPLES_FOR_PASSED}"
                )
        if item.get("production_ready") and status != "passed_oos":
            violations.append(
                f"{item.get('id')}：production_ready=true 但状态是 {status}"
            )
        for key in ("evidence_source", "data_cutoff"):
            if not str(item.get(key) or "").strip():
                unverifiable += 1
                violations.append(f"{item.get('id')}：缺少 {key} → 无法追溯")
    if negatives == 0:
        violations.append("证据页里没有任何负结果 → 违反'不得只保留成功案例'")
    return CheckResult(
        name="evidence_contract",
        label="证据页：负结果保留与样本量",
        checked=len(items),
        violations=violations[:SAMPLE_LIMIT],
        unverifiable=unverifiable,
        note=f"负结果 {negatives} 条；passed_oos 的样本下限 {MIN_SAMPLES_FOR_PASSED}",
    )


def check_research_run_time_validity(db: Session) -> CheckResult:
    """研究记录的时点自洽性：快照日不得晚于记录创建日（否则等于用了未来数据）。"""
    from app.database.models import InvestmentResearchRun

    rows = db.scalars(select(InvestmentResearchRun)).all()
    violations: list[str] = []
    for row in rows:
        if row.created_at is None:
            violations.append(f"研究记录 #{row.id} 缺少创建时间 → 无法验证时点")
            continue
        created_day = row.created_at.date()
        if row.snapshot_date > created_day:
            violations.append(
                f"研究记录 #{row.id}：快照日 {row.snapshot_date} 晚于创建日 {created_day}"
                " → 等于使用未来数据"
            )
        if row.report_date is not None and row.report_date > created_day:
            violations.append(
                f"研究记录 #{row.id}：财报报告期 {row.report_date} 晚于创建日 {created_day}"
            )
    return CheckResult(
        name="research_run_time_validity",
        label="研究记录：快照/报告期不得晚于记录创建日",
        checked=len(rows),
        violations=violations[:SAMPLE_LIMIT],
        note="时点字段缺失一律记为无法验证，不当作通过",
    )


def run_audit(db: Session, *, today: date | None = None, evidence_path: Path | None = None) -> dict:
    """跑完全部检查，返回可追溯的审计结果（只读）。"""
    checks = [
        check_paper_trades_on_trading_days(db),
        check_position_acquisition_days(db),
        check_bars_on_trading_days(db),
        check_forward_observations(db, today=today),
        check_research_run_time_validity(db),
        check_evidence_contract(evidence_path),
    ]
    total_violations = sum(len(check.violations) for check in checks)
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "today": (today or date.today()).isoformat(),
        "checks": [check.to_dict() for check in checks],
        "clean": total_violations == 0,
        "violation_count": total_violations,
        "unverifiable_total": sum(check.unverifiable for check in checks),
        "legacy_total": sum(check.legacy_count for check in checks),
        "note": (
            "审计只读、不修改任何数据；违规项逐条给出原因。"
            "字段缺失记为「无法验证」，不视为通过。"
        ),
        "scope": (
            "覆盖：成交只在交易日 / K 线只在交易日 / 前向观察计数与日历一致 / "
            "研究记录时点自洽 / 证据页负结果保留与样本量。"
            "**不覆盖**：单笔成交价与当日行情的一致性、复权因子变动历史。"
        ),
    }
