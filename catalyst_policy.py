"""Source-dated catalyst and sector confirmation, independent of issuer ticker."""
from datetime import datetime,timedelta,time
from zoneinfo import ZoneInfo
import math

SECTOR_PROXIES={
 'Technology':{'기술','반도체','AI반도체','IT·전자부품'},
 'Financial Services':{'금융','은행','증권'},'Healthcare':{'헬스케어','바이오'},
 'Industrials':{'산업재','운송','조선','방산'},'Energy':{'에너지'},
 'Basic Materials':{'소재','철강'},'Utilities':{'유틸리티','AI전력설비'},
 'Consumer Defensive':{'필수소비재'},'Consumer Cyclical':{'경기소비재','자동차'},
 'Real Estate':{'부동산'},'Communication Services':{'커뮤니케이션','미디어·엔터','K콘텐츠'}
}

def stamp(value):
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result if result.utcoffset() is not None else None
    except ValueError:return None

def completed_bar_available_at(day,market):
    zone=ZoneInfo('America/New_York' if market=='US' else 'Asia/Seoul')
    try:
        import exchange_calendars
        cal=exchange_calendars.get_calendar('XNYS' if market=='US' else 'XKRX')
        close=cal.session_close(day.isoformat()).to_pydatetime().astimezone(zone)
        if market=='KR' and day.isoformat()=='2026-11-19':close=close.replace(hour=16,minute=30)
        return (close+timedelta(minutes=15)).isoformat()
    except (ImportError,ValueError):
        return (datetime.combine(day,time.min,zone)+timedelta(days=1)).isoformat()

def catalyst_context(company,indicators,price,at,sectors):
    result=dict(eligible=False,reason='no_verified_fresh_catalyst')
    graph=company.get('event_analysis',{}).get('economic_graph',{})
    links=[r for r in graph.get('links',[]) if r.get('direction',0)>0]+[
        r for r in graph.get('expectation_links',[]) if r.get('watch_bias')=='positive_policy_sympathy']
    candidates=[]
    for link in links:
        published=stamp(link.get('available_at'))
        if (link.get('source_url') and link.get('publication_verified') is True and published
                and at-timedelta(hours=48)<=published<=at):
            candidates.append((published,link))
    if not candidates:return result
    # Correlated policy documents do not increase strength by headline count.
    published,link=max(candidates,key=lambda pair:pair[0])
    matched=[]
    price_day=indicators.get('data_date')
    for row in sectors:
        if company.get('market') and row.get('market')!=company['market']:continue
        if row.get('name') not in SECTOR_PROXIES.get(company.get('sector'),set()):continue
        observed=stamp(row.get('data_as_of'))
        if observed and observed>at:continue
        if row.get('data_date')!=price_day:continue
        if (row.get('return_pct',0)>0 and row.get('market_excess_pct',0)>0
                and row.get('turnover_ratio',0)>=1.3):matched.append(row['name'])
    if not matched:return dict(result,reason='sector_confirmation_missing')
    history=[]
    for bar in company.get('price_history',[]):
        observed=stamp(bar.get('available_at'))
        if observed and observed<=published:
            try:
                close=float(bar['close'])
                if math.isfinite(close) and close>0:history.append((observed,close))
            except (ValueError,TypeError,KeyError):continue
    if not history:return dict(result,reason='pre_catalyst_price_missing')
    _,reference=max(history)
    atr=indicators.get('atr14_pct')
    ma=indicators.get('ma20')
    if not isinstance(atr,(float,int)) or not math.isfinite(atr) or atr<=0 or not ma or ma<=0:
        return dict(result,reason='volatility_measure_missing')
    atr_amount=price*atr/100
    extension=(price-ma)/atr_amount
    reaction=max(0,(price/reference-1)*100)
    eligible=(extension<=1.5 and reaction<=1.5*atr and indicators.get('obv_rising') is True
              and indicators.get('volume_ratio',0)>=1.2 and not indicators.get('breakdown'))
    return dict(eligible=eligible,reason='supported_catalyst' if eligible else 'catalyst_price_risk',
        event_id=link.get('event_id'),source_url=link['source_url'],available_at=published.isoformat(),
        publication_verified=True,availability_basis=link.get('availability_basis'),
        sector_names=sorted(set(matched)),price_before_known_event=reference,
        post_available_return_pct=round(reaction,5),extension_atr=round(extension,5),
        max_extension_atr=1.5,max_post_event_reaction_atr=1.5,
        interpretation='verified news plus observed demand; not verified future earnings or win probability')
