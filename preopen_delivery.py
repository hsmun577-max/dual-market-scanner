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


def attach_exit_watch(report, market, artifact_market=None):
    """직전 세션 계획의 매도 감시 줄을 보고서에 붙인다.

    계획이 나간 뒤 다음 세션들의 +6% · 트레일링 · 손절 · 5거래일 만기를 보는 자리다
    (`exit_watch`). 이 실행은 계좌를 읽지 않으므로 블록이 '체결 미확인 · 계획 진입가 가정'을
    밝힌다. 직전 계획은 그 실행의 산출물(`preopen-analysis.json`)에서 읽고, 받지 못하거나
    시세가 없으면 이 블록만 빠지고 알림은 그대로 나간다.
    """
    try:
        import exit_watch
        prior=exit_watch.previous_analysis(artifact_market or market,limit=6)
        lines,rows=exit_watch.watch(prior,market) if prior else ([],[])
        if lines:
            report['exit_watch_lines']=lines
        print('EXIT_WATCH='+json.dumps(dict(lines=len(lines),positions=len(rows)),
                                      ensure_ascii=False),flush=True)
        return lines
    except Exception as exc:
        print('Exit watch unavailable: '+type(exc).__name__,flush=True)
        return []


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
    # 직전 전 종목 스캔이 계산해 둔 미국 세부 테마(간밤 미국장)는 아래에서 자기 시장 것만
    # 다시 만들기 전에 붙잡아 둔다. 국내 장전 블록의 '미국 연동' 맥락이 이 값이다.
    overnight_us_themes=[row for row in report.get('theme_sectors') or []
                         if isinstance(row,dict) and str(row.get('market') or '').upper().startswith('US')]
    try:
        from theme_sectors import outside_basket_quotes, theme_sector_snapshot
        # 장전은 전날 마감 기준이다(2026-11-26 지시). 당일 장중 봉은 읽지 않는다.
        # 직전 전 종목 스캔이 남긴 바스켓 밖 거래대금 상위 지표를 동적 이슈 클러스터 후보로 넘긴다.
        report['theme_sectors']=[theme_sector_snapshot(
            market,local,mode='previous',
            extra_quotes=outside_basket_quotes(report['results'],market))]
    except Exception as exc:  # 테마 집계 실패가 장전 알림 자체를 막지 않는다
        print('Theme sectors unavailable: '+type(exc).__name__,flush=True)
    scanner.prepare_selected_analysis(report)
    import exchange_calendars
    from preopen_signals import evaluate_preopen
    cal=exchange_calendars.get_calendar('XNYS' if market=='US' else 'XKRX')
    previous_session=cal.previous_session(window['session_date']).strftime('%Y-%m-%d')
    report['preopen_session']=window['session_date']
    evaluate_preopen(report,previous_session,datetime.now(timezone.utc))
    # 미국 장전 요약: 차트 게이트와 무관한 장전 사전 시그널을 발송 전에 준비한다.
    # 발송 창(개장 10분 전 ±2분) 안에서 외부 조회를 하지 않도록 미리 만들어 둔다.
    if market=='US':
        try:
            from premarket_signals import attach as attach_premarket
            watch=attach_premarket(report,datetime.now(timezone.utc))
            print('PREMARKET_WATCH='+json.dumps(dict(
                items=len(watch.get('items') or []),quotes_seen=watch.get('quotes_seen'),
                news_seen=watch.get('news_seen'),error=watch.get('error')),ensure_ascii=False),flush=True)
        except Exception as exc:  # 장전 감시 실패가 장전 알림 자체를 막지 않는다
            report['premarket_watch']=dict(items=[],error=type(exc).__name__)
            print('Premarket watch unavailable: '+type(exc).__name__,flush=True)
    else:
        # 국내 장전 사전 시그널: NXT 장전(08:00~09:00)·시간외 시세와 촉매·간밤 미국 연동을
        # 매수 판정과 분리해 미리 만든다. NXT 단계(07:50 발송)는 개장 전이라 시세가 없고,
        # KRX 단계(08:50 발송)는 08:00부터 쌓인 NXT 체결가를 본다.
        try:
            from kr_premarket import attach as attach_kr_premarket
            kr_watch=attach_kr_premarket(report,datetime.now(timezone.utc),us_rows=overnight_us_themes)
            print('KR_PREMARKET_WATCH='+json.dumps(dict(
                items=len(kr_watch.get('items') or []),session=kr_watch.get('session'),
                quotes_seen=kr_watch.get('quotes_seen'),news_seen=kr_watch.get('news_seen'),
                rejected=kr_watch.get('rejected'),error=kr_watch.get('error')),ensure_ascii=False),flush=True)
        except Exception as exc:  # 장전 감시 실패가 장전 알림 자체를 막지 않는다
            report['kr_premarket_watch']=dict(items=[],error=type(exc).__name__)
            print('Domestic premarket watch unavailable: '+type(exc).__name__,flush=True)
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
    # 보유·매도 감시: 직전 세션 계획의 진입가를 기준으로 +6% · 트레일링 · 손절 · 5거래일 만기를
    # 본다.
    attach_exit_watch(report,market,args.market)
    while datetime.now(timezone.utc)<target:
        time.sleep(min(20,max(0,(target-datetime.now(timezone.utc)).total_seconds())))
    allowed,reason=delivery_status(args.market,datetime.now(timezone.utc))
    os.environ['DELIVERY_MARKET']=args.market
    delivery=dict(sent=False,parts_sent=0,suppressed_by_gate=False,reason=reason)
    if allowed:
        # 알림 최상단용 자산군 흐름·시장 분위기. 직전 스캔이 저장해 둔 블록이 있으면 그것과
        # 비교해 '흐름 변화'를 만들고, 조회 실패는 알림을 막지 않는다.
        try:
            import asset_regime
            asset_regime.attach(report,previous=report.get('asset_regime'))
        except Exception as exc:
            print('Asset regime unavailable: '+type(exc).__name__,flush=True)
        evaluate_preopen(report,previous_session,datetime.now(timezone.utc))
        dump('preopen-analysis.json',report,indent=None)
        delivery=scanner.send_telegram_summary(report)
    result=dict(**window,checked_at=datetime.now(timezone.utc).isoformat(),
                sent=bool(delivery.get('sent')),parts_sent=delivery.get('parts_sent',0),
                suppressed_by_gate=bool(delivery.get('suppressed_by_gate')),
                reason=delivery.get('reason') or reason,
                premarket_signals=len((report.get('premarket_watch') or {}).get('items') or []),
                kr_premarket_signals=len((report.get('kr_premarket_watch') or {}).get('items') or []),
                urgent_issues=sum(len(rows) for rows in (report.get('urgent_issues') or {}).values()))
    dump('preopen-delivery.json',result)
    print('PREOPEN_DELIVERY='+json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__': main()
