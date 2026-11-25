"""Read-only KIS quote and same-time minute-volume checks for a proposed entry."""
from datetime import datetime, timedelta
import math
import time

from exchange_calendars import get_calendar
import requests

from industry_flow import Client, BASE

MINUTE = timedelta(minutes=1)
# Today's endpoint returns at most 30 rows, including a possible unfinished minute.
VOLUME_WINDOW_MINUTES = 29


def raw_get(client, path, tr, params):
    time.sleep(.15)
    response = requests.get(BASE + path, headers=dict(client.headers, tr_id=tr),
                            params=params, timeout=8)
    if response.status_code != 200:
        raise RuntimeError('KIS read request failed')
    body = response.json()
    if body.get('rt_cd') != '0':
        raise RuntimeError('KIS read response unavailable')
    return body


def _number(value):
    try:
        if isinstance(value, bool):
            return None
        result = float(str(value).replace(',', ''))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _minute_rows(payload, zone):
    rows = {}
    for row in payload.get('output2') or []:
        try:
            minute = datetime.strptime(str(row['stck_bsop_date']) + str(row['stck_cntg_hour']),
                                       '%Y%m%d%H%M%S').replace(tzinfo=zone)
        except (ValueError, TypeError, KeyError):
            continue
        volume = _number(row.get('cntg_vol'))
        if volume is None or volume < 0:
            continue
        rows[minute] = (volume, _number(row.get('stck_prpr')), _number(row.get('stck_oprc')))
    return rows


def _same_time_volume(today, prior, observed, anchor):
    """Compare the exact same completed KRX clock minutes on consecutive sessions."""
    calendar = get_calendar('XKRX')
    session = anchor.date().isoformat()
    previous = calendar.previous_session(session)
    current_open = calendar.session_open(session).to_pydatetime().astimezone(anchor.tzinfo)
    prior_open = calendar.session_open(previous).to_pydatetime().astimezone(anchor.tzinfo)
    prior_close = calendar.session_close(previous).to_pydatetime().astimezone(anchor.tzinfo)
    end = min(observed, anchor).replace(second=0, microsecond=0)
    start = max(end - VOLUME_WINDOW_MINUTES * MINUTE, current_open,
                datetime.combine(anchor.date(), prior_open.time(), tzinfo=anchor.tzinfo))
    clocks = [start + offset * MINUTE for offset in range((end - start) // MINUTE)]
    if not clocks:
        return {}
    prior_clocks = [datetime.combine(previous.date(), clock.time(), tzinfo=anchor.tzinfo)
                    for clock in clocks]
    if prior_clocks[-1] + MINUTE > prior_close or any(
            now not in today or then not in prior for now, then in zip(clocks, prior_clocks)):
        return {}
    volume = sum(today[clock][0] for clock in clocks)
    previous_volume = sum(prior[clock][0] for clock in prior_clocks)
    if previous_volume <= 0:
        return {}
    return dict(volume_ratio=volume / previous_volume, volume_window_minutes=len(clocks),
                volume_window_end=end.isoformat(), volume_reference_date=previous.date().isoformat())


def parse_execution_quote(book, minutes, at, *, previous_minutes=None, volume_anchor=None):
    """Use provider time to establish quote freshness, and compare completed minutes."""
    orderbook = book.get('output1') or {}
    detail = book.get('output2') or {}
    market_code = orderbook.get('new_mkop_cls_code') or (
        detail.get('new_mkop_cls_code') if isinstance(detail, dict) else '')
    if str(market_code).strip() != '20':
        raise ValueError('Continuous trading not verified')
    if not isinstance(detail, dict) or str(detail.get('vi_cls_code', '')).upper() != 'N':
        raise ValueError('Volatility interruption status not verified normal')
    hour = str(orderbook.get('aspr_acpt_hour') or '')
    if len(hour) != 6 or not hour.isdigit():
        raise ValueError('Provider quote time unavailable')
    observed = datetime.combine(at.date(), datetime.strptime(hour, '%H%M%S').time(), tzinfo=at.tzinfo)
    rows = _minute_rows(minutes, at.tzinfo)
    today = {minute: row for minute, row in rows.items() if minute.date() == at.date()}
    completed = sorted(minute for minute, row in today.items()
                       if row[0] > 0 and minute + MINUTE <= at)
    if not completed:
        raise ValueError('No completed current-day traded minute')
    traded = [row for minute, row in sorted(today.items()) if minute <= at and row[0] > 0]
    day_open = _number(detail.get('stck_oprc')) or next((row[2] for row in traded if row[2]), None)
    last_price = _number(detail.get('stck_prpr')) or next(
        (row[1] for row in reversed(traded) if row[1]), None)
    comparison = {'volume_ratio': None}
    if previous_minutes:
        comparison.update(_same_time_volume(today, _minute_rows(previous_minutes, at.tzinfo),
                                            observed, volume_anchor or at))
    return dict(ask=float(orderbook['askp1']), bid=float(orderbook['bidp1']),
                ask_size=float(orderbook['askp_rsqn1']), bid_size=float(orderbook['bidp_rsqn1']),
                observed_at=observed.isoformat(), trade_at=completed[-1].isoformat(),
                day_open=day_open, last_price=last_price,
                cumulative_volume=sum(row[0] for row in traded),
                **comparison,
                source='KIS orderbook plus completed current-day minute',
                market_state='continuous', filled=False)


def execution_quote(symbol, at, client=None, *, compare_volume=False):
    client = client or Client()
    params = dict(FID_COND_MRKT_DIV_CODE='J', FID_INPUT_ISCD=symbol.split('.')[0],
                  FID_INPUT_HOUR_1=at.strftime('%H%M%S'), FID_PW_DATA_INCU_YN='N')
    previous_minutes = None
    if compare_volume:
        previous = get_calendar('XKRX').previous_session(at.date().isoformat())
        try:
            previous_minutes = raw_get(
                client, '/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice', 'FHKST03010230',
                dict(params, FID_INPUT_DATE_1=previous.strftime('%Y%m%d'), FID_FAKE_TICK_INCU_YN=''))
        except (ValueError, RuntimeError, requests.RequestException):
            # A missing reference still permits a quote, but the catalyst gate holds the entry.
            pass
    minutes = raw_get(client, '/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice', 'FHKST03010200',
                      dict(params, FID_ETC_CLS_CODE=''))
    book = raw_get(client, '/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn', 'FHKST01010200',
                   dict(FID_COND_MRKT_DIV_CODE='J', FID_INPUT_ISCD=symbol.split('.')[0]))
    return parse_execution_quote(book, minutes, datetime.now(at.tzinfo),
                                 previous_minutes=previous_minutes, volume_anchor=at)
