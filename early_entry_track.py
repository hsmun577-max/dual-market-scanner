"""Short-term early entry — the first one-to-three sessions of an average alignment.

The momentum screen buys a name only after ``price > ma20 >= ma60`` and a 20-day
relative-strength confirmation, and by then the move it wanted to catch is already
underway. The 2026-11-24 study (``evidence/dual-market-scanner/상승초입-조기포착-전략연구.md``,
1부) compared six earlier entries and found exactly one worth wiring:

* **B** — ``MA5 > MA20`` on its 1st~3rd session (or the first close above ``MA20`` after
  the averages were inverted), volume >= 1.5x the 20-day average, a bullish bar, and a
  price within +3% of ``MA20`` (not extended).
* B alone is +0.069% over 5 sessions (1,851 trades, 0.28% cost), i.e. inside the random
  entry band. What pays is *who is buying*: with a measured foreign/institutional inflow
  or a fundamental score of 61+ it is +0.520% (273 trades, 5 sessions, −2% stop) and
  +4.545% held 10 sessions to the close; at a −3% stop +1.141% (CI +0.354~+1.960).
* The entry price matters as much as the signal. Buying the next session's open gave
  +0.069%; a **limit 1% below that open** (filled 83.4% of the time) gave +0.530%
  (CI +0.089~+1.007) — the one entry rule that turned a −2% stop positive in the study.
  Chasing the 16.6% that never dip back to the limit gives up that edge.

So this track is entered on ``open − 1%``, holds 5 sessions, and keeps the owner's own
sell rule: the split stop of the instrument (−3% single stock, −2% fund) and +6% take
profit with the surge trailing hold. It lifts the trend gate's ``unconfirmed_uptrend`` /
``weak_relative_trend`` refusals — that is what makes it early — but never the safety
blocks (liquidity, breakdown, extended price) or the market-regime gate; those stay in
``candidate_selection.early_entry_block_reason``.
"""

import math

TRACK = "early_entry"
STRATEGY = "early_ma_alignment"
LABEL = "[단기 상승 초입]"
ENTRY_OFFSET_PCT = -0.01
HOLD_SESSIONS = 5
MIN_VOLUME_RATIO = 1.5
MIN_ALIGNMENT_DAYS = 1
MAX_ALIGNMENT_DAYS = 3
# The study's "not extended": the close may sit at most this far above the 20-day average.
MAX_ABOVE_MA20 = 0.03
# The two qualifiers the study required beside the chart: who is already buying the
# issuer, or how the issuer's fundamentals score today.
QUALITY_FUNDAMENTAL_SCORE = 61.0
QUALIFIER_LABELS = {"supply_inflow": "수급 유입", "fundamental_61": "펀더멘탈 61+"}


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
    """The early-entry setup these daily indicators qualify for, or ``None``.

    Every condition has to be observed: a frame without the candle's open, without the
    alignment age (``ma5_above_ma20_days``/``ma20_cross_up``) or without the volume ratio
    has no early entry to report, and is not read as a pass.
    """
    values = values or {}
    if values.get("overheated") or values.get("breakdown"):
        return None
    price = _number(values.get("price"))
    opening = _number(values.get("open"))
    ma5 = _number(values.get("ma5"))
    ma20 = _number(values.get("ma20"))
    volume_ratio = _number(values.get("volume_ratio"))
    days = _number(values.get("ma5_above_ma20_days"))
    crossed = values.get("ma20_cross_up")
    if None in (price, opening, ma5, ma20, volume_ratio):
        return None
    if price <= 0 or ma20 <= 0:
        return None
    # A bullish bar holds its close above its own open.
    if price <= opening:
        return None
    if volume_ratio < MIN_VOLUME_RATIO:
        return None
    if price <= ma20 or price / ma20 - 1 > MAX_ABOVE_MA20:
        return None
    aligned = (ma5 > ma20 and days is not None
               and MIN_ALIGNMENT_DAYS <= days <= MAX_ALIGNMENT_DAYS)
    if not aligned and crossed is not True:
        return None
    return dict(track=TRACK, strategy=STRATEGY, label=LABEL,
                entry_offset_pct=ENTRY_OFFSET_PCT,
                evidence=dict(alignment_days=int(days) if aligned else None,
                              ma20_cross_up=crossed is True,
                              above_ma20_pct=round(price / ma20 - 1, 6),
                              volume_ratio=volume_ratio, ma5=ma5, ma20=ma20,
                              return_1d=_number(values.get("return_1d"))))


def qualifier(item):
    """``'supply_inflow'``/``'fundamental_61'`` for this row, or ``None``.

    The chart alone was worth nothing in the study; the setup is only bought when the
    issuer is already being bought (a *measured* foreign/institutional inflow, not a
    price proxy) or scores 61+ on the fundamental axis. An unmeasured flow is not an
    inflow.
    """
    item = item or {}
    supply = item.get("institutional_flows") or {}
    forecast = item.get("company_forecast") or {}
    fundamental = _number(forecast.get("fundamental_score"))
    if supply.get("status") == "measured" and supply.get("direction") == "inflow":
        return "supply_inflow"
    if fundamental is not None and fundamental >= QUALITY_FUNDAMENTAL_SCORE:
        return "fundamental_61"
    return None


def limit_price(next_open, market="KR"):
    """The resting limit ``−1%`` below an observed opening price, floored to the tick."""
    price = float(next_open) * (1 + ENTRY_OFFSET_PCT)
    tick = _tick_size(price, market)
    return round(math.floor(price / tick) * tick, 2)


def entry_state(next_open, market="KR"):
    """Where the entry stands: waiting for the open, or priced from it.

    Unlike the rebound tracks there is no gap-down cancel: the study's entry waits for a
    1% dip from whatever the open is, and a name that gaps up simply never trades through
    its limit (the message says so instead of chasing it).
    """
    observed = _number(next_open)
    if observed is None or observed <= 0:
        return dict(status="awaiting_open", entry_limit=None)
    return dict(status="limit_set", entry_limit=limit_price(observed, market))


def observed_open(values):
    """The plan session's opening price, when the caller already holds one."""
    return (values or {}).get("next_open")


def entry_plan(row, forecast, reference_date, expires_session, at, market="KR",
               next_open=None, qualifier_reason="supply_inflow", caveats=()):
    """The conditional plan for a qualifying row, or the reason it was cancelled."""
    from exit_policy import exit_policy_for, policy_snapshot, stop_basis
    values = row.get("indicators") or {}
    price = _number(values.get("price"))
    if price is None or price <= 0:
        return "early_entry_reference_price_missing", None
    state = entry_state(next_open, market)
    offset = ENTRY_OFFSET_PCT
    tick = _tick_size(price * (1 + offset), market)
    flat_limit = round(math.floor(price * (1 + offset) / tick) * tick, 2)
    policy = exit_policy_for(row)
    plan = dict(status="conditional_buy", confirmed=False, track=TRACK,
                strategy=STRATEGY, label=LABEL, track_label=LABEL,
                reference_price=price, reference_date=reference_date,
                generated_at=at.isoformat(), expires_session=expires_session,
                entry_offset_pct=offset, entry_limit=state["entry_limit"], entry_floor=None,
                awaiting_open=state["status"] == "awaiting_open",
                flat_open_entry_limit=flat_limit,
                entry_basis="next_session_open_less_1pct",
                fresh_execution_quote_required=True, hold_sessions=HOLD_SESSIONS,
                composite_score=forecast.get("composite_score"),
                qualifier=qualifier_reason,
                qualifier_label=QUALIFIER_LABELS.get(qualifier_reason),
                score_applied=True,
                take_profit_pct=policy.take_profit_pct,
                take_profit_mode=policy.take_profit_mode, exit_policy=policy_snapshot(policy),
                **stop_basis(row),
                evidence=dict(**(setup(values) or {}).get("evidence", {})),
                caveats=list(caveats))
    return "conditional_buy", plan


def watch_candidates(pool, market, limit=4):
    """Reserve a few research slots for names the early-entry chart qualifies for.

    The plan needs the issuer's measured flow or its fundamental score, and neither is
    read for a row that was never selected for analysis. The chart conditions alone are
    enough to spend a slot; the qualifier decides the plan later.
    """
    from candidate_selection import liquidity_rejection_reason, early_entry_block_reason
    rows = [row for row in pool
            if liquidity_rejection_reason(row, market) is None
            and early_entry_block_reason(row, market) is None
            and setup(row["indicators"])]
    return sorted(rows, key=lambda row: (
        -(_number(row["indicators"].get("volume_ratio")) or 0), row["symbol"]))[:max(0, limit)]


def offset_label():
    return f"시가 −{abs(ENTRY_OFFSET_PCT):.0%}"
