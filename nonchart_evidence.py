"""비차트 근거 — 공시 촉매 가산 · 리스크 공시 배제 · 수급 선행 관찰.

2026-11-27 「비차트 정보 통합 검증」(``notes/비차트-정보-통합-검증-2026-11-27.md``)의
10년(680종목, 1,351,173 판별일) 실측을 코드로 옮긴다. 결론은 한 줄이다 —
**비차트 정보는 "무엇이 곧 차트 게이트에 들어올지"는 잘 맞히지만, "들어온 뒤 더 버는지"는
거의 못 맞힌다.** 그래서 이 모듈은 차트 게이트(`selection`)를 대체하거나 완화하지 않는다.

* **공시 촉매**(최근 5세션 내 ``단일판매·공급계약``·``기술이전·라이선스``·``임상·승인``·
  ``자사주취득``): 게이트를 통과한 신호에 붙으면 10일 평균 +0.93% → +2.45%(초과 +0.18 →
  +1.50%p, 판단일 클러스터 CI 0 초과), 20일 +1.52% → +3.61%. 다만 연 23건이고 해별 부호가
  안정적이지 않아 **단독 매수 규칙이 아니라 텔레그램 우선순위·표시**로만 쓴다.
* **리스크 공시**(유상증자·CB·교환사채·감자, 횡령·배임·소송·의견거절·관리종목,
  최대주주변경·자사주처분): 전환 초입에서 10일 −1.28~−0.38%p, 승률 45%(기준 48%)로 나빠
  매수 후보에서 **배제**하고 그 사유를 기록한다.
* **수급**(외인+기관 5일 순매수 강도 ≥ 3%, 거래대금 대비 — ``supply_flow``와 같은 단위):
  평균수익은 바꾸지 못했지만(CI 0 포함), 비정배열에서 수급이 들어오면 10일 내 정배열 진입이
  26.4% → 43.8%였고 게이트 신호의 43.4%가 중앙값 2세션 전에 이 경고를 갖고 있었다. 하드
  필터로 쓰면 게이트 기대값이 −0.26%p 나빠지므로(CI −0.50~−0.03) **매수 필터가 아니라
  별도 관찰 블록**이다.
* **실적(잠정)공시**는 +6% 도달률이 4~8%p 낮아 촉매로 치지 않고, 주총·배당·주식선물·의결권
  같은 노이즈 공시도 어느 쪽으로도 읽지 않는다.

공시는 DART 키가 필요한 ``kis_intraday.dart_disclosures`` 대신 **네이버 종목 공시 이력**
(``m.stock.naver.com/api/stock/{code}/disclosure``)을 읽는다. 키 없이 동작하고 접수 시각을
그대로 주므로 판단 시각 이전에 공개된 공시만 쓸 수 있다(미래정보 없음).

이 모듈은 매수 후보를 만들지 않는다. 게이트가 이미 통과시킨 후보의 **표시 순서**(우선순위),
리스크 공시에 의한 **배제 사유**, 그리고 ``preopen_signal``·``buy_evaluation``을 건드리지 않는
**관찰 블록**만 담당한다.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
import math
import re
import time as clock
from zoneinfo import ZoneInfo

import requests

from candidate_selection import liquidity_rejection_reason
from research_asset import is_fund
import supply_flow

KST = ZoneInfo("Asia/Seoul")

# 최근 몇 거래일의 공시를 근거로 읽는가. 게이트 신호에 붙은 촉매공시의 효과는 5일 지평에서
# 가장 컸고(게이트 +0.47% → 촉매 +0.93%), 10일·20일로 갈수록 희석됐다.
WINDOW_SESSIONS = 5
# 5일 (외인+기관 순매수 금액) ÷ 같은 기간 거래대금 = ``supply_flow.score_rows``의
# ``net_to_turnover_pct``. 현행 supply_flow와 같은 단위이며, 3%는 검증에서 쓴 문턱이다.
MIN_SUPPLY_INTENSITY_PCT = 3.0
NAVER_DISCLOSURE = "https://m.stock.naver.com/api/stock/{code}/disclosure?pageSize={size}&page={page}"
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# 조회 한 건의 타임아웃과 전체 시간 예산. 현행 전 종목 스캔은 26분/한도 90분이므로, 수급·공시
# 조회를 더해도 여유가 남는다(연구 실측: 네이버 10스레드 초당 13~32건, 990종목 2,000회 ≈ 1~3분).
REQUEST_TIMEOUT = 8
TIME_BUDGET_SECONDS = 180
DEFAULT_WORKERS = 8
# 필터를 통과한 비정배열 종목은 보통 400여 개다. 상한은 그 위에 두되, 실행이 길어지면
# 시간 예산이 먼저 끊는다(끊긴 수는 요약에 남긴다).
MAX_SYMBOLS = 700

_MEASURED = "measured"
_UNAVAILABLE = "unavailable"

# 회사의 사건이 아닌 정기·제도 공시. 촉매도 리스크도 아니므로 어느 쪽으로도 읽지 않는다
# (검증 표본의 32.3%). '공시예고'는 실적 공시의 예고라 실적 공시 자체와 구분한다.
NOISE_KEYS = ("주식선물", "주식옵션", "의결권", "주주총회", "주식명의개서", "배당",
              "추가상장", "기업설명회", "공시예고", "실적공시예고", "결산실적공시예고",
              "주식분할", "주식병합", "전자투표")
# 실적 공시 — 촉매로 치지 않는다. +6% 도달률이 오히려 4~8%p 떨어졌다(변동성만 커진다).
EARNINGS_KEYS = ("영업(잠정)실적", "잠정실적", "결산실적", "잠정 영업실적")
# 리스크 공시 — 매수 후보 배제 사유. 제목 키워드 기반 분류(검증과 같은 방식).
# 유상증자·CB·감자는 발행 맥락을 따로 읽는다(`_is_dilution`).
RISK_DILUTION_LABEL = "유상증자·CB·감자"
RISK_CATEGORIES = (
    ("credibility", "횡령·배임·소송·의견거절",
     ("횡령", "배임", "의견거절", "관리종목", "소송", "불성실공시", "상장폐지",
      "투자주의환기종목", "압수수색", "검찰", "회생절차", "파산")),
    ("control", "최대주주 변경·자사주 처분",
     ("최대주주변경", "자기주식처분", "자사주처분", "주식담보")),
)
# 촉매 공시 — 강한 순서대로. 계약(수주)이 가장 강했고(게이트 +0.93% → +2.92%),
# 임상·승인·라이선스·자사주취득이 그 뒤를 잇는다.
CATALYST_ORDER = ("contract", "license", "clinical", "buyback")
CATALYST_LABELS = {"contract": "단일판매·공급계약", "license": "기술이전·라이선스",
                   "clinical": "임상·승인", "buyback": "자사주취득"}
CONTRACT_KEYS = ("단일판매", "수주")
LICENSE_KEYS = ("기술이전", "라이선스", "기술수출", "판권")
APPROVAL_WORDS = ("승인", "허가")
# 맨 '승인'은 임상·품목·의약 맥락에서만 촉매다. '주주총회 승인'·'정관 변경 승인' 같은
# 제도 공시를 촉매로 올리면 알림의 우선순위가 매일 틀어진다.
APPROVAL_CONTEXT = ("임상", "품목", "의약", "신약", "치료제", "식약처", "FDA", "백신", "진단", "특허")
BUYBACK_KEYS = ("자기주식취득", "자사주취득")
# 계약이 해지·취소·중단·철회된 공시는 촉매가 아니다(부호가 반대다). 계약 **정정** 공시는
# 금액이 바뀌었더라도 계약 사건의 갱신이라 촉매로 유지한다 — 검증 표본도 정정 공시를 포함해
# 효과(+2.92%)를 쟀고, 건설·조선은 정정이 원공시 못지않게 많다.
CONTRACT_CANCELLED = ("해지", "취소", "중단", "철회")
# '유동성공급계약'(시장조성)은 수주가 아니다 — '공급계약' 문자열이 들어 있어 따로 뺀다.
LIQUIDITY_SUPPLY = "유동성공급"

HEADING = "[수급 선행 관찰] · 매수 신호 아님"
NOTE = ("※ 정배열 전 단계에서 외인·기관이 먼저 들어온 이름 · 매수 계획·자동 주문 대상 아님 "
        "(10년 검증: 10일 내 정배열 진입 43.8%, 기준 26.4%)")
MARKET_LABELS = {"KR": "국내", "US": "미국"}
MAX_PER_MARKET = 5


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _aware(value):
    """A timezone-aware datetime, reading a naive stamp as Korean local time."""
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else value.replace(tzinfo=KST)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=KST)


def _decision_time(value):
    """판단 시각 — 시간대가 없으면 거부한다(9시간 어긋난 채로 공시 창을 재지 않게)."""
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("Timezone-aware decision time required")
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Timezone-aware decision time required") from None
    if parsed.utcoffset() is None:
        raise ValueError("Timezone-aware decision time required")
    return parsed


def _normalise(title):
    """Disclosure titles carry spaces and a middle dot inconsistently (``단일판매ㆍ공급계약``)."""
    return re.sub(r"[\s\u00a0]+", "", str(title or ""))


def classify(title):
    """공시 제목 하나를 촉매·리스크·실적·노이즈·기타로 나눈다.

    검증(``panel_nonchart.classify``)과 같은 제목 키워드 방식이되, 두 가지 오분류를 닫았다:
    ``유동성공급계약``(시장조성)은 수주가 아니고, 맨 ``승인``은 임상·품목 맥락에서만 촉매다.
    해지·취소·정정된 계약도 촉매가 아니다.
    """
    text = _normalise(title)
    if not text:
        return None
    if any(key in text for key in NOISE_KEYS):
        return dict(bucket="noise", kind=None, label=None)
    # 발행사가 상장한 ETN·상장지수증권의 상장폐지는 회사 자신의 상장폐지가 아니다
    # (2026-11-27 실측: ``미래에셋증권 상장지수증권 상장폐지(인버스 2X 옥수수 ETN)``가
    # 신용 리스크로 잘못 읽혔다). 상품의 만기·조기청산은 회사 사건이 아니므로 '기타'로 둔다.
    if "상장폐지" in text and ("상장지수증권" in text or "ETN" in text.upper()):
        return dict(bucket="other", kind=None, label=None)
    if _is_dilution(text):
        return dict(bucket="risk", kind="dilution", label=RISK_DILUTION_LABEL)
    for kind, label, keys in RISK_CATEGORIES:
        if any(key in text for key in keys):
            return dict(bucket="risk", kind=kind, label=label)
    if any(key.replace(" ", "") in text for key in EARNINGS_KEYS):
        return dict(bucket="earnings", kind="earnings", label="실적 공시")
    if _is_contract(text):
        return dict(bucket="catalyst", kind="contract", label=CATALYST_LABELS["contract"])
    if any(key in text for key in LICENSE_KEYS):
        return dict(bucket="catalyst", kind="license", label=CATALYST_LABELS["license"])
    if "임상" in text or _is_approval(text):
        return dict(bucket="catalyst", kind="clinical", label=CATALYST_LABELS["clinical"])
    if any(key in text for key in BUYBACK_KEYS):
        return dict(bucket="catalyst", kind="buyback", label=CATALYST_LABELS["buyback"])
    return dict(bucket="other", kind=None, label=None)


def _is_dilution(text):
    """희석·자본축소 공시인가.

    전환사채·교환사채는 **발행**이 희석이다. 회사가 자기 사채를 만기 전에 사들이는
    ``전환사채 만기전 취득 결정``은 부채를 줄이는 일이라 리스크로 읽지 않는다
    (2026-11-27 실측: 빛과전자가 이 문자열로 배제됐다).
    """
    if any(key in text for key in ("유상증자", "신주인수권", "제3자배정", "감자")):
        return True
    return ("전환사채" in text or "교환사채" in text) and "발행" in text


def _is_contract(text):
    if any(word in text for word in CONTRACT_CANCELLED):
        return False
    if any(key in text for key in CONTRACT_KEYS):
        return True
    return "공급계약" in text and LIQUIDITY_SUPPLY not in text


def _is_approval(text):
    return (any(word in text for word in APPROVAL_WORDS)
            and any(context in text for context in APPROVAL_CONTEXT))


def window_start(at, market="KR", sessions=WINDOW_SESSIONS):
    """최근 ``sessions`` 거래일 중 첫 세션의 시작 시각(그 시장 시간대)."""
    moment = _decision_time(at)
    zone = KST if market == "KR" else ZoneInfo("America/New_York")
    local = moment.astimezone(zone)
    try:
        import exchange_calendars
        import pandas as pd
        calendar = exchange_calendars.get_calendar("XKRX" if market == "KR" else "XNYS")
        session = calendar.date_to_session(pd.Timestamp(local.date().isoformat()),
                                           direction="previous")
        for _ in range(max(0, int(sessions) - 1)):
            session = calendar.previous_session(session)
        day = session.date() if hasattr(session, "date") else session
        return datetime.combine(day, time(0, 0), tzinfo=zone)
    except Exception:
        # 달력을 읽지 못하면 달력일로 넓게 잡는다. 휴장일을 모르므로 오래된 공시가 하나 더
        # 들어올 수 있지만, 창을 좁혀 최근 공시를 놓치는 쪽보다 안전하다.
        day = local.date() - timedelta(days=max(1, int(sessions)) + 2)
        return datetime.combine(day, time(0, 0), tzinfo=zone)


def naver_disclosures(code, start, at, size=100, max_pages=2, timeout=REQUEST_TIMEOUT,
                      get=requests.get):
    """네이버 종목 공시 이력 — 접수 시각·제목. 최신이 위에 온다.

    한 페이지(100건)가 5세션을 덮지 못하는 경우(대형주 공시 폭주)에만 다음 페이지를 읽는다.
    """
    rows = []
    for page in range(1, max(1, int(max_pages)) + 1):
        url = NAVER_DISCLOSURE.format(code=code, size=size, page=page)
        response = get(url, headers=NAVER_HEADERS, timeout=timeout)
        if getattr(response, "status_code", 0) != 200:
            break
        try:
            payload = response.json()
        except ValueError:
            break
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            if not isinstance(row, dict):
                continue
            when = _aware(row.get("datetime"))
            title = str(row.get("title") or "").strip()
            if when is None or not title:
                continue
            rows.append(dict(title=title, published_at=when,
                             disclosure_id=row.get("disclosureId"),
                             author=str(row.get("author") or "")))
        oldest = min((row["published_at"] for row in rows), default=None)
        if oldest is None or oldest < start or len(payload) < size:
            break
    return rows


def _default_fetch(code, start, at):
    return naver_disclosures(code, start, at)


def disclosure_evidence(code, at, market="KR", start=None, fetch=None):
    """한 종목의 최근 창 공시를 촉매/리스크/실적/노이즈로 나눈 근거.

    ``at`` 이전에 공개된 공시만 읽는다(발송 시각 이후 접수분은 다음 판단의 몫이다).
    읽지 못하면 ``status='unavailable'``로 남기고, 촉매 없음과 구분한다.
    """
    moment = _decision_time(at)
    if market != "KR":
        return dict(status=_UNAVAILABLE, market=market, code=code,
                    reason="no_disclosure_source_for_market",
                    meaning="공시 이력 원천이 국내 종목에만 있다")
    start = start or window_start(moment, market)
    try:
        rows = (fetch or _default_fetch)(code, start, moment)
    except Exception as exc:  # provider errors are recorded, never raised into the scan
        return dict(status=_UNAVAILABLE, market=market, code=code, observed_at=moment.isoformat(),
                    window_start=start.isoformat(), reason=type(exc).__name__,
                    meaning="공시 이력을 읽지 못해 촉매·리스크 미확인")
    published = [row for row in rows or []
                 if isinstance(row, dict) and row.get("published_at")
                 and start <= row["published_at"] <= moment]
    catalysts, risks = [], []
    earnings = noise = other = 0
    for row in published:
        verdict = classify(row.get("title"))
        if verdict is None:
            continue
        if verdict["bucket"] == "noise":
            noise += 1
            continue
        if verdict["bucket"] == "earnings":
            earnings += 1
            continue
        if verdict["bucket"] == "other":
            other += 1
            continue
        item = dict(kind=verdict["kind"], label=verdict["label"],
                    title=row["title"], published_at=row["published_at"].isoformat(),
                    disclosure_id=row.get("disclosure_id"))
        (catalysts if verdict["bucket"] == "catalyst" else risks).append(item)
    # 강한 촉매 종류가 먼저, 같은 종류 안에서는 최근 공시가 먼저(안정 정렬).
    catalysts.sort(key=lambda item: item["published_at"], reverse=True)
    catalysts.sort(key=lambda item: CATALYST_ORDER.index(item["kind"])
                   if item["kind"] in CATALYST_ORDER else len(CATALYST_ORDER))
    risks.sort(key=lambda item: item["published_at"], reverse=True)
    return dict(status=_MEASURED, market=market, code=code, source="Naver Finance disclosure list",
                observed_at=moment.isoformat(), window_sessions=WINDOW_SESSIONS,
                window_start=start.isoformat(), disclosures=len(published),
                catalysts=catalysts, risks=risks,
                earnings_count=earnings, noise_count=noise, other_count=other,
                meaning="공시 접수 시각 기준 · 판단 시각 이전 공개분만")


def strongest_catalyst(evidence):
    catalysts = (evidence or {}).get("catalysts") or []
    return catalysts[0] if catalysts else None


def strongest_risk(evidence):
    risks = (evidence or {}).get("risks") or []
    return risks[0] if risks else None


def candidate_rows(report, market="KR"):
    """공시를 읽을 분석 후보 — 심층 분석이 붙은 개별주(상품 제외)."""
    rows = []
    for row in (report or {}).get("results") or []:
        if not isinstance(row, dict) or row.get("market") != market:
            continue
        if "company_analysis" not in row or is_fund(row):
            continue
        rows.append(row)
    return rows


def attach_candidate_evidence(report, at=None, market="KR", fetch=None, workers=4,
                              start=None):
    """분석 후보의 최근 5세션 공시를 읽어 촉매·리스크 표시를 붙인다.

    매수 여부는 여기서 정하지 않는다. 촉매는 텔레그램 우선순위(표시)에, 리스크는
    ``preopen_signals``가 매수 후보를 배제하는 근거로 쓴다.
    """
    moment = _decision_time(at) if at is not None else datetime.now(ZoneInfo("UTC"))
    rows = candidate_rows(report, market)
    summary = dict(market=market, candidates=len(rows), measured=0, unavailable=0,
                   catalysts=0, risks=0, observed_at=moment.isoformat())
    if not rows:
        report["nonchart_candidate_summary"] = summary
        return summary
    start = start or window_start(moment, market)
    evidences = {}
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(rows)))) as pool:
        futures = {pool.submit(disclosure_evidence,
                               str(row.get("symbol") or "").split(".")[0], moment, market,
                               start, fetch): row for row in rows}
        for future in as_completed(futures):
            try:
                evidences[id(futures[future])] = future.result()
            except Exception as exc:  # a thread failure is one unread name, not a dead scan
                evidences[id(futures[future])] = dict(status=_UNAVAILABLE, market=market,
                                                      reason=type(exc).__name__)
    for row in rows:
        evidence = evidences.get(id(row)) or dict(status=_UNAVAILABLE, reason="not_read")
        row["nonchart_evidence"] = evidence
        for key in ("nonchart_catalyst", "nonchart_risk"):
            row.pop(key, None)
        if evidence.get("status") == _MEASURED:
            summary["measured"] += 1
        else:
            summary["unavailable"] += 1
        catalyst = strongest_catalyst(evidence)
        if catalyst:
            row["nonchart_catalyst"] = catalyst
            summary["catalysts"] += 1
        risk = strongest_risk(evidence)
        if risk:
            row["nonchart_risk"] = risk
            summary["risks"] += 1
    report["nonchart_candidate_summary"] = summary
    return summary


def is_aligned(values):
    """정배열(MA5 > MA20 > MA60, 가격 > MA20)인가. 값이 모자라면 정배열로 읽지 않는다."""
    values = values or {}
    numbers = [_number(values.get(key)) for key in ("price", "ma5", "ma20", "ma60")]
    if any(value is None or value <= 0 for value in numbers):
        return False
    price, ma5, ma20, ma60 = numbers
    return ma5 > ma20 > ma60 and price > ma20


def supply_lead_pool(report, market="KR"):
    """유동성·가격·상품 필터를 통과했지만 아직 정배열이 아닌 개별주.

    차트 게이트의 안전 필터(``liquidity_rejection_reason``)를 그대로 쓰므로, 읽을 수 없는
    값이 있는 종목은 여기 오지 않는다. 상품(ETF·인버스·레버리지)은 개별주의 수급이 아니고,
    검증에서도 ETF는 관찰 블록의 대상이 아니었다. 이미 매수 계획이 나간 종목(``preopen_signal``
    ``conditional_buy``)은 메시지 다른 자리에 있으므로 중복으로 싣지 않는다.
    """
    rows = []
    for row in (report or {}).get("results") or []:
        if not isinstance(row, dict) or row.get("market") != market:
            continue
        if is_fund(row):
            continue
        if liquidity_rejection_reason(row, market):
            continue
        if (row.get("preopen_signal") or {}).get("status") == "conditional_buy":
            continue
        if is_aligned(row.get("indicators")):
            continue
        rows.append(row)
    return rows


def observe_supply(row, at, start, trend_fetcher=None, disclosure_fetcher=None):
    """비정배열 한 종목의 5일 수급 강도와 동반 촉매를 읽는다.

    ``None``은 문턱 미달(관찰 대상 아님), ``status='unread'``는 수급을 읽지 못한 경우다.
    """
    moment = _decision_time(at)
    code = str((row or {}).get("symbol") or "").split(".")[0]
    if not code:
        return dict(status="unread", reason="missing_symbol")
    try:
        readings = (trend_fetcher or supply_flow.naver_trend)(code, supply_flow.WINDOW_SESSIONS)
        reading = supply_flow.score_rows(readings or [])
    except Exception as exc:
        return dict(status="unread", symbol=row.get("symbol"), reason=type(exc).__name__)
    if not reading:
        return dict(status="unread", symbol=row.get("symbol"), reason="empty_response")
    intensity = _number(reading.get("net_to_turnover_pct"))
    if intensity is None or intensity < MIN_SUPPLY_INTENSITY_PCT:
        return None
    evidence = disclosure_evidence(code, moment, "KR", start=start, fetch=disclosure_fetcher)
    values = row.get("indicators") or {}
    return dict(status="qualified", symbol=row.get("symbol"), market=row.get("market"),
                name=row.get("name") or row.get("symbol"), code=code,
                intensity_pct=round(intensity, 2),
                net_value=reading.get("net_value"),
                buy_days=reading.get("buy_days"), sessions=reading.get("sessions"),
                last_session=reading.get("last_session"),
                reference_price=values.get("price"), reference_session=values.get("data_date"),
                catalyst=strongest_catalyst(evidence),
                risk=strongest_risk(evidence),
                disclosure_status=evidence.get("status"))


def attach_supply_lead_watch(report, at=None, market="KR", trend_fetcher=None,
                             disclosure_fetcher=None, workers=DEFAULT_WORKERS,
                             limit=MAX_SYMBOLS, deadline_seconds=TIME_BUDGET_SECONDS):
    """수급 선행 관찰 블록 — 매수 후보·``preopen_signal``과 무관한 읽기 전용 기록."""
    moment = _decision_time(at) if at is not None else datetime.now(ZoneInfo("UTC"))
    pool = supply_lead_pool(report, market)
    start = window_start(moment, market)
    started = clock.monotonic()
    summary = dict(market=market, considered=len(pool), read=0, qualified=0,
                   risk_excluded=0, unread=0, skipped=0, min_intensity_pct=MIN_SUPPLY_INTENSITY_PCT,
                   window_sessions=supply_flow.WINDOW_SESSIONS, source="Naver Finance deal trend",
                   observed_at=moment.isoformat(), window_start=start.isoformat())
    rows = pool[:max(0, int(limit))]
    summary["skipped"] = len(pool) - len(rows)
    items, excluded = [], []
    if rows:
        with ThreadPoolExecutor(max_workers=max(1, min(int(workers), len(rows)))) as pool_exec:
            futures = {}
            for row in rows:
                if clock.monotonic() - started > deadline_seconds:
                    summary["skipped"] += 1
                    continue
                futures[pool_exec.submit(observe_supply, row, moment, start,
                                         trend_fetcher, disclosure_fetcher)] = row
            for future in as_completed(futures):
                try:
                    item = future.result()
                except Exception:
                    summary["unread"] += 1
                    continue
                if item is None:
                    continue
                if item.get("status") != "qualified":
                    summary["unread"] += 1
                    continue
                summary["read"] += 1
                if item.get("risk"):
                    summary["risk_excluded"] += 1
                    excluded.append(dict(symbol=item["symbol"], name=item["name"],
                                         kind=item["risk"].get("kind"),
                                         label=item["risk"].get("label"),
                                         title=item["risk"].get("title")))
                    continue
                summary["qualified"] += 1
                items.append(item)
    items.sort(key=rank_key)
    excluded.sort(key=lambda item: item.get("symbol") or "")
    summary["elapsed_seconds"] = round(clock.monotonic() - started, 1)
    watch = dict(items=items[:60], market=market, summary=summary,
                 excluded=excluded[:20])
    report["supply_lead_watch"] = watch
    report["supply_lead_watch_at"] = moment.isoformat()
    return watch


def rank_key(item):
    """수급 강도 → 순매수 금액 → 종목."""
    return (-(item.get("intensity_pct") or 0), -(item.get("net_value") or 0),
            item.get("symbol") or "")


def _date_label(value):
    """'2026-11-26T09:12:00+09:00' → '11/26'."""
    moment = _aware(value)
    if moment is None:
        return None
    local = moment.astimezone(KST)
    return f"{local.month}/{local.day}"


def _price_label(item):
    price = _number(item.get("reference_price"))
    if price is None:
        return None
    return f"${price:,.2f}" if item.get("market") == "US" else f"{price:,.0f}원"


def format_line(item):
    """한 줄: 종목명(코드) · 5일 수급 강도 · 금액 · (촉매) · 기준일 종가."""
    symbol = str(item.get("symbol") or "")
    code = item.get("code") or (symbol if item.get("market") == "US"
                                else symbol.split(".")[0])
    parts = [f"5일 수급 {item['intensity_pct']:+.1f}%"]
    net = _number(item.get("net_value"))
    if net is not None:
        parts.append(f"외인·기관 {net / 1e8:+.1f}억")
    catalyst = item.get("catalyst") or {}
    if catalyst.get("label"):
        when = _date_label(catalyst.get("published_at"))
        parts.append(f"촉매 {catalyst['label']}" + (f" ({when})" if when else ""))
    price = _price_label(item)
    if price:
        parts.append(price)
    line = f"• {item.get('name')} ({code}) · " + " · ".join(parts)
    session = item.get("reference_session")
    if isinstance(session, str) and len(session) >= 10:
        line += f" ({int(session[5:7])}/{int(session[8:10])} 종가)"
    return line


def format_block(watch, market=None):
    """텔레그램 블록. 머리줄에서 매수 신호가 아님을 밝히고 계획 형식은 쓰지 않는다."""
    if isinstance(watch, dict):
        items = [item for item in watch.get("items") or [] if isinstance(item, dict)]
        watch_market = watch.get("market")
    else:
        items = [item for item in watch or [] if isinstance(item, dict)]
        watch_market = None
    if market is not None and watch_market is not None and watch_market != market:
        return []
    if market is not None:
        items = [item for item in items if item.get("market") == market]
    if not items:
        return []
    items.sort(key=rank_key)
    lines = [HEADING]
    markets = {item.get("market") for item in items}
    if len(markets) > 1 and market is None:
        for name in sorted(markets):
            rows = [item for item in items if item.get("market") == name]
            lines.append(f"[{MARKET_LABELS.get(name, name)}]")
            lines.extend(format_line(item) for item in rows[:MAX_PER_MARKET])
            if len(rows) > MAX_PER_MARKET:
                lines.append(f"   외 {len(rows) - MAX_PER_MARKET}건 (분석자료 참고)")
    else:
        lines.extend(format_line(item) for item in items[:MAX_PER_MARKET])
        if len(items) > MAX_PER_MARKET:
            lines.append(f"   외 {len(items) - MAX_PER_MARKET}건 (분석자료 참고)")
    lines.append(NOTE)
    return lines


def priority(item):
    """텔레그램 정렬용 우선순위 — 촉매 공시가 붙은 후보가 먼저.

    10년 검증에서 게이트+촉매는 게이트만보다 10일 +0.14~2.97%p, 20일 +0.01~4.26%p
    좋았지만 표본이 연 23건이라 **순서·표시**에만 쓰고 점수는 건드리지 않는다.
    """
    catalyst = (item or {}).get("nonchart_catalyst") or {}
    return 1 if catalyst.get("label") else 0


def catalyst_line(item):
    """후보 한 줄 아래에 붙는 공시 근거 한 줄(없으면 빈 문자열)."""
    catalyst = (item or {}).get("nonchart_catalyst") or {}
    label = catalyst.get("label")
    if not label:
        return ""
    when = _date_label(catalyst.get("published_at"))
    return f"  공시: {label} 확인" + (f" ({when})" if when else "") + "\n"


def risk_exclusion_line(exclusions):
    """매수 후보에서 배제한 리스크 공시를 블록 안내 한 줄로."""
    rows = [row for row in exclusions or [] if isinstance(row, dict)]
    if not rows:
        return None
    names = []
    for row in rows[:2]:
        name = row.get("name") or row.get("symbol")
        label = row.get("label")
        names.append(f"{name} {label}" if label else str(name))
    more = f" 외 {len(rows) - 2}종목" if len(rows) > 2 else ""
    return f"※ 리스크 공시로 매수 후보 제외 {len(rows)}종목: " + ", ".join(names) + more
