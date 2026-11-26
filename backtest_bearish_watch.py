"""Compare bearish stock screens on timestamped Yahoo daily OHLCV files.

Pass a universe.json and prices/<symbol>.json exported from Yahoo's chart endpoint.
Use --scan-archive and --download to rebuild these inputs.
"""

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import exchange_calendars as xc
import requests

from candidate_selection import (
    candidate_rejection_reason,
    index_symbol_for,
    instrument_rejection_reason,
)
from research_asset import is_fund
from bearish_watch import quiet_demand, rank_key, resilience_setup
from exit_policy import STOCK_STOP_PCT, DEFAULT_TAKE_PROFIT_PCT
from theme_sectors import theme_membership

START, SPLIT, END = "2026-05-27", "2026-08-27", "2026-11-26"
WARMUP = "2026-02-01"
HOLD = 5
GAP = 0.01
FEE = 0.005
PRICE_KEYS = ("open", "high", "low", "close", "volume")
PEERS = {market: theme_membership(market) for market in ("KR", "US")}
VARIANTS = ("trend", "resilience", "volume", "quiet", "theme")


def sample_universe(archive):
    with zipfile.ZipFile(archive) as scan:
        report = json.loads(scan.read("latest.json"))
    universe = {}
    for market, pattern in (("KR", r"[0-9A-Z]{6}\.(KS|KQ)"), ("US", r"[A-Z][A-Z0-9.-]{0,6}")):
        names = {
            row["symbol"]: row.get("name") or row["symbol"]
            for row in report["results"]
            if row["market"] == market
            and re.fullmatch(pattern, row["symbol"])
            and not is_fund(row)
            and not instrument_rejection_reason(row["symbol"], row.get("name"))
        }
        stable_order = sorted(
            names, key=lambda symbol: hashlib.sha256(f"{START}/{symbol}".encode()).digest()
        )
        symbols = set(stable_order[:440]) | (PEERS[market].keys() & names.keys())
        universe[market] = {symbol: names[symbol] for symbol in sorted(symbols)}
    return universe


def download_inputs(root, universe):
    folder = root / "prices"
    folder.mkdir(parents=True, exist_ok=True)

    def unix_day(day):
        return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())

    params = dict(
        period1=unix_day(date.fromisoformat(WARMUP)),
        period2=unix_day(date.fromisoformat(END) + timedelta(days=1)),
        interval="1d",
        includePrePost="false",
    )

    def fetch(symbol):
        target = folder / f"{symbol}.json"
        if target.is_file():
            return
        for host in ("query2", "query1"):
            try:
                response = requests.get(
                    f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=12,
                )
                response.raise_for_status()
                row = response.json()["chart"]["result"][0]
                if row["meta"]["symbol"] == symbol and row.get("timestamp"):
                    target.write_text(json.dumps(row), encoding="utf-8")
                    return
            except (requests.RequestException, ValueError, TypeError, IndexError, KeyError):
                continue
        print(f"Price history unavailable: {symbol}", flush=True)

    symbols = sorted({"^KS11", "^KQ11", "SPY"}.union(*universe.values()))
    with ThreadPoolExecutor(max_workers=10) as workers:
        list(workers.map(fetch, symbols))


def market_data(root, symbol, *, equity_only=False):
    file = root / "prices" / f"{symbol}.json"
    if not file.exists():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    metadata = data["meta"]
    if metadata["symbol"] != symbol or (equity_only and metadata.get("instrumentType") != "EQUITY"):
        return None
    zone = ZoneInfo(data["meta"]["exchangeTimezoneName"])
    dates = [datetime.fromtimestamp(t, zone).date().isoformat() for t in data["timestamp"]]
    quote = data["indicators"]["quote"][0]
    frame = pd.DataFrame({key: quote[key] for key in PRICE_KEYS}, index=dates, dtype=float)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index().loc[:END]
    prices = frame[list(PRICE_KEYS[:4])]
    valid = (
        prices.gt(0).all(axis=1)
        & prices["high"].ge(prices[["open", "low", "close"]].max(axis=1))
        & prices["low"].le(prices[["open", "high", "close"]].min(axis=1))
    )
    return frame.loc[valid]


def features(frame, benchmark, dates):
    frame = frame.reindex(dates)
    b = benchmark["close"].reindex(dates)
    h, l, c, v = (frame[key] for key in ("high", "low", "close", "volume"))
    change = c.pct_change(fill_method=None)
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    r5 = c.pct_change(5, fill_method=None)
    r20 = c.pct_change(20, fill_method=None)
    b5 = b.pct_change(5, fill_method=None)
    b20 = b.pct_change(20, fill_method=None)
    depth = c / h.rolling(20).max() - 1
    obv = (np.sign(change) * v).rolling(5).sum() > 0
    values = pd.DataFrame(
        dict(
            price=c,
            volume=v,
            ma20=ma20,
            ma60=ma60,
            return_1d=change,
            return_5d=r5,
            return_20d=r20,
            market_return_5d=b5,
            relative_strength=r20 - b20,
            market_above_ma20=b.gt(b.rolling(20).mean()),
            average_turnover_20d=(c * v).shift().rolling(20).mean(),
            volume_ratio=v / v.shift().rolling(20).mean(),
            compression=change.rolling(5).std() < change.rolling(20).std() * 0.75,
            obv_rising=obv,
            breakdown=c < l.shift().rolling(20).min(),
            rebound=(depth < -0.08) & (change > 0) & obv,
            overheated=(r5 > 0.15) | (c > ma20 * 1.15),
        )
    ).replace([np.inf, -np.inf], np.nan)
    price_history = c.notna() & ma60.notna() & b.rolling(21).mean().notna()
    suspect_move = (change.abs() > 0.60).rolling(21).max().fillna(1).astype(bool)
    values["eligible_data"] = price_history & ~suspect_move
    values["data_date"] = values.index
    values["market_ma20_date"] = values.index
    values["market_lag_sessions"] = 0
    numeric = values.select_dtypes(include=["floating"]).columns
    values[numeric] = values[numeric].round(4)
    return values.loc[START:END]


def peer_confirmation(symbol, market, day, changes, members):
    for theme in PEERS[market].get(symbol, ()):
        peers = [changes[day][s] for s in members[theme] if s != symbol and s in changes[day]]
        if (
            len(peers) >= 3
            and sum(x > 0 for x in peers) / len(peers) >= 0.60
            and sum(peers) / len(peers) > 0
        ):
            return True
    return False


def sim(bars, sessions, signal_price, fee):
    fill_day = sessions[1]
    fill = bars.reindex(sessions[1:])
    entry = fill.loc[fill_day, "open"]
    if not np.isfinite(entry):
        return "missing", None
    if not signal_price * (1 - GAP) <= entry <= signal_price * (1 + GAP):
        return "gap_cancel", None
    stop = entry * (1 - STOCK_STOP_PCT)
    target = entry * (1 + DEFAULT_TAKE_PROFIT_PCT)
    marks = {}
    for day, row in fill.iterrows():
        if row[["open", "high", "low", "close"]].isna().any():
            return "missing", None
        if row["open"] <= stop:
            price, status = row["open"], "stop_gap"
        elif row["low"] <= stop:
            price, status = stop, "stop"
        elif row["high"] >= target:
            price, status = target, "target"
        else:
            price, status = row["close"], "time" if day == sessions[-1] else None
        marks[day] = price / entry * (1 - fee / 2) / (1 + fee / 2) - 1
        if status:
            return status, marks


def summarize(trades, days, counts):
    equity = 1.0
    cash = equity
    active = {}
    max_equity = equity
    worst_drawdown = 0
    capital_cost = {}
    booking = defaultdict(list)
    for pick, path in trades:
        booking[next(iter(path))].append((pick, path))
    for day in days:
        for pick, path in booking[day]:
            # One new trade per session, five sleeves with equal cash allocation.
            allocation = min(equity / HOLD, cash)
            cash -= allocation
            capital_cost[pick] = allocation
            active[pick] = path
        daily_gain = 0
        for pick, path in tuple(active.items()):
            marks = list(path)
            index = marks.index(day)
            previous = path[marks[index - 1]] if index else 0
            daily_gain += capital_cost[pick] * (path[day] - previous)
            if day == marks[-1]:
                del active[pick]
                cash += capital_cost.pop(pick) * (1 + path[day])
        equity += daily_gain
        max_equity = max(max_equity, equity)
        worst_drawdown = min(worst_drawdown, equity / max_equity - 1)
    returns = np.array([list(path.values())[-1] for _, path in trades])
    if not len(returns):
        return dict(signals=sum(counts.values()), trades=0, skips=counts)
    losses = returns[returns < 0]
    tail = np.sort(returns)[: int(np.ceil(len(returns) * 0.10))]
    return dict(
        signals=sum(counts.values()),
        trades=len(returns),
        mean_pct=np.mean(returns) * 100,
        win_pct=np.mean(returns > 0) * 100,
        loss_pct=np.mean(returns < 0) * 100,
        mean_loss_pct=np.mean(losses) * 100 if len(losses) else 0,
        tail10_pct=np.mean(tail) * 100,
        worst_pct=np.min(returns) * 100,
        portfolio_pct=(equity - 1) * 100,
        max_drawdown_pct=worst_drawdown * 100,
        stops=counts["stop"] + counts["stop_gap"],
        skips=dict(counts),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--scan-archive", type=Path, help="Build the universe from the archived scan ZIP"
    )
    parser.add_argument("--download", action="store_true", help="Fetch missing Yahoo daily price files")
    args = parser.parse_args()
    root = args.data_dir
    manifest = root / "universe.json"
    if args.scan_archive:
        uni = sample_universe(args.scan_archive)
        root.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(uni, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        uni = json.loads(manifest.read_text(encoding="utf-8"))
    if args.download:
        download_inputs(root, uni)
    out = {}
    for market, names in uni.items():
        cal = xc.get_calendar("XKRX" if market == "KR" else "XNYS")
        days = cal.sessions_in_range(WARMUP, END).strftime("%Y-%m-%d").tolist()
        study = [d for d in days if d >= START]
        indices = {
            name: market_data(root, name)
            for name in (("^KS11", "^KQ11") if market == "KR" else ("SPY",))
        }
        missing_indices = [symbol for symbol, frame in indices.items() if frame is None]
        if missing_indices:
            raise SystemExit(f"Missing benchmark files: {missing_indices}")
        bearish = {
            s: [
                d
                for d, current in df["close"].lt(df["close"].rolling(20).mean()).items()
                if d >= START and current
            ]
            for s, df in indices.items()
        }
        members = defaultdict(set)
        for symbol, themes in PEERS[market].items():
            for theme in themes:
                members[theme].add(symbol)
        raw = {s: data for s in names if (data := market_data(root, s, equity_only=True)) is not None}
        picks = {k: defaultdict(list) for k in VARIANTS}
        changes = defaultdict(dict)
        for s in PEERS[market].keys() & raw.keys():
            series = raw[s]["close"].reindex(days).pct_change(fill_method=None)
            for day, ret in series.loc[START:END].dropna().items():
                if abs(ret) <= 0.60:
                    changes[day][s] = ret
        observed = sum(1 for df in raw.values() if df.index[0] < START and df.index[-1] >= study[-2])
        for s, bars in raw.items():
            frame = features(bars, indices[index_symbol_for(s, market)], days)
            for day, val in frame.loc[frame["eligible_data"] & ~frame["market_above_ma20"]].iterrows():
                values = val.to_dict()
                row = dict(symbol=s, name=names[s], indicators=values)
                rank = rank_key(row, round(values["return_5d"] - values["market_return_5d"], 4))
                detail = (rank, s, bars.loc[day, "close"])
                if candidate_rejection_reason(row, market) == "market_headwind":
                    picks["trend"][day].append(detail)
                if resilience_setup(row, market) is None:
                    continue
                picks["resilience"][day].append(detail)
                if values["volume_ratio"] >= 1.2:
                    picks["volume"][day].append(detail)
                    if peer_confirmation(s, market, day, changes, members):
                        picks["theme"][day].append(detail)
                if quiet_demand(values):
                    picks["quiet"][day].append(detail)
        out[market] = dict(
            universe=len(names),
            downloaded=len(raw),
            coverage=observed,
            bearish_days={
                s: {"train": sum(d < SPLIT for d in ds), "holdout": sum(d >= SPLIT for d in ds)}
                for s, ds in bearish.items()
            },
            first=study[0],
            last=study[-1],
            results={},
        )
        for label, sessions in [
            ("train", [d for d in study if d < SPLIT]),
            ("holdout", [d for d in study if d >= SPLIT]),
        ]:
            for fee_label, fee in [("standard", FEE), ("stress", 0.008)]:
                results = {}
                for key, by_day in picks.items():
                    counts = Counter()
                    trades = []
                    held_through = {}
                    for i, day in enumerate(sessions[:-HOLD]):
                        if not by_day[day]:
                            continue
                        # Choose the strongest candidate once per market session.
                        _, symbol, price = min(by_day[day], key=lambda item: item[0])
                        if held_through.get(symbol, "") >= sessions[i + 1]:
                            counts["already_held"] += 1
                            continue
                        status, marks = sim(raw[symbol], sessions[i : i + HOLD + 1], price, fee)
                        counts[status] += 1
                        if marks:
                            held_through[symbol] = next(reversed(marks))
                            trades.append(((day, symbol), marks))
                    results[key] = summarize(trades, sessions, counts)
                out[market]["results"][f"{label}_{fee_label}"] = results
    if args.output:
        args.output.write_text(json.dumps(out, indent=2), encoding="utf-8")
    for market, data in out.items():
        print(market, {k: v for k, v in data.items() if k != "results"})
        for period, variants in data["results"].items():
            for variant, metrics in variants.items():
                print(
                    period,
                    variant,
                    " ".join(f"{k}={v:.2f}" for k, v in metrics.items() if isinstance(v, (int, float))),
                )


if __name__ == "__main__":
    main()
