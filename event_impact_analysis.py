"""Auditable causal event analysis. Rule inferences are not verified earnings forecasts."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse,parse_qs,urlencode,urlunparse,urljoin
from xml.etree import ElementTree
import math
import re
import requests
from economic_graph import frames as event_frames, connect as connect_business

ALLOWED = ('federalreserve.gov', 'ecb.europa.eu', 'sec.gov', 'reuters.com',
           'finance.yahoo.com', 'cnbc.com', 'hanaw.com', 'samsungpop.com',
           'yna.co.kr', 'dart.fss.or.kr', 'eia.gov', 'ofac.treasury.gov', 'korea.kr', 'motir.go.kr',
           'n.news.naver.com','mofa.go.kr')
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
    try:
        parsed=urlparse(url or '')
        host=(parsed.hostname or '').lower()
        return (parsed.scheme=='https' and not parsed.username and not parsed.password
                and parsed.port in (None,443)
                and any(host==domain or host.endswith('.'+domain) for domain in ALLOWED))
    except (ValueError,AttributeError):
        return False


def canonical_article_url(url):
    parsed=urlparse(url or '')
    if parsed.hostname and (parsed.hostname=='korea.kr' or parsed.hostname.endswith('.korea.kr')):
        news_id=parse_qs(parsed.query).get('newsId')
        if news_id:
            return urlunparse((parsed.scheme,parsed.netloc,parsed.path,'',urlencode({'newsId':news_id[0]}),''))
    return url


class ArticleBody(HTMLParser):
    """Capture semantic article containers, including publishers using div instead of p."""
    def __init__(self):
        super().__init__();self.stack=[];self.parts=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);label=attrs.get('id','')+' '+attrs.get('class','')
        capture=(self.stack[-1][1] if self.stack else False) or tag=='article' or bool(re.search(
            r'(?i)(?:^|[\s_-])(?:view_cont|view_con|view-content|article_body|articleBody|dic_area|newsct_article|article-view-content-div)(?:$|\s)',label))
        skip=(self.stack[-1][2] if self.stack else False) or tag in ('script','style','nav','header','footer')
        if tag=='br' and capture and not skip:self.parts.append('\n')
        if tag not in ('br','img','hr','meta','link','input','source','wbr','area','base','embed','param','track'):
            self.stack.append((tag,capture,skip))
    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:
                if self.stack[index][1] and not self.stack[index][2]:self.parts.append('\n')
                self.stack=self.stack[:index];break
    def handle_data(self,data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:self.parts.append(data)


_BODY_CACHE={}
_ARTICLE_METADATA={}


def article_text(url, redirects=0):
    result=_read_article_text(url,redirects)
    parsed=urlparse(url or '')
    if not result[0] and parsed.hostname=='www.korea.kr':
        alternate=urlunparse((parsed.scheme,'m.korea.kr',parsed.path,'',parsed.query,''))
        result=_read_article_text(alternate,redirects)
        if result[0]:
            canonical=canonical_article_url(url)
            _ARTICLE_METADATA[canonical]=_ARTICLE_METADATA.get(canonical_article_url(alternate),{})
            _BODY_CACHE[canonical]=(datetime.now(timezone.utc)+timedelta(minutes=5),result)
    return result


def _read_article_text(url, redirects=0):
    """Read public allowed publishers; no redirect to arbitrary hosts or paywall bypass."""
    url=canonical_article_url(url)
    if not allowed_url(url): return '', 'publisher_not_supported'
    cached=_BODY_CACHE.get(url)
    if cached and cached[0]>datetime.now(timezone.utc):return cached[1]
    try:
        response=requests.get(url,headers=HEADERS,timeout=10 if any(host in url for host in ('korea.kr/','mofa.go.kr/')) else 5,allow_redirects=False,stream=True)
        try:
            response.raise_for_status()
            if response.status_code in (301,302,303,307,308) and redirects<2:
                destination=urljoin(url,response.headers.get('Location',''))
                if destination!=url and allowed_url(destination):return article_text(destination,redirects+1)
            if response.status_code != 200: return '', 'redirect_or_unavailable'
            if 'html' not in response.headers.get('Content-Type','').lower(): return '', 'not_html'
            body=bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body)>600000: return '', 'body_too_large'
            encoding=response.encoding
            if not encoding or encoding.lower()=='iso-8859-1':encoding='utf-8'
            document=bytes(body).decode(encoding,errors='replace')
            from event_publication import publication_metadata
            _ARTICLE_METADATA[url]=publication_metadata(document,url,datetime.now(timezone.utc))
            parser=ArticleBody();parser.feed(document)
            if len(''.join(parser.parts))<400:
                parser=Paragraphs();parser.feed(document)
            text=re.sub(r'[ \t]+',' ',''.join(parser.parts)).strip()
            if len(text)<400: return '', 'body_insufficient'
            if any(marker in text[:1200].lower() for marker in ('verify you are human','access denied','subscribe to continue')):
                return '', 'access_restricted'
            result=(text,'public_body')
            _BODY_CACHE[url]=(datetime.now(timezone.utc)+timedelta(minutes=5),result)
            return result
        finally:
            response.close()
    except (requests.RequestException,ValueError): return '', 'download_failed'


def _fetch_world_events(now):
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
    korean_events,korean_coverage=korean_policy_events(now)
    rows.extend(korean_events); coverage.append(korean_coverage)
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


def issuer_name_in_text(text, alias):
    if not isinstance(alias,str):return False
    alias=alias.strip()
    if len(alias)<3:return False
    if re.search(r'[가-힣]',alias):
        return bool(re.search(r'(?<![가-힣a-zA-Z0-9])'+re.escape(alias)+
            r'(?:(?![가-힣a-zA-Z0-9])|(?:은|는|이|가|의|을|를|와|과|도|에서|으로|로)(?![가-힣a-zA-Z0-9]))',text,re.I))
    return bool(re.search(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',text,re.I))


def direct_company_event(event, company):
    # The ticker's news feed can contain market-wide and unrelated articles.
    text=(event.get('headline','')+' '+event.get('body','')).lower()
    aliases=company.get('entity_aliases',[])
    name=company.get('company_name','')
    simplified=re.sub(r',?\s+(?:Holdings|Corporation|Corp\.?|Inc\.?|Limited|Ltd\.?).*$', '',name,flags=re.I).strip()
    if simplified and len(simplified)>=5: aliases=[*aliases,simplified]
    aliases=[*aliases,name]
    return any(issuer_name_in_text(text,alias) for alias in aliases)


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
    graph = connect_business(company, event_frames(events, at), at)
    uncertain.extend(graph.get('expectation_links', []))
    for link in graph['links']:
        (positive if link['direction']>0 else negative if link['direction']<0 else uncertain).append(link)
    for event in events:
        published=timestamp(event.get('available_at'))
        expiry=timestamp(event.get('expires_at'))
        if not published or published>at or at-published>timedelta(days=90) or (expiry and expiry<at): continue
        identity=event.get('event_id') or event.get('url')
        if not identity or identity in seen: continue
        seen.add(identity)
        body=event.get('body','')
        if len(body)<400:
            unsupported.append(dict(url=event.get('url'),required=direct_company_event(event,company),
                body_status=event.get('body_status'),
                reason='본문 미확보: 제목만으로 실적 영향 확정 금지'))
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
        economic_graph=graph,
        positive_drivers=positive,negative_drivers=negative,uncertainties=uncertain,
        assessed_events=assessed,unread_events=unsupported,feed_coverage=list(feed_coverage),
        fx_sensitivity=currency_sensitivity(company, {
            code: 1. for code in set((company.get('currency_exposure') or {}).get('revenue_shares', {}))
            | set((company.get('currency_exposure') or {}).get('operating_cost_shares', {}))
            if code != company.get('financial_currency')},at),
        earnings_sensitivity=earnings_bridge(company.get('revenue_amount'),company.get('operating_income_amount'),
            company.get('variable_cost_share'),company.get('earnings_scenario_assumptions') or {}),
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


def currency_sensitivity(company, changes, at):
    """Translation sensitivity to FX against the reporting currency.

    Positive change means one foreign currency unit buys more reporting currency.
    Operating revenue and operating costs are modeled separately; financial debt FX
    and hedge P&L are separate from operating income. Shares must come from a dated
    source, and missing hedge information must not be treated as zero hedging.
    """
    data=company.get('currency_exposure') or {}
    available=timestamp(data.get('available_at'))
    sales=data.get('revenue_shares'); costs=data.get('operating_cost_shares')
    revenue=company.get('revenue_amount'); income=company.get('operating_income_amount')
    missing=[]
    if not data.get('source_url') or not available or available>at: missing.append('dated_exposure_source')
    if not company.get('financial_currency'): missing.append('reporting_currency')
    if str(data.get('period',''))[:7] != str(company.get('quarter',''))[:7]: missing.append('matching_financial_period')
    for key,shares in [('revenue_shares',sales),('operating_cost_shares',costs)]:
        if (not isinstance(shares,dict) or not shares
                or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in shares.values())
                or sum(shares.values())>1.000001): missing.append(key)
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (revenue,income)):
        missing.append('financial_base')
    elif revenue<=0 or income>revenue: missing.append('financial_base')
    if missing: return dict(status='missing_currency_exposure',missing=missing,operating_income_change=None)
    currencies=(set(sales)|set(costs))-{company.get('financial_currency')}
    if any(code not in changes or isinstance(changes[code],bool) or not isinstance(changes[code],(int,float))
           or not math.isfinite(changes[code]) or changes[code]<-100 for code in currencies):
        return dict(status='missing_fx_scenario',missing=sorted(currencies-set(changes)),operating_income_change=None)
    sales_effect=revenue*sum(sales.get(code,0)*changes[code]/100 for code in currencies)
    cost_effect=(revenue-income)*sum(costs.get(code,0)*changes[code]/100 for code in currencies)
    operating_effect=sales_effect-cost_effect
    result=dict(status='conditional_fx_sensitivity',reporting_currency=company.get('financial_currency'),
        foreign_currency_change_pct=changes,revenue_change=sales_effect,operating_cost_change=cost_effect,
        unhedged_operating_income_change=operating_effect,operating_income_change=None,
        source_url=data['source_url'],period=data['period'],hedge_status='unavailable',
        debt_fx_effect='not modeled: debt currency, amount and maturity needed',
        interpretation='환율 가정에 따른 민감도이며 환율·실적 예측치가 아님')
    hedge=data.get('net_operating_hedge_coverage')
    if isinstance(hedge,(int,float)) and not isinstance(hedge,bool) and math.isfinite(hedge) and 0<=hedge<=1:
        result.update(after_hedge_economic_effect=operating_effect*(1-hedge),hedge_status='scenario_coverage_available',
                      hedge_accounting='경제적 노출 상쇄치; 헤지손익의 손익계산서 분류는 별도 확인 필요')
    return result


def korean_policy_events(now):
    """Read current official policy links; first-seen time prevents hindsight claims."""
    from urllib.parse import urljoin
    class Links(HTMLParser):
        def __init__(self): super().__init__(); self.href=None; self.text=[]; self.rows=[]
        def handle_starttag(self,tag,attrs):
            if tag=='a': self.href=dict(attrs).get('href'); self.text=[]
        def handle_data(self,text):
            if self.href: self.text.append(text)
        def handle_endtag(self,tag):
            if tag=='a' and self.href:
                self.rows.append((self.href,' '.join(self.text).strip())); self.href=None
    rows=[]; status='unavailable'
    try:
        response=requests.get('https://www.korea.kr/main.do',headers=HEADERS,timeout=5,allow_redirects=False)
        response.raise_for_status()
        parser=Links(); parser.feed(response.text)
        # The homepage alone drops material news quickly; include the latest policy and briefing lists.
        for listing in ('https://www.korea.kr/news/policyNewsList.do',
                        'https://www.korea.kr/briefing/briefingHomeList.do',
                        'https://www.mofa.go.kr/www/brd/m_4080/list.do'):
            try:
                page=requests.get(listing,headers=HEADERS,timeout=5,allow_redirects=False)
                if page.status_code==200:
                    extra=Links();extra.feed(page.text)
                    parser.rows.extend((urljoin(listing,href),title) for href,title in extra.rows)
            except requests.RequestException: pass
        links=[]
        for href,title in parser.rows:
            url=urljoin('https://www.korea.kr/',href)
            if any(path in url for path in ('policyNewsView.do?','pressReleaseView.do?','presidentView.do?','policyBriefingView.do?','/www/brd/m_4080/view.do')) and allowed_url(url):
                if re.search(r'FTA|정상|관세|금리|수출|콘텐츠|제재|협상|에너지|반도체|문화교류|문화 교류|한한령|공연|음악|게임|외교장관|일정|예정|사전',title,re.I):
                    links.append((canonical_article_url(url),title))
        unique={}
        for url,title in links:unique.setdefault(url,title)
        for url,title in list(unique.items())[:16]:
            text,body_status=article_text(url)
            metadata=_ARTICLE_METADATA.get(url,dict(available_at=datetime.now(now.tzinfo).isoformat(),
                publication_verified=False,availability_basis='first_seen_not_original_publication'))
            rows.append(dict(event_id=url,url=url,headline=title,source='외교부' if 'mofa.go.kr/' in url else '대한민국 정책브리핑',
                **metadata,
                body=text,body_status=body_status,scope='world'))
        status='checked' if links else 'listing_unparsed_or_no_matching_links'
    except (requests.RequestException,ValueError): pass
    return rows,dict(source='대한민국 정책브리핑',status=status,events=len(rows),checked_at=now.isoformat())

_WORLD_CACHE={}
def world_events(now):
    """Reuse one policy snapshot across discovery and company evaluation in a run."""
    key=int(now.timestamp())//300
    if key not in _WORLD_CACHE:
        _WORLD_CACHE.clear()
        from policy_event_index import prime_public_policy_cache
        prime_public_policy_cache(_BODY_CACHE,_ARTICLE_METADATA,now)
        events,coverage=_fetch_world_events(now)
        from policy_event_index import merge_with_history
        _WORLD_CACHE[key]=(merge_with_history(events,now),coverage)
    return _WORLD_CACHE[key]
