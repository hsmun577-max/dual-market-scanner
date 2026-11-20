"""Refresh failed news evidence without repeating already verified financial downloads."""
from datetime import datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
from event_impact_analysis import world_events,company_news_events,article_text,analyze_events
from company_forecast import build_company_forecast
from industry_flow import industry_sector_evidence
from issuer_news import issuer_news_result

def refresh_report(report,at,industry):
    selected=[r for r in report['results'] if r.get('company_analysis')]
    for row in selected:
        checked=datetime.fromisoformat(row['company_analysis'].get('checked_at',''))
        if checked.utcoffset() is None or not at-timedelta(minutes=30)<=checked<=at:
            raise ValueError('Cached issuer analysis must be verified within 30 minutes')
    if not selected:raise ValueError('No verified issuer analysis to refresh')
    events,coverage=world_events(at)
    report['sectors']=industry_sector_evidence(industry)
    def refresh(row):
        company=dict(row['company_analysis'])
        symbol=row['symbol'].split('.')[0]
        native=issuer_news_result(symbol,row.get('name') or symbol,at)
        if native['status']=='checked':
            company.update(news=native['articles'],news_status='ok' if native['articles'] else 'no_recent_articles',
                news_provider='domestic_direct',news_checked_at=at.isoformat(),news_provider_attempts=native['attempts'])
        else:
            # Do not relabel an old successful news fetch as a new check.
            company['news_refresh_status']='unavailable'
        company['event_analysis']=analyze_events(company,[*events,*company_news_events(company,article_text)],at,coverage)
        # Original financial/profile timestamps are deliberately retained.
        row['company_analysis']=company
        row['company_forecast']=build_company_forecast(company,row['indicators'],row['indicators']['price'],at,
            row.get('risk_evidence',[]),report['sectors'])
        return row
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(refresh,selected))
    report['evidence_refreshed_at']=at.isoformat()
    return report
