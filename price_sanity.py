"""시세 자체가 틀린 행(티커 혼동·액면병합·극저가)을 급등·테마 집계에서 함께 막는다.

왜 필요한가: 2026-11-23 미국 세부 테마 `엔터·미디어`가 **+1926%**로 집계됐다. 원인은 PARA 한
행의 `+9,614%`였다. 파라마운트는 합병으로 티커가 바뀐 종목이라 시세 공급자가 0.03달러짜리
다른 종목의 가격을 이어 붙였고, 그 값이 테마 등락률·대표주·상승 비율에 그대로 섞였다.

하루 등락이 가격제한을 넘는 값은 뉴스가 아니라 데이터 오류다. 급등 알림(`urgent_issues`)과
테마 집계(`theme_sectors`)가 같은 기준을 쓰도록 그 판정만 여기에 모은다. 각 모듈이 스스로
판단하게 두면 한쪽만 기준을 고쳐서 같은 오류가 다른 블록에 남는다.

값의 의미는 두 가지다.
  - `abnormal_change`: 하루 등락이 +150%를 넘거나 -90% 아래이거나, 한국 거래소 일일
    가격제한(±30%)을 넘는다.
  - `low_price`: 종가가 시장 최저가(한국 1,000원·미국 1달러) 아래다. 티커를 잃은 종목은
    대개 센트 단위로 남는다.
"""

import math

# 하루 +150% 초과는 뉴스가 아니라 가격 데이터 오류다(예: PARA +9,614%).
MAX_DAILY_CHANGE_PCT = 150.0
# 하루 -90% 아래도 같다(역병합·티커 재사용). 상승 쪽만 막으면 하락 오류가 테마 평균을 끌어내린다.
MIN_DAILY_CHANGE_PCT = -90.0
# 한국 거래소 일일 가격제한. 이를 넘는 값은 시세 오류로 본다(신규 상장 첫날 포함).
KR_PRICE_LIMIT_PCT = 30.0
# 호가단위·반올림으로 상한가가 30%를 살짝 넘게 계산되는 경우만 허용한다.
KR_PRICE_LIMIT_TOLERANCE_PCT = 0.5
# 테마·급등 집계에서 '가격 오류'로 보는 최저 종가. 급등 알림은 유동성 기준이 따로 있어
# (`candidate_selection.MIN_PRICE`) 더 높은 하한을 쓴다.
MIN_PRICE = {"KR": 1_000.0, "US": 1.0}
REASON_LABELS = {
    "abnormal_change": "비정상 등락",
    "price_limit_exceeded": "가격제한 초과",
    "abnormal_price": "비정상 가격",
    "low_price": "극저가",
}


class PriceError(ValueError):
    """집계에서 빼야 하는 시세 행. `reason`에 사유 코드를 남긴다."""

    def __init__(self, reason):
        super().__init__(f"시세 오류: {REASON_LABELS.get(reason, reason)}")
        self.reason = reason


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _market(market):
    return "KR" if str(market or "").upper().startswith("KR") else "US"


def rejection_reason(market, change_pct=None, price=None):
    """이 시세 행을 집계에서 빼야 하는지. 정상이면 None.

    ``change_pct``가 없으면 등락 검사를 건너뛴다(시세만 있는 호출부). ``price``가 없으면
    가격 하한 검사를 건너뛴다. 둘 다 없으면 판단할 근거가 없으므로 None이다.
    """
    code = _market(market)
    change = _number(change_pct)
    if change is not None:
        if change > MAX_DAILY_CHANGE_PCT or change < MIN_DAILY_CHANGE_PCT:
            return "abnormal_change"
        if code == "KR" and abs(change) > KR_PRICE_LIMIT_PCT + KR_PRICE_LIMIT_TOLERANCE_PCT:
            return "price_limit_exceeded"
    value = _number(price)
    if value is None:
        return None
    if value <= 0:
        return "abnormal_price"
    if value < MIN_PRICE[code]:
        return "low_price"
    return None
