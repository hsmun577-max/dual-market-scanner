"""Audit delivery and point-in-time coverage. Never substitute current evidence into past signals."""
from collections import Counter
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json,os,subprocess
import exchange_calendars
import requests
import yfinance as yf
import scanner

ROOT=Path('verification-download')
KST=ZoneInfo('Asia/Seoul')
ARCHIVES={
 '2026-11-13':43312414727,
 '2026-11-16':None,
 '2026-11-17':43962632425,
 '2026-11-18':44183104468,
 '2026-11-19':44404812105,
}
TICKERS={'352820.KS':'하이브','041510.KQ':'에스엠','122870.KQ':'와이지엔터테인먼트','035900.KQ':'JYP Ent.'}

def gh(*args):
    r=subprocess.run(['gh',*args],capture_output=True,text=True)
    return r.returncode,r.stdout

def download(run,artifact):
    folder=ROOT/str(run)/artifact
    code,_=gh('run','download',str(run),'--name',artifact,'--dir',str(folder))
    return folder if code==0 else None

def stamp(value):
    try:
        at=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return at if at.utcoffset() is not None else None
    except ValueError:return None

def coverage(report,cutoff,previous):
    result=Counter(); missing=Counter()
    for row in report.get('results',[]):
        if row.get('market')!='KR':continue
        result['scanned']+=1
        forecast=row.get('company_forecast')
        if not forecast:result['no_archived_company_forecast']+=1;continue
        result['company_forecasts']+=1
        company=row.get('company_analysis',{})
        observed=[stamp(forecast.get('generated_at')),stamp(company.get('available_at'))]
        if any(at and at>cutoff for at in observed):result['future_evidence']+=1;continue
        if forecast.get('composite_score') is None or forecast.get('assessment_status')!='complete':
            result['incomplete_forecasts']+=1;missing.update(forecast.get('missing_dimensions',[]));continue
        result['complete_known_by_cutoff']+=1
        if row.get('indicators',{}).get('data_date')!=previous:
            result['wrong_price_session']+=1;continue
        when=stamp(forecast.get('generated_at'))
        if not when or when<cutoff-timedelta(minutes=30):
            result['stale_at_delivery']+=1;continue
        result['replayable_complete_decisions']+=1
    return dict(counts=dict(result),missing_dimensions=dict(missing))

def archived_coverage():
    rows=[];cal=exchange_calendars.get_calendar('XKRX')
    for day,run in ARCHIVES.items():
        previous=cal.previous_session(day).strftime('%Y-%m-%d')
        cutoff=datetime.fromisoformat(day+('T09:50:00' if day=='2026-11-19' else 'T08:50:00')).replace(tzinfo=KST)
        if not run:
            rows.append(dict(session=day,status='no_preopen_archive',cutoff=cutoff.isoformat()));continue
        folder=download(run,'scan-results')
        files=list(folder.rglob('latest.json')) if folder else []
        if not files:rows.append(dict(session=day,status='artifact_unavailable'));continue
        report=json.loads(files[0].read_text(encoding='utf-8-sig'))
        generated=stamp(report.get('generated_at'))
        if not generated or generated>cutoff:
            rows.append(dict(session=day,run=run,status='scan_not_available_before_cutoff',
                             generated_at=report.get('generated_at')));continue
        rows.append(dict(session=day,run=run,status='archive_checked',cutoff=cutoff.isoformat(),
                         generated_at=report.get('generated_at'),**coverage(report,cutoff,previous)))
    return rows

def historical_price_diagnostic():
    symbols=[*TICKERS,'^KS11']
    data=yf.download(symbols,start='2026-08-01',end='2026-11-20',group_by='ticker',
                     auto_adjust=False,progress=False,threads=4,timeout=10,multi_level_index=True)
    output=[]
    for day in ARCHIVES:
        for symbol,name in TICKERS.items():
            try:
                before=data[symbol].loc[data.index.strftime('%Y-%m-%d')<day]
                before=scanner.clean_prices(before)
                benchmark=scanner.clean_prices(data['^KS11'].loc[data.index.strftime('%Y-%m-%d')<day])
                indicators=scanner.calculate_indicators(before,benchmark)
                row=dict(market='KR',symbol=symbol,name=name,indicators=indicators,signal={'score':0})
                reason=scanner.candidate_rejection_reason(row,'KR')
                current=data[symbol].loc[data.index.strftime('%Y-%m-%d')==day].dropna(subset=['Open','Close'])
                result=dict(session=day,symbol=symbol,name=name,prior_data_date=indicators['data_date'],
                    technical_filter_only=reason or 'eligible',previous_close=indicators['price'],
                    prior_5d_return_pct=round(indicators['return_5d']*100,4),
                    prior_ma20_extension_pct=round((indicators['price']/indicators['ma20']-1)*100,4))
                if len(current)==1:
                    actual=current.iloc[0]
                    result.update(open=float(actual['Open']),close=float(actual['Close']),
                        close_vs_previous_pct=round((float(actual['Close'])/indicators['price']-1)*100,4),
                        open_to_close_pct=round((float(actual['Close'])/float(actual['Open'])-1)*100,4))
                output.append(result)
            except Exception as exc:
                output.append(dict(session=day,symbol=symbol,error=type(exc).__name__))
    return dict(basis='price/technical observation only, NOT strategy trade returns',rows=output)

def scheduled_status():
    code,text=gh('workflow','list','--all','--limit','100','--json','id,name,path,state')
    if code:return dict(status='workflow_registry_read_failed')
    workflow=next((r for r in json.loads(text) if r.get('path')=='.github/workflows/preopen-delivery.yml'),None)
    if not workflow:return dict(status='workflow_not_registered')
    enabled=False
    if workflow.get('state')!='active':
        enabled=gh('workflow','enable','preopen-delivery.yml')[0]==0
    code,text=gh('run','list','--workflow','preopen-delivery.yml','--limit','10',
                 '--json','databaseId,event,status,conclusion,createdAt,headSha')
    return dict(workflow=workflow,enabled_during_verification=enabled,
                runs=json.loads(text) if code==0 else None)

def test_telegram():
    token=os.environ.get('TELEGRAM_BOT_TOKEN','').strip();chat=os.environ.get('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat:return dict(confirmed=False,reason='configuration_missing')
    try:
        r=requests.post('https://api.telegram.org/bot'+token+'/sendMessage',json={
            'chat_id':chat,'text':'듀얼스캐너 발송 연결 테스트\n텔레그램 연결을 확인하는 테스트입니다.\n실제 매수 추천이 아닙니다.'},
            timeout=10,allow_redirects=False)
        body=r.json()
        success=r.status_code==200 and body.get('ok') is True
        return dict(confirmed=success,http_status=r.status_code,
            message_id=body.get('result',{}).get('message_id') if success else None,
            sent_at=datetime.now(timezone.utc).isoformat())
    except (requests.RequestException,ValueError):
        return dict(confirmed=False,reason='request_failed_or_invalid_response')

def main():
    result=dict(checked_at=datetime.now(timezone.utc).isoformat(),schedule=scheduled_status())
    result['archived_coverage']=archived_coverage()
    result['historical_prices']=historical_price_diagnostic()
    current_folder=download(44520102227,'preopen-composite-verification')
    files=list(current_folder.rglob('preopen-repair-analysis.json')) if current_folder else []
    if files:
        report=json.loads(files[0].read_text(encoding='utf-8-sig'))
        cutoff=datetime(2026,11,19,9,50,tzinfo=KST)
        result['reject_after_close_evidence_test']=coverage(report,cutoff,'2026-11-18')
    replayable=sum(row.get('counts',{}).get('replayable_complete_decisions',0) for row in result['archived_coverage'])
    result['profitability']=dict(status='insufficient_point_in_time_evidence' if not replayable else 'requires_trade_path_replay',
        replayable_complete_decisions=replayable,strategy_trade_count=0,win_rate=None,net_return=None,
        latest_news_used_for_past=False,today_valuation_used_for_past=False)
    result['telegram_transport']=test_telegram()
    Path('verification-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('VERIFICATION_EVIDENCE='+json.dumps(result,ensure_ascii=False),flush=True)
    if not result['telegram_transport']['confirmed']:raise RuntimeError('Telegram test delivery not confirmed')

if __name__=='__main__':main()
