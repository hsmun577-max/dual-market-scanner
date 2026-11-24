"""Per-issuer foreign and institutional demand, measured rather than assumed.

The daily scan used to leave ``institutional_flows`` as the string "미검증" for every
row: the report never said whether the foreigners and institutions that a broker screen
reads were actually buying. This module reads that evidence per issuer.

Domestic (KR) sources, in order:
  1. KIS ``inquire-investor`` (FHKST01010900) with the shared ``industry_flow.Client``;
     only when its credentials are present, as they are inside the workflow.
  2. Naver Finance deal trend (``/api/stock/{code}/trend``), which carries the daily
     foreigner and institution net purchase quantity and the foreigner holding ratio.
     This is the fallback that works without any key, and it is what the backtest below
     reads for its historical series.

Overseas (US) sources: Yahoo Finance issuer information (institutional holding share),
and where that is absent the observed dollar-volume/OBV trend is reported as a proxy.
Either way the evidence carries ``status`` so a missing observation is never shown as a
neutral one.

Net purchase is valued as quantity × that session's close for every source, so the two
providers share one unit without depending on either provider's amount field. The raw
provider amount fields are kept under ``provider_amounts`` for inspection but are not
used in a ratio.
"""
from datetime import datetime, timezone
import math
import os
import requests

WINDOW_SESSIONS = 5
# A net purchase equal to this share of the window's turnover is a strong signal; the
# score saturates near it rather than scaling forever with the size of the issuer.
MATERIAL_NET_RATIO = 0.03
INFLOW_SCORE = 0.15
OUTFLOW_SCORE = -0.15
NAVER_TREND = "https://m.stock.naver.com/api/stock/{code}/trend?pageSize={size}"
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_STATUS_MEASURED = "measured"
_STATUS_PROXY = "proxy"
_STATUS_UNAVAILABLE = "unavailable"


def number(value):
    """Parse a provider figure that may carry thousands separators or a sign prefix."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).replace(",", "").replace("%", "").replace("+", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _session(value):
    """YYYYMMDD (Naver) or YYYY-MM-DD (KIS) to one date string."""
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if len(text) >= 10 else ""


def naver_trend(code, sessions=WINDOW_SESSIONS, timeout=8):
    """Daily close, volume and investor net quantities from Naver Finance.

    Naver returns at most 60 sessions per request, newest first; ``bizdate`` is an
    exclusive upper bound, so a second call pages further back when more sessions are
    needed. Only completed sessions are carried, which is what a daily plan reads.
    """
    rows = {}
    cursor = None
    for _ in range(1 + sessions // 50):
        url = NAVER_TREND.format(code=code, size=min(60, max(sessions, 10)))
        if cursor:
            url += f"&bizdate={cursor}"
        response = requests.get(url, headers=NAVER_HEADERS, timeout=timeout)
        if response.status_code != 200:
            break
        try:
            payload = response.json()
        except ValueError:
            break
        if not isinstance(payload, list) or not payload:
            break
        fresh = 0
        for row in payload:
            day = _session(row.get("bizdate"))
            if day and day not in rows:
                rows[day] = row
                fresh += 1
        if not fresh or len(rows) >= sessions:
            break
        cursor = sorted(rows)[0].replace("-", "")
    return [{"date": day,
             "close": number(rows[day].get("closePrice")),
             "volume": number(rows[day].get("accumulatedTradingVolume")),
             "foreign_qty": number(rows[day].get("foreignerPureBuyQuant")),
             "institution_qty": number(rows[day].get("organPureBuyQuant")),
             "foreign_holding_pct": number(rows[day].get("foreignerHoldRatio"))}
            for day in sorted(rows)][-sessions:]


def kis_rows(code, client, sessions=WINDOW_SESSIONS):
    """Daily investor rows for one issuer from the KIS quote API."""
    body = client.get_body("/uapi/domestic-stock/v1/quotations/inquire-investor",
                           "FHKST01010900",
                           {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    raw = (body or {}).get("output2") or []
    if isinstance(raw, dict):
        raw = [raw]
    rows = {}
    for row in raw:
        day = _session(row.get("stck_bsop_date"))
        if not day:
            continue
        rows[day] = {"date": day,
                     "close": number(row.get("stck_clpr")),
                     "volume": number(row.get("acml_vol")),
                     "foreign_qty": number(row.get("frgn_ntby_qty")),
                     "institution_qty": number(row.get("orgn_ntby_qty")),
                     "provider_amounts": {"foreign": number(row.get("frgn_ntby_tr_pbmn")),
                                          "institution": number(row.get("orgn_ntby_tr_pbmn")),
                                          "turnover": number(row.get("acml_tr_pbmn"))}}
    return [rows[day] for day in sorted(rows)][-sessions:]


def _kis_client():
    if not (os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET")):
        return None
    try:
        from industry_flow import Client
        return Client()
    except Exception:
        return None


def _usable(rows):
    """A row needs a price and at least one investor quantity to carry a flow."""
    return [row for row in rows
            if isinstance(row.get("close"), (int, float)) and row["close"] > 0
            and (row.get("foreign_qty") is not None or row.get("institution_qty") is not None)]


def score_rows(rows):
    """Turn the window's daily investor rows into one -1..1 demand reading.

    Intensity (net purchase against the window's own turnover) carries most of the
    weight; persistence and a current streak separate a steady accumulation from one
    large print, and the foreigner holding ratio adds the same information a broker
    screen shows. Missing parts are dropped from the denominator rather than scored 0.
    """
    rows = _usable(rows)
    if not rows:
        return None
    turnover = sum(row["close"] * (row.get("volume") or 0) for row in rows)
    foreign_value = sum((row.get("foreign_qty") or 0) * row["close"] for row in rows)
    institution_value = sum((row.get("institution_qty") or 0) * row["close"] for row in rows)
    net_value = foreign_value + institution_value
    ratio = net_value / turnover if turnover > 0 else None
    buy_days = sum(1 for row in rows if (row.get("foreign_qty") or 0) + (row.get("institution_qty") or 0) > 0)
    streak = 0
    for row in reversed(rows):
        if (row.get("foreign_qty") or 0) + (row.get("institution_qty") or 0) > 0:
            streak += 1
        else:
            break
    ratios = [row.get("foreign_holding_pct") for row in rows if row.get("foreign_holding_pct") is not None]
    holding_change = ratios[-1] - ratios[0] if len(ratios) >= 2 else None
    terms = []
    if ratio is not None:
        terms.append((.45, math.tanh(ratio / MATERIAL_NET_RATIO)))
    terms.append((.30, (buy_days / len(rows) - .5) * 2))
    terms.append((.15, math.tanh(streak / 3)))
    if holding_change is not None:
        terms.append((.10, math.tanh(holding_change / .5)))
    score = sum(weight * value for weight, value in terms) / sum(weight for weight, _ in terms)
    return {"score": round(score, 4),
            "net_value": round(net_value, 2),
            "foreign_value": round(foreign_value, 2),
            "institution_value": round(institution_value, 2),
            "turnover": round(turnover, 2),
            "net_to_turnover_pct": round(100 * ratio, 3) if ratio is not None else None,
            "buy_days": buy_days, "sessions": len(rows),
            "current_buy_streak": streak,
            "foreign_holding_change_pp": round(holding_change, 3) if holding_change is not None else None,
            "first_session": rows[0]["date"], "last_session": rows[-1]["date"]}


def direction_of(score):
    if score is None:
        return None
    return "inflow" if score >= INFLOW_SCORE else "outflow" if score <= OUTFLOW_SCORE else "neutral"


def kr_evidence(symbol, at=None, client=None, sessions=WINDOW_SESSIONS):
    """Foreign and institutional demand for one domestic issuer, with its source named."""
    code = symbol.split(".")[0]
    at = at or datetime.now(timezone.utc)
    attempts = []
    client = client if client is not None else _kis_client()
    if client is not None:
        try:
            rows = _usable(kis_rows(code, client, sessions))
            if rows:
                return _evidence("KR", symbol, rows, "KIS inquire-investor", at, attempts)
            attempts.append({"source": "KIS inquire-investor", "reason": "empty_response"})
        except Exception as exc:
            attempts.append({"source": "KIS inquire-investor", "reason": type(exc).__name__})
    try:
        rows = _usable(naver_trend(code, sessions))
        if rows:
            return _evidence("KR", symbol, rows, "Naver Finance deal trend", at, attempts)
        attempts.append({"source": "Naver Finance deal trend", "reason": "empty_response"})
    except (requests.RequestException, ValueError) as exc:
        attempts.append({"source": "Naver Finance deal trend", "reason": type(exc).__name__})
    return {"status": _STATUS_UNAVAILABLE, "market": "KR", "symbol": symbol,
            "source": None, "observed_at": at.isoformat(), "attempts": attempts,
            "reason": "no_per_issuer_domestic_flow_source",
            "meaning": "foreign and institutional demand not observed for this issuer"}


def _evidence(market, symbol, rows, source, at, attempts):
    reading = score_rows(rows)
    return {"status": _STATUS_MEASURED, "market": market, "symbol": symbol, "source": source,
            "observed_at": at.isoformat(), "window_sessions": len(rows),
            "as_of": reading["last_session"], "direction": direction_of(reading["score"]),
            "score": reading["score"], "window": reading, "attempts": attempts,
            "basis": "per_issuer_foreign_and_institution_net_purchase_quantity_x_close",
            "meaning": "observed net buying, not a forecast"}


def us_evidence(symbol, company=None, indicators=None, at=None):
    """Read the overseas demand evidence that actually exists for a single issuer.

    Yahoo reports an institutional holding share per issuer but no daily net purchase,
    so a measured holding share is reported as such and, when it is absent, the
    observed dollar-volume and OBV trend is reported as a proxy. Neither is called a
    daily institutional flow.
    """
    at = at or datetime.now(timezone.utc)
    company = company or {}
    holding = number(company.get("institutional_holding_pct"))
    basic = {"market": "US", "symbol": symbol, "observed_at": at.isoformat(),
             "window_sessions": WINDOW_SESSIONS}
    if holding is not None:
        change = number(company.get("institutional_holding_change_pp"))
        score = max(-1.0, min(1.0, math.tanh((holding - 50) / 40) * .5
                              + (math.tanh(change / 2) * .5 if change is not None else 0)))
        return {**basic, "status": _STATUS_MEASURED, "source": "Yahoo Finance institutional holdings",
                "as_of": (company.get("profile_available_at") or at.isoformat())[:10],
                "institutional_holding_pct": holding,
                "institutional_holding_change_pp": change,
                "direction": direction_of(score), "score": round(score, 4),
                "window": {"sessions": 0, "institutional_holding_pct": holding},
                "basis": "quarterly_institutional_holding_share",
                "meaning": "holders of record, not a daily net purchase"}
    values = indicators or {}
    momentum = number(values.get("return_5d"))
    volume = number(values.get("volume_ratio"))
    obv = values.get("obv_rising")
    if momentum is None and obv is None:
        return {**basic, "status": _STATUS_UNAVAILABLE, "source": None,
                "reason": "no_per_issuer_overseas_flow_source",
                "meaning": "foreign and institutional demand not observed for this issuer"}
    terms = []
    if momentum is not None:
        terms.append((.6, math.tanh(momentum / .05)))
    if isinstance(obv, bool):
        terms.append((.4, .5 if obv else -.5))
    score = sum(weight * value for weight, value in terms) / sum(weight for weight, _ in terms)
    return {**basic, "status": _STATUS_PROXY, "source": "Yahoo Finance price and volume proxy",
            "as_of": values.get("data_date"), "direction": direction_of(score),
            "score": round(score, 4),
            "window": {"sessions": WINDOW_SESSIONS, "return_5d": momentum, "volume_ratio": volume,
                       "obv_rising": obv},
            "basis": "dollar_volume_and_obv_proxy",
            "meaning": "price and volume demand, not observed institutional net purchase",
            "reason": "no_daily_institutional_flow_for_single_us_issuer"}


def evidence_for(row, company=None, at=None, client=None):
    """Dispatch by market using the row the scan already holds."""
    market = (row or {}).get("market")
    symbol = (row or {}).get("symbol") or ""
    if market == "KR":
        return kr_evidence(symbol, at=at, client=client)
    return us_evidence(symbol, company, (row or {}).get("indicators"), at=at)


def share_line(evidence):
    """One short phrase for a report: who bought, how much, and against what."""
    if not evidence or evidence.get("status") == _STATUS_UNAVAILABLE:
        return "수급 미확인"
    window = evidence.get("window") or {}
    if evidence.get("status") == _STATUS_PROXY:
        return "수급 대용지표(거래대금·OBV)만 확인"
    if evidence.get("institutional_holding_pct") is not None:
        change = evidence.get("institutional_holding_change_pp")
        return (f"기관 보유 {evidence['institutional_holding_pct']:.1f}%"
                + (f" ({change:+.2f}%p)" if change is not None else ""))
    net = window.get("net_value")
    ratio = window.get("net_to_turnover_pct")
    parts = [f"외인·기관 {net/1e8:+.1f}억원" if net is not None else "외인·기관 미확인",
             f"거래대금 대비 {ratio:+.2f}%" if ratio is not None else "",
             f"{window['current_buy_streak']}일 연속 순매수" if window.get("current_buy_streak") else "",
             f"{window['buy_days']}/{window['sessions']}일 순매수" if window else ""]
    return " · ".join(part for part in parts if part)
