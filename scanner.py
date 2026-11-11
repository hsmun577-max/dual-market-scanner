import json
import math
import os
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
import yfinance as yf

WATCHLIST = {
    "KR": [
        "005930.KS", "000660.KS", "035420.KS",
        "035720.KS", "005380.KS", "068270.KS",
        "000270.KS", "012450.KS", "042700.KS",
        "069500.KS", "229200.KS", "114800.KS",
        "252670.KS", "122630.KS"
    ],
    "US": [
        "AAPL", "MSFT", "NVDA", "AMD", "AVGO",
        "AMZN", "META", "GOOGL", "TSLA",
        "PLTR", "MU", "MRVL", "SMCI",
        "QQQ", "SPY", "IWM", "SOXX",
        "SOXL", "SOXS", "TQQQ", "SQQQ",
        "SPXL", "SPXS", "TLT"
    ]
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

            required = ["Open", "High", "Low", "Close", "Volume"]

            for column in required:
                if column not in data.columns:
                    raise ValueError(f"{column} 누락")

            data = data[required].copy()
            data = data.replace([np.inf, -np.inf], np.nan).dropna()
            prices = ["Open", "High", "Low", "Close"]
            data = data.loc[data[prices].gt(0).all(axis=1) & data["Volume"].ge(0)]
            data = data.loc[~data.index.duplicated(keep="last")].sort_index()

            if len(data) < 65:
                raise ValueError(
                    f"데이터 부족: {len(data)}거래일"
                )

            return data

        except Exception as exc:
            last_error = str(exc)

            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError(last_error)

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

def scan_market(market, tickers, benchmark):
    results = []
    errors = []

    for symbol in dict.fromkeys(tickers):
        try:
            data = download_prices(symbol)

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
            print(
                f"{market} {symbol}: "
                f"{signal['stage']}"
            )

        except Exception as exc:
            errors.append({
                "market": market,
                "symbol": symbol,
                "error": str(exc)
            })
            print(f"ERROR {market} {symbol}: {exc}")

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
    if not candidates:
        print("Telegram skipped: no watch candidates")
        return

    lines = [
        "듀얼스캐너 일봉 관찰 후보",
        f"조회 성공 {report['successful']}/{report['attempted']} · 후보 {len(candidates)}개",
    ]
    for item in candidates:
        signal = item["signal"]
        data_date = item["indicators"]["data_date"]
        reasons = ", ".join(signal["reasons"][:2])
        lines.append(
            f"{item['market']} {item['symbol']} · {data_date} · "
            f"{signal['stage']} ({signal['score']}점) · {reasons}"
        )

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
        choices=["sample"],
        default="sample"
    )
    args = parser.parse_args()

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

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

    for market, tickers in WATCHLIST.items():
        results, errors = scan_market(
            market,
            tickers,
            benchmarks[market]
        )

        all_results.extend(results)
        all_errors.extend(errors)

    attempted = sum(
        len(set(tickers))
        for tickers in WATCHLIST.values()
    )

    report = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "scan_mode": args.mode,
        "universe_scope": "고정 관심종목 표본",
        "full_market_scan": False,
        "attempted": attempted,
        "successful": len(all_results),
        "failed": len(all_errors),
        "benchmark_errors": benchmark_errors,
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
            for market, tickers in WATCHLIST.items()
        },
        "results": all_results,
        "errors": all_errors,
        "limitations": [
            "전 종목 스캔 아님",
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
