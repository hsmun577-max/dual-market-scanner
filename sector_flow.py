"""Compare sector ETF price and turnover with time-matched observations."""

from datetime import datetime, timedelta
import math
import pandas as pd
import yfinance as yf


def intraday_sector_metrics(prices, benchmark, now):
    data = prices[["Open", "Close", "Volume"]].dropna().sort_index()
    if data.empty or data.index.tz is None:
        raise ValueError("Sector minute data unavailable")
    data.index = data.index.tz_convert(now.tzinfo)
    data = data.loc[data.index + pd.Timedelta(minutes=5) <= now]
    if data.empty:
        raise ValueError("No completed sector bars")
    end = data.index[-1] + pd.Timedelta(minutes=5)
    if end.date() != now.date() or now - end > timedelta(minutes=20):
        raise ValueError("Current sector bars unavailable")

    def window(frame, finish):
        starts = pd.date_range(finish - timedelta(minutes=15), periods=3, freq="5min")
        return frame.reindex(starts)

    current = window(data, end)
    if current.isna().any().any() or current["Volume"].sum() <= 0 or current["Open"].iloc[0] <= 0:
        raise ValueError("Incomplete current sector window")
    prior_days = sorted({stamp.date() for stamp in data.index if stamp.date() < end.date()})[-5:]

    def ratio(finish):
        sample = window(data, finish)
        if sample.isna().any().any() or sample["Volume"].sum() <= 0:
            return None
        history = []
        for day in prior_days:
            baseline = window(data, finish.replace(year=day.year, month=day.month, day=day.day))
            if not baseline.isna().any().any() and baseline["Volume"].sum() > 0:
                history.append(float((baseline["Close"] * baseline["Volume"]).sum()))
        if len(history) < 3:
            return None
        return float((sample["Close"] * sample["Volume"]).sum()) / (sum(history) / len(history))

    turnover_ratio = ratio(end)
    if turnover_ratio is None or not math.isfinite(turnover_ratio):
        raise ValueError("Time-matched sector history unavailable")
    benchmark = benchmark.copy()
    if benchmark.index.tz is None:
        raise ValueError("Benchmark minute timezone unavailable")
    benchmark.index = benchmark.index.tz_convert(now.tzinfo)
    market = window(benchmark[["Open", "Close"]], end)
    if market.isna().any().any() or market["Open"].iloc[0] <= 0:
        raise ValueError("Incomplete market comparison window")
    change = float(current["Close"].iloc[-1] / current["Open"].iloc[0] - 1)
    excess = change - float(market["Close"].iloc[-1] / market["Open"].iloc[0] - 1)
    previous_ratio = ratio(end - timedelta(minutes=15))
    early = change > 0 and excess > 0 and turnover_ratio >= 1.3
    return {"data_date": end.date().isoformat(), "data_as_of": end.isoformat(),
            "return_pct": round(change*100,4), "market_excess_pct": round(excess*100,4),
            "turnover_ratio": round(turnover_ratio,4), "previous_turnover_ratio": previous_ratio,
            "early": early, "active": early and turnover_ratio >= 2,
            "starting": early and previous_ratio is not None and previous_ratio < 1.3,
            "source": "15m_vs_prior_same_time", "lookback_sessions": len(prior_days)}


def fetch_intraday_sectors(sectors, benchmark, market, now):
    prices = yf.download([*sectors, benchmark], period="10d", interval="5m", prepost=market=="US",
                         auto_adjust=False, group_by="ticker", progress=False, threads=4,
                         timeout=8, multi_level_index=True)
    results = []
    for symbol, name in sectors.items():
        try:
            metrics = intraday_sector_metrics(prices[symbol], prices[benchmark], now)
            results.append({"market": market, "symbol": symbol, "name": name, **metrics})
        except (KeyError, ValueError, TypeError):
            continue
    return results


def format_sector_header(rows, market=None):
    markets = (market,) if market else ("KR", "US")
    lines = ["섹터 매수세 · 대표 ETF 가격·거래대금 추정"]
    for code in markets:
        available = [row for row in rows or [] if row.get("market") == code]
        selected = sorted((row for row in available if row.get("early") or row.get("active")),
                          key=lambda row: (-int(bool(row.get("starting"))), -row["turnover_ratio"]))[:3]
        label = "국장" if code == "KR" else "미장"
        if not selected:
            latest = max((row["data_date"] for row in available), default="")
            lines.append(f"{label} · 확인된 유입 조짐 없음 · 자료 기준 {latest}" if available else f"{label} · 섹터 자료 미확보")
        for row in selected:
            state = "유입 시작 추정" if row.get("starting") else "집중 추정" if row.get("active") else "유입 조짐"
            intraday = row.get("source") == "15m_vs_prior_same_time"
            stamp = (datetime.fromisoformat(row["data_as_of"]).strftime("%m/%d %H:%M")
                     + (" KST" if code == "KR" else " ET")) if intraday else row["data_date"] + " 일봉"
            comparison = "최근 15분/과거 동시간대" if intraday else "해당 일자/20일 평균"
            lines.extend([f"• {label} {row['name']} · {state}",
                          f"  거래대금 {row['turnover_ratio']:.2f}배 ({comparison}) · 가격 {row['return_pct']:+.2f}% · 시장 대비 {row['market_excess_pct']:+.2f}%p · {stamp}"])
    return lines
