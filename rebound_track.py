"""Reversal tracks — R2 bottom rebound and R3 oversold rebound — entered on an open dip.

The owner's rule (2026-11-23): **"상승할 종목이 눌렸을 때 사야 하고, 이미 털어먹고 나가는
종목이 눌렸다고 사면 안 된다."** The momentum screen cannot buy these names: while the
averages are inverted it returns ``unconfirmed_uptrend``, and in a downtrend the market gate
holds every plan back. So the reversal tracks run beside it, in their own section, and are
never lifted by (or allowed to lift) the momentum gate.

Backtest (565 symbols, 2025-02-28~2026-11-13, next-session entry, 5 sessions held, 0.28%
round-trip cost):

* **R2** — inverted alignment (ma20 <= ma60), a 5-day-MA break (price above the 5-day
  average, the prior close below it), 2.5x volume, a bullish bar, +3~15% on the day, with
  the sell-off filters (OBV rising, not overheated, upper wick <= 30%, cancel on a gap down
  of 1% or more). Buying the plain open was -0.86% a trade; buying a -2% dip from that open
  returned +3.19% over 5 sessions (59.2% win rate, 142 trades), and the -4%/+6%
  stop-and-target variant +0.80%.
* **R3** — RSI(14) <= 35, +2% or more on the day, 1.8x volume, a bullish bar. +2.19% bought
  at the open, +4.82% bought at -3% from the open (75.5% win rate).

Both are 5-session holds. The narrow -2% stop is **not** recommended for them: the dip entry
sits inside the normal intraday shakeout of a rebound, and the -3% dip study lost its edge
once the tight stop was applied. That is why a plan here carries no fixed stop and says so.

The dip is measured from the **next session's open**, which a preopen report does not have
yet. The plan therefore carries the rule and a flat-open reference level, and the real limit
is fixed from the observed open (``tradable_signal.validate_entry``) — or, when a row
already carries the plan session's own bar, straight away.
"""

import math

LABEL = "[반등 눌림 후보]"
GAP_CANCEL_PCT = -0.01
HOLD_SESSIONS = 5
STOP_POLICY = "narrow_stop_not_recommended"

R2 = dict(strategy="bottom_rebound", label="R2 바닥반등", entry_offset_pct=-0.02,
          min_volume_ratio=2.5, return_1d_min=0.03, return_1d_max=0.15, max_upper_wick=0.30)
R3 = dict(strategy="oversold_rebound", label="R3 과매도반등", entry_offset_pct=-0.03,
          min_volume_ratio=1.8, max_rsi=35.0, return_1d_min=0.02)
CONFIG = {track["strategy"]: track for track in (R2, R3)}
TRACKS = frozenset(CONFIG)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _tick_size(price, market):
    if str(market) == "US":
        return 0.01
    from tradable_signal import krx_tick
    return krx_tick(price)


def setup(values):
    """The rebound track these daily indicators qualify for, or None.

    A value that was never observed (a missing wick, OBV or 5-day average) is not read as
    a pass: every condition below is evidence the setup needs, and an archived frame
    without the bar's open simply has no rebound setup to report.
    """
    if values.get("overheated"):
        return None
    price, opening = _number(values.get("price")), _number(values.get("open"))
    previous, ma5 = _number(values.get("prev_close")), _number(values.get("ma5"))
    ma20, ma60 = _number(values.get("ma20")), _number(values.get("ma60"))
    volume_ratio = _number(values.get("volume_ratio"))
    change = _number(values.get("return_1d"))
    if None in (price, opening, previous, ma5, ma20, ma60, volume_ratio, change):
        return None
    if price <= opening:
        # Both tracks need a bullish bar: the close has to hold above its own open.
        return None
    wick = _number(values.get("upper_wick_ratio"))
    if (ma20 <= ma60 and ma5 >= previous and price > ma5
            and volume_ratio >= R2["min_volume_ratio"]
            and R2["return_1d_min"] <= change <= R2["return_1d_max"]
            and values.get("obv_rising") is True
            and wick is not None and wick <= R2["max_upper_wick"]):
        return dict(R2)
    rsi = _number(values.get("rsi14"))
    if (rsi is not None and rsi <= R3["max_rsi"] and change >= R3["return_1d_min"]
            and volume_ratio >= R3["min_volume_ratio"]):
        return dict(R3)
    return None


def limit_price(strategy, next_open, market="KR"):
    """The resting limit ``offset`` below an observed opening price, floored to the tick."""
    offset = CONFIG[strategy]["entry_offset_pct"]
    price = float(next_open) * (1 + offset)
    tick = _tick_size(price, market)
    return round(math.floor(price / tick) * tick, 2)


def entry_state(strategy, prev_close, next_open=None, market="KR"):
    """Where the entry stands: waiting for the open, cancelled by a gap, or priced.

    A gap down of 1% or more from the prior close cancels the entry outright: the morning
    is selling the name, and the backtest's positive edge came from names that opened
    close to the prior close and then dipped.
    """
    strategy = strategy if strategy in CONFIG else R2["strategy"]
    previous = _number(prev_close)
    if previous is None or previous <= 0:
        return dict(status="reference_price_missing", entry_limit=None, gap_pct=None)
    observed = _number(next_open)
    if observed is None or observed <= 0:
        return dict(status="awaiting_open", entry_limit=None, gap_pct=None)
    gap = observed / previous - 1
    if gap < GAP_CANCEL_PCT:
        return dict(status="gap_down_cancelled", entry_limit=None, gap_pct=gap)
    return dict(status="limit_set", entry_limit=limit_price(strategy, observed, market),
                gap_pct=gap)


def observed_open(values):
    """The plan session's opening price, when the caller already holds one.

    A preopen report is written before the open, so this is normally empty. A caller that
    has observed the plan session's open (a premarket or after-open pass) may put it on
    the row as ``next_open``; the plan then fixes its limit and applies the gap cancel
    instead of waiting for the execution check to do it.
    """
    return values.get("next_open")


def rebound_plan(track, values, reference_date, expires_session, at, market="KR",
                 next_open=None, caveats=()):
    """The conditional plan for a qualifying row, or the reason it was cancelled."""
    strategy = track["strategy"]
    previous = _number(values.get("prev_close"))
    state = entry_state(strategy, previous, next_open, market)
    if state["status"] == "gap_down_cancelled":
        return "rebound_gap_down_cancelled", None
    if state["status"] == "reference_price_missing":
        return "rebound_reference_price_missing", None
    offset = track["entry_offset_pct"]
    plan = dict(status="conditional_buy", confirmed=False, track="rebound",
                strategy=strategy, label=track["label"], track_label=track["label"],
                reference_price=_number(values.get("price")), reference_date=reference_date,
                generated_at=at.isoformat(), expires_session=expires_session,
                entry_offset_pct=offset, entry_limit=state["entry_limit"], entry_floor=None,
                awaiting_open=state["status"] == "awaiting_open",
                flat_open_entry_limit=(limit_price(strategy, previous, market)
                                       if previous else None),
                gap_cancel_pct=GAP_CANCEL_PCT, gap_reference_price=previous,
                gap_observed_pct=state["gap_pct"],
                entry_basis=f"next_session_open_less_{abs(int(round(offset * 100)))}pct",
                fresh_execution_quote_required=True, hold_sessions=HOLD_SESSIONS,
                stop_policy=STOP_POLICY, stop_pct=None, take_profit_pct=None,
                score_applied=False,
                evidence=dict(return_1d=_number(values.get("return_1d")),
                              volume_ratio=_number(values.get("volume_ratio")),
                              rsi14=_number(values.get("rsi14")),
                              upper_wick_ratio=_number(values.get("upper_wick_ratio")),
                              ma5=_number(values.get("ma5")), ma20=_number(values.get("ma20")),
                              ma60=_number(values.get("ma60"))),
                caveats=list(caveats))
    return "conditional_buy", plan


def watch_candidates(pool, market, limit=4):
    """Reserve a few research slots for names a rebound track already qualifies for.

    The plan itself needs no company forecast, but a row that was never selected for
    analysis never reaches the decision function at all, so the setup is scored here.
    """
    from candidate_selection import liquidity_rejection_reason
    rows = [row for row in pool
            if liquidity_rejection_reason(row, market) is None
            and not row["indicators"].get("breakdown")
            and setup(row["indicators"])]
    return sorted(rows, key=lambda row: (
        -(_number(row["indicators"].get("volume_ratio")) or 0),
        row["symbol"]))[:max(0, limit)]


def offset_label(strategy):
    track = CONFIG.get(strategy, R2)
    return f"시가 −{abs(track['entry_offset_pct']):.0%}"


def price_text(value, market="KR"):
    number = _number(value)
    if number is None:
        return None
    return f"${number:,.2f}" if str(market) == "US" else f"{number:,.0f}원"
