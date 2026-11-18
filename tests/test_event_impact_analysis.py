from datetime import datetime, timedelta, timezone
import unittest
from event_impact_analysis import analyze_events, earnings_bridge, allowed_url
AT=datetime(2026,11,18,13,tzinfo=timezone.utc)
COVER=[dict(source='official',status='checked')]


def event(text, **changes):
    row=dict(event_id='https://www.federalreserve.gov/example',url='https://www.federalreserve.gov/example',
        source='official',headline='Event',available_at=(AT-timedelta(days=2)).isoformat(),
        body=text+'\n'+'Company background and methodology. '*20,body_status='public_body',scope='world')
    row.update(changes)
    return row


class EventImpactTests(unittest.TestCase):
    def test_same_input_cost_event_has_opposite_business_effect(self):
        e=event('Energy costs increased.')
        producer=analyze_events(dict(business_description='Oil exploration and production'),[e],AT,COVER)
        airline=analyze_events(dict(business_description='An airline operator'),[e],AT,COVER)
        self.assertGreater(producer['score'],0)
        self.assertLess(airline['score'],0)
        self.assertIsNone(producer['quantified_earnings_impact'])

    def test_unrelated_news_is_not_company_demand(self):
        company=dict(company_name='Alpha Systems',business_description='subscription software')
        result=analyze_events(company,[event('Beta Networks orders increased.')],AT,COVER)
        self.assertIsNone(result['score'])
        self.assertFalse(result['assessed_events'][0]['relevant'])

    def test_named_company_demand_has_causal_path_and_time_horizon(self):
        company=dict(company_name='Alpha Systems',business_description='subscription software')
        result=analyze_events(company,[event('Alpha Systems orders increased.')],AT,COVER)
        self.assertGreater(result['score'],0)
        claim=result['positive_drivers'][0]
        self.assertIn('매출',claim['path'])
        self.assertIn('분기',claim['realization'])
        self.assertEqual(claim['support'],'rule_based_hypothesis')

    def test_future_expired_and_duplicate_events(self):
        company=dict(business_description='An airline operator')
        e=event('Energy costs increased.')
        baseline=analyze_events(company,[e],AT,COVER)
        self.assertEqual(baseline,analyze_events(company,[e,e],AT,COVER))
        for change in [dict(available_at=(AT+timedelta(seconds=1)).isoformat()),
                       dict(expires_at=(AT-timedelta(seconds=1)).isoformat())]:
            self.assertIsNone(analyze_events(company,[event('Energy costs increased.',**change)],AT,COVER)['score'])
        self.assertLess(baseline['score'],0)  # Two-day-old persistent effect is retained.

    def test_headline_only_is_unread_not_neutral(self):
        result=analyze_events(dict(company_name='Alpha Systems'),[event('',headline='Alpha Systems orders increased.',body='')],AT,COVER)
        self.assertIsNone(result['score'])
        self.assertEqual(len(result['unread_events']),1)
        self.assertEqual(result['status'],'insufficient_causal_evidence')

    def test_negation_and_forecasts_are_not_confirmed_facts(self):
        company=dict(company_name='Alpha Systems')
        for text in ['Alpha Systems demand did not increase.', 'Alpha Systems demand may increase.',
                     'Alpha Systems demand increased but orders declined.']:
            self.assertIsNone(analyze_events(company,[event(text)],AT,COVER)['score'])

    def test_unknown_currency_exposure_is_not_assigned_a_direction(self):
        result=analyze_events(dict(company_name='Alpha Systems'),[event('Alpha Systems exchange rate increased.')],AT,COVER)
        self.assertIsNone(result['score'])
        self.assertTrue(result['uncertainties'])

    def test_article_urls_are_restricted(self):
        self.assertTrue(allowed_url('https://www.sec.gov/Archives/a.htm'))
        for url in ['http://www.sec.gov/a','https://127.0.0.1/a','https://sec.gov.evil.example/a',
                    'https://name:password@www.sec.gov/a','https://www.sec.gov:8080/a']:
            self.assertFalse(allowed_url(url))

    def test_earnings_bridge_keeps_volume_price_and_cost_separate(self):
        assumptions=dict(volume_change_pct=10,selling_price_change_pct=5,unit_cost_change_pct=3,fixed_cost_change_pct=0)
        result=earnings_bridge(1000,100,.6,assumptions)
        self.assertAlmostEqual(result['revenue'],1155.)
        self.assertAlmostEqual(result['operating_income'],175.2)
        self.assertEqual(result['status'],'sensitivity_only')
        del assumptions['unit_cost_change_pct']
        self.assertEqual(earnings_bridge(1000,100,.6,assumptions)['status'],'missing_assumptions')


if __name__=='__main__': unittest.main()
