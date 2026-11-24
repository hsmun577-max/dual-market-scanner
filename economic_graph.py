"""Shared event -> product/region -> business exposure graph, independent of ticker."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

from event_publication import MIN_VERIFIED_BODY_CHARS

PRODUCTS = {
 'screen_content': ('film','movie','drama','animation','broadcasting','영화','드라마','애니메이션','방송'),
 'games': ('video game','online game','온라인게임','게임'),
 'recorded_music': ('recorded music','music publishing','music production','music','음반','음원'),
 'live_entertainment': ('concert','live entertainment','live performance','콘서트','공연'),
 'semiconductors': ('semiconductor','반도체'),
 'circuit_boards': ('printed circuit','circuit board','기판','pcb','mlb'),
 'power_equipment': ('electrical equipment','power distribution','전력기기','변압기','배전'),
 'oil_gas': ('oil production','crude oil','natural gas','원유','천연가스'),
 'transport': ('airline','shipping','trucking','항공운송','해운','운송'),
 'tourism': ('tourism','travel agency','tour operator','관광','여행사'),
 'software': ('software','소프트웨어'),
}
COUNTRIES = {'South Korea': ('south korea','korean','한국','한중','한·중','한-중'),
             'China': ('china','chinese','중국','한중','한·중','한-중'),
             'United States': ('united states','u.s.','미국'), 'Japan': ('japan','japanese','일본'),
             'Europe': ('europe','european','유럽')}
ACCESS_UP = ('market opening','market access expanded','restriction lifted','most-favoured',
             'most-favored','최혜국','시장 개방','시장개방','비차별','규제 완화','규제완화')
ACCESS_DOWN = ('market access restricted','export ban','수출 금지','수출금지','시장 폐쇄','시장폐쇄')
UNCERTAIN = ('rumor','unconfirmed','검토 중','검토중','미확정','not','no','아니다','않는다')


def stamp(value):
    try:
        at=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return at if at.utcoffset() is not None else None
    except ValueError: return None


def contains(text, words):
    # ASCII words require boundaries: 'gas' must not match an unrelated name.
    text=text.casefold()
    return any((re.search(r'(?<![a-z])'+re.escape(word.casefold())+r'(?:s|es)?(?![a-z])',text)
                if word.isascii() else word in text) for word in words)


def profile(company, at):
    text=' '.join(str(company.get(k) or '') for k in ('business_description','industry'))
    products={key for key,words in PRODUCTS.items() if contains(text,words)}
    # A product mentioned as a customer's use is not the issuer's content business.
    industry=str(company.get('industry') or '')
    sector=company.get('sector')
    if sector and industry and sector!='Communication Services' and not contains(industry,
            ('entertainment','electronic gaming','broadcasting','media','publishing')):
        products -= {'screen_content','games','recorded_music','live_entertainment'}
    countries={key for key,words in COUNTRIES.items() if contains(text,words)}
    home=company.get('country')
    if home: countries.add(home)
    observed=stamp(company.get('profile_available_at') or company.get('available_at') or company.get('checked_at'))
    usable=bool(text.strip()) and observed is not None and observed<=at and at-observed<=timedelta(days=180)
    links=[]
    for link in company.get('business_relationships',[]):
        available=stamp(link.get('available_at'))
        if (link.get('role') in {'customer','supplier'} and link.get('entity') and link.get('source_url')
                and available and available<=at and at-available<=timedelta(days=180)):
            links.append(link)
    return dict(products=sorted(products),countries=sorted(countries),relationships=links,
        home_country=home,usable=usable,source=company.get('profile_source','Yahoo Finance issuer profile'),
        observed_at=observed.isoformat() if observed else None,
        confidence='inferred_from_business_description',revenue_weights=None)


def frames(events, at):
    output=[]; seen=set()
    for event in events:
        published=stamp(event.get('available_at')); expires=stamp(event.get('expires_at'))
        identity=event.get('event_id') or event.get('url')
        if (not identity or identity in seen or not published or not published<=at
                or at-published>timedelta(days=90) or (expires and expires<at)):
            continue
        seen.add(identity)
        body=event.get('body','')
        if len(body)<MIN_VERIFIED_BODY_CHARS: continue
        # Public diplomatic preparations can identify exposed businesses before an outcome or price surge.
        if (event.get('publication_verified') is True
                and contains(body,COUNTRIES['China']) and contains(body,COUNTRIES['South Korea'])
                and contains(body,('다음 주','다음주','예정','ahead of','upcoming'))
                and contains(body,('정상회담','정상회의','summit'))
                and contains(body,('인적교류','문화','서비스','콘텐츠','cultural','services'))
                and contains(body,('협상','협력','교류','cooperation','negotiation'))):
            local=published.astimezone(ZoneInfo('Asia/Seoul'))
            anchor=local.date()
            if event.get('source_published_date'):
                try:anchor=datetime.fromisoformat(event['source_published_date']).date()
                except ValueError:pass
            if contains(body,('다음 주','다음주')):
                first=anchor+timedelta(days=7-anchor.weekday())
                last=first+timedelta(days=6)
            else:
                first=anchor;last=first+timedelta(days=7)
            output.append(dict(event_id=identity,kind='pre_event_policy_watch',
                products=['screen_content','games','recorded_music','live_entertainment','tourism','software','transport'],
                regions=['China','South Korea'],beneficiary_regions=['South Korea'],direction=0,
                status='preparation_not_outcome',available_at=published.isoformat(),source_url=event.get('url'),
                publication_verified=True,availability_basis=event.get('availability_basis'),
                event_window_start=first.isoformat(),event_window_end=last.isoformat(),
                realization='예정된 협의의 결과는 미확정; 해당 기업의 수혜 가능성 사전 검토'))
        for index,sentence in enumerate(re.split(r'(?<=[.!?。])\s+|\n+',body)):
            if (contains(sentence,('문화교류','문화 교류','한한령','공연 재개'))
                    and contains(sentence,('강화','추진','확대','재개','모색'))
                    and not contains(sentence,UNCERTAIN)
                    and contains(sentence,COUNTRIES['China'])
                    and contains(sentence,COUNTRIES['South Korea'])):
                output.append(dict(event_id=identity,kind='cultural_cooperation_expectation',
                    products=['screen_content','games','recorded_music','live_entertainment'],
                    regions=['China','South Korea'],beneficiary_regions=['South Korea'],direction=0,
                    status='expectation_only',available_at=published.isoformat(),source_url=event.get('url'),
                    publication_verified=event.get('publication_verified',False),
                    availability_basis=event.get('availability_basis'),
                    sentence_index=index,realization='시장 기대 관찰; 공연 허가·계약·매출 미확정'))
            up=contains(sentence,ACCESS_UP); down=contains(sentence,ACCESS_DOWN)
            if not up and not down: continue
            # Scope is taken from the same sentence, not from unrelated page text.
            products={key for key,words in PRODUCTS.items() if contains(sentence,words)}
            regions={key for key,words in COUNTRIES.items() if contains(sentence,words)}
            beneficiaries=set()
            scope_words=[word for product in products for word in PRODUCTS[product]]
            for country,aliases in COUNTRIES.items():
                if any(re.search(re.escape(alias)+r'(?:의|산)?\s+'+re.escape(word)+r'(?:s|es)?',sentence,re.I)
                       or (not alias.isascii() and re.search(re.escape(alias)+r'(?:의|산)?\s*'+re.escape(word),sentence))
                       for alias in aliases for word in scope_words):
                    beneficiaries.add(country)
            certainty='unconfirmed' if contains(sentence,UNCERTAIN) else 'announced_policy'
            direction=1 if up and not down else -1 if down and not up else 0
            if certainty=='unconfirmed': direction=0
            output.append(dict(event_id=identity,kind='market_access',products=sorted(products),
                regions=sorted(regions),beneficiary_regions=sorted(beneficiaries),direction=direction,status=certainty,
                available_at=published.isoformat(),source_url=event.get('url'),sentence_index=index,
                publication_verified=event.get('publication_verified',False),
                availability_basis=event.get('availability_basis'),
                realization='시행·심사·계약 이행 이후; 매출 인식 시점 미확정'))
    return output


def connect(company, event_frames, at):
    business=profile(company,at)
    if not business['usable']: return dict(profile=business,links=[],status='missing_or_stale_profile')
    links=[]; expectations=[]
    for event in event_frames:
        observed=stamp(event.get('available_at'))
        if not observed or observed>at: continue
        if event.get('beneficiary_regions') and business['home_country'] not in event['beneficiary_regions']:
            continue
        products=set(business['products']) & set(event.get('products',[]))
        regions=set(business['countries']) & set(event.get('regions',[]))
        content={'screen_content','games','recorded_music','live_entertainment'}
        if event.get('kind') in {'cultural_cooperation_expectation','pre_event_policy_watch'}:
            if event.get('event_window_end') and at.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()>event['event_window_end']:
                continue
            if products and regions:
                expectations.append(dict(event_id=event['event_id'],source_url=event['source_url'],
                    available_at=event.get('available_at'),publication_verified=event.get('publication_verified',False),
                    availability_basis=event.get('availability_basis'),
                    channel='market_expectations',direction=0,
                    watch_bias='pre_event_business_exposure' if event['kind']=='pre_event_policy_watch' else 'positive_policy_sympathy',
                    stage='pre_event' if event['kind']=='pre_event_policy_watch' else 'announced_expectation',
                    event_window_start=event.get('event_window_start'),event_window_end=event.get('event_window_end'),
                    matched_regions=sorted(regions),confidence=.1,quantified=False,
                    path='문화교류 논의 → 콘텐츠 업종 기대 변화 → 수급·가격 반응 관찰',
                    uncertainty='공연 허가·계약·매출 증가 확정 아님'))
            continue
        if (regions and not products and set(business['products']) & content
                and set(event.get('products',[])) & content and event.get('direction',0)>0):
            expectations.append(dict(event_id=event['event_id'],source_url=event['source_url'],
                available_at=event.get('available_at'),publication_verified=event.get('publication_verified',False),
                availability_basis=event.get('availability_basis'),
                channel='market_expectations',direction=0,watch_bias='positive_policy_sympathy',
                matched_regions=sorted(regions),confidence=.1,quantified=False,
                path='인접 콘텐츠 업종의 정책 변화 → 시장의 개방 기대 확산 → 밸류에이션 반응 관찰',
                uncertainty='동종 업종 기대 확산 가설; 해당 상품 허가나 매출 증가를 뜻하지 않음'))
        if not products or not regions: continue
        links.append(dict(**event,matched_products=sorted(products),matched_regions=sorted(regions),
            channel='market_access',confidence=.25,
            path='정책상 시장 접근 변화 → 해당 사업 판매·라이선스 기회 → 계약·출하 후 매출 → 비용 차감 후 이익',
            quantified=False,support='scoped_business_exposure_hypothesis',
            uncertainty='개방 대상·국적·허가·시행일·지역별 매출 비중 확인 필요; 전면 허용으로 확대 해석 금지'))
    return dict(profile=business,links=links,expectation_links=expectations,
                status='linked_hypotheses' if links else 'expectation_watch_only' if expectations else 'no_scoped_link_identified')


def discover(registry, events, at, limit=12):
    event_frames=frames(events,at)
    ranked=[]
    for symbol,company in registry.items():
        if not isinstance(company,dict): continue
        result=connect(company,event_frames,at)
        positive=[link for link in result['links'] if link['direction']>0]
        if positive: ranked.append((symbol,len({(l['event_id'],p) for l in positive for p in l['matched_products']})))
        elif result.get('expectation_links'): ranked.append((symbol,.1))
    ordered=[symbol for symbol,_ in sorted(ranked,key=lambda item:(-item[1],item[0]))]
    return ordered if limit is None else ordered[:limit]


def load_profiles(path='reports/company-profiles.json'):
    import json
    from pathlib import Path
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError): return {}


def save_profiles(companies, path='reports/company-profiles.json'):
    import json
    from pathlib import Path
    registry=load_profiles(path)
    keys=('company_name','business_description','industry','country','profile_available_at',
          'profile_source','business_relationships','available_at','checked_at','source_symbol','exchange','display_name','sector')
    for symbol,company in companies.items():
        if company.get('business_description'):
            target=company.get('source_symbol') or symbol
            old=registry.get(target,{})
            old=old if isinstance(old,dict) else {}
            registry[target]={**{key:old[key] for key in keys if key in old},
                              **{key:company[key] for key in keys if key in company}}
    file=Path(path); file.parent.mkdir(parents=True,exist_ok=True)
    temporary=file.with_suffix('.tmp')
    temporary.write_text(json.dumps(registry,ensure_ascii=False),encoding='utf-8')
    temporary.replace(file)


def select_analysis_candidates(pool, market, event_symbols=(), limit=48, entry_slots=12, pinned=()):
    """Reserve research capacity for event links, then for the names the entries could use.

    On a scan of 13,000 instruments the event and mega-cap rows used to take the whole
    analysis budget, and the names that actually passed the entry filters were never
    looked at — so the only candidates an entry plan could be built for were dropped
    without a score. The entry-filtered names therefore come first, then event research,
    then the liquidity anchors that keep the report's context.

    ``pinned`` rows are the market's most traded funds, which the selection reserves a
    slot for before any other claim on the budget: a fund that the momentum screen does
    not rank still has to be read before it can be refused with a reason.
    """
    from candidate_selection import (rank_market_candidates,rank_recovery_candidates,
                                      liquidity_rejection_reason,average_turnover)
    from advance_policy import eligible_for_advance_research
    from catalyst_track import watch_candidates as catalyst_watch_candidates
    from rebound_track import watch_candidates as rebound_watch_candidates
    from early_entry_track import watch_candidates as early_entry_watch_candidates
    event_set=set(event_symbols)
    eligible=[row for row in pool if liquidity_rejection_reason(row,market) is None
        or (row['symbol'] in event_set and eligible_for_advance_research(row,market))]
    indexed={row['symbol']:row for row in eligible}
    event_rows=sorted((indexed[s] for s in set(event_symbols) if s in indexed),
                        key=lambda row:(-average_turnover(row['indicators']),row['symbol']))[:max(1,2*limit//3)]
    trend=rank_market_candidates(eligible,market,limit=max(5,entry_slots))
    recovery=rank_recovery_candidates(eligible,market,limit=max(5,entry_slots))
    # Names the first screen rejected as a rebound or an unconfirmed trend are researched
    # too: only after a company's disclosure is read can a confirmed catalyst lift them,
    # and that read never happens for a row that was never selected.
    catalyst=catalyst_watch_candidates(eligible,market,limit=max(2,limit//8))
    # A bottom or oversold reversal qualifies on its own candle, which the momentum screen
    # refuses; without a slot it would never be analysed and never planned.
    rebound=rebound_watch_candidates(eligible,market,limit=max(2,limit//8))
    # The short-term early-entry track reads an alignment the momentum screen refuses; its
    # qualifier (measured inflow or a 61+ fundamental score) is only known after analysis.
    early_entry=early_entry_watch_candidates(eligible,market,limit=max(2,limit//8))
    neutral=sorted(eligible,key=lambda row:(-average_turnover(row['indicators']),row['symbol']))[:8]
    pinned_symbols=[symbol for symbol in pinned if symbol in indexed]
    pinned_rows=[indexed[symbol] for symbol in pinned_symbols]
    selected=[]; seen=set(); entries=set(); catalysts=set(); rebounds=set()
    from itertools import zip_longest
    entry=[row for group in zip_longest(trend,recovery) for row in group if row is not None]
    for row in [*pinned_rows,*entry,*event_rows,*catalyst,*rebound,*early_entry,*neutral]:
        if row['symbol'] not in seen:
            selected.append(row); seen.add(row['symbol'])
    entries={row['symbol'] for row in entry}; catalysts={row['symbol'] for row in catalyst}
    rebounds={row['symbol'] for row in rebound}; pinned_set=set(pinned_symbols)
    early_entries={row['symbol'] for row in early_entry}
    for row in selected[:limit]:
        # Only a name that arrived through its own budget is labelled with it; a reversal
        # setup is the more specific reason when both apply.
        if row['symbol'] in rebounds and row['symbol'] not in entries:
            row['selection_reason']='rebound_watch'
        elif row['symbol'] in early_entries and row['symbol'] not in entries:
            row['selection_reason']='early_entry_watch'
        elif row['symbol'] in catalysts and row['symbol'] not in entries:
            row['selection_reason']='catalyst_watch'
        elif row['symbol'] in pinned_set and row['symbol'] not in entries:
            row['selection_reason']='fund_candidate'
    return selected[:limit]
