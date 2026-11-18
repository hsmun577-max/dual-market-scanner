"""Eligibility and ranking for the equity momentum recommendation strategy."""

import math
import re

MIN_AVERAGE_TURNOVER = {"KR": 5_000_000_000, "US": 10_000_000}
MIN_PRICE = {"KR": 1000, "US": 5}
SPECIAL_SECURITY = re.compile(
    r"스팩|애퀴지션|우선주|유닛|워런트|\b(?:spac|acquisition|preferred|warrants?|units?)\b", re.I)
CASH_OR_BOND = re.compile(
    r"머니마켓|KOFR|CD(?:\d|금리)|단기금리|단기채|채권|국채|회사채|통안채|"
    r"\b(?:treasury|treasuries|bonds?|money market|ultrashort|floating rate)\b", re.I)
COMPLEX_ETF = re.compile(
    r"인버스|레버리지|커버드콜|위클리페이|\b(?:inverse|leveraged|weeklypay|covered call|[23]x)\b", re.I)
KNOWN_COMPLEX = {"SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS"}


def instrument_rejection_reason(symbol, name, asset_type=""):
    """Filter identifiable instruments with a different payoff or liquidity profile."""
    label = name or ""
    ticker = symbol.removeprefix("DNAS").split(".")[0]
    if SPECIAL_SECURITY.search(label) or re.search(r"우[BC]?(?:\([^)]*\))?$", label):
        return "special_security"
    if CASH_OR_BOND.search(label):
        return "cash_or_bond_product"
    if (COMPLEX_ETF.search(label) or ticker in KNOWN_COMPLEX
            or asset_type in {"inverse_etf", "leveraged_etf"}
            or (re.search(r"Direxion|디렉시온|ProShares|프로셰어즈", label, re.I)
                and re.search(r"\bbear\b|베어", label, re.I))):
        return "complex_product"
    return None


def average_turnover(indicators):
    """Support archived scans using current price times prior average share volume."""
    actual = indicators.get("average_turnover_20d")
    if actual is not None:
        return actual
    ratio = indicators.get("volume_ratio") or 0
    return indicators["price"] * indicators["volume"] / ratio if ratio > 0 else 0


def liquidity_rejection_reason(item, market):
    reason = instrument_rejection_reason(item["symbol"], item.get("name"), item.get("asset_type", ""))
    if reason:
        return reason
    values = item.get("indicators", {})
    required = ("price", "volume", "volume_ratio", "ma20", "ma60",
                "return_5d", "return_20d", "relative_strength")
    if any(not isinstance(values.get(key), (int, float)) or not math.isfinite(values[key]) for key in required):
        return "missing_indicators"
    if values["price"] < MIN_PRICE[market]:
        return "low_price"
    average = average_turnover(values)
    if (not math.isfinite(average) or average < MIN_AVERAGE_TURNOVER[market]
            or values["price"] * values["volume"] < MIN_AVERAGE_TURNOVER[market]):
        return "insufficient_liquidity"
    return None


def candidate_rejection_reason(item, market):
    reason = liquidity_rejection_reason(item, market)
    if reason:
        return reason
    values = item["indicators"]
    if values.get("breakdown") or values.get("rebound"):
        return "breakdown_or_rebound"
    if not values["price"] > values["ma20"] >= values["ma60"] > 0:
        return "unconfirmed_uptrend"
    if not (values["return_5d"] > 0 and values["return_20d"] > 0 and values["relative_strength"] > 0):
        return "weak_relative_trend"
    if values.get("overheated") or values["return_5d"] > 0.12 or values["price"] / values["ma20"] > 1.08:
        return "extended_price"
    if not values.get("obv_rising") or values["volume_ratio"] < 1.2:
        return "unconfirmed_demand"
    if not values.get("breakout") and values["volume_ratio"] < 1.5:
        return "no_price_or_volume_trigger"
    return None


def rank_market_candidates(candidates, market, limit=5):
    """Rank eligible candidates without reserving slots for volume outliers."""
    eligible = [item for item in candidates if candidate_rejection_reason(item, market) is None]
    return sorted(eligible, key=lambda item: (
        -int(bool(item["indicators"].get("breakout"))),
        -item["indicators"]["relative_strength"],
        -min(item["indicators"]["volume_ratio"], 3),
        -average_turnover(item["indicators"]),
        -item["signal"]["score"], item["symbol"],
    ))[:max(0, limit)]


def rank_recovery_candidates(candidates, market, limit=5):
    """Watch liquid early recoveries before long moving averages can turn upward."""
    eligible = []
    for item in candidates:
        if liquidity_rejection_reason(item, market):
            continue
        values = item["indicators"]
        if (values.get("breakdown") or values.get("overheated") or not values.get("obv_rising")
                or not 0 < values["return_5d"] <= 0.12 or values["volume_ratio"] < 1.5
                or values["ma20"] <= 0 or not 0.95 <= values["price"] / values["ma20"] <= 1.08):
            continue
        if values.get("rebound") or values["ma20"] < values["ma60"]:
            eligible.append(item)
    return sorted(eligible, key=lambda item: (
        -average_turnover(item["indicators"]),
        -min(item["indicators"]["volume_ratio"], 3), item["symbol"],
    ))[:max(0, limit)]
