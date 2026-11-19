"""Frozen-case mechanism test and observed 5m execution diagnostic; not a point-in-time profitability claim."""
from copy import deepcopy
from datetime import datetime,timedelta,time,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json,subprocess
import yfinance as yf
import scanner
from event_impact_analysis import world_events,analyze_events,article_text
from event_impact_analysis import _ARTICLE_METADATA,canonical_article_url
from economic_graph import frames
from company_forecast import build_company_forecast
from preopen_signals import evaluate_preopen
from valuation_data import domestic_valuations
from catalyst_policy import completed_bar_available_at
KST=ZoneInfo('Asia/Seoul')

def bars_for(symbol,day='2026-11-19'):
    df=yf.download(symbol,start=day,end='2026-11-20',interval='5m',auto_adjust=False,
                   progress=False,threads=False,timeout=10,multi_level_index=False)
    if df.empty or df.index.tz is None:return None
    df.index=df.index.tz_convert(KST)
    return df.dropna(subset=['Open','High','Low','Close']).loc[
        (df.index>=datetime(2026,11,19,10,tzinfo=KST))&
        (df.index<datetime(2026,11,19,16,30,tzinfo=KST))]

def execution_diagnostic(row):
    plan=row.get('preopen_signal',{})
    if plan.get('status')!='conditional_buy':return dict(status='no_new_plan')
    df=bars_for(row['symbol'])
    if df is None or df.empty:return dict(status='intraday_data_unavailable')
    expected=datetime(2026,11,19,10,tzinfo=KST)
    if df.index[0]!=expected:return dict(status='opening_bar_missing',first_bar=str(df.index[0]))
    opening=float(df.iloc[0]['Open']);entry=None;entry_at=None
    for at,bar in df.iterrows():
        op=float(bar['Open']);lo=float(bar['Low']);hi=float(bar['High'])
        if plan['entry_floor']<=op<=plan['entry_limit']:
            entry=op;entry_at=at;break
        if op>plan['entry_limit'] and lo<=plan['entry_limit']:
            entry=plan['entry_limit'];entry_at=at;break
    if entry is None:return dict(status='no_observed_entry',opening_price=opening)
    stop=entry*.98;target=entry*1.06
    base=dict(entry=entry,entry_at=entry_at.isoformat(),opening_price=opening,stop=stop,target=target,
              basis='hypothetical_limit_fill_5m_OHLC_no_historical_orderbook',
              point_in_time_full_model_validated=False,roundtrip_cost_assumption_pct=.2)
    for at,bar in df.iterrows():
        if at<entry_at:continue
        low=float(bar['Low']);high=float(bar['High']);op=float(bar['Open'])
        if at>entry_at and op<=stop:return dict(base,status='stop_gap',exit_at=at.isoformat(),exit=op,gross_pct=(op/entry-1)*100)
        if low<=stop and high>=target:
            return dict(base,status='intrabar_order_unknown',bar_at=at.isoformat(),gross_return_range_pct=[-2,6])
        if at==entry_at and op>entry and high>=target:
            return dict(base,status='entry_target_order_unknown',bar_at=at.isoformat(),gross_return_range_pct=[0,6])
        if low<=stop:return dict(base,status='stop',exit_at=at.isoformat(),exit=stop,gross_pct=-2,net_pct=-2.2)
        if high>=target:return dict(base,status='target',exit_at=at.isoformat(),exit=target,gross_pct=6,net_pct=5.8)
    close=float(df.iloc[-1]['Close'])
    return dict(base,status='still_open_at_regular_close',marked_price=close,
                unrealized_pct=round((close/entry-1)*100,4))

def main():
    folder=Path('catalyst-case-download')
    r=subprocess.run(['gh','run','download','44520102227','--name','preopen-composite-verification','--dir',str(folder)],
                     capture_output=True)
    files=list(folder.rglob('preopen-repair-analysis.json'))
    if r.returncode or not files:raise RuntimeError('Frozen diagnostic source unavailable')
    report=json.loads(files[0].read_text(encoding='utf-8-sig'))
    original=deepcopy(report)
    at=datetime.now(timezone.utc)
    events,coverage=world_events(at)
    # A historical primary document is a validation case, never a ticker-specific live rule.
    early_url='https://www.korea.kr/briefing/pressReleaseView.do?newsId=156789234'
    early_body,early_status=article_text(early_url)
    early_metadata=_ARTICLE_METADATA.get(canonical_article_url(early_url),{})
    early_event=dict(event_id=early_url,url=early_url,body=early_body,**early_metadata)
    first_cutoff=datetime(2026,11,16,8,50,tzinfo=KST)
    early_frames=frames([early_event],first_cutoff)
    early_result=dict(source_url=early_url,body_status=early_status,metadata=early_metadata,
        cutoff=first_cutoff.isoformat(),pre_event_frames=sum(f.get('kind')=='pre_event_policy_watch' for f in early_frames),
        outcome_news_used=False,current_company_profiles_used_for_past=False,
        scope='dated policy-to-business-category discovery only, not a historical stock trade or win rate')
    now=datetime.now(timezone.utc)
    codes=[r['symbol'].split('.')[0] for r in report['results']]
    quotes=domestic_valuations(codes)
    history=yf.download([r['symbol'] for r in report['results']],start='2026-09-01',end='2026-11-19',
        group_by='ticker',auto_adjust=False,threads=4,progress=False,timeout=10,multi_level_index=True)
    for row in report['results']:
        company=row['company_analysis'];symbol=row['symbol'];code=symbol.split('.')[0]
        company.update(quotes.get(code,{}))
        frame=history[symbol].dropna(subset=['Close'])
        company['price_history']=[dict(date=day.date().isoformat(),close=float(bar['Close']),
            available_at=completed_bar_available_at(day.date(),'KR'))
            for day,bar in frame.tail(35).iterrows()]
        own=[]
        for news in company.get('news',[])[:3]:
            text,status=article_text(news.get('url'))
            own.append(dict(**news,body=text,body_status=status,event_id=news.get('url'),
                publication_verified=True,availability_basis='source_news_timestamp'))
        company['event_analysis']=analyze_events(company,[*events,*own],datetime.now(timezone.utc),coverage)
        row['company_forecast']=build_company_forecast(company,row['indicators'],row['indicators']['price'],
                                                      datetime.now(timezone.utc),sectors=report['sectors'])
    evaluate_preopen(report,'2026-11-18',datetime.now(timezone.utc))
    result=dict(basis='frozen Nov18 price case plus currently observed company evidence; mechanism test only',
        historical_full_model_backtest=False,ticker_specific_rules=False,
        source_run=44520102227,publication_events=[{k:e.get(k) for k in
            ('event_id','available_at','publication_verified','availability_basis','source_published_date')}
            for e in events if e.get('source')=='대한민국 정책브리핑'],
        early_source_validation=early_result,
        companies=[dict(symbol=r['symbol'],name=r.get('name'),
            old_decision=next(x.get('preopen_decision') for x in original['results'] if x['symbol']==r['symbol']),
            new_decision=r.get('preopen_decision'),score=r['company_forecast'].get('composite_score'),
            forecast_complete=r['company_forecast'].get('assessment_status'),
            missing_dimensions=r['company_forecast'].get('missing_dimensions'),
            catalyst=r['company_forecast'].get('catalyst_context'),plan=r.get('preopen_signal'),
            execution_diagnostic=execution_diagnostic(r)) for r in report['results']])
    Path('catalyst-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    Path('catalyst-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    Path('catalyst-message-preview.txt').write_text('\n\n'.join(scanner.telegram_candidate_line(r)
        for r in report['results'] if r.get('preopen_signal')),encoding='utf-8')
    print('CATALYST_VERIFICATION='+json.dumps(result,ensure_ascii=False),flush=True)
    if any(r['company_forecast']['assessment_status']!='complete' for r in report['results']):
        raise RuntimeError('Evidence incomplete in live mechanism verification')
    if not early_result['pre_event_frames']:
        raise RuntimeError('Dated advance policy evidence did not reach discovery before the outcome')

if __name__=='__main__':main()
