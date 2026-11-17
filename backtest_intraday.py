"""Replay opening signals from five-minute bars and observed quotes.

Each JSONL row contains market, symbol, signal_at, daily_eligible, three
bars (start, high, close, volume), and time-ordered quotes (at, bid, ask).
Supply quotes from just before the signal through the following 30 minutes.
"""

import argparse
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from kis_intraday import breakout_v2, candidate_metrics, protective_stop, sell_target
from signal_context import context_status


def replay(row, fee_bps, *, strict=False, use_support_stop=False):
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

    before = [(at, quote) for at, quote in zip(times, quotes) if at < signal_at]
    if not before:
        return {"status": "missing_signal_quote"}
    at, quote = before[-1]
    spread_pct = 200 * (quote["ask"] - quote["bid"]) / (quote["ask"] + quote["bid"])
    signal_quote = {"event_time": at.isoformat(), "spread_pct": spread_pct, "ask": quote["ask"]}
    if not candidate_metrics(dict(enumerate(bars)), signal_quote, signal_at):
        return {"status": "no_signal"}
    limit = breakout_v2(dict(enumerate(bars)), signal_quote, signal_at) if strict else None
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
    after = [(at, quote) for at, quote in zip(times, quotes) if at > signal_at]
    entry_index = next(
        (i for i, (at, quote) in enumerate(after)
         if at <= entry_deadline and (limit is None or quote["ask"] <= limit)
         and (stop is None or quote["bid"] > stop)),
        None,
    )
    if entry_index is None:
        return {"status": "missing_entry_quote"}
    entry = after[entry_index][1]["ask"]
    target = sell_target(limit, stop, row["market"].startswith("US-")) if stop is not None else entry * 1.02
    stop = entry * 0.99 if stop is None else stop

    for at, quote in after[entry_index + 1:]:
        if at > exit_at + timedelta(seconds=30):
            break
        bid = quote["bid"]
        if at >= exit_at or bid <= stop or bid >= target:
            fee = fee_bps / 10_000
            return {"status": "evaluated", "net_return": bid * (1 - fee) / (entry * (1 + fee)) - 1}
    return {"status": "missing_exit_quote"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--fee-bps", type=float, required=True, help="Estimated cost per side in basis points")
    parser.add_argument("--context", type=Path, help="JSON array of timestamped evidence records")
    args = parser.parse_args()
    evidence = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else None
    if not math.isfinite(args.fee_bps) or not 0 <= args.fee_bps < 10_000:
        parser.error("--fee-bps must be finite and between 0 and 10,000")

    groups = {}
    with args.samples.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            market = groups.setdefault(row["market"], {})
            strategies = [("baseline", False), ("breakout_v2", True)]
            if evidence is not None:
                strategies.append(("breakout_with_context", True))
            for strategy, strict in strategies:
                group = market.setdefault(strategy, {"counts": {}, "returns": []})
                result = None
                if strategy == "breakout_with_context" and row["daily_eligible"]:
                    status = context_status(evidence, row["market"], row["symbol"],
                                            datetime.fromisoformat(row["signal_at"]))
                    if status != "supported":
                        result = {"status": status}
                if result is None:
                    result = replay(row, args.fee_bps, strict=strict,
                                    use_support_stop=strategy == "breakout_with_context")
                status = result["status"]
                group["counts"][status] = group["counts"].get(status, 0) + 1
                if status == "evaluated":
                    group["returns"].append(result["net_return"])
    if not groups:
        parser.error("No samples")
    for market in groups.values():
        for group in market.values():
            returns = group.pop("returns")
            group["closed_trades"] = len(returns)
            group["winning_trades"] = sum(value > 0 for value in returns)
            group["win_rate_pct"] = 100 * group["winning_trades"] / len(returns) if returns else None
            group["mean_net_return_pct"] = 100 * sum(returns) / len(returns) if returns else None
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


