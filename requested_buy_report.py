"""Explicitly requested one-off report; does not override scheduled delivery guards."""
import argparse,json,os
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import requests
import exchange_calendars
import scanner
from profile_universe import restore_index,restore_prices
from preopen_signals import evaluate_preopen
from industry_flow import snapshot,format_report,Client
from telegram_text import split_telegram_text

KST=ZoneInfo('Asia/Seoul')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--session',required=True)
    args=parser.parse_args();at=datetime.now(KST)
    if at.date().isoformat()!=args.session:raise RuntimeError('Requested report session expired')
    cal=exchange_calendars.get_calendar('XKRX')
    if not cal.is_session(args.session):raise RuntimeError('Not a Korean trading session')
    previous=cal.previous_session(args.session).strftime('%Y-%m-%d')
    restore_index();source=Path('opening-scan/latest.json')
    if not source.is_file():restore_prices()
    if not source.is_file():raise RuntimeError('No full-universe scan available')
    report=json.loads(source.read_text(encoding='utf-8-sig'))
    report['source_scan_generated_at']=report.get('generated_at')
    report['results']=[r for r in report['results'] if r['market']=='KR']
    print('REQUESTED_ANALYSIS_START='+json.dumps(dict(session=args.session,
        universe=len(report['results']),source_scan=report['source_scan_generated_at'])),flush=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        industry_future=executor.submit(snapshot)
        report['sectors']=scanner.sector_flow_snapshot('KR',datetime.now(KST))
        scanner.prepare_selected_analysis(report)
        try:industry=industry_future.result()
        except Exception as exc:industry=dict(rows=[],error=type(exc).__name__)
    decided=datetime.now(timezone.utc)
    report['preopen_session']=args.session
    evaluation=evaluate_preopen(report,previous,decided)
    recommendations=sorted([r for r in report['results'] if r.get('preopen_signal',{}).get('status')=='conditional_buy'],
        key=lambda r:(-r['company_forecast']['composite_score'],r['symbol']))
    # The production conditional plan uses the previous close. A current quote is
    # displayed separately and never relabelled as a model entry or an actual fill.
    quotes={};client=None
    if recommendations:
        try:client=Client()
        except Exception:pass
    for row in recommendations[:5]:
        if client:
            try:
                q=client.get('/uapi/domestic-stock/v1/quotations/inquire-price','FHKST01010100',
                    dict(FID_COND_MRKT_DIV_CODE='J',FID_INPUT_ISCD=row['symbol'].split('.')[0]))
                price=float(q['stck_prpr'])
                if price>0:quotes[row['symbol']]=dict(price=price,queried_at=datetime.now(KST).isoformat(),source='KIS inquire-price')
            except (TypeError,ValueError,KeyError,requests.RequestException):pass
    sent_at=datetime.now(KST)
    if sent_at.date().isoformat()!=args.session:raise RuntimeError('Requested report session expired during analysis')
    if (sent_at-decided).total_seconds()>120:raise RuntimeError('Decision became stale before requested delivery')
    # Only the separately requested one-off transport bypasses preopen timing.
    # Every regular sender continues to use delivery_status unchanged.
    lines=(format_report(industry).splitlines() if industry.get('rows') else ['오늘의 주요 섹터','확인 불가'])
    lines+=['',f'매수 신호 · {sent_at:%m/%d %H:%M} KST · 요청 재분석']
    for row in recommendations[:5]:
        lines+=['',scanner.telegram_candidate_line(row)]
        quote=quotes.get(row['symbol'])
        if quote:
            lines.append(f"  조회 현재가 {quote['price']:,.0f}원 · {datetime.fromisoformat(quote['queried_at']):%H:%M:%S}")
        lines.append('  지정한 진입 조건 도달 시 매수 검토 · 체결 확인 아님')
    if not recommendations:
        incomplete=sum(v for k,v in evaluation.get('counts',{}).items() if k in
            {'analysis_stale_or_missing','analysis_incomplete','price_session_mismatch','missing_indicators','invalid_average_price','entry_tick_missing'})
        if incomplete:lines.append('매수 판정 보류 · 종합 분석자료 미확인')
        else:lines.append('현재 매수 조건을 충족한 종목 없음')
    message='\n'.join(lines)
    audit=dict(session=args.session,generated_at=sent_at.isoformat(),strategy_commit=os.getenv('GITHUB_SHA'),
        explicit_manual_request=True,scheduled_delivery_guards_unchanged=True,
        evaluation=evaluation,recommendations=[dict(symbol=r['symbol'],name=r.get('name'),
            score=r['company_forecast']['composite_score'],plan=r['preopen_signal'],quote=quotes.get(r['symbol'])) for r in recommendations],
        missing_dimensions=dict(Counter(key for r in report['results'] for key in r.get('company_forecast',{}).get('missing_dimensions',[]))),
        confirmed_fills=0,message=message,telegram_sent=False)
    Path('requested-buy-analysis.json').write_text(json.dumps(report,ensure_ascii=False,default=str),encoding='utf-8')
    Path('requested-buy-audit.json').write_text(json.dumps(audit,ensure_ascii=False,default=str),encoding='utf-8')
    Path('requested-buy-message.txt').write_text(message,encoding='utf-8')
    receipts=[]
    for part in split_telegram_text(message):
        try:
            response=requests.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/sendMessage',
                json=dict(chat_id=os.environ['TELEGRAM_CHAT_ID'],text=part),timeout=10,allow_redirects=False)
        except requests.RequestException:raise RuntimeError('Telegram network response unconfirmed; not retried') from None
        if response.status_code!=200:raise RuntimeError(f'Telegram HTTP {response.status_code}; not retried')
        body=response.json()
        if body.get('ok') is not True:raise RuntimeError('Telegram did not confirm delivery')
        receipts.append(dict(message_id=body['result']['message_id'],date=body['result']['date']))
    audit.update(telegram_sent=True,telegram_receipts=receipts)
    Path('requested-buy-audit.json').write_text(json.dumps(audit,ensure_ascii=False,default=str),encoding='utf-8')
    print('REQUESTED_BUY_DELIVERY='+json.dumps(audit,ensure_ascii=False,default=str),flush=True)

if __name__=='__main__':main()
