"""Advance research candidates from announced preparations, before outcome or sector price confirmation."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from catalyst_policy import stamp
import math
from candidate_selection import instrument_rejection_reason,average_turnover,MIN_AVERAGE_TURNOVER,MIN_PRICE

def eligible_for_advance_research(row,market):
    if instrument_rejection_reason(row['symbol'],row.get('name'),row.get('asset_type','')):return False
    try:
        price=row['indicators']['price'];amount=average_turnover(row['indicators'])
        return (math.isfinite(price) and price>=MIN_PRICE[market] and math.isfinite(amount)
                and amount>=MIN_AVERAGE_TURNOVER[market]*.1)
    except (KeyError,TypeError,ValueError):return False

def advance_context(company,indicators,at):
    graph=company.get('event_analysis',{}).get('economic_graph',{})
    links=[*graph.get('links',[]),*graph.get('expectation_links',[])]
    day=at.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
    for event in links:
        known=stamp(event.get('available_at'))
        start=event.get('event_window_start');end=event.get('event_window_end')
        if (event.get('stage')!='pre_event' or event.get('publication_verified') is not True
                or not known or known>at or not start or not end or end<day
                or (datetime.fromisoformat(start).date()-at.date()).days>14):
            continue
        # Subsequent published outcomes supersede preparation-stage expectations.
        resolved=any(other.get('stage')!='pre_event' and other.get('publication_verified') is True
            and stamp(other.get('available_at')) and known<stamp(other['available_at'])<=at
            and set(other.get('matched_regions',[]))&set(event.get('matched_regions',[]))
            for other in links)
        if resolved:continue
        baselines=[(stamp(bar.get('available_at')),bar.get('close')) for bar in company.get('price_history',[])
            if stamp(bar.get('available_at')) and stamp(bar['available_at'])<=known and bar.get('close',0)>0]
        if not baselines:continue
        _,before=max(baselines)
        atr=indicators.get('atr14_pct');price=indicators.get('price',0)
        if not atr or atr<=0 or price<=0:continue
        reaction=(price/before-1)*100
        if reaction>.5*atr:continue
        return dict(eligible=True,stage='before_outcome_research',source_url=event.get('source_url'),
            known_at=known.isoformat(),event_window_start=start,event_window_end=end,
            post_available_return_pct=round(reaction,4),price_before_event=before,
            hypothesis='관련 사업의 수혜 가능성; 정책 결과·매출 증가·매수 체결 미확정',
            requires_sector_rally=False,requires_ma_uptrend=False)
    return dict(eligible=False,reason='no_unresolved_pre_price_catalyst')
