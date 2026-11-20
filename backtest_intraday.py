"""Replay opening signals from five-minute bars and observed quotes.

Each JSONL row contains market, symbol, signal_at, daily_eligible, three
bars (start, high, close, volume), and time-ordered quotes (at, bid, ask).
Supply quotes from just before the signal through the following 30 minutes.
Rows may also carry ``post_bars``: five-minute bars after the signal with their
``volume``. They let the replay apply the sell policy's surge trailing exit,
which needs volume and is never inferred when a row has none.

Two exit modes are measured side by side:

* ``fixed`` — the legacy comparison: +2% target, a 30-minute forced close, and
  the support stop when a strategy asks for one.
* ``policy`` — the actual sell rule (``exit_policy.ExitPolicy``): −2% stop,
  +6% default take profit that is held through a short-term surge, then closed
  at the price seen when volume collapses and price rolls over from the peak.
  A position still open at the quote horizon is reported as
  ``held_past_horizon`` with its mark-to-market, not forced closed.
"""

import argparse
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from exit_policy import DEFAULT_EXIT_POLICY, ExitTracker
from kis_intraday import (BAR_SECONDS, breakout_v2, candidate_metrics, protective_stop,
                          price_projection, volume_reference)
from signal_context import context_status

# (strategy name, replay arguments, needs the context check)
STRATEGIES = (
    ("baseline", dict(strict=False, use_support_stop=False, exit_mode="fixed"), False),
    ("baseline_policy", dict(strict=False, use_support_stop=False, exit_mode="policy"), False),
    ("breakout_v2", dict(strict=True, use_support_stop=False, exit_mode="fixed"), False),
    ("breakout_v2_policy", dict(strict=True, use_support_stop=False, exit_mode="policy"), False),
    ("breakout_with_context", dict(strict=True, use_support_stop=True, exit_mode="fixed"), True),
    ("breakout_with_context_policy", dict(strict=True, use_support_stop=True, exit_mode="policy"), True),
)


def _baseline_volume(bars):
    """Average volume of the three signal bars, or None when it is unusable."""
    volumes = [bar.get("volume") for bar in bars]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value > 0 for value in volumes):
        return None
    return sum(volumes) / len(volumes)


def _bar_volume_at(post_bars, at):
    """Volume of the latest five-minute bar already completed at ``at``.

    A bar is used only after it closed, so the replay never reads volume from
    the interval the quote belongs to.
    """
    latest = None
    for bar in post_bars:
        start = datetime.fromisoformat(bar["start"])
        if start + timedelta(seconds=BAR_SECONDS) > at:
            continue
        if latest is None or start > latest[0]:
            latest = (start, bar)
    if latest is None:
        return None
    volume = latest[1].get("volume")
    if not isinstance(volume, (int, float)) or isinstance(volume, bool) \
            or not math.isfinite(volume) or volume < 0:
        return None
    return float(volume)


def _policy_exit(row, entry, entry_at, quotes, horizon, fee_bps, policy):
    """Run the sell rule over the quotes after entry."""
    fee = fee_bps / 10_000
    tracker = ExitTracker(entry, policy, entry_at=entry_at,
                          baseline_volume=_baseline_volume(row["bars"]))
    post_bars = row.get("post_bars") or []
    last_bid = None
    for at, quote in quotes:
        if at > horizon + timedelta(seconds=30):
            break
        last_bid = quote["bid"]
        decision = tracker.observe(at, quote["bid"], volume=_bar_volume_at(post_bars, at))
        if decision["action"] == "exit":
            return {"status": "evaluated", "exit_mode": "policy",
                    "exit_reason": decision["reason"], "exit_at": at.isoformat(),
                    "entry_price": entry, "exit_price": decision["exit_price"],
                    "held_mode": decision["mode"],
                    "net_return": decision["exit_price"] * (1 - fee) / (entry * (1 + fee)) - 1}
    if last_bid is None:
        return {"status": "missing_exit_quote", "exit_mode": "policy"}
    return {"status": "held_past_horizon", "exit_mode": "policy", "exit_reason": "open_at_horizon",
            "entry_price": entry, "last_bid": last_bid, "held_mode": tracker.mode,
            "unrealized_return": last_bid * (1 - fee) / (entry * (1 + fee)) - 1}


def replay(row, fee_bps, *, strict=False, use_support_stop=False, exit_mode="fixed",
           policy=DEFAULT_EXIT_POLICY):
    if exit_mode not in ("fixed", "policy"):
        raise ValueError("exit_mode must be 'fixed' or 'policy'")
    signal_at = datetime.fromisoformat(row["signal_at"])
    bars = row["bars"]
    quotes = row["quotes"]
    times = [datetime.fromisoformat(quote["at"]) for quote in quotes]
    expected = [signal_at - timedelta(minutes=minutes) for minutes in (15, 10, 5)]
    if len(bars) != 3 or [datetime.fromisoformat(bar["start"]) for bar in bars] != expected:
        raise ValueError("Expected three consecutive five-minute bars before signal")
    if times != sorted(times):
        raise ValueError("Quotes must be sorted by timestamp")
    if not row["daily_eligible"]:
        return {"status": "ineligible"}

    for bar in bars:
        if not all(math.isfinite(bar[key]) and bar[key] > 0 for key in ("high", "close", "volume")):
            raise ValueError("Invalid bar")
    for quote in quotes:
        bid, ask = quote["bid"], quote["ask"]
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
            raise ValueError("Invalid quote")

    before = [(at, quote) for at, quote in zip(times, quotes, strict=False) if at < signal_at]
    if not before:
        return {"status": "missing_signal_quote"}
    at, quote = before[-1]
    spread_pct = 200 * (quote["ask"] - quote["bid"]) / (quote["ask"] + quote["bid"])
    signal_quote = {"event_time": at.isoformat(), "spread_pct": spread_pct, "ask": quote["ask"]}
    # Use the volume reference the live decision used. Rows recorded before the
    # baseline existed fall back to the opening-share rule, and the report says which.
    volume_baseline = row.get("volume_baseline")
    basis = volume_reference(dict(enumerate(bars)), volume_baseline)[1]
    if not candidate_metrics(dict(enumerate(bars)), signal_quote, signal_at, volume_baseline):
        return {"status": "no_signal", "volume_basis": basis}
    limit = (breakout_v2(dict(enumerate(bars)), signal_quote, signal_at, volume_baseline)
             if strict else None)
    if strict and limit is None:
        return {"status": "filtered_by_breakout_v2"}

    stop = None
    if use_support_stop:
        if "low" not in bars[2]:
            return {"status": "missing_breakout_low"}
        low = bars[2]["low"]
        if not math.isfinite(low) or not 0 < low <= bars[2]["close"]:
            raise ValueError("Invalid breakout candle low")
        stop = protective_stop(dict(enumerate(bars)), limit, quote["bid"], row["market"].startswith("US-"))
        if stop is None:
            return {"status": "invalid_support_stop"}

    entry_deadline = signal_at + timedelta(seconds=30)
    exit_at = signal_at + timedelta(minutes=30)
    after = [(at, quote) for at, quote in zip(times, quotes, strict=False) if at > signal_at]
    entry_index = next(
        (i for i, (at, quote) in enumerate(after)
         if at <= entry_deadline and (limit is None or quote["ask"] <= limit)
         and (stop is None or quote["bid"] > stop)),
        None,
    )
    if entry_index is None:
        return {"status": "missing_entry_quote"}
    entry_at, entry_quote = after[entry_index]
    entry = entry_quote["ask"]

    if exit_mode == "policy":
        result = _policy_exit(row, entry, entry_at, after[entry_index + 1:], exit_at, fee_bps, policy)
        result["volume_basis"] = basis
        return result

    target = entry * 1.02
    if stop is not None:
        projection = price_projection(dict(enumerate(bars)), limit, row.get("prior_highs", ()),
                                      row["market"].startswith("US-"))
        if projection is None:
            return {"status": "no_upside_projection"}
        target = projection[0]
    stop = entry * 0.99 if stop is None else stop

    for at, quote in after[entry_index + 1:]:
        if at > exit_at + timedelta(seconds=30):
            break
        bid = quote["bid"]
        if at >= exit_at or bid <= stop or bid >= target:
            fee = fee_bps / 10_000
            return {"status": "evaluated", "exit_mode": "fixed",
                    "net_return": bid * (1 - fee) / (entry * (1 + fee)) - 1,
                    "volume_basis": basis}
    return {"status": "missing_exit_quote", "exit_mode": "fixed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--fee-bps", type=float, required=True, help="Estimated cost per side in basis points")
    parser.add_argument("--context", type=Path, help="JSON array of timestamped evidence records")
    parser.add_argument("--exit-policy", choices=("both", "fixed", "policy"), default="both",
                        help="fixed keeps the legacy +2%/30-minute comparison; policy applies the sell rule")
    args = parser.parse_args()
    evidence = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else None
    if not math.isfinite(args.fee_bps) or not 0 <= args.fee_bps < 10_000:
        parser.error("--fee-bps must be finite and between 0 and 10,000")

    strategies = (list(STRATEGIES) if args.exit_policy == "both"
                  else [row for row in STRATEGIES if row[1]["exit_mode"] == args.exit_policy])
    if evidence is None:
        strategies = [row for row in strategies if not row[2]]

    groups = {}
    with args.samples.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            market = groups.setdefault(row["market"], {})
            for strategy, options, with_context in strategies:
                group = market.setdefault(strategy, {"counts": {}, "returns": [],
                                                     "unrealized": [], "exit_reasons": {},
                                                     "volume_basis": {}})
                result = None
                if with_context and row["daily_eligible"]:
                    status = context_status(evidence, row["market"], row["symbol"],
                                            datetime.fromisoformat(row["signal_at"]))
                    if status != "supported":
                        result = {"status": status}
                if result is None:
                    result = replay(row, args.fee_bps, **options)
                status = result["status"]
                group["counts"][status] = group["counts"].get(status, 0) + 1
                if result.get("volume_basis"):
                    basis = result["volume_basis"]
                    group["volume_basis"][basis] = group["volume_basis"].get(basis, 0) + 1
                if status == "evaluated":
                    group["returns"].append(result["net_return"])
                    if result.get("exit_reason"):
                        group["exit_reasons"][result["exit_reason"]] = \
                            group["exit_reasons"].get(result["exit_reason"], 0) + 1
                elif status == "held_past_horizon":
                    group["unrealized"].append(result["unrealized_return"])
    if not groups:
        parser.error("No samples")
    for market in groups.values():
        for group in market.values():
            returns = group.pop("returns")
            unrealized = group.pop("unrealized")
            group["closed_trades"] = len(returns)
            group["winning_trades"] = sum(value > 0 for value in returns)
            group["win_rate_pct"] = 100 * group["winning_trades"] / len(returns) if returns else None
            group["mean_net_return_pct"] = 100 * sum(returns) / len(returns) if returns else None
            group["unrealized_trades"] = len(unrealized)
            group["mean_unrealized_return_pct"] = (
                100 * sum(unrealized) / len(unrealized) if unrealized else None)
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
