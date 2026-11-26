"""증권사 리서치식 세부 테마·이슈 섹터: 실제 구성종목의 거래대금 유입으로 순위를 매긴다.

왜 필요한가: `SECTOR_ETFS`의 15개 대분류 ETF는 `return>0 + 거래대금 2배` 조건과 함께 쓰이면
지수가 빠지는 날 전부 음수라 아무것도 통과하지 못하고, 메시지는 `1위 은행` 한 단어나
`확인 불가`로 끝났다. 그런 날에도 로봇·방산처럼 수급이 쏠린 세부 테마는 존재한다.

순위 기준(2026-11-26 지시: 증권사 `지금 섹터는?`처럼 "등락률이 아니라 거래대금이 몰리는 순서"):
이 모듈은 테마별 실제 구성종목의 일봉에서
  - 구성종목 거래대금 합계(누적 거래대금 규모)와 20일 평균 대비 배수(자금 유입 강도),
  - 장중이면 경과시간으로 보정한 거래대금 페이스(유입 속도),
  - 테마 등락률(동일가중 평균)·상승 종목 비율(확산도)·지수 대비 초과등락
를 계산해 **거래대금 유입(급증 배수 40% + 규모 30%)을 핵심으로, 등락률·확산도를 보조(각 15%)**로
한 백분위 가중합으로 정렬한다. 등락률이 뒤로 밀린 대신 `state`(주도/상대강세/약세)로 방향을
남겨 하락장의 상대강세를 상승으로 오해하지 않게 하고, 하락만 하는 투매 섹터(등락률 -3% 이하이면서
하락 종목이 과반)는 `dumping`으로 표시해 뒤로 보낸다.

한 종목은 여러 테마에 동시에 속한다(삼성전기 = `FC-BGA·PCB·기판` + `AI서버·데이터센터 부품`).
테마는 자기 구성종목만 집계하므로 중복 포함이 테마 등락·거래대금을 왜곡하지 않고, 시세는
한 번만 받는다(`dict.fromkeys`).

고정 바스켓 밖의 종목은 거래대금이 몰려도 테마가 없다. 그래서 호출부가 넘긴
"고정 바스켓 밖 거래대금 상위" 지표(`extra_quotes`)와 이미 수집한 헤드라인으로
`dynamic_theme_clusters`가 이슈 라벨 클러스터를 만든다 — 이슈 키워드 표에 있는 말만 뽑는
결정적 규칙이라 같은 입력이면 같은 클러스터가 나오고, 뉴스 조회가 실패하면 클러스터가
비어 있을 뿐 섹터 블록은 그대로다.

각 상위 테마에는 대표 상승주 2~3개와 그 등락률, 그리고 기존 뉴스 수집기
(`issuer_news.issuer_news_result` - 국내 종목 뉴스, `yfinance` - 미국 종목 뉴스)에서 가져온
당일 헤드라인 한 줄을 붙인다. 뉴스가 없으면 수급·상승 요약으로 대체하며 이유를 지어내지 않는다.

집계에 넣기 전에 시세 자체를 검사한다(`price_sanity`). PARA +9,614%처럼 하루 등락이 가격제한을
넘는 값은 티커가 바뀐 종목의 가격이 섞인 데이터 오류라서, 그 행을 빼지 않으면 테마 등락률·
대표주·상승 비율이 전부 오염된다. 뺀 행은 `excluded`에 사유와 함께 남긴다.
"""

import re
import unicodedata
from datetime import datetime, time, timedelta, timezone
from html import unescape

import price_sanity
from theme_universe_additions import KR_ADDITIONS, US_ADDITIONS

# 테마 구성종목은 상장 종목코드(한국)와 티커(미국)로 고정한다. 이름은 종목 뉴스 수집기의
# 엔티티 매칭에 그대로 쓰이므로 상장명과 같아야 한다.
BEAUTY_BRANDS = (
    ("278470.KS", "에이피알"), ("090430.KS", "아모레퍼시픽"), ("051900.KS", "LG생활건강"),
    ("123690.KS", "한국화장품"), ("214420.KS", "토니모리"), ("078520.KS", "에이블씨엔씨"),
)
BEAUTY_SUPPLY_CHAIN = (
    ("192820.KS", "코스맥스"), ("161890.KS", "한국콜마"), ("241710.KQ", "코스메카코리아"),
    ("003350.KS", "한국화장품제조"), ("257720.KQ", "실리콘투"),
)
KR_THEMES = (
    ("화장품·K뷰티", BEAUTY_BRANDS + BEAUTY_SUPPLY_CHAIN),
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
        # 2026-11-26: 증권사 거래대금 상위 섹터에 FC-BGA로 오르는데 이 바스켓에 없던 두 종목.
        ("323280.KQ", "태성"), ("090460.KS", "비에이치"),
    )),
    # 한 종목이 여러 테마에 속한다(2026-11-26 지시). 삼성전기·이수페타시스·대덕전자는
    # `FC-BGA·PCB·기판`과 여기 양쪽에 있고, 한미반도체는 `HBM·메모리 반도체`와 겹친다.
    # 같은 종목이 기판 수요와 AI 서버 증설 양쪽에서 거래대금을 받기 때문이다.
    ("AI서버·데이터센터 부품", (
        ("009150.KS", "삼성전기"), ("007660.KS", "이수페타시스"), ("353200.KS", "대덕전자"),
        ("058470.KQ", "리노공업"), ("403870.KQ", "HPSP"), ("042700.KS", "한미반도체"),
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
    ("AI 소프트웨어", (
        ("018260.KS", "삼성에스디에스"), ("304100.KQ", "솔트룩스"), ("047560.KQ", "이스트소프트"),
        ("108860.KQ", "셀바스AI"), ("079940.KQ", "가비아"),
    )),
    # 보안은 증권사 화면에서 독립 섹터로 오른다(2026-11-26 10:45 거래대금 1위 '보안주·사이버보안').
    # AI 소프트웨어와 섮여 있으면 테마 평균이 희석되고 이름도 안 맞는다.
    ("보안·사이버보안", (
        ("053800.KQ", "안랩"), ("263860.KQ", "지니언스"), ("150900.KQ", "파수AI"),
        ("067920.KQ", "이글루"),
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
        ("RBLX", "로블록스", "Roblox"), ("CMCSA", "컴캐스트", "Comcast"),
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

KR_THEMES += KR_ADDITIONS
US_THEMES += US_ADDITIONS
THEME_UNIVERSE = {"KR": KR_THEMES, "US": US_THEMES}
MARKET_LABELS = {"KR": "KOSPI", "US": "S&P500"}
BENCHMARK_SYMBOLS = {"KR": "^KS11", "US": "SPY"}
# 마지막 봉이 오늘 것이어도 정규장 마감 전이어야 '장중'이다(마감 뒤에는 완성된 당일 종가).
SESSION_CLOSE = {"KR": time(15, 30), "US": time(16, 0)}
SESSION_OPEN = {"KR": time(9, 0), "US": time(9, 30)}
SESSION_MINUTES = {"KR": 390, "US": 390}
MINIMUM_MEMBERS = 2
DEFAULT_LIMIT = 5
TELEGRAM_LIMIT = 3

# 순위 가중치(2026-11-26 지시: "등락률이 아니라 거래대금 유입·급증 속도로"):
#   거래대금 배수(평소 대비 얼마나 빨리 몰리나) 40% + 거래대금 규모(얼마가 몰랐나) 30%가 핵심,
#   등락률·확산도는 각 15%의 보조. 증권사 `지금 섹터는?`의 `최근 3분 +53.3억 상승`처럼
#   돈이 들어오는 순서를 먼저 본다.
LEADERSHIP_WEIGHTS = {"turnover_ratio": 0.40, "turnover": 0.30,
                      "return_pct": 0.15, "breadth_pct": 0.15}
# 장중은 당일 누적 거래대금을 경과시간으로 나눈 페이스(유입 속도)를 배수 자리에 넣는다.
# 종전에는 장중에도 전일 종가 기준 순위를 그대로 찍었다(2026-11-26 지시). 장전은 전일
# 마감 기준 LEADERSHIP_WEIGHTS 그대로다(장전 = 전일 마감 / 장중 = 당일 실시간 분리는 유지).
INTRADAY_LEADERSHIP_WEIGHTS = {"turnover_pace_ratio": 0.40, "turnover": 0.30,
                               "return_pct": 0.15, "breadth_pct": 0.15}
# 유입 점수만 따로 낼 때의 비중(메시지·기록에서 `거래대금 유입` 순위를 보여 준다).
INFLOW_WEIGHTS = {"turnover_ratio": 0.60, "turnover": 0.40}
INTRADAY_INFLOW_WEIGHTS = {"turnover_pace_ratio": 0.60, "turnover": 0.40}
# 하락만 하는 과매도·투매 섹터를 거르는 최소 필터: 등락률이 이 값 이하이고 하락 종목이
# 과반이면 매수 유입이 아니라 매도 소화다. 점수를 지우지 않고 뒤로만 보낸 뒤 `dumping`으로 남긴다.
DUMPING_RETURN_PCT = -3.0
# 동적 이슈 클러스터: 고정 바스켓 밖 거래대금 상위 종목을 헤드라인 키워드로 묶는다.
DYNAMIC_CLUSTER_LIMIT = 2
DYNAMIC_CLUSTER_MINIMUM = 2
DYNAMIC_CLUSTER_LOOKUPS = 8
# 헤드라인에서 뽑는 이슈 라벨. 임의 명사 추출 대신 이 표의 말만 본다 — 결정적이고,
# "수주"·"장비"처럼 어디에나 나오는 말은 넣지 않는다(모든 종목이 한 클러스터로 빨려 든다).
ISSUE_KEYWORDS = (
    ("HBM·메모리", ("HBM", "고대역폭", "CXL", "D램", "낸드")),
    ("AI서버·부품", ("AI 서버", "AI서버", "데이터센터", "데이터 센터", "GPU", "액체냉각")),
    ("로봇", ("휴머노이드", "로봇", "액추에이터")),
    ("2차전지·전고체", ("전고체", "2차전지", "양극재", "배터리")),
    ("전력·변압기", ("변압기", "전력망", "전력기기", "송전")),
    ("원전·SMR", ("원전", "SMR", "소형모듈원자로")),
    ("방산·수출", ("방산", "방위산업", "국방")),
    ("화장품·K뷰티", ("화장품", "K뷰티", "K-뷰티", "cosmetics")),
    ("자율주행·전장", ("자율주행", "라이다", "ADAS")),
    ("통신장비", ("통신장비", "광트랜시버", "5G", "6G")),
    ("태양광·풍력", ("태양광", "해상풍력", "풍력발전")),
    ("수소·연료전지", ("수소 생산", "수소연료전지", "수전해")),
    ("유리기판", ("유리기판", "글라스기판", "glass substrate")),
    ("게임", ("게임 출시", "신작 게임", "게임 판호")),
    ("K푸드", ("K푸드", "K-푸드", "불닭볶음면")),
    ("리츠", ("리츠", "REIT")),
    ("금속·희토류", ("희토류", "구리 가격", "광산 개발")),
    ("의료기기", ("의료기기", "미용장비")),
    ("ESS·전력저장", ("에너지저장장치", "전력저장", "ESS")),
    ("파운드리·시스템반도체", ("파운드리", "시스템반도체", "팹리스")),
    ("우주·위성", ("위성 통신", "위성 발사", "우주발사체")),
    ("디스플레이·OLED", ("OLED", "마이크로LED", "폴더블 디스플레이")),
    ("전선·전력망", ("초고압 케이블", "송전망", "HVDC")),
    ("바이오·임상", ("임상", "FDA", "비만", "ADC", "품목허가")),
    ("보안", ("보안", "해킹", "랜섬웨어")),
    ("조선·LNG", ("조선", "LNG선", "LNG 운반")),
    ("기판·FC-BGA", ("FC-BGA", "기판", "MLB")),
)
NEWS_LOOKUPS_PER_RUN = 6
NEWS_LOOKUPS_PER_THEME = 2
# 기사가 없을 때 이슈 줄 대신 붙는 수급 요약의 머리표. 메시지에서는 위 테마 줄과 숫자가
# 겹치므로 출력하지 않고, 기록에는 그대로 남긴다.
ISSUE_UNCONFIRMED = "이슈: 뉴스 미확인"

_HEADLINE_CACHE = {}


def theme_membership(market):
    """종목코드 → 그 종목이 속한 테마 이름들. 한 종목이 여러 테마에 속할 수 있다.

    2026-11-26 지시(증권사 화면은 한 종목을 여러 섹터에 중복으로 넣는다)에 따라 테마 정의는
    종목 중복을 허용한다. 집계는 테마별 자기 구성종목만 보므로 중복이 등락률·거래대금을
    두 번 더하지 않고, 어느 테마가 같은 종목을 공유하는지는 여기서 확인한다.
    """
    themes = THEME_UNIVERSE["US" if str(market).upper().startswith("US") else "KR"]
    membership = {}
    for name, members in themes:
        for member in members:
            membership.setdefault(member[0], []).append(name)
    return {symbol: tuple(names) for symbol, names in membership.items()}


def outside_basket_quotes(results, market, limit=12):
    """고정 테마 바스켓에 없는 종목의 당일 거래대금·등락 지표(동적 클러스터 입력).

    스캔 결과(`scanner.scan_market`의 지표)에서 거래대금(종가×거래량)과 20일 평균 배수를
    꺼낸다. 새 API를 부르지 않고, 값이 모자란 행·ETF·이미 바스켓에 있는 종목은 뺀다.
    테마 집계에는 들어가지 않고 동적 클러스터의 후보로만 쓰인다.
    """
    market = "US" if str(market).upper().startswith("US") else "KR"
    basket = theme_membership(market)
    quotes = []
    for row in results or []:
        if row.get("market") != market or row.get("asset_type") not in ("stock_or_etf", "stock"):
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in basket:
            continue
        values = row.get("indicators") or {}
        price, volume = values.get("price"), values.get("volume")
        baseline = values.get("average_turnover_20d")
        move = values.get("return_1d")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in (price, volume, move)):
            continue
        turnover = float(price) * float(volume)
        if turnover <= 0:
            continue
        quotes.append({
            "symbol": symbol, "name": row.get("name") or symbol,
            "return_pct": round(float(move) * 100, 2),
            "turnover": turnover,
            "turnover_ratio": (round(turnover / float(baseline), 2)
                               if isinstance(baseline, (int, float)) and not isinstance(baseline, bool)
                               and baseline > 0 else None),
            "data_date": values.get("data_date"), "intraday": False, "outside_basket": True,
        })
    quotes.sort(key=lambda quote: (-quote["turnover"], quote["symbol"]))
    return quotes[:limit]


def _session_stamps(index):
    """일봉 인덱스의 세션 날짜(ISO 문자열) 목록. 타임존이 있어도 날짜만 본다."""
    stamps = []
    for stamp in index:
        value = stamp.date() if hasattr(stamp, "date") else None
        if value is not None:
            stamps.append(value.isoformat())
    return stamps


def frame_sessions(frame):
    """그 시세가 가진 세션 날짜 집합."""
    if frame is None or getattr(frame, "empty", True):
        return set()
    try:
        return set(_session_stamps(frame.index))
    except (TypeError, ValueError, AttributeError):
        return set()


def benchmark_session_moves(frame):
    """기준지수의 세션별 등락률 표(%).

    테마 구성종목의 등락을 같은 날의 지수 등락과 견주려면 두 값이 **한 세션**에서 나와야 한다.
    2026-11-26 발송본은 11/25 구성종목 등락에 11/24 지수 등락(+6.32%)을 붙여 상대강도와
    `지수 대비 강세` 라벨을 다른 세션 기준으로 계산했다(Yahoo에 11/25 지수 봉이 없었다).
    여기서 만든 표를 기준으로 구성종목을 같은 세션에 맞춰 읽는다.
    """
    if frame is None:
        return {}
    data = frame[["Close"]].replace([float("inf"), float("-inf")], float("nan")).dropna(subset=["Close"])
    data = data.loc[~data.index.duplicated(keep="last")].sort_index()
    close = data["Close"].astype(float)
    close = close.loc[close > 0]
    if len(close) < 2:
        return {}
    moves = {}
    previous = None
    for stamp, value in close.items():
        day = stamp.date().isoformat() if hasattr(stamp, "date") else None
        if day is None:
            previous = value
            continue
        if previous is not None and previous > 0:
            moves[day] = round((float(value) / float(previous) - 1) * 100, 4)
        previous = value
    return moves


def last_common_session(frame, sessions):
    """구성종목의 마지막 봉 중 지수도 가진 세션(교집합의 최신). 없으면 None."""
    if not sessions:
        return None
    common = frame_sessions(frame) & set(sessions)
    return max(common) if common else None


def aligned_session(metrics):
    """구성종목 지표가 공유하는 기준 세션: 가장 많은 종목이 가진 세션, 동률이면 최신.

    한두 종목만 다른 날 봉을 가진 채로 테마 평균에 섞이면 등락률·상승비율·거래대금 배수가
    서로 다른 세션의 값을 더하게 된다. 기준 세션이 다른 종목은 집계에서 빼고 기록에 남긴다.
    """
    counts = {}
    for metric in (metrics or {}).values():
        day = (metric or {}).get("data_date")
        if day:
            counts[day] = counts.get(day, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]


def member_metric(frame, today=None, market=None, session=None):
    """구성종목 하나의 마지막 봉 등락률·거래량 배수·거래대금 배수를 계산한다.

    마지막 봉이 `today`와 같은 날이면 장중 미완성 봉이며 `intraday=True`로 표시한다.
    값이 모자라면(기준 20일 미만) ValueError로 알리고 테마 집계에서 뺀다.

    `session`을 주면 그 세션(포함)까지만 읽는다. 테마 스냅샷은 이 값으로 구성종목과
    기준지수를 같은 세션에 맞춘다.

    등락이 가격제한을 넘거나 종가가 극저가면 `price_sanity.PriceError`로 알린다. `market`을
    주지 않으면 시장별 가격제한·최저가 검사 없이 150% 상한만 본다(시장을 모르는 호출부).
    """
    if frame is None:
        raise ValueError("구성종목 시세 없음")
    data = frame[["Close", "Volume"]].replace([float("inf"), float("-inf")], float("nan")).dropna(subset=["Close"])
    if data.empty:
        raise ValueError("구성종목 종가 없음")
    data = data.loc[~data.index.duplicated(keep="last")].sort_index()
    if session:
        stamps = _session_stamps(data.index)
        data = data.loc[[day <= session for day in stamps]]
    if len(data) < 22:
        raise ValueError("일봉 22개 미만")
    close = data["Close"].astype(float)
    volume = data["Volume"].fillna(0).astype(float)
    previous = float(close.iloc[-2])
    if previous <= 0:
        raise ValueError("직전 종가 0")
    last = float(close.iloc[-1])
    return_pct = round((last / previous - 1) * 100, 2)
    reason = price_sanity.rejection_reason(market if market else "US", return_pct,
                                           last if market else None)
    if reason:
        raise price_sanity.PriceError(reason)
    last_date = data.index[-1]
    data_date = (last_date.date() if hasattr(last_date, "date") else last_date).isoformat()
    baseline_volume = float(volume.iloc[-21:-1].mean())
    turnover = close * volume
    baseline_turnover = float(turnover.iloc[-21:-1].mean())
    return {
        "return_pct": return_pct,
        "price": round(last, 4),
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


def theme_rows(market, themes, metrics, benchmark_return_pct=None, session_progress=None):
    """테마별 집계. 등락률은 구성종목 동일가중, 거래대금 배수는 금액가중(합계/합계기준)이다.

    `session_progress`(장중 경과 비율)를 주면 `turnover_pace_ratio`를 함께 낸다 — 아직
    반나절인 당일 누적 거래대금을 하루 평균으로 그대로 나누면 0.3배처럼 작게 보이고,
    장중 순위가 "거래대금 급증"을 놓친다(2026-11-26 지시).

    `metrics`를 직접 만들어 넘기는 호출부(과거 리포트 재계산 등)도 있으므로 시세 오류 행은
    여기서 한 번 더 거른다. 뺀 종목 수는 `excluded`로 남겨 조용히 사라지지 않게 한다.
    """
    rows = []
    for name, members in themes:
        aliases = {member[0]: (member[2] if len(member) > 2 else None) for member in members}
        present = []
        excluded = 0
        for symbol, label, *_ in members:
            metric = metrics.get(symbol)
            if metric is None:
                continue
            if price_sanity.rejection_reason(market, metric.get("return_pct"), metric.get("price")):
                excluded += 1
                continue
            present.append((symbol, label, metric))
        if not present:
            continue
        returns = [metric["return_pct"] for _, _, metric in present]
        move = sum(returns) / len(returns)
        up = sum(1 for value in returns if value > 0)
        turnover = sum(metric["turnover"] for _, _, metric in present if metric.get("turnover"))
        baseline = sum(metric["turnover"] / metric["turnover_ratio"] for _, _, metric in present
                       if metric.get("turnover") and metric.get("turnover_ratio"))
        ratio = turnover / baseline if baseline > 0 else None
        pace = (round(ratio / session_progress, 2)
                if ratio is not None and session_progress and 0.05 <= session_progress <= 1
                else None)
        leaders = sorted(present, key=lambda item: (-item[2]["return_pct"], item[1]))[:3]
        excess = None if benchmark_return_pct is None else move - benchmark_return_pct
        state = ("주도" if move > 0 and (excess is None or excess > 0)
                 else "상대강세" if excess is not None and excess > 0 else "약세")
        # 하락만 하는 과매도·투매는 매수 유입이 아니라 매도 소화다(등락률 -3% 이하 +
        # 하락 종목 과반). 점수를 지우지 않고 뒤로 보낸 뒤 이유를 남긴다.
        dumping = bool(move <= DUMPING_RETURN_PCT and up * 2 < len(present))
        rows.append({
            "market": market, "name": name,
            "return_pct": round(move, 2),
            "market_excess_pct": None if excess is None else round(excess, 2),
            "benchmark_return_pct": benchmark_return_pct,
            "up": up, "total": len(present), "declared": len(members),
            "excluded": excluded,
            "breadth_pct": round(100 * up / len(present), 1),
            "turnover": turnover or None,
            "turnover_ratio": None if ratio is None else round(ratio, 2),
            "turnover_pace_ratio": pace,
            # 순위에 실제로 쓰인 유입 배수(장중이면 페이스, 장전이면 평소 대비 배수).
            "inflow_multiple": pace if pace is not None else (None if ratio is None else round(ratio, 2)),
            "dumping": dumping,
            # 이 테마를 이루는 종목코드: 동적 클러스터가 '이미 보여 준 테마'인지 판단하고,
            # 같은 종목이 여러 테마에 속하는지 확인하는 데 쓴다.
            "symbols": [symbol for symbol, _, _ in present],
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


def rank_theme_sectors(rows, limit=DEFAULT_LIMIT, minimum_members=MINIMUM_MEMBERS,
                       weights=None):
    """거래대금 유입(급증 배수 + 규모) 백분위 가중합으로 정렬한다.

    2026-11-26 지시: 증권사 `지금 섹터는?`처럼 등락률이 아니라 **거래대금이 몰리는 순서**가
    먼저다. 배수(평소 대비 급증·장중 페이스) 40% + 거래대금 규모 30%가 핵심이고 등락률·
    확산도는 각 15%의 보조다. 유입만 따로 본 `inflow_score`도 함께 남겨, 화면에는
    "거래대금 순위"와 "등락률"을 구분해 읽을 수 있게 한다.

    하락만 하는 투매 섹터(`dumping`: 등락률 -3% 이하 + 하락 종목 과반)는 점수와 별개로
    뒤로 보낸다 — 거래대금이 커도 그 돈은 매수 유입이 아니다. 지수가 빠지는 날에는
    후보가 전부 투매일 수 있으므로 점수를 지우지 않는다(그 안에서 유입 순서는 그대로).
    """
    ranked = [dict(row) for row in rows or [] if (row.get("total") or 0) >= minimum_members]
    if not ranked:
        return []
    weights = weights or LEADERSHIP_WEIGHTS
    ranks = {key: _percentile_rank(ranked, key) for key in weights}
    inflow_weights = (INTRADAY_INFLOW_WEIGHTS if "turnover_pace_ratio" in weights
                      else INFLOW_WEIGHTS)
    inflow_ranks = {key: _percentile_rank(ranked, key) for key in inflow_weights}
    for row in ranked:
        row["leadership_score"] = round(
            sum(weight * ranks[key](row) for key, weight in weights.items()), 4)
        row["inflow_score"] = round(
            sum(weight * inflow_ranks[key](row) for key, weight in inflow_weights.items()), 4)
    ranked.sort(key=lambda row: (bool(row.get("dumping")), -row["leadership_score"],
                                 -row["inflow_score"],
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
    return f"{ISSUE_UNCONFIRMED} · {supply} 수급"


def headline_labels(headline):
    """헤드라인에서 이슈 라벨을 결정적으로 뽑는다(`ISSUE_KEYWORDS`의 말만 본다).

    임의 명사 추출을 하지 않는 이유: 같은 기사 묶음이면 언제나 같은 라벨이 나와야 테스트가
    되고, "수주"·"장비" 같은 범용어로 모든 종목이 한 클러스터에 빨려 들지 않게 하기 위해서다.
    """
    text = " ".join(unicodedata.normalize("NFKC", str(headline or "")).split()).casefold()
    if not text:
        return []
    labels = []
    for label, words in ISSUE_KEYWORDS:
        if any(
            re.search(r"(?<![a-z0-9])" + re.escape(word.casefold()) + r"(?![a-z0-9])", text)
            if word in {"ESS", "REIT", "5G", "6G"} else word.casefold() in text
            for word in words
        ):
            labels.append(label)
    return labels


def dynamic_theme_clusters(metrics, market, now, headlines=None, budget=None,
                           membership=None, limit=DYNAMIC_CLUSTER_LIMIT):
    """고정 바스켓 밖 거래대금 상위 종목을 헤드라인 이슈 라벨로 묶는다.

    2026-11-26 지시(증권사는 기본 테마 DB + 뉴스·공시 연관도로 동적으로 묶는다)의 구현.
    고정 바스켓 테마가 못 잡는 무리는 두 종류다 — 바스켓에 아예 없는 종목, 그리고 여러
    테마에 걸쳐 같은 재료로 움직이는 종목. 둘 다 그 종목의 **당일 헤드라인 키워드**로 잡는다.

    * 후보: `metrics` 중 거래대금이 큰 순서로 `DYNAMIC_CLUSTER_LOOKUPS`개(뉴스 조회 예산).
    * 묶음: 같은 라벨이 `DYNAMIC_CLUSTER_MINIMUM`종목 이상이면 클러스터.
    * 결정적: 정렬·라벨 추출에 난수·현재시각 의존이 없다. 뉴스 조회가 터지면 그 종목만 빠진다.
    * `outside_basket`: 바스켓에 없는 종목 수, `themes`: 이 클러스터가 걸친 고정 테마 이름.
    """
    metrics = metrics or {}
    membership = theme_membership(market) if membership is None else membership
    fetch = headlines or _headlines
    budget = [DYNAMIC_CLUSTER_LOOKUPS] if budget is None else budget
    ordered = sorted((symbol for symbol, metric in metrics.items()
                      if metric and (metric.get("turnover") or 0) > 0),
                     key=lambda symbol: (-(metrics[symbol].get("turnover") or 0), symbol))
    labels_by_symbol = {}
    for symbol in ordered[:DYNAMIC_CLUSTER_LOOKUPS]:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        metric = metrics[symbol] or {}
        try:
            articles = fetch(market, symbol, metric.get("name") or symbol, now) or []
        except Exception:  # 뉴스 실패가 섹터 블록을 막지 않는다
            articles = []
        if not articles:
            continue
        newest = max(articles, key=lambda item: str(item.get("available_at") or ""))
        labels = headline_labels(newest.get("headline"))
        if labels:
            labels_by_symbol[symbol] = labels[:2]
    grouped = {}
    for symbol, labels in labels_by_symbol.items():
        for label in labels:
            grouped.setdefault(label, []).append(symbol)
    rows = []
    for label, symbols in grouped.items():
        if len(symbols) < DYNAMIC_CLUSTER_MINIMUM:
            continue
        symbols = sorted(symbols)
        moves = [float(metrics[symbol].get("return_pct") or 0) for symbol in symbols]
        turnover = sum(float(metrics[symbol].get("turnover") or 0) for symbol in symbols)
        theme_names = sorted({name for symbol in symbols for name in membership.get(symbol, ())})
        move = sum(moves) / len(moves)
        rows.append({
            "label": label, "market": market, "symbols": symbols,
            "members": [dict(symbol=symbol, name=metrics[symbol].get("name") or symbol,
                             return_pct=moves[index],
                             outside_basket=symbol not in membership)
                        for index, symbol in enumerate(symbols)],
            "turnover": turnover or None,
            "return_pct": round(move, 2),
            "up": sum(1 for value in moves if value > 0), "total": len(symbols),
            "outside_basket": sum(1 for symbol in symbols if symbol not in membership),
            "themes": theme_names,
            "state": "주도" if move > 0 else "약세",
            "source": "headline_keyword",
        })
    rows.sort(key=lambda row: (-(row["turnover"] or 0), -row["total"], row["label"]))
    return rows[:limit]


def _download(symbols, market, downloader):
    if downloader is not None:
        return downloader(symbols, market)
    import pandas as pd
    from scanner import yf_download

    batches = []
    for start in range(0, len(symbols), 75):
        try:
            frame = yf_download(symbols[start:start + 75], period="2mo", interval="1d",
                                prepost=market == "US", auto_adjust=False, group_by="ticker",
                                progress=False, threads=8, timeout=12, multi_level_index=True)
        except Exception as exc:
            print(f"{market} theme price batch failed ({type(exc).__name__})", flush=True)
            continue
        if frame is not None and not frame.empty:
            batches.append(frame)
    return pd.concat(batches, axis=1) if batches else None


def session_progress(now, market):
    """정규장이 얼마나 지났는지(0~1). 개장 전·마감 뒤·휴장이면 None."""
    market = "US" if str(market).upper().startswith("US") else "KR"
    clock = now.time()
    if clock < SESSION_OPEN[market] or clock >= SESSION_CLOSE[market]:
        return None
    elapsed = ((clock.hour - SESSION_OPEN[market].hour) * 60
               + clock.minute - SESSION_OPEN[market].minute)
    return max(0.0, min(1.0, elapsed / SESSION_MINUTES[market]))


def theme_sector_snapshot(market, now, limit=DEFAULT_LIMIT, downloader=None,
                          headlines=None, with_news=True, mode=None, extra_quotes=None):
    """한 시장의 세부 테마 스냅샷. 시세가 없으면 지어내지 않고 error로 남긴다.

    `mode`는 어느 세션을 읽을지 정한다.

    * ``previous`` — **전일 마감(마지막 완료 세션)** 만 읽는다. 장전 알림은 이 값으로 보낸다
      (2026-11-26 지시: "장전에는 전날마감기준으로 섹터 분석해서 보내").
    * ``intraday`` — 당일 장중 봉을 포함해 실시간 등락·거래대금 페이스로 순위를 낸다.
    * ``None`` — 지금이 정규장이면 ``intraday``, 아니면 ``previous``가 아닌 **있는 그대로**
      (마감 뒤에는 당일 종가가 이미 완성본이라 그것을 쓴다).

    `extra_quotes`는 고정 바스켓 밖에서 거래대금이 몰린 종목의 지표다(호출부가 이미 가진
    스캔 지표·KIS 당일 거래대금 순위). 테마 집계에는 들어가지 않고 동적 이슈 클러스터의
    후보로만 쓰인다 — 바스켓에 없는 종목이 뉴스 재료로 같이 움직이는 것을 잡기 위해서다.
    """
    market = "US" if str(market).upper().startswith("US") else "KR"
    live = (mode == "intraday") if mode else (session_progress(now, market) is not None)
    if mode == "previous":
        live = False
    themes = THEME_UNIVERSE[market]
    symbols = [member[0] for _, members in themes for member in members]
    benchmark = BENCHMARK_SYMBOLS[market]
    snapshot = dict(market=market, benchmark=benchmark, benchmark_return_pct=None,
                    benchmark_date=None, data_date=None, intraday=False, rows=[], quoted=0,
                    mode=mode or ("intraday" if live else "latest"),
                    members=len(set(symbols)), source="theme_member_daily_aggregate",
                    excluded=[], missing_price_data=[], unaligned=[], error=None, checked_at=now.isoformat(),
                    dynamic_clusters=[], outside_quotes=0,
                    # 한 종목이 여러 테마에 속하는 공유 종목 수(중복 허용의 가시화).
                    shared_members=sum(1 for names in theme_membership(market).values()
                                       if len(names) > 1))
    try:
        prices = _download(list(dict.fromkeys([*symbols, benchmark])), market, downloader)
    except Exception as exc:  # 테마 집계 실패가 알림 자체를 막지 않는다
        print(f"{market} theme sector download failed ({type(exc).__name__})")
        prices = None
    if prices is None:
        snapshot["error"] = "시세 미확보"
        snapshot["missing_price_data"] = sorted(set(symbols))
        return snapshot
    today = now.date()
    # 장전(mode='previous')은 오늘(아직 끝나지 않은 세션) 봉을 읽지 않는다.
    through = (today - timedelta(days=1)).isoformat() if mode == "previous" else None
    # 기준지수의 세션별 등락: 구성종목의 기준 세션과 견줄 값. 지수가 세션을 건너뛰었으면
    # 그 사실을 따로 남기고 상대강도는 계산하지 않는다(아래).
    benchmark_moves = benchmark_session_moves(_frame(prices, benchmark))
    if through:
        benchmark_moves = {day: move for day, move in benchmark_moves.items() if day <= through}
    metrics = {}
    for symbol in dict.fromkeys(symbols):
        frame = _frame(prices, symbol)
        try:
            metric = member_metric(frame, today, market, session=through)
        except price_sanity.PriceError as exc:
            # 티커 혼동·액면병합·극저가: 테마 등락률·대표주·상승 비율에 넣지 않는다.
            snapshot["excluded"].append(dict(symbol=symbol, reason=exc.reason,
                                             label=price_sanity.REASON_LABELS.get(exc.reason)))
            print(f"{market} theme price error excluded: {symbol} ({exc.reason})", flush=True)
            continue
        except (ValueError, TypeError, KeyError):
            snapshot["missing_price_data"].append(symbol)
            continue
        metrics[symbol] = metric
    # 구성종목이 공유하는 기준 세션(가장 많은 종목이 가진 최신 세션)을 정하고, 그 세션 봉이
    # 없는 종목은 테마 평균에서 뺀다(다른 세션 값을 섞으면 등락률·상승비율이 어긋난다).
    reference = aligned_session(metrics)
    # 구성종목이 가진 세션들: 지수가 몇 세션 밀렸는지 세는 기준(지수에 구멍이 있으면 그 날은
    # 지수 쪽에 없으므로 지수 세션만으로는 셀 수 없다).
    member_sessions = sorted({metric.get("data_date") for metric in metrics.values()
                              if metric.get("data_date")})
    if reference:
        aligned = {symbol: metric for symbol, metric in metrics.items()
                   if metric.get("data_date") == reference}
        snapshot["unaligned"] = [dict(symbol=symbol, data_date=metric.get("data_date"))
                                 for symbol, metric in metrics.items()
                                 if metric.get("data_date") != reference]
        metrics = aligned
    snapshot["quoted"] = len(metrics)
    # 지수가 그 세션 봉을 갖고 있으면 같은 세션에서 상대강도를 낸다. 없으면(^KS11이 11/25를
    # 건너뛴 2026-11-26처럼) 지수의 마지막 세션을 적고 몇 세션 밀렸는지 남긴다 — 다른 세션
    # 등락을 '오늘 지수'로 붙이지 않고, 상대강도는 계산하지 않는다.
    if reference and reference in benchmark_moves:
        benchmark_date = reference
    elif reference and benchmark_moves:
        earlier = [day for day in benchmark_moves if day <= reference]
        benchmark_date = max(earlier) if earlier else None
    else:
        benchmark_date = None
    snapshot["benchmark_date"] = benchmark_date
    snapshot["benchmark_return_pct"] = (benchmark_moves.get(benchmark_date)
                                        if benchmark_date else None)
    snapshot["benchmark_aligned"] = bool(benchmark_date and reference == benchmark_date)
    snapshot["benchmark_lag_sessions"] = (
        sum(1 for day in member_sessions if benchmark_date < day <= reference)
        if benchmark_date and reference else 0)
    # 장중이면 당일 등락·거래대금 페이스로 순위를 낸다. 전일 마감 자료에는 페이스가 없다.
    progress = session_progress(now, market)
    # 장중 집계는 '지금 정규장이 돌고 있고, 기준 세션이 오늘'일 때만이다.
    intraday_now = bool(live and reference == today.isoformat() and progress is not None)
    excess_return = (snapshot["benchmark_return_pct"]
                     if snapshot.get("benchmark_aligned") else None)
    ranked = rank_theme_sectors(
        theme_rows(market, themes, metrics, excess_return,
                   session_progress=progress if intraday_now else None), limit,
        weights=INTRADAY_LEADERSHIP_WEIGHTS if intraday_now else LEADERSHIP_WEIGHTS)
    if with_news and ranked:
        budget = [NEWS_LOOKUPS_PER_RUN]
        for row in ranked:
            row["issue"] = issue_line(row, market, now, headlines, budget)
    dates = [row.get("data_date") for row in ranked if row.get("data_date")]
    snapshot["data_date"] = max(dates) if dates else None
    # '장중'은 시계가 정규장 안일 때만이다. 마감 뒤에 남은 당일 봉은 완성된 종가다.
    snapshot["intraday"] = bool(snapshot["data_date"] == today.isoformat()
                                 and progress is not None)
    snapshot["session_progress"] = progress
    if snapshot["intraday"]:
        snapshot["mode"] = "intraday"
    elif mode == "previous" or snapshot["data_date"] != today.isoformat():
        snapshot["mode"] = "previous"
    snapshot["rows"] = ranked
    if not ranked:
        snapshot["error"] = "테마 구성종목 시세 부족"
    # 동적 이슈 클러스터: 고정 바스켓 밖 거래대금 상위(extra_quotes)까지 같은 헤드라인
    # 키워드로 묶는다. 뉴스·추가 지표가 없으면 빈 목록이고, 실패해도 블록은 그대로다.
    if with_news and ranked:
        names = {member[0]: member[1] for _, members in themes for member in members}
        cluster_metrics = {symbol: dict(metric, name=names.get(symbol, symbol))
                           for symbol, metric in metrics.items()}
        for quote in extra_quotes or []:
            symbol = str((quote or {}).get("symbol") or "")
            if not symbol or symbol in cluster_metrics:
                continue
            session = quote.get("data_date")
            if reference and session and session != reference:
                continue
            cluster_metrics[symbol] = dict(quote)
        snapshot["outside_quotes"] = sum(1 for symbol in cluster_metrics if symbol not in names)
        try:
            snapshot["dynamic_clusters"] = dynamic_theme_clusters(
                cluster_metrics, market, now, headlines,
                budget=[DYNAMIC_CLUSTER_LOOKUPS], membership=theme_membership(market))
        except Exception as exc:  # 클러스터 실패가 섹터 블록을 막지 않는다
            print(f"{market} dynamic cluster failed ({type(exc).__name__})")
            snapshot["dynamic_clusters"] = []
    return snapshot


def _turnover_text(value, market):
    """거래대금을 읽는 사람 단위로: 국내는 억원, 미국은 백만 달러."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    if str(market).upper().startswith("US"):
        return f"${value / 1e6:,.0f}M"
    return f"{value / 1e8:,.0f}억"


def _previous_session(day, market):
    """그 날짜의 직전 거래 세션(XKRX/XNYS). 달력이 없으면 None."""
    if not day:
        return None
    try:
        from exchange_calendars import get_calendar
        calendar = get_calendar("XKRX" if str(market).upper() != "US" else "XNYS")
        return calendar.previous_session(str(day)).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _theme_stamp(snapshot, market):
    """`전일(11/25) 마감 기준` 또는 `장중 11/26 10:45 기준` — 어느 세션의 숫자인지 머리에 쓴다.

    장전 알림은 전일 마감, 장중 알림은 당일 실시간이다(2026-11-26 지시). 두 숫자가 같은
    메시지 안에서 섞이지 않도록 기준을 머리줄에 박아 둔다.
    """
    day = str(snapshot.get("data_date") or "")
    short = day[5:].replace("-", "/") if len(day) == 10 else None
    if snapshot.get("intraday"):
        clock = ""
        try:
            stamp = datetime.fromisoformat(str(snapshot.get("checked_at") or ""))
            clock = f" {stamp:%H:%M}"
        except (TypeError, ValueError):
            clock = ""
        return f"장중{clock} 기준" + (" · 당일 등락·거래대금" if snapshot.get("mode") == "intraday" else "")
    if short:
        # 마감 뒤에 돌린 스캔은 오늘 종가가 완성본이다. 그때는 '당일'이라고 쓴다.
        today = str(snapshot.get("checked_at") or "")[:10]
        if day == today:
            return f"당일({short}) 마감 기준"
        # '전일'은 직전 거래 세션일 때만이다. 지수가 세션을 건너뛰어 그보다 오래됐으면 '직전'.
        previous = _previous_session(today, market)
        if previous and day != previous:
            return f"직전({short}) 마감 기준"
        return f"전일({short}) 마감 기준"
    return "직전 세션 기준"


def _turnover_measure(row, market, live):
    """`거래대금 5,350억(4.4배)` — 규모와 유입 배수를 한 덩어리로 읽히게."""
    amount = _turnover_text(row.get("turnover"), market)
    multiple = row.get("turnover_pace_ratio") if live else None
    if multiple is None:
        multiple = row.get("inflow_multiple")
    if multiple is None:
        multiple = row.get("turnover_ratio")
    if amount:
        return f"거래대금 {amount}" + (f"({multiple:.1f}배)" if isinstance(multiple, (int, float)) else "")
    if isinstance(multiple, (int, float)):
        return f"거래대금 {multiple:.1f}배"
    return None


def _cluster_lines(snapshot, market, shown):
    """동적 이슈 클러스터 줄. 위 테마 줄이 이미 같은 종목 묶음을 보여 줬으면 싣지 않는다.

    고정 테마 하나에 그대로 들어가는 무리는 새 정보가 아니다. 클러스터가 여러 테마에
    걸치거나 바스켓 밖 종목을 포함할 때만 한 줄로 알린다.
    """
    shown_members = [set(row.get("symbols") or []) for row in shown]
    lines = []
    for cluster in snapshot.get("dynamic_clusters") or []:
        members = set(cluster.get("symbols") or [])
        if not members:
            continue
        if any(members <= block for block in shown_members if block):
            continue
        names = " · ".join(f"{member['name']} {member['return_pct']:+.1f}%"
                           for member in (cluster.get("members") or [])[:3])
        amount = _turnover_text(cluster.get("turnover"), market)
        lines.append(f"동적 이슈: {cluster.get('label')} — {names}"
                     + (f" · 거래대금 {amount}" if amount else ""))
        if len(lines) >= DYNAMIC_CLUSTER_LIMIT:
            break
    return lines


def format_theme_block(snapshot, market=None, limit=4):
    """`세부 테마` 블록: 거래대금 유입(규모·배수)·등락률·상승비율, 대표주, 이슈 한 줄."""
    snapshot = snapshot or {}
    rows = (snapshot.get("rows") or [])[:limit]
    if not rows:
        return [f"세부 테마: {snapshot['error']}"] if snapshot.get("error") else []
    market = market or snapshot.get("market")
    label = MARKET_LABELS.get(market, market or "시장")
    head = f"세부 테마 · {_theme_stamp(snapshot, market)} · 거래대금 유입 순"
    if snapshot.get("benchmark_return_pct") is not None:
        pct = snapshot["benchmark_return_pct"]
        if snapshot.get("benchmark_aligned", True):
            head += f" · {label} {pct:+.2f}% 대비 상대강도"
        else:
            # 지수가 그 세션 봉을 건너뛰었으면 그 날짜와 지연을 적는다(상대강도는 계산하지 않음).
            day = str(snapshot.get("benchmark_date") or "")[5:].replace("-", "/")
            lag = snapshot.get("benchmark_lag_sessions")
            head += (f" · {label} {day} {pct:+.2f}%"
                     + (f" (지수 {lag}세션 지연)" if isinstance(lag, int) and lag > 0 else ""))
    lines = [head]
    live = bool(snapshot.get("intraday"))
    for index, row in enumerate(rows, 1):
        # 거래대금(규모 + 유입 배수)을 먼저, 등락률을 뒤에 — 증권사 `지금 섹터는?`과 같은
        # 순서다(2026-11-26 지시). 배수는 장중이면 경과시간 보정 페이스, 장전이면 평소 대비다.
        parts = [part for part in (_turnover_measure(row, market, live),
                                   f"{row.get('return_pct', 0):+.1f}%") if part]
        if row.get("total"):
            parts.append(f"상승 {row.get('up', 0)}/{row['total']}")
        if row.get("dumping"):
            parts.append("투매")
        elif row.get("state") == "상대강세":
            parts.append("지수 대비 강세")
        lines.append(f"{index}. {row['name']} " + " · ".join(parts))
        leaders = row.get("leaders") or []
        if leaders:
            # 상승 종목이 절반을 넘을 때만 '주도'라고 쓴다. 아니면 덜 빠진 종목의 나열이다.
            lead = "주도" if row.get("up", 0) > (row.get("total") or 0) / 2 else "상대 선두"
            body = " · ".join(f"{leader['name']} {leader['return_pct']:+.1f}%" for leader in leaders[:3])
            lines.append(f"   {lead} {body}")
        if row.get("issue") and ISSUE_UNCONFIRMED not in str(row["issue"]):
            # 기사가 없을 때의 '상승 7/7종목 · 거래대금 0.6배' 요약은 바로 위 테마 줄과 같은
            # 숫자를 두 번 쓰는 것이다. 기록(`row['issue']`)은 남기되 메시지에는 싣지 않는다.
            lines.append(f"   {row['issue']}")
    lines.extend(_cluster_lines(snapshot, market, rows))
    return lines


def theme_lines(snapshots, limit=TELEGRAM_LIMIT):
    """여러 시장 스냅샷을 메시지용 줄로 만든다(시장마다 자기 블록)."""
    lines = []
    for snapshot in snapshots or []:
        if isinstance(snapshot, dict):
            lines.extend(format_theme_block(snapshot, snapshot.get("market"), limit))
    return lines


def sector_section(etf_rows, snapshot, market=None, override=None, limit=4):
    """세부 테마가 있으면 그 블록만, 없으면 예전 `오늘의 주요 섹터`(1위/2위 ETF)를 돌려준다.

    대분류 ETF 순위('1위 기계·장비')와 세부 테마는 같은 내용을 두 번 말한다. 세부 테마가
    계산되면 그것이 더 구체적인 같은 정보이므로, 대분류 머리줄은 자료가 없을 때의 폴백으로만
    남긴다(2026-11-24 지시: "옛날 섹터 중복 제거").
    """
    from sector_flow import format_sector_header
    detail = format_theme_block(snapshot, market, limit) if snapshot else []
    if detail:
        return detail
    return list(override) if override else format_sector_header(etf_rows, market)
