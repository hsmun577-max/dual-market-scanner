"""Check three completed five-minute bars and notify Telegram."""

import argparse
import asyncio
import json
import os
import re
from html import unescape
from time import sleep
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from websockets.exceptions import WebSocketException

from kis_feed_probe import FEEDS, FEED_URL, KST, connect, parse_records, request_approval, subscription
from signal_context import context_status
from scanner import KR_SYMBOL_NAMES, US_SYMBOL_NAMES, calculate_indicators, classify_signal, clean_prices


# Common shares from the current watchlist. NXT subscription acceptance is checked per session.
SYMBOLS = tuple(symbol.split(".")[0] for symbol in list(KR_SYMBOL_NAMES)[:9])
US_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA")
OPEN_TIMES = {"NXT": time(8), "KRX": time(9), "US-early": time(4), "US-late": time(8, 20)}
BAR_SECONDS = 300
MAX_QUOTE_AGE = 10
MAX_SPREAD_PCT = 0.5
MIN_VOLUME_RATIO = 1.5
MAX_BREAKOUT_EXTENSION = 0.005


def collection_open(scheduled, ready_at):
    """Keep the scheduled window or use the next complete five-minute boundary."""
    for minutes in (0, 5, 10, 15):
        start = scheduled + timedelta(minutes=minutes)
        if ready_at + timedelta(minutes=1) <= start:
            return start
    raise RuntimeError("개장 후 15분 이내에 완전한 5분봉 수집을 시작하지 못했습니다")


def wait_for_connection_window(opens):
    connection_at = opens - timedelta(minutes=3)
    delay = (connection_at - datetime.now(opens.tzinfo)).total_seconds()
    if delay > 0:
        print(f"KIS 연결 예정: {connection_at.isoformat()}", flush=True)
        sleep(delay)
    if datetime.now(opens.tzinfo) >= opens:
        raise RuntimeError("시세 수집 시작 시각을 지나 첫 봉을 확보할 수 없습니다")


def breakout_v2(bars, quote, close_time):
    """Volume confirmation and a capped entry above the broken resistance."""
    metrics = candidate_metrics(bars, quote, close_time)
    if metrics is None or metrics[0] < 2.0:
        return None
    resistance = max(bars[0]["high"], bars[1]["high"])
    limit = resistance * (1 + MAX_BREAKOUT_EXTENSION)
    if not resistance < quote["ask"] <= limit or metrics[2] > limit:
        return None
    return limit


def protective_stop(bars, entry_limit, bid, overseas):
    """Place the stop below the completed breakout candle's low."""
    support = Decimal(str(bars[2]["low"]))
    step = Decimal("0.01" if overseas else "1")
    stop = (support * Decimal("0.999")).quantize(step, rounding=ROUND_DOWN)
    if not Decimal(str(entry_limit)) * Decimal("0.98") <= stop < Decimal(str(bid)):
        return None
    return float(stop)


def market_evidence(market, previous_session, available_at):
    """Read the most recent completed index session for the live decision."""
    benchmark = "^GSPC" if market.startswith("US-") else "^KS11"
    try:
        prices = yf.Ticker(benchmark).history(period="3mo", auto_adjust=False, timeout=8)
        closes = prices.loc[prices.index.date <= previous_session, "Close"].dropna()
        if len(closes) < 20 or closes.index[-1].date() != previous_session:
            print(f"{market} market context unavailable: incomplete benchmark history")
            return []
        state = "supportive" if closes.iloc[-1] > closes.iloc[-20:].mean() else "neutral"
    except Exception as exc:
        print(f"{market} market context unavailable ({type(exc).__name__})")
        return []
    print(f"{market} market context: {benchmark} {previous_session} {state}")
    return [{"kind": "market", "market": market, "symbol": "*",
             "available_at": available_at.isoformat(), "assessment": state}]


def domestic_research(market, symbols, now):
    url = "https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=1800"
    verified = {("2026-11-12", "035420", "매수")}
    try:
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", response.text, re.I | re.S):
            cells = " ".join(unescape(re.sub(r"<[^>]+>", " ", row)).split())
            match = re.search(r"(\d{4}-\d{2}-\d{2}).*?\((\d{6})\s*/\s*([^()]+)\)", cells)
            if match:
                verified.add(match.groups())
        if len(verified) == 1:
            print(f"{market} domestic research listing returned no parseable rows")
    except requests.RequestException as exc:
        print(f"{market} domestic research listing unavailable ({type(exc).__name__})")
    evidence = []
    for day, symbol, grade in sorted(verified):
        stamp = datetime.fromisoformat(day).replace(tzinfo=KST) + timedelta(days=1)
        if symbol in symbols and now - timedelta(days=7) <= stamp < now:
            evidence.append({"kind": "research", "market": market, "symbol": symbol,
                             "available_at": stamp.isoformat(), "source": url,
                             "assessment": "supportive" if grade.strip() == "매수" else "neutral"})
    print(f"{market} recent domestic brokerage records: {len(evidence)}")
    return evidence


def research_evidence(market, symbols, now):
    if not market.startswith("US-"):
        return domestic_research(market, symbols, now)
    evidence = []
    for symbol in symbols:
        ticker = symbol[4:]
        try:
            grades = yf.Ticker(ticker).get_upgrades_downgrades().sort_index(ascending=False)
            for published, grade in grades.head(20).iterrows():
                stamp = published.to_pydatetime()
                # Yahoo's grade timestamps are UTC when the index has no timezone.
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=ZoneInfo("UTC"))
                if not now - timedelta(days=7) <= stamp < now:
                    continue
                opinion = str(grade.get("ToGrade", "")).strip().casefold()
                if not opinion or opinion == "nan":
                    continue
                strong = {"buy", "strong buy", "outperform", "overweight", "market outperform"}
                evidence.append({"kind": "research", "market": market, "symbol": symbol,
                                 "available_at": stamp.isoformat(),
                                 "assessment": "supportive" if opinion in strong else "neutral"})
        except Exception as exc:
            print(f"{ticker} research context unavailable ({type(exc).__name__})")
    print(f"{market} recent brokerage rating records: {len(evidence)}")
    return evidence


def scheduled_event_evidence(market, now, decision_at):
    """Screen the holding window and preceding 30 minutes against BLS releases."""
    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    # Verified BLS November/December lists and Fed FOMC calendar.
    fallback = (
        "2026-11-13T08:30", "2026-11-17T08:30",
        "2026-11-18T14:00", "2026-11-20T10:00", "2026-12-01T10:00",
        "2026-12-02T10:00", "2026-12-04T08:30", "2026-12-08T08:30",
        "2026-12-09T14:00", "2026-12-10T08:30", "2026-12-15T08:30",
        "2026-12-16T10:00", "2026-12-17T08:30", "2026-12-18T10:00",
        "2026-12-30T10:00",
    )
    eastern = ZoneInfo("America/New_York")
    try:
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        body = re.sub(r"\r?\n[ \t]", "", response.text)
        starts = []
        for event in body.split("BEGIN:VEVENT")[1:]:
            match = re.search(r"(?m)^DTSTART(?:;TZID=America/New_York)?:([0-9]{8}T[0-9]{6})(Z?)\r?$", event)
            if not match:
                raise ValueError("Unexpected BLS calendar timestamp")
            zone = ZoneInfo("UTC") if match[2] else eastern
            starts.append(datetime.strptime(match[1], "%Y%m%dT%H%M%S").replace(tzinfo=zone))
        if not starts or not min(starts) < decision_at < max(starts):
            raise ValueError("BLS calendar does not cover decision")
    except (requests.RequestException, ValueError) as exc:
        day = decision_at.astimezone(eastern).date().isoformat()
        if not "2026-11-14" <= day < "2026-12-31":
            print(f"{market} scheduled-event context unavailable ({type(exc).__name__})")
            return []
        print(f"{market} using checked BLS/Fed calendar through 2026-12-30")
        starts = [datetime.fromisoformat(day).replace(tzinfo=eastern) for day in fallback]
    fed = [datetime.fromisoformat(day).replace(tzinfo=eastern)
           for day in ("2026-11-18T14:00", "2026-12-09T14:00")]
    starts.extend(fed)
    risk = any(decision_at - timedelta(minutes=30) <= at <= decision_at + timedelta(minutes=31)
               for at in starts)
    state = "risk" if risk else "clear"
    print(f"{market} scheduled BLS events: {state}")
    return [{"kind": "event", "market": market, "symbol": "*",
             "available_at": now.isoformat(), "assessment": state}]


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
        bars[slot] = {"high": price, "low": price, "close": price, "last": event_time, "volume": size}
        return
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["volume"] += size
    if event_time >= bar["last"]:
        bar["close"], bar["last"] = price, event_time


def evaluate_breakout(bars, quote, close_time):
    if set(bars) != {0, 1, 2}:
        return "missing_bars", None
    if quote is None:
        return "missing_quote", None
    age = (close_time - datetime.fromisoformat(quote["event_time"])).total_seconds()
    if not 0 <= age <= MAX_QUOTE_AGE:
        return "stale_quote", None
    if quote["spread_pct"] > MAX_SPREAD_PCT:
        return "wide_spread", None
    volume = (bars[0]["volume"] + bars[1]["volume"]) / 2
    if volume <= 0 or bars[2]["volume"] / volume < MIN_VOLUME_RATIO:
        return "low_volume", None
    if bars[2]["close"] <= max(bars[0]["high"], bars[1]["high"]):
        return "no_price_breakout", None
    return "base_breakout", (bars[2]["volume"] / volume, quote["spread_pct"], bars[2]["close"])


def candidate_metrics(bars, quote, close_time):
    return evaluate_breakout(bars, quote, close_time)[1]


def evaluate_candidate(bars, quote, closes, window, symbol, evidence, overseas, observation_only=False):
    status, metrics = evaluate_breakout(bars, quote, closes)
    if metrics is None:
        return status, None
    if observation_only or evidence is None:
        status = context_status(evidence or [], window, symbol, closes)
        return status if status != "supported" else "observation_only", (symbol, *metrics, None, None)
    limit = breakout_v2(bars, quote, closes)
    if limit is None:
        return "strict_breakout_failed", None
    status = context_status(evidence, window, symbol, closes)
    if status != "supported":
        return status, None
    stop = protective_stop(bars, limit, quote["bid"], overseas)
    if stop is None:
        return "invalid_support_stop", None
    return "buy_signal", (symbol, *metrics, limit, stop)


async def collect(approval, market, symbols, opens, on_signal, samples_path, evidence=None, observation_only=False):
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
                candidates = []
                decisions = {}
                for symbol in symbols:
                    if symbol in rejected:
                        decisions[symbol] = "subscription_failed"
                        continue
                    status, candidate = evaluate_candidate(
                        bars[symbol], quotes.get(symbol), closes, samples_path.stem, symbol,
                        evidence, market == "US", observation_only,
                    )
                    decisions[symbol] = status
                    if candidate is not None:
                        candidates.append(candidate)
                counts = {status: sum(value == status for value in decisions.values())
                          for status in sorted(set(decisions.values()))}
                print(f"{samples_path.stem} 판정 집계 {json.dumps(counts)}", flush=True)
                for symbol, status in decisions.items():
                    print(f"{samples_path.stem} {symbol} 판정 {status}", flush=True)
                samples_path.with_name(f"{samples_path.stem}-decisions-replay.json").write_text(
                    json.dumps({"signal_at": closes.isoformat(), "counts": counts, "symbols": decisions},
                               indent=2), encoding="utf-8",
                )
                if len(rejected) == len(symbols):
                    raise RuntimeError("개장 전 구독 승인에 성공한 종목이 없습니다")
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
                     **{key: bar[key] for key in ("high", "low", "close", "volume")}}
                    for slot, bar in sorted(bars[symbol].items())
                ],
                "quotes": before + followup[symbol],
            }
            output.write(json.dumps(row, allow_nan=False) + "\n")
    await notification


def format_notification(market, candidates, closes):
    overseas = market.startswith("US-")
    session = {"US-early": "미국 프리장", "US-late": "미국 프리장 갱신",
               "NXT": "국내 NXT 프리장", "KRX": "국내 KRX 정규장"}[market]
    headline = "매수 신호" if candidates and candidates[0][4] is not None else "관찰 후보"
    timestamp = f"{closes:%m/%d %H:%M} ET · {closes.astimezone(KST):%H:%M} KST" if overseas else f"{closes:%m/%d %H:%M} KST"
    lines = [f"{headline} | {session}", timestamp]
    for symbol, ratio, spread, price, entry_limit, stop in candidates[:5]:
        ticker = symbol[4:] if overseas else symbol
        name = US_SYMBOL_NAMES[ticker] if overseas else KR_SYMBOL_NAMES[f"{symbol}.KS"]
        price_label = f"${price:,.2f}" if overseas else f"{price:,}원"
        lines.extend(["", f"{name} ({ticker}) · 신호가 {price_label}"])
        if entry_limit is not None:
            # Round the display down so it never exceeds the strategy's price cap.
            step = Decimal("0.01" if overseas else "1")
            limit = Decimal(str(entry_limit)).quantize(step, rounding=ROUND_DOWN)
            limit_label = f"${limit:,.2f}" if overseas else f"{limit:,}원"
            stop_label = f"${stop:,.2f}" if overseas else f"{stop:,.0f}원"
            risk_pct = (1 - stop / float(limit)) * 100
            expires = closes + timedelta(seconds=30)
            lines.extend([
                f"진입 상한 {limit_label} · {expires:%H:%M:%S}까지 ({'ET' if overseas else 'KST'})",
                "상한 초과·시간 경과·진입 전 손절선 이탈 시 진입 보류",
                f"손절선 {stop_label} · 진입 상한 대비 −{risk_pct:.2f}%",
                "손절 근거 · 돌파 5분봉 저점 아래 0.1% / 매도 가능 호가가 손절선 이하이면 청산",
                "목표 · 실제 체결가 대비 +2% / 신호 후 30분 경과 시 청산",
                "확인 · 증권사 의견·시황 우호 / 등록된 일정 위험 필터 통과",
            ])
        else:
            lines.append("진입 보류 · 거래량·가격 돌파를 관찰 중")
        lines.append(f"근거 · 5분봉 고점 돌파 / 거래량 {ratio:.2f}배 / 호가 간격 {spread:.2f}%")
    return "\n".join(lines)


def notify(market, candidates, closes, token, chat_id):
    message = format_notification(market, candidates, closes)
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
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
        if row[0] not in earlier
        or (row[4] is not None and (len(earlier[row[0]]) < 5 or earlier[row[0]][4] is None))
        or (row[3] > earlier[row[0]][3] and row[1] >= earlier[row[0]][1])
    ]


def main():
    import exchange_calendars

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market", choices=OPEN_TIMES)
    parser.add_argument("--context", type=Path, help="JSON array of timestamped evidence records")
    args = parser.parse_args()
    evidence = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else []
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
    scheduled_open = opens
    if not opens - timedelta(minutes=100) <= now <= opens + timedelta(minutes=14):
        raise SystemExit(f"수집 창을 놓쳤거나 너무 이릅니다. 현재 {now.isoformat()}, 목표 {opens.isoformat()}")

    keys = [os.getenv(name, "").strip() for name in (
        "KIS_APP_KEY", "KIS_APP_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    )]
    if not all(keys):
        raise SystemExit("KIS 및 Telegram 비밀값 4개를 설정하세요")
    app_key, app_secret, token, chat_id = keys
    snapshot = Path("us-early-candidates.json")
    try:
        previous_session = calendar.previous_session(today).date()
        symbols = daily_candidates(previous_session, overseas)
        if symbols:
            opens = collection_open(opens, datetime.now(local_zone))
        if symbols:
            observed_market = market_evidence(market, previous_session, datetime.now(local_zone))
            evidence.extend(observed_market)
            evidence.extend(research_evidence(market, symbols, datetime.now(local_zone)))
            evidence.extend(scheduled_event_evidence(
                market, datetime.now(local_zone), opens + timedelta(minutes=15)))
            if args.context or evidence:
                Path("signal-context.json").write_text(json.dumps(evidence), encoding="utf-8")
        available_kinds = {row["kind"] for row in evidence if row["market"] in (market, "*")}
        missing_kinds = {"research", "market", "event"} - available_kinds
        if missing_kinds:
            print(f"{market} missing context: {', '.join(sorted(missing_kinds))}; observation alerts only")
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
            wait_for_connection_window(opens)
            approval = request_approval(app_key, app_secret)
            opens = collection_open(scheduled_open, datetime.now(local_zone))
            if opens != scheduled_open:
                print(f"{market} 대체 수집 시작 {opens.isoformat()} · 알림 {opens + timedelta(minutes=15):%H:%M}")
            asyncio.run(collect(approval, "US" if overseas else market, symbols, opens, publish, Path(f"{market}.jsonl"), evidence, observation_only=bool(missing_kinds)))
        else:
            print(f"{market} 직전 거래일 관찰 후보 없음")
            publish([])
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, ValueError, TimeoutError, WebSocketException) as exc:
        raise SystemExit(f"{market} 장중 실행 실패 ({type(exc).__name__})") from None


if __name__ == "__main__":
    main()
