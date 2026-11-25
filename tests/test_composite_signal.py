from datetime import datetime, timedelta
import math
import unittest
from company_forecast import (MIN_BASE_REWARD_PCT, MIN_COMPOSITE_SCORE,
                              SCENARIO_SIGMA_CAP, SCENARIO_THRESHOLD_SCORE, build_company_forecast)
from kis_intraday import evaluate_candidate, format_notification, KST

AT=datetime(2026,11,18,9,15,tzinfo=KST)

def inputs():
    company=dict(quarter='2026-09',checked_at='2026-11-18T08:00:00+09:00',
        operating_margin_pct=12.,revenue_yoy_pct=20.,operating_margin_yoy_pp=3.,
        free_cash_flow_margin_pct=8.,operating_cash_conversion=1.2,net_debt_to_equity=.3,
        trailing_pe=25.,forward_pe=20.,sector='Technology',filing_status='checked',news_status='ok',
        event_analysis=dict(generated_at='2026-11-18T08:00:00+09:00',status='no_material_events_identified',
                            feed_coverage=[dict(status='checked')],unread_events=[]))
    indicators=dict(price=100.,ma20=98.,return_5d=.03,return_20d=.08,
        relative_strength=.06,volume_ratio=2.,volatility_20d=.02,market_return_20d=.02)
    sectors=[dict(name='반도체',data_date='2026-11-17',market_excess_pct=.5,turnover_ratio=1.8)]
    return company,indicators,sectors


class CompositeSignalTests(unittest.TestCase):
    def test_missing_causal_analysis_keeps_the_verified_evidence_but_flags_it(self):
        company,indicators,sectors=inputs()
        complete=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        del company['event_analysis']
        result=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        self.assertIn('causal_event_analysis',result['missing_dimensions'])
        self.assertEqual(result['assessment_status'],'partial')
        self.assertIsNotNone(result['composite_score'])
        # The events dimension scored nothing either way here, so the score itself is not
        # inflated; the evidence coverage is what records the gap.
        self.assertLess(result['data_coverage'],complete['data_coverage'])
        self.assertLessEqual(result['composite_score'],complete['composite_score'])

    def test_unread_news_body_keeps_the_score_and_widens_it(self):
        company,indicators,sectors=inputs()
        complete=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        company['event_analysis'].update(score=.3,status='directional_hypotheses',
                                       unread_events=[dict(url='https://example.com')])
        result=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
        self.assertIn('unread_event_bodies',result['missing_dimensions'])
        self.assertIn('unread_event_bodies',result['caveats'])
        self.assertEqual(result['assessment_status'],'partial')
        self.assertIsNotNone(result['composite_score'])
        self.assertLess(result['composite_score'],complete['composite_score'])

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
        # The real scenario formula, not a patched base price, must carry the candidate.
        status,candidate=evaluate_candidate(bars,quote,AT,'KRX','005930',evidence,False,
                                            company=company,sectors=sectors)
        self.assertEqual(status,'buy_signal')
        forecast=company['forecast']
        self.assertGreaterEqual(forecast['composite_score'],MIN_COMPOSITE_SCORE)
        self.assertGreaterEqual(forecast['base_price']/candidate[4]-1,MIN_BASE_REWARD_PCT/100)
        text=format_notification('KRX',[candidate],AT,{'005930':company},sectors)
        self.assertIn('종합 ',text)
        # The score is one compact figure now, not '종합 66.2/100'.
        self.assertIn(f"종합 {forecast['composite_score']:.1f}",text)
        for noise in ['영업이익률','증권사','공시','최근 기사','관찰 후보','30분','차트 점수']:
            self.assertNotIn(noise,text)
        status,candidate=evaluate_candidate(bars,quote,AT,'KRX','005930',evidence,False,
                                            company={},sectors=sectors)
        self.assertEqual(status,'composite_assessment_incomplete')
        self.assertIsNone(candidate)

    def test_low_volatility_candidate_still_fails_the_reward_floor(self):
        company,indicators,sectors=inputs()
        indicators['volatility_20d']=.01
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
        self.assertGreaterEqual(company['forecast']['composite_score'],MIN_COMPOSITE_SCORE)
        self.assertLess(company['forecast']['base_return_pct'],MIN_BASE_REWARD_PCT)
        self.assertEqual(status,'insufficient_expected_reward')
        self.assertIsNone(candidate)

    def test_reward_floor_and_scenario_shape_meet_at_the_score_threshold(self):
        company,indicators,sectors=inputs()
        for volatility in (.015,.02,.03):
            indicators.update(volatility_20d=volatility)
            forecast=build_company_forecast(company,indicators,100.,AT,sectors=sectors)
            self.assertAlmostEqual(forecast['scenario_sigma_multiple'],
                                   math.sqrt((forecast['composite_score']-50)/10),places=2)
            # A candidate at the scenario anchor (``SCENARIO_THRESHOLD_SCORE``=60) clears the
            # 3.3% floor from 1.5% daily volatility upward, using the real formula. The entry
            # gate is lower (``MIN_COMPOSITE_SCORE``=53), so this score still passes it.
            self.assertGreaterEqual(forecast['composite_score'],MIN_COMPOSITE_SCORE)
            self.assertGreaterEqual(forecast['composite_score'],SCENARIO_THRESHOLD_SCORE)
            self.assertGreaterEqual(forecast['base_return_pct'],MIN_BASE_REWARD_PCT)
            self.assertLessEqual(forecast['scenario_sigma_multiple'],SCENARIO_SIGMA_CAP)
            self.assertLessEqual(forecast['bear_price'],100.)
            self.assertGreaterEqual(forecast['bull_price'],100.)
        self.assertEqual(forecast['min_base_reward_pct'],MIN_BASE_REWARD_PCT)


if __name__=='__main__':
    unittest.main()
