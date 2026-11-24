"""Separate listed fund scans from issuer earnings analysis."""
import math
import re

# The owner's decision of 2026-11-23: "거래량 상위8개 etf만 매수후보에넣자". The most
# traded funds are the ones the entries are actually priced from, so they are researched
# and planned like any other instrument; everything below them keeps the fund screen only.
FUND_CANDIDATE_LIMIT = 8


def is_fund_company(company):
    """A listed fund, read from the issuer profile the scan holds for it.

    ``quote_type`` is the provider's own instrument class (ETF, MUTUALFUND). It is the
    only field that identifies a US listing whose English name carries no Korean brand
    prefix, which is why the demotion is re-checked once the profile has arrived.

    ``instrument_class`` is the scan's own classification, stamped on the profile when
    the row is already known to be a fund (a KODEX/TIGER listing, an ``asset_type`` of
    ``etf``). A restored profile cache can carry an entry built before ``quote_type`` was
    read: on the 2026-11-24 12:03 scan the five promoted Korean funds had no
    ``quote_type`` and were therefore graded as operating companies, which withheld
    their composite score with ``financial_statements`` — a listed fund has no income
    statement to be missing. The instrument class is what decides the model; the
    provider's quote type is one way of learning it, not the only one.
    """
    if not isinstance(company, dict):
        return False
    quote = str(company.get('quote_type') or '').strip().upper()
    if quote in {'ETF', 'MUTUALFUND', 'EXCHANGE_TRADED_FUND'}:
        return True
    return str(company.get('instrument_class') or '').strip().lower() in {
        'listed_fund', 'etf', 'etn'}


def is_fund(row):
    """A listed fund, however the scan happens to record its class.

    ``asset_type`` carries ``etf``, ``inverse_etf`` and ``leveraged_etf`` for the listings
    the scanner knows by symbol, the Korean brand prefixes catch the rest of the domestic
    funds, and ``quote_type`` catches the US listings once their profile has been read.
    """
    kind = str(row.get('asset_type') or '').lower()
    # ``stock_or_etf`` is the scanner's unclassified default, so only an explicit fund class
    # counts here.
    if kind in {'etf', 'etn', 'fund', 'inverse_etf', 'leveraged_etf'}:
        return True
    if is_fund_company(row.get('company_analysis')):
        return True
    return bool(re.match(r'^(?:KODEX|TIGER|ACE|RISE|KBSTAR|ARIRANG|HANARO|SOL|PLUS|KOSEF|KIWOOM|TIMEFOLIO|1Q|WON)\s',
                         str(row.get('name') or '')))


def traded_volume(row):
    """Last completed session's share volume; ``-1`` when it was not observed."""
    value = (row.get('indicators') or {}).get('volume')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return -1.
    return float(value)


def promoted_fund_symbols(rows, limit=FUND_CANDIDATE_LIMIT):
    """The most traded funds of each market that stay on the buy-candidate path.

    Ranked by the market's own scan, so a Korean fund is never compared with a US one.
    A fund that the instrument filter refuses on its name alone (cash or bond products,
    covered-call and weekly-pay structures, special securities) is left out of the
    ranking: promoting one could not produce a candidate, and it would take a slot from
    a fund that could. The entry screen's own liquidity read is applied the same way — a
    fund quoted at 140원 (KODEX 200선물인버스2X on 2026-11-24) or one whose 20-day dollar
    turnover is under the floor (KDEF, $1.5M) is refused as ``low_price`` /
    ``insufficient_liquidity`` before its profile is read, so promoting it reserved one of
    the eight research slots for a listing that can never be planned. The predicate is
    the one ``select_analysis_candidates`` uses to admit a row, so a promoted fund is
    always a fund the analysis pass can actually read. The eighth and below are not
    promoted, so the existing liquidity, trend, demand and market-regime gates decide
    the rest as before.
    """
    from candidate_selection import MIN_PRICE, liquidity_rejection_reason
    grouped = {}
    for row in rows:
        if not is_fund(row):
            continue
        if row.get('market') not in MIN_PRICE:
            continue
        if liquidity_rejection_reason(row, row.get('market')):
            continue
        grouped.setdefault(row.get('market'), []).append(row)
    return {market: [row['symbol'] for row in
                     sorted(group, key=lambda row: (-traded_volume(row), str(row.get('symbol'))))[:max(0, limit)]]
            for market, group in grouped.items()}
