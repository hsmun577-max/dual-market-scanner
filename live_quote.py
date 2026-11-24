"""Live quotes for the names a report names, when their market is trading.

The scan is a daily-close model: every indicator, score and decision reads completed
sessions, so the alert could only ever show a limit price. The owner asked why the scanner
never confirms a live quote while the intraday watch does (2026-11-24:
"스캐너 실시간호가 왜확인안돼?"), and approved reading it on the manual and scheduled scans
too ("그렇게해").

This module reads the same KIS REST quote the intraday fallback already uses
(``rest_fallback.domestic_quote`` / ``overseas_quote``) — one call per named instrument —
and attaches it to the rows the message actually prints: the conditional plans, the
quality-watch block and the urgent-issue block. The number of calls is capped per market,
so a scan that names fifty issuers does not turn into fifty quote requests.

Three rules hold:

* **A quote is not an entry confirmation.** It is stored with the provider's own time and
  labelled "지금 호가(체결 아님)"; no score, gate or limit price reads it.
* **A closed market is not a failed call.** Outside the session (or on a non-trading day)
  the row says ``market_closed`` and no request is made.
* **A missing credential or a provider error never fails the scan.** The row says
  ``credentials_missing`` / ``unavailable`` and the report still goes out. The report's
  limitations text is rewritten to match what actually happened.
"""
import math
from zoneinfo import ZoneInfo

import exchange_calendars
import requests

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
# Bounds on API calls in one report: a market's plans and watch blocks never exceed these.
DEFAULT_LIMIT = 8
URGENT_LIMIT = 3
LABELS = {
    "market_closed": "장 마감 · 호가 미확인",
    "credentials_missing": "호가 조회 자격증명 없음",
    "unavailable": "호가 확인 실패",
    "not_selected": "호가 미조회",
}
MEANING = "브로커 현재가·1호가(체결 아님)"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def session_state(market, at):
    """``open`` / ``closed`` / ``unknown`` for the row's own market at ``at``.

    ``unknown`` means the exchange calendar could not answer; the caller then tries the
    quote rather than reporting a session state it did not observe.
    """
    if at is None or at.utcoffset() is None:
        return "unknown"
    kr = str(market).upper() == "KR"
    zone = KST if kr else NY
    local = at.astimezone(zone)
    day = local.date().isoformat()
    try:
        calendar = exchange_calendars.get_calendar("XKRX" if kr else "XNYS")
        if not calendar.is_session(day):
            return "closed"
        opened = calendar.session_open(day).to_pydatetime().astimezone(zone)
        closed = calendar.session_close(day).to_pydatetime().astimezone(zone)
    except Exception:
        return "unknown"
    return "open" if opened <= local < closed else "closed"


def _client():
    """A KIS client, or the reason there is none. Never raises."""
    try:
        from industry_flow import Client
        return Client(), None
    except Exception as exc:  # missing or unusable credentials
        return None, type(exc).__name__


def fetch(market, symbol, at, client=None):
    """One quote for one instrument, with the reason it is not there when it is not."""
    state = session_state(market, at)
    record = {"market": market, "symbol": symbol, "checked_at": at.isoformat() if at else None,
              "meaning": MEANING}
    if state == "closed":
        return {**record, "status": "market_closed",
                "reason": "outside_the_trading_session"}
    client = client or _client()[0]
    if client is None:
        return {**record, "status": "credentials_missing",
                "reason": "KIS_APP_KEY_OR_SECRET_not_set"}
    import rest_fallback
    code = str(symbol or "").split(".")[0] if str(market).upper() == "KR" else str(symbol)
    try:
        quote = (rest_fallback.overseas_quote(client, code) if str(market).upper() != "KR"
                 else rest_fallback.domestic_quote(client, code))
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return {**record, "status": "unavailable", "reason": type(exc).__name__}
    if not quote:
        return {**record, "status": "unavailable", "reason": "no_order_book_returned"}
    ask, bid = _number(quote.get("ask")), _number(quote.get("bid"))
    if ask is None or bid is None or ask <= 0 or bid <= 0:
        return {**record, "status": "unavailable", "reason": "incomplete_order_book"}
    return {**record, "status": "observed", "price": _number(quote.get("price")),
            "bid": bid, "ask": ask, "spread_pct": _number(quote.get("spread_pct")),
            "source": quote.get("source"), "exchange": quote.get("exchange"),
            "session": state}


def _targets(report, limit):
    """The rows the message prints, per market, capped so the calls stay bounded."""
    chosen = {}
    for row in report.get("results", []):
        plan = row.get("preopen_signal") or {}
        if plan.get("status") != "conditional_buy":
            continue
        chosen.setdefault(row.get("market"), [])
        if len(chosen[row.get("market")]) < limit:
            chosen[row.get("market")].append(row)
    for entry in report.get("quality_watch") or []:
        if entry.get("grade") != "supply_inflow_quality":
            continue
        market = entry.get("market") or "KR"
        bucket = chosen.setdefault(market, [])
        if len(bucket) < limit and all(row.get("symbol") != entry.get("symbol") for row in bucket):
            # A watch entry is not a scan row; it carries the same symbol and market.
            bucket.append(entry)
    return chosen


def attach(report, at, limit=DEFAULT_LIMIT, client=None):
    """Read one quote per named candidate and store it on the row.

    Returns the report-level summary so a caller (or a later reader of ``latest.json``)
    can tell what was actually observed without walking every row.
    """
    targets = _targets(report, limit)
    client, client_reason = (client, None) if client is not None else _client()
    summary = {"checked_at": at.isoformat(), "observed": 0, "market_closed": 0,
               "credentials_missing": 0, "unavailable": 0, "markets": {}}
    for market, rows in sorted(targets.items()):
        if not market:
            continue
        for row in rows:
            if client is None:
                quote = {"market": market, "symbol": row.get("symbol"), "status":
                         "credentials_missing", "reason": client_reason,
                         "checked_at": at.isoformat(), "meaning": MEANING}
            else:
                quote = fetch(market, row.get("symbol"), at, client)
            row["live_quote"] = quote
            status = quote.get("status")
            summary[status] = summary.get(status, 0) + 1
            summary["markets"][market] = summary["markets"].get(market, 0) + 1
    report["live_quotes"] = summary
    _rewrite_limitations(report, summary)
    return summary


def attach_urgent(issues, at, limit=URGENT_LIMIT, client=None):
    """Attach a quote to the urgent-issue items as well (at most ``limit`` per market)."""
    if not isinstance(issues, dict):
        return issues
    client, _reason = (client, None) if client is not None else _client()
    for market, items in issues.items():
        for item in (items or [])[:limit]:
            if client is None:
                item["live_quote"] = {"market": market, "symbol": item.get("symbol"),
                                      "status": "credentials_missing",
                                      "checked_at": at.isoformat(), "meaning": MEANING}
            else:
                item["live_quote"] = fetch(market, item.get("symbol"), at, client)
    return issues


def quote_line(quote, market=None):
    """One short line for the message, or ``None`` when there is nothing to say."""
    if not quote:
        return None
    status = quote.get("status")
    if status != "observed":
        return LABELS.get(status, LABELS["unavailable"])
    price, bid, ask = quote.get("price"), quote.get("bid"), quote.get("ask")
    spread = quote.get("spread_pct")
    fmt = (lambda value: f"${value:,.2f}") if str(market or quote.get("market")).upper() != "KR" \
        else (lambda value: f"{value:,.0f}원")
    parts = []
    if price is not None:
        parts.append(f"현재가 {fmt(price)}")
    parts.append(f"매수 {fmt(bid)} / 매도 {fmt(ask)}")
    if spread is not None:
        parts.append(f"스프레드 {spread:.2f}%")
    stamp = str(quote.get("checked_at") or "")[11:16]
    head = " · ".join(parts) + (f" ({stamp})" if stamp else "")
    return head + " · 체결 아님"


def _rewrite_limitations(report, summary):
    """Say what actually happened with quotes instead of a fixed '미포함' line."""
    limitations = [row for row in report.get("limitations") or []
                   if not (isinstance(row, str) and ("실시간 호가" in row or "일봉 기준" in row
                                                    or "일봉 기반" in row or "VWAP" in row))]
    if summary.get("observed"):
        limitations.append(f"후보 {summary['observed']}종목의 현재 호가는 KIS REST로 조회했고, "
                           "지표·점수·판정은 완료된 거래일 일봉 종가 기준이며 실시간 매수 확정은 아님")
    elif summary.get("market_closed"):
        limitations.append("장 마감(또는 휴장)으로 현재 호가는 조회하지 않았고, "
                           "지표·점수·판정은 완료된 거래일 일봉 종가 기준이며 실시간 매수 확정은 아님")
    elif summary.get("credentials_missing"):
        limitations.append("KIS 자격증명이 없어 현재 호가는 조회하지 않았고, "
                           "지표·점수·판정은 완료된 거래일 일봉 종가 기준이며 실시간 매수 확정은 아님")
    else:
        limitations.append("현재 호가를 확인하지 못했고, 지표·점수·판정은 완료된 거래일 일봉 종가 "
                           "기준이며 실시간 매수 확정은 아님")
    limitations.append("실시간 VWAP 미구현")
    report["limitations"] = limitations
