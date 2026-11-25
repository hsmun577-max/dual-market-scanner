"""미국 장전 사전 시그널: 차트 게이트와 무관하게 대형 기술주의 장전 급등락·촉매 뉴스를 본다.

왜 필요한가: 2026-11-23 뉴욕장에서 엔비디아 CEO 복귀와 함께 NVDA가 하루 +31% 급등했지만,
스캐너는 이 종목을 `breakdown_or_rebound`·`market_headwind`로 매수 후보에서 뺐고 그날 아침
텔레그램에는 "매수 조건 충족 없음"만 남았다(사용자 지적 2026-11-24: "복귀할거라는 시그널을
찾았어야지"). 게이트는 매수 판단에 필요하지만, 게이트가 뉴스까지 가리는 것은 아니다.

이 모듈은 매수 판정과 분리된 관찰 블록이다:

- 감시 풀(미국 시총 상위·핵심 기술주 20종목)은 차트·게이트 결과와 무관하게 항상 본다.
- 프리마켓(또는 직전 시간외) 변동률 ±5% 이상, 또는 24시간 내 촉매 키워드 뉴스
  (CEO·복귀/사임·정책·관세·수주/M&A·실적)가 있는 종목만 올린다.
- 매수 신호가 아니다: `preopen_signal`·점수·`buy_evaluation` 카운트를 만들지 않는다.
"""
import math
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# (티커, 한글명, 영문명). 영문명은 Yahoo 헤드라인이 종목을 부르는 이름이다.
WATCH_UNIVERSE = (
    ('NVDA', '엔비디아', 'Nvidia'), ('AAPL', '애플', 'Apple'), ('MSFT', '마이크로소프트', 'Microsoft'),
    ('AMZN', '아마존', 'Amazon'), ('GOOGL', '알파벳', 'Alphabet'), ('META', '메타', 'Meta'),
    ('TSLA', '테슬라', 'Tesla'), ('AVGO', '브로드컴', 'Broadcom'), ('AMD', 'AMD', 'AMD'),
    ('INTC', '인텔', 'Intel'), ('TSM', 'TSMC', 'TSMC'), ('MU', '마이크론', 'Micron'),
    ('ASML', 'ASML', 'ASML'), ('ARM', 'ARM', 'Arm'), ('QCOM', '퀄컴', 'Qualcomm'),
    ('NFLX', '넷플릭스', 'Netflix'), ('ORCL', '오라클', 'Oracle'), ('PLTR', '팔란티어', 'Palantir'),
    ('DELL', '델', 'Dell'), ('SMCI', '슈퍼마이크로', 'Super Micro'),
)
WATCH_SYMBOLS = tuple(row[0] for row in WATCH_UNIVERSE)
WATCH_NAMES = {symbol: korean for symbol, korean, _english in WATCH_UNIVERSE}
WATCH_ALIASES = {symbol: (korean, english) for symbol, korean, english in WATCH_UNIVERSE}
PREMARKET_MOVE_PCT = 5.0
NEWS_WINDOW_HOURS = 24
MAX_SIGNALS = 5
HEADING = '[미국 장전 사전 시그널]'
SUBTITLE = '매수 신호 아님'
NY = ZoneInfo('America/New_York')
SESSION_LABELS = {'premarket': '프리마켓', 'after_hours': '시간외'}
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
CATALYST_LABELS = {'ceo': 'CEO·경영진', 'policy': '정책·규제', 'deal': '수주·M&A', 'earnings': '실적'}
CATALYST_PRIORITY = {name: len(CATALYST_PATTERNS) - index for index, (name, _pattern) in enumerate(CATALYST_PATTERNS)}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _clean(text, limit=110):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def catalyst_categories(headline):
    text = str(headline or '')
    return [name for name, pattern in CATALYST_PATTERNS if pattern.search(text)]


def _frame(prices, symbol):
    """yfinance 다중 티커 프레임에서 한 종목의 일봉·분봉 프레임을 꺼낸다."""
    columns = getattr(prices, 'columns', None)
    if columns is None:
        raise KeyError(symbol)
    if getattr(columns, 'nlevels', 1) > 1:
        level0 = list(columns.get_level_values(0))
        level1 = list(columns.get_level_values(1))
        if symbol in level0:
            return prices[symbol]
        if symbol in level1:
            return prices.xs(symbol, axis=1, level=1)
        raise KeyError(symbol)
    return prices


def premarket_move(frame, now):
    """전일 정규장 종가 대비 프리마켓(또는 직전 시간외) 마지막 체결의 변동률.

    프리마켓이 아직 열리지 않은 시각(03:50 ET 장전 요약)에는 직전 세션의 시간외
    체결을 `after_hours`로 표시한다. 어느 쪽인지 `session`에 남겨, 아침 4시 이전 값을
    '프리마켓'이라고 부르지 않는다.
    """
    if frame is None or 'Close' not in getattr(frame, 'columns', []):
        return None
    import pandas as pd
    data = frame.dropna(subset=['Close'])
    if data.empty:
        return None
    index = pd.to_datetime(data.index)
    if index.tz is None:
        return None
    local = index.tz_convert(NY)
    today = now.astimezone(NY).date()
    closes = list(data['Close'])
    regular = [(stamp, close) for stamp, close in zip(local, closes)
               if stamp.date() < today and time(9, 30) <= stamp.time() < time(16, 0)]
    if not regular:
        return None
    previous_date, previous = regular[-1]
    session = None
    latest = None
    for stamp, close in zip(local, closes):
        if stamp.date() == today and time(4, 0) <= stamp.time() < time(9, 30):
            latest, session = (stamp, close), 'premarket'
    if latest is None:
        for stamp, close in zip(local, closes):
            if stamp.date() == previous_date.date() and time(16, 0) <= stamp.time() < time(20, 0):
                latest, session = (stamp, close), 'after_hours'
    if latest is None:
        return None
    stamp, last = latest
    last, base = _number(last), _number(previous)
    if not last or not base or last <= 0 or base <= 0:
        return None
    change = (last / base - 1) * 100
    # A five-minute bar that moved more than 5x in extended hours is a data error.
    if not math.isfinite(change) or change > 400 or change < -95:
        return None
    return dict(change_pct=round(change, 2), price=round(last, 4), previous_close=round(base, 4),
                as_of=stamp.isoformat(), session=session)


def _default_quotes(symbols, now):
    from scanner import yf_download
    prices = yf_download(list(symbols), period='5d', interval='5m', prepost=True, auto_adjust=False,
                         group_by='ticker', progress=False, threads=8, timeout=20,
                         multi_level_index=True)
    quotes = {}
    if prices is None:
        return quotes
    for symbol in symbols:
        try:
            move = premarket_move(_frame(prices, symbol), now)
        except (KeyError, ValueError, TypeError):
            move = None
        if move:
            quotes[symbol] = move
    return quotes


def _recent(article, now, hours=NEWS_WINDOW_HOURS):
    stamp = article.get('available_at') or article.get('published_at')
    if not stamp:
        return True
    try:
        published = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return True
    if published.utcoffset() is None:
        return True
    return now - timedelta(hours=hours) <= published <= now + timedelta(minutes=10)


def _mentions(headline, symbol, names=None):
    """헤드라인이 그 종목을 가리키는지. 영문 약칭은 단어 경계로만 본다(Arm≠harm)."""
    text = str(headline or '')
    for name in ([names] if isinstance(names, str) else list(names or [])):
        if not name:
            continue
        pattern = rf'(?<![A-Za-z0-9]){re.escape(str(name))}(?![A-Za-z0-9])'
        if re.search(pattern, text, re.I):
            return True
    return bool(symbol) and bool(re.search(rf'(?<![A-Za-z0-9]){re.escape(str(symbol))}(?![A-Za-z0-9])',
                                           text, re.I))


def _default_articles(symbol, now):
    """Yahoo 종목 뉴스(24시간). 실패는 빈 목록으로 돌려주고 블록 자체를 막지 않는다."""
    import yfinance as yf
    articles = []
    try:
        items = yf.Ticker(symbol).get_news(count=12) or []
    except Exception:  # 뉴스 공급 실패가 장전 감시를 막지 않는다
        return articles
    for item in items:
        content = item.get('content') if isinstance(item, dict) else None
        if isinstance(content, dict):
            title = content.get('title')
            stamp = content.get('pubDate') or content.get('displayTime')
            link = (content.get('canonicalUrl') or {}).get('url')
            source = (content.get('provider') or {}).get('displayName')
        else:
            title = item.get('title') if isinstance(item, dict) else None
            stamp = item.get('providerPublishTime') if isinstance(item, dict) else None
            link = item.get('link') if isinstance(item, dict) else None
            source = item.get('publisher') if isinstance(item, dict) else None
        published = None
        if isinstance(stamp, (int, float)):
            published = datetime.fromtimestamp(stamp, timezone.utc)
        elif isinstance(stamp, str) and stamp:
            try:
                published = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            except ValueError:
                published = None
        if not title or published is None or published.utcoffset() is None:
            continue
        if not _recent(dict(available_at=published.isoformat()), now):
            continue
        articles.append(dict(headline=str(title), available_at=published.isoformat(),
                             source=source, url=link))
    return articles


def articles_from_report(report, symbols=None):
    """이미 받아 둔 종목 뉴스(`company_analysis.news`)를 재사용한다."""
    symbols = set(symbols or WATCH_SYMBOLS)
    articles = {}
    for row in (report or {}).get('results') or []:
        ticker = str(row.get('symbol') or '').split('.')[0]
        if ticker not in symbols:
            continue
        news = (row.get('company_analysis') or {}).get('news') or []
        if news:
            articles.setdefault(ticker, []).extend(news)
    return articles


def _catalyst(articles, symbol, now, names=None):
    best = None
    for article in articles or []:
        headline = str(article.get('headline') or '')
        if not _recent(article, now) or not _mentions(headline, symbol, names):
            continue
        categories = catalyst_categories(headline)
        if not categories:
            continue
        priority = max(CATALYST_PRIORITY.get(category, 0) for category in categories)
        rank = (priority, str(article.get('available_at') or ''))
        if best is None or rank > best[0]:
            best = (rank, dict(headline=_clean(headline), categories=categories,
                               url=article.get('url'), source=article.get('source'),
                               available_at=article.get('available_at')))
    return best[1] if best else None


def select_signals(quotes, articles, now=None, symbols=None, limit=MAX_SIGNALS):
    """프리마켓 ±5% 또는 촉매 뉴스가 있는 종목만 고른다(매수 판정 아님)."""
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('Timezone-aware premarket check time required')
    items = []
    for symbol in (symbols or WATCH_SYMBOLS):
        quote = (quotes or {}).get(symbol) or {}
        articles_for_symbol = (articles or {}).get(symbol) or []
        change = _number(quote.get('change_pct'))
        move = change if change is not None and abs(change) >= PREMARKET_MOVE_PCT else None
        catalyst = _catalyst(articles_for_symbol, symbol, now, WATCH_ALIASES.get(symbol))
        if move is None and catalyst is None:
            continue
        categories = (catalyst or {}).get('categories') or []
        items.append(dict(
            symbol=symbol, name=WATCH_NAMES.get(symbol) or symbol,
            change_pct=change, move_pct=move, session=quote.get('session'),
            as_of=quote.get('as_of'),
            catalysts=categories,
            catalyst_labels=[CATALYST_LABELS.get(name, name) for name in categories],
            headline=(catalyst or {}).get('headline'),
            url=(catalyst or {}).get('url'),
            headline_at=(catalyst or {}).get('available_at'),
        ))
    items.sort(key=lambda item: (-(abs(item['move_pct']) if item['move_pct'] is not None else 0),
                                 -max([CATALYST_PRIORITY.get(name, 0) for name in item['catalysts']] or [0]),
                                 item['symbol']))
    return items[:max(0, limit)]


def collect(symbols=None, now=None, quotes=None, articles=None, limit=MAX_SIGNALS,
            quote_fetcher=None, news_fetcher=None):
    """장전 감시를 한 번 돌린다. 시세·뉴스 실패는 error로 남기고 블록은 비운다."""
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('Timezone-aware premarket check time required')
    symbols = tuple(symbols or WATCH_SYMBOLS)
    result = dict(items=[], checked_at=now.isoformat(), universe=list(symbols),
                  quotes_seen=0, news_seen=0, error=None)
    try:
        if quotes is None:
            quotes = (quote_fetcher or _default_quotes)(list(symbols), now) or {}
        result['quotes_seen'] = len(quotes)
        known = dict(articles or {})
        for symbol in symbols:
            known.setdefault(symbol, [])
        for symbol in symbols:
            if known[symbol]:
                continue
            try:
                known[symbol] = (news_fetcher or _default_articles)(symbol, now) or []
            except Exception as exc:  # 한 종목 뉴스 실패가 감시 전체를 막지 않는다
                print(f'{symbol} premarket news unavailable ({type(exc).__name__})', flush=True)
        result['news_seen'] = sum(1 for symbol in symbols if known.get(symbol))
        result['items'] = select_signals(quotes, known, now=now, symbols=symbols, limit=limit)
    except Exception as exc:
        result['error'] = type(exc).__name__
        print(f'Premarket watch unavailable ({type(exc).__name__})', flush=True)
    return result


def attach(report, now=None, symbols=None, **options):
    """`report['premarket_watch']`로 남긴다. 발송 시점에는 다시 조회하지 않는다."""
    watch = collect(symbols=symbols, now=now,
                    articles=options.pop('articles', None)
                    or articles_from_report(report, symbols or WATCH_SYMBOLS),
                    **options)
    report['premarket_watch'] = watch
    return watch


def format_block(items):
    """텔레그램 블록. 매수 후보와 섞이지 않게 별도 머리줄을 쓴다."""
    items = list(items or [])
    if not items:
        return []
    lines = [f'{HEADING} · {SUBTITLE}']
    for item in items:
        symbol = item.get('symbol') or ''
        session = SESSION_LABELS.get(item.get('session'), '장전')
        stamp = ''
        if item.get('as_of'):
            try:
                stamp = ' (' + datetime.fromisoformat(item['as_of']).astimezone(NY).strftime('%H:%M ET') + ')'
            except (ValueError, TypeError):
                stamp = ''
        if item.get('move_pct') is not None:
            move = f"{session} {item['move_pct']:+.1f}%{stamp}"
        else:
            move = f"{session} ±{PREMARKET_MOVE_PCT:.0f}% 미만"
        parts = [f"• {item.get('name')} ({symbol}) · {move}"]
        if item.get('catalyst_labels'):
            parts.append('촉매: ' + '·'.join(item['catalyst_labels']))
        lines.append(' · '.join(parts))
        if item.get('headline'):
            lines.append(f"  {_clean(item['headline'])}")
    return lines
