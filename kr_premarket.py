"""국내 장전 사전 시그널: 차트 게이트와 무관하게 국내 핵심 대표주의 NXT 장전·시간외 움직임과
간밤 미국 연동 이슈를 본다.

왜 필요한가: 미국 쪽에는 같은 블록(`premarket_signals`)이 있어 NVDA +31% 같은 장전 급등을 매수
후보와 별개로 보여 줬지만, 국내는 그런 창구가 없었다. 국내 대표주가 간밤 미국 섹터 급등에
연동해 NXT 장전에서 5% 넘게 움직여도, 스캔이 `market_headwind`·`breakdown_or_rebound`로 그
종목을 뺐다면 아침 알림에는 아무 흔적이 남지 않았다.

이 모듈은 매수 판정과 분리된 관찰 블록이다:

- 감시 풀(국내 시총 상위·핵심 대표주 18종목)은 차트·게이트 결과와 무관하게 항상 본다.
- NXT 장전(08:00~09:00)·NXT 시간외(15:30~20:00) 체결가가 전일 정규장 종가 대비 ±5% 넘게
  움직였거나, 24시간 내 촉매 뉴스(수주·계약·실적·CEO·정책·미국발)가 있거나, 간밤 미국 대응
  테마가 +2.5% 넘게 오른 종목만 올린다.
- 매수 신호가 아니다: `preopen_signal`·점수·`buy_evaluation` 카운트를 만들지 않는다.

시세는 KIS 국내 현재가(`FID_COND_MRKT_DIV_CODE='NX'`)를 쓴다. NXT 체결 시각을 응답이 주지
않으므로, 장전·시간외 세션이 아닐 때는 시세를 '장전 움직임'으로 부르지 않고 촉매·미국 연동만
보여 준다. 전일 정규장 종가를 알면 그 값을 기준으로 계산하고, 모르면 KIS가 주는 NXT 전일 대비율을
쓴다(어느 쪽인지 `base`에 남긴다).
"""

import math
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import price_sanity
from catalyst_track import personnel_only

# (종목코드, 한글명). 국내 시총 상위·핵심 대표주. 테마 대표주가 여기에 없어도 테마 집계는 따로 돈다.
WATCH_UNIVERSE = (
    ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('005380', '현대차'), ('000270', '기아'),
    ('373220', 'LG에너지솔루션'), ('006400', '삼성SDI'), ('005490', 'POSCO홀딩스'),
    ('068270', '셀트리온'), ('207940', '삼성바이오로직스'), ('035420', 'NAVER'),
    ('035720', '카카오'), ('012450', '한화에어로스페이스'), ('329180', 'HD현대중공업'),
    ('034020', '두산에너빌리티'), ('277810', '레인보우로보틱스'), ('105560', 'KB금융'),
    ('009150', '삼성전기'), ('012330', '현대모비스'),
)
WATCH_CODES = tuple(code for code, _name in WATCH_UNIVERSE)
WATCH_NAMES = {code: name for code, name in WATCH_UNIVERSE}

PREMARKET_MOVE_PCT = 5.0
# 간밤 미국 대응 테마가 이보다 오르면 '미국 연동' 맥락으로 붙인다.
US_THEME_MOVE_PCT = 2.5
# 이보다 오래된 미국 테마 스냅샷은 '간밤'이 아니다(금요일장~월요일 아침 3일은 허용).
US_THEME_MAX_AGE_DAYS = 4
NEWS_WINDOW_HOURS = 24
MAX_SIGNALS = 5
# 미국 테마 연동만으로 올린 종목은 소수만 둔다. 실제 장전 움직임·촉매 뉴스 자리를 빼앗지 않게.
MAX_US_LINK_ONLY = 3
HEADING = '[국내 장전 사전 시그널] · 매수 신호 아님'
KST = ZoneInfo('Asia/Seoul')
# 정규장 개장. 이 뒤에 모은 '장전' 감시는 이미 지난 창문이라 메시지에 싣지 않는다.
REGULAR_OPEN = time(9, 0)
# NXT 세션(한국 시간). 장전 08:00~09:00, 시간외 15:30~20:00.
NXT_SESSIONS = (('nxt_premarket', time(8, 0), time(9, 0)), ('nxt_after_hours', time(15, 30), time(20, 0)))
SESSION_LABELS = {'nxt_premarket': 'NXT 장전', 'nxt_after_hours': 'NXT 시간외'}

# 한국 종목 뉴스는 이미 종목명으로 걸러진 기사만 온다(`issuer_news`). 그중 촉매인 문장만 고른다.
CATALYST_PATTERNS = (
    ('ceo', re.compile(r'\bCEO\b|chief executive|대표이사|회장|사장|선임|사임|해임|유임|복귀|교체', re.I)),
    ('policy', re.compile(r'Trump|White House|export control|tariff|sanction|백악관|트럼프|관세|'
                          r'수출통제|수출 규제|규제|법안|상법|보조금|정부|공정위|금리|연준', re.I)),
    ('deal', re.compile(r'acquisition|acquire|merger|contract|order|deal|partnership|stake|'
                        r'수주|공급 계약|계약|납품|합병|인수|MOU|투자 유치|지분', re.I)),
    ('earnings', re.compile(r'earnings|guidance|beats?\b|misses?\b|outlook|surprise|'
                            r'실적|가이던스|어닝|영업이익|매출|잠정', re.I)),
)
CATALYST_LABELS = {'ceo': 'CEO·경영진', 'policy': '정책·규제', 'deal': '수주·M&A', 'earnings': '실적'}
CATALYST_PRIORITY = {name: len(CATALYST_PATTERNS) - index for index, (name, _pattern) in enumerate(CATALYST_PATTERNS)}
# 간밤 미국 이슈가 국내 종목까지 번진 경우를 한 줄로 남기기 위한 표시.
US_LINK_PATTERNS = re.compile(r'엔비디아|마이크론|브로드컴|TSMC|AMD|테슬라|애플|마이크로소프트|'
                              r'오라클|팔란티어|빅테크|나스닥|S&P|미국 증시|간밤', re.I)

# 국내 테마 → 간밤 미국에서 같은 축으로 움직이는 테마. 테마 이름은 `theme_sectors`의 유니버스와 같다.
US_THEME_COUNTERPART = {
    'HBM·메모리 반도체': 'AI 반도체·GPU',
    '반도체 장비·소재': '반도체 장비·소재',
    'FC-BGA·PCB·기판': 'AI 반도체·GPU',
    '2차전지·전고체': '2차전지·전기차',
    '전력기기·변압기': '전력·원전·그리드',
    '원전·SMR': '전력·원전·그리드',
    '방산·수출': '방산·우주',
    '우주항공·위성': '방산·우주',
    '바이오·비만/ADC': '바이오·비만·ADC',
    'AI 소프트웨어·보안': 'AI 소프트웨어·클라우드',
    '엔터·미디어': '엔터·미디어',
    '금융·밸류업': '금융·은행',
    '휴머노이드·로봇·액추에이터': '로봇·자동화',
}

_HEADLINE_CACHE = {}


def _number(value):
    """KIS 응답은 모든 값을 문자열로 준다('78,000'). 숫자·문자열 모두 받아 실수로 만든다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    try:
        result = float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clean(text, limit=110):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def session_of(now):
    """지금이 NXT 어느 세션인지. 장전·시간외가 아니면 None(그때 시세를 '장전'이라 부르지 않는다)."""
    local = now.astimezone(KST)
    if local.weekday() >= 5:
        return None
    for name, opens, closes in NXT_SESSIONS:
        if opens <= local.time() < closes:
            return name
    return None


def premarket_run(now=None):
    """그 시각이 국내 정규장 개장 전인지. 장전 블록을 메시지에 실을 수 있는 시간이다."""
    local = (now or datetime.now(timezone.utc)).astimezone(KST)
    if local.weekday() >= 5:
        return False
    return local.time() < REGULAR_OPEN


def include_block(watch):
    """이 감시 결과를 메시지에 실을지.

    장전 실행(08:00~09:00 NXT, 또는 개장 전 실행)에서 모은 것만 싣는다. 정규장 장중 스캔은
    이미 지난 장전 창문을 다시 보여 주는 것이고, 그때는 시세도 '장전'이라 부를 수 없다
    (2026-11-24 12:34 발송의 '장전 시세 미확인' 다섯 줄).
    """
    watch = watch or {}
    if watch.get('session') == 'nxt_premarket':
        return True
    checked = watch.get('checked_at')
    if not checked:
        return True  # 시각을 모르는 예전 보고서는 그대로 둔다
    try:
        return premarket_run(datetime.fromisoformat(str(checked)))
    except (TypeError, ValueError):
        return True


def theme_membership():
    """종목코드 → 그 종목이 속한 국내 세부 테마 이름들."""
    from theme_sectors import KR_THEMES
    mapping = {}
    for name, members in KR_THEMES:
        for member in members:
            mapping.setdefault(str(member[0]).split('.')[0], []).append(name)
    return mapping


def _us_theme_rows(us_rows, today=None):
    """미국 테마 행 목록을 이름 → 등락률로. 스냅샷 목록(리스트)과 행 목록을 모두 받는다.

    `today`를 주면 오래된 스냅샷은 버린다. 며칠 전 미국장 등락을 '간밤'이라 부르지 않기 위해서다.
    """
    rows = {}
    for item in us_rows or []:
        if not isinstance(item, dict):
            continue
        if 'rows' in item:
            if str(item.get('market') or 'US').upper().startswith('KR'):
                continue
            data_date = item.get('data_date')
            if today is not None and isinstance(data_date, str) and data_date:
                try:
                    if (today - datetime.fromisoformat(data_date).date()).days > US_THEME_MAX_AGE_DAYS:
                        continue
                except ValueError:
                    pass
            for row in item.get('rows') or []:
                if isinstance(row, dict) and row.get('name'):
                    rows[row['name']] = _number(row.get('return_pct'))
            continue
        if item.get('name'):
            rows[item['name']] = _number(item.get('return_pct'))
    return rows


def us_links(code, us_rows, membership=None, today=None):
    """이 종목이 속한 국내 테마의 미국 대응 테마가 간밤 얼마나 올랐는지."""
    membership = membership if membership is not None else theme_membership()
    rows = _us_theme_rows(us_rows, today)
    links = []
    for kr_theme in membership.get(str(code).split('.')[0], []):
        us_theme = US_THEME_COUNTERPART.get(kr_theme)
        move = rows.get(us_theme) if us_theme else None
        if move is None or move < US_THEME_MOVE_PCT:
            continue
        if us_theme not in [link['name'] for link in links]:
            links.append(dict(name=us_theme, return_pct=round(move, 2), kr_theme=kr_theme))
    return sorted(links, key=lambda link: -link['return_pct'])


def catalyst_categories(headline):
    text = str(headline or '')
    categories = [name for name, pattern in CATALYST_PATTERNS if pattern.search(text)]
    # 단순 임원 인사(승진·정기 인사·보직 이동)는 경영 변화가 아니다. 이 필터가 없으면
    # "삼성SDI, 부사장 4명 등 11명 승진"·"임원 승진 인사"가 매일 CEO·경영진 촉매로 실린다.
    if 'ceo' in categories and personnel_only(text):
        categories = [name for name in categories if name != 'ceo']
    return categories


def _headlines(code, name, now):
    key = (code, now.astimezone(KST).date().isoformat())
    if key in _HEADLINE_CACHE:
        return _HEADLINE_CACHE[key]
    try:
        from issuer_news import issuer_news_result
        articles = issuer_news_result(code, name, now).get('articles') or []
    except Exception:  # 뉴스 실패가 장전 감시를 막지 않는다
        articles = []
    _HEADLINE_CACHE[key] = articles
    return articles


def _catalyst(articles, now):
    best = None
    for article in articles or []:
        headline = str(article.get('headline') or '')
        categories = catalyst_categories(headline)
        if not categories:
            continue
        stamp = article.get('available_at')
        if stamp:
            try:
                published = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
            except ValueError:
                published = None
            if published is not None and published.utcoffset() is not None:
                if not now - timedelta(hours=NEWS_WINDOW_HOURS) <= published <= now + timedelta(minutes=10):
                    continue
        priority = max(CATALYST_PRIORITY.get(category, 0) for category in categories)
        rank = (priority, US_LINK_PATTERNS.search(headline) is not None, str(stamp or ''))
        if best is None or rank > best[0]:
            best = (rank, dict(headline=_clean(headline), categories=categories,
                               url=article.get('url'), source=article.get('source'),
                               available_at=stamp))
    return best[1] if best else None


def _base_close(previous, code, today):
    """전일 정규장 종가. 당일 봉이면 쓰지 않는다(당일 종가를 전일 종가로 부르지 않는다)."""
    entry = (previous or {}).get(code) or {}
    price = _number(entry.get('price') if isinstance(entry, dict) else entry)
    data_date = entry.get('data_date') if isinstance(entry, dict) else None
    if price is None or price <= 0:
        return None
    if isinstance(data_date, str) and data_date:
        try:
            if datetime.fromisoformat(data_date).date() >= today:
                return None
        except ValueError:
            return None
    return price


def quote_from_output(output, base=None):
    """KIS 국내 현재가 응답 → 장전 시세. 값이 이상하면 `price_sanity.PriceError`."""
    output = output or {}
    price = _number(output.get('stck_prpr') if isinstance(output, dict) else None)
    if price is None or price <= 0:
        raise price_sanity.PriceError('abnormal_price')
    basis = 'previous_regular_close'
    change = _number((price / base - 1) * 100) if base else None
    if change is None:
        change = _number(output.get('prdy_ctrt'))
        vrss = _number(output.get('prdy_vrss'))
        basis = 'nxt_previous_close'
        base = price - vrss if change is not None and vrss is not None else None
    if change is None:
        raise price_sanity.PriceError('abnormal_change')
    # 한국 가격제한(±30%)을 넘는 등락은 시세 오류다(티커 혼동·액면병합).
    reason = price_sanity.rejection_reason('KR', change, price)
    if reason:
        raise price_sanity.PriceError(reason)
    return dict(price=round(price, 2), previous_close=None if base is None else round(base, 2),
                change_pct=round(change, 2), base=basis)


def _inquire(client, code, market_div='NX'):
    import requests
    from industry_flow import BASE
    response = requests.get(BASE + '/uapi/domestic-stock/v1/quotations/inquire-price',
                            headers=dict(client.headers, tr_id='FHKST01010100'),
                            params=dict(FID_COND_MRKT_DIV_CODE=market_div, FID_INPUT_ISCD=code),
                            timeout=6)
    if response.status_code != 200:
        raise RuntimeError(f'KIS 현재가 HTTP {response.status_code}')
    body = response.json()
    if body.get('rt_cd') != '0':
        raise RuntimeError(f"KIS 현재가 오류 {body.get('msg_cd')}")
    return body.get('output') or {}


def _default_quotes(codes, now, previous=None):
    """KIS NXT 현재가로 장전 시세를 만든다. 세션 밖이면 조회하지 않는다.

    한 종목의 시세 오류·실패가 감시 전체를 막지 않도록 종목마다 담는다. 오류는 그 종목의
    `error`로 남겨, 그 종목은 시세 없이 뉴스·미국 연동만으로 평가하게 한다.
    """
    session = session_of(now)
    if session is None:
        return {}
    from industry_flow import Client
    client = Client()
    today = now.astimezone(KST).date()
    observed = now.astimezone(KST).isoformat()
    quotes = {}
    for code in codes:
        try:
            quote = quote_from_output(_inquire(client, code), _base_close(previous, code, today))
        except price_sanity.PriceError as exc:
            quotes[code] = dict(error=exc.reason, session=session, observed_at=observed)
            continue
        except Exception as exc:  # 종목 하나의 조회 실패는 나머지 감시를 막지 않는다
            quotes[code] = dict(error=type(exc).__name__, session=session, observed_at=observed)
            continue
        quote.update(session=session, observed_at=observed)
        quotes[code] = quote
    return quotes


def select_signals(quotes, articles, now, symbols=None, us_rows=None,
                   limit=MAX_SIGNALS, us_link_only=MAX_US_LINK_ONLY):
    """장전 ±5% 또는 촉매 뉴스 또는 간밤 미국 연동이 있는 종목만 고른다(매수 판정 아님)."""
    if now.utcoffset() is None:
        raise ValueError('Timezone-aware premarket check time required')
    membership = theme_membership() if us_rows else {}
    today = now.astimezone(KST).date()
    found = []
    for code in (symbols or WATCH_CODES):
        quote = (quotes or {}).get(code) or {}
        change = _number(quote.get('change_pct'))
        move = change if change is not None and abs(change) >= PREMARKET_MOVE_PCT else None
        catalyst = _catalyst((articles or {}).get(code), now)
        links = us_links(code, us_rows, membership, today) if us_rows is not None else []
        if move is None and catalyst is None and not links:
            continue
        categories = (catalyst or {}).get('categories') or []
        bucket = 0 if move is not None else 1 if catalyst is not None else 2
        found.append((bucket, {
            'code': code, 'name': WATCH_NAMES.get(code) or code,
            'change_pct': change, 'move_pct': move,
            'price': quote.get('price'), 'previous_close': quote.get('previous_close'),
            'base': quote.get('base'), 'session': quote.get('session'),
            'observed_at': quote.get('observed_at'),
            'catalysts': categories,
            'catalyst_labels': [CATALYST_LABELS.get(name, name) for name in categories],
            'headline': (catalyst or {}).get('headline'),
            'url': (catalyst or {}).get('url'),
            'headline_at': (catalyst or {}).get('available_at'),
            'us_links': links,
        }))
    found.sort(key=lambda entry: (entry[0],
                                  -(abs(entry[1]['change_pct']) if entry[1]['change_pct'] is not None else 0),
                                  -(entry[1]['us_links'][0]['return_pct'] if entry[1]['us_links'] else -999),
                                  -max([CATALYST_PRIORITY.get(name, 0) for name in entry[1]['catalysts']] or [0]),
                                  entry[1]['code']))
    items = []
    link_only = 0
    for bucket, item in found:
        if bucket == 2:
            if link_only >= us_link_only:
                continue
            link_only += 1
        items.append(item)
        if len(items) >= max(0, limit):
            break
    return items


def collect(now=None, symbols=None, quotes=None, articles=None, previous=None, us_rows=None,
            limit=MAX_SIGNALS, quote_fetcher=None, news_fetcher=None):
    """장전 감시를 한 번 돌린다. 시세·뉴스 실패는 error로 남기고 블록은 비운다."""
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('Timezone-aware premarket check time required')
    symbols = tuple(symbols or WATCH_CODES)
    session = session_of(now)
    result = dict(items=[], checked_at=now.isoformat(), session=session,
                  session_label=SESSION_LABELS.get(session), universe=list(symbols),
                  quotes_seen=0, news_seen=0, rejected=[], error=None)
    try:
        if quotes is None and session is not None:
            quotes = (quote_fetcher or _default_quotes)(list(symbols), now, previous) or {}
        quotes = dict(quotes or {})
        result['rejected'] = [dict(symbol=code, reason=quote.get('error'))
                              for code, quote in quotes.items()
                              if isinstance(quote, dict) and quote.get('error')]
        result['quotes_seen'] = sum(1 for quote in quotes.values()
                                    if isinstance(quote, dict) and not quote.get('error'))
        known = dict(articles or {})
        for code in symbols:
            known.setdefault(code, [])
        for code in symbols:
            if known[code]:
                continue
            try:
                known[code] = (news_fetcher or _headlines)(code, WATCH_NAMES.get(code, code), now) or []
            except Exception as exc:  # 한 종목 뉴스 실패가 감시 전체를 막지 않는다
                print(f'{code} domestic premarket news unavailable ({type(exc).__name__})', flush=True)
        result['news_seen'] = sum(1 for code in symbols if known.get(code))
        result['items'] = select_signals(quotes, known, now, symbols=symbols,
                                         us_rows=us_rows, limit=limit)
    except Exception as exc:
        result['error'] = type(exc).__name__
        print(f'Domestic premarket watch unavailable ({type(exc).__name__})', flush=True)
    return result


def previous_closes(report):
    """직전 정규장 종가: 보고서의 스캔 행에서 종목코드별 마지막 종가와 그 날짜."""
    closes = {}
    for row in (report or {}).get('results') or []:
        if row.get('market') != 'KR':
            continue
        code = str(row.get('symbol') or '').split('.')[0]
        values = row.get('indicators') or {}
        price = _number(values.get('price'))
        if not code or price is None or price <= 0:
            continue
        closes[code] = dict(price=price, data_date=values.get('data_date'))
    return closes


def us_theme_rows(report):
    """보고서에 들어 있는 미국 세부 테마 스냅샷(간밤 미국장)."""
    return [snapshot for snapshot in (report or {}).get('theme_sectors') or []
            if isinstance(snapshot, dict) and str(snapshot.get('market') or '').upper().startswith('US')]


def attach(report, now=None, us_rows=None, **options):
    """`report['kr_premarket_watch']`로 남긴다. 발송 시점에는 다시 조회하지 않는다."""
    watch = collect(now=now, previous=options.pop('previous', None) or previous_closes(report),
                    us_rows=us_rows if us_rows is not None else us_theme_rows(report), **options)
    report['kr_premarket_watch'] = watch
    return watch


def _stamp(item):
    observed = item.get('observed_at')
    if not observed:
        return ''
    try:
        return ' (' + datetime.fromisoformat(observed).astimezone(KST).strftime('%H:%M') + ')'
    except (ValueError, TypeError):
        return ''


def format_block(items, session=None):
    """텔레그램 블록. 매수 후보와 섞이지 않게 별도 머리줄을 쓴다."""
    items = list(items or [])
    if not items:
        return []
    lines = [HEADING]
    for item in items:
        code = item.get('code') or ''
        label = SESSION_LABELS.get(item.get('session') or session)
        if item.get('move_pct') is not None and label:
            move = f"{label} {item['move_pct']:+.1f}%{_stamp(item)}"
        elif label and item.get('change_pct') is not None:
            move = f"{label} ±{PREMARKET_MOVE_PCT:.0f}% 미만"
        else:
            # 체결가가 없을 때 '장전 시세 미확인'을 줄마다 붙이면 촉매 줄만 길어진다.
            # 확인하지 못했다는 사실은 시세 칸을 비우는 것으로 말한다.
            move = None
        parts = [f"• {item.get('name')} ({code})" + (f" · {move}" if move else '')]
        context = []
        if item.get('catalyst_labels'):
            context.append('촉매: ' + '·'.join(item['catalyst_labels']))
        if item.get('us_links'):
            link = item['us_links'][0]
            context.append(f"간밤 미국 {link['name']} {link['return_pct']:+.1f}%")
        if not item.get('catalyst_labels') and not item.get('us_links'):
            context.append('촉매 뉴스 미확인')
        lines.append(' · '.join(parts + [' · '.join(context)]))
        if item.get('headline'):
            lines.append(f"  {_clean(item['headline'])}")
    return lines
