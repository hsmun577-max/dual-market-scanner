import json
import math
import os
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
from candidate_selection import candidate_rejection_reason, rank_market_candidates
from sector_flow import fetch_intraday_sectors, format_sector_header
from telegram_text import split_telegram_text
from research_asset import is_fund
from company_forecast import build_company_forecast
from delivery_policy import delivery_status
from exit_policy import DEFAULT_EXIT_POLICY, policy_snapshot
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

INVERSE_ETFS = {
    "114800.KS", "252670.KS",
    "SOXS", "SQQQ", "SPXS"
}

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
    names = {}
    for line in lines:
        if len(line) <= 21 + tail_width:
            continue
        head, tail = line[:-tail_width], line[-tail_width:]
        symbol, name = head[:9].strip(), head[21:].strip()
        if tail[:2] in KR_SECURITY_GROUPS and symbol and name:
            names[f"{symbol}.{suffix}"] = name
    return names


def parse_us_master(lines):
    names = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 23:
            continue
        security_type, product_type = fields[8].strip(), fields[22].strip()
        if security_type == "2" or (security_type == "3" and product_type in {"001", "005"}):
            symbol = fields[4].strip().replace(".", "-")
            name = US_SYMBOL_NAMES.get(symbol) or fields[6].strip() or fields[7].strip()
            if symbol and name:
                names[symbol] = name
    return names


def load_universe():
    names = {"KR": {}, "US": {}}
    # Tail widths exclude the newline counted by the KIS examples.
    for filename, suffix, width in [
        ("kospi_code.mst", "KS", 227),
        ("kosdaq_code.mst", "KQ", 221),
    ]:
        instruments = parse_kr_master(load_master(filename), suffix, width)
        if not instruments:
            raise ValueError(f"종목 목록을 읽을 수 없음: {filename}")
        names["KR"].update(instruments)

    for filename in ("nasmst.cod", "nysmst.cod", "amsmst.cod"):
        instruments = parse_us_master(load_master(filename))
        if not instruments:
            raise ValueError(f"종목 목록을 읽을 수 없음: {filename}")
        names["US"].update(instruments)
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


def scan_batches(market, names, benchmark):
    symbols = list(names)
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

        found, failed = scan_market(market, batch, benchmark, names=names, prices=prices)
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

def calculate_indicators(data, benchmark=None):
    close = data["Close"].astype(float)
    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    volume = data["Volume"].astype(float)

    price = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())

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

    if benchmark is not None:
        # Market regime from the index itself: an entry needs the index above its own
        # 20-day average. Left as None (not False) when the index is unavailable so a
        # fetch gap cannot read as a headwind.
        index_closes = benchmark["Close"].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        if len(index_closes) >= 20:
            market_ma20 = float(index_closes.tail(20).mean())
            market_above_ma20 = bool(float(index_closes.iloc[-1]) > market_ma20)

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
        "ma20": safe_float(ma20),
        "ma60": safe_float(ma60),
        "volume": safe_float(volume.iloc[-1]),
        "volume_ratio": safe_float(volume_ratio),
        "volatility_20d": safe_float(volatility_20),
        "atr14_pct": safe_float(100 * pd.concat([high-low, (high-close.shift()).abs(),
            (low-close.shift()).abs()],axis=1).max(axis=1).tail(14).mean()/price),
        "market_return_20d": safe_float(market_return),
        "market_above_ma20": market_above_ma20,
        "market_ma20": safe_float(market_ma20),
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

def build_risk_plan(price, stop_pct=DEFAULT_EXIT_POLICY.stop_pct,
                    target_pct=DEFAULT_EXIT_POLICY.take_profit_pct,
                    risk_budget=30000,
                    policy=DEFAULT_EXIT_POLICY):
    """Sell levels of the current policy: −2% stop, +6% take profit, surge hold.

    ``target_price`` is the default +6% level, not a hard sell when a
    short-term surge carried the price through it: ``exit_policy`` carries the
    trailing parameters used by ``exit_policy.ExitTracker``.
    """
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

def completed_daily_prices(data, market, now=None):
    """Daily candidate selection uses previous local dates; live entries use intraday data."""
    zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
    local_day = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    dates = pd.DatetimeIndex(data.index)
    if dates.tz is not None:
        dates = dates.tz_convert(zone)
    return data.loc[dates.date < local_day].copy()


def scan_market(market, tickers, benchmark, names=None, prices=None):
    names = names or {}
    results = []
    errors = []

    selection_now = datetime.now(timezone.utc)
    if benchmark is not None:
        benchmark = completed_daily_prices(benchmark, market, selection_now)
    for symbol in dict.fromkeys(tickers):
        try:
            data = download_prices(symbol) if prices is None else clean_prices(prices[symbol])
            data = completed_daily_prices(data, market, selection_now)

            indicators = calculate_indicators(
                data, benchmark
            )

            signal = classify_signal(
                indicators,
                inverse=symbol in INVERSE_ETFS
            )

            result = {
                "market": market,
                "symbol": symbol,
                "name": names.get(symbol),
                "asset_type": (
                    "inverse_etf"
                    if symbol in INVERSE_ETFS
                    else "leveraged_etf"
                    if symbol in LEVERAGED_ETFS
                    else "stock_or_etf"
                ),
                "indicators": indicators,
                "signal": signal,
                "risk_plan": build_risk_plan(
                    indicators["price"]
                ),
                "data_checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "fundamentals": "발송 후보 선정 후 조회",
                "news_and_filings": "발송 후보 선정 후 조회",
                "institutional_flows": "미검증"
            }

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
    from research_asset import is_fund
    funds=[row for row in report['results'] if is_fund(row)]
    for row in funds:
        row['analysis_kind']='fund_screen_only'
        for key in ('company_analysis','company_forecast','preopen_signal','advance_signal'):
            row.pop(key,None)
    report['research_scope']=dict(funds_screened=len(funds),funds_graded_as_operating_companies=False)
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
        pool = [item for item in report["results"] if item["market"] == market and not is_fund(item)]
        event_symbols = [item["symbol"] for item in pool for symbol in linked
            if (item["symbol"] == (registry.get(symbol,{}).get("source_symbol") or symbol.removeprefix("DNAS"))
                or (market=="KR" and item["symbol"].split(".")[0]==symbol))]
        selected = select_analysis_candidates(pool, market, event_symbols)
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
            if observed_prices.get('price') and observed_prices.get('data_date'):
                item['indicators'] = observed_prices.copy()
            item["company_analysis"] = company
            item["company_analysis_at"] = company.get("checked_at", checked.isoformat())
            margin = company.get("operating_margin_pct")
            item["fundamentals"] = ("영업흑자" if margin is not None and margin > 0 else
                                    "영업적자" if margin is not None and margin < 0 else
                                    "손익분기" if margin == 0 else "재무자료 조회 실패 또는 미제공")
            item["news_and_filings"] = f"기사 {len(company.get('news', []))}건 · 공시 {len(company.get('filings', []))}건"
            item["company_forecast"] = build_company_forecast(company, {**item["indicators"], **company.get("price_indicators", {})},
                item["indicators"]["price"], datetime.now(timezone.utc),
                [row for row in risks if row.get("symbol") == symbol], report.get("sectors", []))
            item["risk_headlines"] = list(dict.fromkeys(
                row.get("headline", "") for row in risks if row.get("symbol") == symbol))[:2]
            item['risk_evidence']=[row for row in risks if row.get('symbol')==symbol]
            if is_fund(item):
                # A listed fund has no issuer income statement; grading it as an operating
                # company produced "analysis incomplete" rows for ETFs (QQQ, SPY, BIL...).
                # The quote type only arrives with the profile, so it is re-checked here.
                item['analysis_kind']='fund_screen_only'
                item.pop('company_forecast',None)
                item.pop('company_analysis',None)
                item.pop('risk_evidence',None)
    report["company_analysis_at"] = checked.isoformat()
    demoted=[row for row in report['results'] if row.get('analysis_kind')=='fund_screen_only']
    previous=report.get('research_scope',{})
    report['research_scope']=dict(funds_screened=max(len(demoted),previous.get('funds_screened',0) or 0),
                                  funds_demoted_after_profile=len(demoted),
                                  funds_graded_as_operating_companies=False)
    # The daily alert is the same conditional-plan path as the preopen delivery: without
    # this pass nothing in the scan could ever carry a buy plan, because a daily close is
    # never a live execution confirmation.
    from preopen_signals import evaluate_preopen_markets
    evaluate_preopen_markets(report, datetime.now(timezone.utc))


def telegram_candidate_line(item):
    forecast = item.get("company_forecast", {})
    signal = item.get("signal", {})
    preopen = item.get('preopen_signal', {})
    conditional = preopen.get('status') == 'conditional_buy'
    entry = preopen.get('entry_limit') if conditional else signal.get("entry_limit")
    score = forecast.get("composite_score")
    advance=item.get('advance_signal',{})
    if advance.get('status')=='advance_candidate' and not conditional and score is not None:
        return (f"• {item.get('name') or item['symbol']} ({item['symbol'].split('.')[0]}) · 사전 매수 후보 · 종합 {score:.1f}/100\n"
                f"  예정 재료 {advance['event_window_start']}~{advance['event_window_end']}\n"
                "  정책 결과 발표 전 수혜 가능성 분석 · 매수 확정 대기" + caveat_note(item))
    if (signal.get("confirmed") is not True and not conditional) or score is None or entry is None:
        return ""
    symbol = item["symbol"]
    overseas = item["market"] == "US"
    name = item.get("name") or symbol
    kind = " · ETF" if is_fund(item) else ""
    code = symbol if overseas else symbol.split(".")[0]
    entry_label = "$" + f"{entry:,.2f}" if overseas else f"{entry:,.0f}원"
    target, upside = forecast["base_price"], forecast["bull_price"]
    target_label = "$" + f"{target:,.2f} / $" + f"{upside:,.2f}" if overseas else f"{target:,.0f} / {upside:,.0f}원"
    label = '조건부 매수' if conditional else '매수 확인'
    if conditional and preopen.get('strategy')=='event_catalyst':
        floor=preopen['entry_floor']
        floor_label=f'${floor:,.2f}' if overseas else f'{floor:,.0f}원'
        upper_label=f'${upside:,.2f}' if overseas else f'{upside:,.0f}원'
        return (f"• {name} ({code}) · 호재형 조건부 매수 · 종합 {score:.1f}/100\n"
                f"  매수 구간 {floor_label}~{entry_label} · 눌림 구간 진입 시\n"
                f"  기준 종가 {preopen['reference_date']} · 상승 시나리오 목표 {upper_label}\n"
                f"  손절 −{DEFAULT_EXIT_POLICY.stop_pct:.0%} · 기본 익절 +{DEFAULT_EXIT_POLICY.take_profit_pct:.0%} · "
                f"단기 급등 시 {DEFAULT_EXIT_POLICY.take_profit_pct:.0%} 매도 대신 트레일링 보유,\n"
                "  거래량 급감·고점 대비 하락 신호 시 그 가격에 청산 (실제 체결가 기준)" + caveat_note(item))
    basis = f" · {preopen['reference_date']} 종가 기준" if conditional else ''
    caveat = caveat_note(item)
    return (f"• {name} ({code}) · {label}{kind} · 종합 {score:.1f}/100\n"
            f"  매수 상한 {entry_label}{basis}\n"
            f"  5거래일 분석 목표 · 기준/상승 {target_label}\n"
            f"  손절 −{DEFAULT_EXIT_POLICY.stop_pct:.0%} · 기본 익절 +{DEFAULT_EXIT_POLICY.take_profit_pct:.0%} · "
            f"단기 급등 시 {DEFAULT_EXIT_POLICY.take_profit_pct:.0%} 매도 대신 트레일링 보유,\n"
            "  거래량 급감·고점 대비 하락 신호 시 그 가격에 청산 (실제 체결가 기준)" + caveat)


CAVEAT_LABELS = {
    "valuation_outlook": "밸류에이션", "sector_pressure": "업종 수급", "events": "이벤트",
    "unread_event_bodies": "기사 본문", "cash_flow_quality": "현금흐름",
    "world_event_coverage": "세계 이벤트", "causal_event_analysis": "이벤트 분석",
    "growth_scale_unverified": "매출 성장 검증", "leverage_unverified": "부채 검증",
    "growth": "매출 성장", "earnings_quality": "이익의 질", "balance_sheet": "재무구조",
    "price_and_demand": "가격·수요",
}


def caveat_note(item):
    """Say which evidence is missing instead of hiding a partial assessment."""
    caveats = [key for key in (item.get('company_forecast', {}).get('caveats')
                               or item.get('preopen_signal', {}).get('caveats') or []) if key]
    if not caveats:
        return ""
    labels = [CAVEAT_LABELS.get(key, key) for key in dict.fromkeys(caveats)][:3]
    return "\n  자료 일부 미확보: " + ", ".join(labels) + " (점수에 반영해 감점)"

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
                benchmark = benchmarks.get(market)
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


def market_regime_note(report):
    """Say when the market itself held candidates back, so a quiet day is not a silent zero."""
    names = {"KR": "KOSPI", "US": "S&P500(SPY)"}
    lines = []
    for market, data in sorted((report.get("market_regime") or {}).items()):
        if not data.get("observed"):
            lines.append(f"시장 국면({names.get(market, market)}): 지수 확인 불가 · 국면 필터 미적용")
            continue
        state = "상승 국면(지수 > 20일선)" if data.get("above_ma20") else "하락 국면(지수 < 20일선)"
        note = f"시장 국면({names.get(market, market)}): {state}"
        if data.get("above_ma20") is False and data.get("headwind_skipped"):
            note += f" · 추세 통과 {data['headwind_skipped']}종목은 시장 역풍으로 보류"
        lines.append(note)
    return lines


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

    lines = list(report.get('sector_header_override') or format_sector_header(report.get("sectors", [])))
    lines.extend(market_regime_note(report))
    confirmed = [item for item in report["results"]
                 if (item.get("signal", {}).get("confirmed") is True
                 and item.get("signal", {}).get("entry_limit") is not None
                 or item.get('preopen_signal',{}).get('status')=='conditional_buy'
                 or item.get('advance_signal',{}).get('status')=='advance_candidate')
                 and item.get("company_forecast", {}).get("composite_score") is not None]
    confirmed.sort(key=lambda item: -item['company_forecast']['composite_score'])
    # A preopen price plan is not an executable live entry confirmation.
    heading=('조건부 매수 계획' if any(row.get('preopen_signal',{}).get('status')=='conditional_buy' for row in confirmed)
             else '사전 관찰 후보' if confirmed else '분석 결과' if (report.get('buy_evaluation',{}).get('completed') is True
                 or report.get('buy_evaluation',{}).get('decision_completed',0)>0)
             else '분석 상태')
    lines.extend(["", heading])
    if not confirmed:
        evaluation=report.get('buy_evaluation',{})
        if evaluation.get('decision_completed',0)>0:
            lines.append(f"판정 완료 {evaluation['decision_completed']}종목 · 현재 매수 조건 충족 없음")
            if evaluation.get('data_excluded'):
                lines.append(f"자료 부족 {evaluation['data_excluded']}종목은 매수 판정에서 제외")
            blocked = (evaluation.get('counts') or {}).get('market_headwind')
            if blocked and not (report.get('market_regime') or {}):
                # Stored preopen reports do not carry the regime block; the count still
                # says why nothing qualified instead of leaving a silent zero.
                lines.append(f"시장 역풍으로 보류 {blocked}종목 (지수 < 20일선)")
        elif evaluation.get('completed') is True:
            lines.append("매수 조건 충족 종목 없음")
        else:
            lines.append("매수 판정 보류 · 분석 미완료")
    for item in confirmed[:5]:
        lines.append(telegram_candidate_line(item))

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
    return True


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

    for market, symbol in BENCHMARKS.items():
        try:
            benchmarks[market] = download_prices(
                symbol
            )
        except Exception as exc:
            benchmarks[market] = None
            benchmark_errors.append({
                "market": market,
                "symbol": symbol,
                "error": str(exc)
            })

    all_results = []
    all_errors = []

    for market, tickers in watchlist.items():
        if full_scan:
            results, errors = scan_batches(market, names[market], benchmarks[market])
        else:
            results, errors = scan_market(market, tickers, benchmarks[market], names=names[market])

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
        observed = next((item["indicators"].get("market_above_ma20") for item in rows
                         if item["indicators"].get("market_above_ma20") is not None), None)
        ma20 = next((item["indicators"].get("market_ma20") for item in rows
                     if item["indicators"].get("market_ma20") is not None), None)
        headwind_skipped = sum(1 for item in rows if item.get("selection_filter") == "market_headwind")
        market_regime[market] = {"index_symbol": BENCHMARKS[market], "above_ma20": observed,
                                 "index_ma20": ma20, "headwind_skipped": headwind_skipped,
                                 "observed": observed is not None}

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
            "실시간 VWAP 미구현",
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
        prepare_selected_analysis(report)

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
