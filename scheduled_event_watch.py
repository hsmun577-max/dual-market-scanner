"""Early observation for an upcoming event, independently of the trend screen."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import headwind_watch
from supply_flow import naver_trend, number
from theme_sectors import KR_THEMES

KST = ZoneInfo("Asia/Seoul")
EVENTS = (
    {
        "name": "CES 2027",
        "theme": "휴머노이드·로봇·액추에이터",
        "starts_at": "2027-01-06T00:00:00-08:00",
        "calendar_url": "https://www.ces.tech/attend/registration-notification",
        "expectation": "삼성전자 휴머노이드 공개 가능성",
        "published_at": "2026-11-22T06:20:00+09:00",
        "expectation_url": "https://www.yna.co.kr/view/AKR20261121027600008",
        "issuers": {
            "277810.KQ": {
                "relationship": "삼성전자 미래로봇 개발 협력",
                "published_at": "2024-12-31T23:59:59+09:00",
                "source_url": (
                    "https://news.samsung.com/kr/삼성전자-레인보우로보틱스-최대주주-지위-확보"
                ),
            },
        },
    },
)
MAX_PEERS = 4


def _flows(symbol, session):
    try:
        rows = naver_trend(symbol.split(".")[0], sessions=10)
    except (requests.RequestException, ValueError):
        return None
    return next((row for row in rows if row.get("date") == session), None)


def _institution_buy(flow):
    if not flow:
        return None
    return number(flow.get("institution_qty"))


def attach(report, at, reference_session, *, read_flows=None):
    """Find linked issuers with relative strength and same-session theme buying."""
    read_flows = read_flows or _flows
    if at.utcoffset() is None:
        raise ValueError("Event observation requires an aware time")
    rows = {}
    if reference_session <= at.astimezone(KST).date().isoformat():
        rows = {row["symbol"]: row for row in report.get("results", ())
                if row.get("market") == "KR"
                and (row.get("indicators") or {}).get("data_date") == reference_session}
    watches = []
    report["scheduled_event_watch"] = watches
    flows = {}

    for event in EVENTS:
        known = datetime.fromisoformat(event["published_at"])
        start = datetime.fromisoformat(event["starts_at"])
        if not known <= at < start:
            continue
        members = dict(dict(KR_THEMES).get(event["theme"], ()))
        issuers = {
            symbol: link for symbol, link in event["issuers"].items()
            if symbol in rows and symbol in members
            and datetime.fromisoformat(link["published_at"]) <= at
            and headwind_watch.qualifies(rows[symbol])
        }
        if not issuers:
            continue
        peers = sorted((symbol for symbol in members if symbol in rows and symbol not in issuers),
                       key=lambda symbol: (
                           -(number(rows[symbol]["indicators"].get("average_turnover_20d")) or 0),
                           symbol))
        check = [*issuers, *peers[:MAX_PEERS]]
        missing = [symbol for symbol in check if symbol not in flows]
        if missing:
            with ThreadPoolExecutor(max_workers=5) as executor:
                observed = executor.map(lambda symbol: read_flows(symbol, reference_session), missing)
                for symbol, flow in zip(missing, observed):
                    flows[symbol] = flow if flow and flow.get("date") == reference_session else None
        buyers = [symbol for symbol in check if (_institution_buy(flows[symbol]) or 0) > 0]
        if len(buyers) < 2:
            continue
        for symbol, link in issuers.items():
            if symbol not in buyers:
                continue
            row = rows[symbol]
            values = row["indicators"]
            watches.append(dict(
                status="observation_only", event=event["name"], theme=event["theme"],
                starts_at=event["starts_at"], calendar_url=event["calendar_url"],
                expectation=event["expectation"], expectation_url=event["expectation_url"],
                available_at=event["published_at"], symbol=symbol,
                name=row.get("name") or members[symbol], relationship=link["relationship"],
                relationship_url=link["source_url"], reference_session=reference_session,
                relative_strength=values["relative_strength"], volume_ratio=values["volume_ratio"],
                institutional_buy_shares=_institution_buy(flows[symbol]),
                foreign_net_shares=number(flows[symbol].get("foreign_qty")),
                buying_peers=[members[peer] for peer in buyers if peer != symbol],
                flow_source="Naver Finance deal trend",
            ))
    return watches


def format_block(items, market):
    if not items or market not in (None, "KR"):
        return []
    lines = ["[산업 행사 사전 관찰]"]
    for row in items[:4]:
        peer_names = "·".join(row["buying_peers"][:3])
        foreign = row.get("foreign_net_shares")
        foreign_line = f" · 외인 {foreign:+,.0f}주" if foreign is not None else ""
        lines.extend([
            f"• {row['name']} · {row['event']} {row['expectation']}",
            f"  {row['reference_session'][5:]} 기관 +{row['institutional_buy_shares']:,.0f}주"
            f"{foreign_line} · {peer_names} 기관 동반 매수"
            f" · 지수 대비 20일 {row['relative_strength'] * 100:+.1f}%p"
            f" · 거래량 {row['volume_ratio']:.2f}배",
        ])
    return lines
