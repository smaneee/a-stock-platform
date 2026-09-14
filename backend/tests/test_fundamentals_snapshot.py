"""基本面快照解析与入库测试（用 2026-09-14 实测真实数值做夹具）。"""
from __future__ import annotations

from datetime import date

import httpx

from app.fundamentals.eastmoney_fundamentals import (
    FIELD_MAP,
    REQUEST_FIELDS,
    FetchStats,
    FundamentalSnapshotData,
    annualization_factor,
    fetch_market_fundamentals,
    parse_fundamentals,
)
from app.fundamentals.repository import (
    coverage,
    industry_peers,
    latest_snapshot,
    snapshot_history,
    upsert_snapshots,
)

#: 美的集团 000333 —— 2026-09-14 真实接口返回（同时用 akshare 财务摘要交叉核对过）
MEIDE = {
    "f12": "000333",
    "f14": "美的集团",
    "f2": 87.02,
    "f20": 663904738662,
    "f21": 599166776423,
    "f9": 12.55,
    "f23": 3.13,
    "f37": 11.33,
    "f40": 261052379000.0,
    "f41": 3.4561222865,
    "f45": 26446037000.0,
    "f46": 1.661997971068,
    "f49": 25.2557645483,
    "f57": 64.8900151015,
    "f100": "白色家电",
    "f112": 3.466361973,
    "f113": 27.816806308,
    "f129": 10.2225117134,
    "f135": 225792941000.0,
    "f221": 20260630,
}


def _one(rows: list[dict]) -> FundamentalSnapshotData:
    parsed = parse_fundamentals(rows)
    assert len(parsed) == 1
    return parsed[0]


def test_request_fields_exactly_match_the_verified_field_map():
    assert REQUEST_FIELDS == ",".join(
        sorted({em for em, _ in FIELD_MAP.values()}, key=lambda k: int(k[1:]))
    )
    # 未验证的字段（如 f132/f127/f173）不得出现在请求里
    for unverified in ("f132", "f127", "f173", "f55"):
        assert unverified not in REQUEST_FIELDS.split(",")


def test_parse_real_row_matches_akshare_cross_check():
    row = _one([MEIDE])
    assert row.symbol == "000333" and row.name == "美的集团"
    assert row.report_date == date(2026, 6, 30)
    assert row.revenue == 261052379000.0
    assert row.net_profit_parent == 26446037000.0
    assert row.roe == 11.33
    assert row.gross_margin == 25.2557645483
    assert row.debt_ratio == 64.8900151015
    assert row.bps == 27.816806308
    assert row.net_margin == 10.2225117134
    assert row.equity == 225792941000.0
    assert row.industry == "白色家电"
    assert row.warnings == ()   # 自洽性检查通过 → 无告警


def test_derived_values_reproduce_the_interface_numbers():
    row = _one([MEIDE])
    # 总股本 = 总市值 / 现价 → 与真实股本 76.3 亿股一致（1% 以内）
    assert abs(row.shares_outstanding / 7.63e9 - 1.0) < 0.01
    # 自算动态 PE 必须复现接口的 f9=12.55（差 <0.1%）
    assert abs(row.pe_from_statements / 12.55 - 1.0) < 0.001
    # 自算 PB 必须复现接口的 f23=3.13（差 <0.1%）
    assert abs(row.pb_from_statements / 3.13 - 1.0) < 0.001
    # 年化市销率 = 市值 / (营收 × 2)（半年报年化）
    assert abs(row.ps_annualized - 663904738662 / (261052379000.0 * 2)) < 1e-6
    # 行情接口拿不到现金流与商誉 → 必须为 None，不许用 0 冒充
    assert row.ocf_to_profit is None
    assert row.goodwill_to_equity is None


def test_annualization_factor_by_report_month():
    assert annualization_factor(date(2026, 3, 31)) == 4.0
    assert annualization_factor(date(2026, 6, 30)) == 2.0
    assert abs(annualization_factor(date(2026, 9, 30)) - 4.0 / 3.0) < 1e-9
    assert annualization_factor(date(2026, 12, 31)) == 1.0
    assert annualization_factor(None) is None
    assert annualization_factor(date(2026, 7, 15)) is None   # 非标准报告期 → 不猜


def test_rows_without_symbol_price_or_market_cap_are_skipped():
    assert parse_fundamentals([]) == []
    assert parse_fundamentals([{**MEIDE, "f12": "00033"}]) == []          # 代码长度不对
    assert parse_fundamentals([{**MEIDE, "f12": "ABCDEF"}]) == []
    assert parse_fundamentals([{**MEIDE, "f2": 0}]) == []                 # 停牌/无价
    assert parse_fundamentals([{**MEIDE, "f2": "-"}]) == []
    assert parse_fundamentals([{**MEIDE, "f20": None}]) == []             # 无市值
    assert len(parse_fundamentals([MEIDE, {"f12": "600519"}])) == 1       # 半截行被跳过


def test_dash_and_bad_values_become_none_not_zero():
    row = _one([{**MEIDE, "f37": "-", "f49": None, "f221": "2026-06-30"}])
    assert row.roe is None
    assert row.gross_margin is None
    assert row.report_date is None          # 非 YYYYMMDD → 不解析
    assert any("缺少报告期" in w for w in row.warnings)


def test_inconsistent_pe_is_flagged_not_silently_used():
    row = _one([{**MEIDE, "f9": 20.0}])
    assert any("自洽性偏差" in w for w in row.warnings)
    row_ok = _one([MEIDE])
    assert not any("自洽性偏差" in w for w in row_ok.warnings)


def test_missing_revenue_or_profit_is_flagged():
    row = _one([{**MEIDE, "f40": None}])
    assert any("缺少营收或净利润" in w for w in row.warnings)


# ── 入库 ────────────────────────────────────────────────────────────────────


def test_upsert_is_idempotent_within_the_same_day(db_session):
    rows = parse_fundamentals([MEIDE])
    first = upsert_snapshots(db_session, rows, snapshot_date=date(2026, 9, 14))
    assert first.inserted == 1 and first.updated == 0

    changed = parse_fundamentals([{**MEIDE, "f2": 88.0, "f20": 671000000000}])
    second = upsert_snapshots(db_session, changed, snapshot_date=date(2026, 9, 14))
    assert second.inserted == 0 and second.updated == 1

    latest = latest_snapshot(db_session, "000333")
    assert latest is not None
    assert latest.price == 88.0                      # 同日更新而非追加
    assert latest.fetched_at is not None
    assert len(snapshot_history(db_session, "000333")) == 1


def test_different_days_create_separate_rows(db_session):
    rows = parse_fundamentals([MEIDE])
    upsert_snapshots(db_session, rows, snapshot_date=date(2026, 9, 11))
    upsert_snapshots(db_session, rows, snapshot_date=date(2026, 9, 14))
    history = snapshot_history(db_session, "000333")
    assert [r.snapshot_date for r in history] == [date(2026, 9, 14), date(2026, 9, 11)]


def test_coverage_reports_facts_for_cross_section_use(db_session):
    rows = parse_fundamentals([MEIDE, {**MEIDE, "f12": "600519", "f14": "贵州茅台"}])
    upsert_snapshots(db_session, rows, snapshot_date=date(2026, 9, 14))
    stats = coverage(db_session)
    assert stats["rows"] == 2 and stats["symbols"] == 2
    assert stats["latest_snapshot_date"] == "2026-09-14"
    assert stats["report_date_distribution"] == [
        {"report_date": "2026-06-30", "rows": 2}
    ]
    assert stats["top_industries"][0]["industry"] == "白色家电"
    assert stats["rows_with_warnings"] == 0


def test_industry_peers_filters_by_industry_and_day(db_session):
    rows = parse_fundamentals(
        [MEIDE, {**MEIDE, "f12": "600519", "f14": "贵州茅台", "f100": "酿酒行业"}]
    )
    upsert_snapshots(db_session, rows, snapshot_date=date(2026, 9, 14))
    peers = industry_peers(db_session, "白色家电")
    assert [p.symbol for p in peers] == ["000333"]
    assert industry_peers(db_session, "不存在的行业") == []


# ── 上游不稳定的应对（2026-09-14 实测第 44 页 ReadError 只抓到 4087/5913） ─────


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FlakyClient:
    """前 N 次调用抛 ReadError，之后返回一行数据。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def get(self, url: str, params: dict | None = None) -> _FakeResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ReadError("模拟上游瞬时断连")
        return _FakeResponse({"data": {"total": 1, "diff": [MEIDE]}})

    async def aclose(self) -> None:  # pragma: no cover - 由 fetcher 决定是否调用
        return None


def test_fetch_retries_failed_page_instead_of_giving_up():
    import asyncio

    import httpx as _httpx

    client = _FlakyClient(fail_times=2)   # 第 1 轮两个主机都失败 → 第 2 轮成功
    rows, stats = asyncio.run(
        fetch_market_fundamentals(page_retries=2, pause_seconds=0, client=client)
    )
    assert len(rows) == 1
    assert stats.retries == 1
    assert stats.failures == []
    assert stats.coverage_ratio == 1.0
    assert stats.to_dict()["complete"] is True


def test_fetch_reports_partial_coverage_and_never_claims_complete():
    import asyncio

    class _DeadClient:
        async def get(self, url, params=None):
            raise OSError("全主机不可用")

        async def aclose(self):
            return None

    rows, stats = asyncio.run(
        fetch_market_fundamentals(page_retries=1, pause_seconds=0, client=_DeadClient())
    )
    assert rows == []
    assert stats.failures and "仍失败" in stats.failures[0]
    assert stats.to_dict()["complete"] is False
    assert stats.coverage_ratio is None


def test_stats_to_dict_counts_skipped_rows():
    stats = FetchStats(rows_raw=100, rows_parsed=97, rows_skipped=3, total_reported=5913)
    payload = stats.to_dict()
    assert payload["rows_skipped"] == 3
    assert payload["coverage_ratio"] == 0.0164
    assert payload["retrieval_ratio"] == 0.0169
    assert payload["complete"] is False


def test_complete_measures_retrieval_not_parsing():
    """取回完整但有意跳过若干行时，不应被误报成"抓取不完整"。"""
    stats = FetchStats(
        pages_fetched=60, rows_raw=5913, rows_parsed=5550, rows_skipped=363,
        total_reported=5913,
    )
    payload = stats.to_dict()
    assert payload["complete"] is True
    assert payload["retrieval_ratio"] == 1.0
    assert payload["coverage_ratio"] == 0.9386
