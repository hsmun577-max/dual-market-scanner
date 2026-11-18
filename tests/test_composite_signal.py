from copy import deepcopy
from unittest.mock import patch
from datetime import datetime, timedelta
import unittest
from company_forecast import build_company_forecast
from kis_intraday import evaluate_candidate, format_notification, KST

AT=datetime(2026,11,18,9,15,tzinfo=KST)

def inputs():
    company=dict(quarter='2026-09',checked_at='2026-11-18T08:00:00+09:00',
        operating_margin_pct=12.,revenue_yoy_pct=20.,operating_margin_yoy_pp=3.,
        free_cash_flow_margin_pct=8.,operating_cash_conversion=1.2,net_debt_to_equity=.3,
        trailing_pe=25.,forward_pe=20.,sector='Technology',filing_status='checked',news_status='ok')
    indicators=dict(price=100.,ma20=98.,return_5d=.03,return_20d=.08,
        relative_strength=.06,volume_ratio=2.,volatility_20d=.02,market_return_20d=.02)
    sectors=[dict(name='반도체',data_date='2026-11-17',market_excess_pct=.5,turnover_ratio=1.8)]
    return company,indicators,sectors


class CompositeSignalTests(unittest.TestCase):
    def test_new_periodic_filing_blocks_old_financials(self):
        company,indicators,sectors=inputs()
        company.update(quarter='2026-06-30',filings=[dict(form='10-K',filing_date='2026-11-17')])
        result=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        self.assertIsNone(result['composite_score'])
        self.assertIn('latest_report_not_in_financials',result['missing_dimensions'])

    def test_matching_periodic_filing_does_not_block(self):
        company,indicators,sectors=inputs()
        company['filings']=[dict(form='10-Q',filing_date='2026-11-05',report_period='2026-09-30')]
        self.assertIsNotNone(build_company_forecast(company,indicators,100.,AT,sectors=sectors)['composite_score'])
    def test_score_uses_all_dimensions_and_is_not_a_probability(self):
        company,indicators,sectors=inputs()
        result=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        self.assertEqual(result['assessment_status'],'complete')
        self.assertEqual(len(result['components']),7)
        self.assertGreater(result['composite_score'],50)
        self.assertLessEqual(result['composite_score'],100)
        self.assertLess(result['bear_price'],result['base_price'])
        self.assertLess(result['base_price'],result['bull_price'])
        self.assertNotIn('win_probability',result)
        self.assertEqual(result['validation'],'out_of_sample_accuracy_not_established')

    def test_same_chart_with_worse_business_gets_lower_score(self):
        company,indicators,sectors=inputs()
        healthy=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        company.update(operating_margin_pct=-10.,revenue_yoy_pct=-30.,operating_margin_yoy_pp=-8.,
            free_cash_flow_margin_pct=-15.,operating_cash_conversion=.1,net_debt_to_equity=3.,
            trailing_pe=15.,forward_pe=30.)
        weak=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        self.assertLess(weak['composite_score'],50)
        self.assertGreater(healthy['composite_score'],weak['composite_score'])

    def test_missing_financials_never_become_a_chart_only_composite_score(self):
        _,indicators,sectors=inputs()
        result=build_company_forecast({},indicators,100.,AT,sectors=sectors)
        self.assertIsNone(result['composite_score'])
        self.assertEqual(result['assessment_status'],'incomplete')

    def test_rise_already_priced_in_reduces_remaining_forecast(self):
        company,indicators,sectors=inputs()
        before=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        after=build_company_forecast(company,indicators,115.,AT,sectors=sectors)
        self.assertLess(after['composite_score'],before['composite_score'])

    def test_future_world_event_is_not_used(self):
        company,indicators,sectors=inputs()
        baseline=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        event=dict(kind='market',available_at=(AT+timedelta(minutes=1)).isoformat(),
                   snapshot={'^VIX':{'change_pct':100.}})
        actual=build_company_forecast(company,indicators,100.,AT,[event],sectors)
        self.assertEqual(actual,baseline)

    def test_unquantified_news_increases_uncertainty_without_inventing_revenue_damage(self):
        company,indicators,sectors=inputs()
        before=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        event=dict(kind='event',assessment='risk',available_at=(AT-timedelta(minutes=1)).isoformat())
        after=build_company_forecast(company,indicators,100.,AT,[event],sectors)
        self.assertEqual(after['components']['events'],0)
        self.assertEqual(after['evidence']['events']['unquantified_events'],1)
        self.assertGreater(after['bull_price']-after['bear_price'],before['bull_price']-before['bear_price'])

    def test_live_entry_and_compact_message_use_the_composite_score(self):
        company,indicators,sectors=inputs()
        company['price_indicators']=indicators
        bars={0:dict(high=100.,low=99.,close=99.9,volume=100),
              1:dict(high=100.,low=99.5,close=100.,volume=100),
              2:dict(high=100.3,low=99.9,close=100.2,volume=250)}
        quote=dict(event_time=(AT-timedelta(seconds=1)).isoformat(),spread_pct=.1,ask=100.2,bid=100.1)
        evidence=[dict(kind=kind,market='KRX',symbol='*',assessment=grade,
                       available_at=(AT-timedelta(minutes=1)).isoformat())
                  for kind,grade in [('market','supportive'),('event','clear')]]
        status,candidate=evaluate_candidate(bars,quote,AT,'KRX','005930',evidence,False,
                                            company=company,sectors=sectors)
        self.assertEqual(status,'insufficient_expected_reward')
        self.assertIsNone(candidate)
        forecast=build_company_forecast(company,indicators,100.5,AT,sectors=sectors)
        forecast['base_price']=105.
        with patch('kis_intraday.build_company_forecast',return_value=forecast):
            status,candidate=evaluate_candidate(bars,quote,AT,'KRX','005930',evidence,False,
                                                company=company,sectors=sectors)
        self.assertEqual(status,'buy_signal')
        text=format_notification('KRX',[candidate],AT,{'005930':company},sectors)
        self.assertIn('종합 ',text)
        self.assertIn('/100',text)
        for noise in ['영업이익률','증권사','공시','최근 기사','관찰 후보','30분','차트 점수']:
            self.assertNotIn(noise,text)
        status,candidate=evaluate_candidate(bars,quote,AT,'KRX','005930',evidence,False,
                                            company={},sectors=sectors)
        self.assertEqual(status,'composite_assessment_incomplete')
        self.assertIsNone(candidate)


if __name__=='__main__':
    unittest.main()
