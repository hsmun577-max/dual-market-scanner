"""A notification is not a trade-ready signal until its execution contract is valid."""
from datetime import datetime,timedelta
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import asset_regime
from entry_policy import reward_check, score_floor
from catalyst_track import entry_gate
from exit_policy import classify_asset, exit_policy_for
from rebound_track import entry_state as rebound_entry_state
from early_entry_track import entry_state as early_entry_state

def krx_tick(price, *, fund=False):
    if fund:
        return 1 if price < 2000 else 5
    for upper,tick in [(2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500)]:
        if price<upper:return tick
    return 1000

def floor_order_price(price, *, overseas=False, fund=False):
    """Highest permitted order price at or below the strategy's raw cap."""
    value = Decimal(str(price))
    tick = Decimal('0.01') if overseas else Decimal(krx_tick(value, fund=fund))
    return float((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)


def round_trigger(price):
    tick=Decimal(krx_tick(float(price)))
    return float((price/tick).to_integral_value(rounding=ROUND_CEILING)*tick)

def stamp(value):
    try:
        at=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return at if at.utcoffset() is not None else None
    except (ValueError,TypeError):return None

def number(value):
    if isinstance(value,bool):return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None

def support_stop_reason(bid, stop, support):
    support = number(support)
    if support is None or support <= 0:
        return 'pullback_support_unverified'
    if bid <= support:
        return 'pullback_support_lost'
    if stop >= support:
        return 'fixed_stop_above_support'
    return None


def live_support_rejection(quote, row, entry_cap, bid):
    # Current execution quotes always include the field, even on a data gap.
    # Archived quotes can still be evaluated under their historical contract.
    if 'pullback_support' not in quote:
        return None
    policy = exit_policy_for(row)
    stop = round_trigger(Decimal(str(entry_cap)) * (1 - Decimal(str(policy.stop_pct))))
    reason = support_stop_reason(bid, stop, quote['pullback_support'])
    return dict(reason=reason, stop_price=stop,
                pullback_support=quote['pullback_support']) if reason else None


def rebound_entry(plan,quote,at,result,seen,market='KR'):
    """A rebound plan is priced from the observed open, never from the prior close.

    The preopen plan carries a rule (open -2% for R2, -3% for R3) and the prior close its
    1% gap-cancel is measured against; here the session's own open turns the rule into a
    limit. An ask at or under that limit means the dip is here. A gap down of 1% or more
    cancels the entry instead of buying a name the morning is selling, and a price already
    well below the limit means the plan's premise is gone and needs a new analysis.
    """
    def reject(reason):return dict(result,reason=reason)
    state=rebound_entry_state(plan.get('strategy'),plan.get('gap_reference_price'),
                              (quote or {}).get('day_open'),market)
    if state['status']=='reference_price_missing':return reject('rebound_reference_price_missing')
    if state['status']=='awaiting_open':return reject('rebound_open_unverified')
    if state['status']=='gap_down_cancelled':
        return dict(reject('rebound_gap_down_cancelled'),gap_pct=state['gap_pct'],
            detail=f"시가 {number(quote.get('day_open')):,.0f} · 전일 종가 대비 {state['gap_pct']:+.1%} · 갭하락 진입 취소")
    limit=state['entry_limit'];ask=number(quote.get('ask'));bid=number(quote.get('bid'))
    if ask is None or bid is None or not 0<bid<=ask:return reject('invalid_orderbook')
    ask_size=number(quote.get('ask_size'));bid_size=number(quote.get('bid_size'))
    if ask_size is None or bid_size is None or min(ask_size,bid_size)<1:
        return reject('empty_or_unverified_orderbook_size')
    if (ask-bid)/ask>.005:return reject('spread_too_wide')
    if limit is None or limit<=0:return reject('entry_limit_missing')
    if ask>limit:return reject('waiting_for_entry_price')
    if ask<limit*.98:return reject('price_moved_requires_reanalysis')
    return dict(result,trade_ready=True,reason='entry_conditions_met',track='rebound',
        strategy=plan.get('strategy'),label=plan.get('label'),
        hold_sessions=plan.get('hold_sessions'),stop_policy=plan.get('stop_policy'),
        reference_price=plan.get('reference_price'),reference_date=plan.get('reference_date'),
        indicative_entry=ask,entry_limit=limit,entry_floor=None,bid=bid,
        day_open=number(quote.get('day_open')),gap_pct=state['gap_pct'],
        stop_price=None,take_profit_price=None,quote_at=seen.isoformat(),
        valid_until=(at+timedelta(seconds=30)).isoformat(),generated_at=at.isoformat(),
        filled=False)


def early_entry(plan,quote,at,result,seen,market='KR',score=None,requirements=None):
    """An early-entry plan is priced from the observed open less 1%, never from a close.

    The study's edge came from the resting limit (-1% under the open, filled 83.4%): a
    quote at or under it means the dip is there, and a quote far below it means the premise
    of the plan is gone. Nothing is chased — an unfilled limit is left unfilled.
    """
    def reject(reason):return dict(result,reason=reason)
    state=early_entry_state((quote or {}).get('day_open'),market)
    if state['status']=='awaiting_open':return reject('early_entry_open_unverified')
    limit=state['entry_limit'];ask=number(quote.get('ask'));bid=number(quote.get('bid'))
    if limit is None or limit<=0:return reject('entry_limit_missing')
    if ask>limit:return reject('waiting_for_entry_price')
    if ask<limit*.98:return reject('price_moved_requires_reanalysis')
    instrument = {'asset_type': plan.get('asset_class'), 'name': result.get('name')}
    blocked = live_support_rejection(quote, instrument, limit, bid)
    if blocked:
        return dict(result, **blocked)
    policy = exit_policy_for(instrument)
    return dict(result,trade_ready=True,reason='entry_conditions_met',track='early_entry',
        strategy=plan.get('strategy'),label=plan.get('label'),score=score,
        hold_sessions=plan.get('hold_sessions'),entry_requirements=requirements,
        reference_price=number(plan.get('reference_price')),reference_date=plan.get('reference_date'),
        indicative_entry=ask,entry_limit=limit,entry_floor=None,bid=bid,
        day_open=number(quote.get('day_open')),entry_offset_pct=plan.get('entry_offset_pct'),
        stop_price=round_trigger(Decimal(str(ask))*Decimal(str(1-policy.stop_pct))),
        take_profit_price=round_trigger(Decimal(str(ask))*Decimal(str(1+policy.take_profit_pct))),
        pullback_support=quote.get('pullback_support'),
        stop_pct=policy.stop_pct,take_profit_pct=policy.take_profit_pct,
        asset_class=plan.get('asset_class'),take_profit_mode=policy.take_profit_mode,
        exit_policy=policy.snapshot(),quote_at=seen.isoformat(),
        valid_until=(at+timedelta(seconds=30)).isoformat(),generated_at=at.isoformat(),
        filled=False)


def analysis_ready(forecast):
    """Whether this forecast's evidence may be executed, or why it may not.

    The score is withheld only by an integrity gap (``company_forecast.BLOCKING_GAPS``): a
    missing statement, a period that is not the latest filing, evidence stamped in the
    future. A supporting dimension that was not observed — valuation multiples, sector
    flow, an unread article body — is a caveat that the score already carries a deduction
    for, and it used to be enough on its own to refuse an entry that the very same run had
    planned. On 2026-11-25 that blocked 7 of the 10 Korean conditional plans
    ("assessment_status: partial") forever, while their scores were published in the alert.
    """
    forecast = forecast or {}
    if forecast.get('composite_score') is None:
        return False
    return not forecast.get('score_withheld_reasons')


def validate_entry(row,quote,at,session_open):
    """Validate a conditional plan at a fresh executable ask; never assume a fill."""
    result=dict(symbol=row.get('symbol'),name=row.get('name'),trade_ready=False)
    def reject(reason):return dict(result,reason=reason)
    if at.utcoffset() is None:raise ValueError('Aware decision time required')
    if not session_open:return reject('market_closed')
    plan=row.get('preopen_signal',{});forecast=row.get('company_forecast',{})
    if plan.get('status')!='conditional_buy':return reject('no_conditional_buy_plan')
    if plan.get('expires_session')!=at.date().isoformat():return reject('wrong_plan_session')
    if not analysis_ready(forecast):
        return reject('analysis_incomplete')
    seen=stamp((quote or {}).get('observed_at'));trade=stamp((quote or {}).get('trade_at'))
    if seen is None or not at-timedelta(seconds=10)<=seen<=at:return reject('quote_unverified_or_stale')
    if trade is None or trade.date()!=at.date() or not at-timedelta(minutes=2)<=trade<=at:
        return reject('last_trade_unverified_or_stale')
    if plan.get('track')=='rebound':
        return rebound_entry(plan,quote,at,result,seen,row.get('market') or 'KR')
    score=number(forecast.get('composite_score'));created=stamp(forecast.get('generated_at'))
    strategy=plan.get('strategy') or ('early_ma_alignment' if plan.get('track')=='early_entry' else 'trend')
    required=score_floor(row, strategy)
    if score is None or score<required:return reject('score_below_policy')
    if created is None or not at-timedelta(minutes=30)<=created<=at:return reject('stale_analysis')
    ask=number(quote.get('ask'));bid=number(quote.get('bid'))
    if ask is None or bid is None or not 0<bid<=ask:return reject('invalid_orderbook')
    ask_size=number(quote.get('ask_size'));bid_size=number(quote.get('bid_size'))
    if ask_size is None or bid_size is None or min(ask_size,bid_size)<1:return reject('empty_or_unverified_orderbook_size')
    if (ask-bid)/ask>.005:return reject('spread_too_wide')
    if plan.get('track')=='early_entry':
        # Priced from this session's own open, so it sits before the previous-close limit.
        reward = reward_check(row, ask, forecast.get('base_price'), bid)
        if not reward['allowed']:return reject('insufficient_expected_reward')
        return early_entry(plan,quote,at,result,seen,row.get('market') or 'KR',score,
                           dict(min_score=required, **reward))
    limit=number(plan.get('entry_limit'));floor=number(plan.get('entry_floor'))
    if limit is None or limit<=0:return reject('entry_limit_missing')
    limit = floor_order_price(limit, overseas=row.get('market') == 'US',
                              fund=classify_asset(row)[0] == 'fund')
    if limit<=0:return reject('entry_limit_missing')
    if ask>limit:return reject('waiting_for_entry_price')
    if floor is not None and ask<floor:return reject('below_entry_band')
    if floor is None and ask<limit*.98:return reject('price_moved_requires_reanalysis')
    base=number(forecast.get('base_price'));bull=number(forecast.get('bull_price'))
    if base is None or bull is None or base<=ask or bull<base:return reject('invalid_price_targets')
    # The stop follows the instrument the row is: −2% on a fund, −3% on a single stock.
    policy=exit_policy_for(row)
    if plan.get('strategy') in ('event_catalyst','earnings_catalyst'):
        verified=(forecast.get('catalyst_context',{}).get('eligible') is True
                  if plan.get('strategy')=='event_catalyst'
                  else (plan.get('catalyst') or {}).get('eligible') is True)
        if not verified:return reject('catalyst_not_verified')
        if floor is None:return reject('entry_band_missing')
        # 촉매 계획도 매도 규칙의 +6%를 남겨야 한다(1.06 하드코딩을 정책 상수로).
        if bull/ask-1 < policy.take_profit_pct:return reject('insufficient_catalyst_upside_room')
        if plan.get('strategy')=='earnings_catalyst':
            # A disclosed catalyst is not an entry. If the stock trades under its open or
            # same-time volume has not expanded, the market is selling the news.
            tone=entry_gate(quote.get('last_price',ask),quote.get('day_open'),quote.get('volume_ratio'))
            if not tone['allowed']:
                return dict(reject(tone['reason']),detail=tone['detail'],
                    session_price=quote.get('last_price'),day_open=quote.get('day_open'),
                    volume_ratio=quote.get('volume_ratio'),catalyst=plan.get('catalyst'))
    reward = reward_check(row, ask, base, bid)
    if not reward['allowed']:return reject('insufficient_expected_reward')
    blocked = live_support_rejection(quote, row, limit, bid)
    if blocked:
        return dict(result, **blocked)
    return dict(result,trade_ready=True,reason='entry_conditions_met',score=score,
        entry_requirements=dict(min_score=required, **reward),
        reference_price=plan.get('reference_price'),reference_date=plan.get('reference_date'),
        indicative_entry=ask,entry_limit=limit,entry_floor=floor,bid=bid,
        stop_price=round_trigger(Decimal(str(ask))*Decimal(str(1-policy.stop_pct))),
        take_profit_price=round_trigger(Decimal(str(ask))*Decimal(str(1+policy.take_profit_pct))),base_target=base,bull_target=bull,
        pullback_support=quote.get('pullback_support'),
        stop_pct=policy.stop_pct,take_profit_pct=policy.take_profit_pct,
        asset_class=classify_asset(row)[0],
        take_profit_mode=policy.take_profit_mode,exit_policy=policy.snapshot(),
        catalyst=plan.get('catalyst') if plan.get('strategy')=='earnings_catalyst' else None,
        market_regime_lifted=plan.get('market_regime_lifted') is True,
        live_quote=row.get('live_quote'),
        quote_at=seen.isoformat(),valid_until=(at+timedelta(seconds=30)).isoformat(),
        generated_at=at.isoformat(),filled=False)

HOLD_REASON_LABELS = {
    'market_closed': '장 마감 · 다음 거래일 시가 확인',
    'pullback_support_unverified': '눌림 지지와 회복 확인 대기',
    'pullback_support_lost': '눌림 지지 이탈 · 새 지지 확인 대기',
    'fixed_stop_above_support': '손절선이 눌림 지지와 같거나 위 · 더 낮은 진입 또는 새 지지 대기',
    'wrong_plan_session': '계획 세션 아님 · 다음 세션 계획',
    'waiting_for_entry_price': '현재 호가가 매수 상한 초과',
    'price_moved_requires_reanalysis': '가격이 계획 밖으로 이동 · 재분석 필요',
    'rebound_open_unverified': '시가 확인 전 · 시가 −2~3% 지정가 대기',
    'early_entry_open_unverified': '시가 확인 전 · 시가 −1% 지정가 대기',
    'quote_unverified_or_stale': '호가 신선도 미확인',
    'analysis_incomplete': '분석 자료 부족',
    'stale_analysis': '분석이 오래됨',
    'score_below_policy': '종합점수 미달',
    'insufficient_expected_reward': '비용 차감 후 손익비 미달',
}


# The plan's own label, or the Korean name of its strategy: a raw 'earnings_catalyst' in
# the message tells the reader nothing about why the name is there.
TRACK_LABELS = {
    'earnings_catalyst': '실적·호재 촉매', 'event_catalyst': '호재형',
    'early_ma_alignment': '단기 상승 초입', 'bottom_rebound': 'R2 바닥반등',
    'oversold_rebound': 'R3 과매도반등', 'trend': '정배열 추세',
}


def plan_line(plan):
    """One compact entry for a conditional plan that could not be confirmed yet.

    The short form the owner asked for on 2026-11-25: the name, what the news was, and
    the price band with the target — no per-name sell-rule paragraph and no repeated
    '매수 신호 아님' sentence, which the block's heading already says once. The target is
    the sell rule's +6% (5 sessions), never a forecast upside scenario: the 2026-11-26
    alert printed `목표 1,045,793원 (+38.5%)` for a five-session plan.
    """
    code = str(plan.get('symbol') or '').split('.')[0]
    label = plan.get('label') or TRACK_LABELS.get(plan.get('strategy'), plan.get('strategy')) or '조건부'
    label = str(label).strip('[]')
    band = []
    floor, limit = plan.get('entry_floor'), plan.get('entry_limit')
    text = lambda value: f"{value:,.0f}원" if plan.get('market') != 'US' else f"${value:,.2f}"
    if isinstance(floor, (int, float)) and isinstance(limit, (int, float)) and floor < limit:
        band.append(f"{text(floor)}~{text(limit)}")
    elif isinstance(limit, (int, float)):
        band.append(f"상한 {text(limit)}")
    elif isinstance(plan.get('flat_open_entry_limit'), (int, float)):
        band.append(f"갭 없이 열리면 {text(plan['flat_open_entry_limit'])}")
    score = plan.get('composite_score')
    head = f"• {plan.get('name')} ({code}) · [{label}]"
    if isinstance(score, (int, float)):
        head += f" · 종합 {score:.1f}"
    lines = [head]
    if plan.get('catalyst_summary'):
        lines.append(f"  호재: {plan['catalyst_summary']}")
    target = plan.get('bull_target') or plan.get('target_price')
    price_line = []
    if band:
        price_line.append(f"매수 {band[0]}")
    take_profit = plan.get('take_profit_pct')
    if isinstance(take_profit, (int, float)) and not isinstance(take_profit, bool):
        price_line.append(f"목표 +{take_profit:.0%}(5거래일)")
        if isinstance(target, (int, float)):
            price_line.append(f"강세 참고 {text(target)}")
    elif isinstance(target, (int, float)):
        price_line.append(f"참고 {text(target)}")
    stop = plan.get('stop_pct')
    if isinstance(stop, (int, float)):
        price_line.append(f"손절 −{stop:.0%}")
    if price_line:
        lines.append('  ' + ' · '.join(price_line))
    return "\n".join(lines)


def format_requested_report(sector_lines,signals,evaluation,at,regime=None,plans=None,
                            market_note=None,plan_total=None):
    ready=[row for row in signals if row.get('trade_ready') is True]
    # 알림 최상단: 지금 상승 흐름 · 시장 분위기 · (바뀌었으면) 흐름 변화.
    regime_lines=asset_regime.header_lines(regime)
    lines=[*([*regime_lines,''] if regime_lines else []),*sector_lines,*([*market_note] if market_note else []),'']
    if ready:
        lines.append(f'매수 신호 · {at:%m/%d %H:%M:%S} KST')
        for row in ready[:5]:
            code = str(row.get('symbol','')).split('.')[0]
            target = row.get('bull_target') or row.get('base_target')
            try:
                until = datetime.fromisoformat(row['valid_until']).strftime('%H:%M:%S')
            except (KeyError, TypeError, ValueError):
                until = None
            if row.get('track')=='early_entry':
                # The early entry is a limit 1% under the session's own open.
                lines.extend(['',f"• {row['name']} ({code}) · 단기 상승 초입 · 종합 {row.get('score', 0):.1f}",
                    f"  시가 −1% 지정가 {row['entry_limit']:,.0f}원 (시가 {row['day_open']:,.0f}원) · 추격 없음",
                    f"  목표 +{row['take_profit_pct']:.0%} · 손절 −{row['stop_pct']:.0%} · 5거래일 보유"
                    + (f" · {until}까지" if until else "")])
                continue
            if row.get('track')=='rebound':
                # A rebound entry has no +6% target and no -2% stop on purpose.
                lines.extend(['',f"• {row['name']} ({code}) · {row.get('label')} · 5일 보유",
                    f"  눌림 지정가 {row['entry_limit']:,.0f}원 (시가 {row['day_open']:,.0f}원)"
                    + (f" · {until}까지" if until else "")])
                continue
            evidence=row.get('catalyst') or {}
            lines.extend(['',f"• {row['name']} ({code}) · 종합 {row['score']:.1f}",
                *([f"  호재: {evidence['summary']}"] if evidence.get('summary') else []),
                f"  매수 {row['entry_limit']:,.0f}원 · 목표 {target:,.0f}원 · 손절 −{row['stop_pct']:.0%}"
                + (f" · {until}까지" if until else ""),
                *([f"  눌림 지지 {row['pullback_support']:,.0f}원 · 기준 손절가 {row['stop_price']:,.0f}원"]
                  if row.get('pullback_support') else [])])
        if len(ready) > 5:
            from telegram_text import rest_note
            lines.append(f"   {rest_note(len(ready), 5)} (분석자료 참고)")
        lines.append('')
        lines.append('※ 익절 +6%, 급등 시 트레일링 보유 · 실제 체결가 기준')
    elif any(row.get('reason')=='waiting_for_entry_price' for row in signals):
        lines.extend([f'매수 대기 · {at:%m/%d %H:%M} KST','분석 조건 충족 · 현재 호가가 매수 상한 초과'])
    elif evaluation.get('decision_completed',0)>0 and not signals:
        lines.extend([f'분석 결과 · {at:%m/%d %H:%M} KST',
            f"판정 완료 {evaluation['decision_completed']}종목 · 매수 조건 충족 없음"])
        if evaluation.get('data_excluded'):
            lines.append(f"자료 부족 {evaluation['data_excluded']}종목 제외")
    elif evaluation.get('completed') is False:
        lines.extend([f'분석 상태 · {at:%m/%d %H:%M} KST','매수 판정 보류 · 분석자료 미확인'])
    elif signals:
        # No entry is confirmed right now (a closed session, a quote above the limit), but
        # the analysis did produce plans. They are printed as plans, never as a signal.
        lines.extend([f'조건부 계획 · {at:%m/%d %H:%M} KST',
                      '지금 체결 기준 미확인 · 매수 신호 아님'])
        shown = [plan for plan in (plans or [])][:5]
        lines.extend(['', *[plan_line(plan) for plan in shown]] if shown else
                     ['실시간 호가·가격 조건 미확인 또는 미충족'])
        if len(plans or []) > 5:
            from telegram_text import rest_note
            lines.append(f"   {rest_note(len(plans or []), 5)} (분석자료 참고)")
        elif isinstance(plan_total, int) and plan_total > len(shown):
            from telegram_text import rest_note
            lines.append(f"   {rest_note(plan_total, len(shown))} (분석자료 참고)")
    else:
        lines.extend([f'분석 결과 · {at:%m/%d %H:%M} KST','매수 조건 충족 종목 없음'])
    return '\n'.join(lines)
