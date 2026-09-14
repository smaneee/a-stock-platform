"""按标的读取已核实的资产负债表与现金流量表字段。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime

import httpx

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "*/*",
}


class StatementDetailError(RuntimeError):
    pass


def _number(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: object) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass(frozen=True)
class StatementDetail:
    symbol: str
    report_date: date | None
    operating_cash_flow: float | None
    capital_expenditure: float | None
    monetary_funds: float | None
    short_loan: float | None
    long_loan: float | None
    bonds_payable: float | None
    noncurrent_liab_due_year: float | None
    lease_liabilities: float | None
    goodwill: float | None
    statement_equity: float | None
    source: str = "eastmoney-f10"

    @property
    def free_cash_flow(self) -> float | None:
        if self.operating_cash_flow is None or self.capital_expenditure is None:
            return None
        return self.operating_cash_flow - self.capital_expenditure

    @property
    def identified_debt(self) -> float | None:
        components = (
            self.short_loan,
            self.long_loan,
            self.bonds_payable,
            self.noncurrent_liab_due_year,
            self.lease_liabilities,
        )
        known = [value for value in components if value is not None]
        return sum(known) if known else None

    @property
    def identified_net_debt(self) -> float | None:
        if self.identified_debt is None or self.monetary_funds is None:
            return None
        return self.identified_debt - self.monetary_funds

    def to_dict(self, *, revenue: float | None, net_profit: float | None) -> dict:
        fcf = self.free_cash_flow
        return {
            "report_date": self.report_date.isoformat() if self.report_date else None,
            "operating_cash_flow": self.operating_cash_flow,
            "capital_expenditure": self.capital_expenditure,
            "free_cash_flow": fcf,
            "fcf_margin": fcf / revenue if fcf is not None and revenue and revenue > 0 else None,
            "ocf_to_profit": (
                self.operating_cash_flow / net_profit
                if self.operating_cash_flow is not None and net_profit and net_profit > 0
                else None
            ),
            "monetary_funds": self.monetary_funds,
            "identified_debt": self.identified_debt,
            "identified_net_debt": self.identified_net_debt,
            "debt_components": {
                "short_loan": self.short_loan,
                "long_loan": self.long_loan,
                "bonds_payable": self.bonds_payable,
                "noncurrent_liab_due_year": self.noncurrent_liab_due_year,
                "lease_liabilities": self.lease_liabilities,
            },
            "goodwill": self.goodwill,
            "statement_equity": self.statement_equity,
            "goodwill_to_equity": (
                self.goodwill / self.statement_equity * 100.0
                if self.goodwill is not None and self.statement_equity and self.statement_equity > 0
                else None
            ),
            "source": self.source,
            "net_debt_note": (
                "已识别净负债 = 短期借款 + 长期借款 + 应付债券 + 一年内到期非流动负债"
                " + 租赁负债 − 货币资金；不包含字段未披露的其他有息负债"
            ),
        }


async def _fetch_report(client: httpx.AsyncClient, symbol: str, report_name: str) -> dict:
    response = await client.get(
        URL,
        params={
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "pageSize": "1",
            "pageNumber": "1",
            "reportName": report_name,
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{symbol}")',
            "source": "WEB",
            "client": "WEB",
        },
    )
    response.raise_for_status()
    payload = response.json()
    rows = ((payload.get("result") or {}).get("data") or [])
    if not payload.get("success") or not rows:
        raise StatementDetailError(f"{report_name} 未返回 {symbol} 的数据")
    return rows[0]


async def fetch_statement_detail(
    symbol: str, *, client: httpx.AsyncClient | None = None
) -> StatementDetail:
    if len(symbol) != 6 or not symbol.isdigit():
        raise StatementDetailError("股票代码必须是 6 位数字")
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0, headers=HEADERS, trust_env=False)
    try:
        balance, cashflow = await asyncio.gather(
            _fetch_report(client, symbol, "RPT_F10_FINANCE_GBALANCE"),
            _fetch_report(client, symbol, "RPT_F10_FINANCE_GCASHFLOW"),
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise StatementDetailError(f"三表接口失败：{type(exc).__name__}") from exc
    finally:
        if owned:
            await client.aclose()
    balance_date = _date(balance.get("REPORT_DATE"))
    cashflow_date = _date(cashflow.get("REPORT_DATE"))
    if balance_date != cashflow_date:
        raise StatementDetailError(
            f"资产负债表与现金流量表报告期不一致：{balance_date} / {cashflow_date}"
        )
    return StatementDetail(
        symbol=symbol,
        report_date=balance_date,
        operating_cash_flow=_number(cashflow.get("NETCASH_OPERATE")),
        capital_expenditure=_number(cashflow.get("CONSTRUCT_LONG_ASSET")),
        monetary_funds=_number(balance.get("MONETARYFUNDS")),
        short_loan=_number(balance.get("SHORT_LOAN")),
        long_loan=_number(balance.get("LONG_LOAN")),
        bonds_payable=_number(balance.get("BOND_PAYABLE")),
        noncurrent_liab_due_year=_number(balance.get("NONCURRENT_LIAB_1YEAR")),
        lease_liabilities=_number(balance.get("LEASE_LIAB")),
        goodwill=_number(balance.get("GOODWILL")),
        statement_equity=_number(balance.get("TOTAL_EQUITY")),
    )
