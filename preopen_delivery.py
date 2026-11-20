"""Prepare analysis ahead of an exchange opening and deliver only in its window."""
import argparse
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo
from delivery_policy import plan,delivery_status
from json_utils import dump


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('market',choices=['NXT','KRX','US_PRE','US'])
    args=parser.parse_args(); now=datetime.now(timezone.utc)
    window=plan(args.market,now)
    if not window:
        print('PREOPEN_SKIPPED=closed_or_unsupported_session'); return
    deadline=datetime.fromisoformat(window['expires_at']); target=datetime.fromisoformat(window['target_at'])
    print('PREOPEN_PLAN='+json.dumps(window),flush=True)
    if now>=deadline:
        print('PREOPEN_SKIPPED=missed_delivery_window'); return
    prepare_at=target-timedelta(minutes=30)
    while datetime.now(timezone.utc)<prepare_at:
        time.sleep(min(30,max(0,(prepare_at-datetime.now(timezone.utc)).total_seconds())))
    from profile_universe import restore_index
    restore_index()
    source=Path('opening-scan/latest.json')
    if not source.is_file(): raise RuntimeError('No completed full scan available for preopen analysis')
    report=json.loads(source.read_text(encoding='utf-8-sig'))
    market='US' if args.market.startswith('US') else 'KR'
    report['results']=[row for row in report['results'] if row['market']==market]
    import scanner
    local=datetime.now(ZoneInfo('America/New_York' if market=='US' else 'Asia/Seoul'))
    report['sectors']=scanner.sector_flow_snapshot(market,local)
    scanner.prepare_selected_analysis(report)
    import exchange_calendars
    from preopen_signals import evaluate_preopen
    cal=exchange_calendars.get_calendar('XNYS' if market=='US' else 'XKRX')
    previous_session=cal.previous_session(window['session_date']).strftime('%Y-%m-%d')
    report['preopen_session']=window['session_date']
    evaluate_preopen(report,previous_session,datetime.now(timezone.utc))
    dump('preopen-analysis.json',report)
    print('PREOPEN_BUY_EVALUATION='+json.dumps(report['buy_evaluation'],ensure_ascii=False),flush=True)
    if market=='KR':
        try:
            import exchange_calendars
            from industry_flow import snapshot,format_report
            cal=exchange_calendars.get_calendar('XKRX')
            previous=cal.previous_session(window['session_date']).strftime('%Y%m%d')
            data=snapshot(previous)
            if data['rows']:
                report['sector_header_override']=format_report(data).split('\n')
        except Exception as exc:
            print('Industry investor data unavailable: '+type(exc).__name__,flush=True)
    while datetime.now(timezone.utc)<target:
        time.sleep(min(20,max(0,(target-datetime.now(timezone.utc)).total_seconds())))
    allowed,reason=delivery_status(args.market,datetime.now(timezone.utc))
    os.environ['DELIVERY_MARKET']=args.market
    delivery=dict(sent=False,parts_sent=0,suppressed_by_gate=False,reason=reason)
    if allowed:
        evaluate_preopen(report,previous_session,datetime.now(timezone.utc))
        dump('preopen-analysis.json',report,indent=None)
        delivery=scanner.send_telegram_summary(report)
    result=dict(**window,checked_at=datetime.now(timezone.utc).isoformat(),
                sent=bool(delivery.get('sent')),parts_sent=delivery.get('parts_sent',0),
                suppressed_by_gate=bool(delivery.get('suppressed_by_gate')),
                reason=delivery.get('reason') or reason)
    dump('preopen-delivery.json',result)
    print('PREOPEN_DELIVERY='+json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__': main()
