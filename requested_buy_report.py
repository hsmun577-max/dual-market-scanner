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
from json_utils import dump as dump_json
from profile_universe import restore_index,restore_prices
from preopen_signals import evaluate_preopen
from industry_flow import snapshot,format_report,Client
from telegram_text import split_telegram_text
from execution_quotes import execution_quote
from tradable_signal import validate_entry,format_requested_report
import asset_regime

KST=ZoneInfo('Asia/Seoul')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--session',required=True)
    parser.add_argument('--reuse-analysis',help='Refresh a verified same-session analysis at most 30 minutes old')
    args=parser.parse_args();at=datetime.now(KST)
    if at.date().isoformat()!=args.session:raise RuntimeError('Requested report session expired')
    cal=exchange_calendars.get_calendar('XKRX')
    if not cal.is_session(args.session):raise RuntimeError('Not a Korean trading session')
    previous=cal.previous_session(args.session).strftime('%Y-%m-%d')
    if not args.reuse_analysis:restore_index()
    source=Path(args.reuse_analysis or 'opening-scan/latest.json')
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
        try:industry=industry_future.result()
        except Exception as exc:industry=dict(rows=[],error=type(exc).__name__)
        report['industry_snapshot']=industry
        if args.reuse_analysis:
            from evidence_refresh import refresh_report
            refresh_report(report,datetime.now(KST),industry)
        else:
            scanner.prepare_selected_analysis(report)
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
                quotes[row['symbol']]=execution_quote(row['symbol'],datetime.now(KST),client)
            except (TypeError,ValueError,KeyError,RuntimeError,requests.RequestException):pass
    sent_at=datetime.now(KST)
    if sent_at.date().isoformat()!=args.session:raise RuntimeError('Requested report session expired during analysis')
    if (sent_at-decided).total_seconds()>120:raise RuntimeError('Decision became stale before requested delivery')
    # Only the separately requested one-off transport bypasses preopen timing.
    # Every regular sender continues to use delivery_status unchanged.
    session_open=cal.session_open(args.session).to_pydatetime()<=sent_at<cal.session_close(args.session).to_pydatetime()
    entries=[validate_entry(row,quotes.get(row['symbol']),sent_at,session_open) for row in recommendations[:5]]
    from theme_sectors import format_theme_block, theme_sector_snapshot
    # 간밤 미국 세부 테마는 자기 시장(한국) 스냅샷으로 덮어쓰기 전에 붙잡아 둔다.
    overnight_us_themes=[row for row in report.get('theme_sectors') or []
                         if isinstance(row,dict) and str(row.get('market') or '').upper().startswith('US')]
    theme=theme_sector_snapshot('KR',datetime.now(KST))
    report['theme_sectors']=[theme]
    sector_lines=(format_report(industry).splitlines() if industry.get('rows') else ['오늘의 주요 섹터','확인 불가'])
    if theme.get('rows'):sector_lines=sector_lines+format_theme_block(theme,'KR')
    import mid_term_watch
    message=format_requested_report(sector_lines,entries,evaluation,sent_at,
                                    regime=asset_regime.cached_block())
    # 요청 장전 리포트에도 매수 후보와 분리된 '긴급 이슈 참고'를 붙인다.
    urgent={}
    try:
        import urgent_issues
        urgent=urgent_issues.select_urgent_issues(report,now=datetime.now(timezone.utc))
        block=urgent_issues.format_urgent_block(urgent)
        if block:message='\n'.join([message,'',*block])
    except Exception as exc:
        print('Urgent issue check unavailable: '+type(exc).__name__,flush=True)
    # 요청 장전 리포트에도 국내 핵심 대표주의 NXT 장전 움직임·촉매·간밤 미국 연동을 붙인다.
    kr_premarket=dict(items=[])
    try:
        import kr_premarket as kr_module
        kr_premarket=kr_module.attach(report,datetime.now(timezone.utc),us_rows=overnight_us_themes)
        block=kr_module.format_block(kr_premarket.get('items'),kr_premarket.get('session'))
        if block:message='\n'.join([message,'',*block])
    except Exception as exc:
        print('Domestic premarket watch unavailable: '+type(exc).__name__,flush=True)
    # 중기 +30% 눌림 발굴은 당일 매수 판정과 다른 20~60일 관찰 목록이다. 리포트 메시지에
    # 별도 섹션으로만 붙고 ``recommendations``에는 들어가지 않는다.
    if report.get('mid_term_watch') is None:
        try:
            import mid_term_evidence
            mid_term_watch.attach(report, datetime.now(timezone.utc),
                                  evidence=mid_term_evidence.collect)
        except Exception as exc:
            print('Mid-term fundamentals screen unavailable: '+type(exc).__name__,flush=True)
            mid_term_watch.attach(report, datetime.now(timezone.utc))
    mid_term_lines=mid_term_watch.format_block(report.get('mid_term_watch') or [])
    if mid_term_lines:message='\n'.join([message,'',*mid_term_lines])
    audit=dict(session=args.session,generated_at=sent_at.isoformat(),strategy_commit=os.getenv('GITHUB_SHA'),
        explicit_manual_request=True,scheduled_delivery_guards_unchanged=True,
        evaluation=evaluation,entry_checks=entries,trade_ready_count=sum(e['trade_ready'] for e in entries),
        complete_forecasts=sum(r.get('company_forecast',{}).get('assessment_status')=='complete' for r in report['results']),
        research_scope=report.get('research_scope'),
        urgent_issues=urgent,kr_premarket_watch=kr_premarket,
        mid_term_watch=report.get('mid_term_watch') or [],
        mid_term_watch_scope=report.get('mid_term_watch_scope'),
        early_entry_candidates=(evaluation or {}).get('early_entry_candidates'),
        industry_rows=len(industry.get('rows',[])),industry_error=industry.get('error'),
        recommendations=[dict(symbol=r['symbol'],name=r.get('name'),
            score=r['company_forecast']['composite_score'],plan=r['preopen_signal'],quote=quotes.get(r['symbol'])) for r in recommendations],
        missing_dimensions=dict(Counter(key for r in report['results'] for key in r.get('company_forecast',{}).get('missing_dimensions',[]))),
        confirmed_fills=0,message=message,telegram_sent=False)
    dump_json('requested-buy-analysis.json',report)
    dump_json('requested-buy-audit.json',audit)
    Path('requested-buy-message.txt').write_text(message,encoding='utf-8')
    receipts=[]
    valid_until=min([datetime.fromisoformat(e['valid_until']) for e in entries if e['trade_ready']],default=None)
    for part in split_telegram_text(message):
        if valid_until is not None and datetime.now(KST)>=valid_until:raise RuntimeError('Entry quote expired before send')
        try:
            response=requests.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/sendMessage',
                json=dict(chat_id=os.environ['TELEGRAM_CHAT_ID'],text=part),timeout=10,allow_redirects=False)
        except requests.RequestException:raise RuntimeError('Telegram network response unconfirmed; not retried') from None
        if response.status_code!=200:raise RuntimeError(f'Telegram HTTP {response.status_code}; not retried')
        body=response.json()
        if body.get('ok') is not True:raise RuntimeError('Telegram did not confirm delivery')
        receipts.append(dict(message_id=body['result']['message_id'],date=body['result']['date']))
    audit.update(telegram_sent=True,telegram_receipts=receipts)
    dump_json('requested-buy-audit.json',audit)
    print('REQUESTED_BUY_DELIVERY='+json.dumps(audit,ensure_ascii=False,default=str),flush=True)

if __name__=='__main__':main()
