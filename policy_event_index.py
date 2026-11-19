"""Persist public government policy evidence between runs, preserving publication and first observation."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,os,subprocess
from urllib.parse import urlparse

PATH=Path('reports/policy-event-index.json')
_RESTORED=False

def stamp(value):
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result if result.utcoffset() is not None else None
    except ValueError:return None

def restore():
    global _RESTORED
    if _RESTORED or not os.getenv('GH_TOKEN'):return
    _RESTORED=True
    if PATH.exists():return
    r=subprocess.run(['gh','run','list','--workflow','policy-event-index.yml','--branch','main',
        '--status','success','--limit','3','--json','databaseId'],capture_output=True,text=True)
    if r.returncode:return
    for run in json.loads(r.stdout):
        folder=Path('policy-index-download')/str(run['databaseId'])
        done=subprocess.run(['gh','run','download',str(run['databaseId']),'--name','policy-event-index',
            '--dir',str(folder)],capture_output=True)
        files=list(folder.rglob('policy-event-index.json'))
        if done.returncode==0 and files:
            PATH.parent.mkdir(exist_ok=True,parents=True)
            PATH.write_bytes(files[0].read_bytes());return

def merge_index(old,new,at):
    merged={}
    for record in [*old,*new]:
        published=stamp(record.get('available_at'))
        host=urlparse(record.get('url','')).hostname or ''
        if not (host=='korea.kr' or host.endswith('.korea.kr')):continue
        if not published or published<at-timedelta(days=30) or published>at+timedelta(days=1):continue
        key=record.get('event_id') or record.get('url')
        if not key:continue
        previous=merged.get(key)
        row=dict(record)
        if len(row.get('body',''))>=400:row['last_seen_at']=at.isoformat()
        row['first_seen_at']=previous.get('first_seen_at') if previous else record.get('first_seen_at',at.isoformat())
        if previous:
            if len(row.get('body',''))<400 and len(previous.get('body',''))>=400:continue
            if previous.get('publication_verified') and not row.get('publication_verified'):continue
            if previous.get('publication_verified') and row.get('publication_verified'):
                row['available_at']=min(previous['available_at'],row['available_at'],key=lambda v:stamp(v))
        merged[key]=row
    return list(merged.values())

def prime_public_policy_cache(body_cache,metadata_cache,at):
    restore()
    try:records=json.loads(PATH.read_text(encoding='utf-8'))
    except (OSError,ValueError):return
    for row in records:
        seen=stamp(row.get('last_seen_at'))
        if (seen and at-timedelta(minutes=15)<=seen<=at and len(row.get('body',''))>=400
                and row.get('body_status')=='public_body'):
            key=row.get('url')
            body_cache[key]=(at+timedelta(minutes=5),(row['body'],'public_body'))
            metadata_cache[key]={k:row[k] for k in ('available_at','publication_verified','availability_basis','source_published_date') if k in row}

def merge_with_history(events,at):
    restore()
    try:old=json.loads(PATH.read_text(encoding='utf-8'))
    except (OSError,ValueError):old=[]
    policy=merge_index(old,events,at)
    if os.getenv('GH_TOKEN'):
        PATH.parent.mkdir(exist_ok=True,parents=True)
        PATH.write_text(json.dumps(policy,ensure_ascii=False),encoding='utf-8')
    nonpolicy=[e for e in events if not (urlparse(e.get('url','')).hostname or '').endswith('korea.kr')]
    return [*nonpolicy,*policy]
