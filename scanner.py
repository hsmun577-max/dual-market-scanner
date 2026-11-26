import json
import math
import os
import re
import time
from zoneinfo import ZoneInfo
import argparse
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
import asset_regime
from candidate_selection import (INDEX_LABELS, INVERSE_BENCHMARKS, MARKET_INDEX_SYMBOLS,
                                 candidate_rejection_reason, index_symbol_for, is_inverse,
                                 rank_market_candidates)
from sector_flow import fetch_intraday_sectors, format_sector_header
from theme_sectors import theme_lines, theme_sector_snapshot
from telegram_text import rest_note, split_telegram_text
from research_asset import is_fund
from company_forecast import build_company_forecast
from delivery_policy import delivery_status
from exit_policy import (DEFAULT_EXIT_POLICY, FUND_STOP_PCT, STOCK_STOP_PCT,
                         exit_policy_for, policy_snapshot)
import yfinance as yf

KR_SYMBOL_NAMES = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
    "005380.KS": "현대차",
    "068270.KS": "셀트리온",
    "000270.KS": "기아",
    "012450.KS": "한화에어로스페이스",
    "042700.KS": "한미반도체",
    "069500.KS": "KODEX 200",
    "229200.KS": "KODEX 코스닥150",
    "114800.KS": "KODEX 인버스",
    "252670.KS": "KODEX 200선물인버스2X",
    "122630.KS": "KODEX 레버리지",
}

US_SYMBOL_NAMES = {
    "AAPL": "애플",
    "MSFT": "마이크로소프트",
    "NVDA": "엔비디아",
    "AMD": "어드밴스드 마이크로 디바이시스",
    "AVGO": "브로드컴",
    "AMZN": "아마존",
    "META": "메타 플랫폼스",
    "GOOGL": "알파벳 A",
    "TSLA": "테슬라",
    "PLTR": "팔란티어",
    "MU": "마이크론 테크놀로지",
    "MRVL": "마벨 테크놀로지",
    "SMCI": "슈퍼 마이크로 컴퓨터",
    "QQQ": "인베스코 QQQ",
    "SPY": "SPDR S&P 500 ETF",
    "IWM": "아이셰어즈 러셀 2000 ETF",
    "SOXX": "아이셰어즈 반도체 ETF",
    "SOXL": "디렉시온 반도체 3배 ETF",
    "SOXS": "디렉시온 반도체 인버스 3배 ETF",
    "TQQQ": "프로셰어즈 나스닥100 3배 ETF",
    "SQQQ": "프로셰어즈 나스닥100 인버스 3배 ETF",
    "SPXL": "디렉시온 S&P 500 3배 ETF",
    "SPXS": "디렉시온 S&P 500 인버스 3배 ETF",
    "TLT": "아이셰어즈 미국 장기국채 ETF",
}

WATCHLIST = {
    "KR": list(KR_SYMBOL_NAMES),
    "US": list(US_SYMBOL_NAMES),
}

BENCHMARKS = {
    "KR": "^KS11",
    "US": "SPY"
}

# 국내 지수는 상장 시장별로 다르다(정의는 `candidate_selection`): 코스닥 종목을 코스피
# 20일선으로 판정하면 그 종목이 속하지 않은 시장의 국면이 붙는다(2026-11-26 전수점검 5항).


def index_frame(benchmarks, market, symbol=None):
    """시장 대표 지수 시세. 국내는 지수별 시세 묶음을 받는다."""
    value = (benchmarks or {}).get(market)
    if isinstance(value, dict):
        return value.get(index_symbol_for(symbol, market) if symbol else BENCHMARKS[market])
    return value


LEVERAGED_ETFS = {
    "252670.KS", "122630.KS",
    "SOXL", "SOXS", "TQQQ",
    "SQQQ", "SPXL", "SPXS"
}

REPORT_DIR = Path("reports")
SECTOR_ETFS = {
    "KR": {
        "091160.KS": "반도체", "305720.KS": "2차전지", "244580.KS": "바이오",
        "091180.KS": "자동차", "091170.KS": "은행", "102970.KS": "증권",
        "266370.KS": "IT·전자부품", "395160.KS": "AI반도체",
        "117700.KS": "건설", "117680.KS": "철강", "140710.KS": "운송",
        "487240.KS": "AI전력설비", "0115D0.KS": "조선", "0080G0.KS": "방산",
        "228810.KS": "미디어·엔터",
    },
    "US": {
        "XLK": "기술", "XLF": "금융", "XLE": "에너지", "XLV": "헬스케어",
        "XLI": "산업재", "XLB": "소재", "XLC": "커뮤니케이션",
        "XLY": "경기소비재", "XLP": "필수소비재", "XLU": "유틸리티",
        "XLRE": "부동산",
    },
}
SECTOR_TURNOVER_THRESHOLD = 2.0

MASTER_URL = "https://new.real.download.dws.co.kr/common/master/"
KR_SECURITY_GROUPS = {"ST", "MF", "RT", "SC", "IF", "DR", "EF", "FE", "FS"}
# The master file's own group code for a listed fund ('EF' 상장형 ETF, 'FE'/'FS' 펀드형).
# A fund is priced from what it tracks, not from an issuer statement it never had, so the
# classification decides which scoring model the row is read with (research_asset.is_fund).
KR_FUND_GROUPS = {"EF", "FE", "FS"}
# US master (nasmst/nysmst/amsmst) '구분' 3 + '상품구분' 001/005 is an ETF; '구분' 2 is a
# stock. QQQ and SPY are listed that way, and their Korean display names ("인베스코 QQQ")
# carry no fund marker at all, so without this field they were graded as operating
# companies and every one of them was refused as analysis_incomplete (2026-11-25 scan).
US_ETF_SECURITY_TYPE = "3"
US_ETF_PRODUCT_TYPES = {"001", "005"}
# Filled by load_universe(): the instruments the exchange master itself calls a fund.
UNIVERSE_FUNDS = {"KR": set(), "US": set()}
FULL_SCOPE = "한국투자증권 종목정보 기준 코스피·코스닥·NASDAQ·NYSE·AMEX 주식 및 ETF"


def load_master(filename):
    response = requests.get(f"{MASTER_URL}{filename}.zip", timeout=30)
    response.raise_for_status()
    with ZipFile(BytesIO(response.content)) as archive:
        members = [entry for entry in archive.infolist() if not entry.is_dir()]
        member = next(
            (entry for entry in members if Path(entry.filename).name.casefold() == filename.casefold()),
            None,
        )
        if member is None and len(members) == 1:
            member = members[0]
        if member is None:
            raise ValueError(f"종목 목록 ZIP에서 파일을 찾을 수 없음: {filename}")
        return archive.read(member).decode("cp949").splitlines()


def parse_kr_master(lines, suffix, tail_width):
    """``(names, fund_symbols)`` — the group code is what tells a fund from a stock."""
    names = {}
    funds = set()
    for line in lines:
        if len(line) <= 21 + tail_width:
            continue
        head, tail = line[:-tail_width], line[-tail_width:]
        symbol, name = head[:9].strip(), head[21:].strip()
        if tail[:2] in KR_SECURITY_GROUPS and symbol and name:
            names[f"{symbol}.{suffix}"] = name
            if tail[:2] in KR_FUND_GROUPS:
                funds.add(f"{symbol}.{suffix}")
    return names, funds


def parse_us_master(lines):
    """``(names, fund_symbols)`` — '구분' 3 with 상품구분 001/005 is a listed fund."""
    names = {}
    funds = set()
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 23:
            continue
        security_type, product_type = fields[8].strip(), fields[22].strip()
        if security_type == "2" or (security_type == US_ETF_SECURITY_TYPE
                                    and product_type in US_ETF_PRODUCT_TYPES):
            symbol = fields[4].strip().replace(".", "-")
            name = US_SYMBOL_NAMES.get(symbol) or fields[6].strip() or fields[7].strip()
            if symbol and name:
                names[symbol] = name
                if security_type == US_ETF_SECURITY_TYPE:
                    funds.add(symbol)
    return names, funds


def load_universe():
    names = {"KR": {}, "US": {}}
    UNIVERSE_FUNDS["KR"].clear()
    UNIVERSE_FUNDS["US"].clear()
    # Tail widths exclude the newline counted by the KIS examples.
    for filename, suffix, width in [
        ("kospi_code.mst", "KS", 227),
        ("kosdaq_code.mst", "KQ", 221),
    ]:
        instruments, funds = parse_kr_master(load_master(filename), suffix, width)
        if not instruments:
            raise ValueError(f"종목 목록을 읽을 수 없음: {filename}")
        names["KR"].update(instruments)
        UNIVERSE_FUNDS["KR"].update(funds)

    for filename in ("nasmst.cod", "nysmst.cod", "amsmst.cod"):
        instruments, funds = parse_us_master(load_master(filename))
        if not instruments:
            raise ValueError(f"종목 목록을 읽을 수 없음: {filename}")
        names["US"].update(instruments)
        UNIVERSE_FUNDS["US"].update(funds)
    return names


def safe_float(value):
    try:
        value = float(value)
        return round(value, 4) if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None

YF_ATTEMPTS = 3
YF_BACKOFF_SECONDS = (2, 5)
_RATELIMIT_HINTS = ("ratelimit", "rate limit", "too many requests", "429")


def rate_limited(exc):
    """Tell a provider throttle apart from a bad ticker or a data fault."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "ratelimit" in name or "ratelimited" in name or any(
        hint in text for hint in _RATELIMIT_HINTS)


def yf_download(tickers, **options):
    """One yfinance entry point with bounded retries, so every caller behaves alike.

    Returns None when the provider stays unavailable; the caller then records a data
    reason instead of silently scoring a company without prices.
    """
    last = None
    for attempt in range(YF_ATTEMPTS):
        try:
            data = yf.download(tickers, **options)
            if data is not None and not data.empty:
                return data
            last = ValueError("empty download")
        except Exception as exc:  # provider errors surface as many types
            last = exc
            if rate_limited(exc) and attempt < YF_ATTEMPTS - 1:
                time.sleep(YF_BACKOFF_SECONDS[min(attempt, len(YF_BACKOFF_SECONDS) - 1)])
                continue
        if attempt < YF_ATTEMPTS - 1:
            print(f"yfinance download retry {attempt + 1}/{YF_ATTEMPTS - 1}: {type(last).__name__}")
            time.sleep(YF_BACKOFF_SECONDS[min(attempt, len(YF_BACKOFF_SECONDS) - 1)])
    print(f"yfinance download unavailable after {YF_ATTEMPTS} attempts: {type(last).__name__}")
    return None


def download_prices(symbol, period="1y", retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            data = yf_download(
                symbol,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
                timeout=20
            )

            if data is None or data.empty:
                raise ValueError("가격 데이터 없음")

            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)

            return clean_prices(data)

        except Exception as exc:
            last_error = str(exc)

            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(last_error)

def clean_prices(data):
    required = ["Open", "High", "Low", "Close", "Volume"]
    for column in required:
        if column not in data.columns:
            raise ValueError(f"{column} 누락")
    data = data[required].replace([np.inf, -np.inf], np.nan).dropna()
    data = data.loc[data[required[:4]].gt(0).all(axis=1) & data["Volume"].ge(0)]
    data = data.loc[~data.index.duplicated(keep="last")].sort_index()
    if len(data) < 65:
        raise ValueError(f"데이터 부족: {len(data)}거래일")
    return data


def scan_batches(market, names, benchmark, inverse_prices=None):
    symbols = list(names)
    selection_now = datetime.now(timezone.utc)
    results, errors = [], []
    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]
        prices = None
        last_error = None
        for attempt in range(3):
            try:
                prices = yf_download(
                    batch, period="1y", interval="1d", auto_adjust=True,
                    progress=False, threads=8, timeout=20, group_by="ticker",
                    multi_level_index=True,
                )
                if prices is None or prices.empty:
                    raise ValueError("일괄 가격 데이터 없음")
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    delay = 2 ** attempt
                    print(
                        f"{market}: batch {start + 1}-{start + len(batch)} "
                        f"retry {attempt + 1}/2 in {delay}s: {type(exc).__name__}"
                    )
                    time.sleep(delay)

        if last_error is not None:
            errors.extend(
                {"market": market, "symbol": symbol,
                 "error": f"batch download failed after 3 attempts: {type(last_error).__name__}: {last_error}"}
                for symbol in batch
            )
            continue

        found, failed = scan_market(market, batch, benchmark, names=names, prices=prices,
                                    inverse_prices=inverse_prices, selection_now=selection_now)
        results.extend(found)
        errors.extend(failed)
        print(f"{market}: {len(results) + len(errors)}/{len(symbols)}")
    return results, errors


def summarize_scan_errors(errors, limit=10):
    counts = {}
    for row in errors:
        raw = str(row.get("error") or "unknown error").splitlines()[0].strip()
        category = raw[:180] or "unknown error"
        key = (row.get("market", "unknown"), category)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"market": market, "reason": reason, "count": count}
        for (market, reason), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )[:limit]
    ]


def calculate_obv(close, volume):
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()

def calculate_rsi(close, period=14):
    """Wilder's RSI over completed daily closes; None without enough history.

    The rebound tracks read an oversold day from it (`rsi14 <= 35`). A partial window
    would produce a number that looks like a level but is not the level, so the value is
    left as None instead of being estimated.
    """
    values = close.astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < period + 1:
        return None
    delta = values.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = float(gain.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    average_loss = float(loss.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return float(100 - 100 / (1 + average_gain / average_loss))


def calculate_indicators(data, benchmark=None):
    close = data["Close"].astype(float)
    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    volume = data["Volume"].astype(float)

    price = float(close.iloc[-1])
    ma5 = float(close.tail(5).mean())
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())

    # How young the 5/20-day alignment is: the short-term early-entry track buys the first
    # one-to-three sessions of it (or the first close back above the 20-day average), which
    # the momentum gate refuses as ``unconfirmed_uptrend``. Both are read off the series so
    # an archived frame that does not carry them is never read as a pass.
    ma5_series = close.rolling(5).mean()
    ma20_series = close.rolling(20).mean()
    aligned_days = None
    if len(close) >= 20 and pd.notna(ma5_series.iloc[-1]) and pd.notna(ma20_series.iloc[-1]):
        if ma5_series.iloc[-1] > ma20_series.iloc[-1]:
            aligned_days = 0
            for position in range(len(close) - 1, 3, -1):
                short, long = ma5_series.iloc[position], ma20_series.iloc[position]
                if pd.isna(short) or pd.isna(long) or short <= long:
                    break
                aligned_days += 1
    ma20_cross_up = None
    if len(close) >= 21 and pd.notna(ma20_series.iloc[-2]):
        ma20_cross_up = bool(close.iloc[-2] <= ma20_series.iloc[-2] and price > ma20_series.iloc[-1])

    # The bar's own open/high/low and one-day change: a rebound setup is read from the
    # day's candle (a bullish close, a 5-day-MA break, a short upper wick) and not from
    # the 5-day return alone. ``Open`` is optional so archived frames and test fixtures
    # that only carry closes still calculate the momentum indicators.
    opening = float(data["Open"].astype(float).iloc[-1]) if "Open" in data.columns else None
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])
    previous_close = float(close.iloc[-2]) if len(close) > 1 else None
    return_1d = price / previous_close - 1 if previous_close else None
    upper_wick_ratio = ((last_high - price) / (last_high - last_low)
                        if last_high > last_low else 0.0)

    previous_volume = volume.iloc[-21:-1]
    avg_volume = float(previous_volume.mean())

    volume_ratio = (
        float(volume.iloc[-1]) / avg_volume
        if avg_volume > 0 else 0
    )

    returns = close.pct_change()
    volatility_5 = float(returns.tail(5).std())
    volatility_20 = float(returns.tail(20).std())

    compression = (
        volatility_5 < volatility_20 * 0.75
        if volatility_20 > 0 else False
    )

    obv = calculate_obv(close, volume)
    obv_rising = bool(
        obv.iloc[-1] > obv.iloc[-6]
    )

    previous_high = float(high.iloc[-21:-1].max())
    previous_low = float(low.iloc[-21:-1].min())

    breakout = price > previous_high
    breakdown = price < previous_low

    return_5 = (
        price / float(close.iloc[-6]) - 1
    )

    return_20 = (
        price / float(close.iloc[-21]) - 1
    )

    relative_strength = None
    market_return = None
    market_above_ma20 = None
    market_ma20 = None
    market_ma20_date = None
    market_lag_sessions = None

    if benchmark is not None:
        # Market regime from the index itself: an entry needs the index above its own
        # 20-day average. Left as None (not False) when the index is unavailable so a
        # fetch gap cannot read as a headwind.
        index_closes = benchmark["Close"].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        index_closes = index_closes.loc[~index_closes.index.duplicated(keep="last")].sort_index()
        index_closes = index_closes.loc[index_closes > 0]
        # 기준일을 종목의 마지막 봉에 맞춘다. 지수가 더 새 봉(아직 끝나지 않은 당일 봉)을
        # 갖고 있으면 20일선에 미완성 세션이 들어가고, 하루 밀려 있으면 전일 국면이 오늘
        # 국면으로 읽힌다 — 2026-11-26 발송본의 index_ma20 5,322.07은 실제로 11/24까지의
        # 20일 평균이었고, 지수 봉이 11/25를 건너뛴 탓이었다. 두 경우 모두 종목 봉의 마지막
        # 세션까지만 읽고, 기준일과 지연 세션 수를 함께 남겨 메시지가 그 사실을 말하게 한다.
        last_session = close.index[-1]
        last_day = last_session.date() if hasattr(last_session, "date") else last_session
        stamps = [stamp.date() if hasattr(stamp, "date") else stamp for stamp in index_closes.index]
        index_upto = index_closes.loc[[day <= last_day for day in stamps]]
        if len(index_upto) >= 20:
            market_ma20 = float(index_upto.tail(20).mean())
            market_above_ma20 = bool(float(index_upto.iloc[-1]) > market_ma20)
            index_last = index_upto.index[-1]
            market_ma20_date = (index_last.date() if hasattr(index_last, "date")
                                else index_last).isoformat()
            market_lag_sessions = sum(
                1 for stamp in close.index
                if (stamp.date() if hasattr(stamp, "date") else stamp) > (index_last.date()
                                                                         if hasattr(index_last, "date") else index_last))

        aligned = pd.concat(
            [
                close.rename("stock"),
                benchmark["Close"].rename("benchmark")
            ],
            axis=1,
            join="inner"
        ).dropna()

        if len(aligned) >= 21:
            stock_return = (
                aligned["stock"].iloc[-1]
                / aligned["stock"].iloc[-21] - 1
            )

            market_return = (
                aligned["benchmark"].iloc[-1]
                / aligned["benchmark"].iloc[-21] - 1
            )

            relative_strength = float(
                stock_return - market_return
            )

    drawdown_20 = (
        price / float(high.tail(20).max()) - 1
    )

    rebound = bool(
        drawdown_20 < -0.08
        and close.iloc[-1] > close.iloc[-2]
        and obv_rising
    )

    overheated = bool(
        return_5 > 0.15 or
        price > ma20 * 1.15
    )

    return {
        "price": safe_float(price),
        "open": safe_float(opening),
        "high": safe_float(last_high),
        "low": safe_float(last_low),
        "prev_close": safe_float(previous_close),
        "ma5": safe_float(ma5),
        "ma20": safe_float(ma20),
        "ma60": safe_float(ma60),
        "ma5_above_ma20_days": aligned_days,
        "ma20_cross_up": ma20_cross_up,
        "return_1d": safe_float(return_1d),
        "rsi14": safe_float(calculate_rsi(close)),
        "upper_wick_ratio": safe_float(upper_wick_ratio),
        "volume": safe_float(volume.iloc[-1]),
        "volume_ratio": safe_float(volume_ratio),
        "volatility_20d": safe_float(volatility_20),
        "atr14_pct": safe_float(100 * pd.concat([high-low, (high-close.shift()).abs(),
            (low-close.shift()).abs()],axis=1).max(axis=1).tail(14).mean()/price),
        "market_return_20d": safe_float(market_return),
        "market_above_ma20": market_above_ma20,
        "market_ma20": safe_float(market_ma20),
        "market_ma20_date": market_ma20_date,
        "market_lag_sessions": market_lag_sessions,
        "average_turnover_20d": safe_float((close * volume).iloc[-21:-1].mean()),
        "return_5d": safe_float(return_5),
        "return_20d": safe_float(return_20),
        "relative_strength": safe_float(relative_strength),
        "compression": compression,
        "obv_rising": obv_rising,
        "breakout": bool(breakout),
        "breakdown": bool(breakdown),
        "rebound": rebound,
        "overheated": overheated,
        "data_date": str(data.index[-1].date())
    }

def classify_signal(indicators, inverse=False):
    direction = (
        "인버스 ETF 가격 상승 관찰" if inverse else "가격 상승 관찰"
    )
    score = 0
    reasons = []

    if indicators["volume_ratio"] >= 1.2:
        score += 1
        reasons.append("거래량 증가")

    if indicators["compression"]:
        score += 1
        reasons.append("변동성 수축")

    if indicators["obv_rising"]:
        score += 1
        reasons.append("OBV 상승")

    rs = indicators["relative_strength"]

    if rs is not None and rs > 0:
        score += 1
        reasons.append("시장 대비 상대강도 우위")

    if indicators["price"] > indicators["ma20"]:
        score += 1
        reasons.append("20일선 상회")

    if indicators["breakout"]:
        score += 2
        reasons.append("20일 고점 돌파")

    if indicators["rebound"]:
        reasons.append("급락 후 반등 · 추세 확인 필요")

    if indicators["overheated"]:
        return {
            "stage": "과열 관찰",
            "score": score,
            "reasons": reasons,
            "confirmed": False,
            "direction": direction
        }

    if indicators.get("breakdown"):
        stage = "하락 관찰"
    elif indicators["rebound"]:
        stage = "반등 관찰"
    elif score >= 5:
        stage = "장중 조건부 진입 후보"
    elif score >= 3:
        stage = "장전 관찰 후보"
    else:
        stage = "조건 미충족"

    return {
        "stage": stage,
        "score": score,
        "reasons": reasons,
        "confirmed": False,
        "direction": direction
    }

def build_risk_plan(price, stop_pct=None,
                    target_pct=None,
                    risk_budget=30000,
                    policy=None):
    """Sell levels of the instrument's policy: −2% stop on a fund, −3% on a stock.

    ``policy`` is the row's own policy (``exit_policy.exit_policy_for(row)``); without it
    the fund policy applies. ``stop_pct`` and ``target_pct`` default to that policy, so the
    stop price, the quantity the risk budget buys and the reward-to-risk ratio all follow
    the same number. ``target_price`` is the default +6% level, not a hard sell when a
    short-term surge carried the price through it: ``exit_policy`` carries the trailing
    parameters used by ``exit_policy.ExitTracker``.
    """
    policy = policy or DEFAULT_EXIT_POLICY
    stop_pct = policy.stop_pct if stop_pct is None else stop_pct
    target_pct = policy.take_profit_pct if target_pct is None else target_pct
    stop_price = price * (1 - stop_pct)
    target_price = price * (1 + target_pct)

    risk_per_share = price - stop_price

    quantity = (
        math.floor(risk_budget / risk_per_share)
        if risk_per_share > 0 else 0
    )

    return {
        "reference_entry": safe_float(price),
        "stop_price": safe_float(stop_price),
        "target_price": safe_float(target_price),
        "stop_pct": safe_float(stop_pct),
        "asset_class": ("fund" if policy.stop_pct == FUND_STOP_PCT else
                        "stock" if policy.stop_pct == STOCK_STOP_PCT else None),
        "take_profit_pct": safe_float(target_pct),
        "quantity_risk_only": quantity,
        "risk_reward_ratio": safe_float(
            target_pct / stop_pct
        ),
        "exit_policy": policy_snapshot(policy),
        "warning": (
            "참고 계산이며 실제 주문 신호가 아님. "
            "가용 현금, 수수료, 슬리피지 미반영."
        )
    }

def last_completed_session(market, now):
    from exchange_calendars import get_calendar
    zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
    calendar = get_calendar('XKRX' if market == 'KR' else 'XNYS')
    local_day = now.astimezone(zone).date()
    session = calendar.date_to_session(local_day.isoformat(), direction='previous')
    if calendar.session_close(session).to_pydatetime() > now:
        session = calendar.previous_session(session)
    return session.date()


def completed_daily_prices(data, market, now=None, *, through=None):
    zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
    cutoff = through or last_completed_session(market, now or datetime.now(timezone.utc))
    dates = pd.DatetimeIndex(data.index)
    if dates.tz is not None:
        dates = dates.tz_convert(zone)
    return data.loc[dates.date <= cutoff].copy()


def attach_inverse_regime(indicators, symbol, benchmark):
    index_symbol = INVERSE_BENCHMARKS.get(symbol)
    below = None
    if benchmark is not None and 'Close' in benchmark:
        closes = benchmark['Close'].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        if len(closes) >= 20 and str(closes.index[-1].date()) == indicators.get('data_date'):
            below = bool(closes.iloc[-1] < closes.tail(20).mean())
    indicators['inverse_underlying'] = index_symbol
    indicators['inverse_underlying_below_ma20'] = below


def scan_market(market, tickers, benchmark, names=None, prices=None, *,
                inverse_prices=None, selection_now=None):
    names = names or {}
    results = []
    errors = []
    if selection_now is None:
        selection_now = datetime.now(timezone.utc)
    cutoff = last_completed_session(market, selection_now)
    # `benchmark`는 한 시장의 지수 하나이거나, 국내처럼 시장별 지수 묶음일 수 있다.
    if isinstance(benchmark, dict):
        market_benchmarks = {name: (completed_daily_prices(frame, market, through=cutoff)
                                    if frame is not None else None)
                             for name, frame in benchmark.items()}
    elif benchmark is not None:
        market_benchmarks = {BENCHMARKS[market]: completed_daily_prices(benchmark, market, through=cutoff)}
    else:
        market_benchmarks = {}
    inverse_prices = {symbol: completed_daily_prices(frame, market, through=cutoff)
                      for symbol, frame in (inverse_prices or {}).items() if frame is not None}
    for symbol in dict.fromkeys(tickers):
        try:
            data = download_prices(symbol) if prices is None else clean_prices(prices[symbol])
            data = completed_daily_prices(data, market, through=cutoff)

            indicators = calculate_indicators(
                data, market_benchmarks.get(index_symbol_for(symbol, market))
            )

            inverse = is_inverse(symbol, names.get(symbol))
            if inverse:
                attach_inverse_regime(indicators, symbol, inverse_prices.get(INVERSE_BENCHMARKS.get(symbol)))
            signal = classify_signal(indicators, inverse=inverse)

            result = {
                "market": market,
                "symbol": symbol,
                "name": names.get(symbol),
                # The exchange master's own fund classification, not a name pattern: a
                # US listing such as QQQ ("인베스코 QQQ") carries no fund marker in its
                # display name, and an unclassified ETF was scored as an operating
                # company and refused for a statement it never had.
                "asset_type": (
                    "inverse_etf"
                    if inverse
                    else "leveraged_etf"
                    if symbol in LEVERAGED_ETFS
                    else "etf"
                    if symbol in UNIVERSE_FUNDS.get(market, ())
                    else "stock_or_etf"
                ),
                "indicators": indicators,
                "signal": signal,
                "data_checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "fundamentals": "발송 후보 선정 후 조회",
                "news_and_filings": "발송 후보 선정 후 조회",
                # Not read yet: the issuer is only read once the screen keeps it. A row
                # that reaches the analysis gets the measured flow in its place.
                "institutional_flows": {"status": "not_observed",
                                        "reason": "issuer_not_read_in_screen"}
            }

            result["risk_plan"] = build_risk_plan(
                indicators["price"], policy=exit_policy_for(result)
            )
            result["selection_filter"] = candidate_rejection_reason(result, market) or "eligible"
            results.append(result)
            if prices is None:
                print(f"{market} {symbol}: {signal['stage']}")

        except Exception as exc:
            errors.append({
                "market": market,
                "symbol": symbol,
                "error": str(exc)
            })
            if prices is None:
                print(f"ERROR {market} {symbol}: {exc}")

    return results, errors

def rank_korean_candidates(candidates, limit=5):
    """Keep the public domestic selector on the shared market eligibility rules."""
    return rank_market_candidates(candidates, "KR", limit)

def prepare_selected_analysis(report):
    """Fetch evidence for the actual daily alert candidates before saving and sending."""
    from kis_intraday import company_context, KR_SYMBOL_NAMES as context_kr_names, US_SYMBOL_NAMES as context_us_names
    from economic_graph import load_profiles, discover, select_analysis_candidates
    from event_impact_analysis import world_events
    from research_asset import is_fund, promoted_fund_symbols, FUND_CANDIDATE_LIMIT
    funds=[row for row in report['results'] if is_fund(row)]
    # The eight most traded funds of each market are researched and planned like any other
    # instrument (owner's decision 2026-11-23: "거래량 상위8개 etf만 매수후보에넣자").
    # Everything below them keeps the fund screen only: the issuer grading that fills
    # ``company_analysis`` is not what a listed fund is, so that path found nothing for them.
    promoted={market:list(symbols) for market,symbols in promoted_fund_symbols(funds).items()}
    promoted_sets={market:set(symbols) for market,symbols in promoted.items()}
    for row in funds:
        if row['symbol'] in promoted_sets.get(row.get('market'),set()):
            row['analysis_kind']='fund_candidate'
            row['fund_rank']=promoted[row['market']].index(row['symbol'])+1
            continue
        row['analysis_kind']='fund_screen_only'
        for key in ('company_analysis','company_forecast','preopen_signal','advance_signal'):
            row.pop(key,None)
    report['research_scope']=dict(funds_screened=len(funds)-sum(len(s) for s in promoted.values()),
        funds_promoted=sum(len(s) for s in promoted.values()),
        fund_candidate_limit=FUND_CANDIDATE_LIMIT,funds_graded_as_operating_companies=False)
    if (any(row['market']=='KR' for row in report['results'])
            and ('industry_snapshot' in report or (os.getenv('KIS_APP_KEY') and os.getenv('KIS_APP_SECRET')))):
        from industry_flow import snapshot,industry_sector_evidence,format_report
        try:
            industry=report.get('industry_snapshot')
            if industry is None:
                import exchange_calendars
                local=datetime.now(ZoneInfo('Asia/Seoul'));cal=exchange_calendars.get_calendar('XKRX')
                session=cal.date_to_session(local.date().isoformat(),direction='previous')
                day=cal.previous_session(session) if local<cal.session_open(session).to_pydatetime() else session
                industry=snapshot(day.strftime('%Y%m%d'))
            report['industry_snapshot']=industry
            report['sectors']=[*report.get('sectors',[]),*industry_sector_evidence(industry)]
            if industry.get('rows'):report['sector_header_override']=format_report(industry).splitlines()
        except Exception as exc:
            report['industry_snapshot_error']=type(exc).__name__

    checked = datetime.now(timezone.utc)
    shared_events, event_coverage = world_events(checked)
    registry = load_profiles()
    linked = discover(registry, shared_events, datetime.now(timezone.utc),limit=None)
    report["event_discovery"] = {"profile_count":len(registry),"linked_symbols":linked,
                                 "feed_coverage":event_coverage,"universe_wide_news_coverage":False}
    for market in ("KR", "US"):
        pool = [item for item in report["results"] if item["market"] == market
                and (not is_fund(item) or item["symbol"] in promoted_sets.get(market,set()))]
        event_symbols = [item["symbol"] for item in pool for symbol in linked
            if (item["symbol"] == (registry.get(symbol,{}).get("source_symbol") or symbol.removeprefix("DNAS"))
                or (market=="KR" and item["symbol"].split(".")[0]==symbol))]
        selected = select_analysis_candidates(pool, market, event_symbols,
                                              pinned=promoted_sets.get(market,()))
        watched=[item['symbol'] for item in selected if item.get('selection_reason')=='catalyst_watch']
        if watched:
            # Research budget spent before any news is read; the promotion itself only
            # happens when a confirmed disclosure exists (preopen_signals).
            report.setdefault('catalyst_watch',[]).extend(watched)
            print(f"{market} 실적·호재 촉매 감시 {len(watched)}종목: {', '.join(watched)}")
        keys = {}
        for item in selected:
            symbol = item["symbol"].split(".")[0] if market == "KR" else "DNAS" + item["symbol"]
            keys[symbol] = item
            if market == "KR":
                context_kr_names[item["symbol"]] = item.get("name") or item["symbol"]
                context_kr_names[symbol + ".KS"] = item.get("name") or symbol
            else:
                context_us_names[item["symbol"]] = item.get("name") or item["symbol"]
        if not keys:
            continue
        try:
            companies, risks = company_context("KRX" if market == "KR" else "US-late", list(keys), checked)
        except Exception as exc:
            companies, risks = {}, []
            print(f"{market} daily company analysis failed ({type(exc).__name__})")
        for symbol, item in keys.items():
            company = companies.get(symbol, {})
            observed_prices = company.get('price_indicators', {})
            if (observed_prices.get('price') and observed_prices.get('data_date')
                    and observed_prices['data_date'] >= item['indicators'].get('data_date', '')):
                indicators = {**item['indicators'], **observed_prices}
                if (is_inverse(item['symbol'], item.get('name'), item.get('asset_type'))
                        and 'inverse_underlying_below_ma20' not in observed_prices
                        and observed_prices['data_date'] != item['indicators'].get('data_date')):
                    attach_inverse_regime(indicators, item['symbol'], None)
                item['indicators'] = indicators
            item["company_analysis"] = company
            item["company_analysis_at"] = company.get("checked_at", checked.isoformat())
            margin = company.get("operating_margin_pct")
            if is_fund(item):
                # A listed fund has no issuer income statement, so there is no margin to read
                # and no statement to be current. Its score comes from the fund model in
                # company_forecast, and whether it stays a candidate is settled by the final
                # top-eight ranking below.
                # The scan's own classification is stamped on the profile: a restored profile
                # cache may hold an entry built before the provider's ``quote_type`` was read,
                # and without it the fund would be graded as an operating company and have
                # its score withheld for a statement it never had.
                company.setdefault('instrument_class', 'listed_fund')
                item['fund_profile_seen']=True
                item["fundamentals"]="ETF 상장상품 · 발행사 재무제표 없음"
            else:
                item["fundamentals"] = ("영업흑자" if margin is not None and margin > 0 else
                                        "영업적자" if margin is not None and margin < 0 else
                                        "손익분기" if margin == 0 else "재무자료 조회 실패 또는 미제공")
            item["news_and_filings"] = f"기사 {len(company.get('news', []))}건 · 공시 {len(company.get('filings', []))}건"
            # Who is actually buying the issuer, read per name instead of left as a
            # placeholder. A name the screen already holds is read here, once.
            from supply_flow import evidence_for, share_line
            from issuer_consensus import consensus_for
            supply = evidence_for(item, company, checked)
            item["institutional_flows"] = supply
            item["supply_line"] = share_line(supply)
            consensus = consensus_for(item, company, at=checked, price=item["indicators"]["price"])
            item["company_forecast"] = build_company_forecast(company, item["indicators"],
                item["indicators"]["price"], datetime.now(timezone.utc),
                [row for row in risks if row.get("symbol") == symbol], report.get("sectors", []),
                supply=supply, consensus=consensus)
            item["risk_headlines"] = list(dict.fromkeys(
                row.get("headline", "") for row in risks if row.get("symbol") == symbol))[:2]
            item['risk_evidence']=[row for row in risks if row.get('symbol')==symbol]
    # A US fund is often recognisable only from the quote type of its profile, so the
    # top-eight is settled once every profile is in: the ranking covers every fund the scan
    # now knows about, and a fund that a late profile pushed below eighth is demoted here.
    known=[row for row in report['results'] if is_fund(row)]
    final={market:list(symbols) for market,symbols in promoted_fund_symbols(known).items()}
    final_sets={market:set(symbols) for market,symbols in final.items()}
    for row in known:
        market=row.get('market')
        if row['symbol'] in final_sets.get(market,set()):
            row['analysis_kind']='fund_candidate'
            row['fund_rank']=final[market].index(row['symbol'])+1
            continue
        row['analysis_kind']='fund_screen_only'
        if row.get('company_analysis'):
            row['fund_demoted_after_profile']=True
        for key in ('company_analysis','company_forecast','preopen_signal','advance_signal',
                    'risk_evidence','fund_rank'):
            row.pop(key,None)
    report["company_analysis_at"] = checked.isoformat()
    demoted=[row for row in report['results'] if row.get('analysis_kind')=='fund_screen_only']
    promoted_rows=[row for row in report['results'] if row.get('analysis_kind')=='fund_candidate']
    report['research_scope']=dict(funds_screened=len(demoted),funds_promoted=len(promoted_rows),
        fund_candidate_limit=FUND_CANDIDATE_LIMIT,
        funds_demoted_after_profile=sum(1 for row in demoted if row.get('fund_demoted_after_profile')),
        funds_graded_as_operating_companies=False)
    # The daily alert is the same conditional-plan path as the preopen delivery: without
    # this pass nothing in the scan could ever carry a buy plan, because a daily close is
    # never a live execution confirmation.
    from preopen_signals import evaluate_preopen_markets
    evaluate_preopen_markets(report, datetime.now(timezone.utc))
    # The momentum pass is done, so the names it dropped for turnover alone can be read
    # without touching its decisions: they never enter ``preopen_signal``.
    # The quality block is a record of what the same session's scan already read. Reading
    # it again costs a second issuer pass (DART map, quotes, price history) and, on the
    # requested report, that pass alone can push the run past its budget.
    if report.get('quality_watch') and _same_session(report.get('quality_watch_at'), checked):
        print('Quality watch reused from this session; no second issuer read')
    else:
        quality_watch_research(report, datetime.now(timezone.utc))
    # 중기 +30% 눌림 발굴은 20~60일 보유 전제의 관찰 목록이다. 당일 매수 판정과 분리한
    # 별도 필드(``mid_term_watch``)에만 쓰고 ``preopen_signal``·``buy_evaluation``은 건드리지 않는다.
    # 후보군(30~50종목)만 최근 실적·공시를 읽어 적자·유증을 걸러내고 재료 1줄을 붙인다.
    import mid_term_watch
    try:
        import mid_term_evidence
        mid_term_watch.attach(report, datetime.now(timezone.utc),
                              evidence=mid_term_evidence.collect)
    except Exception as exc:
        print('Mid-term fundamentals screen unavailable (' + type(exc).__name__ +
              '); list kept on the chart condition')
        mid_term_watch.attach(report, datetime.now(timezone.utc))
    # The message names conditions plans and watch names, so read their live quote once
    # here (bounded per market). A closed session or a missing credential is recorded, not
    # raised: the scan and the alert go out either way.
    try:
        import live_quote
        live_quote.attach(report, datetime.now(timezone.utc))
    except Exception as exc:
        print('Live quote lookup unavailable (' + type(exc).__name__ + ')')
        report['live_quotes'] = {'status': 'error', 'reason': type(exc).__name__}


QUALITY_WATCH_LIMIT = 8


def _same_session(stamp, at):
    """Whether ``stamp`` falls on the Korean trading day of ``at``."""
    try:
        observed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return False
    if observed.utcoffset() is None or at is None:
        return False
    return (observed.astimezone(ZoneInfo('Asia/Seoul')).date()
            == at.astimezone(ZoneInfo('Asia/Seoul')).date())


def quality_watch_research(report, at):
    """Read the 10억~50억 turnover issuers the momentum screen drops, and grade them.

    For each one: the statements and broker consensus the scan already fetches, plus the
    measured foreign/institutional flow, then the two axes of ``company_forecast``.
    A name that is fundamentally sound and already bought by foreigners and institutions
    is a '수급 유입 우량주' candidate; one that is sound but not yet bought is a
    '수급 대기 우량주' watch entry, kept here as a record only. Only the inflow grade is
    printed in the Telegram message. Neither is a buy signal and neither is written back
    into the row's selection, so the momentum path stays exactly as it was.
    """
    from candidate_selection import quality_watch_reason, average_turnover
    from company_forecast import QUALITY_FUNDAMENTAL_SCORE
    from issuer_consensus import consensus_for
    from supply_flow import evidence_for, share_line
    report['quality_watch'] = []
    pool = []
    for item in report.get('results', []):
        if item.get('market') != 'KR' or is_fund(item):
            continue
        if item.get('selection_filter') != 'insufficient_liquidity':
            continue
        if quality_watch_reason(item, 'KR') is not None:
            continue
        pool.append(item)
    if not pool:
        return []
    pool.sort(key=lambda item: (-average_turnover(item['indicators']), item['symbol']))
    selected = pool[:QUALITY_WATCH_LIMIT]
    try:
        from kis_intraday import company_context
        companies, _risks = company_context('KRX', [item['symbol'].split('.')[0] for item in selected], at)
    except Exception as exc:
        report['quality_watch_error'] = type(exc).__name__
        return []
    entries = []
    for item in selected:
        company = companies.get(item['symbol'].split('.')[0], {})
        if not company:
            continue
        supply = evidence_for(item, company, at)
        consensus = consensus_for(item, company, at=at, price=item['indicators']['price'])
        try:
            forecast = build_company_forecast(
                company, {**item['indicators'], **company.get('price_indicators', {})},
                item['indicators']['price'], at, [], report.get('sectors', []),
                supply=supply, consensus=consensus)
        except (ValueError, KeyError, TypeError) as exc:
            report.setdefault('quality_watch_errors', []).append(
                {"symbol": item['symbol'], "error": type(exc).__name__})
            continue
        grade = forecast.get('quality_grade')
        fundamental = forecast.get('fundamental_score')
        if grade is None and (fundamental is None or fundamental < QUALITY_FUNDAMENTAL_SCORE):
            continue
        entries.append({
            "market": "KR", "symbol": item['symbol'], "name": item.get('name'),
            "grade": grade or "supply_waiting_quality",
            "fundamental_score": fundamental, "demand_score": forecast.get('demand_score'),
            "fundamental_coverage": forecast.get('fundamental_coverage'),
            "target_upside_pct": forecast.get('target_upside_pct'),
            "analyst_target": forecast.get('analyst_target'),
            "supply": supply, "supply_line": share_line(supply),
            "supply_direction": supply.get('direction'), "supply_score": supply.get('score'),
            "price": item['indicators']['price'],
            "average_turnover": average_turnover(item['indicators']),
            "indicators": item['indicators'],
            "reason": "fundamentals_and_broker_target_with_supply_evidence",
            "entry_signal": False})
    entries.sort(key=lambda entry: (entry['grade'] != "supply_inflow_quality",
                                    -(entry['fundamental_score'] or 0)))
    report['quality_watch'] = entries
    report['quality_watch_at'] = at.isoformat()
    report['quality_watch_scope'] = dict(tier='10억~50억 20일 평균 거래대금',
                                         candidates=len(pool), read=len(selected),
                                         graded=len(entries))
    return entries


def quality_watch_line(entry):
    """One compact entry for a quality name: the name, who is buying, the target.

    The owner asked for the short form on 2026-11-25 ("띄울거면 간단하게적어 종목,
    호재,목표가 이정도만"): a name line, one line of evidence and the target — no grade
    labels, turnover recap or repeated disclaimers. What the block is (a review, not a
    buy signal) is said once in its heading.
    """
    name = entry.get('name') or entry.get('symbol')
    code = (entry.get('symbol') or '').split('.')[0]
    label = ('수급 유입 우량주' if entry.get('grade') == 'supply_inflow_quality'
             else '수급 대기')
    fundamental = entry.get('fundamental_score')
    target = entry.get('analyst_target') or {}
    upside = entry.get('target_upside_pct')
    head = f"• {name} ({code})"
    if entry.get('grade') != 'supply_inflow_quality':
        head += f" · {label}"
    if fundamental is not None:
        head += f" · 펀더멘탈 {fundamental:.0f}"
    detail = []
    supply_line = entry.get('supply_line')
    if supply_line:
        # '외인·기관 +25.5억원 · 거래대금 대비 +8.78% · 2일 연속 순매수 · 4/5일 순매수'
        # keeps only the amount and the streak: the ratio and the second count say the
        # same thing twice. '외인·기관' itself contains '·', so it is matched, not split.
        amount = re.search(r"외인·기관\s*[+-][\d.,]+억원", str(supply_line))
        streak = re.search(r"\d+일 연속 순매수", str(supply_line))
        if amount:
            detail.append(amount.group(0).replace('원', ''))
        if streak:
            detail.append(streak.group(0))
    if target.get('target_price') and upside is not None:
        detail.append(f"목표 {target['target_price']:,.0f}원 ({upside:+.0f}%)")
    quote = entry.get('live_quote')
    if isinstance(quote, dict) and quote.get('status') == 'observed':
        price = quote.get('price')
        if isinstance(price, (int, float)):
            detail.append(f"현재 {price:,.0f}원")
    lines = [head]
    if detail:
        lines.append("  " + " · ".join(detail))
    return "\n".join(lines)


def telegram_rebound_line(item):
    """A rebound entry in the short form: why this name, then the entry rule.

    The track is priced from the next session's open, which a preopen report does not
    have yet, so the line carries the dip rule and the flat-open reference level; once
    the open prints the real limit replaces them.
    """
    plan = item.get('preopen_signal') or {}
    if plan.get('track') != 'rebound':
        return ''
    import rebound_track
    overseas = item.get('market') == 'US'
    symbol = item['symbol']
    code = symbol if overseas else symbol.split('.')[0]
    name = item.get('name') or symbol
    values = plan.get('evidence') or item.get('indicators') or {}
    limit = rebound_track.price_text(plan.get('entry_limit'), item.get('market'))
    flat = rebound_track.price_text(plan.get('flat_open_entry_limit'), item.get('market'))
    if limit:
        entry = f"눌림 지정가 {limit} · 시가 확인됨"
    else:
        entry = (f"눌림 지정가 {rebound_track.offset_label(plan['strategy'])}"
                 + (f" · 갭 없이 열리면 {flat}" if flat else ''))
    numbers = []
    rsi = values.get('rsi14')
    volume = values.get('volume_ratio')
    change = values.get('return_1d')
    if plan.get('strategy') == 'oversold_rebound' and isinstance(rsi, (int, float)):
        numbers.append(f"RSI {rsi:.1f}")
    if isinstance(change, (int, float)) and not isinstance(change, bool):
        numbers.append(f"당일 {change:+.1%}")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        numbers.append(f"거래량 {volume:.1f}배")
    return (f"• {name} ({code}) · {plan.get('label')}\n"
            + ("  " + " · ".join(numbers) + "\n" if numbers else "")
            + f"  {entry} · 5일 보유 · 갭하락 −1%면 취소" + quote_note(item))


def telegram_early_entry_line(item):
    """An early entry in the short form: why this name, then the open −1% limit.

    The study's entry is a resting limit 1% under the next session's open (+0.530% over 5
    sessions against +0.069% bought at the open). Until the open prints, the plan carries
    the rule and the flat-open reference level; once it prints, the real limit is shown.
    """
    plan = item.get('preopen_signal') or {}
    if plan.get('track') != 'early_entry':
        return ''
    from exit_policy import exit_policy_for
    overseas = item.get('market') == 'US'
    symbol = item['symbol']
    code = symbol if overseas else symbol.split('.')[0]
    name = item.get('name') or symbol
    score = plan.get('composite_score')
    price_text = lambda value: (f"${value:,.2f}" if overseas else f"{value:,.0f}원")
    entry = plan.get('entry_limit')
    flat = plan.get('flat_open_entry_limit')
    if entry:
        limit_line = f"시가 −1% 지정가 {price_text(entry)} · 시가 확인됨"
    else:
        limit_line = ("시가 −1% 지정가"
                      + (f" · 갭 없이 열리면 {price_text(flat)}" if flat else ""))
    evidence = plan.get('evidence') or {}
    numbers = []
    days = evidence.get('alignment_days')
    if isinstance(days, int):
        numbers.append(f"MA5>MA20 {days}일차")
    elif evidence.get('ma20_cross_up'):
        numbers.append("MA20 첫 돌파일")
    volume = evidence.get('volume_ratio')
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        numbers.append(f"거래량 {volume:.1f}배")
    qualifier = plan.get('qualifier_label')
    if qualifier:
        numbers.append(qualifier)
    policy = exit_policy_for(item)
    heading = (f"• {name} ({code}) · 종합 {score:.1f} · 단기 상승 초입" if score is not None
               else f"• {name} ({code}) · 단기 상승 초입")
    return (heading + "\n"
            + ("  " + " · ".join(numbers) + "\n" if numbers else "")
            + f"  {limit_line} · 5거래일 보유 · 손절 −{policy.stop_pct:.0%} · 미체결 시 추격 없음"
            + quote_note(item))


def reference_note(plan):
    """'(11/24 종가)' — the close a conditional plan's limit was built from.

    The date was a line of its own under every plan; without it the limit price has no
    stated basis, so it stays, attached to the price.
    """
    value = str((plan or {}).get('reference_date') or '')
    if len(value) != 10:
        return ""
    return f" ({value[5:7]}/{value[8:10]} 종가)"


def plan_target_line(exit_policy):
    """'목표 +6%(5거래일)' — 계획 줄이 말하는 목표는 사용자의 매도 규칙이다.

    2026-11-26 발송본은 촉매 계획마다 `forecast['bull_price']`만 `목표`로 인쇄했다
    (삼성전기 +38.5% · NHN +49.9%). 5거래일 목표로 읽을 수 없는 상승 시나리오이고,
    실제 매도 기준은 +6%(급등 시 트레일링)이다. 예측 시나리오는 `강세 참고`로 구분해 붙인다.
    """
    return f"목표 +{exit_policy.take_profit_pct:.0%}(5거래일)"


def scenario_note(bull_target, overseas=False):
    """' · 강세 참고 1,045,793원' — 시나리오일 뿐 목표가 아니라는 표시를 붙인다."""
    if not isinstance(bull_target, (int, float)) or isinstance(bull_target, bool):
        return ""
    return f" · 강세 참고 {bull_target:,.2f}$" if overseas else f" · 강세 참고 {bull_target:,.0f}원"


def telegram_candidate_line(item):
    forecast = item.get("company_forecast", {})
    signal = item.get("signal", {})
    preopen = item.get('preopen_signal', {})
    if preopen.get('track') == 'rebound':
        return telegram_rebound_line(item)
    if preopen.get('track') == 'early_entry':
        return telegram_early_entry_line(item)
    conditional = preopen.get('status') == 'conditional_buy'
    entry = preopen.get('entry_limit') if conditional else signal.get("entry_limit")
    score = forecast.get("composite_score")
    advance=item.get('advance_signal',{})
    if advance.get('status')=='advance_candidate' and not conditional and score is not None:
        return (f"• {item.get('name') or item['symbol']} ({item['symbol'].split('.')[0]}) · 종합 {score:.1f} · 사전 관찰\n"
                f"  정책 재료 {advance['event_window_start']}~{advance['event_window_end']}")
    if (signal.get("confirmed") is not True and not conditional) or score is None or entry is None:
        return ""
    symbol = item["symbol"]
    overseas = item["market"] == "US"
    name = item.get("name") or symbol
    # The alert names the stop of this instrument, not of the default policy.
    exit_policy = exit_policy_for(item)
    kind = " · ETF" if exit_policy.stop_pct == FUND_STOP_PCT else ""
    code = symbol if overseas else symbol.split(".")[0]
    entry_label = "$" + f"{entry:,.2f}" if overseas else f"{entry:,.0f}원"
    upside = forecast.get("bull_price")
    label = '조건부 매수' if conditional else '매수 확인'
    if conditional and preopen.get('strategy') in ('event_catalyst','earnings_catalyst'):
        track = preopen.get('strategy')=='earnings_catalyst'
        floor=preopen.get('entry_floor')
        floor_label=(f'${floor:,.2f}' if overseas else f'{floor:,.0f}원') if floor else None
        entry_zone=f'{floor_label}~{entry_label}' if floor_label else entry_label
        heading='[실적·호재 촉매]' if track else '[호재 촉매]'
        # 호재 한 줄: the disclosure and when it was made. Without it the name is a chart
        # with a number, and the reader cannot check what the news was.
        basis_line=''
        evidence=preopen.get('catalyst') or {}
        if evidence.get('summary'):
            when=''
            try:
                when=datetime.fromisoformat(evidence['observed_at']).astimezone(
                    ZoneInfo('Asia/Seoul')).strftime('%m/%d')
            except (KeyError,TypeError,ValueError):when=''
            basis_line=f"  호재: {evidence['summary']}" + (f' ({when})' if when else '') + '\n'
        return (f"• {name} ({code}) · 종합 {score:.1f} · {heading}{kind}\n"
                f"{basis_line}"
                f"  매수 {entry_zone} · {plan_target_line(exit_policy)}"
                f" · 손절 −{exit_policy.stop_pct:.0%}"
                + scenario_note(upside, overseas) + reference_note(preopen) + quote_note(item))
    return (f"• {name} ({code}) · {label}{kind} · 종합 {score:.1f}\n"
            f"  매수 상한 {entry_label} · {plan_target_line(exit_policy)}"
            f" · 손절 −{exit_policy.stop_pct:.0%}"
            + (scenario_note(upside, overseas) if conditional else '')
            + reference_note(preopen) + quote_note(item))


CAVEAT_LABELS = {
    "valuation_outlook": "밸류에이션", "sector_pressure": "업종 수급", "events": "이벤트",
    "unread_event_bodies": "기사 본문", "cash_flow_quality": "현금흐름",
    "world_event_coverage": "세계 이벤트", "causal_event_analysis": "이벤트 분석",
    "growth_scale_unverified": "매출 성장 검증", "leverage_unverified": "부채 검증",
    "growth": "매출 성장", "earnings_quality": "이익의 질", "balance_sheet": "재무구조",
    "price_and_demand": "가격·수요",
}


def _theme_issue(snapshots):
    """The first theme's issue line, as the mood header quotes it.

    Used only to drop the duplicate: the same headline was printed once in the mood line
    and again under theme 1 in the 2026-11-25 07:50 message.
    """
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        for row in snapshot.get('rows') or []:
            issue = row.get('issue') if isinstance(row, dict) else None
            if issue and '뉴스 미확인' not in issue:
                text = str(issue).strip()
                return text[len('이슈:'):].strip() if text.startswith('이슈:') else text
    return None


def caveat_summary(report, keys=None):
    """One line saying which evidence the whole block is thin on, or '' when none.

    ``keys`` reads the per-name forecasts of the rows the message prints; without it the
    evaluation's own caveat counts are used, so a block of plans and a block of names
    cannot disagree about what is missing.
    """
    counts = {}
    if keys:
        for row in keys:
            for key in (row.get('company_forecast') or {}).get('caveats') or []:
                counts[key] = counts.get(key, 0) + 1
    else:
        counts = dict((report.get('buy_evaluation') or {}).get('caveats') or {})
    labels = [CAVEAT_LABELS.get(key, key) for key in
              sorted(counts, key=lambda key: -counts[key])][:3]
    return (", ".join(labels) + " 자료 미확보 종목은 점수에 감점 반영") if labels else ""


def quote_note(item):
    """The observed live quote of this row, or nothing at all.

    A closed session, a missing credential and a failed call used to print the same line
    under every name — twelve identical '장 마감 · 호가 미확인' rows in the 2026-11-25
    07:50 alert, where the market had not even opened. What matters per name is the
    quote when it was actually read; the rest is said once, by the block.
    """
    from live_quote import quote_line
    quote = (item or {}).get('live_quote')
    if not isinstance(quote, dict) or quote.get('status') != 'observed':
        return ""
    text = quote_line(quote, (item or {}).get('market'))
    return "\n  " + text if text else ""

def benchmark_daily_closes(benchmark):
    """Benchmark close series without an unfinished bar or a duplicated session.

    The index is downloaded separately from the sector ETFs, so its last row is
    sometimes the current day with no close yet (NaN). Left in place, that row
    makes the excess return NaN, which drops the sector_pressure component and
    therefore the whole composite score.
    """
    if benchmark is None:
        raise ValueError("시장 기준 지표 없음")
    if "Close" not in benchmark.columns:
        raise ValueError("시장 기준 지표에 종가 열 없음")
    columns = [name for name in ("Close", "Adj Close") if name in benchmark.columns]
    closes = benchmark[columns].replace([np.inf, -np.inf], np.nan).dropna(subset=["Close"])
    closes = closes.loc[closes["Close"] > 0]
    closes = closes.loc[~closes.index.duplicated(keep="last")].sort_index()
    if len(closes) < 2:
        raise ValueError("시장 기준 지표 종가 2개 미만")
    return closes


def sector_metrics(prices, benchmark):
    data = prices[["Close", "Adj Close", "Volume"]].replace([np.inf, -np.inf], np.nan).dropna()
    data = data.loc[~data.index.duplicated(keep="last")].sort_index()
    if len(data) < 21:
        raise ValueError("섹터 일봉 21개 미만")
    # Compare only sessions both series hold. When the index still lags the sector
    # ETF by a session (premarket runs), the excess return is computed on the last
    # shared session instead of being dropped altogether.
    benchmark = benchmark_daily_closes(benchmark)
    common = data.index.intersection(benchmark.index)
    if len(common) < 21:
        raise ValueError("섹터와 시장 기준 지표의 공통 거래일 21개 미만")
    lag = int(len(data.index) - len(common))
    data = data.loc[common]
    benchmark = benchmark.loc[common]

    turnover = data["Close"] * data["Volume"]
    average_turnover = float(turnover.iloc[-21:-1].mean())
    if average_turnover <= 0:
        raise ValueError("유효한 거래대금 기준 없음")
    dates = common[-2:]

    change = float(data["Adj Close"].iloc[-1] / data["Adj Close"].iloc[-2] - 1)
    market_change = float(benchmark["Close"].iloc[-1] / benchmark["Close"].iloc[-2] - 1)
    ratio = float(turnover.iloc[-1] / average_turnover)
    relative = change - market_change
    previous_ratio = (float(turnover.iloc[-2] / turnover.iloc[-22:-2].mean())
                      if len(data) >= 22 and turnover.iloc[-22:-2].mean() > 0 else None)
    early = change > 0 and relative > 0 and ratio >= 1.3
    return {
        "data_date": dates[-1].date().isoformat(),
        "benchmark_lag_sessions": lag,
        "return_pct": safe_float(change * 100),
        "market_excess_pct": safe_float(relative * 100),
        "estimated_turnover": safe_float(turnover.iloc[-1]),
        "turnover_ratio": safe_float(ratio),
        "active": change > 0 and relative > 0 and ratio >= SECTOR_TURNOVER_THRESHOLD,
        "early": early,
        "starting": early and previous_ratio is not None and previous_ratio < 1.3,
        "previous_turnover_ratio": previous_ratio,
        "source": "daily_vs_20d",
    }


def scan_sectors(benchmarks):
    results, errors = [], []
    selection_now = datetime.now(timezone.utc)
    for market, sectors in SECTOR_ETFS.items():
        prices = yf_download(
            list(sectors), period="3mo", interval="1d", auto_adjust=False,
            progress=False, group_by="ticker", threads=6, timeout=15,
            multi_level_index=True,
        )
        if prices is None:
            errors.append({"market": market, "error": "sector daily download unavailable"})
            continue
        for symbol, name in sectors.items():
            try:
                benchmark = index_frame(benchmarks, market)
                if benchmark is not None:
                    benchmark = completed_daily_prices(benchmark, market, selection_now)
                metrics = sector_metrics(completed_daily_prices(prices[symbol], market, selection_now), benchmark)
                results.append({"market": market, "symbol": symbol, "name": name, **metrics})
            except (KeyError, ValueError, ArithmeticError) as exc:
                errors.append({"market": market, "symbol": symbol, "error": str(exc)})
    print(f"Sector ETFs: {len(results)} analyzed, {len(errors)} errors")
    return results, errors


def sector_flow_snapshot(market, now):
    """Prefer dated intraday comparisons and fall back to the last completed daily session."""
    sectors = SECTOR_ETFS[market]
    try:
        rows = fetch_intraday_sectors(sectors, BENCHMARKS[market], market, now)
        if rows:
            return rows
    except Exception as exc:
        print(f"{market} sector minute data unavailable ({type(exc).__name__})")
    rows = []
    try:
        prices = yf_download([*sectors, BENCHMARKS[market]], period="3mo", interval="1d",
                             auto_adjust=False, group_by="ticker", progress=False,
                             threads=4, timeout=8, multi_level_index=True)
        if prices is None:
            return rows
        benchmark = prices[BENCHMARKS[market]]
        benchmark = benchmark.loc[benchmark.index.date < now.date()]
        for symbol, name in sectors.items():
            try:
                data = prices[symbol]
                data = data.loc[data.index.date < now.date()]
                metrics = sector_metrics(data, benchmark)
                rows.append({"market": market, "symbol": symbol, "name": name, **metrics})
            except (KeyError, ValueError, ArithmeticError):
                continue
    except Exception as exc:
        print(f"{market} sector daily data unavailable ({type(exc).__name__})")
    return rows


def market_regime_note(report, market=None):
    """Say when the market itself held candidates back, so a quiet day is not a silent zero.

    ``market`` limits the note to the market the message is for: a Korean preopen alert
    carrying 'S&P500(SPY) 추세 통과 8종목은 시장 역풍으로 보류' describes a session the
    reader cannot act on in this message.
    """
    names = {"KR": "KOSPI", "US": "S&P500(SPY)"}
    lines = []
    for name, data in sorted((report.get("market_regime") or {}).items()):
        if market and name != market:
            continue
        if data is None or not data.get("observed"):
            lines.append(f"시장 국면({names.get(name, name)}): 지수 확인 불가 · 국면 필터 미적용")
            continue
        state = "상승 국면(지수 > 20일선)" if data.get("above_ma20") else "하락 국면(지수 < 20일선)"
        note = f"시장 국면({names.get(name, name)}): {state}"
        # 국내는 코스피·코스닥을 따로 본다. 종목은 자기 시장의 지수로 판정되므로 둘을 함께 쓴다.
        for symbol, item in (data.get("indices") or {}).items():
            if symbol == data.get("index_symbol") or not isinstance(item, dict):
                continue
            if item.get("above_ma20") is None:
                continue
            label = INDEX_LABELS.get(symbol, symbol)
            note += (f" · {label} "
                     + ("상승 국면" if item["above_ma20"] else "하락 국면"))
        if data.get("above_ma20") is False and data.get("headwind_skipped"):
            note += f" · 추세 통과 {data['headwind_skipped']}종목은 시장 역풍으로 보류"
        # 지수 봉이 종목 봉보다 한 세션 늦으면 그 사실을 말한다. 전일 국면이 오늘 국면으로 읽히지 않게.
        lag = data.get("lag_sessions")
        if isinstance(lag, int) and lag > 0 and data.get("index_date"):
            note += (f" · 지수 기준일 {data['index_date'][5:].replace('-', '/')}"
                     f"({lag}세션 지연)")
        lines.append(note)
    return lines


def evaluation_view(report, market=None):
    """The buy evaluation of the market this message is for.

    A US premarket alert said '판정 완료 39종목' because the merged count carried the
    Korean pass too; the per-market part is what this message judged.
    """
    parts = report.get('buy_evaluation_by_market') or {}
    if market and isinstance(parts, dict) and isinstance(parts.get(market), dict):
        return parts[market]
    return report.get('buy_evaluation') or {}


def delivery_regime_market(market=None):
    """The market a delivery belongs to ('NXT'/'KRX' → KR, 'US*' → US)."""
    value = str(market or os.getenv('DELIVERY_MARKET') or '').upper()
    if value.startswith('US'):
        return 'US'
    if value in ('NXT', 'KRX'):
        return 'KR'
    return None


def send_telegram_summary(report):
    """Send the daily/preopen summary and report what actually happened.

    Returns a dict so a caller can tell "sent" from "skipped by the delivery window"
    instead of finding an empty list and guessing.
    """
    result = dict(market=os.getenv('DELIVERY_MARKET') or None, parts_sent=0, sent=False,
                  suppressed_by_gate=False, reason=None)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token and not chat_id:
        print("Telegram skipped: configuration not set")
        result.update(suppressed_by_gate=True, reason="configuration_not_set")
        return result
    if not token or not chat_id:
        raise RuntimeError("Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    allowed,reason=delivery_status(os.getenv('DELIVERY_MARKET'),datetime.now(timezone.utc))
    result["reason"]=reason
    if not allowed:
        print('Telegram suppressed_by_gate: '+reason)
        result["suppressed_by_gate"]=True
        return result

    # 알림 최상단: 지금 상승 흐름 · 시장 분위기 · (바뀌었으면) 흐름 변화.
    regime_lines = asset_regime.header_lines(report.get('asset_regime'))
    # 세부 테마가 있으면 그것만 보낸다. 대분류 ETF 순위('1위 기계·장비')는 같은 내용을 두 번
    # 말하는 것이고, 세부 테마는 그보다 구체적인 같은 정보다. 테마가 비었을 때만 예전 섹터 폴백.
    theme_block = theme_lines(report.get('theme_sectors'))
    # 테마 1번의 이슈는 분위기 줄의 '이슈:'로도 들어간다. 같은 문장을 여덟 줄 안에서 두 번
    # 읽히지 않게, 테마 블록이 그 문장을 실었으면 분위기 줄에서는 뺀다.
    if theme_block:
        issue = _theme_issue(report.get('theme_sectors'))
        if issue:
            regime_lines = [line.replace(f" · 이슈: {issue}", '').replace(f"이슈: {issue}", '').rstrip(' ·')
                            for line in regime_lines]
    sector_header = [] if theme_block else list(
        report.get('sector_header_override') or format_sector_header(report.get("sectors", [])))
    lines = ((regime_lines + ['']) if regime_lines else []) + sector_header + theme_block
    market = delivery_regime_market()
    lines.extend(market_regime_note(report, market))
    # 미국 장전 요약에는 게이트와 무관한 장전 사전 시그널을 붙인다. 이 블록은 발송 전에
    # preopen_delivery/premarket_signals가 준비해 두고, 발송 시점에는 다시 조회하지 않는다.
    from premarket_signals import format_block as format_premarket_block
    premarket_lines = format_premarket_block((report.get('premarket_watch') or {}).get('items'))
    if premarket_lines:
        lines.extend(['', *premarket_lines])
    # 국내 핵심 대표주의 NXT 장전 움직임·촉매·간밤 미국 연동도 같은 참고 블록으로 붙인다.
    # 장전 실행에서 모은 감시만 싣는다(정규장 장중 스캔에는 이미 지난 창문이다).
    kr_watch = report.get('kr_premarket_watch') or {}
    if kr_watch.get('items'):
        from kr_premarket import format_block as format_kr_premarket_block
        from kr_premarket import include_block as include_kr_premarket
        kr_lines = (format_kr_premarket_block(kr_watch.get('items'), kr_watch.get('session'))
                    if include_kr_premarket(kr_watch) else [])
        if kr_lines:
            lines.extend(['', *kr_lines])
    # The reversal tracks are delivered in their own section: they are exempt from the
    # regime gate by design (they are the entries a downtrend leaves on the table), so
    # they must never be read as momentum buys that passed it.
    rebound = [item for item in report["results"]
               if item.get('preopen_signal',{}).get('track')=='rebound']
    rebound.sort(key=lambda item: (-(item['indicators'].get('volume_ratio') or 0), item['symbol']))
    confirmed = [item for item in report["results"]
                 if (item.get("signal", {}).get("confirmed") is True
                 and item.get("signal", {}).get("entry_limit") is not None
                 or item.get('preopen_signal',{}).get('status')=='conditional_buy'
                 or item.get('advance_signal',{}).get('status')=='advance_candidate')
                 and item.get('preopen_signal',{}).get('track')!='rebound'
                 and item.get("company_forecast", {}).get("composite_score") is not None]
    confirmed.sort(key=lambda item: -item['company_forecast']['composite_score'])
    # A preopen price plan is not an executable live entry confirmation.
    conditional_rows = [row for row in confirmed
                        if row.get('preopen_signal', {}).get('status') == 'conditional_buy']
    heading = '조건부 매수 계획' if conditional_rows else '사전 관찰' if confirmed else '분석 결과' if (
        report.get('buy_evaluation', {}).get('completed') is True
        or report.get('buy_evaluation', {}).get('decision_completed', 0) > 0) else '분석 상태'
    lines.extend(["", heading])
    if not confirmed:
        evaluation=evaluation_view(report, market)
        if rebound:
            # Not a silent zero: the momentum side has nothing, the reversal side does.
            lines.append(f"정배열 매수 조건 충족 없음 · 반등 후보 {len(rebound)}종목은 아래")
        elif evaluation.get('decision_completed',0)>0:
            lines.append(f"판정 완료 {evaluation['decision_completed']}종목 · 매수 조건 충족 없음")
            if evaluation.get('data_excluded'):
                lines.append(f"자료 부족 {evaluation['data_excluded']}종목 제외")
            blocked = (evaluation.get('counts') or {}).get('market_headwind')
            if blocked and not (report.get('market_regime') or {}):
                # Stored preopen reports do not carry the regime block; the count still
                # says why nothing qualified instead of leaving a silent zero.
                lines.append(f"시장 역풍 보류 {blocked}종목 (지수 < 20일선)")
        elif evaluation.get('completed') is True:
            lines.append("매수 조건 충족 종목 없음")
        else:
            lines.append("매수 판정 보류 · 분석 미완료")
    # 계획마다 반복되던 안내는 블록에 한 번씩만 쓴다: 같은 문장을 다섯 번 읽히던 줄들
    # (지수 20일선 아래 예외, 시가·거래량 게이트, 손절·익절 문구)이 2026-11-25 07:50
    # 메시지의 절반을 차지했다.
    catalysts = [row for row in conditional_rows
                 if row['preopen_signal'].get('strategy') in ('event_catalyst', 'earnings_catalyst')]
    if catalysts:
        from catalyst_track import MIN_ENTRY_VOLUME_RATIO
        lifted = any(row['preopen_signal'].get('market_regime_lifted') for row in catalysts)
        rule = ["확정 공시 근거", f"장초반 시가 아래·거래량 {MIN_ENTRY_VOLUME_RATIO:.1f}배 미달이면 진입 안 함"]
        if lifted:
            rule.insert(0, "지수 20일선 아래는 이 종목만 예외")
        lines.append(" · ".join(rule))
    for item in confirmed[:5]:
        lines.append(telegram_candidate_line(item))
    if len(confirmed) > 5:
        lines.append(f"   {rest_note(len(confirmed), 5)} (분석자료 참고)")
    if rebound:
        lines.extend(["", "[반등 눌림 후보] · 시장 국면 게이트 미적용"])
        for item in rebound[:5]:
            lines.append(telegram_rebound_line(item))
        if len(rebound) > 5:
            lines.append(f"   {rest_note(len(rebound), 5)} (분석자료 참고)")
    # 보유·매도 감시: 직전 세션 계획의 진입가를 기준으로 +6% · 트레일링 · 손절 · 5거래일
    # 만기를 본다(계좌 체결을 읽지 않으므로 가정임을 블록이 밝힌다). 계산은
    # `exit_watch`가 발송 전에 끝내고, 여기서는 그 줄만 싣는다.
    exit_lines = list(report.get('exit_watch_lines') or [])
    if exit_lines:
        lines.extend(['', *exit_lines])
    # 당일 급등했지만 게이트에 막혀 매수 후보에서 빠진 종목은 별도 참고 블록으로만 나간다.
    # 매수 후보 카운트·`preopen_signal`은 건드리지 않는다(urgent_issues는 읽기 전용).
    import urgent_issues
    urgent = urgent_issues.select_urgent_issues(report)
    # 이 블록도 종목을 이름으로 보여 주므로 같은 호가를 붙인다(시장당 3건으로 제한).
    try:
        from live_quote import attach_urgent
        attach_urgent(urgent, datetime.now(timezone.utc))
    except Exception as exc:
        print('Urgent quote lookup unavailable (' + type(exc).__name__ + ')')
    if market and isinstance(urgent, dict):
        # A US premarket alert was listing the Korean 긴급 이슈 block as well: the scan
        # report carries both markets and the message belongs to one of them.
        urgent = {name: rows for name, rows in urgent.items() if name == market}
    report['urgent_issues'] = urgent
    urgent_lines = urgent_issues.format_urgent_block(urgent)
    if urgent_lines:
        lines.extend(['', *urgent_lines])
    # 외인·기관이 실제로 사고 있는 종목만 보낸다. 수급 미유입 대기 종목은 리포트 JSON에
    # 기록으로만 남기고 메시지 본문에는 싣지 않는다
    # (2026-11-24 지시: "수급대기우량주는 안띄워도될듯").
    inflow = [row for row in report.get('quality_watch') or []
              if row.get('grade') == 'supply_inflow_quality'
              and (not market or (row.get('market') or 'KR') == market)]
    if inflow:
        lines.extend(['', '[수급 유입 우량주] · 매수 신호 아님'])
        lines.extend(quality_watch_line(row) for row in inflow[:5])
        if len(inflow) > 5:
            lines.append(f"   {rest_note(len(inflow), 5)}")
    # 블록 단위 안내: 매도 규칙 하나, 자료가 얇은 축, 개장 전이라 호가를 못 읽었다는 사실.
    # 계획마다 반복되던 문단을 여기 한 줄씩으로 모았다.
    notes = []
    if conditional_rows:
        notes.append("※ 익절 +6% · 급등 시 트레일링 보유 · 손절은 실제 체결가 기준")
    thin = caveat_summary(report, [*confirmed, *rebound, *inflow])
    if thin:
        notes.append(f"※ {thin}")
    quotes = report.get('live_quotes') or {}
    if isinstance(quotes, dict) and not quotes.get('observed') and (
            quotes.get('preopen') or quotes.get('market_closed')
            or quotes.get('credentials_missing')):
        notes.append("※ 개장 전 호가 미확인 · 위 계획은 조건부")
    if notes:
        lines.extend(['', *notes])
    # 중기 +30% 눌림 발굴은 20~60일 보유·−15% 손절 전제의 관찰 목록이다. 알림 본문에는 싣지 않고
    # 분석 자료(`mid_term_watch`·`mid_term_watch_scope`)에만 남긴다(2026-11-24 지시:
    # "매수종목 뽑을 때 분석자료에 포함하라고 한건데").

    messages = split_telegram_text("\n".join(lines))
    for message in messages:
        allowed,reason=delivery_status(os.getenv('DELIVERY_MARKET'),datetime.now(timezone.utc))
        if not allowed:
            print('Telegram suppressed_by_gate before send: '+reason)
            result.update(suppressed_by_gate=True, reason=reason)
            return result
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Telegram returned HTTP {response.status_code}")
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise RuntimeError("Telegram did not confirm delivery")
        except (requests.RequestException, ValueError):
            raise RuntimeError("Telegram request failed or returned invalid JSON") from None
    
    result.update(sent=bool(messages), parts_sent=len(messages))
    print(f"Telegram confirmed: {len(messages)} daily report parts")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["sample", "full", "universe"],
        default="full"
    )
    args = parser.parse_args()

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if args.mode == "universe":
        names = load_universe()
        output = REPORT_DIR / "universe.json"
        output.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
        for market, symbols in names.items():
            print(f"{market} universe: {len(symbols)} instruments")
        for market, symbol in [("KR", "069500.KS"), ("US", "AAPL"), ("US", "QQQ")]:
            print(f"{market} {symbol}: {names[market][symbol]}")
        return

    full_scan = args.mode == "full"
    if full_scan:
        names = load_universe()
        watchlist = {market: list(items) for market, items in names.items()}
    else:
        names = {"KR": KR_SYMBOL_NAMES, "US": US_SYMBOL_NAMES}
        watchlist = WATCHLIST

    benchmarks = {}
    benchmark_errors = []

    for market, symbols in MARKET_INDEX_SYMBOLS.items():
        if len(symbols) == 1:
            symbol = symbols[0]
            try:
                benchmarks[market] = download_prices(symbol)
            except Exception as exc:
                benchmarks[market] = None
                benchmark_errors.append({"market": market, "symbol": symbol, "error": str(exc)})
            continue
        # 국내는 코스피·코스닥을 모두 받는다. 코스닥 종목을 코스피 하나로 판정하지 않게.
        frames = {}
        for symbol in symbols:
            try:
                frames[symbol] = download_prices(symbol)
            except Exception as exc:
                frames[symbol] = None
                benchmark_errors.append({"market": market, "symbol": symbol, "error": str(exc)})
        benchmarks[market] = frames

    inverse_prices = {"KR": {}, "US": {}}
    for market, tickers in watchlist.items():
        underlying = {INVERSE_BENCHMARKS[ticker] for ticker in tickers if ticker in INVERSE_BENCHMARKS}
        for symbol in sorted(underlying):
            try:
                inverse_prices[market][symbol] = download_prices(symbol)
            except Exception as exc:
                benchmark_errors.append({"market": market, "symbol": symbol, "error": str(exc)})

    all_results = []
    all_errors = []

    for market, tickers in watchlist.items():
        if full_scan:
            results, errors = scan_batches(market, names[market], benchmarks[market], inverse_prices[market])
        else:
            results, errors = scan_market(market, tickers, benchmarks[market], names=names[market],
                                          inverse_prices=inverse_prices[market])

        all_results.extend(results)
        all_errors.extend(errors)

    attempted = sum(
        len(set(tickers))
        for tickers in watchlist.values()
    )
    sectors, sector_errors = scan_sectors(benchmarks) if all_results else ([], [])
    market_regime = {}
    for market in BENCHMARKS:
        rows = [item for item in all_results if item["market"] == market]
        indices = {}
        for symbol in MARKET_INDEX_SYMBOLS[market]:
            same = [item for item in rows if index_symbol_for(item["symbol"], market) == symbol]
            above = next((item["indicators"].get("market_above_ma20") for item in same
                          if item["indicators"].get("market_above_ma20") is not None), None)
            ma20 = next((item["indicators"].get("market_ma20") for item in same
                         if item["indicators"].get("market_ma20") is not None), None)
            date = next((item["indicators"].get("market_ma20_date") for item in same
                         if item["indicators"].get("market_ma20_date")), None)
            lag = next((item["indicators"].get("market_lag_sessions") for item in same
                        if item["indicators"].get("market_lag_sessions") is not None), None)
            indices[symbol] = {"above_ma20": above, "index_ma20": ma20,
                               "index_date": date, "lag_sessions": lag,
                               "observed": above is not None}
        head = indices.get(BENCHMARKS[market], {})
        headwind_skipped = sum(1 for item in rows if item.get("selection_filter") == "market_headwind")
        market_regime[market] = {"index_symbol": BENCHMARKS[market],
                                 "above_ma20": head.get("above_ma20"),
                                 "index_ma20": head.get("index_ma20"),
                                 "index_date": head.get("index_date"),
                                 "lag_sessions": head.get("lag_sessions"),
                                 "headwind_skipped": headwind_skipped,
                                 "observed": head.get("above_ma20") is not None,
                                 "indices": indices}

    report = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "scan_mode": args.mode,
        "universe_scope": FULL_SCOPE if full_scan else "고정 관심종목 표본",
        "full_market_scan": full_scan,
        "attempted": attempted,
        "successful": len(all_results),
        "failed": len(all_errors),
        "status": "partial" if all_errors or benchmark_errors or sector_errors else "complete",
        "error_summary": summarize_scan_errors(all_errors),
        "benchmark_errors": benchmark_errors,
        "market_regime": market_regime,
        "sectors": sectors,
        "sector_errors": sector_errors,
        "theme_sectors": [],
        "market_summary": {
            market: {
                "attempted": len(set(tickers)),
                "successful": sum(
                    1 for item in all_results
                    if item["market"] == market
                ),
                "failed": sum(
                    1 for item in all_errors
                    if item["market"] == market
                )
            }
            for market, tickers in watchlist.items()
        },
        "results": all_results,
        "errors": all_errors,
        "limitations": [
            "시세 미지원 또는 이력 부족 종목은 오류로 기록",
            "섹터 거래대금은 대표 ETF 종가×거래량 추정치이며 투자자 순매수·ETF 순유입은 미수집",
            "일봉 기반이며 실시간 매수 확정 불가",
            "완료된 거래일 일봉 기준이며 일중 VWAP·실시간 호가 미포함",
            "VWAP는 별도 장중 감시에서 체결 기반으로 평가",
            "기사·공시 조회는 발송 후보에 한정하며 거시 이벤트의 기업 실적 영향 정량평가는 미구현",
            "발송 후보의 분기 손익·매출 증감은 조회하나 기업가치 전체 평가는 미구현",
            "백테스트 및 표본 외 검증 미구현",
            "실제 투자 승률 검증되지 않음"
        ]
    }

    if os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("DART_API_KEY"):
        checked = datetime.now(timezone.utc)
        report["sectors"] = [
            row for market, zone in (("KR", "Asia/Seoul"), ("US", "America/New_York"))
            for row in sector_flow_snapshot(market, checked.astimezone(ZoneInfo(zone)))
        ]
        report["theme_sectors"] = []
        for market, zone in (("KR", "Asia/Seoul"), ("US", "America/New_York")):
            try:
                report["theme_sectors"].append(
                    theme_sector_snapshot(market, checked.astimezone(ZoneInfo(zone))))
            except Exception as exc:  # 테마 집계 실패가 전체 리포트를 막지 않는다
                print(f"{market} theme sectors unavailable ({type(exc).__name__})")
        prepare_selected_analysis(report)

    # 자산군 상승 흐름·시장 분위기는 리포트 JSON(`asset_regime`)과 텔레그램 최상단에 함께
    # 남긴다. 조회 실패가 스캔·발송을 막지 않는다.
    try:
        asset_regime.attach(report)
    except Exception as exc:
        print(f"Asset regime unavailable ({type(exc).__name__})")
        report["asset_regime_error"] = type(exc).__name__

    output = REPORT_DIR / "latest.json"

    with output.open(
        "w", encoding="utf-8"
    ) as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
            allow_nan=False
        )

    status_label = "SCAN PARTIAL" if report["status"] == "partial" else "SCAN COMPLETE"
    print(
        f"{status_label}: "
        f"{len(all_results)} success, "
        f"{len(all_errors)} failed"
    )
    if report["error_summary"]:
        print("SCAN ERROR SUMMARY: " + json.dumps(
            report["error_summary"], ensure_ascii=False
        ))

    if not all_results:
        raise SystemExit(
            "전체 데이터 수집 실패"
        )

    try:
        send_telegram_summary(report)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None

if __name__ == "__main__":
    main()
