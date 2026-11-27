"""Find published issuer catalysts before reading technical or company scores."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import os
import re
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

FEEDS = {
    'thebionews': ('KR', 'https://www.thebionews.net/rss/allArticle.xml'),
    'biotimes': ('KR', 'https://www.biotimes.co.kr/rss/allArticle.xml'),
    'globenewswire': ('US', 'https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20Public%20Companies'),
}
DART_LIST = 'https://opendart.fss.or.kr/api/list.json'
RESEARCH_SLOTS = 6
KST = ZoneInfo('Asia/Seoul')
FAVORABLE = re.compile(
    r'공급계약\s*체결|판매계약\s*체결|수주\s*(?:성공|확보)|'
    r'기술\s*이전.{0,18}(?:계약|체결)|기술수출.{0,14}(?:계약|체결)|'
    r'(?:국가신약개발사업|정부과제|국책과제).{0,40}선정|'
    r'(?:FDA|식약처).{0,25}(?:품목허가|임상.{0,8}승인)|'
    r'단일판매[·ㆍ・]?공급계약체결|'
    r'\b(?:awarded .{0,35}contract|(?:signs|announces) .{0,35}(?:licensing|supply) agreement|'
    r'FDA (?:approval|approves)|raises (?:its )?(?:full.year )?(?:revenue )?guidance)\b', re.I)
ADVERSE = re.compile(r'미선정|미승인|불허|정정|해지|철회|실패|루머|신청|추진|기대|'
                     r'\b(?:terminat\w*|withdraw\w*|denied|seeks|application)\b', re.I)
US_LISTING = re.compile(r'\b(?:NASDAQ|NYSE(?:\s+(?:American|Arca))?)\s*:\s*([A-Z][A-Z0-9.\-]{0,9})\b', re.I)
KR_SUBJECT = re.compile(r'^\s*(?:\[[^\]]+\]\s*)?([^\s,，:·“”"\']+)')
PARTICLES = ('에서', '으로', '은', '는', '이', '가', '의', '을', '를')


def eligible_headline(title):
    return bool(FAVORABLE.search(title) and not ADVERSE.search(title))


def _by_unique_key(rows, key):
    indexed = {}
    for row in rows:
        value = key(row)
        if value:
            indexed[value] = row if value not in indexed else None
    return indexed


def read_rss(source, market, url, rows, at):
    response = requests.get(url, timeout=6)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    kr_names = _by_unique_key(rows, lambda row: (row.get('name') or '').strip())
    us_symbols = _by_unique_key(rows, lambda row: row['symbol'].upper().replace('.', '-'))
    events = []
    for item in root.findall('./channel/item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        if not eligible_headline(title) or not link.startswith('https://'):
            continue
        try:
            published = parsedate_to_datetime(item.findtext('pubDate'))
        except (ValueError, TypeError, OverflowError):
            continue
        if published.utcoffset() is None or not at - timedelta(days=5) <= published <= at:
            continue
        if market == 'KR':
            subject = KR_SUBJECT.match(title)
            if not subject:
                continue
            name = subject[1]
            row = kr_names.get(name)
            if name not in kr_names:
                row = next((kr_names[name[:-len(particle)]] for particle in PARTICLES
                            if name.endswith(particle) and name[:-len(particle)] in kr_names), None)
        else:
            mentions = {match.upper().replace('.', '-') for match in US_LISTING.findall(
                title + ' ' + (item.findtext('description') or ''))}
            row = us_symbols.get(mentions.pop()) if len(mentions) == 1 else None
        if row:
            events.append(dict(symbol=row['symbol'], name=row.get('name'), market=market,
                               headline=title, url=link, source=source, available_at=published.isoformat()))
    return events, 'ok'


def read_dart(corp_class, rows, at, api_key):
    indexed = _by_unique_key(rows, lambda row: row['symbol'].split('.')[0])
    local = at.astimezone(KST)
    events = []
    for page in range(1, 7):
        response = requests.get(DART_LIST, params=dict(
            crtfc_key=api_key, bgn_de=(local - timedelta(days=4)).strftime('%Y%m%d'),
            end_de=local.strftime('%Y%m%d'), corp_cls=corp_class, page_count=100, page_no=page), timeout=6)
        response.raise_for_status()
        payload = response.json()
        if payload.get('status') == '013':
            return events, 'ok'
        if payload.get('status') != '000':
            return events, 'unavailable'
        for item in payload.get('list', ()):
            title = item.get('report_nm') or ''
            row = indexed.get(item.get('stock_code'))
            receipt = item.get('rcept_no') or ''
            if not row or not receipt.isdigit() or not eligible_headline(title):
                continue
            # The list has a receipt date only. This live observation has no
            # historical publication timestamp and must never be used in replay.
            events.append(dict(symbol=row['symbol'], name=row.get('name'), market='KR',
                               headline=title, url='https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + receipt,
                               source='dart', available_at=datetime.now(timezone.utc).isoformat(),
                               receipt_date=item.get('rcept_dt'),
                               timestamp_basis='observed_during_live_scan'))
        if page >= int(payload['total_page']):
            return events, 'ok'
    return events, 'page_limit'


def discover(rows, at):
    from research_asset import is_fund
    universes = {market: [row for row in rows if row.get('market') == market and not is_fund(row)]
                 for market in ('KR', 'US')}
    events, coverage = [], {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        pending = {source: executor.submit(read_rss, source, market, url, universes[market], at)
                   for source, (market, url) in FEEDS.items() if universes[market]}
        if universes['KR']:
            api_key = os.getenv('DART_API_KEY', '').strip()
            if api_key:
                for corp_class in ('Y', 'K'):
                    pending['dart_' + corp_class] = executor.submit(
                        read_dart, corp_class, universes['KR'], at, api_key)
            else:
                coverage['dart'] = 'missing_key'
        for source, task in pending.items():
            try:
                found, coverage[source] = task.result()
                events.extend(found)
            except (requests.RequestException, ValueError, TypeError, KeyError, ElementTree.ParseError):
                coverage[source] = 'unavailable'
    unique = {}
    for event in sorted(events, key=lambda event: (
            event['source'] != 'dart',
            -(int(event.get('receipt_date') or '0') if event['source'] == 'dart' else
              datetime.fromisoformat(event['available_at']).timestamp()),
            event['symbol'], event['headline'])):
        unique.setdefault((event['market'], event['symbol']), event)
    return {'items': list(unique.values()), 'coverage': coverage, 'complete_news_coverage': False}


def merge_candidates(selected, pool, events, market):
    from advance_policy import eligible_for_advance_research
    selected_by_symbol = {row['symbol']: row for row in selected}
    pool_by_symbol = {row['symbol']: row for row in pool}
    reserved = 0
    for event in events:
        if event['market'] != market:
            continue
        symbol = event['symbol']
        row = selected_by_symbol.get(symbol)
        if row is None:
            row = pool_by_symbol.get(symbol)
            if row is None or reserved >= RESEARCH_SLOTS or not eligible_for_advance_research(row, market):
                continue
            selected_by_symbol[symbol] = row
            reserved += 1
        tracks = row.setdefault('discovery_tracks', [])
        if 'issuer_catalyst' not in tracks:
            tracks.append('issuer_catalyst')
        row['issuer_catalyst_watch'] = event
    return list(selected_by_symbol.values())


def format_block(discovery, market):
    items = [event for event in discovery.get('items', ()) if not market or event['market'] == market]
    return ['🔎 호재 독립 감시 · 최종 검토 전', *[
        f"  {event.get('name') or event['symbol']} · {event['headline']}" for event in items[:4]
    ]] if items else []
