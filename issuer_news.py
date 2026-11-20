"""Public issuer news discovery with direct article URLs and strict entity/date matching."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import re
import requests
from event_impact_analysis import issuer_name_in_text,issuer_title_subject,HEADERS

def _items(document):
    if isinstance(document,list):
        for row in document:yield from _items(row)
    elif isinstance(document,dict):
        if document.get('articleId') and document.get('officeId'):yield document
        else:
            for value in document.values():
                if isinstance(value,(list,dict)):yield from _items(value)

def parse_issuer_news(document,name,now):
    rows=[];aliases=[name]
    shortened=re.sub(r'\s+Ent\.$','',name)
    if shortened!=name:aliases.append(shortened)
    for row in _items(document):
        title=str(row.get('title') or '').strip()
        if not any(issuer_name_in_text(issuer_title_subject(title),alias) for alias in aliases):continue
        raw=str(row.get('datetime') or row.get('publishDateTime') or row.get('publishedAt') or '')
        try:
            if re.fullmatch(r'\d{14}',raw):published=datetime.strptime(raw,'%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Seoul'))
            else:
                published=datetime.fromisoformat(raw.replace('Z','+00:00'))
                if published.utcoffset() is None:continue
        except ValueError:continue
        if not now-timedelta(hours=24)<=published<now:continue
        office=str(row['officeId']);article=str(row['articleId'])
        if not re.fullmatch(r'\d{3}',office) or not re.fullmatch(r'\d{6,20}',article):continue
        rows.append(dict(headline=title,available_at=published.isoformat(),
            source=row.get('officeName') or 'Naver syndicated publisher',
            url=f'https://n.news.naver.com/article/{office}/{article}'))
    return sorted({row['url']:row for row in rows}.values(),key=lambda row:row['available_at'],reverse=True)

def issuer_news(symbol,name,now):
    for url,params in [
        ('https://m.stock.naver.com/front-api/news/list/integration',dict(itemCode=symbol,page=1,pageSize=30)),
        (f'https://m.stock.naver.com/api/news/stock/{symbol}',dict(page=1,pageSize=30))]:
        try:
            response=requests.get(url,params=params,headers=HEADERS,timeout=6,allow_redirects=False)
            if response.status_code!=200:continue
            rows=parse_issuer_news(response.json(),name,now)
            if rows:return rows
        except (requests.RequestException,ValueError,TypeError):continue
    return []
