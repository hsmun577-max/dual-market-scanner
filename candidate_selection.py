"""Eligibility and ranking for the equity momentum recommendation strategy."""

import math
import re

# 20일 평균 거래대금 하한. 국내는 2026-11-26 혜선 님 지시("거래대금 기준 40억으로 낮춰")로
# 50억→40억 내렸다. 40억~50억 구간은 그전까지 `quality_watch`의 리서치 티어에서만 읽히던
# 종목이라, 이제는 모멘텀 매수 경로의 유동성 검사를 통과한다(티어 상단도 이 값을 따라간다).
# 미국 $10M은 그대로 둔다.
MIN_AVERAGE_TURNOVER = {"KR": 4_000_000_000, "US": 10_000_000}
MIN_PRICE = {"KR": 1000, "US": 5}
# Demand has to be observed in volume, not inferred from the price high alone. The
# five-session backtest of 2,300 candidates (2025-02-28~2026-11-12, 565 symbols) shows the
# 1.2-1.8x band has no edge over the same day's universe (-0.54%p for individual stocks)
# while 1.8x and above turns positive, and the improvement is monotonic up to 3x. A price
# breakout on 1.2-1.5x volume was -0.67%p, so a breakout does not replace volume.
MIN_DEMAND_VOLUME_RATIO = 1.2
MIN_EXPANSION_VOLUME_RATIO = 1.8
SPECIAL_SECURITY = re.compile(
    r"스팩|애퀴지션|우선주|유닛|워런트|\b(?:spac|acquisition|preferred|warrants?|units?)\b", re.I)
CASH_OR_BOND = re.compile(
    r"머니마켓|KOFR|CD(?:\d|금리)|단기금리|단기채|채권|국채|회사채|통안채|"
    r"\b(?:treasury|treasuries|bonds?|money market|ultrashort|floating rate|"
    r"variable rate|t-bills?|clo)\b|\b1-3M\b|"
    # 표기 변형으로 빠져나간 미국 채권·만기매칭형 ETF. iShares iBonds는 'Bonds'가 단어 경계로
    # 시작하지 않아 위 목록에 걸리지 않았고, 만기매칭형은 'TERM CORPORATE'·'TERM TIPS'로만
    # 채권임을 밝힌다. 2026-11-24 스캔에서 IBDR·IBDW·CGSD·DFGX가 이 틈으로 주식처럼
    # 시장 역풍 판정까지 올라갔다.
    r"\bibonds?\b|\bterm (?:corporate|treasury|tips|high yield|municipal|muni|credit)\b|"
    r"fixed income|\bduration\b|\bmunicipal\b|\bmuni\b|securitized|(?<!carbon )\bcredit\b|"
    r"high yield(?! equity)|\btreasury bills?\b", re.I)
# Inverse and leveraged products are no longer refused as a class (owner's call,
# 2026-11-23: "차단만 먼저 풀어", after a US down day where SQQQ was screened out only by
# this block). They now carry the same trend, demand and market-regime rules as any other
# instrument. What stays refused is the dividend-style structure whose payoff is the
# premium written, not the underlying's price.
COMPLEX_ETF = re.compile(r"커버드콜|위클리페이|\b(?:weeklypay|covered call)\b", re.I)
# 상환은 기초자산 가격이 아니라 옵션 구조에서 나온다: 자동조기상환(autocallable) 노트,
# 담보(collateral)·테일리스크 헤지 상품. 이들은 20일 추세로 판정할 대상이 아니고 채권 대체재처럼
# 움직인다(2026-11-24: CAOS·CBOX가 주식처럼 시장 국면 게이트까지 올라갔다).
STRUCTURED_PAYOFF = re.compile(r"autocallable|tail risk|\bcollateral\b", re.I)


# Use the index the fund actually shorts, including sector and small-cap funds.
INVERSE_BENCHMARKS = {
    # Listed funds are explicit proxies for the corresponding Korean cash indices.
    "114800.KS": "069500.KS", "252670.KS": "069500.KS", "251340.KS": "229200.KS",
    "SQQQ": "^NDX", "QID": "^NDX", "PSQ": "^NDX",
    "SOXS": "SOXX", "SPXS": "^GSPC", "SPXU": "^GSPC", "SDS": "^GSPC",
    "SH": "^GSPC", "TZA": "^RUT", "TWM": "^RUT", "RWM": "^RUT",
    "DOG": "^DJI", "DXD": "^DJI", "SDOW": "^DJI",
}
INVERSE_NAME = re.compile(
    r"인버스|곱버스|\b(?:inverse|ultrapro short)\b|(?<!\w)-[123]x\b"
    r"|\b(?:bear|short)\b.*\b(?:[123]x|etf)\b",
    re.I,
)


def is_inverse(symbol, name="", asset_type=""):
    return (symbol in INVERSE_BENCHMARKS or asset_type == "inverse_etf"
            or bool(INVERSE_NAME.search(name or "")))


# 국내 지수는 상장 시장별로 다르다. 코스닥 종목을 코스피 20일선으로 판정하면 그 종목이
# 속하지 않은 시장의 국면이 붙고, 장전(코스피 하나)과 장중(kis_intraday는 두 지수를 모두
# 봄)의 판정도 갈렸다(2026-11-26 전수점검 5항). 종목은 자기 시장의 지수로 판정한다.
KR_INDEX_BY_SUFFIX = {".KS": "^KS11", ".KQ": "^KQ11"}
MARKET_INDEX_SYMBOLS = {"KR": ("^KS11", "^KQ11"), "US": ("SPY",)}
INDEX_LABELS = {"^KS11": "KOSPI", "^KQ11": "코스닥", "SPY": "S&P500(SPY)"}


def index_symbol_for(symbol, market):
    """그 종목이 상장된 시장의 기준지수(코스피 · 코스닥 · SPY)."""
    if market != "KR":
        return MARKET_INDEX_SYMBOLS["US"][0]
    code = str(symbol or "").upper()
    for suffix, index in KR_INDEX_BY_SUFFIX.items():
        if code.endswith(suffix):
            return index
    # 접미사 없는 국내 표기(지수 ETF·직접 지정)는 코스피로 둔다.
    return KR_INDEX_BY_SUFFIX[".KS"]


def regime_above_ma20(regime, market, symbols=None):
    """그 종목이 상장된 시장의 국면(지수 > 20일선). 모르는 값은 None.

    `regime`은 스캔 보고서의 `market_regime`이다. 국내는 코스피·코스닥을 따로 남기므로
    종목의 접미사로 자기 지수를 고른다.
    """
    data = (regime or {}).get(market) or {}
    indices = data.get("indices") or {}
    if indices and symbols:
        item = indices.get(index_symbol_for(symbols, market)) or {}
        if item.get("above_ma20") is not None:
            return item["above_ma20"]
    return data.get("above_ma20")


def instrument_rejection_reason(symbol, name, asset_type=""):
    """Filter identifiable instruments with a different payoff or liquidity profile.

    ``asset_type`` is kept in the signature for the callers that carry it; inverse and
    leveraged listings are no longer rejected on it.
    """
    label = name or ""
    if SPECIAL_SECURITY.search(label) or re.search(r"우[BC]?(?:\([^)]*\))?$", label):
        return "special_security"
    if CASH_OR_BOND.search(label):
        return "cash_or_bond_product"
    if COMPLEX_ETF.search(label) or STRUCTURED_PAYOFF.search(label):
        return "complex_product"
    return None


def meets_price_floor(symbol, market, price):
    # The 1,000-won exemption applies only to KODEX 200 Futures Inverse 2X.
    if market == "KR" and symbol in {"252670", "252670.KS"}:
        return price >= 1
    return price >= MIN_PRICE[market]


def average_turnover(indicators):
    """Support archived scans using current price times prior average share volume."""
    actual = indicators.get("average_turnover_20d")
    if actual is not None:
        return actual
    ratio = indicators.get("volume_ratio") or 0
    return indicators["price"] * indicators["volume"] / ratio if ratio > 0 else 0


def liquidity_rejection_reason(item, market):
    reason = instrument_rejection_reason(item["symbol"], item.get("name"), item.get("asset_type", ""))
    if reason:
        return reason
    values = item.get("indicators", {})
    required = ("price", "volume", "volume_ratio", "ma20", "ma60",
                "return_5d", "return_20d", "relative_strength")
    if any(not isinstance(values.get(key), (int, float)) or not math.isfinite(values[key]) for key in required):
        return "missing_indicators"
    if not meets_price_floor(item["symbol"], market, values["price"]):
        return "low_price"
    average = average_turnover(values)
    if (not math.isfinite(average) or average < MIN_AVERAGE_TURNOVER[market]
            or values["price"] * values["volume"] < MIN_AVERAGE_TURNOVER[market]):
        return "insufficient_liquidity"
    return None


def market_headwind_reason(indicators, item=None):
    """The entries have no edge while the market index is below its 20-day average.

    Compared same-day (so market direction is already removed), individual-stock
    candidates were -1.11%p against their own universe on those days and +0.14%p on the
    other days. ``None`` means the regime was not observed: an archived report or a
    missing benchmark must not be read as a headwind.
    """
    if item and is_inverse(item["symbol"], item.get("name"), item.get("asset_type")):
        underlying = indicators.get("inverse_underlying_below_ma20")
        if underlying is None:
            return "inverse_underlying_unverified"
        return "market_headwind" if not underlying else None
    return "market_headwind" if indicators.get("market_above_ma20") is False else None


def candidate_rejection_reason(item, market):
    reason = liquidity_rejection_reason(item, market)
    if reason:
        return reason
    values = item["indicators"]
    if values.get("breakdown") or values.get("rebound"):
        return "breakdown_or_rebound"
    if not values["price"] > values["ma20"] >= values["ma60"] > 0:
        return "unconfirmed_uptrend"
    if not (values["return_5d"] > 0 and values["return_20d"] > 0 and values["relative_strength"] > 0):
        return "weak_relative_trend"
    if values.get("overheated") or values["return_5d"] > 0.12 or values["price"] / values["ma20"] > 1.08:
        return "extended_price"
    if not values.get("obv_rising") or values["volume_ratio"] < MIN_DEMAND_VOLUME_RATIO:
        return "unconfirmed_demand"
    if values["volume_ratio"] < MIN_EXPANSION_VOLUME_RATIO:
        return "insufficient_demand_expansion"
    # A stock that passes every own-signal test is still held back by its market.
    return market_headwind_reason(values, item)


# The short-term early-entry track (``early_entry_track``) buys the first one-to-three
# sessions of an average alignment, so the momentum gate's own trend confirmation —
# ``unconfirmed_uptrend`` (price > ma20 >= ma60) and ``weak_relative_trend`` (5- and
# 20-day returns plus relative strength already positive) — is exactly what it is meant to
# see past. What it may never lift is the safety around it: instrument, liquidity, price,
# a breakdown, an extended price, and the market regime. Those stay here, in one place, so
# the two paths cannot drift apart.
EARLY_ENTRY_LIFTABLE_REASONS = frozenset({"unconfirmed_uptrend", "weak_relative_trend"})

# 규칙 기반 가설(`economic_graph`의 긍정 링크, `event_support`)이 차트 게이트에서 넘길 수 있는
# 사유. 수요(거래량) 쪽 흔들림과 반등 캔들(`breakdown_or_rebound`, event_catalyst 트랙의 전제)만
# 넘긴다. 추세 확인(`unconfirmed_uptrend`·`weak_relative_trend`)은 넘기지 않는다 — 그것을
# 실제로 넘기는 길은 단기 초입 트랙 하나이고, 그 트랙은 자기 안전 목록
# (`early_entry_block_reason`·`qualifier`)을 따로 갖는다. 2026-11-26 전수점검: `event_support`가
# 참인 분석 42종목 중 20종목이 이 구멍에 해당해, 추세 미확인 상태도 점수·손익비만 보면 통과했다.
EVENT_SUPPORT_LIFTABLE_REASONS = frozenset({"breakdown_or_rebound", "unconfirmed_demand",
                                           "insufficient_demand_expansion"})


def early_entry_block_reason(item, market):
    """The block the short-term early-entry track may not lift, or ``None``.

    Read straight off the same indicators the momentum path reads, so a name the early
    track would plan is still refused when it is untradable, thin, breaking down, already
    extended, or trading under a market index beneath its 20-day average. This is the
    complement of ``EARLY_ENTRY_LIFTABLE_REASONS``: those two refusals are the only ones
    the track is allowed to see past.
    """
    reason = liquidity_rejection_reason(item, market)
    if reason:
        return reason
    values = item["indicators"]
    if values.get("breakdown"):
        return "breakdown"
    return_5d = values.get("return_5d")
    if values.get("overheated") or (isinstance(return_5d, (int, float))
                                    and not isinstance(return_5d, bool)
                                    and return_5d > 0.12):
        return "extended_price"
    return market_headwind_reason(values, item)


def rank_market_candidates(candidates, market, limit=5):
    """Rank eligible candidates without reserving slots for volume outliers."""
    eligible = [item for item in candidates if candidate_rejection_reason(item, market) is None]
    return sorted(eligible, key=lambda item: (
        -int(bool(item["indicators"].get("breakout"))),
        -item["indicators"]["relative_strength"],
        -min(item["indicators"]["volume_ratio"], 3),
        -average_turnover(item["indicators"]),
        -item["signal"]["score"], item["symbol"],
    ))[:max(0, limit)]


def rank_recovery_candidates(candidates, market, limit=5):
    """Watch liquid early recoveries before long moving averages can turn upward."""
    eligible = []
    for item in candidates:
        if liquidity_rejection_reason(item, market):
            continue
        values = item["indicators"]
        if (values.get("breakdown") or values.get("overheated") or not values.get("obv_rising")
                or not 0 < values["return_5d"] <= 0.12 or values["volume_ratio"] < 1.5
                or values["ma20"] <= 0 or not 0.95 <= values["price"] / values["ma20"] <= 1.08):
            continue
        if values.get("rebound") or values["ma20"] < values["ma60"]:
            eligible.append(item)
    return sorted(eligible, key=lambda item: (
        -average_turnover(item["indicators"]),
        -min(item["indicators"]["volume_ratio"], 3), item["symbol"],
    ))[:max(0, limit)]


# The owner's broker screen (Reuters focus, 2026-11-23, 롯데정밀화학 004000) showed an
# issuer the momentum screen discards for turnover alone: it traded 18.4억원, so
# ``insufficient_liquidity`` rejected it before its statements, its broker target or its
# investor flows were ever read. These issuers are now read on a second tier instead of
# being dropped, and the tier never promotes them into the momentum entry path.
RESEARCH_MIN_AVERAGE_TURNOVER = {"KR": 1_000_000_000, "US": 2_000_000}
# A falling knife is not a value entry: the price may sit below its 20-day average, but
# not more than this far, and an extended price is left to the momentum path.
QUALITY_WATCH_PRICE_BAND = (0.90, 1.15)


def quality_watch_reason(item, market):
    """Say why an issuer is read on the quality tier, or ``None`` when it is not.

    The row has to be tradable and observable but under the momentum turnover floor: a
    name that already clears that floor belongs to the momentum path, and one with no
    turnover at all has no price to read. Nothing here is an entry signal.
    """
    reason = instrument_rejection_reason(item["symbol"], item.get("name"), item.get("asset_type", ""))
    if reason:
        return reason
    values = item.get("indicators", {}) or {}
    required = ("price", "volume", "ma20", "return_5d")
    if any(not isinstance(values.get(key), (int, float)) or not math.isfinite(values[key])
           for key in required):
        return "missing_indicators"
    if values["price"] < MIN_PRICE[market]:
        return "low_price"
    average = average_turnover(values)
    floor = RESEARCH_MIN_AVERAGE_TURNOVER[market]
    if (not math.isfinite(average) or average < floor
            or values["price"] * values["volume"] < floor):
        return "below_research_turnover"
    if average >= MIN_AVERAGE_TURNOVER[market]:
        return "above_momentum_turnover"
    if values.get("breakdown"):
        return "breakdown"
    if values["ma20"] <= 0:
        return "invalid_average_price"
    low, high = QUALITY_WATCH_PRICE_BAND
    if not low <= values["price"] / values["ma20"] <= high:
        return "price_off_average"
    return None
