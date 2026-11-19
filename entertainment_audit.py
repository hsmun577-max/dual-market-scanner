"""Compare archived decisions with current event routing; never simulate a historical buy."""
import json
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import subprocess
from candidate_selection import candidate_rejection_reason,liquidity_rejection_reason
from economic_graph import load_profiles,connect,frames,discover,select_analysis_candidates
from event_impact_analysis import world_events
from profile_universe import restore_index

def audit(report,registry,events,at):
    event_frames=frames(events,at)
    linked=discover(registry,events,at,limit=None)
    pool=[r for r in report['results'] if r['market']=='KR']
    selected={r['symbol'] for r in select_analysis_candidates(pool,'KR',linked)}
    companies=[]
    for code in ('352820','041510','122870','035900'):
        row=next((r for r in pool if r['symbol'].split('.')[0]==code),None)
        company=next((v for k,v in registry.items() if k.removeprefix('DNAS').split('.')[0]==code),{})
        graph=connect(company,event_frames,at)
        companies.append(dict(symbol=code,name=row.get('name') if row else None,
            in_archived_scan=bool(row),selection_filter_then=row.get('selection_filter') if row else None,
            current_rule_rejection=candidate_rejection_reason(row,'KR') if row else 'missing',
            liquidity_rejection=liquidity_rejection_reason(row,'KR') if row else 'missing',
            analysed_then='company_analysis' in row if row else False,
            valuation_inputs_then={key:(row.get('company_analysis') or {}).get(key) for key in
                ('trailing_pe','forward_pe','trailing_eps','forward_eps','sector','industry')} if row else None,
            missing_dimensions=(row.get('company_forecast') or {}).get('missing_dimensions') if row else None,
            indicators=row.get('indicators') if row else None,
            profile_usable_now=graph['profile']['usable'],event_status_now=graph['status'],
            discovered_now=bool(row and row['symbol'] in linked),selected_for_research_now=bool(row and row['symbol'] in selected)))
    assessed=[r for r in report['results'] if 'company_forecast' in r]
    return dict(source_generated_at=report.get('generated_at'),comparison_basis='archived scan plus current evidence; not a historical replay',
        archived_assessed=len(assessed),archived_complete=sum(r['company_forecast'].get('composite_score') is not None for r in assessed),
        archived_sector_count=len(report.get('sectors',[])),
        archived_missing_dimensions=dict(Counter(k for r in assessed for k in r['company_forecast'].get('missing_dimensions',[]))),
        daily_buy_evaluation_connected=False,companies=companies,
        prior_event_discovery=report.get('event_discovery'),current_linked_count=len(linked))

def main():
    restore_index()
    registry=load_profiles()
    events,coverage=world_events(datetime.now(timezone.utc))
    at=datetime.now(timezone.utc)
    outputs={}
    latest=Path('opening-scan/latest.json')
    if latest.exists(): outputs['latest']=audit(json.loads(latest.read_text(encoding='utf-8-sig')),registry,events,at)
    folder=Path('historical-entertainment')
    result=subprocess.run(['gh','run','download','44439674147','--name','scan-results','--dir',str(folder)],capture_output=True)
    files=list(folder.rglob('latest.json'))
    if result.returncode==0 and files:
        outputs['morning']=audit(json.loads(files[0].read_text(encoding='utf-8-sig')),registry,events,at)
    else: outputs['morning_error']='historical_artifact_unavailable'
    outputs['current_feed_coverage']=coverage
    outputs['current_event_headlines']=[e.get('headline') for e in events]
    Path('entertainment-audit.json').write_text(json.dumps(outputs,ensure_ascii=False,indent=2),encoding='utf-8')
    print('ENTERTAINMENT_AUDIT='+json.dumps(outputs,ensure_ascii=False),flush=True)
if __name__=='__main__': main()
