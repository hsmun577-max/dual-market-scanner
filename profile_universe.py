"""Refresh a reusable issuer exposure index over the full exchange master lists."""
import argparse
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import time
import yfinance as yf
from scanner import load_universe
from candidate_selection import instrument_rejection_reason
from economic_graph import load_profiles


def refresh(entries, cached, fetch, at, max_age_days=30, checkpoint=None, deadline=None):
    """Retain old evidence with its old date; failed downloads never become fresh."""
    profiles=dict(cached); status={}; failures=0
    for symbol,name in entries:
        if deadline is not None and time.monotonic()>=deadline: break
        old=profiles.get(symbol,{})
        if not isinstance(old,dict): old={}
        try:
            stamp=datetime.fromisoformat(old.get('profile_available_at',''))
            fresh=stamp.utcoffset() is not None and timedelta(0)<=at-stamp<=timedelta(days=max_age_days)
        except ValueError: fresh=False
        if fresh and old.get('business_description'):
            status[symbol]='cached'; continue
        try:
            info=fetch(symbol)
            if not isinstance(info,dict) or not info.get('quoteType'):
                status[symbol]='provider_data_unavailable'; failures+=1
                if failures>=10: break
                continue
            failures=0
            if info.get('quoteType') != 'EQUITY':
                status[symbol]='unsupported_instrument'; continue
            text=info.get('longBusinessSummary')
            if not isinstance(text,str) or len(text)<80:
                status[symbol]='missing_business_description'; continue
            profiles[symbol]=dict(company_name=info.get('longName') or info.get('shortName') or name,
                display_name=name,
                business_description=text,industry=info.get('industry'),sector=info.get('sector'),
                country=info.get('country'),exchange=info.get('exchange'),
                profile_available_at=datetime.now(timezone.utc).isoformat(),
                profile_source='Yahoo Finance issuer profile',source_symbol=symbol)
            status[symbol]='refreshed'
        except Exception as exc:
            failures+=1
            status[symbol]='provider_error:'+type(exc).__name__
            if 'ratelimit' in type(exc).__name__.lower() or failures>=10:
                # Stop this batch rather than hammering a provider that rejected it.
                break
        if checkpoint is not None and len(status)%100==0:
            checkpoint(profiles,status)
    return profiles,status


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--shard',type=int,default=0)
    parser.add_argument('--shards',type=int,default=4)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--restore',action='store_true')
    parser.add_argument('--merge',action='store_true')
    args=parser.parse_args()
    if args.restore:
        print('Restored issuer profiles:',restore_index()); return
    if args.merge:
        print('Merged issuer profiles:',merge_profiles(Path('profiles').glob('part-*.json'))); return
    if not 0<=args.shard<args.shards: parser.error('Invalid shard')
    root=Path('profiles'); root.mkdir(exist_ok=True)
    path=root/f'part-{args.shard}.json'
    cached=load_profiles(str(path))
    universe=load_universe()
    entries=sorted((symbol,name) for market,names in universe.items() for symbol,name in names.items()
                   if instrument_rejection_reason(symbol,name) is None)
    assigned=entries[args.shard::args.shards]
    if args.limit: assigned=assigned[:args.limit]
    def fetch(symbol):
        time.sleep(.25)
        return yf.Ticker(symbol).get_info() or {}
    def checkpoint(profiles,status):
        path.write_text(json.dumps(profiles,ensure_ascii=False),encoding='utf-8')
        print('Profile checkpoint',args.shard,len(status),len(assigned),flush=True)
    profiles,status=refresh(assigned,cached,fetch,datetime.now(timezone.utc),
                            checkpoint=checkpoint,deadline=time.monotonic()+3600)
    path.write_text(json.dumps(profiles,ensure_ascii=False),encoding='utf-8')
    counts={value:list(status.values()).count(value) for value in set(status.values())}
    audit=dict(shard=args.shard,universe_count=len(entries),assigned=len(assigned),attempted=len(status),
               retained_profiles=len(profiles),counts=counts,enumeration_complete=len(status)==len(assigned),
               usable_in_this_batch=sum(v in {'cached','refreshed'} for v in status.values()),
               unattempted=len(assigned)-len(status),
               missing_or_unsupported=sum(v not in {'cached','refreshed'} for v in status.values()))
    (root/f'audit-{args.shard}.json').write_text(json.dumps(audit),encoding='utf-8')
    print('PROFILE_SHARD='+json.dumps(audit),flush=True)




def merge_profiles(paths, output='reports/company-profiles.json'):
    from economic_graph import stamp
    merged=load_profiles(output); korean_symbols=None
    for path in paths:
        for symbol,company in load_profiles(str(path)).items():
            if not isinstance(company,dict): continue
            key=company.get('source_symbol') or symbol
            if key.isdigit() and len(key)==6:
                if korean_symbols is None:
                    try: korean_symbols=load_universe()['KR']
                    except Exception: korean_symbols={}
                matches=[ticker for ticker in korean_symbols if ticker.split('.')[0]==key]
                if len(matches)==1:
                    key=matches[0]; company={**company,'source_symbol':key}
            old=merged.get(key,{})
            old_at=stamp(old.get('profile_available_at')); new_at=stamp(company.get('profile_available_at'))
            if new_at and (not old_at or new_at>=old_at): merged[key]=company
    target=Path(output); target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(merged,ensure_ascii=False),encoding='utf-8')
    return len(merged)


def restore_index():
    import subprocess
    for workflow,artifact in [('company-profile-index.yml','company-exposures'),
                              ('economic-graph-check.yml','economic-graph-audit')]:
        result=subprocess.run(['gh','run','list','--workflow',workflow,'--branch','main',
            '--status','success','--limit','3','--json','databaseId'],capture_output=True,text=True)
        if result.returncode: continue
        for row in json.loads(result.stdout):
            folder=Path('profile-download')/str(row['databaseId'])
            done=subprocess.run(['gh','run','download',str(row['databaseId']),'--name',artifact,
                                 '--dir',str(folder)],capture_output=True)
            files=list(folder.rglob('company-profiles.json'))
            if done.returncode==0 and files:
                count=merge_profiles(files)
                restore_prices()
                return count
    return 0


def restore_prices():
    """Use the last completed full scan for prioritization, never as a live quote."""
    import subprocess
    import shutil
    result=subprocess.run(['gh','run','list','--workflow','scanner.yml','--branch','main',
        '--status','success','--limit','3','--json','databaseId'],capture_output=True,text=True)
    if result.returncode: return
    for row in json.loads(result.stdout):
        folder=Path('price-download')/str(row['databaseId'])
        done=subprocess.run(['gh','run','download',str(row['databaseId']),'--name','scan-results',
                             '--dir',str(folder)],capture_output=True)
        files=list(folder.rglob('latest.json'))
        if done.returncode==0 and files:
            target=Path('opening-scan/latest.json'); target.parent.mkdir(exist_ok=True)
            shutil.copyfile(files[0],target); return


if __name__=='__main__': main()
