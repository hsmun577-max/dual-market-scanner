"""Preopen recommendations are conditional plans, never a live execution confirmation."""
from datetime import datetime,timedelta,timezone
from collections import Counter
import math
from candidate_selection import (liquidity_rejection_reason,candidate_rejection_reason,
                                 market_headwind_reason,early_entry_block_reason,
                                 EARLY_ENTRY_LIFTABLE_REASONS)
import rebound_track
import early_entry_track
from advance_policy import advance_context,eligible_for_advance_research
from catalyst_track import MIN_CATALYST_COMPOSITE_SCORE,confirmed_catalyst,liftable_reason
from company_forecast import MIN_BASE_REWARD_PCT, MIN_COMPOSITE_SCORE
from exit_policy import (DEFAULT_EXIT_POLICY, FUND_STOP_PCT, STOCK_STOP_PCT, exit_policy_for,
                         policy_snapshot, stop_basis)

INCOMPLETE_REASONS={'analysis_stale_or_missing','analysis_incomplete','price_session_mismatch',
                    'missing_indicators','invalid_average_price','entry_tick_missing'}


def catalyst_plan(row, forecast, evidence, at, reference_date, expires_session, score, strategy):
    """A prior-close band entry for a confirmed catalyst: identify it early, never chase.

    The band is the previous session's close less half the 14-day ATR, so a gap-up past it
    is not bought; the upside case still has to leave the +6% policy room.
    """
    values=row['indicators']; price=values['price']; policy=exit_policy_for(row)
    for key in ('bull_price','base_price'):
        value=forecast.get(key)
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value):
            return 'catalyst_outlook_not_positive',None
    if forecast['base_price']<=price:
        return 'catalyst_outlook_not_positive',None
    tick=(row.get('company_analysis') or {}).get('price_tick',.01 if row.get('market')=='US' else None)
    if not isinstance(tick,(int,float)) or isinstance(tick,bool) or not math.isfinite(tick) or tick<=0:
        return 'entry_tick_missing',None
    entry_limit=round(math.floor(min(price,forecast['bull_price']/1.06)/tick)*tick,2)
    if entry_limit<price:
        return 'insufficient_catalyst_upside_room',None
    entry_floor=None
    atr=values.get('atr14_pct')
    if isinstance(atr,(int,float)) and not isinstance(atr,bool) and math.isfinite(atr) and atr>0:
        entry_floor=max(tick,round(math.floor(price*(1-.5*atr/100)/tick)*tick,2))
    plan=dict(status='conditional_buy',confirmed=False,entry_limit=entry_limit,
        entry_floor=entry_floor,strategy=strategy,
        reference_price=price,reference_date=reference_date,
        generated_at=at.isoformat(),composite_score=score,
        caveats=list(forecast.get('caveats',[])),
        entry_basis='source_dated_catalyst_pullback_band',fresh_execution_quote_required=True,
        target_basis='upside_scenario_not_expected_return',expires_session=expires_session,
        # The stop follows the instrument: −2% on a fund, −3% on a single stock.
        take_profit_pct=policy.take_profit_pct,
        take_profit_mode=policy.take_profit_mode,exit_policy=policy_snapshot(policy),
        **stop_basis(row),
        catalyst=dict(evidence))
    return 'conditional_buy',plan


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
    counts=Counter(); missing=Counter(); caveats=Counter(); recommendations=[]; advance=[]; catalyst_picks=[]; rebound_picks=[]; early_picks=[]
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
        # Read the regime on its own, not as the last item of the trend check: the trend
        # check reports the chart reason it finds first, so a name that is misaligned or
        # thin still has to be held back by the market it trades in.
        headwind=market_headwind_reason(values)
        catalyst=forecast.get('catalyst_context',{}).get('eligible') is True
        # A confirmed, quantified disclosure may override a soft chart rejection. The
        # safety blocks (liquidity, price, breakdown, extended, fund) are checked inside
        # ``liftable_reason`` and can never be lifted here.
        lift_reason=liftable_reason(row,row['market'])
        catalyst_evidence=confirmed_catalyst(company,at,row.get('market')) if lift_reason else {}
        ready_evidence=catalyst_evidence.get('eligible') is True
        entry_limit=price; entry_floor=None; strategy='trend'; plan=None
        reference_date=_expected_session(previous_session,row.get('market'))
        # The reversal tracks read their own candle and are never lifted by the momentum
        # gate; they are also refused by its safety blocks (liquidity, breakdown, an
        # overheated or extended price). They sit after the confirmed-catalyst plan (a
        # quantified disclosure is the stronger evidence) and before the regime branch, so
        # a downtrend cannot hold them back and a name the screen would refuse as
        # ``unconfirmed_uptrend`` can still be planned here.
        rebound=rebound_track.setup(values)
        # The short-term early-entry track reads the first sessions of an average alignment
        # (``unconfirmed_uptrend``/``weak_relative_trend`` by the momentum gate) and enters
        # on the next open less 1%. Its own safety list — liquidity, breakdown, an extended
        # price and the market regime — is read beside it, so "early" never means "unsafe".
        early=early_entry_track.setup(values)
        if reason: status=reason
        elif values['ma20']<=0: status='invalid_average_price'
        elif values.get('breakdown'): status='breakdown'
        elif not catalyst and (values.get('overheated') or values.get('return_5d',0)>.12 or price/values['ma20']>1.08):
            status='extended_price'
        elif lift_reason and ready_evidence and score<MIN_CATALYST_COMPOSITE_SCORE:
            status='catalyst_score_below_policy'
        elif lift_reason and ready_evidence:
            status,plan=catalyst_plan(row,forecast,catalyst_evidence,at,reference_date,
                report.get('preopen_session'),score,'earnings_catalyst')
            if plan: strategy='earnings_catalyst'
        elif rebound:
            status,plan=rebound_track.rebound_plan(rebound,values,reference_date,
                report.get('preopen_session'),at,row.get('market') or market or 'KR',
                next_open=rebound_track.observed_open(values),
                caveats=forecast.get('caveats',[]))
            if plan: strategy=plan['strategy']
        elif early and early_entry_block_reason(row,row['market']):
            # Not liftable by this track: the momentum gate's safety blocks and the market
            # regime stay exactly as they are for every other entry.
            status=early_entry_block_reason(row,row['market'])
        elif early and not early_entry_track.qualifier(row):
            # The chart alone carried no edge in the study; without a measured inflow or a
            # 61+ fundamental score there is no early entry to plan.
            status='early_entry_qualifier_missing'
        elif early and (not isinstance(score,(int,float)) or not math.isfinite(score)
                        or score<MIN_COMPOSITE_SCORE):
            status='composite_score_below_policy'
        elif early and (not isinstance(forecast.get('base_price'),(int,float))
                        or isinstance(forecast.get('base_price'),bool)
                        or not math.isfinite(forecast['base_price'])
                        or forecast['base_price']/price-1<MIN_BASE_REWARD_PCT/100):
            status='insufficient_expected_reward'
        elif early:
            status,plan=early_entry_track.entry_plan(row,forecast,reference_date,
                report.get('preopen_session'),at,row.get('market') or market or 'KR',
                next_open=early_entry_track.observed_open(values),
                qualifier_reason=early_entry_track.qualifier(row),
                caveats=forecast.get('caveats',[]))
            if plan: strategy=plan['strategy']
        elif headwind and (event_support or not trend_reason):
            # Event support may soften the chart (it can plan a name before the 60-day
            # average turns up), but it may not walk a plan through a market headwind. The
            # 2026-11-23 run sent NHN/LG유플러스/NC while the index was below its 20-day
            # average: their first rejection was a chart one, so the trend check never
            # returned ``market_headwind`` and the event-support branch below skipped the
            # regime gate. This branch sits before it, so no plan reaches it under a
            # headwind. A name that the chart alone already refuses keeps that reason, so
            # the rejection counts still say why it failed on its own. Only the
            # confirmed-disclosure track above may lift a headwind
            # (catalyst_track.LIFTABLE_REASONS).
            status=headwind
        elif trend_reason and (trend_reason == 'market_headwind' or not event_support): status=trend_reason
        elif score<MIN_COMPOSITE_SCORE: status='composite_score_below_policy'
        elif catalyst:
            status,plan=catalyst_plan(row,forecast,forecast.get('catalyst_context',{}),at,
                reference_date,report.get('preopen_session'),score,'event_catalyst')
            if plan: strategy='event_catalyst'
        elif not math.isfinite(forecast.get('base_price',float('nan'))) or forecast['base_price']/price-1<MIN_BASE_REWARD_PCT/100:
            status='insufficient_expected_reward'
        else:
            status='conditional_buy'
        if plan:
            row['preopen_signal']=plan
            recommendations.append(row['symbol'])
            if plan.get('strategy')=='earnings_catalyst': catalyst_picks.append(row['symbol'])
            elif plan.get('track')=='early_entry': early_picks.append(row['symbol'])
            elif plan.get('track')=='rebound': rebound_picks.append(row['symbol'])
        elif status=='conditional_buy':
            policy=exit_policy_for(row)
            row['preopen_signal']=dict(status=status,confirmed=False,entry_limit=entry_limit,
                entry_floor=entry_floor,strategy=strategy,
                reference_price=price,reference_date=_expected_session(previous_session,row.get('market')),
                generated_at=at.isoformat(),composite_score=score,
                caveats=list(forecast.get('caveats',[])),
                entry_basis='source_dated_catalyst_pullback_band' if catalyst else 'previous_session_close_limit_not_current_market_quote',
                fresh_execution_quote_required=True,
                target_basis='upside_scenario_not_expected_return' if catalyst else 'base_scenario',
                expires_session=report.get('preopen_session'),
                take_profit_pct=policy.take_profit_pct,
                take_profit_mode=policy.take_profit_mode,exit_policy=policy_snapshot(policy),
                **stop_basis(row))
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
        catalyst_candidates=catalyst_picks,
        early_entry_candidates=early_picks,
        rebound_candidates=rebound_picks,
        policy=dict(min_composite=MIN_COMPOSITE_SCORE,min_base_reward_pct=MIN_BASE_REWARD_PCT,
                    stop_pct=dict(fund=FUND_STOP_PCT,stock=STOCK_STOP_PCT),take_profit_pct=6,
                    take_profit_mode=DEFAULT_EXIT_POLICY.take_profit_mode,
                    early_entry=dict(entry_offset_pct=early_entry_track.ENTRY_OFFSET_PCT,
                                     hold_sessions=early_entry_track.HOLD_SESSIONS,
                                     min_volume_ratio=early_entry_track.MIN_VOLUME_RATIO,
                                     max_alignment_days=early_entry_track.MAX_ALIGNMENT_DAYS,
                                     max_above_ma20_pct=early_entry_track.MAX_ABOVE_MA20*100,
                                     qualifiers=sorted(early_entry_track.QUALIFIER_LABELS),
                                     lifted_reasons=sorted(EARLY_ENTRY_LIFTABLE_REASONS),
                                     stop_pct='instrument_split',
                                     take_profit_pct=6),
                    rebound=dict(gap_cancel_pct=rebound_track.GAP_CANCEL_PCT,
                                 hold_sessions=rebound_track.HOLD_SESSIONS,
                                 stop_policy=rebound_track.STOP_POLICY,
                                 score_gate_applied=False,
                                 trades={name:dict(entry_offset_pct=track['entry_offset_pct'])
                                         for name,track in rebound_track.CONFIG.items()}),
                    exit_policy=policy_snapshot()),
        live_execution_confirmed=False)
    return report['buy_evaluation']


def _merge(parts):
    counts=Counter(); missing=Counter(); caveats=Counter(); recommendations=[]; advance=[]; catalyst=[]; rebound=[]; early=[]
    for part in parts.values():
        counts.update(part['counts']); missing.update(part['missing_dimensions'])
        caveats.update(part.get('caveats',{}))
        recommendations.extend(part['recommendations']); advance.extend(part['advance_candidates'])
        catalyst.extend(part.get('catalyst_candidates',[]))
        rebound.extend(part.get('rebound_candidates',[]))
        early.extend(part.get('early_entry_candidates',[]))
    evaluated=sum(counts.values()); excluded=sum(counts[s] for s in INCOMPLETE_REASONS)
    policy=next(iter(parts.values()))['policy'] if parts else {}
    return dict(completed=bool(evaluated) and not excluded,
        decision_completed=evaluated-excluded,data_excluded=excluded,
        connected=True,mode='conditional_preopen',scope='analysed_candidates',
        previous_session={market:part['previous_session'] for market,part in parts.items()},
        markets=sorted(parts),generated_at=next(iter(parts.values()))['generated_at'] if parts else None,
        evaluated=evaluated,counts=dict(counts),missing_dimensions=dict(missing),
        caveats=dict(caveats),recommendations=recommendations,advance_candidates=advance,
        catalyst_candidates=catalyst,early_entry_candidates=early,rebound_candidates=rebound,policy=policy,
        live_execution_confirmed=False)


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
            catalyst_candidates=[],early_entry_candidates=[],rebound_candidates=[],
            policy=dict(min_composite=MIN_COMPOSITE_SCORE,min_base_reward_pct=MIN_BASE_REWARD_PCT,
                        stop_pct=dict(fund=FUND_STOP_PCT,stock=STOCK_STOP_PCT),take_profit_pct=6,
                        take_profit_mode=DEFAULT_EXIT_POLICY.take_profit_mode,
                        early_entry=dict(entry_offset_pct=early_entry_track.ENTRY_OFFSET_PCT,
                                         hold_sessions=early_entry_track.HOLD_SESSIONS,
                                         min_volume_ratio=early_entry_track.MIN_VOLUME_RATIO,
                                         max_alignment_days=early_entry_track.MAX_ALIGNMENT_DAYS,
                                         max_above_ma20_pct=early_entry_track.MAX_ABOVE_MA20*100,
                                         qualifiers=sorted(early_entry_track.QUALIFIER_LABELS),
                                         lifted_reasons=sorted(EARLY_ENTRY_LIFTABLE_REASONS),
                                         stop_pct='instrument_split',
                                         take_profit_pct=6),
                        rebound=dict(gap_cancel_pct=rebound_track.GAP_CANCEL_PCT,
                                     hold_sessions=rebound_track.HOLD_SESSIONS,
                                     stop_policy=rebound_track.STOP_POLICY,
                                     score_gate_applied=False,
                                     trades={name:dict(entry_offset_pct=track['entry_offset_pct'])
                                             for name,track in rebound_track.CONFIG.items()}),
                        exit_policy=policy_snapshot()),
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
