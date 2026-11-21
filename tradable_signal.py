"""A notification is not a trade-ready signal until its execution contract is valid."""
from datetime import datetime,timedelta
import math
from decimal import Decimal,ROUND_CEILING
from company_forecast import MIN_BASE_REWARD_PCT, MIN_COMPOSITE_SCORE
from catalyst_track import MIN_CATALYST_COMPOSITE_SCORE, entry_gate
from exit_policy import DEFAULT_EXIT_POLICY

def krx_tick(price):
    for upper,tick in [(2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500)]:
        if price<upper:return tick
    return 1000

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

def validate_entry(row,quote,at,session_open):
    """Validate a conditional plan at a fresh executable ask; never assume a fill."""
    result=dict(symbol=row.get('symbol'),name=row.get('name'),trade_ready=False)
    def reject(reason):return dict(result,reason=reason)
    if at.utcoffset() is None:raise ValueError('Aware decision time required')
    if not session_open:return reject('market_closed')
    plan=row.get('preopen_signal',{});forecast=row.get('company_forecast',{})
    if plan.get('status')!='conditional_buy':return reject('no_conditional_buy_plan')
    if plan.get('expires_session')!=at.date().isoformat():return reject('wrong_plan_session')
    if forecast.get('assessment_status')!='complete' or forecast.get('missing_dimensions'):
        return reject('analysis_incomplete')
    score=number(forecast.get('composite_score'));created=stamp(forecast.get('generated_at'))
    # The catalyst track's own floor is lower than the momentum gate (its evidence is the
    # disclosure), so the execution check must read the same floor the plan was made on.
    required=(MIN_CATALYST_COMPOSITE_SCORE if plan.get('strategy')=='earnings_catalyst'
              else MIN_COMPOSITE_SCORE)
    if score is None or score<required:return reject('score_below_policy')
    if created is None or not at-timedelta(minutes=30)<=created<=at:return reject('stale_analysis')
    seen=stamp((quote or {}).get('observed_at'));trade=stamp((quote or {}).get('trade_at'))
    if seen is None or not at-timedelta(seconds=10)<=seen<=at:return reject('quote_unverified_or_stale')
    if trade is None or trade.date()!=at.date() or not at-timedelta(minutes=2)<=trade<=at:
        return reject('last_trade_unverified_or_stale')
    ask=number(quote.get('ask'));bid=number(quote.get('bid'))
    if ask is None or bid is None or not 0<bid<=ask:return reject('invalid_orderbook')
    ask_size=number(quote.get('ask_size'));bid_size=number(quote.get('bid_size'))
    if ask_size is None or bid_size is None or min(ask_size,bid_size)<1:return reject('empty_or_unverified_orderbook_size')
    if (ask-bid)/ask>.005:return reject('spread_too_wide')
    limit=number(plan.get('entry_limit'));floor=number(plan.get('entry_floor'))
    if limit is None or limit<=0:return reject('entry_limit_missing')
    if ask>limit:return reject('waiting_for_entry_price')
    if floor is not None and ask<floor:return reject('below_entry_band')
    if floor is None and ask<limit*.98:return reject('price_moved_requires_reanalysis')
    base=number(forecast.get('base_price'));bull=number(forecast.get('bull_price'))
    if base is None or bull is None or base<=ask or bull<base:return reject('invalid_price_targets')
    if plan.get('strategy') in ('event_catalyst','earnings_catalyst'):
        verified=(forecast.get('catalyst_context',{}).get('eligible') is True
                  if plan.get('strategy')=='event_catalyst'
                  else (plan.get('catalyst') or {}).get('eligible') is True)
        if not verified:return reject('catalyst_not_verified')
        if floor is None:return reject('entry_band_missing')
        if bull/ask-1<.06:return reject('insufficient_catalyst_upside_room')
        if plan.get('strategy')=='earnings_catalyst':
            # A disclosed catalyst is not an entry. If the stock trades under its open or
            # same-time volume has not expanded, the market is selling the news.
            tone=entry_gate(quote.get('last_price',ask),quote.get('day_open'),quote.get('volume_ratio'))
            if not tone['allowed']:
                return dict(reject(tone['reason']),detail=tone['detail'],
                    session_price=quote.get('last_price'),day_open=quote.get('day_open'),
                    volume_ratio=quote.get('volume_ratio'),catalyst=plan.get('catalyst'))
    elif base/ask-1<MIN_BASE_REWARD_PCT/100:return reject('insufficient_expected_reward')
    return dict(result,trade_ready=True,reason='entry_conditions_met',score=score,
        reference_price=plan.get('reference_price'),reference_date=plan.get('reference_date'),
        indicative_entry=ask,entry_limit=limit,entry_floor=floor,bid=bid,
        stop_price=round_trigger(Decimal(str(ask))*Decimal('.98')),
        take_profit_price=round_trigger(Decimal(str(ask))*Decimal(str(1+DEFAULT_EXIT_POLICY.take_profit_pct))),base_target=base,bull_target=bull,
        take_profit_mode=DEFAULT_EXIT_POLICY.take_profit_mode,exit_policy=DEFAULT_EXIT_POLICY.snapshot(),
        catalyst=plan.get('catalyst') if plan.get('strategy')=='earnings_catalyst' else None,
        quote_at=seen.isoformat(),valid_until=(at+timedelta(seconds=30)).isoformat(),
        generated_at=at.isoformat(),filled=False)

def format_requested_report(sector_lines,signals,evaluation,at):
    ready=[row for row in signals if row.get('trade_ready') is True]
    lines=[*sector_lines,'']
    if ready:
        lines.append(f'매수 신호 · {at:%m/%d %H:%M:%S} KST')
        for row in ready[:5]:
            evidence=row.get('catalyst') or {}
            track='[실적·호재 촉매] ' if evidence else ''
            lines.extend(['',f"• {row['name']} ({row['symbol'].split('.')[0]}) · {track}종합 {row['score']:.1f}/100",
                *([f"근거 {evidence['summary']}"] if evidence.get('summary') else []),
                f"진입 확인가 {row['indicative_entry']:,.0f}원 · 매수 상한 {row['entry_limit']:,.0f}원",
                f"{datetime.fromisoformat(row['valid_until']):%H:%M:%S}까지 가격 재확인 후 진입",
                f"분석 목표 {row['base_target']:,.0f}원 / 상승 시나리오 {row['bull_target']:,.0f}원",
                f"예상 손절 {row['stop_price']:,.0f}원 · 기본 익절 {row['take_profit_price']:,.0f}원",
                '손절 −2%·익절 +6%는 실제 체결가로 재계산 · 급등 시 6% 매도 대신 트레일링 보유 후 거래량 급감 시 청산'])
    elif any(row.get('reason')=='waiting_for_entry_price' for row in signals):
        lines.extend([f'매수 대기 · {at:%m/%d %H:%M} KST','분석 조건은 충족했으나 현재 호가가 매수 상한 초과'])
    elif evaluation.get('decision_completed',0)>0 and not signals:
        lines.extend([f'분석 결과 · {at:%m/%d %H:%M} KST',
            f"판정 완료 {evaluation['decision_completed']}종목 · 현재 매수 조건 충족 없음"])
        if evaluation.get('data_excluded'):
            lines.append(f"자료 부족 {evaluation['data_excluded']}종목은 매수 판정에서 제외")
    elif evaluation.get('completed') is False:
        lines.extend([f'분석 상태 · {at:%m/%d %H:%M} KST','매수 판정 보류 · 분석자료 미확인'])
    elif signals:
        lines.extend([f'진입 확인 보류 · {at:%m/%d %H:%M} KST','실시간 호가·가격 조건 미확인 또는 미충족'])
    else:
        lines.extend([f'분석 결과 · {at:%m/%d %H:%M} KST','현재 매수 조건을 충족한 종목 없음'])
    return '\n'.join(lines)
