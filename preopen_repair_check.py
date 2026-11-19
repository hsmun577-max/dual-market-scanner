"""Live read-only verification of the production analysis -> conditional plan -> message path."""
from datetime import datetime,timezone
from pathlib import Path
import json
from zoneinfo import ZoneInfo
import exchange_calendars
from profile_universe import restore_index
import scanner
from preopen_signals import evaluate_preopen
from sector_flow import format_sector_header

def main():
    restore_index()
    source=json.loads(Path('opening-scan/latest.json').read_text(encoding='utf-8-sig'))
    wanted={'352820','041510','122870','035900'}
    rows=[row for row in source['results'] if row['symbol'].split('.')[0] in wanted]
    now=datetime.now(timezone.utc)
    local=now.astimezone(ZoneInfo('Asia/Seoul'))
    cal=exchange_calendars.get_calendar('XKRX')
    session=cal.date_to_session(local.date().isoformat(),direction='next')
    previous=cal.previous_session(session).strftime('%Y-%m-%d')
    report=dict(results=rows,sectors=scanner.sector_flow_snapshot('KR',local),
                preopen_session=session.strftime('%Y-%m-%d'),verification_only=True)
    scanner.prepare_selected_analysis(report)
    evaluate_preopen(report,previous,datetime.now(timezone.utc))
    lines=format_sector_header(report['sectors'],'KR')+['','매수 신호']
    signals=[r for r in rows if r.get('preopen_signal',{}).get('status')=='conditional_buy']
    lines += [scanner.telegram_candidate_line(r) for r in signals]
    if not signals: lines.append('매수 조건 충족 종목 없음' if report['buy_evaluation']['completed'] else '분석 미완료')
    Path('preopen-repair-preview.txt').write_text('\n'.join(lines),encoding='utf-8')
    Path('preopen-repair-analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    audit=dict(as_of=datetime.now(timezone.utc).isoformat(),historical_replay=False,
        telegram_sent=False,buy_evaluation=report['buy_evaluation'],
        companies=[dict(name=r.get('name'),symbol=r['symbol'],
            decision=r.get('preopen_decision'),valuation_source=r.get('company_analysis',{}).get('valuation_source'),
            peer_valuation=r.get('company_analysis',{}).get('peer_valuation'),
            sector=r.get('company_analysis',{}).get('sector'),
            forecast_status=r.get('company_forecast',{}).get('assessment_status'),
            missing=r.get('company_forecast',{}).get('missing_dimensions'),
            score=r.get('company_forecast',{}).get('composite_score'),
            event_status=r.get('company_analysis',{}).get('event_analysis',{}).get('status'),
            event_graph_status=r.get('company_analysis',{}).get('event_analysis',{}).get('economic_graph',{}).get('status'),
            unread_material_events=[e for e in r.get('company_analysis',{}).get('event_analysis',{}).get('unread_events',[]) if e.get('required')],
            price_date=r['indicators'].get('data_date')) for r in rows])
    Path('preopen-repair-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PREOPEN_REPAIR_AUDIT='+json.dumps(audit,ensure_ascii=False),flush=True)
    if not report['buy_evaluation']['connected'] or report['buy_evaluation']['evaluated']!=4:
        raise RuntimeError('All four requested issuers must reach the production decision function')
    if len(rows)!=4: raise RuntimeError('Requested issuer missing from the full scan')
    if any(r.get('company_forecast',{}).get('assessment_status')!='complete' for r in rows):
        raise RuntimeError('Live issuer assessments still incomplete; inspect missing evidence in the audit artifact')

if __name__=='__main__':main()
