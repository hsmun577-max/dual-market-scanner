"""Read-only relative-strength watch for securities under a falling benchmark."""
import math

from research_asset import is_fund

# 20세션 기준 지수 대비 초과수익(종목 20일 수익률 − 지수 20일 수익률) +10%p.
MIN_RELATIVE_STRENGTH = 0.10
# 당일 거래량 / 20일 평균 거래량. 하락장에서 돈이 실제로 들어온 날만 본다.
MIN_VOLUME_RATIO = 2.0
# 한 시장에서 이름으로 보여 줄 최대 건수. 넘는 건은 '외 N건'으로 센다.
MAX_PER_MARKET = 5
HEADING = '[하락장 관찰 후보] · 매수 신호 아님'
MARKET_LABELS = {'KR': '국내', 'US': '미국'}
# 블록 전체에 한 번만 붙는 안내. 계획 한 건마다 반복하지 않는다(2026-11-25 지시).
NOTE = '※ 지수 20일선 아래에서 상대강도가 높은 종목'


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def qualifies(row):
    """개별주 + RS ≥ +10%p + 거래량 ≥ 2.0배인가. 게이트 판정은 부르는 쪽이 이미 했다."""
    if not isinstance(row, dict):
        return False
    if row.get('market') not in MARKET_LABELS:
        return False
    values = row.get('indicators') or {}
    if is_fund(row):
        return False
    relative = values.get('relative_strength')
    volume = values.get('volume_ratio')
    return (_number(relative) and _number(volume)
            and relative >= MIN_RELATIVE_STRENGTH and volume >= MIN_VOLUME_RATIO)


def observation(row):
    """관찰 후보 한 건. 조건을 못 채우면 ``None``."""
    if not qualifies(row):
        return None
    values = row['indicators']
    change = values.get('return_1d')
    return dict(symbol=row.get('symbol'), market=row.get('market'),
                name=row.get('name') or row.get('symbol'),
                change_pct=round(change * 100, 1) if _number(change) else None,
                relative_strength=round(values['relative_strength'], 4),
                volume_ratio=round(values['volume_ratio'], 2),
                reference_price=values.get('price'),
                reference_session=values.get('data_date'),
                gate='market_headwind')


def rank_key(item):
    """상대강도 → 거래량 → 종목. 게이트가 가장 세게 막은 이름이 위로 온다."""
    return (-(item.get('relative_strength') or 0), -(item.get('volume_ratio') or 0),
            item.get('symbol') or '')


def select(report, limit=MAX_PER_MARKET):
    """``report['results']``에서 게이트 때문에 보류된 관찰 후보를 시장별로 고른다.

    스캔 행의 ``preopen_decision``이 ``market_headwind``인 것만 본다 — 자기 차트 조건에서
    먼저 걸러진 종목은 게이트 때문에 보류된 것이 아니므로 여기 오지 않는다.
    """
    grouped = {}
    for row in (report or {}).get('results') or []:
        if row.get('preopen_decision') != 'market_headwind':
            continue
        item = observation(row)
        if item:
            grouped.setdefault(item['market'], []).append(item)
    for market, items in grouped.items():
        items.sort(key=rank_key)
        grouped[market] = items[:max(0, limit)]
    return grouped


def _price_label(item):
    price = item.get('reference_price')
    if not _number(price):
        return None
    return f"${price:,.2f}" if item.get('market') == 'US' else f"{price:,.0f}원"


def _session_label(value):
    """'2026-11-26' → '11/26'. 기준일을 숨기지 않고 짧게 붙인다."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    return f"{int(value[5:7])}/{int(value[8:10])} 종가"


def format_line(item):
    """한 줄: 종목명(코드) · 등락 · RS · 거래량 · 가격."""
    symbol = str(item.get('symbol') or '')
    code = symbol if item.get('market') == 'US' else symbol.split('.')[0]
    parts = []
    if item.get('change_pct') is not None:
        parts.append(f"{item['change_pct']:+.1f}%")
    parts.append(f"RS {item['relative_strength'] * 100:+.1f}%p")
    parts.append(f"거래량 {item['volume_ratio']:.2f}배")
    price = _price_label(item)
    if price:
        parts.append(price)
    line = f"• {item.get('name')} ({code}) · " + ' · '.join(parts)
    session = _session_label(item.get('reference_session'))
    if session:
        line += f" ({session})"
    return line


def format_block(items, market=None):
    """텔레그램 블록. 매수 후보 섹션과 섞이지 않게 머리줄에서 매수 신호 아님을 밝힌다."""
    if isinstance(items, dict):
        chosen = {name: rows for name, rows in items.items() if market in (None, name)}
    else:
        chosen = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if market not in (None, item.get('market')):
                continue
            chosen.setdefault(item.get('market'), []).append(item)
    groups = {}
    for name, rows in chosen.items():
        items_ = [item for item in (rows or []) if isinstance(item, dict)]
        if items_:
            items_.sort(key=rank_key)
            groups[name] = items_
    if not groups:
        return []
    lines = [HEADING]
    for name in sorted(groups):
        rows = groups[name]
        if market is None and len(groups) > 1:
            lines.append(f"[{MARKET_LABELS.get(name, name)}]")
        lines.extend(format_line(item) for item in rows[:MAX_PER_MARKET])
        if len(rows) > MAX_PER_MARKET:
            lines.append(f"   외 {len(rows) - MAX_PER_MARKET}건 (분석자료 참고)")
    lines.append(NOTE)
    return lines
