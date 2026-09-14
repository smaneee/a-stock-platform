"""三表明细：字段解析、派生口径与报告期一致性。"""
from __future__ import annotations

import asyncio

import pytest

from app.fundamentals.statement_details import (
    StatementDetailError,
    fetch_statement_detail,
)


class _Response:
    status_code = 200

    def __init__(self, row: dict) -> None:
        self.row = row

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"success": True, "result": {"data": [self.row]}}


class _Client:
    def __init__(self, balance: dict, cashflow: dict) -> None:
        self.balance = balance
        self.cashflow = cashflow

    async def get(self, _url, params):
        row = self.balance if params["reportName"].endswith("GBALANCE") else self.cashflow
        return _Response(row)


def test_statement_detail_parses_and_derives_verified_values():
    balance = {
        "REPORT_DATE": "2026-06-30 00:00:00",
        "MONETARYFUNDS": 90.0,
        "SHORT_LOAN": 40.0,
        "LONG_LOAN": 20.0,
        "BOND_PAYABLE": 10.0,
        "NONCURRENT_LIAB_1YEAR": 5.0,
        "LEASE_LIAB": 2.0,
        "GOODWILL": 12.0,
        "TOTAL_EQUITY": 120.0,
    }
    cashflow = {
        "REPORT_DATE": "2026-06-30 00:00:00",
        "NETCASH_OPERATE": 30.0,
        "CONSTRUCT_LONG_ASSET": 6.0,
    }
    detail = asyncio.run(
        fetch_statement_detail("000333", client=_Client(balance, cashflow))
    )
    payload = detail.to_dict(revenue=200.0, net_profit=20.0)
    assert payload["free_cash_flow"] == 24.0
    assert payload["fcf_margin"] == 0.12
    assert payload["ocf_to_profit"] == 1.5
    assert payload["identified_debt"] == 77.0
    assert payload["identified_net_debt"] == -13.0
    assert payload["goodwill_to_equity"] == 10.0
    assert "不包含" in payload["net_debt_note"]


def test_statement_detail_rejects_mismatched_report_dates():
    balance = {"REPORT_DATE": "2026-06-30 00:00:00"}
    cashflow = {"REPORT_DATE": "2026-03-31 00:00:00"}
    with pytest.raises(StatementDetailError, match="报告期不一致"):
        asyncio.run(fetch_statement_detail("000333", client=_Client(balance, cashflow)))
