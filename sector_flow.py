"""Compare sector ETF price and turnover with time-matched observations."""

from datetime import timedelta
import math
import pandas as pd


def intraday_sector_metrics(prices, benchmark, now):
    data = prices[["Open", "Close", "Volume"]].dropna().sort_index()
    if data.empty or data.index.tz is None:
        raise ValueError("Sector minute data unavailable")
    data.index = data.index.tz_convert(now.tzinfo)
    data = data.loc[data.index + pd.Timedelta(5, unit="min") <= now]
    if data.empty:
        raise ValueError("No completed sector bars")
    end = data.index[-1] + pd.Timedelta(5, unit="min")
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
    from scanner import yf_download
    prices = yf_download([*sectors, benchmark], period="10d", interval="5m", prepost=market=="US",
                         auto_adjust=False, group_by="ticker", progress=False, threads=4,
                         timeout=8, multi_level_index=True)
    results = []
    if prices is None:
        return results
    for symbol, name in sectors.items():
        try:
            metrics = intraday_sector_metrics(prices[symbol], prices[benchmark], now)
            results.append({"market": market, "symbol": symbol, "name": name, **metrics})
        except (KeyError, ValueError, TypeError):
            continue
    return results


def format_sector_header(rows, market=None):
    available=[row for row in rows or [] if market is None or row.get('market')==market]
    # 증권사식 순위(2026-11-26 지시): 등락률이 아니라 거래대금 유입 배수가 먼저다.
    # 대분류 ETF 폴백은 세부 테마가 없을 때만 나오지만, 같은 기준을 쓴다.
    ranked=sorted(available,key=lambda row:(
        -float(row.get('turnover_pace_ratio') or row.get('turnover_ratio') or 0),
        -float(row.get('market_excess_pct') or 0),
        -float(row.get('return_pct') or 0)))
    selected=[]; names=set()
    for row in ranked:
        name=row.get('name')
        if not name or name in names: continue
        selected.append(name); names.add(name)
        if len(selected)==2: break
    # No sector reading at all: say nothing instead of a heading plus '확인 불가'. The
    # message already carries the theme block or the market regime; an empty heading was
    # two lines of noise (2026-11-25 short form).
    if not selected:
        return []
    return ['오늘의 주요 섹터']+[f'{i}위 {name}' for i,name in enumerate(selected,1)]
