"""Check KIS domestic real-time data before enabling intraday alerts."""

import argparse
import asyncio
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


KST = timezone(timedelta(hours=9))
APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
FEED_URL = "ws://ops.koreainvestment.com:21000/tryitout"
FEEDS = {
    "NXT": {"H0NXCNT0": "trade", "H0NXASP0": "quote"},
    "KRX": {"H0STCNT0": "trade", "H0STASP0": "quote"},
    "US": {"HDFSCNT0": "trade", "HDFSASP0": "quote"},
}


def request_approval(app_key, app_secret):
    try:
        response = requests.post(
            APPROVAL_URL,
            json={"grant_type": "client_credentials", "appkey": app_key, "secretkey": app_secret},
            timeout=15,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"KIS 접속키 발급 실패 (HTTP {response.status_code})")
        approval = response.json().get("approval_key")
    except (requests.RequestException, ValueError, AttributeError):
        raise RuntimeError("KIS 접속키 응답을 읽지 못했습니다") from None
    if not isinstance(approval, str) or not approval.strip():
        raise RuntimeError("KIS 접속키가 응답에 없습니다")
    return approval


def subscription(approval, tr_id, symbol):
    return {
        "header": {
            "approval_key": approval, "custtype": "P",
            "tr_type": "1", "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": tr_id, "tr_key": symbol}},
    }


def parse_records(raw, feeds, received_at):
    parts = raw.split("|", 3)
    if len(parts) != 4 or parts[0] != "0" or parts[1] not in feeds:
        return []
    _, tr_id, count_text, payload = parts
    fields = payload.removesuffix("^").split("^")
    count = int(count_text)
    if count < 1 or len(fields) % count:
        raise ValueError(f"{tr_id}: {len(fields)} fields for {count} records")
    width = len(fields) // count
    kind = feeds[tr_id]
    overseas = tr_id in FEEDS["US"]
    minimum_width = (19 if kind == "trade" else 12) if overseas else (13 if kind == "trade" else 14)
    if width < minimum_width:
        raise ValueError(f"{tr_id}: received {width} fields, expected at least {minimum_width}")

    records = []
    for start in range(0, len(fields), width):
        row = fields[start:start + width]
        if overseas:
            date_index = 5 if kind == "trade" else 4
            date_text, time_text = row[date_index:date_index + 2]
            event_zone = KST
            if len(date_text) != 8 or not date_text.isdigit() or date_text == "00000000":
                date_text, time_text = row[date_index - 2:date_index]
                event_zone = ZoneInfo("America/New_York")
            try:
                event_time = datetime.strptime(
                    date_text + time_text.split(".")[0].zfill(6), "%Y%m%d%H%M%S"
                ).replace(tzinfo=event_zone).astimezone(KST)
            except ValueError:
                continue
        else:
            clock = datetime.strptime(row[1], "%H%M%S").time()
            event_time = datetime.combine(received_at.date(), clock, KST)
        lag = (received_at - event_time).total_seconds()
        record = {
            "symbol": row[0], "kind": kind, "event_time": event_time.isoformat(),
            "received_at": received_at.isoformat(), "lag_seconds": round(lag, 3),
        }
        if kind == "trade":
            price, size = (float(row[10] or "0"), float(row[18] or "0")) if overseas else (int(row[2]), int(row[12]))
            if not math.isfinite(price) or not math.isfinite(size) or price <= 0 or size <= 0:
                continue
            record.update(price=price, size=size)
        else:
            ask, bid = (float(row[11] or "0"), float(row[10] or "0")) if overseas else (int(row[3]), int(row[13]))
            if not math.isfinite(ask) or not math.isfinite(bid) or bid <= 0 or ask < bid:
                continue
            record.update(bid=bid, ask=ask, spread_pct=round((ask - bid) * 200 / (ask + bid), 4))
        records.append(record)
    return records


async def probe(approval, market, symbol, seconds):
    feeds = FEEDS[market]
    seen = set()
    async with connect(FEED_URL, open_timeout=15, close_timeout=3) as socket:
        for tr_id in feeds:
            await socket.send(json.dumps(subscription(approval, tr_id, symbol)))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline and len(seen) < len(feeds):
            try:
                raw = await asyncio.wait_for(socket.recv(decode=True), deadline - loop.time())
            except TimeoutError:
                break
            if raw.startswith("{"):
                status = json.loads(raw)
                tr_id = status.get("header", {}).get("tr_id")
                if tr_id == "PINGPONG":
                    await socket.pong(raw.encode("utf-8"))
                elif tr_id in feeds and status.get("body", {}).get("rt_cd") not in (None, "0"):
                    raise RuntimeError(f"{market} {feeds[tr_id]} 구독이 거절됐습니다")
                continue
            try:
                records = parse_records(raw, feeds, datetime.now(KST))
            except ValueError as exc:
                raise RuntimeError(f"{market} 시세 응답 형식 오류: {exc}") from None
            for record in records:
                kind = record["kind"]
                matches = record["symbol"] == symbol or (market == "US" and record["symbol"] == symbol[4:])
                if matches and kind not in seen:
                    seen.add(kind)
                    print(json.dumps({"market": market, **record}, ensure_ascii=False))
    missing = set(feeds.values()) - seen
    if missing:
        raise RuntimeError(f"{market} 제한 시간 내 미수신: {', '.join(sorted(missing))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market", choices=FEEDS)
    parser.add_argument("--symbol", help="Korean symbol or KIS overseas subscription key")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    symbol = args.symbol or ("DNASAAPL" if args.market == "US" else "005930")
    symbol_pattern = r"D(?:NAS|NYS|AMS)[A-Z0-9.\-]+" if args.market == "US" else r"[0-9A-Z]{6}"
    if not re.fullmatch(symbol_pattern, symbol) or args.seconds < 1:
        parser.error("시장에 맞는 종목코드와 1초 이상의 수신 시간을 지정하세요")
    app_key = os.getenv("KIS_APP_KEY", "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        parser.exit(1, "KIS_APP_KEY와 KIS_APP_SECRET을 설정하세요.\n")
    try:
        approval = request_approval(app_key, app_secret)
        asyncio.run(probe(approval, args.market, symbol, args.seconds))
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
        parser.exit(1, f"KIS 시세 연결 실패 ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
