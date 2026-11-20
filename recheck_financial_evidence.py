"""Bounded follow-up of an actual recent run; never backdate or erase risk evidence."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from collections import Counter
from zoneinfo import ZoneInfo
import json,os
import requests
from kis_intraday import dart_company_codes,dart_financial_reports,dart_quarterly_financials
from company_forecast import build_company_forecast
from preopen_signals import evaluate_preopen
from execution_quotes import execution_quote
from tradable_signal import validate_entry,format_requested_report
from telegram_text import split_telegram_text
import exchange_calendars

def main():
    folder=Path('repair-parent')
    report=json.loads((folder/'requested-buy-analysis.json').read_text())
    parent=json.loads((folder/'requested-buy-audit.json').read_text())
    now=datetime.now(ZoneInfo('Asia/Seoul'));session=parent['session']
    created=datetime.fromisoformat(parent['generated_at'])
    if now.date().isoformat()!=session or not now-timedelta(minutes=30)<=created<=now:
        raise RuntimeError('Parent analysis expired; a full fresh scan is required')
    codes=dart_company_codes(os.environ['DART_API_KEY'])
    financial_missing={'balance_sheet','cash_flow_quality','growth','earnings_quality','current_financial_period'}
    refreshed=[]
    for row in report['results']:
        old=row.get('company_forecast',{});missing=set(old.get('missing_dimensions',[]))
        if not missing & financial_missing or len(refreshed)>=8:continue
        company=row.get('company_analysis',{});code=codes.get(row['symbol'].split('.')[0])
        if not code:continue
        record=dict(symbol=row['symbol'],before=sorted(missing))
        try:
            index=dart_financial_reports(os.environ['DART_API_KEY'],code,datetime.now(now.tzinfo))
            values=dart_quarterly_financials(os.environ['DART_API_KEY'],code,index)
            if not values:
                record['status']='financial_fields_still_unavailable';refreshed.append(record);continue
            company.update(values);company['financial_checked_at']=datetime.now(now.tzinfo).isoformat()
            company['available_at']=company['financial_checked_at']
            at=datetime.now(timezone.utc)
            forecast=build_company_forecast(company,row['indicators'],row['indicators']['price'],at,sectors=report.get('sectors',[]))
            # Risk headline inputs are not fully retained by older reports. Do not
            # accidentally promote a company after dropping any known risk inputs.
            if row.get('risk_headlines'):
                forecast['missing_dimensions']=sorted(set(forecast['missing_dimensions'])|{'risk_evidence_requires_refresh'})
                forecast.update(assessment_status='incomplete',composite_score=None)
            row['company_forecast']=forecast
            record.update(status='refetched_matching_filing',after=forecast['missing_dimensions'],
                financial_source=values.get('financial_filing_url'),ratio=values.get('liabilities_to_equity'),
                operating_cash_flow_margin_pct=values.get('operating_cash_flow_margin_pct'))
        except (requests.RequestException,ValueError,TypeError) as exc:record.update(status='provider_unavailable',error=type(exc).__name__)
        refreshed.append(record)
    at=datetime.now(now.tzinfo);cal=exchange_calendars.get_calendar('XKRX')
    evaluation=evaluate_preopen(report,parent['evaluation']['previous_session'],at)
    candidates=sorted([r for r in report['results'] if r.get('preopen_signal',{}).get('status')=='conditional_buy'],
        key=lambda r:(-r['company_forecast']['composite_score'],r['symbol']))[:5]
    quotes={}
    for row in candidates:
        try:quotes[row['symbol']]=execution_quote(row['symbol'],datetime.now(now.tzinfo))
        except (requests.RequestException,ValueError,RuntimeError,KeyError,TypeError):pass
    at=datetime.now(now.tzinfo)
    opened=cal.session_open(session).to_pydatetime()<=at<cal.session_close(session).to_pydatetime()
    entries=[validate_entry(r,quotes.get(r['symbol']),at,opened) for r in candidates]
    previously_sent={r['symbol'] for r in parent.get('entry_checks',[]) if r.get('trade_ready')}
    fresh_entries=[r for r in entries if r.get('trade_ready') and r['symbol'] not in previously_sent]
    message=format_requested_report(report.get('sector_header_override',['오늘의 주요 섹터','확인 불가']),fresh_entries,evaluation,at)
    receipts=[]
    if fresh_entries:
        expires=min(datetime.fromisoformat(r['valid_until']) for r in fresh_entries)
        for part in split_telegram_text(message):
            if datetime.now(now.tzinfo)>=expires:raise RuntimeError('Entry quote expired before delivery')
            try:response=requests.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/sendMessage',
                json=dict(chat_id=os.environ['TELEGRAM_CHAT_ID'],text=part),timeout=10,allow_redirects=False)
            except requests.RequestException:raise RuntimeError('Telegram response unconfirmed; not retried') from None
            if response.status_code!=200:raise RuntimeError('Telegram did not confirm delivery')
            body=response.json()
            if body.get('ok') is not True:raise RuntimeError('Telegram response not confirmed')
            receipts.append(body['result']['message_id'])
    result=dict(mode='recent_analysis_with_matching_financial_filing_refresh',generated_at=at.isoformat(),
        parent_generated_at=parent['generated_at'],financial_refresh=refreshed,evaluation=evaluation,
        entry_checks=entries,trade_ready_count=sum(e['trade_ready'] for e in entries),
        telegram_sent=bool(receipts),telegram_receipts=receipts,duplicate_status_alert_sent=False,
        complete_forecasts=sum(r.get('company_forecast',{}).get('assessment_status')=='complete' for r in report['results']),
        missing_dimensions=dict(Counter(k for r in report['results'] for k in r.get('company_forecast',{}).get('missing_dimensions',[]))),
        message=message,trade_order_sent=False)
    Path('financial-recheck.json').write_text(json.dumps(result,ensure_ascii=False,default=str),encoding='utf-8')
    Path('financial-recheck-analysis.json').write_text(json.dumps(report,ensure_ascii=False,default=str),encoding='utf-8')
    print('FINANCIAL_RECHECK='+json.dumps(result,ensure_ascii=False,default=str),flush=True)

if __name__=='__main__':main()
