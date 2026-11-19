"""Point-in-time audit and reconstruction; never turn missing evidence into a buy."""
import copy,json,os,subprocess,sys
from pathlib import Path
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import requests
import yfinance as yf
import scanner
from economic_graph import select_analysis_candidates
from kis_intraday import dart_company_codes,dart_financial_reports,dart_quarterly_financials
from company_forecast import build_company_forecast
from preopen_signals import evaluate_preopen

AT=datetime(2026,11,12,8,50,tzinfo=ZoneInfo('Asia/Seoul'))
CONTROLS={'352820.KS','041510.KQ','122870.KQ','035900.KQ'}
OUT=Path('nov12-evidence'); OUT.mkdir(exist_ok=True)
def safe_error(exc): return type(exc).__name__
def event_index(key):
    found=[]; coverage=[]
    for kind in ['B','I']:
        for page in range(1,101):
            r=requests.get('https://opendart.fss.or.kr/api/list.json',params=dict(crtfc_key=key,
                bgn_de='20261029',end_de='20261111',pblntf_ty=kind,page_count=100,page_no=page),timeout=15)
            r.raise_for_status(); d=r.json()
            if d.get('status') not in {'000','013'}: raise ValueError('DART index unavailable')
            found.extend(d.get('list',[]))
            if page>=int(d.get('total_page',0)): break
        if page<int(d.get('total_page',0)): raise ValueError('Incomplete historical DART pagination')
        coverage.append(dict(type=kind,pages=page,total_pages=d.get('total_page',0),truncated=False))
    return found,coverage

def price_reference(row,prices):
    """Close-to-close reference only. Not a recommendation, fill or strategy return."""
    try:
        data=scanner.clean_prices(prices[row['symbol']])
        data=data.loc[data.index.strftime('%Y-%m-%d')<='2026-11-18']
        entry=data.loc[data.index.strftime('%Y-%m-%d')=='2026-11-12']
        end=data.loc[data.index.strftime('%Y-%m-%d')=='2026-11-18']
        if entry.empty or end.empty: return dict(status='missing_endpoint_prices')
        close=float(entry.Close.iloc[0]); final=float(end.Close.iloc[0])
        return dict(status='reference_only_not_strategy',nov12_open=float(entry.Open.iloc[0]),
            nov12_close=close,nov18_close=final,close_to_close_pct=round((final/close-1)*100,4))
    except Exception as exc:return dict(status='unavailable',error=safe_error(exc))

def main():
    subprocess.run(['gh','run','download','43031791802','--name','scan-results','--dir','nov12-archive'],check=True)
    source=next(Path('nov12-archive').rglob('latest.json'))
    report=json.loads(source.read_text(encoding='utf-8-sig'))
    generated=datetime.fromisoformat(report['generated_at'].replace('Z','+00:00'))
    assert generated<=AT,'Future archive'
    pool=[r for r in report['results'] if r['market']=='KR' and r['indicators'].get('data_date')=='2026-11-11']
    key=os.environ['DART_API_KEY']; errors={}
    print('START_DATED_EVENT_INDEX',flush=True)
    try: events,coverage=event_index(key)
    except Exception as exc: events=[];coverage=[];errors['dart_event_index']=safe_error(exc)
    # Dated material disclosures expand research before chart ranking, not after observing winners.
    event_symbols={r['stock_code'] for r in events if r.get('stock_code') and '단일판매' in r.get('report_nm','')}
    selected=select_analysis_candidates(pool,'KR',[r['symbol'] for r in pool if r['symbol'].split('.')[0] in event_symbols])
    selected_symbols={r['symbol'] for r in selected}
    controls=[r for r in pool if r['symbol'] in CONTROLS-selected_symbols]
    print('ARCHIVE',len(pool),'RESEARCH',len(selected),'CONTROLS',len(controls),flush=True)
    print('START_CORPORATE_CODE_DOWNLOAD',flush=True)
    codes=dart_company_codes(key)
    print('START_HISTORICAL_VALUATION_PROBE',flush=True)
    try:
        import pandas as pd
        # pykrx has unbounded upstream HTTP calls. Isolate the optional probe.
        subprocess.run([sys.executable,'-c',"from pykrx import stock; stock.get_market_fundamental('20261111',market='ALL').to_csv('nov12-evidence/historical-fundamentals.csv')"],
                       timeout=45,check=True,capture_output=True)
        fundamental=pd.read_csv(OUT/'historical-fundamentals.csv',index_col=0,dtype={'티커':str})
        fundamental.index=fundamental.index.astype(str).str.zfill(6)
        if fundamental.empty: raise ValueError('Empty historical fundamentals')
        fundamental.to_csv(OUT/'historical-fundamentals.csv')
        valuation_status=dict(status='downloaded',rows=len(fundamental),date='2026-11-11')
    except Exception as exc:
        fundamental=None;valuation_status=dict(status='unavailable',error=safe_error(exc))
        if isinstance(exc,subprocess.CalledProcessError):
            valuation_status['diagnostic']=(exc.stderr or b'').decode('utf-8',errors='replace')[-1800:]
    print('START_DATED_PRICE_DOWNLOAD',valuation_status,flush=True)
    symbols=[r['symbol'] for r in selected+controls]
    daily=yf.download(symbols+['^KS11'],start='2026-06-01',end='2026-11-19',group_by='ticker',
        auto_adjust=False,progress=False,threads=6,timeout=15,multi_level_index=True)
    daily.to_csv(OUT/'downloaded-daily-prices.csv')
    def reconstruct(row):
        row=copy.deepcopy(row); symbol=row['symbol']; company=dict(market='KR',source_symbol=symbol,
            filing_status='unavailable',news_status='historical_coverage_unavailable')
        try:
            code=codes.get(symbol.split('.')[0]); filings=dart_financial_reports(key,code,AT-timedelta(days=1)) if code else []
            # Date-only disclosures are considered known from the following midnight.
            assert all(f['url'].rsplit('=',1)[-1][:8]<='20261111' for f in filings)
            company.update(dart_quarterly_financials(key,code,filings) if filings else {})
            company.update(filings=filings,filing_status='checked' if code else 'issuer_unmapped')
        except Exception as exc: company['financial_error']=safe_error(exc)
        try:
            before=scanner.clean_prices(daily[symbol]);before=before.loc[before.index.strftime('%Y-%m-%d')<'2026-11-12']
            market=scanner.clean_prices(daily['^KS11']);market=market.loc[market.index.strftime('%Y-%m-%d')<'2026-11-12']
            row['indicators']=scanner.calculate_indicators(before,market)
            assert row['indicators']['data_date']=='2026-11-11'
        except Exception as exc: row['price_reconstruction_error']=safe_error(exc)
        if fundamental is not None and symbol.split('.')[0] in fundamental.index:
            f=fundamental.loc[symbol.split('.')[0]]
            company.update(trailing_pe=float(f.PER),price_to_book=float(f.PBR),
                valuation_source='pykrx dated KRX fundamentals 2026-11-11')
        # Historical sector classification, worldwide news bodies and source-linked causal
        # analysis are not archived. Keep absent; do not insert current profiles or neutral zeros.
        row['company_analysis']=company
        row['company_forecast']=build_company_forecast(company,row['indicators'],row['indicators']['price'],AT)
        row['historical_price_reference']=price_reference(row,daily)
        row['research_selection']='current_selector_on_dated_universe' if symbol in selected_symbols else 'entertainment_diagnostic_control_not_selected'
        print('RECONSTRUCTED',symbol,row['name'],company.get('quarter'),row['company_forecast']['missing_dimensions'],flush=True)
        return row
    with ThreadPoolExecutor(max_workers=6) as executor: reconstructed=list(executor.map(reconstruct,selected+controls))
    decision=evaluate_preopen({'results':[r for r in reconstructed if r['symbol'] in selected_symbols]},'2026-11-11',AT)
    missing=Counter(k for r in reconstructed if r['symbol'] in selected_symbols for k in r['company_forecast']['missing_dimensions'])
    signals=[r for r in reconstructed if r.get('preopen_decision')=='conditional_buy']
    result=dict(decision_at=AT.isoformat(),evaluation_through='2026-11-18 KRX close',
        archive_run=43031791802,archive_generated_at=report['generated_at'],universe_count=len(pool),
        archived_kr_count=sum(r['market']=='KR' for r in report['results']),
        research_count=len(selected),control_count=len(controls),dated_event_index_count=len(events),
        event_index_coverage=coverage,event_candidate_count=len(event_symbols),valuation=valuation_status,
        financial_reconstruction_count=sum(bool(r['company_analysis'].get('quarter')) for r in reconstructed if r['symbol'] in selected_symbols),
        complete_forecasts=sum(r['company_forecast']['assessment_status']=='complete' for r in reconstructed if r['symbol'] in selected_symbols),
        conditional_buy_count=len(signals),missing_dimensions=dict(missing),decisions=decision,
        historical_validation_complete=False,
        strategy_statistics=dict(status='not_estimable_missing_historical_evidence',trades=None,win_rate=None,net_return=None),
        price_reference_is_not_strategy_return=True,errors=errors,results=reconstructed,
        limitations=['No historical world/issuer news body coverage or dated business exposure/sector snapshot',
            'Current valuation, profiles and post-cutoff policy announcements were excluded',
            'Research selector is bounded; full universe screened, not every issuer deeply analysed',
            'No historical order book/execution quote; no fills or bearish exits asserted',
            '0 validated buys under missing evidence does not mean 0% return or 0% win rate'])
    (OUT/'replay-result.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,default=str),encoding='utf-8')
    print('REPLAY_SUMMARY',json.dumps({k:v for k,v in result.items() if k not in {'results','decisions'}},ensure_ascii=False),flush=True)
    print('REPLAY_ROWS',json.dumps([dict(symbol=r['symbol'],name=r['name'],selection=r['research_selection'],
        quarter=r['company_analysis'].get('quarter'),financial_error=r['company_analysis'].get('financial_error'),
        price_error=r.get('price_reconstruction_error'),missing=r['company_forecast']['missing_dimensions'],
        decision=r.get('preopen_decision'),price_reference=r['historical_price_reference']) for r in reconstructed],ensure_ascii=False),flush=True)
if __name__=='__main__':main()
