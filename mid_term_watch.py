"""Mid-term +30% discovery — a 20~60 session pullback list, not a same-day buy.

The 2026-11-24 study (``evidence/dual-market-scanner/상승초입-조기포착-전략연구.md``, 2부)
found where +30% moves actually start: **not on a breakout but on a pullback**. The prior
session of the 2,367 +30%-in-20-session cases sat 5.1% under its 20-day average, 6.2% down
over 20 days, RSI 43, 16.2% below its 20-day high, with volume *lower* than usual. Chart
breakout filters recall 0.1~2.7% of them — less often than they fire on ordinary sessions.

One set of point-in-time conditions survived, and it is the one wired here:

    ``20-day return <= -10%`` + ``RSI(14) <= 45`` +
    ``20-day average turnover 10억~40억원`` + ``KOSPI-relative 20-day strength > 0``

* 60 sessions held: 977 trades, **+9.54%** after 0.28% cost, **58.4%** touch +30% at the
  high, max drawdown −15.7% (the all-session baseline is 41.6% and −19.6%).
* In the falling half (2026-05-30 onward) only: +11.30% and 59.8% — it keeps working.
* 20 sessions: +9.12%, 28.7% touch +30%, 67.7% win rate.
* No fundamentals are used, so this set has no look-ahead: every input is the session's own
  price, volume and relative strength.

Two consequences are built into the module. The turnover band is the *research* band
(10억~40억원) — the same tier ``candidate_selection.quality_watch_reason`` reads — because
the study found the 10~50억 names beat the 50억+ ones (+7.88% vs +2.85% at 60 sessions).
The band top is the momentum path's own KR floor, not a separate number: the study's
10~50억 was exactly "below the 50억 floor", and the owner lowered that floor to 40억 on
2026-11-26 ("거래대금 기준 40억으로 낮춰"), so the 40~50억 slice now belongs to the momentum
buy path instead of this list. And the hold is 20~60 sessions
with a −15% stop, so **nothing here is a same-day buy signal**: entries carry
``entry_signal: False`` and live in their own report field (``mid_term_watch``) and their
own message section, never in ``recommendations`` or ``preopen_signal``.

The chart condition alone is not enough to be worth a 20~60 session hold. The 2026-11-24
scan put ``상지건설(042940)`` on the list while it carried a third-quarter operating loss
and an 82억 rights offering, so the owner asked for a fundamentals screen and a one-line
reason per name: "적자·유증 종목은 제외하고, 종목마다 최근 실적·재료 1줄 요약이 같이
뜨도록". :mod:`mid_term_evidence` supplies both; the screen runs only for the pinned
candidates (the pool is 30~50 rows, not the universe) and a provider failure keeps a name
in the list as ``unverified`` rather than dropping it.
"""

import math

from candidate_selection import MIN_AVERAGE_TURNOVER

LABEL = "[중기 +30% 눌림 발굴 (20~60일 보유 · -15% 손절)]"
MIN_RETURN_20D = -0.10
MAX_RSI = 45.0
MIN_TURNOVER = 1_000_000_000
# The band top is the momentum path's KR turnover floor (40억 as of 2026-11-26), derived
# rather than copied so the two tiers can never overlap when that floor moves. The study's
# band was 10~50억 because the floor was 50억 then.
MAX_TURNOVER = MIN_AVERAGE_TURNOVER["KR"]
MIN_RELATIVE_STRENGTH = 0.0
STOP_PCT = 0.15
HOLD_SESSIONS = (20, 60)
DEFAULT_LIMIT = 8
REFERENCE = ("60거래일 +9.54% · +30% 고점 도달 58.4% · 최대낙폭 −15.7% "
             "(977건, 2025-06~2026-11, 비용 0.28% 반영)")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def reason(item, market="KR"):
    """Why the row is not a mid-term candidate, or ``None`` when it is one.

    The conditions are read only from what the session itself showed: the 20-day return,
    RSI(14), the 20-day average turnover and the 20-day return against the index. A value
    that was never observed fails here instead of being filled in.
    """
    from candidate_selection import (instrument_rejection_reason, MIN_PRICE,
                                     average_turnover)
    if str(market) != "KR":
        # The turnover band is stated in won and the relative strength is measured
        # against KOSPI, so the study's condition is a domestic one.
        return "unsupported_market"
    values = (item or {}).get("indicators") or {}
    rejected = instrument_rejection_reason((item or {}).get("symbol"),
                                           (item or {}).get("name"),
                                           (item or {}).get("asset_type", ""))
    if rejected:
        return rejected
    price = _number(values.get("price"))
    if price is None or price <= 0:
        return "missing_indicators"
    if price < MIN_PRICE["KR"]:
        return "low_price"
    return_20d = _number(values.get("return_20d"))
    rsi = _number(values.get("rsi14"))
    relative = _number(values.get("relative_strength"))
    turnover = _number(values.get("average_turnover_20d"))
    if turnover is None and all(key in values for key in ("price", "volume", "volume_ratio")):
        turnover = average_turnover(values)
    if None in (return_20d, rsi, relative) or not turnover or turnover <= 0:
        return "missing_indicators"
    if return_20d > MIN_RETURN_20D:
        return "pullback_too_shallow"
    if rsi > MAX_RSI:
        return "rsi_above_limit"
    if not MIN_TURNOVER <= turnover <= MAX_TURNOVER:
        return "turnover_outside_research_band"
    if relative <= MIN_RELATIVE_STRENGTH:
        return "weak_relative_strength"
    return None


def entry(item, at=None, evidence=None):
    """The observation record for a qualifying row (never an entry signal)."""
    values = item.get("indicators") or {}
    record = {
        "market": item.get("market") or "KR",
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "price": _number(values.get("price")),
        "return_20d": _number(values.get("return_20d")),
        "rsi14": _number(values.get("rsi14")),
        "average_turnover": _number(values.get("average_turnover_20d")),
        "relative_strength": _number(values.get("relative_strength")),
        "data_date": values.get("data_date"),
        "hold_sessions": list(HOLD_SESSIONS),
        "stop_pct": STOP_PCT,
        "entry_signal": False,
        "reason": "pullback_with_relative_strength_inside_research_turnover_band",
        "reference": REFERENCE,
        "observed_at": at.isoformat() if at is not None else None,
    }
    if evidence is not None:
        import mid_term_evidence
        record["catalyst_summary"] = mid_term_evidence.summary(evidence, at)
        record["financial"] = evidence.get("financial")
        record["recent_material"] = mid_term_evidence.material(
            evidence, at, financial=evidence.get("financial"))
        record["evidence_status"] = {
            "status": evidence.get("status"),
            "financial": evidence.get("financial_status"),
            "filings": evidence.get("filing_status"),
            "news": evidence.get("news_status"),
            "verified": evidence.get("verified"),
        }
    return record


def attach(report, at=None, limit=DEFAULT_LIMIT, evidence=None):
    """Build ``report['mid_term_watch']`` from the rows the scan already holds.

    Every scanned row carries its own price, volume and relative strength, so the chart
    condition needs no company analysis and touches neither ``preopen_signal`` nor
    ``buy_evaluation``. Rows excluded on the way are counted by reason so a short list is
    not a silent one.

    ``evidence`` is the fundamentals reader (:func:`mid_term_evidence.collect` in the scan).
    When it is supplied, the strongest candidates are screened for a recent operating/net
    loss and for a rights offering before the list is cut to ``limit``, and each kept entry
    carries its ``catalyst_summary`` line. Without it — as in the unit tests and in any
    offline replay — the list is exactly the chart result it was before.
    """
    counts = {}
    pool = []
    from research_asset import is_fund
    for item in report.get("results") or []:
        if item.get("market") != "KR":
            continue
        if is_fund(item):
            counts["fund"] = counts.get("fund", 0) + 1
            continue
        rejected = reason(item, "KR")
        if rejected:
            counts[rejected] = counts.get(rejected, 0) + 1
            continue
        pool.append(item)
    pool.sort(key=lambda item: (-(_number((item.get("indicators") or {}).get("relative_strength"))
                                  or 0), item.get("symbol") or ""))
    screen = None
    if evidence is not None and pool:
        import mid_term_evidence
        screen = mid_term_evidence.screen(pool, at, evidence)
        for rejected, count in screen["excluded"].items():
            counts[rejected] = counts.get(rejected, 0) + count
        chosen = [(row["item"], row["evidence"]) for row in screen["kept"]]
    else:
        chosen = [(item, None) for item in pool]
    entries = [entry(item, at, evidence) for item, evidence in chosen[:max(0, limit)]]
    report["mid_term_watch"] = entries
    scope = dict(
        evaluated=len(report.get("results") or []),
        qualified=len(pool),
        listed=len(entries),
        turnover_band=[MIN_TURNOVER, MAX_TURNOVER],
        conditions=dict(max_return_20d=MIN_RETURN_20D, max_rsi=MAX_RSI,
                        min_relative_strength=MIN_RELATIVE_STRENGTH),
        hold_sessions=list(HOLD_SESSIONS), stop_pct=STOP_PCT,
        entry_signal=False, excluded=counts)
    if screen is not None:
        # How many of the listed names were checked against real earnings, how many went in
        # unverified, and what the fundamentals screen threw out.
        scope["fundamentals_screen"] = dict(
            screened=screen["screened"],
            excluded=screen["excluded"],
            unverified=screen["unverified"],
            rules=["operating_or_net_loss", "recent_rights_offering"],
            window_days=mid_term_evidence.LOOKBACK_DAYS)
    report["mid_term_watch_scope"] = scope
    if at is not None:
        report["mid_term_watch_at"] = at.isoformat()
    return entries


def _turnover_text(value):
    if value is None:
        return None
    return f"{value / 1e8:.1f}억"


def line(entry_row):
    """One message line pair for an observation record."""
    name = entry_row.get("name") or entry_row.get("symbol") or ""
    code = (entry_row.get("symbol") or "").split(".")[0]
    numbers = []
    if entry_row.get("return_20d") is not None:
        numbers.append(f"20일 {entry_row['return_20d']:+.1%}")
    if entry_row.get("rsi14") is not None:
        numbers.append(f"RSI {entry_row['rsi14']:.1f}")
    if entry_row.get("average_turnover") is not None:
        numbers.append(f"거래대금 {_turnover_text(entry_row['average_turnover'])}")
    if entry_row.get("relative_strength") is not None:
        numbers.append(f"코스피 대비 {entry_row['relative_strength']:+.1%}p")
    head = f"• {name} ({code})"
    if numbers:
        head += " · " + " · ".join(numbers)
    lines = [head]
    if "catalyst_summary" in entry_row:
        # Every listed name carries the line, including the ones no filing or article was
        # found for, so the shape of the section never changes with the news day.
        lines.append("   재료: " + (entry_row.get("catalyst_summary") or "최근 공시·뉴스 없음"))
    lines.append("  20~60일 보유 · -15% 손절 전제 · 매수 신호 아님")
    return "\n".join(lines)


def format_block(entries):
    """The separate section for the daily/preopen message, or an empty list."""
    rows = [row for row in entries or [] if row.get("entry_signal") is not True]
    if not rows:
        return []
    return [LABEL,
            "20일 눌림 + 거래대금 10~40억 + 코스피 대비 상대강도 유지 · 당일 매수 신호 아님",
            *(line(row) for row in rows[:DEFAULT_LIMIT])]
