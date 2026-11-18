"""Auditable causal event analysis. Rule inferences are not verified earnings forecasts."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse
from xml.etree import ElementTree
import math
import re
import requests

ALLOWED = ('federalreserve.gov', 'ecb.europa.eu', 'sec.gov', 'reuters.com',
           'finance.yahoo.com', 'cnbc.com', 'hanaw.com', 'samsungpop.com',
           'yna.co.kr', 'dart.fss.or.kr', 'eia.gov', 'ofac.treasury.gov')
HEADERS = {'User-Agent': 'DualMarketScanner research https://github.com/hsmun577-max/dual-market-scanner'}
CHANNELS = {
 'demand': ('demand', 'orders', 'backlog', '수요', '수주', '수주잔고'),
 'pricing': ('selling prices', 'price increase', 'pricing power', '판가', '판매가격'),
 'input_cost': ('input costs', 'raw material', 'energy costs', '원자재', '원가', '유가'),
 'capacity': ('production capacity', 'factory', 'capacity expansion', '증설', '생산능력', '양산'),
 'trade_policy': ('tariff', 'sanction', 'export control', '관세', '제재', '수출규제', '수출 규제'),
 'financing': ('interest rate', 'policy rate', 'federal funds', '금리', '차입'),
 'currency': ('exchange rate', 'currency', 'stronger dollar', '환율', '원화'),
}
POSITIVE = ('increased', 'increasing', 'growth', 'strong demand', 'raised', 'higher',
            'expansion', 'expanded', 'record', '증가', '확대', '인상', '개선', '상향', '호조')
NEGATIVE = ('decreased', 'declined', 'weaker', 'reduced', 'lower', 'cut', 'weak demand',
            'downturn', '감소', '축소', '인하', '하향', '부진', '악화')
NEGATION = re.compile(r'\b(?:not|no|unlikely|without)\b|않|아니|미확정|검토|가능성|예상|전망|expects?|forecast|may\b', re.I)
FEEDS = (
 ('Federal Reserve', 'https://www.federalreserve.gov/feeds/press_monetary.xml'),
 ('ECB', 'https://www.ecb.europa.eu/rss/press.html'),
 ('EIA', 'https://www.eia.gov/rss/todayinenergy.xml'),
)


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp if stamp.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.depth=0; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','nav','header','footer'): self.skip += 1
        if tag in ('p','td','li'): self.depth += 1
    def handle_endtag(self, tag):
        if tag in ('p','td','li'): self.depth=max(0,self.depth-1); self.parts.append('\n')
        if tag in ('script','style','nav','header','footer'): self.skip=max(0,self.skip-1)
    def handle_data(self, data):
        if self.depth and not self.skip: self.parts.append(data)


def allowed_url(url):
    parsed=urlparse(url or '')
    host=(parsed.hostname or '').lower()
    return (parsed.scheme=='https' and not parsed.username and not parsed.password
            and parsed.port in (None,443)
            and any(host==domain or host.endswith('.'+domain) for domain in ALLOWED))


def article_text(url):
    """Read public allowed publishers; no redirect to arbitrary hosts or paywall bypass."""
    if not allowed_url(url): return '', 'publisher_not_supported'
    try:
        response=requests.get(url,headers=HEADERS,timeout=5,allow_redirects=False,stream=True)
        try:
            response.raise_for_status()
            if response.status_code != 200: return '', 'redirect_or_unavailable'
            if 'html' not in response.headers.get('Content-Type','').lower(): return '', 'not_html'
            body=bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body)>600000: return '', 'body_too_large'
            parser=Paragraphs(); parser.feed(bytes(body).decode(response.encoding or 'utf-8',errors='replace'))
            text=re.sub(r'[ \t]+',' ',''.join(parser.parts)).strip()
            if len(text)<400: return '', 'body_insufficient'
            if any(marker in text[:1200].lower() for marker in ('verify you are human','access denied','subscribe to continue')):
                return '', 'access_restricted'
            return text, 'public_body'
        finally:
            response.close()
    except (requests.RequestException,ValueError): return '', 'download_failed'


def world_events(now):
    """Keep persistent policy events, with explicit feed coverage and publication dates."""
    from email.utils import parsedate_to_datetime
    rows=[]; coverage=[]
    for source,url in FEEDS:
        status='unavailable'; count=0
        try:
            response=requests.get(url,headers=HEADERS,timeout=5,allow_redirects=False)
            response.raise_for_status()
            root=ElementTree.fromstring(response.content)
            for item in root.findall('.//item')[:5]:
                try: published=parsedate_to_datetime(item.findtext('pubDate',''))
                except (ValueError,TypeError): continue
                if published.utcoffset() is None or not now-timedelta(days=90)<=published<=now: continue
                link=item.findtext('link',''); text,body_status=article_text(link)
                rows.append(dict(event_id=link,headline=item.findtext('title',''),url=link,
                    source=source,available_at=published.isoformat(),body=text,body_status=body_status,
                    scope='world',expires_at=(published+timedelta(days=90)).isoformat()))
                count+=1
            status='checked'
        except (requests.RequestException,ElementTree.ParseError,ValueError): pass
        coverage.append(dict(source=source,status=status,events=count,checked_at=now.isoformat()))
    status='unavailable'; count=0
    try:
        response=requests.get('https://ofac.treasury.gov/recent-actions',headers=HEADERS,timeout=5,allow_redirects=False)
        response.raise_for_status()
        days=list(dict.fromkeys(re.findall(r'/recent-actions/(\d{8})',response.text)))[:5]
        for day in days:
            # A date-only record becomes eligible the next UTC day, avoiding same-day lookahead.
            published=datetime.strptime(day,'%Y%m%d').replace(tzinfo=timezone.utc)+timedelta(days=1)
            if not now-timedelta(days=90)<=published<=now: continue
            link='https://ofac.treasury.gov/recent-actions/'+day
            text,body_status=article_text(link)
            rows.append(dict(event_id=link,headline='OFAC sanctions action '+day,url=link,
                source='OFAC',available_at=published.isoformat(),body=text,body_status=body_status,
                scope='world',expires_at=(published+timedelta(days=90)).isoformat()))
            count+=1
        status='checked' if days else 'listing_unparsed'
    except (requests.RequestException,ValueError): pass
    coverage.append(dict(source='OFAC',status=status,events=count,checked_at=now.isoformat()))
    return rows,coverage


def exposure(company):
    """Classify disclosed businesses; sector labels alone do not prove customer exposure."""
    text=str(company.get('business_description') or '').lower()
    groups={
        'capital_equipment': ('electrical equipment','power distribution','semiconductor equipment','printed circuit','circuit board','기판','전력기기'),
        'energy_producer': ('oil production','exploration and production','upstream','석유 생산'),
        'fuel_consumer': ('airline','trucking','shipping','항공운송','해운'),
        'credit': ('lending','loans','mortgage','대출'),
        'recurring_software': ('subscription','software-as-a-service','saas','구독'),
        'data_center': ('data center','datacenter','artificial intelligence','데이터센터','ai 가속기'),
        'exporter': ('exports','export sales','수출'),
        'importer': ('imports','imported materials','수입 원자재'),
    }
    return {key for key,words in groups.items() if any(word in text for word in words)}


def direct_company_event(event, company):
    # The ticker's news feed can contain market-wide and unrelated articles.
    text=(event.get('headline','')+' '+event.get('body','')).lower()
    aliases=company.get('entity_aliases',[])
    name=company.get('company_name','')
    simplified=re.sub(r',?\s+(?:Holdings|Corporation|Corp\.?|Inc\.?|Limited|Ltd\.?).*$', '',name,flags=re.I).strip()
    if simplified and len(simplified)>=5: aliases=[*aliases,simplified]
    aliases=[*aliases,name]
    return any(len(alias.strip())>=4 and alias.lower().strip() in text for alias in aliases if isinstance(alias,str))


def _direction(sentence):
    if NEGATION.search(sentence): return 0
    up=any(word in sentence.lower() for word in POSITIVE)
    down=any(word in sentence.lower() for word in NEGATIVE)
    return 1 if up and not down else -1 if down and not up else 0


def _route(channel, event, company, direct):
    profile=exposure(company)
    text=(event.get('headline','')+' '+event.get('body','')).lower()
    if channel=='financing':
        if profile & {'capital_equipment','credit'} or (company.get('net_debt_to_equity') or 0)>0.5:
            return -1,'금리 → 조달비용·고객 투자여력 → 금융비용 또는 수요','다음 1~4분기'
    if channel=='input_cost':
        if 'fuel_consumer' in profile: return -1,'연료·원자재 가격 → 투입원가 → 영업이익률','당분기~다음 2분기'
        if 'energy_producer' in profile: return 1,'판매 상품가격 → 매출단가 → 영업이익','당분기~다음 2분기'
    if channel=='trade_policy':
        if direct: return -1,'관세·규제 → 판매가능 시장·조달비용 → 매출·이익','시행일 이후; 적용국·품목 확인 필요'
    if channel=='currency':
        return None  # Destination sales, invoicing and hedging are needed for the sign.
    if channel in {'demand','capacity'}:
        if direct or ('data_center' in profile and any(w in text for w in ('data center','datacenter','데이터센터'))):
            return 1,'고객 수요·수주/생산능력 → 출하·매출 인식 → 영업이익','다음 1~4분기; 가동·검수 시점 확인 필요'
    if channel=='pricing' and direct: return 1,'판매가격 → 매출단가 → 원가 전가 후 이익률','당분기~다음 2분기'
    return None


def analyze_events(company, events, at, feed_coverage=()):
    assessed=[]; unsupported=[]; seen=set(); positive=[]; negative=[]; uncertain=[]
    for event in events:
        published=timestamp(event.get('available_at'))
        expiry=timestamp(event.get('expires_at'))
        if not published or published>at or at-published>timedelta(days=90) or (expiry and expiry<at): continue
        identity=event.get('event_id') or event.get('url')
        if not identity or identity in seen: continue
        seen.add(identity)
        body=event.get('body','')
        if len(body)<400:
            unsupported.append(dict(url=event.get('url'),reason='본문 미확보: 제목만으로 실적 영향 확정 금지'))
            continue
        direct=direct_company_event(event,company)
        sentences=re.split(r'(?<=[.!?。])\s+|\n+',body)
        matches=[]
        for channel,keywords in CHANNELS.items():
            for sentence in sentences:
                if not any(word in sentence.lower() for word in keywords): continue
                route=_route(channel,event,company,direct)
                if route is None:
                    if channel=='currency' and direct:
                        uncertain.append(dict(event=identity,channel=channel,reason='통화별 매출·원가·헤지 노출 미확보'))
                    continue
                multiplier,path,horizon=route
                direction=_direction(sentence)*multiplier
                confidence=.6 if direct else .3
                # One document can be copied by publishers; one channel per document.
                claim=dict(channel=channel,direction=direction,confidence=confidence,path=path,
                    realization=horizon,relation='direct_named_company' if direct else 'business_exposure_inference',
                    support='rule_based_hypothesis',quantified=False,
                    # Source pointer replaces redistribution of copyrighted paragraphs.
                    sentence_index=sentences.index(sentence),source_url=event.get('url'),
                    uncertainty='수주 취소·경쟁·가격 전가·정책 시행 및 주가 선반영 확인 필요')
                matches.append(claim)
                (positive if direction>0 else negative if direction<0 else uncertain).append(claim)
                break
        assessed.append(dict(event_id=identity,headline=event.get('headline'),url=event.get('url'),
            available_at=published.isoformat(),source=event.get('source'),body_status=event.get('body_status'),
            relevant=bool(matches),channels=matches))
    claims=positive+negative
    # Bound correlated reports: average by channel, then across channels, never sum headlines.
    channel_scores={}
    for claim in claims: channel_scores.setdefault(claim['channel'],[]).append(claim['direction']*claim['confidence'])
    means=[sum(values)/len(values) for values in channel_scores.values()]
    score=sum(means)/len(means) if means else None
    coverage_checked=any(row.get('status')=='checked' for row in feed_coverage)
    quiet=(not claims and not uncertain and not unsupported and coverage_checked
           and bool(assessed) and bool(company.get('business_description'))
           and company.get('news_status')=='no_recent_articles')
    return dict(model='causal_rules_v1_not_calibrated',generated_at=at.isoformat(),
        score=round(score,4) if score is not None else None,
        status='directional_hypotheses' if claims else 'no_material_events_identified' if quiet else 'insufficient_causal_evidence',
        business_profile_available=bool(company.get('business_description')),exposures=sorted(exposure(company)),
        positive_drivers=positive,negative_drivers=negative,uncertainties=uncertain,
        assessed_events=assessed,unread_events=unsupported,feed_coverage=list(feed_coverage),
        quantified_earnings_impact=None,
        limitations=['자동 규칙에 의한 인과 가설이며 사실로 확정된 실적 예측이 아님',
                    '고객별 매출·가격·원가·계약 이행 자료 없이는 영향액 산출 불가',
                    '수집 피드 밖의 지정학·정책 이벤트와 접근 불가 본문은 분석 누락 가능'])


def earnings_bridge(base_revenue, operating_income, variable_cost_share, assumptions):
    """Explicit sensitivity scenario, never an extracted fact or probability.

    Revenue = price * volume; variable cost changes with volume and input cost.
    Fixed costs stay constant unless an explicit fixed-cost change is supplied.
    All change inputs are percentages. No unavailable value defaults to zero.
    """
    required=('volume_change_pct','selling_price_change_pct','unit_cost_change_pct','fixed_cost_change_pct')
    numbers=[base_revenue,operating_income,variable_cost_share,*[assumptions.get(k) for k in required]]
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in numbers):
        return dict(status='missing_assumptions',revenue_change=None,operating_income_change=None)
    if base_revenue<=0 or not 0<=variable_cost_share<=1:
        return dict(status='invalid_base',revenue_change=None,operating_income_change=None)
    volume,price,cost,fixed=(assumptions[k]/100 for k in required)
    if min(volume,price,cost,fixed)<-1:
        return dict(status='invalid_change',revenue_change=None,operating_income_change=None)
    variable=base_revenue*variable_cost_share
    fixed_base=base_revenue-operating_income-variable
    if fixed_base<0:
        return dict(status='invalid_cost_split',revenue_change=None,operating_income_change=None)
    sales=base_revenue*(1+volume)*(1+price)
    profit=sales-variable*(1+volume)*(1+cost)-fixed_base*(1+fixed)
    return dict(status='sensitivity_only',revenue=sales,operating_income=profit,
                revenue_change=sales-base_revenue,operating_income_change=profit-operating_income,
                source_assumptions=assumptions,interpretation='조건부 민감도이며 실제 실적 예측치가 아님')
