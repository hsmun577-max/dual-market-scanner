"""당일 급등했지만 매수 후보에서 빠진 종목의 '긴급 이슈 참고' 블록.

하락장 게이트(`market_headwind`)·과열(`extended_price`)·이탈/반등 구간
(`breakdown_or_rebound`)처럼 매수 규칙이 일부러 막은 종목이라도, 실제 뉴스·공시로
설명되는 대형 급등은 참고용으로 따로 보여 준다. 이 모듈은 읽기 전용이다:

- `buy_evaluation`의 어떤 카운트도 바꾸지 않고, `preopen_signal`을 만들지 않는다.
- 뉴스/공시 헤드라인이 그 종목을 실제로 언급한 경우에만 올린다(잡주 급등은 올리지 않는다).
- 가격 오류(상장폐지·극저가·한국 가격제한 초과·PARA +9614% 같은 값)는 버린다.

텔레그램에서는 매수 후보(`조건부 매수`/`반등 후보`)와 섞이지 않는 별도 블록으로 나가고,
머리줄에 매수 신호가 아님을 명시한다.
"""
import math
import re
from datetime import datetime, timedelta, timezone

from candidate_selection import (MIN_AVERAGE_TURNOVER, MIN_PRICE, average_turnover,
                                 instrument_rejection_reason)
from research_asset import is_fund

HEADING = '🚨 긴급 이슈 참고 (매수 신호 아님 · 게이트 보류)'
SUBTITLE = '게이트에 막혀 매수 후보에서 빠진 당일 급등 · 뉴스로 확인된 종목만'
# 당일 급등 기준. 8%로 두면 잡주 급등이 섞여서, 실제 대형 이슈 급등만 남도록 10%로 잡았다.
MIN_CHANGE_PCT = 10.0
# 이보다 큰 '하루 등락'은 뉴스가 아니라 가격 데이터 오류다(예: PARA +9,614%).
MAX_CHANGE_PCT = 150.0
# 한국 거래소 일일 가격제한. 이를 넘는 값은 시세 오류로 본다(신규 상장 첫날 포함).
KR_PRICE_LIMIT_PCT = 30.0
# 급등 당일 실제로 돈이 오갔는지: 당일 거래대금 또는 20일 평균 거래대금.
MIN_TODAY_VALUE = {'KR': 5_000_000_000, 'US': 10_000_000}
MAX_ISSUES_PER_MARKET = 3
NEWS_WINDOW_HOURS = 36
MARKET_LABELS = {'KR': '국내', 'US': '미국'}
MARKET_ORDER = ('KR', 'US')
# 레버리지·인버스·ETF는 회사 이슈가 아니라 상품이다. 급등해도 '긴급 이슈'로 올리지 않는다.
DERIVATIVE_PRODUCT = re.compile(
    r'\d\s*[xX]\b|인버스|레버리지|\b(?:inverse|leveraged|ultra|bull|bear)\b|\bETF\b|\bETN\b', re.I)
# 급등을 설명하는 헤드라인만 올린다. 단순히 종목명이 언급된 분석·광고 기사
# ("Is Micron Stock a Buy at $700?")는 급등의 이유가 아니므로 뉴스 확인으로 치지 않는다.
CATALYST_PATTERNS = (
    ('ceo', re.compile(r'\bCEO\b|chief executive|reinstate|reinstatement|return(?:s|ed|ing)? to|'
                       r'step(?:s|ped|ping)? down|resign|resignation|복귀|교체|선임|사임', re.I)),
    ('policy', re.compile(r'Trump|White House|export control|tariff|sanction|investigation|probe|'
                          r'executive order|백악관|트럼프|관세|수출통제|조사', re.I)),
    ('deal', re.compile(r'acquisition|acquire|merger|contract|order|deal|partnership|stake|'
                        r'수주|공급 계약|계약|합병|인수', re.I)),
    ('earnings', re.compile(r'earnings|guidance|beats?\b|misses?\b|outlook|surprise|'
                            r'실적|가이던스|어닝', re.I)),
)
SURGE_PHRASE = re.compile(
    r'surge|jump|soar|rally|stock is up|gains?\b|rises?\b|급등|불기둥|신고가|최고가|상한가', re.I)
CATALYST_LABELS = {'ceo': 'CEO·경영진', 'policy': '정책·규제', 'deal': '수주·M&A', 'earnings': '실적'}

REASON_LABELS = {
    'market_headwind': '시장 역풍',
    'extended_price': '과열',
    'breakdown': '이탈 구간',
    'breakdown_or_rebound': '이탈/반등 구간',
    'unconfirmed_uptrend': '상승 추세 미확인',
    'weak_relative_trend': '상대 추세 약함',
    'unconfirmed_demand': '거래량 미확인',
    'insufficient_demand_expansion': '거래량 확장 부족',
    'insufficient_liquidity': '유동성 미달',
    'low_price': '극저가주',
    'special_security': '특수 증권',
    'composite_score_below_policy': '종합점수 미달',
    'catalyst_score_below_policy': '호재 점수 미달',
    'insufficient_expected_reward': '기대수익 부족',
    'analysis_incomplete': '분석 자료 부족',
    'analysis_stale_or_missing': '분석 미완료',
    'price_session_mismatch': '가격 기준일 불일치',
    'invalid_average_price': '이동평균 미확인',
    'not_evaluated': '매수 후보 미포함',
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _clean(text, limit=96):
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'


def _result_index(report):
    index = {}
    for row in (report or {}).get('results') or []:
        symbol = row.get('symbol')
        if symbol and symbol not in index:
            index[symbol] = row
    return index


def _aliases(row, name=None, alias=None):
    """헤드라인이 이 종목을 가리키는지 확인할 이름 후보(상장명·별칭·영문사명·티커)."""
    company = row.get('company_analysis') or {}
    names = [name, alias, row.get('name'), company.get('company_name'),
             *(company.get('entity_aliases') or [])]
    for candidate in list(names):
        if isinstance(candidate, str) and candidate.strip():
            simplified = re.sub(r',?\s+(?:Holdings|Corporation|Corp\.?|Inc\.?|Limited|Ltd\.?|Co\.?).*$',
                                '', candidate, flags=re.I).strip()
            if simplified and simplified != candidate:
                names.append(simplified)
    symbol = str(row.get('symbol') or '')
    names.append(symbol.split('.')[0])
    aliases = []
    for candidate in names:
        candidate = str(candidate or '').strip()
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    return aliases


def _mentions(text, row, name=None, alias=None):
    from event_impact_analysis import issuer_name_in_text, issuer_title_subject
    subject = issuer_title_subject(text)
    return any(issuer_name_in_text(subject, candidate) for candidate in _aliases(row, name, alias))


def _articles(row, now):
    """종목 뉴스와 공시 헤드라인. 공시는 제목이 있는 것만 쓴다."""
    company = row.get('company_analysis') or {}
    rows = list(company.get('news') or [])
    for filing in company.get('filings') or []:
        title = filing.get('headline') or filing.get('title') or filing.get('report_name')
        if title:
            rows.append(dict(headline=title,
                             available_at=filing.get('available_at') or filing.get('filing_date')
                             or filing.get('date')))
    return rows


def _recent(article, now):
    stamp = article.get('available_at') or article.get('published_at')
    if not stamp:
        # A headline the collector already accepted has no separable date to check.
        return True
    text = str(stamp).strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            return datetime.fromisoformat(text).date() >= (now - timedelta(days=3)).date()
        published = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return True
    if published.utcoffset() is None:
        return True
    return now - timedelta(hours=NEWS_WINDOW_HOURS) <= published <= now + timedelta(minutes=10)


def catalyst_categories(headline):
    """헤드라인이 어떤 종류의 촉매인지(CEO·정책·수주/인수·실적)."""
    text = str(headline or '')
    return [name for name, pattern in CATALYST_PATTERNS if pattern.search(text)]


def explains_surge(headline):
    """급등을 설명하는 뉴스인지. 종목명만 스친 기사는 이유가 아니다."""
    return bool(catalyst_categories(headline)) or bool(SURGE_PHRASE.search(str(headline or '')))


def _headline(row, now, name=None, alias=None, theme_issue=None):
    """그 종목을 실제로 언급한 헤드라인 하나. 없으면 None(뉴스 미확인 종목은 올리지 않는다)."""
    if theme_issue and _mentions(theme_issue, row, name, alias) and explains_surge(theme_issue):
        return _clean(theme_issue)
    candidates = [article for article in _articles(row, now)
                  if _recent(article, now) and _mentions(article.get('headline'), row, name, alias)
                  and explains_surge(article.get('headline'))]
    if not candidates:
        return None
    candidates.sort(key=lambda article: (-len(catalyst_categories(article.get('headline'))),
                                         str(article.get('available_at') or '')), reverse=True)
    return _clean(candidates[0].get('headline'))


def _change_pct(row):
    value = _number((row.get('indicators') or {}).get('return_1d'))
    return None if value is None else value * 100


def _is_buy_signal(row):
    """이미 매수 후보 섹션에 나가는 종목은 긴급 참고에 다시 넣지 않는다."""
    signal = row.get('preopen_signal') or {}
    if signal.get('status') == 'conditional_buy' or signal.get('track') == 'rebound':
        return True
    if (row.get('advance_signal') or {}).get('status') == 'advance_candidate':
        return True
    confirmed = row.get('signal') or {}
    return confirmed.get('confirmed') is True and confirmed.get('entry_limit') is not None


def _price_rejection(row, market, change_pct, check_consistency=True, require_price=True):
    """가격 오류·상품 구조·유동성으로 급등 알림에 올릴 수 없는 종목을 걸러 낸다.

    ``require_price=False``는 테마 대표주처럼 스캔 행이 없어 가격 자료가 없는 경우다.
    이름·상품 구조·비정상 등락은 그대로 검사하고, 가격 기반 검사만 건너뛴다.
    """
    values = row.get('indicators') or {}
    name = row.get('name') or ''
    if is_fund(row):
        return 'fund_product'
    reason = instrument_rejection_reason(row.get('symbol'), name, row.get('asset_type') or '')
    if reason:
        return reason
    if DERIVATIVE_PRODUCT.search(name):
        return 'derivative_product'
    if market == 'KR' and change_pct > KR_PRICE_LIMIT_PCT + 0.5:
        return 'abnormal_price'
    price = _number(values.get('price'))
    if price is None:
        if require_price:
            return 'missing_price'
    elif price <= 0:
        return 'abnormal_price'
    elif price < MIN_PRICE[market]:
        return 'low_price'
    if check_consistency and price is not None:
        previous = _number(values.get('prev_close'))
        change = _number(values.get('return_1d'))
        if previous and change is not None and abs(price / previous - 1 - change) > 0.05:
            return 'inconsistent_price'
    return None


def _liquid(row, market):
    values = row.get('indicators') or {}
    price = _number(values.get('price'))
    volume = _number(values.get('volume'))
    traded = price * volume if price and volume else None
    if traded is not None and traded >= MIN_TODAY_VALUE[market]:
        return True
    try:
        average = average_turnover(values)
    except (KeyError, TypeError, ValueError):
        average = None
    return bool(average and average >= MIN_AVERAGE_TURNOVER[market])


def _reason_codes(row, market, regime):
    codes = []
    if ((regime or {}).get(market) or {}).get('above_ma20') is False:
        codes.append('market_headwind')
    for key in ('preopen_decision', 'selection_filter'):
        code = (row or {}).get(key)
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
    if 'breakdown' in codes and 'breakdown_or_rebound' in codes:
        codes = [code for code in codes if code != 'breakdown']
    if not codes:
        codes.append('not_evaluated')
    return codes


def _reason_labels(codes):
    labels = []
    for code in codes:
        label = REASON_LABELS.get(code)
        if not label:
            continue
        if label not in labels:
            labels.append(label)
    return labels or ['게이트 보류']


def _data_date(row, fallback=None):
    value = (row.get('indicators') or {}).get('data_date')
    return value if isinstance(value, str) and value else fallback


def _theme_issue(theme):
    issue = str((theme or {}).get('issue') or '')
    if issue.startswith('이슈:'):
        issue = issue[len('이슈:'):].strip()
    if not issue or '뉴스 미확인' in issue:
        return None
    return issue


def _collect(report, now):
    """근거별 후보를 모아 기호별로 합친다(테마 대표주 + 스캔 결과 뉴스)."""
    report = report or {}
    regime = report.get('market_regime') or {}
    index = _result_index(report)
    found = {}

    def add(row, market, change_pct, headline, source, gate_row, name=None, date=None):
        symbol = row.get('symbol')
        if not symbol:
            return
        codes = _reason_codes(gate_row if gate_row is not None else row, market, regime)
        found.setdefault(symbol, []).append(dict(
            symbol=symbol, market=market, name=name or row.get('name') or symbol,
            change_pct=round(change_pct, 1), reasons=codes,
            reason_labels=_reason_labels(codes), headline=headline,
            headline_source=source, data_date=date or _data_date(row)))

    for row in report.get('results') or []:
        market = row.get('market')
        if market not in MARKET_LABELS:
            continue
        change = _change_pct(row)
        if change is None or change < MIN_CHANGE_PCT or change > MAX_CHANGE_PCT:
            continue
        if _price_rejection(row, market, change) or _is_buy_signal(row) or not _liquid(row, market):
            continue
        headline = _headline(row, now)
        if not headline:
            continue
        add(row, market, change, headline, 'results_news', row)

    for snapshot in report.get('theme_sectors') or []:
        if not isinstance(snapshot, dict):
            continue
        market = snapshot.get('market')
        if market not in MARKET_LABELS:
            continue
        theme_date = snapshot.get('data_date')
        if isinstance(theme_date, str) and theme_date:
            try:
                if datetime.fromisoformat(theme_date).date() > now.date():
                    continue
            except ValueError:
                pass
        for theme in snapshot.get('rows') or []:
            issue = _theme_issue(theme)
            ratio = _number(theme.get('turnover_ratio'))
            for leader in theme.get('leaders') or []:
                symbol = leader.get('symbol')
                change = _number(leader.get('return_pct'))
                if not symbol or change is None or change < MIN_CHANGE_PCT or change > MAX_CHANGE_PCT:
                    continue
                row = index.get(symbol) or {'symbol': symbol, 'market': market,
                                            'name': leader.get('name'), 'indicators': {}}
                if row.get('market') not in (market, None):
                    continue
                if _price_rejection(row, market, change, check_consistency=False,
                                    require_price=bool(row.get('indicators'))):
                    continue
                if _is_buy_signal(row):
                    continue
                # 테마 집계에는 종목별 유동성이 없으므로, 테마 자금 유입(거래대금 배수)이나
                # 스캔의 당일 거래대금으로 확인한다. 둘 다 없으면 올리지 않는다.
                if not (_liquid(row, market) if row.get('indicators') else False) \
                        and not (ratio is not None and ratio >= 1.0):
                    continue
                name = leader.get('name') or row.get('name')
                alias = leader.get('alias')
                headline = _headline(row, now, name=name, alias=alias, theme_issue=issue)
                if not headline:
                    continue
                add(row, market, change, headline, 'theme_issue'
                    if issue and _mentions(issue, row, name, alias) else 'results_news',
                    row, name=name, date=theme_date)

    return found


def _merge(entries):
    """같은 종목이 여러 근거로 잡히면 더 최신·더 큰 등락을 쓰고 사유는 합친다."""
    def rank(entry):
        return (str(entry.get('data_date') or ''), 1 if entry.get('headline_source') == 'theme_issue' else 0,
                entry.get('change_pct') or 0)

    best = dict(sorted(entries, key=rank)[-1])
    codes = []
    for entry in entries:
        for code in entry.get('reasons') or []:
            if code not in codes:
                codes.append(code)
    best['reasons'] = codes
    best['reason_labels'] = _reason_labels(codes)
    return best


def select_urgent_issues(report, now=None, limit=MAX_ISSUES_PER_MARKET):
    """시장별 긴급 이슈 참고 목록. `report`의 `results`·`theme_sectors`·`market_regime`만 읽는다.

    반환값은 `{'KR': [...], 'US': [...]}`이며, 빈 시장은 키가 없다. 항목은
    `symbol`·`name`·`change_pct`·`reasons`/`reason_labels`·`headline`을 담는다.
    """
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('Timezone-aware issue check time required')
    grouped = {}
    for entries in _collect(report or {}, now).values():
        item = _merge(entries)
        market = item.get('market')
        if market not in MARKET_LABELS:
            continue
        grouped.setdefault(market, []).append(item)
    result = {}
    for market, items in grouped.items():
        items.sort(key=lambda item: (-item['change_pct'], item['symbol']))
        result[market] = items[:max(0, limit)]
    return result


def count_issues(issues):
    return sum(len(items) for items in (issues or {}).values())


def attach_urgent_issues(report, now=None, limit=MAX_ISSUES_PER_MARKET):
    """`report['urgent_issues']`에 감사용으로 남긴다(매수 판정·카운트는 건드리지 않는다)."""
    issues = select_urgent_issues(report, now=now, limit=limit)
    report['urgent_issues'] = issues
    return issues


def format_urgent_block(issues, limit=None):
    """텔레그램용 블록. 매수 후보 섹션과 섞이지 않게 머리줄에서 매수 신호 아님을 밝힌다."""
    if isinstance(issues, list):
        grouped = {}
        for item in issues:
            grouped.setdefault(item.get('market'), []).append(item)
    else:
        grouped = issues or {}
    lines = []
    for market in sorted(grouped, key=lambda name: MARKET_ORDER.index(name)
                         if name in MARKET_ORDER else len(MARKET_ORDER)):
        items = grouped.get(market) or []
        if limit is not None:
            items = items[:limit]
        if not items:
            continue
        if not lines:
            lines.extend([HEADING, SUBTITLE])
        if len(grouped) > 1:
            lines.append(f'[{MARKET_LABELS.get(market, market)}]')
        for item in items:
            symbol = str(item.get('symbol') or '')
            code = symbol.split('.')[0] if item.get('market') == 'KR' else symbol
            reasons = ' · '.join((item.get('reason_labels') or ['게이트 보류'])[:2])
            lines.append(f"• {item.get('name')} ({code}) {item.get('change_pct', 0):+.1f}% · 보류: {reasons}")
            lines.append(f"  이슈: {_clean(item.get('headline'), 110)}")
    return lines
