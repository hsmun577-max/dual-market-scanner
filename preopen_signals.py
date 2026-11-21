"""Preopen recommendations are conditional plans, never a live execution confirmation."""
from datetime import datetime,timedelta,timezone
from collections import Counter
import math
from candidate_selection import liquidity_rejection_reason,candidate_rejection_reason
from advance_policy import advance_context,eligible_for_advance_research
from company_forecast import MIN_BASE_REWARD_PCT, MIN_COMPOSITE_SCORE
from exit_policy import DEFAULT_EXIT_POLICY,policy_snapshot

INCOMPLETE_REASONS={'analysis_stale_or_missing','analysis_incomplete','price_session_mismatch',
                    'missing_indicators','invalid_average_price','entry_tick_missing'}


def session_dates(report,at=None):
    """Last completed session per market, taken from the rows' own price data.

    The daily scan keeps the most recent completed bar, so the plan's reference date is
    that bar's date. It is still checked against the exchange calendar: a row carrying an
    old or invented date must not silently become the reference the entry limit is based
    on, and a market whose rows disagree is reported by the session check below.
    """
    at=at or datetime.now(timezone.utc)
    from exchange_calendars import get_calendar
    calendars={'KR':get_calendar('XKRX'),'US':get_calendar('XNYS')}
    dates={}
    for market,calendar in calendars.items():
        seen=Counter(row.get('indicators',{}).get('data_date') for row in report.get('results',[])
            if row.get('market')==market and 'company_analysis' in row)
        seen.pop(None,None)
        for day,_count in seen.most_common():
            try:
                if not calendar.is_session(day):continue
                close=calendar.session_close(day).to_pydatetime()
                if close.utcoffset() is None:continue
                if close>at:continue
            except (ValueError,TypeError):continue
            dates[market]=day
            break
    return dates


def next_session(day,market):
    """The session a plan built from ``day``'s close applies to."""
    from exchange_calendars import get_calendar
    try:
        return get_calendar('XKRX' if market=='KR' else 'XNYS').next_session(day).date().isoformat()
    except (KeyError,ValueError):return None


def _expected_session(previous_session,market):
    if isinstance(previous_session,dict):return previous_session.get(market)
    return previous_session


def evaluate_preopen(report, previous_session, at=None, market=None):
    """Score the analysed candidates into conditional plans.

    ``previous_session`` is either one date or a mapping of market to date, because the
    Korean and US scans close on different local days. ``market`` restricts the pass to
    one market so a multi-market report can merge the two without double counting.
    """
    at=at or datetime.now(timezone.utc)
    if at.utcoffset() is None: raise ValueError('Timezone-aware decision time required')
    counts=Counter(); missing=Counter(); caveats=Counter(); recommendations=[]; advance=[]
    for row in report.get('results',[]):
        if market is not None and row.get('market')!=market: continue
        row.pop('preopen_signal',None)
        row.pop('advance_signal',None)
        if 'company_analysis' not in row:
            continue
        company=row['company_analysis']; forecast=row.get('company_forecast',{})
        status=None
        try:
            generated=datetime.fromisoformat(forecast.get('generated_at',''))
            fresh=generated.utcoffset() is not None and at-timedelta(minutes=30)<=generated<=at
        except (ValueError,TypeError): fresh=False
        score=forecast.get('composite_score')
        if not fresh: status='analysis_stale_or_missing'
        elif not isinstance(score,(int,float)) or not math.isfinite(score):
            # Only an integrity gap withholds the score (see company_forecast.BLOCKING_GAPS).
            status='analysis_incomplete'
            missing.update(forecast.get('missing_dimensions',[]))
        elif row.get('indicators',{}).get('data_date')!=_expected_session(previous_session,row.get('market')):
            status='price_session_mismatch'
        if status:
            counts[status]+=1
            row['preopen_decision']=status
            continue
        caveats.update(forecast.get('caveats',[]))
        reason=liquidity_rejection_reason(row,row['market'])
        values=row['indicators']; price=values['price']
        early=advance_context(company,values,at)
        if early['eligible'] and score>=55 and eligible_for_advance_research(row,row['market']):
            row['advance_signal']=dict(status='advance_candidate',confirmed=False,**early)
            advance.append(row['symbol'])
        graph=company.get('event_analysis',{}).get('economic_graph',{})
        event_support=any(link.get('direction',0)>0 for link in graph.get('links',[])) or any(
            link.get('watch_bias')=='positive_policy_sympathy' and link.get('source_url')
            for link in graph.get('expectation_links',[]))
        trend_reason=candidate_rejection_reason(row,row['market'])
        catalyst=forecast.get('catalyst_context',{}).get('eligible') is True
        entry_limit=price; entry_floor=None; strategy='trend'
        if reason: status=reason
        elif values['ma20']<=0: status='invalid_average_price'
        elif values.get('breakdown'): status='breakdown'
        elif not catalyst and (values.get('overheated') or values.get('return_5d',0)>.12 or price/values['ma20']>1.08):
            status='extended_price'
        elif trend_reason and not event_support: status=trend_reason
        elif score<MIN_COMPOSITE_SCORE: status='composite_score_below_policy'
        elif catalyst:
            tick=company.get('price_tick',.01 if row['market']=='US' else None)
            upper=forecast.get('bull_price')
            if not isinstance(tick,(int,float)) or not math.isfinite(tick) or tick<=0:
                status='entry_tick_missing'
            elif not isinstance(upper,(int,float)) or not math.isfinite(upper) or forecast['base_price']<=price:
                status='catalyst_outlook_not_positive'
            else:
                # Prior-close limit: identify the idea early without authorizing a gap-up chase.
                raw=min(price,upper/1.06)
                entry_limit=round(math.floor(raw/tick)*tick,2)
                if entry_limit<price:
                    status='insufficient_catalyst_upside_room'
                else:
                    status='conditional_buy'
                    entry_floor=max(tick,round(math.floor(price*(1-.5*values['atr14_pct']/100)/tick)*tick,2))
                    strategy='event_catalyst'
        elif not math.isfinite(forecast.get('base_price',float('nan'))) or forecast['base_price']/price-1<MIN_BASE_REWARD_PCT/100:
            status='insufficient_expected_reward'
        else:
            status='conditional_buy'
        if status=='conditional_buy':
            row['preopen_signal']=dict(status=status,confirmed=False,entry_limit=entry_limit,
                entry_floor=entry_floor,strategy=strategy,
                reference_price=price,reference_date=_expected_session(previous_session,row.get('market')),
                generated_at=at.isoformat(),composite_score=score,
                caveats=list(forecast.get('caveats',[])),
                entry_basis='source_dated_catalyst_pullback_band' if catalyst else 'previous_session_close_limit_not_current_market_quote',
                fresh_execution_quote_required=True,
                target_basis='upside_scenario_not_expected_return' if catalyst else 'base_scenario',
                expires_session=report.get('preopen_session'),stop_pct=DEFAULT_EXIT_POLICY.stop_pct,
                take_profit_pct=DEFAULT_EXIT_POLICY.take_profit_pct,
                take_profit_mode=DEFAULT_EXIT_POLICY.take_profit_mode,exit_policy=policy_snapshot())
            recommendations.append(row['symbol'])
        counts[status]+=1; row['preopen_decision']=status
    evaluated=sum(counts.values())
    excluded=sum(counts[s] for s in INCOMPLETE_REASONS)
    report['buy_evaluation']=dict(completed=bool(evaluated) and not excluded,
        decision_completed=evaluated-excluded, data_excluded=excluded,
        connected=True,mode='conditional_preopen',scope='analysed_candidates',
        previous_session=_expected_session(previous_session,market) if market else previous_session,
        generated_at=at.isoformat(),evaluated=evaluated,
        counts=dict(counts),missing_dimensions=dict(missing),caveats=dict(caveats),
        recommendations=recommendations,
        advance_candidates=advance,
        policy=dict(min_composite=MIN_COMPOSITE_SCORE,min_base_reward_pct=MIN_BASE_REWARD_PCT,stop_pct=2,take_profit_pct=6,
                    take_profit_mode=DEFAULT_EXIT_POLICY.take_profit_mode,exit_policy=policy_snapshot()),
        live_execution_confirmed=False)
    return report['buy_evaluation']


def _merge(parts):
    counts=Counter(); missing=Counter(); caveats=Counter(); recommendations=[]; advance=[]
    for part in parts.values():
        counts.update(part['counts']); missing.update(part['missing_dimensions'])
        caveats.update(part.get('caveats',{}))
        recommendations.extend(part['recommendations']); advance.extend(part['advance_candidates'])
    evaluated=sum(counts.values()); excluded=sum(counts[s] for s in INCOMPLETE_REASONS)
    policy=next(iter(parts.values()))['policy'] if parts else {}
    return dict(completed=bool(evaluated) and not excluded,
        decision_completed=evaluated-excluded,data_excluded=excluded,
        connected=True,mode='conditional_preopen',scope='analysed_candidates',
        previous_session={market:part['previous_session'] for market,part in parts.items()},
        markets=sorted(parts),generated_at=next(iter(parts.values()))['generated_at'] if parts else None,
        evaluated=evaluated,counts=dict(counts),missing_dimensions=dict(missing),
        caveats=dict(caveats),recommendations=recommendations,advance_candidates=advance,
        policy=policy,live_execution_confirmed=False)


def evaluate_preopen_markets(report, at=None):
    """Evaluate every market the report carries with its own reference session."""
    at=at or datetime.now(timezone.utc)
    sessions=session_dates(report,at)
    parts={}
    markets=[market for market in ('KR','US')
             if market in sessions and any(row.get('market')==market and 'company_analysis' in row
                                           for row in report.get('results',[]))]
    caller_session=report.get('preopen_session') if isinstance(report.get('preopen_session'),str) else None
    for market in markets:
        # ``preopen_session`` is the session the plan is for; the reference session the
        # entry limit comes from is passed separately, so a plan is never dated before it
        # was made.
        report['preopen_session']=caller_session or next_session(sessions[market],market) or sessions[market]
        parts[market]=evaluate_preopen(report,sessions[market],at,market=market)
    if not parts:
        report['buy_evaluation']=dict(completed=False,decision_completed=0,data_excluded=0,
            connected=True,mode='conditional_preopen',scope='analysed_candidates',
            previous_session={},markets=[],generated_at=at.isoformat(),evaluated=0,
            counts={},missing_dimensions={},caveats={},recommendations=[],advance_candidates=[],
            policy=dict(min_composite=MIN_COMPOSITE_SCORE,min_base_reward_pct=MIN_BASE_REWARD_PCT,
                        stop_pct=2,take_profit_pct=6,
                        take_profit_mode=DEFAULT_EXIT_POLICY.take_profit_mode,exit_policy=policy_snapshot()),
            live_execution_confirmed=False)
        return report['buy_evaluation'],{}
    merged=_merge(parts)
    report['buy_evaluation']=merged
    report['buy_evaluation_by_market']=parts
    report['preopen_sessions']=dict(sessions)
    # Keep the caller's single session when it set one (the preopen delivery does); the
    # per-market dates stay in ``preopen_sessions`` so a signal never expires on a dict.
    if not isinstance(report.get('preopen_session'),str):
        report['preopen_session']=sessions.get('KR') or sessions.get('US')
    return merged,parts
