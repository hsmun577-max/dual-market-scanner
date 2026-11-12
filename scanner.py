import json
import math
import os
import time
import argparse
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
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

def download_prices(symbol, period="1y", retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            data = yf.download(
                symbol,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
                timeout=20
            )

            if data.empty:
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
        try:
            prices = yf.download(
                batch, period="1y", interval="1d", auto_adjust=True,
                progress=False, threads=8, timeout=15, group_by="ticker",
                multi_level_index=True,
            )
        except Exception as exc:
            errors.extend(
                {"market": market, "symbol": symbol, "error": str(exc)}
                for symbol in batch
            )
            continue
        found, failed = scan_market(market, batch, benchmark, names=names, prices=prices)
        results.extend(found)
        errors.extend(failed)
        print(f"{market}: {len(results) + len(errors)}/{len(symbols)}")
    return results, errors


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

    if benchmark is not None:
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
        score += 2
        reasons.append("급락 후 반등 징후")

    if indicators["overheated"]:
        return {
            "stage": "과열 관찰",
            "score": score,
            "reasons": reasons,
            "confirmed": False,
            "direction": direction
        }

    if score >= 5:
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

def build_risk_plan(price, stop_pct=0.02,
                    target_pct=0.07,
                    risk_budget=30000):
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
        "quantity_risk_only": quantity,
        "risk_reward_ratio": safe_float(
            target_pct / stop_pct
        ),
        "warning": (
            "참고 계산이며 실제 주문 신호가 아님. "
            "가용 현금, 수수료, 슬리피지 미반영."
        )
    }

def scan_market(market, tickers, benchmark, names=None, prices=None):
    names = names or {}
    results = []
    errors = []

    for symbol in dict.fromkeys(tickers):
        try:
            data = download_prices(symbol) if prices is None else clean_prices(prices[symbol])

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
                "fundamentals": "미검증",
                "news_and_filings": "미검증",
                "institutional_flows": "미검증"
            }

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

def telegram_candidate_line(item):
    signal = item["signal"]
    indicators = item["indicators"]
    data_date = indicators["data_date"]
    reasons = " · ".join(signal["reasons"][:2])
    symbol = item["symbol"]
    name = item.get("name") or KR_SYMBOL_NAMES.get(symbol)
    code = symbol.split(".")[0] if item["market"] == "KR" else symbol
    label = f"{name} ({code})" if name else code
    price = indicators.get("price")
    if price is not None:
        price_label = f"{price:,.0f}원" if item["market"] == "KR" else f"${price:,.2f}"
        label = f"{label}  {price_label}"
    return (
        f"• {label}\n"
        f"  {signal['stage']} {signal['score']}점 · {reasons} · {data_date}"
    )


def sector_metrics(prices, benchmark):
    data = prices[["Close", "Adj Close", "Volume"]].replace([np.inf, -np.inf], np.nan).dropna()
    data = data.sort_index()
    if len(data) < 21:
        raise ValueError("섹터 일봉 21개 미만")

    turnover = data["Close"] * data["Volume"]
    average_turnover = float(turnover.iloc[-21:-1].mean())
    if average_turnover <= 0:
        raise ValueError("유효한 거래대금 기준 없음")
    dates = data.index[-2:]
    if benchmark is None or len(benchmark) < 2 or not dates.equals(benchmark.index[-2:]):
        raise ValueError("섹터와 시장 기준 지표의 최근 거래일 불일치")

    change = float(data["Adj Close"].iloc[-1] / data["Adj Close"].iloc[-2] - 1)
    market_change = float(benchmark["Close"].iloc[-1] / benchmark["Close"].iloc[-2] - 1)
    ratio = float(turnover.iloc[-1] / average_turnover)
    relative = change - market_change
    return {
        "data_date": dates[-1].date().isoformat(),
        "return_pct": safe_float(change * 100),
        "market_excess_pct": safe_float(relative * 100),
        "estimated_turnover": safe_float(turnover.iloc[-1]),
        "turnover_ratio": safe_float(ratio),
        "active": change > 0 and relative > 0 and ratio >= SECTOR_TURNOVER_THRESHOLD,
    }


def scan_sectors(benchmarks):
    results, errors = [], []
    for market, sectors in SECTOR_ETFS.items():
        try:
            prices = yf.download(
                list(sectors), period="3mo", interval="1d", auto_adjust=False,
                progress=False, group_by="ticker", threads=6, timeout=15,
                multi_level_index=True,
            )
        except Exception as exc:
            errors.append({"market": market, "error": str(exc)})
            continue
        for symbol, name in sectors.items():
            try:
                metrics = sector_metrics(prices[symbol], benchmarks.get(market))
                results.append({"market": market, "symbol": symbol, "name": name, **metrics})
            except (KeyError, ValueError) as exc:
                errors.append({"market": market, "symbol": symbol, "error": str(exc)})
    print(f"Sector ETFs: {len(results)} analyzed, {len(errors)} errors")
    return results, errors


def send_telegram_summary(report):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token and not chat_id:
        print("Telegram skipped: configuration not set")
        return
    if not token or not chat_id:
        raise RuntimeError("Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    candidates = [
        item for item in report["results"]
        if item["signal"]["stage"] in {"장전 관찰 후보", "장중 조건부 진입 후보"}
    ]
    sectors = [item for item in report.get("sectors", []) if item["active"]]
    if not candidates and not sectors:
        print("Telegram skipped: no watch candidates")
        return

    lines = [
        "듀얼스캐너 일봉 관찰 후보",
        f"조회 성공 {report['successful']}/{report['attempted']} · 후보 {len(candidates)}개",
    ]
    for market, market_name in (("KR", "국장"), ("US", "미장")):
        ranked = sorted(
            (item for item in candidates if item["market"] == market),
            key=lambda item: (-item["signal"]["score"], item["symbol"]),
        )
        if ranked:
            lines.append(f"\n[{market_name} 종목 후보]")
            lines.extend(telegram_candidate_line(item) for item in ranked[:5])
    if sectors:
        for market, market_name in (("KR", "국장"), ("US", "미장")):
            ranked = sorted(
                (item for item in sectors if item["market"] == market),
                key=lambda item: (-item["turnover_ratio"], item["symbol"]),
            )
            if ranked:
                lines.append(f"\n[{market_name} 섹터 거래 집중 · 대표 ETF]")
            for item in ranked[:3]:
                lines.append(
                    f"• {item['name']} ({item['symbol']})  {item['return_pct']:+.2f}%\n"
                    f"  거래대금 약 {item['turnover_ratio']:.2f}배 · "
                    f"시장 대비 {item['market_excess_pct']:+.2f}%p · {item['data_date']}"
                )
    lines.append("\n전체 결과는 GitHub 실행 보고서")

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines)},
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

    print("Telegram sent: daily watch candidates")


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
        "benchmark_errors": benchmark_errors,
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
            "당일 미완성 봉 포함 가능",
            "실시간 VWAP 미구현",
            "뉴스·공시·거시경제 연동 미구현",
            "기업 펀더멘털 분석 미구현",
            "백테스트 및 표본 외 검증 미구현",
            "실제 투자 승률 검증되지 않음"
        ]
    }

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

    print(
        f"SCAN COMPLETE: "
        f"{len(all_results)} success, "
        f"{len(all_errors)} failed"
    )

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
