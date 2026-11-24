"""자산군 상승 흐름 헤더 + 시장 분위기 한 줄.

사용자 지시(2026-11-24): "상승흐름에 들어간 투자처 분석해서 흐름이 바뀌면 알려줘",
"이것도 스캐너에 넣어줘 항상 상위에 상승흐름인자산 제목으로 적어줘",
"추세만보지말고 시장분위기분석해서 적어".

텔레그램으로 나가는 모든 알림의 첫 블록은 세 줄이다.

    1행  📈 지금 상승 흐름: 국내주식(+2.1%), 금·은(+3.4%)   (없으면 📉 전 자산 조정 구간 …)
    2행  🌡️ 시장 분위기: 경계 속 단기 반등 시도 (VIX 26.6(-8.2%) 공포 진정 · 미10년물 4.92% 고금리 부담 · 달러 104.3 강세)
    3행  ⚡ 흐름 변화: 금·은 상승 진입 / 원자재 상승 이탈     (바뀐 것이 있을 때만)

1행은 일봉 종가와 20일선·60일선만 본다(:func:`state_of`). 자산군 하나는 구성 지표
과반이 상승 추세일 때만 `상승 흐름`으로 올린다 — 원자재처럼 구성이 따로 노는 군에서
구리 하나 오른 것을 '원자재 상승'으로 읽지 않기 위해서다. 괄호 안 퍼센트는 그 판정에
쓰인 창과 같은 20거래일(약 한 달) 수익률이다.

2행은 추세가 아니라 심리·매크로다(:func:`sentiment`). VIX(공포), 미10년물 금리(할인율
압력), 달러(위험자산 유입의 반대편), HYG/IEF 비율(위험선호 vs 안전자산)을 읽어 국면을
한 단어로 정하고, 근거 2~3개를 괄호에 붙인다. 뉴스 헤드라인이 있으면 맥락으로 덧붙인다.

3행은 직전 스캔의 `asset_regime` 블록과 비교한 결과다(:func:`changes`). 비교할 직전
스캔이 없으면 같은 일봉의 전일 대비로 판정하고, 그 기준을 줄 끝에 밝힌다.

이 모듈은 어떤 알림도 막지 않는다. 시세를 못 받은 지표는 사유와 함께 빠지고, 그것이
스캔·발송 자체를 깨뜨리지 않는다.
"""

import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MA_FAST = 20
MA_SLOW = 60
RETURN_DAYS = 20
SHORT_RETURN_DAYS = 5
# 60일선을 계산하려면 60개 종가가 필요하다. 그보다 짧은 이력은 판정하지 않는다.
MIN_BARS = MA_SLOW
MAX_FLOW_ITEMS = 6
MAX_HEADLINE = 44

# 자산군 구성. 원/달러(KRW=X)는 오르면 달러 강세·원화 약세이므로 달러군에 둔다.
GROUPS = (
    dict(key='kr_equity', label='국내주식',
         instruments=(('^KS11', '코스피'), ('^KQ11', '코스닥'))),
    dict(key='us_equity', label='미국주식',
         instruments=(('SPY', 'S&P500'), ('QQQ', '나스닥100'))),
    dict(key='dollar', label='달러',
         instruments=(('DX-Y.NYB', '달러인덱스'), ('KRW=X', '원/달러'))),
    dict(key='gold_silver', label='금·은',
         instruments=(('GLD', '금'), ('SLV', '은'))),
    dict(key='treasury', label='미국채',
         instruments=(('TLT', '미국 장기채'), ('IEF', '미국 중기채'))),
    dict(key='commodity', label='원자재',
         instruments=(('CPER', '구리'), ('USO', '원유'), ('DBA', '농산물'))),
    dict(key='reit', label='리츠',
         instruments=(('VNQ', '미국 리츠'),)),
    dict(key='bitcoin', label='비트코인',
         instruments=(('BTC-USD', '비트코인'),)),
)
# 심리·매크로 지표. IEF는 미국채군에서 이미 받으므로 여기서 다시 받지 않는다.
SENTIMENT_SYMBOLS = ('^VIX', '^TNX', 'HYG', 'DX-Y.NYB')

VIX_CALM = 15.0
VIX_CAUTION = 22.0
VIX_FEAR = 30.0
US10Y_STRESS = 4.5
US10Y_ALERT = 5.0
DOLLAR_MOVE_PCT = 1.0

PREVIOUS_PATHS = (
    Path('reports/asset-regime-state.json'),
    Path('opening-scan/latest.json'),
    Path('reports/latest.json'),
    Path('asset-regime/previous.json'),
)
STATE_PATH = Path('reports/asset-regime-state.json')


def _number(value, positive=True):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result if (result > 0 or not positive) else None


def _mean(values):
    return sum(values) / len(values)


def _series(entry):
    """(종가 리스트, 날짜 리스트) — 오래된 것부터. 시세가 아닌 값은 짝과 함께 버린다."""
    if entry is None:
        return [], []
    if isinstance(entry, dict):
        closes, dates = entry.get('closes') or [], entry.get('dates') or []
    else:
        closes, dates = entry, []
    if len(dates) != len(closes):
        dates = [None] * len(closes)
    values, days = [], []
    for close, day in zip(closes, dates):
        number = _number(close)
        if number is None:
            continue
        values.append(number)
        days.append(str(day)[:10] if day else None)
    return values, days


def state_of(close, ma_fast, ma_slow):
    """20일선·60일선 기준 추세. 20일선 위에서 정배열이면 상승, 역배열이면 반등, 아래면 조정."""
    if None in (close, ma_fast, ma_slow):
        return 'unavailable'
    if close > ma_fast:
        return 'uptrend' if ma_fast > ma_slow else 'rebound'
    return 'downtrend'


def _return_pct(values, days):
    if len(values) <= days or values[-1 - days] <= 0:
        return None
    return round((values[-1] / values[-1 - days] - 1) * 100, 2)


def trend(values):
    """한 지표의 현재·전일 추세와 수익률. 이력이 모자라면 판정하지 않는다."""
    if len(values) < MIN_BARS:
        return dict(state='unavailable', reason='insufficient_history', bars=len(values))
    close, ma_fast, ma_slow = values[-1], _mean(values[-MA_FAST:]), _mean(values[-MA_SLOW:])
    record = dict(state=state_of(close, ma_fast, ma_slow), bars=len(values),
                  close=round(close, 4), ma20=round(ma_fast, 4), ma60=round(ma_slow, 4),
                  return_20d_pct=_return_pct(values, RETURN_DAYS),
                  return_5d_pct=_return_pct(values, SHORT_RETURN_DAYS))
    record['previous_state'] = None
    if len(values) > MIN_BARS:
        previous = values[:-1]
        record['previous_state'] = state_of(
            previous[-1], _mean(previous[-MA_FAST:]), _mean(previous[-MA_SLOW:]))
    return record


def instrument(symbol, label, entry):
    values, days = _series(entry)
    record = dict(symbol=symbol, label=label)
    record.update(trend(values))
    if days and days[-1]:
        record['data_date'] = days[-1]
    if len(days) > 1 and days[-2]:
        record['previous_date'] = days[-2]
    return record


def group_state(states):
    """구성 지표 과반이 상승이면 그 자산군을 상승 흐름으로 본다.

    2개짜리 군은 둘 다, 3개짜리 군은 2개 이상이어야 한다. 반등은 과반이 20일선 위일 때다.
    """
    known = [state for state in states if state in ('uptrend', 'rebound', 'downtrend')]
    if not known:
        return 'unavailable'
    if sum(1 for state in known if state == 'uptrend') * 2 > len(known):
        return 'uptrend'
    if sum(1 for state in known if state == 'rebound') * 2 >= len(known):
        return 'rebound'
    return 'downtrend'


def group_block(spec, series):
    members = [instrument(symbol, label, series.get(symbol)) for symbol, label in spec['instruments']]
    state = group_state([row['state'] for row in members])
    if state in ('uptrend', 'rebound'):
        # 상승·반등은 그 판정에 든 지표의 수익률만 보여 준다.
        basis = [row for row in members if row['state'] == state]
    else:
        basis = [row for row in members if row['state'] in ('uptrend', 'rebound', 'downtrend')]
    returns = [row['return_20d_pct'] for row in basis if row.get('return_20d_pct') is not None]
    return dict(key=spec['key'], label=spec['label'], state=state,
                return_20d_pct=round(_mean(returns), 2) if returns else None,
                previous_state=group_state([row.get('previous_state') for row in members]),
                observed=sum(1 for row in members if row['state'] != 'unavailable'),
                total=len(members), instruments=members)


def _level(entry, window=1):
    values, days = _series(entry)
    if not values:
        return None
    level = values[-1]
    change = None
    if len(values) > window and values[-1 - window] > 0:
        change = round((values[-1] / values[-1 - window] - 1) * 100, 2)
    return dict(level=level, change_pct=change, bars=len(values),
                data_date=days[-1] if days else None)


def _yield(entry):
    """^TNX는 4.92처럼 퍼센트로 오지만, 10배로 주는 피드도 있어 20을 넘으면 나눈다."""
    values, days = _series(entry)
    if not values:
        return None
    scale = 10.0 if values[-1] > 20 else 1.0
    level = values[-1] / scale
    change = None
    if len(values) > 1:
        change = round(level - values[-2] / scale, 3)
    return dict(percent=round(level, 3), change_pt=change, bars=len(values),
                data_date=days[-1] if days else None)


def _risk_mix(risky, safe):
    """HYG/IEF 비율이 자기 20일선 위에서 오르면 위험선호, 반대면 안전자산 선호."""
    risky_values, _ = _series(risky)
    safe_values, _ = _series(safe)
    length = min(len(risky_values), len(safe_values))
    if length <= MA_FAST:
        return dict(trend='unavailable', reason='insufficient_history')
    ratio = [risky_values[-length + index] / safe_values[-length + index]
             for index in range(length) if safe_values[-length + index] > 0]
    if len(ratio) <= MA_FAST:
        return dict(trend='unavailable', reason='insufficient_history')
    last, average = ratio[-1], _mean(ratio[-MA_FAST:])
    change = round((last / ratio[-1 - RETURN_DAYS] - 1) * 100, 2) if len(ratio) > RETURN_DAYS else None
    if last > average and (change is None or change > 0):
        trend = 'risk_on'
    elif last < average and (change is None or change < 0):
        trend = 'risk_off'
    else:
        trend = 'flat'
    return dict(trend=trend, change_20d_pct=change, above_ma20=last > average,
                ratio=round(last, 4))


def vix_state(level):
    if level is None:
        return None
    if level < VIX_CALM:
        return '안정'
    if level < VIX_CAUTION:
        return '보통'
    if level < VIX_FEAR:
        return '경계'
    return '공포'


def _vix_phrase(level, change):
    if change is None:
        return '전일비 미확인'
    if change <= -3:
        return '공포 진정' if (level or 0) >= VIX_CAUTION else '변동성 진정'
    if change >= 3:
        return '공포 확대' if (level or 0) >= VIX_CAUTION else '변동성 확대'
    return '변동성 보합'


def _yield_phrase(percent, change):
    if percent >= US10Y_ALERT:
        phrase = '금리 경계'
    elif percent >= US10Y_STRESS:
        phrase = '고금리 부담'
    elif percent <= 3.5:
        phrase = '금리 부담 완화'
    else:
        phrase = '금리 중립'
    if change is not None and change >= 0.08:
        phrase += ' · 급등'
    elif change is not None and change <= -0.08:
        phrase += ' · 진정'
    return phrase


def _dollar_trend(change):
    if change is None:
        return '보합'
    if change >= DOLLAR_MOVE_PCT:
        return '강세'
    if change <= -DOLLAR_MOVE_PCT:
        return '약세'
    return '보합'


def _base_mood(vix, risk):
    state = vix.get('state') if vix else None
    trend = (risk or {}).get('trend')
    if state == '공포':
        return '공포'
    if state == '경계':
        return '경계'
    if trend == 'risk_off':
        return '위험회피 우위'
    if trend == 'risk_on' and state in ('안정', '보통'):
        return '위험선호 우위'
    if state == '안정':
        return '안정'
    return '혼조'


def mood_label(base, vix, uptrend_count):
    """심리 국면 한 단어 + 지금 자산 흐름이 그 안에서 무엇을 하는지."""
    change = (vix or {}).get('change_pct')
    calming = change is not None and change <= -3
    if base == '공포':
        label = '공포 진정 국면' if calming else '공포 구간'
    else:
        label = base
    if uptrend_count:
        if label in ('경계', '공포 구간', '위험회피 우위'):
            return f'{label} 속 단기 반등 시도'
        if label == '공포 진정 국면':
            return '공포 진정 속 반등 시도'
        if label == '위험선호 우위':
            return '위험선호 확산'
        if label == '안정':
            return '안정 속 상승 지속'
        return '혼조 속 일부 자산 상승'
    if label == '위험선호 우위':
        return '위험선호 우위 · 자산은 조정'
    if label in ('경계', '공포 구간', '공포 진정 국면'):
        return f'{label} 속 전 자산 조정'
    return label


def _clip(text, limit=MAX_HEADLINE):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def sentiment(series, context=None):
    """심리·매크로 한 줄. 받지 못한 지표는 사유와 함께 빠지고 나머지로 판정한다."""
    errors = []
    vix_level = _level(series.get('^VIX'))
    vix = None
    if vix_level:
        vix = dict(level=round(vix_level['level'], 2), change_pct=vix_level['change_pct'],
                   state=vix_state(vix_level['level']), data_date=vix_level['data_date'])
        vix['phrase'] = _vix_phrase(vix['level'], vix['change_pct'])
    else:
        errors.append('vix_unavailable')
    ten = _yield(series.get('^TNX'))
    if ten:
        ten['phrase'] = _yield_phrase(ten['percent'], ten['change_pt'])
    else:
        errors.append('us10y_unavailable')
    dollar_level = _level(series.get('DX-Y.NYB'), RETURN_DAYS)
    dollar = None
    if dollar_level:
        dollar = dict(level=round(dollar_level['level'], 2),
                      change_20d_pct=dollar_level['change_pct'],
                      trend=_dollar_trend(dollar_level['change_pct']),
                      data_date=dollar_level['data_date'])
    else:
        errors.append('dollar_unavailable')
    risk = _risk_mix(series.get('HYG'), series.get('IEF'))
    if risk.get('trend') == 'unavailable':
        errors.append('risk_mix_' + str(risk.get('reason') or 'unavailable'))

    evidence = []
    if vix:
        change = '' if vix['change_pct'] is None else f"({vix['change_pct']:+.1f}%)"
        evidence.append(f"VIX {vix['level']:.1f}{change} {vix['phrase']}")
    if ten:
        change = '' if ten['change_pt'] is None else f"({ten['change_pt']:+.2f}%p)"
        evidence.append(f"미10년물 {ten['percent']:.2f}%{change} {ten['phrase']}")
    if dollar:
        evidence.append(f"달러 {dollar['level']:.1f} {dollar['trend']}")
    evidence = evidence[:3]
    if not evidence:
        return _unavailable_sentiment('sentiment_indicators_unavailable')
    # 자산 흐름은 assess가 알려 주므로 국면 이름과 요약은 거기서 완성한다.
    return dict(regime_label=None, base_mood=_base_mood(vix, risk),
                # 브리프가 정한 필드 이름은 값으로 두고, 상세는 details에 남긴다.
                vix=vix['level'] if vix else None,
                vix_state=vix['state'] if vix else None,
                vix_change_pct=vix['change_pct'] if vix else None,
                vix_phrase=vix['phrase'] if vix else None,
                us10y=ten['percent'] if ten else None,
                us10y_change_pt=ten['change_pt'] if ten else None,
                us10y_phrase=ten['phrase'] if ten else None,
                hyg_ief_trend=risk.get('trend'), risk_mix=risk,
                dollar_index=dollar['level'] if dollar else None,
                dollar_trend=dollar['trend'] if dollar else None,
                dollar_change_20d_pct=dollar['change_20d_pct'] if dollar else None,
                evidence=evidence, context=_clip(context) if context else None,
                summary=None, errors=errors,
                details=dict(vix=vix, us10y=ten, dollar=dollar))


def _with_flow(block, uptrend_count):
    label = mood_label(block['base_mood'], (block.get('details') or {}).get('vix'), uptrend_count)
    block['regime_label'] = label
    pieces = [f"{label} ({' · '.join(block['evidence'])})"] if block['evidence'] else [label]
    if block['context']:
        pieces.append(f"이슈: {block['context']}")
    block['summary'] = ' · '.join(pieces)
    return block


def _unavailable_sentiment(reason):
    return dict(regime_label='확인 불가', base_mood='확인 불가', vix=None, vix_state=None,
                vix_change_pct=None, vix_phrase=None, us10y=None, us10y_change_pt=None,
                us10y_phrase=None, hyg_ief_trend=None, risk_mix=None, dollar_index=None,
                dollar_trend=None, dollar_change_20d_pct=None,
                evidence=[], context=None, summary='확인 불가 (지표 미수신)',
                errors=[reason], details=dict(vix=None, us10y=None, dollar=None))


def changes(groups, previous=None):
    """직전 스캔의 상승 흐름과 비교한다. 직전 스캔이 없으면 같은 일봉의 전일 대비를 쓴다."""
    now = [row['label'] for row in groups if row['state'] == 'uptrend']
    if previous and isinstance(previous, dict) and previous.get('groups'):
        before = [row.get('label') for row in previous['groups'] if row.get('state') == 'uptrend']
        basis, observed = 'previous_scan', previous.get('observed_at')
    else:
        before = [row['label'] for row in groups if row.get('previous_state') == 'uptrend']
        basis, observed = 'previous_bar', None
    before = [label for label in before if label]
    return dict(basis=basis, previous_observed_at=observed,
                newly_entered=[label for label in now if label not in before],
                exited=[label for label in before if label not in now])


def _download(symbols, period='1y', attempts=2):
    import yfinance as yf
    from provider_retry import isolate_yfinance_cache
    isolate_yfinance_cache()
    last = None
    for attempt in range(attempts):
        try:
            frame = yf.download(list(symbols), period=period, interval='1d', auto_adjust=True,
                                progress=False, threads=False, timeout=20)
            if frame is not None and not frame.empty:
                return frame
            last = ValueError('empty download')
        except Exception as exc:  # 제공자 오류는 여러 타입으로 온다
            last = exc
        if attempt + 1 < attempts:
            time.sleep(2)
    raise RuntimeError(f'asset regime download failed: {type(last).__name__}') from None


def _close_column(frame, symbol):
    columns = frame.columns
    if not hasattr(columns, 'nlevels'):
        return None
    if getattr(columns, 'nlevels', 1) == 1:
        return frame['Close'] if 'Close' in columns else None
    levels = [columns.get_level_values(index) for index in range(columns.nlevels)]
    if 'Close' in levels[0] and symbol in levels[1]:
        return frame['Close'][symbol]
    if symbol in levels[0] and 'Close' in levels[1]:
        return frame[symbol]['Close']
    return None


def _download_series(symbols, period='1y'):
    frame = _download(symbols, period=period)
    result = {}
    for symbol in symbols:
        try:
            close = _close_column(frame, symbol)
            if close is None:
                continue
            close = close.dropna()
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        result[symbol] = dict(closes=[float(value) for value in close.tolist()],
                              dates=[str(day)[:10] for day in close.index])
    if not result:
        raise RuntimeError('asset regime download had no close prices')
    return result


def _safe_series(symbols, downloader=None):
    downloader = downloader or _download_series
    try:
        data = downloader(list(dict.fromkeys(symbols)))
    except Exception as exc:
        return {}, [f'series_unavailable:{type(exc).__name__}']
    if not isinstance(data, dict):
        return {}, ['series_unavailable:invalid_result']
    errors = [f'series_missing:{symbol}' for symbol in dict.fromkeys(symbols) if symbol not in data]
    return data, errors


def _reference_date(groups):
    dates = Counter(row['data_date'] for group in groups for row in group['instruments']
                    if row.get('data_date'))
    return dates.most_common(1)[0][0] if dates else None


def _previous_date(groups):
    dates = Counter(row['previous_date'] for group in groups for row in group['instruments']
                    if row.get('previous_date'))
    return dates.most_common(1)[0][0] if dates else None


def assess(now=None, previous=None, context=None, downloader=None):
    """지금 상승 흐름·시장 분위기·흐름 변화를 한 블록으로 만든다."""
    now = now or datetime.now(timezone.utc)
    symbols = [symbol for spec in GROUPS for symbol, _label in spec['instruments']]
    symbols.extend(SENTIMENT_SYMBOLS)
    series, errors = _safe_series(symbols, downloader)
    groups = [group_block(spec, series) for spec in GROUPS]
    uptrend = sorted([row for row in groups if row['state'] == 'uptrend'],
                     key=lambda row: (-(row.get('return_20d_pct') or 0), row['label']))
    rebound = [row['label'] for group in groups for row in group['instruments']
               if row['state'] == 'rebound']
    reference = _reference_date(groups)
    try:
        mood = _with_flow(sentiment(series, context=context), len(uptrend))
    except Exception as exc:  # 심리 계산 실패가 흐름 헤더를 없애지는 않는다
        mood = _unavailable_sentiment(f'sentiment_error:{type(exc).__name__}')
    block = dict(observed_at=now.isoformat(), basis='yfinance_daily_close',
                 window=dict(ma_fast=MA_FAST, ma_slow=MA_SLOW, return_days=RETURN_DAYS),
                 reference_date=reference,
                 groups=groups,
                 uptrend_groups=[dict(key=row['key'], label=row['label'],
                                      return_20d_pct=row['return_20d_pct']) for row in uptrend],
                 rebound_assets=rebound,
                 changes=changes(groups, previous),
                 sentiment=mood,
                 errors=errors)
    if not any(row['observed'] for row in groups):
        block['changes'] = dict(basis=block['changes']['basis'],
                                previous_observed_at=block['changes']['previous_observed_at'],
                                newly_entered=[], exited=[])
    block['changes']['reference_date'] = reference
    block['changes']['previous_date'] = _previous_date(groups)
    block['header'] = format_lines(block)
    return block


def _flow_line(block):
    uptrend = block.get('uptrend_groups') or []
    if not any(row.get('observed', 1) for row in block.get('groups') or []):
        return '❔ 지금 상승 흐름: 확인 불가 (자산 시세 미수신)'
    if uptrend:
        shown = uptrend[:MAX_FLOW_ITEMS]
        parts = [f"{row['label']}({row['return_20d_pct']:+.1f}%)"
                 if row.get('return_20d_pct') is not None else row['label'] for row in shown]
        extra = len(uptrend) - len(shown)
        line = '📈 지금 상승 흐름: ' + ', '.join(parts)
        return line + (f' 외 {extra}개' if extra > 0 else '')
    line = '📉 전 자산 조정 구간 (현금·단기채 유리'
    rebound = block.get('rebound_assets') or []
    if rebound:
        line += ' · 단기 반등: ' + ', '.join(rebound[:MAX_FLOW_ITEMS])
    return line + ')'


def _sentiment_line(block):
    mood = block.get('sentiment') or {}
    return '🌡️ 시장 분위기: ' + str(mood.get('summary') or '확인 불가')


def _change_line(block):
    change = block.get('changes') or {}
    entered, exited = change.get('newly_entered') or [], change.get('exited') or []
    if not entered and not exited:
        return None
    halves = []
    if entered:
        halves.append(', '.join(entered) + ' 상승 진입')
    if exited:
        halves.append(', '.join(exited) + ' 상승 이탈')
    line = '⚡ 흐름 변화: ' + ' / '.join(halves)
    if change.get('basis') == 'previous_bar' and change.get('previous_date'):
        line += f" (직전 거래일 {change['previous_date'][5:].replace('-', '/')} 종가 대비)"
    return line


def format_lines(block):
    """텔레그램 최상단 세 줄(변화가 없으면 두 줄)."""
    if not isinstance(block, dict):
        return []
    lines = [_flow_line(block), _sentiment_line(block)]
    change_line = _change_line(block)
    if change_line:
        lines.append(change_line)
    return lines


def header_lines(block):
    """저장된 블록을 그대로 쓰되, 없으면 아무 줄도 넣지 않는다(알림을 막지 않는다)."""
    if not isinstance(block, dict):
        return []
    return list(block.get('header') or format_lines(block))


def load_previous(paths=PREVIOUS_PATHS):
    """직전 스캔이 저장해 둔 블록. 없거나 깨졌으면 None."""
    for path in paths:
        try:
            if not Path(path).is_file():
                continue
            data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        block = data.get('asset_regime') if isinstance(data, dict) else None
        if isinstance(block, dict) and block.get('groups'):
            return block
    return None


def save_state(block, path=STATE_PATH):
    """다음 실행이 '직전 스캔'으로 읽을 상태. 실패해도 알림을 막지 않는다."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(block, ensure_ascii=False), encoding='utf-8')
        return target
    except (OSError, TypeError, ValueError):
        return None


def headlines_from_report(report):
    """당일 테마 블록의 이슈 한 줄을 분위기 맥락으로 쓴다. 없으면 None."""
    snapshots = (report or {}).get('theme_sectors') or []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        for row in snapshot.get('rows') or []:
            issue = row.get('issue') if isinstance(row, dict) else None
            if issue and '뉴스 미확인' not in issue:
                return _clip(issue)
    return None


def attach(report, now=None, previous=None, context=None, downloader=None, save=True):
    """리포트에 `asset_regime` 블록을 붙인다(텔레그램 최상단·JSON 공통)."""
    if previous is None:
        previous = report.get('asset_regime') if isinstance(report, dict) else None
    if previous is None:
        previous = load_previous()
    if context is None and isinstance(report, dict):
        context = headlines_from_report(report)
    block = assess(now=now, previous=previous, context=context, downloader=downloader)
    if isinstance(report, dict):
        report['asset_regime'] = block
    if save:
        save_state(block)
    return block


_CACHE = {'block': None, 'at': None}


def cached_block(now=None, max_age_seconds=1800):
    """한 실행 안에서 여러 알림이 같은 조회를 다시 하지 않도록 캐시한다."""
    now = now or datetime.now(timezone.utc)
    stamp = _CACHE.get('at')
    if _CACHE.get('block') is not None and stamp is not None:
        if (now - stamp).total_seconds() <= max_age_seconds:
            return _CACHE['block']
    try:
        block = assess(now=now, previous=load_previous())
    except Exception as exc:  # 어떤 경우에도 알림 본문은 나가야 한다
        block = dict(observed_at=now.isoformat(), groups=[], uptrend_groups=[],
                     rebound_assets=[], changes=dict(basis='none', newly_entered=[], exited=[]),
                     sentiment=_unavailable_sentiment(f'assess_error:{type(exc).__name__}'),
                     errors=[f'assess_error:{type(exc).__name__}'])
        block['header'] = format_lines(block)
    _CACHE.update(block=block, at=now)
    return block
