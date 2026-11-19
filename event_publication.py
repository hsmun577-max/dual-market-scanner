"""Publication metadata, never first-seen time presented as publication."""
from datetime import datetime,timedelta,timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import json,re

class PublicationParser(HTMLParser):
    def __init__(self):
        super().__init__();self.values=[];self.date_depth=0;self.date_text=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta' and (a.get('property') or a.get('name') or '').lower() in {
            'article:published_time','datepublished','pubdate','publishdate','publication_date','og:regdate'}:
            self.values.append(a.get('content',''))
        if tag=='time' and a.get('datetime'):self.values.append(a['datetime'])
        if tag=='span' and re.search(r'(?:^|\s)(?:date|publish-date|article_date)(?:$|\s)',a.get('class','')):
            self.date_depth+=1
    def handle_endtag(self,tag):
        if tag=='span' and self.date_depth:self.date_depth-=1
    def handle_data(self,text):
        if self.date_depth:self.date_text.append(text)

def publication_metadata(document,url,now):
    parser=PublicationParser();parser.feed(document)
    raw=parser.values+re.findall(r'"datePublished"\s*:\s*"([^"]+)"',document)
    day_values=[' '.join(parser.date_text)]
    korea=bool(re.search(r'https://(?:www\.|m\.)?korea\.kr/',url or ''))
    for value in raw:
        try:
            at=datetime.fromisoformat(value.strip().replace('Z','+00:00'))
            if at.utcoffset() is not None and at<=now:
                return dict(available_at=at.isoformat(),publication_verified=True,availability_basis='source_published_time')
        except ValueError:pass
        day_values.append(value)
    if korea:
        for text in day_values:
            match=re.search(r'\b(20\d{2})[.\-/](\d{2})[.\-/](\d{2})\b',text)
            if not match:continue
            day='-'.join(match.groups())
            at=datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Seoul'))+timedelta(days=1)
            if at<=now:
                return dict(available_at=at.isoformat(),publication_verified=True,
                            source_published_date=day,availability_basis='date_only_next_local_day')
    return dict(available_at=now.isoformat(),publication_verified=False,
                availability_basis='first_seen_not_original_publication')
