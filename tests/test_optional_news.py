from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
import unittest
from event_impact_analysis import analyze_events,company_news_events
from company_forecast import build_company_forecast

AT=datetime(2026,11,19,0,40,tzinfo=timezone.utc)
COVER=[dict(source='official',status='checked')]

def company(**changes):
    result=dict(company_name='Alpha Systems',market='KR',news=[],news_status='no_recent_articles',
        filing_status='checked',quarter='2026-09',sector='Technology',
        operating_margin_pct=15,revenue_yoy_pct=25,operating_margin_yoy_pp=4,
        free_cash_flow_margin_pct=10,operating_cash_conversion=1.5,net_debt_to_equity=.1,
        trailing_pe=15,forward_pe=12)
    result.update(changes);return result

def news(headline,n=0):
    return dict(headline=headline,url=f'https://www.reuters.com/example/{n}',
        available_at=(AT-timedelta(hours=1)).isoformat())

class OptionalNewsTests(unittest.TestCase):
    def test_no_special_news_skips_bodies_and_allows_complete_forecast(self):
        c=company();reader=Mock()
        events=company_news_events(c,reader);reader.assert_not_called()
        c['event_analysis']=analyze_events(c,events,AT,COVER)
        self.assertEqual(c['event_analysis']['analysis_mode'],'skipped_no_material_news')
        i=dict(ma20=99,return_5d=.02,relative_strength=.04,volume_ratio=1.5,
            market_return_20d=.02,volatility_20d=.02)
        f=build_company_forecast(c,i,100,AT,sectors=[dict(market='KR',name='반도체',
            data_date='2026-11-18',market_excess_pct=1,turnover_ratio=1.5)])
        self.assertEqual(f['assessment_status'],'complete')
        self.assertEqual(f['components']['events'],0)

    def test_quote_only_articles_are_not_downloaded(self):
        c=company(news=[news('Alpha Systems stock price today')],news_status='ok')
        reader=Mock();events=company_news_events(c,reader);reader.assert_not_called()
        self.assertEqual(c['news_screening']['skipped_routine_count'],1)
        self.assertEqual(analyze_events(c,events,AT,COVER)['status'],'no_material_events_identified')

    def test_material_item_after_three_quotes_is_still_read(self):
        c=company(news=[*[news('Alpha Systems stock price today',i) for i in range(3)],
            news('Alpha Systems stock price today: orders increased',3)],news_status='ok')
        reader=Mock(return_value=('Alpha Systems orders increased. '+'Background. '*50,'public_body'))
        events=company_news_events(c,reader);reader.assert_called_once()
        self.assertEqual(c['news_screening']['material_count'],1)
        self.assertGreater(analyze_events(c,events,AT,COVER)['score'],0)

    def test_unknown_news_is_not_silently_skipped(self):
        reader=Mock(return_value=('','unavailable'))
        company_news_events(company(news=[news('Alpha Systems update')],news_status='ok'),reader)
        reader.assert_called_once()

    def test_failed_news_lookup_remains_unknown(self):
        c=company(news_status='unavailable')
        events=company_news_events(c,Mock())
        self.assertEqual(analyze_events(c,events,AT,COVER)['status'],'insufficient_causal_evidence')

    def test_missing_filings_or_world_coverage_is_not_waived(self):
        for c,coverage in [(company(filing_status='unavailable'),COVER),(company(),[])]:
            self.assertEqual(analyze_events(c,[],AT,coverage)['status'],'insufficient_causal_evidence')

    def test_unread_material_news_remains_pending(self):
        c=company(news=[news('Alpha Systems contract canceled')],news_status='ok')
        events=company_news_events(c,Mock(return_value=('','unavailable')))
        analysis=analyze_events(c,events,AT,COVER)
        self.assertEqual(analysis['status'],'insufficient_causal_evidence')
        self.assertTrue(analysis['unread_events'][0]['required'])

    def test_world_event_still_affects_company_without_company_news(self):
        c=company(business_description='An airline operator')
        e={**news('Energy costs increased'), 'event_id':'energy','scope':'world',
            'body':'Energy costs increased. '+'Background. '*50,'body_status':'public_body'}
        analysis=analyze_events(c,[e],AT,COVER)
        self.assertLess(analysis['score'],0)
        self.assertEqual(analysis['analysis_mode'],'material_event_analysis')

if __name__=='__main__':unittest.main()
