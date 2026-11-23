"""증권사 리서치식 세부 테마·이슈 섹터: 실제 구성종목 등락·상승비율·거래대금으로 순위를 매긴다.

왜 필요한가: `SECTOR_ETFS`의 15개 대분류 ETF는 `return>0 + 거래대금 2배` 조건과 함께 쓰이면
지수가 빠지는 날 전부 음수라 아무것도 통과하지 못하고, 메시지는 `1위 은행` 한 단어나
`확인 불가`로 끝났다. 그런 날에도 로봇·방산처럼 수급이 쏠린 세부 테마는 존재한다.

이 모듈은 테마별 실제 구성종목의 일봉에서
  - 테마 등락률(구성종목 동일가중 평균),
  - 지수 대비 초과등락(상대강도),
  - 상승 종목 비율(확산도),
  - 구성종목 거래대금 합계와 20일 평균 대비 배수(자금 유입 강도)
를 계산해 위 세 기준의 백분위 가중합(상대강도 45%·확산도 30%·거래대금 25%)으로 순위를 낸다.
상대강도가 1순위 기준이므로 지수가 -3% 빠지는 날 -1%로 버틴 테마도 `1번`이 될 수 있고,
그 사실을 `state`(주도/상대강세/약세)로 함께 남겨 하락장의 상대강세를 상승으로 오해하지 않게 한다.

각 상위 테마에는 대표 상승주 2~3개와 그 등락률, 그리고 기존 뉴스 수집기
(`issuer_news.issuer_news_result` - 국내 종목 뉴스, `yfinance` - 미국 종목 뉴스)에서 가져온
당일 헤드라인 한 줄을 붙인다. 뉴스가 없으면 수급·상승 요약으로 대체하며 이유를 지어내지 않는다.
"""

import re
from datetime import datetime, time, timedelta, timezone
from html import unescape

# 테마 구성종목은 상장 종목코드(한국)와 티커(미국)로 고정한다. 이름은 종목 뉴스 수집기의
# 엔티티 매칭에 그대로 쓰이므로 상장명과 같아야 한다.
KR_THEMES = (
    ("휴머노이드·로봇·액추에이터", (
        ("277810.KQ", "레인보우로보틱스"), ("108490.KQ", "로보티즈"), ("090710.KQ", "휴림로봇"),
        ("058610.KQ", "에스피지"), ("389500.KQ", "에스비비테크"), ("437730.KQ", "삼현"),
        ("454910.KS", "두산로보틱스"), ("007820.KQ", "엠엑스로보틱스"),
    )),
    ("HBM·메모리 반도체", (
        ("000660.KS", "SK하이닉스"), ("005930.KS", "삼성전자"), ("042700.KS", "한미반도체"),
    )),
    ("반도체 장비·소재", (
        ("039030.KQ", "이오테크닉스"), ("240810.KQ", "원익IPS"), ("036930.KQ", "주성엔지니어링"),
        ("319660.KQ", "피에스케이"), ("064760.KQ", "티씨케이"), ("357780.KQ", "솔브레인"),
        ("074600.KQ", "원익QnC"),
    )),
    ("FC-BGA·PCB·기판", (
        ("009150.KS", "삼성전기"), ("353200.KS", "대덕전자"), ("222800.KQ", "심텍"),
        ("195870.KS", "해성디에스"), ("007810.KS", "코리아써키트"), ("007660.KS", "이수페타시스"),
        ("036540.KQ", "SFA반도체"), ("489500.KQ", "엘케이켐"),
    )),
    ("2차전지·전고체", (
        ("247540.KQ", "에코프로비엠"), ("086520.KQ", "에코프로"), ("003670.KS", "포스코퓨처엠"),
        ("373220.KS", "LG에너지솔루션"), ("006400.KS", "삼성SDI"), ("066970.KS", "엘앤에프"),
        ("348370.KQ", "엔켐"), ("078600.KQ", "대주전자재료"),
    )),
    ("전력기기·변압기", (
        ("267260.KS", "HD현대일렉트릭"), ("010120.KS", "LS ELECTRIC"), ("298040.KS", "효성중공업"),
        ("062040.KS", "산일전기"), ("033100.KQ", "제룡전기"), ("103590.KS", "일진전기"),
        ("000500.KS", "가온전선"), ("001440.KS", "대한전선"),
    )),
    ("원전·SMR", (
        ("034020.KS", "두산에너빌리티"), ("052690.KS", "한전기술"), ("083650.KQ", "비에이치아이"),
        ("105840.KS", "우진"), ("051600.KS", "한전KPS"), ("042370.KQ", "비츠로테크"),
        ("019990.KQ", "에너토크"),
    )),
    ("방산·수출", (
        ("012450.KS", "한화에어로스페이스"), ("079550.KS", "LIG디펜스앤에어로스페이스"),
        ("064350.KS", "현대로템"), ("047810.KS", "한국항공우주"), ("272210.KS", "한화시스템"),
        ("010820.KS", "퍼스텍"), ("065450.KQ", "빅텍"),
    )),
    ("조선·LNG", (
        ("009540.KS", "HD한국조선해양"), ("010140.KS", "삼성중공업"), ("042660.KS", "한화오션"),
        ("329180.KS", "HD현대중공업"), ("443060.KS", "HD현대마린솔루션"), ("075580.KS", "세진중공업"),
        ("033500.KQ", "동성화인텍"), ("439260.KS", "대한조선"),
    )),
    ("바이오·비만/ADC", (
        ("207940.KS", "삼성바이오로직스"), ("196170.KQ", "알테오젠"), ("141080.KQ", "리가켐바이오"),
        ("000100.KS", "유한양행"), ("128940.KS", "한미약품"), ("028300.KQ", "HLB"),
        ("068270.KS", "셀트리온"), ("326030.KS", "SK바이오팜"),
    )),
    ("우주항공·위성", (
        ("047810.KS", "한국항공우주"), ("099320.KQ", "쎄트렉아이"), ("451760.KQ", "컨텍"),
        ("211270.KQ", "AP위성"), ("462350.KQ", "이노스페이스"),
    )),
    ("AI 소프트웨어·보안", (
        ("018260.KS", "삼성에스디에스"), ("304100.KQ", "솔트룩스"), ("047560.KQ", "이스트소프트"),
        ("263860.KQ", "지니언스"), ("053800.KQ", "안랩"), ("108860.KQ", "셀바스AI"),
        ("079940.KQ", "가비아"),
    )),
    ("엔터·미디어", (
        ("352820.KS", "하이브"), ("035900.KQ", "JYP Ent."), ("041510.KQ", "에스엠"),
        ("122870.KQ", "와이지엔터테인먼트"), ("035760.KQ", "CJ ENM"), ("253450.KQ", "스튜디오드래곤"),
    )),
    ("금융·밸류업", (
        ("105560.KS", "KB금융"), ("055550.KS", "신한지주"), ("086790.KS", "하나금융지주"),
        ("316140.KS", "우리금융지주"), ("024110.KS", "기업은행"), ("032830.KS", "삼성생명"),
        ("138040.KS", "메리츠금융지주"), ("323410.KS", "카카오뱅크"),
    )),
    ("지배구조·상법개정", (
        ("028260.KS", "삼성물산"), ("034730.KS", "SK"), ("003550.KS", "LG"),
        ("180640.KS", "한진칼"), ("000150.KS", "두산"), ("078930.KS", "GS"),
        ("000670.KS", "영풍"), ("004990.KS", "롯데지주"),
    )),
)

US_THEMES = (
    ("AI 반도체·GPU", (
        ("NVDA", "엔비디아", "NVIDIA"), ("AMD", "AMD", "Advanced Micro Devices"),
        ("AVGO", "브로드컴", "Broadcom"), ("MRVL", "마벨", "Marvell"), ("MU", "마이크론", "Micron"),
        ("ARM", "ARM", "Arm Holdings"), ("ANET", "아리스타", "Arista"),
        ("SMCI", "슈퍼마이크로", "Super Micro"), ("DELL", "델", "Dell"),
    )),
    ("반도체 장비·소재", (
        ("AMAT", "어플라이드", "Applied Materials"), ("LRCX", "램리서치", "Lam Research"),
        ("KLAC", "KLA", "KLA"), ("ASML", "ASML", "ASML"), ("TER", "테라다인", "Teradyne"),
        ("ENTG", "인테그리스", "Entegris"),
    )),
    ("AI 소프트웨어·클라우드", (
        ("MSFT", "마이크로소프트", "Microsoft"), ("ORCL", "오라클", "Oracle"),
        ("CRM", "세일즈포스", "Salesforce"), ("NOW", "서비스나우", "ServiceNow"),
        ("SNOW", "스노우플레이크", "Snowflake"), ("ADBE", "어도비", "Adobe"),
        ("INTU", "인튜이트", "Intuit"), ("PLTR", "팔란티어", "Palantir"),
    )),
    ("사이버보안", (
        ("PANW", "팔로알토", "Palo Alto Networks"), ("CRWD", "크라우드스트라이크", "CrowdStrike"),
        ("ZS", "지스케일러", "Zscaler"), ("FTNT", "포티넷", "Fortinet"), ("OKTA", "옥타", "Okta"),
        ("S", "센티널원", "SentinelOne"),
    )),
    ("전력·원전·그리드", (
        ("VST", "비스트라", "Vistra"), ("CEG", "컨스털레이션", "Constellation Energy"),
        ("NRG", "NRG", "NRG Energy"), ("TLN", "탈런", "Talen Energy"),
        ("GEV", "GE버노바", "GE Vernova"), ("ETN", "이턴", "Eaton"),
        ("PWR", "콴타서비스", "Quanta Services"),
    )),
    ("방산·우주", (
        ("LMT", "록히드마틴", "Lockheed Martin"), ("RTX", "RTX", "RTX"),
        ("NOC", "노스럽그루먼", "Northrop Grumman"), ("GD", "제너럴다이내믹스", "General Dynamics"),
        ("LHX", "L3해리스", "L3Harris"), ("RKLB", "로켓랩", "Rocket Lab"),
        ("ASTS", "AST스페이스모바일", "AST SpaceMobile"),
    )),
    ("로봇·자동화", (
        ("ISRG", "인튜이티브서지컬", "Intuitive Surgical"), ("SYM", "심보틱", "Symbotic"),
        ("ROK", "록웰오토메이션", "Rockwell Automation"), ("ZBRA", "지브라", "Zebra Technologies"),
    )),
    ("바이오·비만·ADC", (
        ("LLY", "일라이릴리", "Eli Lilly"), ("NVO", "노보노디스크", "Novo Nordisk"),
        ("VRTX", "버텍스", "Vertex Pharmaceuticals"), ("REGN", "리제너론", "Regeneron"),
        ("AMGN", "암젠", "Amgen"), ("ABBV", "애브비", "AbbVie"), ("MRNA", "모더나", "Moderna"),
    )),
    ("2차전지·전기차", (
        ("TSLA", "테슬라", "Tesla"), ("ALB", "앨버말", "Albemarle"),
        ("QS", "퀀텀스케이프", "QuantumScape"), ("RIVN", "리비안", "Rivian"),
        ("LCID", "루시드", "Lucid"), ("ENPH", "엔페이즈", "Enphase"),
        ("FSLR", "퍼스트솔라", "First Solar"),
    )),
    ("크립토·거래", (
        ("COIN", "코인베이스", "Coinbase"), ("MSTR", "스트래티지", "MicroStrategy"),
        ("MARA", "마라", "MARA Holdings"), ("RIOT", "라이엇", "Riot Platforms"),
        ("HOOD", "로빈후드", "Robinhood"),
    )),
    ("엔터·미디어", (
        ("NFLX", "넷플릭스", "Netflix"), ("DIS", "디즈니", "Disney"), ("SPOT", "스포티파이", "Spotify"),
        ("RBLX", "로블록스", "Roblox"), ("PARA", "파라마운트", "Paramount"),
    )),
    ("금융·은행", (
        ("JPM", "JP모건", "JPMorgan"), ("BAC", "뱅크오브아메리카", "Bank of America"),
        ("GS", "골드만삭스", "Goldman Sachs"), ("MS", "모건스탠리", "Morgan Stanley"),
        ("C", "씨티그룹", "Citigroup"), ("WFC", "웰스파고", "Wells Fargo"),
        ("SCHW", "찰스슈왑", "Charles Schwab"),
    )),
    ("에너지·원유", (
        ("XOM", "엑슨모빌", "Exxon"), ("CVX", "셰브론", "Chevron"),
        ("COP", "코노코필립스", "ConocoPhillips"), ("SLB", "SLB", "SLB"),
        ("OXY", "옥시덴탈", "Occidental"),
    )),
)

THEME_UNIVERSE = {"KR": KR_THEMES, "US": US_THEMES}
MARKET_LABELS = {"KR": "KOSPI", "US": "S&P500"}
BENCHMARK_SYMBOLS = {"KR": "^KS11", "US": "SPY"}
# 마지막 봉이 오늘 것이어도 정규장 마감 전이어야 '장중'이다(마감 뒤에는 완성된 당일 종가).
SESSION_CLOSE = {"KR": time(15, 30), "US": time(16, 0)}
MINIMUM_MEMBERS = 2
DEFAULT_LIMIT = 5
TELEGRAM_LIMIT = 3

# 순위 가중치: 상대강도(지수 대비) > 확산도(상승 종목 비율) > 거래대금 배수.
LEADERSHIP_WEIGHTS = {"market_excess_pct": 0.45, "breadth_pct": 0.30, "turnover_ratio": 0.25}
NEWS_LOOKUPS_PER_RUN = 6
NEWS_LOOKUPS_PER_THEME = 2

_HEADLINE_CACHE = {}


def member_metric(frame, today=None):
    """구성종목 하나의 마지막 봉 등락률·거래량 배수·거래대금 배수를 계산한다.

    마지막 봉이 `today`와 같은 날이면 장중 미완성 봉이며 `intraday=True`로 표시한다.
    값이 모자라면(기준 20일 미만) ValueError로 알리고 테마 집계에서 뺀다.
    """
    if frame is None:
        raise ValueError("구성종목 시세 없음")
    data = frame[["Close", "Volume"]].replace([float("inf"), float("-inf")], float("nan")).dropna(subset=["Close"])
    if data.empty:
        raise ValueError("구성종목 종가 없음")
    data = data.loc[~data.index.duplicated(keep="last")].sort_index()
    if len(data) < 22:
        raise ValueError("일봉 22개 미만")
    close = data["Close"].astype(float)
    volume = data["Volume"].fillna(0).astype(float)
    previous = float(close.iloc[-2])
    if previous <= 0:
        raise ValueError("직전 종가 0")
    last_date = data.index[-1]
    data_date = (last_date.date() if hasattr(last_date, "date") else last_date).isoformat()
    baseline_volume = float(volume.iloc[-21:-1].mean())
    turnover = close * volume
    baseline_turnover = float(turnover.iloc[-21:-1].mean())
    return {
        "return_pct": round((float(close.iloc[-1]) / previous - 1) * 100, 2),
        "volume_ratio": round(float(volume.iloc[-1]) / baseline_volume, 2) if baseline_volume > 0 else None,
        "turnover": float(turnover.iloc[-1]),
        "turnover_ratio": round(float(turnover.iloc[-1]) / baseline_turnover, 2) if baseline_turnover > 0 else None,
        "data_date": data_date,
        "intraday": bool(today is not None and data_date == today.isoformat()),
    }


def _frame(prices, symbol):
    if prices is None:
        return None
    try:
        frame = prices[symbol]
    except (KeyError, IndexError, TypeError):
        return None
    return None if getattr(frame, "empty", True) else frame


def theme_rows(market, themes, metrics, benchmark_return_pct=None):
    """테마별 집계. 등락률은 구성종목 동일가중, 거래대금 배수는 금액가중(합계/합계기준)이다."""
    rows = []
    for name, members in themes:
        aliases = {member[0]: (member[2] if len(member) > 2 else None) for member in members}
        present = [(symbol, label, metrics[symbol]) for symbol, label, *_ in members if symbol in metrics]
        if not present:
            continue
        returns = [metric["return_pct"] for _, _, metric in present]
        move = sum(returns) / len(returns)
        up = sum(1 for value in returns if value > 0)
        turnover = sum(metric["turnover"] for _, _, metric in present if metric.get("turnover"))
        baseline = sum(metric["turnover"] / metric["turnover_ratio"] for _, _, metric in present
                       if metric.get("turnover") and metric.get("turnover_ratio"))
        ratio = turnover / baseline if baseline > 0 else None
        leaders = sorted(present, key=lambda item: (-item[2]["return_pct"], item[1]))[:3]
        excess = None if benchmark_return_pct is None else move - benchmark_return_pct
        state = ("주도" if move > 0 and (excess is None or excess > 0)
                 else "상대강세" if excess is not None and excess > 0 else "약세")
        rows.append({
            "market": market, "name": name,
            "return_pct": round(move, 2),
            "market_excess_pct": None if excess is None else round(excess, 2),
            "benchmark_return_pct": benchmark_return_pct,
            "up": up, "total": len(present), "declared": len(members),
            "breadth_pct": round(100 * up / len(present), 1),
            "turnover": turnover or None,
            "turnover_ratio": None if ratio is None else round(ratio, 2),
            "leaders": [dict(symbol=symbol, name=label, return_pct=metric["return_pct"],
                             alias=aliases.get(symbol))
                        for symbol, label, metric in leaders],
            "data_date": max((metric.get("data_date") or "" for _, _, metric in present), default="") or None,
            "state": state,
        })
    return rows


def _percentile_rank(rows, key):
    values = sorted(row[key] for row in rows if row.get(key) is not None)

    def rank(row):
        value = row.get(key)
        if value is None or not values:
            return 0.5  # 자료가 없는 기준은 중립으로 둔다
        below = sum(1 for candidate in values if candidate < value)
        same = sum(1 for candidate in values if candidate == value)
        return (below + 0.5 * same) / len(values)
    return rank


def rank_theme_sectors(rows, limit=DEFAULT_LIMIT, minimum_members=MINIMUM_MEMBERS):
    """상대강도·상승비율·거래대금 백분위 가중합으로 정렬한다. 지수가 빠져도 1등은 나온다."""
    ranked = [dict(row) for row in rows or [] if (row.get("total") or 0) >= minimum_members]
    if not ranked:
        return []
    ranks = {key: _percentile_rank(ranked, key) for key in LEADERSHIP_WEIGHTS}
    for row in ranked:
        row["leadership_score"] = round(
            sum(weight * ranks[key](row) for key, weight in LEADERSHIP_WEIGHTS.items()), 4)
    ranked.sort(key=lambda row: (-row["leadership_score"],
                                 -(row["market_excess_pct"] if row["market_excess_pct"] is not None else -999),
                                 row["name"]))
    return ranked[:limit]


def _clean_headline(text, limit=64):
    text = " ".join(unescape(str(text or "")).split())
    # 매체가 붙이는 말미의 [오늘, 이슈] 꼬리표는 문장을 자르기 전에 뺀다(공급사가 중간에 끊은 대괄호 포함).
    trimmed = re.sub(r"\s*\[[^\]]*\]?$", "", text).strip()
    if len(trimmed) >= 12:
        text = trimmed
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _mentions(headline, symbol, alias):
    """헤드라인이 그 종목을 실제로 가리키는지 확인한다(관련 뉴스 목록의 우연한 혼입 제거)."""
    text = str(headline or "").casefold()
    if alias and alias.casefold() in text:
        return True
    return bool(symbol) and f" {symbol.casefold()} " in f" {text} "


def _yahoo_articles(symbol, now, alias=None):
    """미국 종목 뉴스: 기존 수집기와 같은 yfinance 뉴스 소스를 24시간 창으로 읽는다."""
    import yfinance as yf
    articles = []
    for item in (yf.Ticker(symbol).get_news(count=10) or []):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, dict):
            title = content.get("title")
            stamp = content.get("pubDate") or content.get("displayTime")
            link = (content.get("canonicalUrl") or {}).get("url")
        else:
            title = item.get("title") if isinstance(item, dict) else None
            stamp = item.get("providerPublishTime") if isinstance(item, dict) else None
            link = item.get("link") if isinstance(item, dict) else None
        published = None
        if isinstance(stamp, (int, float)):
            published = datetime.fromtimestamp(stamp, timezone.utc)
        elif isinstance(stamp, str) and stamp:
            try:
                published = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                published = None
        if not title or published is None or published.utcoffset() is None:
            continue
        if not now - timedelta(hours=24) <= published < now:
            continue
        if not _mentions(title, symbol, alias):
            continue
        articles.append(dict(headline=str(title), available_at=published.isoformat(), url=link))
    return sorted(articles, key=lambda row: row["available_at"], reverse=True)


def _headlines(market, symbol, name, now, alias=None):
    """기존 뉴스 수집기 재사용: 국내는 종목 뉴스 API, 미국은 Yahoo 뉴스."""
    key = (market, symbol, now.date().isoformat())
    if key in _HEADLINE_CACHE:
        return _HEADLINE_CACHE[key]
    try:
        if market == "KR":
            from issuer_news import issuer_news_result
            articles = issuer_news_result(symbol.split(".")[0], name, now).get("articles") or []
        else:
            articles = _yahoo_articles(symbol, now, alias)
    except Exception:  # 뉴스 실패가 섹터 보고를 막지 않는다
        articles = []
    _HEADLINE_CACHE[key] = articles
    return articles


def issue_line(row, market, now, headlines=None, budget=None):
    """테마 이슈 한 줄: 대표주 당일 헤드라인이 있으면 그 문장, 없으면 수급·상승 요약."""
    fetch = headlines or _headlines
    for leader in (row.get("leaders") or [])[:NEWS_LOOKUPS_PER_THEME]:
        if budget is not None and budget[0] <= 0:
            break
        if budget is not None:
            budget[0] -= 1
        try:
            articles = fetch(market, leader["symbol"], leader["name"], now, leader.get("alias")) or []
        except Exception:
            articles = []
        if articles:
            newest = max(articles, key=lambda item: str(item.get("available_at") or ""))
            return f"이슈: {_clean_headline(newest.get('headline'))}"
    ratio = row.get("turnover_ratio")
    supply = f"상승 {row.get('up', 0)}/{row.get('total', 0)}종목"
    if ratio:
        supply += f" · 거래대금 {ratio:.1f}배"
    return f"이슈: 뉴스 미확인 · {supply} 수급"


def _download(symbols, market, downloader):
    if downloader is not None:
        return downloader(symbols, market)
    from scanner import yf_download
    return yf_download(symbols, period="2mo", interval="1d", prepost=market == "US",
                       auto_adjust=False, group_by="ticker", progress=False, threads=8,
                       timeout=12, multi_level_index=True)


def theme_sector_snapshot(market, now, limit=DEFAULT_LIMIT, downloader=None,
                          headlines=None, with_news=True):
    """한 시장의 세부 테마 스냅샷. 시세가 없으면 지어내지 않고 error로 남긴다."""
    market = "US" if str(market).upper().startswith("US") else "KR"
    themes = THEME_UNIVERSE[market]
    symbols = [member[0] for _, members in themes for member in members]
    benchmark = BENCHMARK_SYMBOLS[market]
    snapshot = dict(market=market, benchmark=benchmark, benchmark_return_pct=None,
                    data_date=None, intraday=False, rows=[], quoted=0,
                    members=len(set(symbols)), source="theme_member_daily_aggregate",
                    error=None, checked_at=now.isoformat())
    try:
        prices = _download(list(dict.fromkeys([*symbols, benchmark])), market, downloader)
    except Exception as exc:  # 테마 집계 실패가 알림 자체를 막지 않는다
        print(f"{market} theme sector download failed ({type(exc).__name__})")
        prices = None
    if prices is None:
        snapshot["error"] = "시세 미확보"
        return snapshot
    today = now.date()
    metrics = {}
    for symbol in dict.fromkeys(symbols):
        try:
            metric = member_metric(_frame(prices, symbol), today)
        except (ValueError, TypeError, KeyError):
            continue
        metrics[symbol] = metric
    snapshot["quoted"] = len(metrics)
    try:
        snapshot["benchmark_return_pct"] = member_metric(_frame(prices, benchmark), today)["return_pct"]
    except (ValueError, TypeError, KeyError):
        snapshot["benchmark_return_pct"] = None
    ranked = rank_theme_sectors(theme_rows(market, themes, metrics, snapshot["benchmark_return_pct"]), limit)
    if with_news and ranked:
        budget = [NEWS_LOOKUPS_PER_RUN]
        for row in ranked:
            row["issue"] = issue_line(row, market, now, headlines, budget)
    dates = [row.get("data_date") for row in ranked if row.get("data_date")]
    snapshot["data_date"] = max(dates) if dates else None
    snapshot["intraday"] = bool(snapshot["data_date"] == today.isoformat()
                                 and now.time() < SESSION_CLOSE[market])
    snapshot["rows"] = ranked
    if not ranked:
        snapshot["error"] = "테마 구성종목 시세 부족"
    return snapshot


def format_theme_block(snapshot, market=None, limit=4):
    """`세부 테마` 블록: 테마 등락·거래대금·상승비율, 대표주 등락, 이슈 한 줄."""
    snapshot = snapshot or {}
    rows = (snapshot.get("rows") or [])[:limit]
    if not rows:
        return [f"세부 테마: {snapshot['error']}"] if snapshot.get("error") else []
    market = market or snapshot.get("market")
    label = MARKET_LABELS.get(market, market or "시장")
    stamp = "장중" if snapshot.get("intraday") else (
        snapshot["data_date"][5:].replace("-", "/") + " 종가" if snapshot.get("data_date") else "직전 세션")
    head = f"세부 테마 · {stamp}"
    if snapshot.get("benchmark_return_pct") is not None:
        head += f" · {label} {snapshot['benchmark_return_pct']:+.2f}% 대비 상대강도"
    lines = [head]
    for index, row in enumerate(rows, 1):
        parts = [f"{row.get('return_pct', 0):+.1f}%"]
        if row.get("turnover_ratio"):
            parts.append(f"거래대금 {row['turnover_ratio']:.1f}배")
        if row.get("total"):
            parts.append(f"상승 {row.get('up', 0)}/{row['total']}")
        if row.get("state") == "상대강세":
            parts.append("지수 대비 강세")
        lines.append(f"{index}. {row['name']} " + " · ".join(parts))
        leaders = row.get("leaders") or []
        if leaders:
            # 상승 종목이 절반을 넘을 때만 '주도'라고 쓴다. 아니면 덜 빠진 종목의 나열이다.
            lead = "주도" if row.get("up", 0) > (row.get("total") or 0) / 2 else "상대 선두"
            body = " · ".join(f"{leader['name']} {leader['return_pct']:+.1f}%" for leader in leaders[:3])
            lines.append(f"   {lead} {body}")
        if row.get("issue"):
            lines.append(f"   {row['issue']}")
    return lines


def theme_lines(snapshots, limit=TELEGRAM_LIMIT):
    """여러 시장 스냅샷을 메시지용 줄로 만든다(시장마다 자기 블록)."""
    lines = []
    for snapshot in snapshots or []:
        if isinstance(snapshot, dict):
            lines.extend(format_theme_block(snapshot, snapshot.get("market"), limit))
    return lines


def sector_section(etf_rows, snapshot, market=None, override=None, limit=4):
    """`오늘의 주요 섹터` 머리줄은 유지하고, 그 아래를 세부 테마 블록으로 채운다.

    세부 테마 자료가 없으면 기존 `format_sector_header`(1위/2위 ETF 또는 확인 불가)를
    그대로 돌려주므로 기존 호출부와 계약 테스트는 바뀌지 않는다.
    """
    from sector_flow import format_sector_header
    base = list(override) if override else format_sector_header(etf_rows, market)
    detail = format_theme_block(snapshot, market, limit) if snapshot else []
    if detail and base and base[-1] == '확인 불가':
        # 세부 테마 자료가 있으면 대분류 ETF의 '확인 불가' 자리표시자를 남기지 않는다.
        base = base[:-1]
    return base + detail
