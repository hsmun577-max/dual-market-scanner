"""Check KIS domestic real-time data before enabling intraday alerts."""

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


KST = timezone(timedelta(hours=9))
APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
FEED_URL = "ws://ops.koreainvestment.com:21000/tryitout"
FEEDS = {
    "NXT": {"H0NXCNT0": "trade", "H0NXASP0": "quote"},
    "KRX": {"H0STCNT0": "trade", "H0STASP0": "quote"},
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
    fields = payload.split("^")
    count = int(count_text)
    if count < 1 or len(fields) % count:
        raise ValueError("Unexpected KIS frame size")
    width = len(fields) // count
    kind = feeds[tr_id]
    if width < (13 if kind == "trade" else 14):
        raise ValueError("Unexpected KIS record width")

    records = []
    for start in range(0, len(fields), width):
        row = fields[start:start + width]
        clock = datetime.strptime(row[1], "%H%M%S").time()
        event_time = datetime.combine(received_at.date(), clock, KST)
        lag = (received_at - event_time).total_seconds()
        record = {
            "symbol": row[0], "kind": kind, "event_time": event_time.isoformat(),
            "received_at": received_at.isoformat(), "lag_seconds": round(lag, 3),
        }
        if kind == "trade":
            price, size = int(row[2]), int(row[12])
            if price <= 0 or size <= 0:
                continue
            record.update(price=price, size=size)
        else:
            ask, bid = int(row[3]), int(row[13])
            if bid <= 0 or ask < bid:
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
            except ValueError:
                raise RuntimeError(f"{market} 시세 응답 형식이 예상과 다릅니다") from None
            for record in records:
                kind = record["kind"]
                if record["symbol"] == symbol and kind not in seen:
                    seen.add(kind)
                    print(json.dumps({"market": market, **record}, ensure_ascii=False))
    missing = set(feeds.values()) - seen
    if missing:
        raise RuntimeError(f"{market} 제한 시간 내 미수신: {', '.join(sorted(missing))}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market", choices=FEEDS)
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9A-Z]{6}", args.symbol) or args.seconds < 1:
        parser.error("종목코드 6자리와 1초 이상의 수신 시간을 지정하세요")
    app_key = os.getenv("KIS_APP_KEY", "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    if not app_key or not app_secret:
        parser.exit(1, "KIS_APP_KEY와 KIS_APP_SECRET을 설정하세요.\n")
    try:
        approval = request_approval(app_key, app_secret)
        asyncio.run(probe(approval, args.market, args.symbol, args.seconds))
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
        parser.exit(1, f"KIS 시세 연결 실패 ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
