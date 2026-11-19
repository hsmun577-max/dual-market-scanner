"""Preopen recommendations are conditional plans, never a live execution confirmation."""
from datetime import datetime,timedelta,timezone
from collections import Counter
import math
from candidate_selection import liquidity_rejection_reason,candidate_rejection_reason

def evaluate_preopen(report, previous_session, at=None):
    at=at or datetime.now(timezone.utc)
    if at.utcoffset() is None: raise ValueError('Timezone-aware decision time required')
    counts=Counter(); missing=Counter(); recommendations=[]
    for row in report.get('results',[]):
        row.pop('preopen_signal',None)
        if 'company_analysis' not in row:
            continue
        company=row['company_analysis']; forecast=row.get('company_forecast',{})
        status=None
        try:
            generated=datetime.fromisoformat(forecast.get('generated_at',''))
            fresh=generated.utcoffset() is not None and at-timedelta(minutes=30)<=generated<=at
        except (ValueError,TypeError): fresh=False
        if not fresh: status='analysis_stale_or_missing'
        elif (not isinstance(forecast.get('composite_score'),(int,float))
              or not math.isfinite(forecast['composite_score'])
              or forecast.get('assessment_status')!='complete'):
            status='analysis_incomplete'
            missing.update(forecast.get('missing_dimensions',[]))
        elif row.get('indicators',{}).get('data_date')!=previous_session:
            status='price_session_mismatch'
        if status:
            counts[status]+=1
            row['preopen_decision']=status
            continue
        reason=liquidity_rejection_reason(row,row['market'])
        values=row['indicators']; price=values['price']
        graph=company.get('event_analysis',{}).get('economic_graph',{})
        event_support=any(link.get('direction',0)>0 for link in graph.get('links',[])) or any(
            link.get('watch_bias')=='positive_policy_sympathy' and link.get('source_url')
            for link in graph.get('expectation_links',[]))
        trend_reason=candidate_rejection_reason(row,row['market'])
        if reason: status=reason
        elif values['ma20']<=0: status='invalid_average_price'
        elif values.get('breakdown'): status='breakdown'
        elif values.get('overheated') or values.get('return_5d',0)>.12 or price/values['ma20']>1.08:
            status='extended_price'
        elif trend_reason and not event_support: status=trend_reason
        elif forecast['composite_score']<60: status='composite_score_below_policy'
        elif not math.isfinite(forecast.get('base_price',float('nan'))) or forecast['base_price']/price-1<.033:
            status='insufficient_expected_reward'
        else:
            status='conditional_buy'
            row['preopen_signal']=dict(status=status,confirmed=False,entry_limit=price,
                reference_price=price,reference_date=previous_session,generated_at=at.isoformat(),
                entry_basis='previous_session_close_limit_not_current_market_quote',
                expires_session=report.get('preopen_session'),stop_pct=.02,take_profit_pct=.06)
            recommendations.append(row['symbol'])
        counts[status]+=1; row['preopen_decision']=status
    incomplete={'analysis_stale_or_missing','analysis_incomplete','price_session_mismatch'}
    evaluated=sum(counts.values())
    report['buy_evaluation']=dict(completed=bool(evaluated) and not any(counts[s] for s in incomplete),
        connected=True,mode='conditional_preopen',scope='analysed_candidates',
        previous_session=previous_session,generated_at=at.isoformat(),evaluated=evaluated,
        counts=dict(counts),missing_dimensions=dict(missing),recommendations=recommendations,
        policy=dict(min_composite=60,min_base_reward_pct=3.3,stop_pct=2,take_profit_pct=6),
        live_execution_confirmed=False)
    return report['buy_evaluation']
