"""Evaluate dated research, market and event evidence available at decision time."""

from datetime import datetime, timedelta


WINDOWS = {"research": timedelta(days=7), "market": timedelta(days=4),
           "event": timedelta(hours=24)}


def context_status(records, market, symbol, decision_at, *, inverse_market_verified=False):
    """Use the latest applicable report and market reading and any active risk event.

    A dated research record counts inside its own seven-day window: a negative
    assessment blocks the entry, a supportive one is reported in the detail but is not
    required, because coverage of brokerage research is uneven. Records of an unknown
    kind are ignored instead of raising, and a record without the required fields is a
    data error, not silence.
    """
    if decision_at.utcoffset() is None:
        raise ValueError("Decision time requires a time zone")
    evidence = {kind: [] for kind in WINDOWS}
    for record in records or ():
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        if kind not in WINDOWS:
            continue
        if record.get("market") not in (market, "*") or record.get("symbol") not in (symbol, "*"):
            continue
        published = datetime.fromisoformat(record["available_at"])
        if published.utcoffset() is None:
            raise ValueError("Evidence timestamps require a time zone")
        if decision_at - WINDOWS[kind] <= published < decision_at:
            evidence[kind].append((published, record.get("assessment")))
    if any(value == "risk" for _, value in evidence["event"]):
        return "event_risk"
    if any(value in {"risk", "caution"} for _, value in evidence["research"]):
        return "research_negative"
    if not inverse_market_verified:
        if not evidence["market"]:
            return "missing_market"
        latest = max(at for at, _ in evidence["market"])
        if any(value != "supportive" for at, value in evidence["market"] if at == latest):
            return "market_not_supportive"
    # Require a positive coverage marker from the event feed as well.
    if not any(value == "clear" for _, value in evidence["event"]):
        return "missing_event"
    return "supported"


def research_detail(records, market, symbol, decision_at):
    """Summarise the research records that were in force at the decision."""
    rows = []
    for record in records or ():
        if not isinstance(record, dict) or record.get("kind") != "research":
            continue
        if record.get("market") not in (market, "*") or record.get("symbol") not in (symbol, "*"):
            continue
        published = datetime.fromisoformat(record["available_at"])
        if published.utcoffset() is None:
            raise ValueError("Evidence timestamps require a time zone")
        if decision_at - WINDOWS["research"] <= published < decision_at:
            rows.append({"available_at": record["available_at"], "assessment": record.get("assessment")})
    return {"count": len(rows), "latest": max((row["available_at"] for row in rows), default=None),
            "assessments": sorted({row["assessment"] for row in rows})}


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
    if len(sys.argv) < 2:
        print("usage: signal_context.py <output path>")
        return 2
    assessments = {"research": {"supportive", "neutral", "risk", "caution"},
                   "market": {"supportive", "neutral"}, "event": {"clear", "risk"}}
    try:
        records = json.loads(raw)
        if not isinstance(records, list) or not records:
            raise ValueError()
        for row in records:
            if not isinstance(row, dict):
                raise ValueError()
            if not all(isinstance(row.get(key), str) and row[key].strip()
                       for key in ("kind", "market", "symbol", "available_at", "assessment")):
                raise ValueError()
            if row["kind"] not in assessments or row["assessment"] not in assessments[row["kind"]]:
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
