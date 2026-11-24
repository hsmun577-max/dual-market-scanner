"""Analyst consensus: the average target price a broker screen compares with the price.

A domestic issuer's consensus is read from Naver Finance's issuer integration payload
(``priceTargetMean``, ``recommMean`` and the date they were compiled). An overseas
issuer's comes from the Yahoo issuer information the company context already holds, so
no extra request is made for it. Coverage is thin for small caps: when no target exists
the evidence says it was not observed instead of leaving the gap to look neutral.
"""
from datetime import datetime, timezone
import math
import os
import requests

NAVER_URL = "https://m.stock.naver.com/api/stock/{code}/integration"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CACHE = {}


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).replace(",", "").replace("%", "").replace("+", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def naver_consensus(code, timeout=8):
    """Average target price and recommendation of the covering brokers, if any."""
    if code in _CACHE:
        return _CACHE[code]
    try:
        response = requests.get(NAVER_URL.format(code=code), headers=HEADERS, timeout=timeout)
        payload = response.json() if response.status_code == 200 else None
    except (requests.RequestException, ValueError):
        payload = None
    consensus = (payload or {}).get("consensusInfo") or {}
    result = {"target_price": number(consensus.get("priceTargetMean")),
              "recommendation_mean": number(consensus.get("recommMean")),
              "compiled_at": consensus.get("createDate")}
    if os.getenv("SUPPLY_FLOW_CACHE_DISABLED"):
        return result
    _CACHE[code] = result
    return result


def unavailable(market, symbol, at, reason):
    return {"status": "unavailable", "market": market, "symbol": symbol,
            "observed_at": at.isoformat(), "reason": reason}


def consensus_for(row, company=None, at=None, price=None):
    """Consensus target and the gap to the current price for one analysed row."""
    at = at or datetime.now(timezone.utc)
    row = row or {}
    company = company or {}
    market = row.get("market")
    symbol = row.get("symbol") or ""
    price = number(price if price is not None else (row.get("indicators") or {}).get("price"))
    if market == "KR":
        reading = naver_consensus(symbol.split(".")[0])
        source = "Naver Finance broker consensus"
        as_of = reading.get("compiled_at")
        target = reading.get("target_price")
        recommendation = reading.get("recommendation_mean")
    else:
        target = number(company.get("target_mean_price"))
        recommendation = number(company.get("recommendation_mean"))
        source = "Yahoo Finance analyst consensus"
        as_of = (company.get("profile_available_at") or at.isoformat())[:10]
    analysts = number(company.get("analyst_count"))
    if target is None or price is None or price <= 0:
        return unavailable(market, symbol, at, "no_analyst_target_observed")
    return {"status": "measured", "market": market, "symbol": symbol, "source": source,
            "as_of": as_of, "target_price": target, "price": price,
            "upside_pct": round(100 * (target / price - 1), 2),
            "recommendation_mean": recommendation,
            "recommendation_scale": "1(매도)~5(매수)" if market == "KR" else "1(strong buy)~5(strong sell)",
            "analyst_count": analysts, "observed_at": at.isoformat(),
            "meaning": "broker mean target against the observed price, not our scenario"}
