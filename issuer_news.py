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
        title=str(row.get('titleFull') or row.get('title') or '').strip()
        # The provider can shorten the headline while preserving the issuer in
        # its public preview. Use that preview only for entity matching.
        match_text=issuer_title_subject(title)+' '+str(row.get('body') or '')
        if not any(issuer_name_in_text(match_text,alias) for alias in aliases):continue
        raw=str(row.get('datetime') or row.get('publishDateTime') or row.get('publishedAt') or '')
        try:
            if re.fullmatch(r'\d{12}',raw):
                published=datetime.strptime(raw,'%Y%m%d%H%M').replace(tzinfo=ZoneInfo('Asia/Seoul'))+timedelta(minutes=1)
            elif re.fullmatch(r'\d{14}',raw):published=datetime.strptime(raw,'%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Seoul'))
            else:
                published=datetime.fromisoformat(raw.replace('Z','+00:00'))
                if published.utcoffset() is None:continue
        except ValueError:continue
        if not now-timedelta(hours=24)<=published<now:continue
        office=str(row['officeId']);article=str(row['articleId'])
        if not re.fullmatch(r'\d{3}',office) or not re.fullmatch(r'\d{6,20}',article):continue
        rows.append(dict(headline=title,available_at=published.isoformat(),
            source=row.get('officeName') or 'Naver syndicated publisher',
            availability_basis='source_minute_next_minute' if len(raw)==12 and raw.isdigit() else 'source_news_timestamp',
            url=f'https://n.news.naver.com/article/{office}/{article}'))
    return sorted({row['url']:row for row in rows}.values(),key=lambda row:row['available_at'],reverse=True)

def issuer_news_result(symbol,name,now):
    attempts=[];checked=False
    for url,params in [
        ('https://m.stock.naver.com/front-api/news/list/integration',dict(itemCode=symbol,page=1,pageSize=30)),
        (f'https://m.stock.naver.com/api/news/stock/{symbol}',dict(page=1,pageSize=30))]:
        try:
            response=requests.get(url,params=params,headers=HEADERS,timeout=6,allow_redirects=False)
            if response.status_code!=200:
                attempts.append(dict(status='unavailable',http_status=response.status_code));continue
            document=response.json()
            known=(isinstance(document,list) or (isinstance(document,dict) and document.get('isSuccess') is True))
            if not known:
                attempts.append(dict(status='unknown_schema'));continue
            checked=True;rows=parse_issuer_news(document,name,now)
            attempts.append(dict(status='checked',count=len(rows)))
            if rows:return dict(status='checked',articles=rows,attempts=attempts)
        except (requests.RequestException,ValueError,TypeError):attempts.append(dict(status='unavailable'))
    return dict(status='checked' if checked else 'unavailable',articles=[],attempts=attempts)


def issuer_news(symbol,name,now):
    return issuer_news_result(symbol,name,now)['articles']
