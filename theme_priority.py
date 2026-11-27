"""Prioritize research using confirmed theme breadth and institutional demand."""
from concurrent.futures import ThreadPoolExecutor

from candidate_selection import liquidity_rejection_reason
from research_asset import is_fund
from scheduled_event_watch import session_flow
from supply_flow import number

MIN_OUTPERFORMERS = 3
RESEARCH_SLOTS = 2
FLOW_CANDIDATES = 4


def _strong_themes(snapshots, market, session, rows):
    for snapshot in snapshots:
        if (snapshot.get("market") != market or snapshot.get("data_date") != session
                or not snapshot.get("benchmark_aligned") or snapshot.get("intraday")):
            continue
        for rank, theme in enumerate(snapshot.get("rows") or (), 1):
            excess = number(theme.get("market_excess_pct"))
            multiple = number(theme.get("inflow_multiple"))
            benchmark = number(theme.get("benchmark_return_pct"))
            if (theme.get("data_date") != session or theme.get("dumping")
                    or excess is None or excess <= 0 or multiple is None or multiple <= 1
                    or benchmark is None):
                continue
            members = []
            for symbol in dict.fromkeys(theme.get("symbols") or ()):
                values = (rows.get(symbol) or {}).get("indicators") or {}
                change = number(values.get("return_1d"))
                if values.get("data_date") == session and change is not None:
                    members.append((symbol, change * 100 > benchmark))
            outperformers = sum(outperformed for _, outperformed in members)
            if outperformers >= MIN_OUTPERFORMERS and 2 * outperformers >= (theme.get("total") or len(members)):
                yield rank, theme, [symbol for symbol, _ in members], outperformers


def _rank(evidence):
    if not evidence:
        return 0, 0
    return -evidence["priority"], evidence["theme_rank"]


def prioritize(report, pool, selected, market, session, *, read_flows=None):
    """Return the research order and store only independently supported priorities."""
    read_flows = read_flows or session_flow
    rows = {row["symbol"]: row for row in pool if not is_fund(row)}
    priorities = {}
    for rank, theme, members, outperformers in _strong_themes(
            report.get("theme_sectors") or (), market, session, rows):
        for symbol in members:
            if symbol in priorities:
                continue
            row = rows[symbol]
            values = row["indicators"]
            change, benchmark, strength = (number(values.get(key)) for key in (
                "return_1d", "market_return_1d", "relative_strength"))
            if (None in (change, benchmark, strength) or change <= benchmark or strength <= 0
                    or (market == "KR" and row.get("nonchart_risk"))
                    or liquidity_rejection_reason(row, market)):
                continue
            priorities[symbol] = dict(symbol=symbol, name=row.get("name") or symbol, market=market,
                                      session=session, theme=theme["name"], theme_rank=rank,
                                      priority=1, outperformers=outperformers,
                                      turnover_multiple=theme["inflow_multiple"],
                                      peers=[peer for peer in members if peer != symbol])

    selected_by_symbol = {row["symbol"]: row for row in selected}
    order = sorted(priorities, key=lambda symbol: (
        *_rank(priorities[symbol]), -(number(rows[symbol]["indicators"].get("relative_strength")) or 0),
        symbol))
    if market == "KR":
        targets = order[:FLOW_CANDIDATES]
        peers = {symbol: sorted(priorities[symbol]["peers"], key=lambda peer: (
                      -(number(rows[peer]["indicators"].get("average_turnover_20d")) or 0), peer))[:2]
                 for symbol in targets}
        checks = list(dict.fromkeys([*targets, *(peer for group in peers.values() for peer in group)]))
        if checks:
            with ThreadPoolExecutor(max_workers=5) as pool_executor:
                flows = dict(zip(checks, pool_executor.map(
                    lambda symbol: read_flows(symbol, session), checks)))
            buys = {symbol for symbol, row in flows.items() if row and row.get("date") == session
                    and (number(row.get("institution_qty")) or 0) > 0}
            for symbol in targets:
                confirmed = [peer for peer in peers[symbol] if peer in buys]
                if symbol in buys and confirmed:
                    priorities[symbol].update(priority=2, institutional_buy=True,
                                               buying_peers=[rows[peer].get("name") or peer
                                                             for peer in confirmed])
    order.sort(key=lambda symbol: _rank(priorities[symbol]))
    extra = (symbol for symbol in order if symbol not in selected_by_symbol)
    for symbol in list(extra)[:RESEARCH_SLOTS]:
        selected_by_symbol[symbol] = rows[symbol]

    report.setdefault("theme_research_priority", {})[market] = [
        {k: value for k, value in priorities[symbol].items() if k != "peers"} for symbol in order
    ]
    return sorted(selected_by_symbol.values(),
                  key=lambda row: _rank(priorities.get(row["symbol"])))


def format_block(groups, market):
    items = [row for code, rows in (groups or {}).items() if not market or market == code for row in rows]
    if not items:
        return []
    items.sort(key=_rank)
    lines = ["[강세 테마 우선 관찰]"]
    for row in items[:4]:
        peers = "·".join(row.get("buying_peers") or ())
        detail = f" · 기관 동반 순매수 ({peers})" if peers else ""
        lines.append(f"• {row['name']} · {row['theme']} · "
                     f"{row['outperformers']}종목 지수 상회 · 거래대금 {row['turnover_multiple']:.2f}배{detail}")
    return lines
