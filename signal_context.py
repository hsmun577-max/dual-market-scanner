"""Evaluate dated research, market and event evidence available at decision time."""

from datetime import datetime, timedelta


WINDOWS = {"research": timedelta(days=7), "market": timedelta(days=4),
           "event": timedelta(hours=24)}


def context_status(records, market, symbol, decision_at):
    """Use the latest applicable report and market reading and any active risk event."""
    evidence = {kind: [] for kind in WINDOWS}
    for record in records:
        if record["market"] not in (market, "*") or record["symbol"] not in (symbol, "*"):
            continue
        kind = record["kind"]
        if kind == "research" and record["symbol"] != symbol:
            continue
        published = datetime.fromisoformat(record["available_at"])
        if published.utcoffset() is None or decision_at.utcoffset() is None:
            raise ValueError("Evidence and decision timestamps require time zones")
        if decision_at - WINDOWS[kind] <= published < decision_at:
            evidence[kind].append((published, record["assessment"]))
    if any(value == "risk" for _, value in evidence["event"]):
        return "event_risk"
    for kind in ("research", "market"):
        if not evidence[kind]:
            return f"missing_{kind}"
        latest = max(at for at, _ in evidence[kind])
        if any(value != "supportive" for at, value in evidence[kind] if at == latest):
            return f"{kind}_not_supportive"
    # Require a positive coverage marker from the event feed as well.
    if not any(value == "clear" for _, value in evidence["event"]):
        return "missing_event"
    return "supported"



def main():
    """Validate the Actions variable and save the input used for this run."""
    import json
    import os
    import sys
    from pathlib import Path

    raw = os.getenv("SIGNAL_CONTEXT_JSON", "").strip()
    if not raw:
        print("SIGNAL_CONTEXT_JSON missing: observation alerts only")
        return 1
    assessments = {"research": {"supportive", "neutral"},
                   "market": {"supportive", "neutral"}, "event": {"clear", "risk"}}
    try:
        records = json.loads(raw)
        if not isinstance(records, list) or not records:
            raise ValueError()
        for row in records:
            if not all(isinstance(row[key], str) and row[key].strip()
                       for key in ("kind", "market", "symbol", "available_at", "assessment")):
                raise ValueError()
            if row["assessment"] not in assessments[row["kind"]]:
                raise ValueError()
            if datetime.fromisoformat(row["available_at"]).utcoffset() is None:
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        print("SIGNAL_CONTEXT_JSON invalid: observation alerts only")
        return 1
    Path(sys.argv[1]).write_text(json.dumps(records), encoding="utf-8")
    print(f"Signal context loaded: {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
