"""Confirmed-catalyst track: a reported, quantified positive event can lift a name that
the momentum screen rejected, and nothing else is lifted with it.

The owner's case (지니언스 263860.KQ, 2026-11-12): the first screen rejected the name as a
plain ``rebound`` under a ``market_headwind``, and only afterwards did the scanner read the
disclosure — a 3Q operating-profit jump — that explained the move. The screen's order was
wrong, not its evidence. So this track reads the confirmed disclosure first and asks
whether the chart rejection was one of the reasons a catalyst is allowed to override.

Accepted evidence, both point-in-time and both quantified:

* a **reported operating-profit jump** from the income statement (DART for KR, SEC/Yahoo
  statements for US), with a disclosure time that is known and recent. A forward estimate
  or a "기대" headline is not this;
* a **confirmed supply contract** whose DART document carries the amount and the share of
  revenue — the number, not the title;
* a **news headline that carries the figure** (``영업익 253% 급증``, ``1,200억 수주``) and
  comes from the issuer's own feed, with a body that was actually read. Expectations,
  검토/추진/전망, MOU and 협약 are refused even when they contain a number: an intention is
  not a disclosure.

The safety blocks stay in force — a catalyst never lifts insufficient liquidity, a low
price, a breakdown, an overheated/extended price, or a listed fund. And a lifted name is
still refused at the moment of entry when the session is selling the news: below its
opening price, or without same-time volume expansion (``entry_gate``).
"""

from datetime import datetime, time, timedelta
import math
import re
from zoneinfo import ZoneInfo

from candidate_selection import (MIN_DEMAND_VOLUME_RATIO, average_turnover,
                                 candidate_rejection_reason, liquidity_rejection_reason)
from research_asset import is_fund

KST = ZoneInfo("Asia/Seoul")

# First-screen reasons a confirmed catalyst may override.
LIFTABLE_REASONS = frozenset({"rebound", "weak_relative_trend", "unconfirmed_uptrend",
                              "unconfirmed_demand", "insufficient_demand_expansion",
                              "market_headwind"})
# Safety blocks the track may never override. They are listed so a change to the first
# screen cannot silently widen this track.
SAFETY_REASONS = frozenset({"insufficient_liquidity", "low_price", "missing_indicators",
                            "special_security", "cash_or_bond_product", "complex_product",
                            "breakdown", "extended_price", "fund_screen_only"})
# A reported jump of this size is a different quarter, not noise. The reference case is
# +253% operating profit year over year.
MIN_OPERATING_SURPRISE_PCT = 50.0
# A supply contract matters to the income statement at this share of revenue.
MIN_CONTRACT_REVENUE_RATIO_PCT = 5.0
# The disclosure has to be recent relative to the decision it supports, and "recent"
# depends on how often that kind of evidence is published. A quarterly report comes four
# times a year, so a 72-hour window left the strongest evidence readable for three days of
# the quarter: on 2026-11-24 테스 (+192.5% operating profit YoY) and 이수페타시스 (+62.4%)
# were both refused for age alone, eight days after the 3Q reports were filed. A contract
# or a headline is a same-week event and keeps the short window.
CATALYST_FRESH_HOURS = 72
EARNINGS_CATALYST_FRESH_DAYS = 21
EARNINGS_CATALYST_FRESH_HOURS = EARNINGS_CATALYST_FRESH_DAYS * 24
# Keyed by the finder's ``kind`` so the window can never be read off the wrong evidence.
CATALYST_FRESH_HOURS_BY_KIND = {
    "earnings_surprise": EARNINGS_CATALYST_FRESH_HOURS,
    "confirmed_contract": CATALYST_FRESH_HOURS,
    "confirmed_news": CATALYST_FRESH_HOURS,
}
# A lifted name still needs a composite score that is not net negative: a catalyst cannot
# carry an issuer whose own evidence points the other way.
MIN_CATALYST_COMPOSITE_SCORE = 45.0
# Same-time volume expansion required before an entry on a catalyst name; the same 1.2x
# demand floor the daily selection uses.
MIN_ENTRY_VOLUME_RATIO = MIN_DEMAND_VOLUME_RATIO
LABEL = "[실적·호재 촉매]"

_QUANTIFIED = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|％|억|조|원|건|대|톤|명)")
_EARNINGS_WORDS = re.compile(r"영업이익|영업익|매출액|매출|순이익|당기순익|어닝|실적|"
                             r"operating (?:profit|income)|revenue|net income|earnings", re.I)
_CONTRACT_WORDS = re.compile(r"수주|공급\s*계약|단일판매|계약\s*체결|수주액|납품|"
                             r"\borders?\b|\bcontract|\baward", re.I)
_APPROVAL_WORDS = re.compile(r"승인|허가|인가|품목\s*허가|approval|cleared|authoriz", re.I)
_POSITIVE_WORDS = re.compile(r"급증|증가|증대|호조|흑자|서프라이즈|상향|최대|기록|돌파|개선|"
                             r"surge|jump|beat|record|higher", re.I)
# A signed contract or an approval is positive by itself; its headline does not need a
# growth word. A cancellation or a cut is the opposite and is refused below.
_NEGATIVE_WORDS = re.compile(r"해지|취소|철회|중단|무산|감소|하락|부진|적자|손실|정정|"
                             r"cancel|terminat|withdraw|suspend|recall|declin|cut|lawsuit", re.I)
_CONFIRMED_VERB = re.compile(r"공시|체결|확정|발표|달성|기록|수주|계약|승인|허가|급증|증가|흑자")
# An intention with a number is still an intention.
_SPECULATION = re.compile(r"추진|검토|기대|전망|가능성|관측|예상|논의|협약|MOU|양해각서|의향서|"
                          r"LOI|우선협상|설$|카더라|"
                          r"expects?\b|plans?\b|may\b|could\b|talks?\b|consider|proposal|"
                          r"would\b|likely", re.I)
_EARNINGS_FILING = re.compile(r"분기보고서|반기보고서|사업보고서|잠정\s*실적|실적\s*공시|"
                              r"\b(?:10-Q|10-K|20-F|6-K)\b", re.I)
# 정기 임원 인사는 회사의 경영 변화가 아니다. "부사장 4명 등 11명 승진"처럼 승진·인사 발령을
# 알리는 기사를 `CEO·경영진` 촉매로 올리면, 알림에는 회사와 무관한 인사 공지가 매일 실린다
# (2026-11-24 장전 블록의 삼성SDI·삼성바이오로직스 두 줄). 실제 경영진 교체만 남긴다.
_PERSONNEL_NOISE = re.compile(r"승진|인사\s*(?:발령|개편|이동|교류|단행)|정기\s*인사|임원\s*인사|"
                              r"전보|보직|연임|퇴임식", re.I)
_EXECUTIVE_CHANGE = re.compile(r"대표이사|\bCEO\b|chief executive|회장|최고경영자|취임|사임|해임|"
                               r"유임|복귀|교체|선임|resign|step(?:s|ped|ping)? down|appoint", re.I)


def personnel_only(headline):
    """A routine promotion/appointment notice rather than a change at the top."""
    text = str(headline or "")
    return bool(_PERSONNEL_NOISE.search(text)) and not _EXECUTIVE_CHANGE.search(text)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _finite(value):
    return _number(value) is not None


def freshness_hours(kind):
    """How long evidence of this kind stays usable, in hours."""
    return CATALYST_FRESH_HOURS_BY_KIND.get(str(kind), CATALYST_FRESH_HOURS)


def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def safety_block_reason(row, market):
    """The reason this track must not lift, or None when only a soft reason remains."""
    if is_fund(row):
        return "fund_screen_only"
    reason = liquidity_rejection_reason(row, market)
    if reason:
        return reason
    values = row["indicators"]
    if values.get("breakdown"):
        return "breakdown"
    ma20 = _number(values.get("ma20"))
    if (values.get("overheated") or (_number(values.get("return_5d")) or 0) > 0.12
            or (ma20 and ma20 > 0 and values["price"] / ma20 > 1.08)):
        # Already moved: the news is in the price and the +6% target is behind it.
        return "extended_price"
    return None


def liftable_reason(row, market):
    """The first-screen rejection a confirmed catalyst may override, else None."""
    if safety_block_reason(row, market):
        return None
    first = candidate_rejection_reason(row, market)
    if first == "breakdown_or_rebound":
        values = row["indicators"]
        if not (values.get("rebound") and not values.get("breakdown")):
            return None
        first = "rebound"
    return first if first in LIFTABLE_REASONS else None


def watch_candidates(pool, market, limit=6):
    """Reserve research slots for rejected names a disclosure could still lift.

    The budget is spent before any news is read, so only the shape that makes a catalyst
    relevant is scored here: a rebound or a trend that has not confirmed itself yet, on
    real volume. Safety-blocked and already-passing names are never included.
    """
    rows = []
    for row in pool:
        if liftable_reason(row, market) is None:
            continue
        if (_number(row["indicators"].get("volume_ratio")) or 0) < MIN_DEMAND_VOLUME_RATIO:
            continue
        rows.append(row)
    return sorted(rows, key=lambda row: (
        -int(bool(row["indicators"].get("rebound"))),
        -min(row["indicators"]["volume_ratio"], 5),
        -average_turnover(row["indicators"]),
        row["symbol"]))[:max(0, limit)]


def filing_time(filing, market=None):
    """When the filing became public, or None when only an unusable date is known.

    DART's index carries the receipt *date* only, so a day-dated filing is treated as
    public at the end of that day and never earlier; a decision time before that cannot
    use it.
    """
    for key in ("available_at", "accepted_at", "published_at"):
        observed = stamp(filing.get(key))
        if observed:
            return observed
    try:
        day = datetime.fromisoformat(str(filing.get("filing_date"))[:10]).date()
    except (TypeError, ValueError):
        return None
    zone = ZoneInfo("America/New_York") if str(market) == "US" else KST
    return datetime.combine(day, time(18, 0), tzinfo=zone)


def _is_earnings_filing(filing):
    form = str(filing.get("form") or "").strip()
    if re.fullmatch(r"8-K(?:/A)?", form, re.I):
        # Only item 2.02 is a results release; the other 8-K items are other events.
        return "2.02" in {str(code).strip() for code in filing.get("items") or []}
    return bool(_EARNINGS_FILING.search(form))


def earnings_disclosure(company, at, market=None, hours=EARNINGS_CATALYST_FRESH_HOURS):
    """The most recent results disclosure that was already public before ``at``."""
    best = None
    for filing in company.get("filings") or []:
        if not isinstance(filing, dict) or not _is_earnings_filing(filing):
            continue
        observed = filing_time(filing, market)
        if observed is None or observed > at or at - observed > timedelta(hours=hours):
            continue
        if best is None or observed > best[0]:
            best = (observed, filing)
    return best


def quarter_label(value):
    try:
        year, month = int(str(value)[:4]), int(str(value)[5:7])
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    return f"{year}년 {month // 3}분기"


def earnings_surprise(company, at, market=None):
    """A reported operating-profit jump, dated by its own disclosure."""
    income = _number(company.get("operating_income_amount"))
    if income is None or income <= 0:
        return None  # a loss is not a positive surprise
    yoy = _number(company.get("operating_income_yoy_pct"))
    turnaround = company.get("operating_income_turnaround") is True
    if not turnaround and (yoy is None or yoy < MIN_OPERATING_SURPRISE_PCT):
        return None
    disclosure = earnings_disclosure(company, at, market, hours=freshness_hours("earnings_surprise"))
    if disclosure is None:
        return None
    observed, filing = disclosure
    label = quarter_label(company.get("quarter")) or "최근 분기"
    change = "흑자전환" if turnaround and (yoy is None or yoy <= 0) else f"{yoy:+.0f}%"
    source = "DART" if "DART" in str(company.get("financial_source")) else "재무제표"
    return dict(kind="earnings_surprise",
                summary=f"{label} 영업이익 전년 대비 {change} ({source} 실적 공시)",
                observed_at=observed.isoformat(), source=filing.get("url"),
                source_name=filing.get("form") or source,
                metrics=dict(operating_income_amount=income, operating_income_yoy_pct=yoy,
                             operating_margin_pct=_number(company.get("operating_margin_pct")),
                             quarter=company.get("quarter")),
                basis="reported income statement; not a forward estimate")


def confirmed_contract(company, at, market=None, hours=None):
    """A supply contract whose amount and revenue share were read from the document."""
    hours = freshness_hours("confirmed_contract") if hours is None else hours
    for filing in company.get("filings") or []:
        contract = filing.get("contract") or {}
        ratio = _number(contract.get("revenue_ratio_pct"))
        amount = _number(contract.get("amount_krw"))
        if ratio is None or ratio < MIN_CONTRACT_REVENUE_RATIO_PCT or amount is None or amount <= 0:
            continue
        observed = filing_time(filing, market)
        if observed is None or observed > at or at - observed > timedelta(hours=hours):
            continue
        return dict(kind="confirmed_contract",
                    summary=f"단일판매·공급계약 {amount / 1e8:,.0f}억원 (매출대비 {ratio:.1f}%)",
                    observed_at=observed.isoformat(), source=filing.get("url"),
                    source_name=filing.get("form") or "DART",
                    metrics=dict(amount_krw=amount, revenue_ratio_pct=ratio),
                    basis="DART contract document; the figure is the filed one")
    return None


def quantified_catalyst(text):
    """Whether a headline states a confirmed figure rather than an intention."""
    headline = str(text or "").strip()
    if not headline or _SPECULATION.search(headline) or _NEGATIVE_WORDS.search(headline):
        return False
    if not _QUANTIFIED.search(headline) or not _CONFIRMED_VERB.search(headline):
        return False
    signed = bool(_CONTRACT_WORDS.search(headline) or _APPROVAL_WORDS.search(headline))
    if not (_EARNINGS_WORDS.search(headline) or signed):
        return False
    return signed or bool(_POSITIVE_WORDS.search(headline))


def confirmed_news(company, at, market=None, hours=None):
    """Fresh, issuer-scoped news whose own headline carries the confirmed figure."""
    hours = freshness_hours("confirmed_news") if hours is None else hours
    urls = {str(row.get("url")) for row in company.get("news") or [] if row.get("url")}
    analysis = company.get("event_analysis") or {}
    for event in analysis.get("assessed_events") or []:
        url = str(event.get("url") or "")
        if url not in urls:
            continue  # a market-wide story is not this issuer's disclosure
        if event.get("body_status") != "public_body":
            continue  # a headline nobody could read is not a verified publication
        headline = str(event.get("headline") or "").strip()
        if not quantified_catalyst(headline):
            continue
        observed = stamp(event.get("available_at"))
        if observed is None or observed > at or at - observed > timedelta(hours=hours):
            continue
        return dict(kind="confirmed_news", summary=headline[:120],
                    observed_at=observed.isoformat(), source=url,
                    source_name=event.get("source") or "기사",
                    metrics={}, basis="headline figure in a body that was read")
    return None


def confirmed_catalyst(company, at, market=None):
    """The strongest confirmed positive catalyst known before ``at``, or not eligible."""
    result = dict(eligible=False, reason="no_confirmed_catalyst")
    if not company:
        return result
    if at.utcoffset() is None:
        raise ValueError("Aware decision time required")
    market = market or company.get("market")
    for finder in (earnings_surprise, confirmed_contract, confirmed_news):
        found = finder(company, at, market)
        if found:
            return {**found, "eligible": True, "reason": "confirmed_catalyst", "label": LABEL}
    return result


def entry_gate(price, day_open, volume_ratio, min_volume_ratio=MIN_ENTRY_VOLUME_RATIO):
    """Whether a catalyst name may be entered now: hold the open, on expanding volume.

    A headline is not demand. Trading under the session's opening price means the market
    is selling the news, and same-time volume that has not expanded means the disclosure
    is not being bought; both refuse the entry and name which one it was.
    """
    price, day_open, volume_ratio = _number(price), _number(day_open), _number(volume_ratio)
    if price is None or day_open is None or day_open <= 0:
        return dict(allowed=False, reason="catalyst_session_unverified",
                    detail="시가·현재가를 확인하지 못해 촉매 진입을 보류")
    if price < day_open:
        return dict(allowed=False, reason="catalyst_sell_on_news",
                    detail=f"현재가 {price:,.0f} < 시가 {day_open:,.0f} · 뉴스에 매도(음봉)")
    if volume_ratio is None:
        return dict(allowed=False, reason="catalyst_volume_unverified",
                    detail="동시간 거래량 기준을 확인하지 못해 촉매 진입을 보류")
    if volume_ratio < min_volume_ratio:
        return dict(allowed=False, reason="catalyst_demand_shortfall",
                    detail=f"동시간 거래량 {volume_ratio:.2f}배 < 기준 {min_volume_ratio:.2f}배 · 수급 미달")
    return dict(allowed=True, reason="catalyst_entry_conditions_met",
                detail=f"시가 이상 · 동시간 거래량 {volume_ratio:.2f}배")
