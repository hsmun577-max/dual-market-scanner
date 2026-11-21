"""Read US issuer fundamentals from SEC XBRL company facts.

Yahoo Finance quoteSummary is rate limited and sometimes answers 404 ("No fundamentals
data found for symbol") for a perfectly valid ticker. When that happens the quarterly
statement the forecast needs still exists at the SEC, which is the primary source for US
issuers. This module reads only reported values: a concept the filing does not carry stays
missing, and nothing is interpolated or filled from a different period.
"""

import math
from datetime import date, datetime, timedelta

import requests

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC's fair-access policy rejects a User-Agent without a contact address (HTTP 403),
# which is what made every EDGAR lookup in the Actions log fail. The address is a project
# alias, not a person; SEC_CONTACT_EMAIL overrides it when the owner wants their own.
DEFAULT_CONTACT = "scanner@invalid.example"


def user_agent():
    import os
    contact = os.getenv("SEC_CONTACT_EMAIL", "").strip() or DEFAULT_CONTACT
    return f"Dual Market Scanner research {contact}"

REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet")
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
NET_INCOME_TAGS = ("NetIncomeLoss",)
OCF_TAGS = ("NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
EQUITY_TAGS = ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
DEBT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebt")
DEBT_CURRENT_TAGS = ("LongTermDebtCurrent", "DebtCurrent")
CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")

QUARTER_MIN_DAYS, QUARTER_MAX_DAYS = 75, 105


def _number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def _unit_facts(document, tags, unit="USD"):
    """All facts of the first tag that reports values in ``unit``."""
    facts = document.get("facts", {}) if isinstance(document, dict) else {}
    for tag in tags:
        node = facts.get("us-gaap", {}).get(tag) or facts.get("ifrs-full", {}).get(tag)
        if not node:
            continue
        rows = node.get("units", {}).get(unit)
        if rows:
            return tag, rows
    return None, []


def _duration_days(row):
    try:
        start = date.fromisoformat(row["start"])
        end = date.fromisoformat(row["end"])
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).days


def quarterly_rows(rows, today):
    """Facts covering about one quarter and already filed, latest period first."""
    picked = []
    for row in rows:
        days = _duration_days(row)
        value = _number(row.get("val"))
        if days is None or not QUARTER_MIN_DAYS <= days <= QUARTER_MAX_DAYS or value is None:
            continue
        try:
            end = date.fromisoformat(row["end"])
        except (KeyError, ValueError):
            continue
        if end > today:
            continue
        picked.append((end, value, row))
    picked.sort(key=lambda item: item[0])
    return picked


def instant_rows(rows, today):
    picked = []
    for row in rows:
        if row.get("start"):
            continue
        value = _number(row.get("val"))
        try:
            end = date.fromisoformat(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if value is None or end > today:
            continue
        picked.append((end, value, row))
    picked.sort(key=lambda item: item[0])
    return picked


def _prior_year(rows, end):
    target = end - timedelta(days=365)
    near = [item for item in rows if abs((item[0] - target).days) <= 25]
    return near[-1] if near else None


def _latest_at_or_before(rows, end):
    chosen = None
    for item in rows:
        if item[0] <= end:
            chosen = item
    return chosen


def quarter_label(end):
    """Calendar quarter of a reported period end, as ``YYYY-MM``."""
    return end.strftime("%Y-%m")


def financials(document, today=None, price=None):
    """Quarterly statement values from one company's XBRL facts.

    Returns ``{}`` when the document carries no usable revenue and operating income, so a
    caller can keep recording "financial statements unavailable" rather than a half row.
    """
    today = today or date.today()
    if not isinstance(document, dict):
        return {}
    revenue_tag, revenue_raw = _unit_facts(document, REVENUE_TAGS)
    income_tag, income_raw = _unit_facts(document, OPERATING_INCOME_TAGS)
    revenue_rows = quarterly_rows(revenue_raw, today)
    income_rows = quarterly_rows(income_raw, today)
    if not revenue_rows or not income_rows:
        return {}
    revenue_end, revenue, _ = revenue_rows[-1]
    matching = [item for item in income_rows if item[0] == revenue_end]
    income_end, income, _ = matching[-1] if matching else income_rows[-1]
    if revenue <= 0 or income is None:
        return {}
    period_end = revenue_end
    result = {
        "quarter": quarter_label(period_end),
        "revenue_amount": revenue,
        "operating_income_amount": income,
        "operating_margin_pct": 100 * income / revenue,
        "financial_source": "SEC XBRL companyfacts",
        "financial_period_end": period_end.isoformat(),
        "financial_tags": {"revenue": revenue_tag, "operating_income": income_tag},
    }
    prior_revenue = _prior_year(revenue_rows, revenue_end)
    prior_income = _prior_year(income_rows, income_end)
    if prior_revenue and prior_revenue[1] > 0:
        result["revenue_yoy_pct"] = 100 * (revenue / prior_revenue[1] - 1)
    if prior_revenue and prior_revenue[1] > 0 and prior_income:
        prior_margin = 100 * prior_income[1] / prior_revenue[1]
        result["operating_margin_yoy_pp"] = result["operating_margin_pct"] - prior_margin

    ocf_tag, ocf_raw = _unit_facts(document, OCF_TAGS)
    ocf_rows = quarterly_rows(ocf_raw, today)
    ocf = next((item for item in reversed(ocf_rows) if item[0] == period_end), None)
    capex_tag, capex_raw = _unit_facts(document, CAPEX_TAGS)
    capex_rows = quarterly_rows(capex_raw, today)
    capex = next((item for item in reversed(capex_rows) if item[0] == period_end), None)
    if ocf:
        result["operating_cash_flow_margin_pct"] = 100 * ocf[1] / revenue
        if income > 0:
            result["operating_cash_conversion"] = ocf[1] / income
        if capex:
            result["free_cash_flow_margin_pct"] = 100 * (ocf[1] - abs(capex[1])) / revenue
        result["cash_flow_source"] = "SEC XBRL companyfacts"

    equity_tag, equity_raw = _unit_facts(document, EQUITY_TAGS)
    debt_tag, debt_raw = _unit_facts(document, DEBT_TAGS)
    debt_current_tag, debt_current_raw = _unit_facts(document, DEBT_CURRENT_TAGS)
    cash_tag, cash_raw = _unit_facts(document, CASH_TAGS)
    equity = _latest_at_or_before(instant_rows(equity_raw, today), period_end)
    debt = _latest_at_or_before(instant_rows(debt_raw, today), period_end)
    debt_current = _latest_at_or_before(instant_rows(debt_current_raw, today), period_end)
    cash = _latest_at_or_before(instant_rows(cash_raw, today), period_end)
    if equity and equity[1] > 0 and debt:
        total_debt = debt[1] + (debt_current[1] if debt_current else 0)
        result["net_debt_to_equity"] = (total_debt - (cash[1] if cash else 0)) / equity[1]
        result["balance_sheet_source"] = "SEC XBRL companyfacts"

    eps_tag, eps_raw = _unit_facts(document, EPS_TAGS, unit="USD/shares")
    eps_rows = quarterly_rows(eps_raw, today)
    trailing = [item for item in eps_rows if item[0] <= period_end][-4:]
    if len(trailing) == 4:
        trailing_eps = sum(item[1] for item in trailing)
        if trailing_eps > 0:
            result["trailing_eps"] = trailing_eps
            if isinstance(price, (int, float)) and price > 0:
                result["trailing_pe"] = price / trailing_eps
                result["trailing_pe_source"] = "SEC XBRL trailing four-quarter EPS with observed price"
                result["valuation_source"] = "SEC XBRL trailing four-quarter EPS with observed price"
                result["valuation_observed_at"] = datetime.now(tz=None).astimezone().isoformat()
    return result


def fetch_companyfacts(cik, session=requests, timeout=10):
    response = session.get(FACTS_URL.format(cik=int(cik)), headers={"User-Agent": user_agent()},
                           timeout=timeout)
    response.raise_for_status()
    return response.json()


def cik_for_ticker(ticker, session=requests, timeout=10):
    """CIK from the SEC's own ticker map (one small file, no per-symbol lookup)."""
    response = session.get(COMPANY_TICKERS_URL, headers={"User-Agent": user_agent()}, timeout=timeout)
    response.raise_for_status()
    for row in response.json().values():
        if str(row.get("ticker", "")).upper() == str(ticker).upper():
            return int(row["cik_str"])
    return None
