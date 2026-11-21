"""KIS REST fallback for the five-minute decision when the realtime feed is down.

The websocket feed carries the opening bars, but a disconnect before the decision used to
end the whole run. These calls rebuild the same three five-minute bars from KIS REST and
take one live quote, so the rule can still be applied — and when a quote is unavailable
for an issuer, no entry is claimed for it.
"""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BASE = "https://openapi.koreainvestment.com:9443"
KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
US_EXCHANGES = ("NAS", "NYS", "AMS")


def _client():
    from industry_flow import Client
    return Client()


def _get(client, path, tr_id, params):
    response = requests.get(BASE + path, headers={**client.headers, "tr_id": tr_id},
                            params=params, timeout=6)
    if response.status_code != 200:
        return {}
    body = response.json()
    return body if body.get("rt_cd") == "0" else {}


def _number(value):
    try:
        if isinstance(value, bool):
            return None
        number = float(str(value).replace(",", ""))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def bars_from_rows(rows, opens, closes, zone, date_keys=("xymd",), time_keys=("xhms",),
                   price_keys=("last",), volume_keys=("evol",),
                   high_keys=("high",), low_keys=("low",)):
    """Aggregate one-minute rows into the five-minute slots the rule evaluates.

    Rows outside ``[opens, closes)`` are dropped rather than shifted into a slot: a bar the
    market did not trade is not an observation.
    """
    slots = {}
    for row in rows:
        day = next((str(row.get(key)) for key in date_keys if row.get(key)), "")
        clock = next((str(row.get(key)) for key in time_keys if row.get(key)), "")
        if len(day) != 8 or len(clock) < 6:
            continue
        try:
            at = datetime.strptime(day + clock[:6], "%Y%m%d%H%M%S").replace(tzinfo=zone)
        except ValueError:
            continue
        if not opens <= at < closes:
            continue
        price = next((_number(row.get(key)) for key in price_keys
                      if _number(row.get(key)) is not None), None)
        volume = next((_number(row.get(key)) for key in volume_keys
                       if _number(row.get(key)) is not None), None)
        high = next((_number(row.get(key)) for key in high_keys
                     if _number(row.get(key)) is not None), price)
        low = next((_number(row.get(key)) for key in low_keys
                    if _number(row.get(key)) is not None), price)
        if price is None or volume is None or price <= 0 or volume < 0:
            continue
        slot = int((at - opens).total_seconds()) // 300
        bar = slots.get(slot)
        if bar is None:
            slots[slot] = {"high": high, "low": low, "close": price, "volume": volume,
                           "last": at.isoformat()}
            continue
        bar["high"] = max(bar["high"], high)
        bar["low"] = min(bar["low"], low)
        bar["close"], bar["last"] = price, at.isoformat()
        bar["volume"] += volume
    return slots


def domestic_bars(client, symbol, opens, closes):
    body = _get(client, "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                "FHKST03010200",
                {"FID_ETC_CLS_CODE": "1", "FID_INPUT_ISCD": symbol,
                 "FID_INPUT_HOUR_1": "", "FID_PW_DATA_INCU_YN": "N"})
    rows = body.get("output2") or []
    return bars_from_rows(rows, opens, closes, KST,
                          date_keys=("stck_bsop_date",), time_keys=("stck_cntg_hour",),
                          price_keys=("stck_prpr",), volume_keys=("cntg_vol",),
                          high_keys=("stck_hgpr",), low_keys=("stck_lwpr",))


def domestic_quote(client, symbol):
    body = _get(client, "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
    output = body.get("output") or {}
    ask, bid = _number(output.get("askp1")), _number(output.get("bidp1"))
    if not ask or not bid or ask <= 0 or bid <= 0 or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "spread_pct": round((ask - bid) * 200 / (ask + bid), 4),
            "price": _number(output.get("stck_prpr")), "source": "KIS REST inquire-price"}


def overseas_bars(client, symbol, opens, closes, exchange=None):
    for code in ([exchange] if exchange else US_EXCHANGES):
        body = _get(client, "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
                    "HHDFS76950200",
                    {"AUTH": "", "EXCD": code, "SYMB": symbol, "NMIN": "1",
                     "PINC": "1", "NEXT": "", "NREC": "120", "FILL": "", "KEYB": ""})
        rows = body.get("output2") or []
        bars = bars_from_rows(rows, opens, closes, NY)
        if bars:
            return bars
    return {}


def overseas_quote(client, symbol, exchange=None):
    for code in ([exchange] if exchange else US_EXCHANGES):
        body = _get(client, "/uapi/overseas-price/v1/quotations/inquire-asking-price-exp-ccn",
                    "HHDFS76200200", {"AUTH": "", "EXCD": code, "SYMB": symbol})
        output = body.get("output1") or body.get("output2") or {}
        ask = next((_number(output.get(key)) for key in ("pask1", "askp1", "ask1")
                    if _number(output.get(key))), None)
        bid = next((_number(output.get(key)) for key in ("pbid1", "bidp1", "bid1")
                    if _number(output.get(key))), None)
        if ask and bid and ask > 0 and bid > 0 and ask >= bid:
            return {"bid": bid, "ask": ask, "spread_pct": round((ask - bid) * 200 / (ask + bid), 4),
                    "price": _number((body.get("output2") or output).get("last")),
                    "source": "KIS REST overseas asking price", "exchange": code}
    return None


def market_snapshot(market, symbols, opens, closes, client=None):
    """Five-minute bars and one live quote per symbol, or an empty result per symbol."""
    client = client or _client()
    overseas = str(market).startswith("US")
    bars, quotes = {}, {}
    for symbol in symbols:
        code = symbol[4:] if overseas else symbol
        try:
            if overseas:
                found = overseas_bars(client, code, opens, closes)
                quote = overseas_quote(client, code)
            else:
                found = domestic_bars(client, code, opens, closes)
                quote = domestic_quote(client, code)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            print(f"{market} {symbol} REST fallback unavailable ({type(exc).__name__})")
            continue
        if found:
            bars[symbol] = found
        if quote:
            quotes[symbol] = dict(quote, event_time=datetime.now(KST).isoformat())
    return bars, quotes
