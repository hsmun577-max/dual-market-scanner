"""Fail-closed pre-opening delivery windows in exchange local time."""
from datetime import datetime,time,timedelta,timezone
from zoneinfo import ZoneInfo

ALIASES={'US-early':'US_PRE','US-late':'US','NXT':'NXT','KRX':'KRX','US':'US','US_PRE':'US_PRE'}
# Official NXT notice 46 (2026-11-05): CSAT day has no premarket.
OVERRIDES={'2026-11-19':{'NXT':None,'KRX':'10:00'}}


def plan(market, now, regular_open=None):
    market=ALIASES.get(market)
    if market is None or now.utcoffset() is None: return None
    zone=ZoneInfo('America/New_York' if market.startswith('US') else 'Asia/Seoul')
    local=now.astimezone(zone); day=local.date().isoformat()
    if regular_open is None:
        import exchange_calendars as calendars
        cal=calendars.get_calendar('XNYS' if market.startswith('US') else 'XKRX')
        if not cal.is_session(day): return None
        regular_open=cal.session_open(day).to_pydatetime().astimezone(zone)
    elif regular_open.utcoffset() is None or regular_open.astimezone(zone).date()!=local.date():
        raise ValueError('Session date or timezone mismatch')
    override=OVERRIDES.get(day,{})
    if market in override:
        if override[market] is None: return None
        opening=datetime.combine(local.date(),time.fromisoformat(override[market]),zone)
    elif market=='NXT':
        if regular_open.astimezone(zone).time()!=time(9): return None
        opening=datetime.combine(local.date(),time(8),zone)
    elif market=='US_PRE':
        opening=datetime.combine(local.date(),time(4),zone)
    else:
        opening=regular_open.astimezone(zone)
    target=opening-timedelta(minutes=10)
    return dict(market=market,session_date=day,open_at=opening.isoformat(),
                target_at=target.isoformat(),expires_at=(target+timedelta(minutes=2)).isoformat())


def delivery_status(market, now, regular_open=None):
    if not market: return False,'dedicated_preopen_delivery_required'
    window=plan(market,now,regular_open)
    if not window: return False,'closed_or_unsupported_session'
    target=datetime.fromisoformat(window['target_at']); deadline=datetime.fromisoformat(window['expires_at'])
    if now<target: return False,'before_delivery_window'
    if now>=deadline: return False,'missed_delivery_window'
    return True,'within_preopen_window'
