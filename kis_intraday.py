"""Check three completed five-minute bars and notify Telegram."""

import argparse
import asyncio
import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from websockets.exceptions import WebSocketException

from kis_feed_probe import FEEDS, FEED_URL, KST, connect, parse_records, request_approval, subscription
from scanner import KR_SYMBOL_NAMES, US_SYMBOL_NAMES, calculate_indicators, classify_signal, clean_prices


# Common shares from the current watchlist. NXT subscription acceptance is checked per session.
SYMBOLS = tuple(symbol.split(".")[0] for symbol in list(KR_SYMBOL_NAMES)[:9])
US_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA")
OPEN_TIMES = {"NXT": time(8), "KRX": time(9), "US-early": time(4), "US-late": time(8, 20)}
BAR_SECONDS = 300
MAX_QUOTE_AGE = 10
MAX_SPREAD_PCT = 0.5
MIN_VOLUME_RATIO = 1.5


def daily_candidates(previous_session, overseas=False):
    tickers = list(US_SYMBOLS) if overseas else [f"{symbol}.KS" for symbol in SYMBOLS]
    prices = yf.download(
        tickers, period="1y", interval="1d", auto_adjust=True,
        group_by="ticker", progress=False, threads=6, timeout=15,
    )
    candidates = []
    for ticker in tickers:
        try:
            history = prices[ticker]
            history = clean_prices(history.loc[history.index.date <= previous_session])
            if history.index[-1].date() != previous_session:
                continue
            signal = classify_signal(calculate_indicators(history))
            if signal["stage"] in {"장전 관찰 후보", "장중 조건부 진입 후보"}:
                candidates.append(f"DNAS{ticker}" if overseas else ticker.split(".")[0])
        except (KeyError, ValueError):
            print(f"일봉 자료 제외: {ticker}")
    return candidates


def update_bar(bars, slot, event_time, price, size):
    bar = bars.get(slot)
    if bar is None:
        bars[slot] = {"high": price, "close": price, "last": event_time, "volume": size}
        return
    bar["high"] = max(bar["high"], price)
    bar["volume"] += size
    if event_time >= bar["last"]:
        bar["close"], bar["last"] = price, event_time


def candidate_metrics(bars, quote, close_time):
    if set(bars) != {0, 1, 2} or quote is None:
        return None
    age = (close_time - datetime.fromisoformat(quote["event_time"])).total_seconds()
    if not 0 <= age <= MAX_QUOTE_AGE or quote["spread_pct"] > MAX_SPREAD_PCT:
        return None
    volume_ratio = bars[2]["volume"] / ((bars[0]["volume"] + bars[1]["volume"]) / 2)
    if volume_ratio < MIN_VOLUME_RATIO or bars[2]["close"] <= max(bars[0]["high"], bars[1]["high"]):
        return None
    return volume_ratio, quote["spread_pct"], bars[2]["close"]


async def collect(approval, market, symbols, opens, on_signal, samples_path):
    closes = opens + timedelta(minutes=15)
    feeds = FEEDS[market]
    pending = {(tr_id, symbol) for tr_id in feeds for symbol in symbols}
    rejected = set()
    bars = {symbol: {} for symbol in symbols}
    quotes = {}
    followup = {symbol: [] for symbol in symbols}
    evaluated = False
    deadline = closes
    async with connect(FEED_URL, open_timeout=15, close_timeout=3) as socket:
        for tr_id, symbol in sorted(pending):
            await socket.send(json.dumps(subscription(approval, tr_id, symbol)))
        while True:
            received = datetime.now(KST)
            seconds_left = (deadline - received).total_seconds()
            if seconds_left <= 0:
                if evaluated:
                    break
                rejected.update(symbol for _, symbol in pending)
                print(f"{market} 구독 검증 {len(symbols) - len(rejected)}/{len(symbols)}")
                if len(rejected) == len(symbols):
                    raise RuntimeError("개장 전 구독 승인에 성공한 종목이 없습니다")
                candidates = []
                for symbol in symbols:
                    if symbol not in rejected:
                        metrics = candidate_metrics(bars[symbol], quotes.get(symbol), closes)
                        if metrics:
                            candidates.append((symbol, *metrics))
                notification = asyncio.create_task(asyncio.to_thread(
                    on_signal, sorted(candidates, key=lambda row: (-row[1], row[0]))
                ))
                evaluated = True
                deadline = closes + timedelta(minutes=30, seconds=30)
                continue
            try:
                raw = await asyncio.wait_for(socket.recv(decode=True), seconds_left)
            except TimeoutError:
                continue
            received = datetime.now(KST)
            if raw.startswith("{"):
                status = json.loads(raw)
                header = status.get("header", {})
                if header.get("tr_id") == "PINGPONG":
                    await socket.pong(raw.encode("utf-8"))
                    continue
                pair = (header.get("tr_id"), header.get("tr_key"))
                if pair in pending:
                    pending.remove(pair)
                    if status.get("body", {}).get("rt_cd") != "0" or received >= opens:
                        rejected.add(pair[1])
                continue
            for item in parse_records(raw, feeds, received):
                symbol = item["symbol"]
                if market == "US" and not symbol.startswith("DNAS"):
                    symbol = f"DNAS{symbol}"
                event_time = datetime.fromisoformat(item["event_time"])
                if symbol not in bars or symbol in rejected or not opens <= event_time:
                    continue
                if not -1 <= item["lag_seconds"] <= MAX_QUOTE_AGE:
                    continue
                if evaluated:
                    if item["kind"] == "quote" and event_time >= closes and received <= deadline:
                        followup[symbol].append({"at": received.isoformat(), "bid": item["bid"], "ask": item["ask"]})
                elif received < closes and event_time < closes and item["kind"] == "trade":
                    slot = int((event_time - opens).total_seconds()) // BAR_SECONDS
                    update_bar(bars[symbol], slot, event_time, item["price"], item["size"])
                elif received < closes and event_time < closes and item["kind"] == "quote" and (
                    symbol not in quotes or item["event_time"] >= quotes[symbol]["event_time"]
                ):
                    quotes[symbol] = item

    with samples_path.open("w", encoding="utf-8") as output:
        for symbol in symbols:
            if symbol in rejected or set(bars[symbol]) != {0, 1, 2}:
                continue
            quote = quotes.get(symbol)
            before = [{"at": quote["event_time"], "bid": quote["bid"], "ask": quote["ask"]}] if quote else []
            row = {
                "market": samples_path.stem,
                "symbol": symbol,
                "signal_at": closes.isoformat(),
                "daily_eligible": True,
                "bars": [
                    {"start": (opens + timedelta(minutes=5 * slot)).isoformat(),
                     **{key: bar[key] for key in ("high", "close", "volume")}}
                    for slot, bar in sorted(bars[symbol].items())
                ],
                "quotes": before + followup[symbol],
            }
            output.write(json.dumps(row, allow_nan=False) + "\n")
    await notification


def notify(market, candidates, closes, token, chat_id):
    overseas = market.startswith("US-")
    if overseas:
        title = "미장 프리장 갱신" if market == "US-late" else "미장 프리장 관찰 후보"
        lines = [title, f"{closes:%m/%d %H:%M} ET · {closes.astimezone(KST):%H:%M} KST"]
    else:
        session = "NXT 프리장" if market == "NXT" else "KRX 정규장"
        lines = [f"국장 {session} 관찰 후보", f"{closes:%m/%d %H:%M} KST"]
    lines.append("")
    for symbol, ratio, spread, price in candidates[:5]:
        ticker = symbol[4:] if overseas else symbol
        name = US_SYMBOL_NAMES[ticker] if overseas else KR_SYMBOL_NAMES[f"{symbol}.KS"]
        price_label = f"${price:,.2f}" if overseas else f"{price:,}원"
        lines.append(
            f"• {name} ({ticker})  {price_label}\n"
            f"  5분봉 고점 돌파 · 거래량 {ratio:.2f}배 · 호가 간격 {spread:.2f}%"
        )
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines)},
            timeout=10, allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Telegram 발송 실패 (HTTP {response.status_code})")
        if response.json().get("ok") is not True:
            raise RuntimeError("Telegram이 발송을 확인하지 않았습니다")
    except (requests.RequestException, ValueError, AttributeError):
        raise RuntimeError("Telegram 발송 응답 오류") from None
    print(f"Telegram sent: {market} intraday candidates")


def filter_us_updates(candidates, today, path):
    if not path.is_file():
        print("04:15 비교 결과 없음: 이번 후보를 첫 관찰로 처리합니다")
        return candidates
    early = json.loads(path.read_text(encoding="utf-8"))
    if early["date"] != today:
        print("04:15 비교 결과 거래일 불일치: 이번 후보를 첫 관찰로 처리합니다")
        return candidates
    earlier = {row[0]: row for row in early["candidates"]}
    return [
        row for row in candidates
        if row[0] not in earlier or (row[3] > earlier[row[0]][3] and row[1] >= earlier[row[0]][1])
    ]


def main():
    import exchange_calendars

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market", choices=OPEN_TIMES)
    args = parser.parse_args()
    market = args.market
    overseas = market.startswith("US-")
    local_zone = ZoneInfo("America/New_York") if overseas else KST
    now = datetime.now(local_zone)
    calendar = exchange_calendars.get_calendar("XNYS" if overseas else "XKRX")
    today = now.date().isoformat()
    if not calendar.is_session(today):
        print(f"{market} 휴장일")
        return
    regular_open = calendar.session_open(today).tz_convert(local_zone).time()
    if regular_open != (time(9, 30) if overseas else OPEN_TIMES["KRX"]):
        print("개장 시각 변경 거래일은 장중 알림을 건너뜁니다")
        return
    opens = datetime.combine(now.date(), OPEN_TIMES[market], local_zone)
    if not opens - timedelta(minutes=25) <= now < opens:
        raise SystemExit("개장 전 25분 이내에 시작해야 첫 5분봉부터 수집할 수 있습니다")

    keys = [os.getenv(name, "").strip() for name in (
        "KIS_APP_KEY", "KIS_APP_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    )]
    if not all(keys):
        raise SystemExit("KIS 및 Telegram 비밀값 4개를 설정하세요")
    app_key, app_secret, token, chat_id = keys
    snapshot = Path("us-early-candidates.json")
    try:
        symbols = daily_candidates(calendar.previous_session(today).date(), overseas)
        def publish(candidates):
            if market == "US-early":
                snapshot.write_text(json.dumps({"date": today, "candidates": candidates[:5]}), encoding="utf-8")
            elif market == "US-late":
                candidates = filter_us_updates(candidates, today, snapshot)
            if candidates:
                notify(market, candidates, opens + timedelta(minutes=15), token, chat_id)
            else:
                print(f"{market} 장중 알림 조건 충족 없음")

        if symbols:
            approval = request_approval(app_key, app_secret)
            if datetime.now(local_zone) >= opens:
                raise RuntimeError("준비가 수집 시작 전에 끝나지 않아 불완전한 봉을 제외했습니다")
            asyncio.run(collect(approval, "US" if overseas else market, symbols, opens, publish, Path(f"{market}.jsonl")))
        else:
            print(f"{market} 직전 거래일 관찰 후보 없음")
            publish([])
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, ValueError, TimeoutError, WebSocketException) as exc:
        raise SystemExit(f"{market} 장중 실행 실패 ({type(exc).__name__})") from None


if __name__ == "__main__":
    main()
