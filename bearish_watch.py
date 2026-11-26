"""End-of-day relative-strength research while the listing benchmark is below MA20."""

import math

from candidate_selection import average_turnover, liquidity_rejection_reason
from research_asset import is_fund


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def resilience_setup(row, market):
    """Return the five-session benchmark excess for an observable setup."""
    values = row.get("indicators") or {}
    session = values.get("data_date")
    if (
        not session
        or values.get("market_ma20_date") != session
        or values.get("market_lag_sessions") != 0
        or values.get("market_above_ma20") is not False
    ):
        return None
    if is_fund(row) or liquidity_rejection_reason(row, market):
        return None
    daily = values.get("return_1d")
    weekly = values.get("return_5d")
    benchmark = values.get("market_return_5d")
    if not all(_number(value) for value in (daily, weekly, benchmark)):
        return None
    if (
        not values.get("obv_rising")
        or values.get("breakdown")
        or values.get("overheated")
        or not values["ma20"] < values["price"] <= 1.08 * values["ma20"]
        or not 0 <= daily <= 0.04
        or not 0 < weekly <= 0.08
    ):
        return None
    excess = round(weekly - benchmark, 4)
    return excess if excess >= 0.01 else None


def quiet_demand(values):
    """Find a narrow range with observable, moderate volume."""
    volume = values.get("volume_ratio")
    return values.get("compression") is True and _number(volume) and 0.8 <= volume < 1.8


def rank_key(row, excess):
    values = row["indicators"]
    return (-excess, -values["relative_strength"], -average_turnover(values), row["symbol"])


def attach(report, previous_us_session):
    """Store the strongest eligible US observation for the last completed session."""
    candidates = []
    for row in report.get("results", ()) if previous_us_session else ():
        values = row.get("indicators") or {}
        if row.get("market") != "US" or values.get("data_date") != previous_us_session:
            continue
        excess = resilience_setup(row, "US")
        if excess is not None and quiet_demand(values):
            candidates.append((rank_key(row, excess), row, excess))

    watch = {}
    if candidates:
        _, row, excess = min(candidates, key=lambda candidate: candidate[0])
        values = row["indicators"]
        watch = dict(
            strategy="bearish_quiet_demand",
            status="observation_only",
            symbol=row["symbol"],
            name=row.get("name") or row["symbol"],
            reference_session=previous_us_session,
            reference_price=values["price"],
            benchmark_return_5d=values["market_return_5d"],
            relative_strength_5d=excess,
            return_5d=values["return_5d"],
            volume_ratio=values["volume_ratio"],
        )
    report["bearish_watch"] = watch
    return watch


def format_block(row, market=None):
    if not row or market not in (None, "US"):
        return []
    return [
        "[미국 하락장 모의 관찰]",
        f"• {row['name']} ({row['symbol']}) · 최근 5일 {row['return_5d']:+.1%}"
        f" · 지수 대비 {row['relative_strength_5d'] * 100:+.1f}%p"
        f" · 거래량 {row['volume_ratio']:.2f}배 · {row['reference_session']} 종가 기준",
    ]
