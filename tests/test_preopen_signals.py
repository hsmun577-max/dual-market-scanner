import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch
from valuation_data import domestic_valuations,apply_peer_valuations
from preopen_signals import evaluate_preopen
from scanner import telegram_candidate_line,send_telegram_summary
AT=datetime(2026,11,19,0,tzinfo=timezone.utc)

class ValuationTests(unittest.TestCase):
    def peers(self):
        return {str(i):dict(source_symbol=str(i),market='KR',country='South Korea',industry='Entertainment',
            valuation_observed_at=AT.isoformat(),trailing_pe=value,price_to_book=2)
            for i,value in enumerate([10,20,30,40])}
    def test_real_kis_fields_are_used_without_forward_estimates(self):
        client=Mock();client.get.return_value=dict(per='12.3',pbr='1.4',eps='100',bps='1000')
        actual=domestic_valuations(['352820'],client)['352820']
        self.assertEqual(actual['trailing_pe'],12.3)
        self.assertNotIn('forward_pe',actual)
        self.assertEqual(actual['valuation_source'],'KIS inquire-price')
    def test_peer_median_excludes_self_and_requires_three_other_issuers(self):
        peers=self.peers();apply_peer_valuations(peers,AT)
        self.assertEqual(peers['0']['peer_valuation']['peer_median'],30)
        self.assertEqual(peers['0']['peer_valuation']['peer_count'],3)
        self.assertNotIn('0',peers['0']['peer_valuation']['peer_symbols'])
        small=self.peers();del small['3'];apply_peer_valuations(small,AT)
        self.assertNotIn('peer_valuation',small['0'])
    def test_foreign_industry_mismatches_stale_future_and_duplicate_listings_not_peers(self):
        for change in [dict(country='United States'),dict(industry='Software'),
                       dict(valuation_observed_at=(AT-timedelta(days=2)).isoformat()),
                       dict(valuation_observed_at=(AT+timedelta(seconds=1)).isoformat()),
                       dict(source_symbol='0')]:
            peers=self.peers();peers['3'].update(change);apply_peer_valuations(peers,AT)
            self.assertNotIn('peer_valuation',peers['0'])
    def test_nonpositive_per_can_use_actual_book_multiple(self):
        peers=self.peers()
        for p in peers.values():p['trailing_pe']=0
        apply_peer_valuations(peers,AT)
        self.assertEqual(peers['0']['peer_valuation']['metric'],'price_to_book')
    def test_peer_valuation_connects_to_complete_composite_without_forward_pe(self):
        from company_forecast import build_company_forecast
        peers=self.peers();apply_peer_valuations(peers,AT)
        c=dict(peers['0'],quarter='2026-09',checked_at=AT.isoformat(),sector='Communication Services',
            operating_margin_pct=15,revenue_yoy_pct=30,operating_margin_yoy_pp=4,
            free_cash_flow_margin_pct=10,operating_cash_conversion=1.5,net_debt_to_equity=.1,
            filing_status='checked',news_status='no_recent_articles',
            event_analysis=dict(generated_at=AT.isoformat(),status='no_material_events_identified',
                                feed_coverage=[dict(status='checked')],unread_events=[]))
        i=dict(ma20=100,return_5d=.03,relative_strength=.1,volume_ratio=2,
               market_return_20d=.02,volatility_20d=.03)
        f=build_company_forecast(c,i,100,AT,sectors=[dict(market='KR',name='미디어·엔터',
            data_date='2026-11-18',market_excess_pct=1,turnover_ratio=2)])
        self.assertEqual(f['assessment_status'],'complete')
        self.assertEqual(f['evidence']['valuation_outlook']['metric'],'trailing_pe')
        self.assertIsNotNone(f['composite_score'])

class PreopenTests(unittest.TestCase):
    def row(self):
        return dict(symbol='352820.KS',market='KR',name='하이브',
            signal=dict(confirmed=False,score=0),
            indicators=dict(price=10000,volume=2000000,volume_ratio=2,average_turnover_20d=20000000000,
                ma20=9900,ma60=9800,return_5d=.03,return_20d=.1,relative_strength=.05,
                obv_rising=True,breakout=True,data_date='2026-11-18'),
            company_analysis=dict(event_analysis=dict(economic_graph={})),
            company_forecast=dict(assessment_status='complete',generated_at=AT.isoformat(),
                composite_score=70,base_price=10600,bull_price=11000,missing_dimensions=[]))
    def report(self,row):return dict(results=[row],preopen_session='2026-11-19',sectors=[])
    def test_daily_unconfirmed_flag_no_longer_blocks_a_valid_conditional_plan(self):
        row=self.row();report=self.report(row)
        result=evaluate_preopen(report,'2026-11-18',AT)
        self.assertEqual(result['recommendations'],['352820.KS'])
        self.assertFalse(row['signal']['confirmed'])
        self.assertFalse(row['preopen_signal']['confirmed'])
        self.assertEqual(row['preopen_signal']['entry_limit'],10000)
        self.assertIn('조건부 매수',telegram_candidate_line(row))
        self.assertIn('2026-11-18 종가 기준',telegram_candidate_line(row))
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'fake'}),patch(
            'scanner.delivery_status',return_value=(True,'test')),patch('scanner.requests.post') as post:
            post.return_value=Mock(status_code=200,json=lambda:{'ok':True})
            send_telegram_summary(report)
        self.assertIn('하이브',post.call_args.kwargs['json']['text'])
    def test_data_failures_stay_incomplete_instead_of_becoming_no_opportunity(self):
        for key,value in [('composite_score',None),('composite_score',float('nan')),
                          ('generated_at',(AT-timedelta(hours=1)).isoformat()),
                          ('generated_at',(AT+timedelta(seconds=1)).isoformat())]:
            row=self.row();row['company_forecast'][key]=value
            result=evaluate_preopen(self.report(row),'2026-11-18',AT)
            self.assertFalse(result['completed'])
            self.assertNotIn('preopen_signal',row)
        row=self.row()
        result=evaluate_preopen(self.report(row),'2026-11-17',AT)
        self.assertFalse(result['completed'])
    def test_real_rejections_are_completed_and_keep_risk_gates(self):
        for field,value in [('overheated',True),('average_turnover_20d',1),('breakdown',True)]:
            row=self.row();row['indicators'][field]=value
            result=evaluate_preopen(self.report(row),'2026-11-18',AT)
            self.assertTrue(result['completed']);self.assertEqual(result['recommendations'],[])
        for field,value in [('composite_score',52),('base_price',10200)]:
            row=self.row();row['company_forecast'][field]=value
            self.assertEqual(evaluate_preopen(self.report(row),'2026-11-18',AT)['recommendations'],[])
    def test_scoped_event_can_research_and_plan_before_ma60_turns_but_not_buy_overheated(self):
        row=self.row();row['indicators']['ma60']=11000
        self.assertEqual(evaluate_preopen(self.report(row),'2026-11-18',AT)['recommendations'],[])
        row['company_analysis']['event_analysis']['economic_graph']['expectation_links']=[
            dict(watch_bias='positive_policy_sympathy',source_url='https://www.korea.kr/test')]
        self.assertEqual(evaluate_preopen(self.report(row),'2026-11-18',AT)['recommendations'],['352820.KS'])
        row['indicators']['overheated']=True
        self.assertEqual(evaluate_preopen(self.report(row),'2026-11-18',AT)['recommendations'],[])
        self.assertNotIn('preopen_signal',row)
    def test_score_gate_is_53_and_the_reward_floor_still_binds(self):
        from company_forecast import MIN_COMPOSITE_SCORE,MIN_BASE_REWARD_PCT
        # The user moved the conditional-buy score gate from 60 to 53 on 2026-11-21.
        self.assertEqual(MIN_COMPOSITE_SCORE,53.0)
        row=self.row();row['company_forecast']['composite_score']=55
        result=evaluate_preopen(self.report(row),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],['352820.KS'])
        self.assertEqual(result['policy']['min_composite'],53.0)
        # Below the new gate the score still refuses the candidate.
        row=self.row();row['company_forecast']['composite_score']=52.9
        result=evaluate_preopen(self.report(row),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],[])
        self.assertEqual(row['preopen_decision'],'composite_score_below_policy')
        # And the 3.3% base-reward floor still refuses a low-score band candidate
        # whose scenario does not cover it.
        row=self.row();row['company_forecast'].update(composite_score=55,base_price=10300)
        result=evaluate_preopen(self.report(row),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],[])
        self.assertEqual(row['preopen_decision'],'insufficient_expected_reward')
        self.assertGreater(MIN_BASE_REWARD_PCT,3.0)


class MarketHeadwindGateTests(unittest.TestCase):
    """The market gate is read on its own, never skipped by event support.

    On 2026-11-23 the preopen delivery sent NHN (181710.KS), LG유플러스 (032640.KS) and
    NC (036570.KS) as conditional buys while the index was below its 20-day average. All
    three had a chart rejection first (misaligned averages or a weak 20-day return) and a
    scoped event in the economic graph, so ``candidate_rejection_reason`` never returned
    ``market_headwind`` and the event-support branch skipped the regime gate altogether.
    """
    def row(self):
        row=PreopenTests().row()
        row['indicators']['market_above_ma20']=True
        return row
    def report(self,rows):return dict(results=rows,preopen_session='2026-11-19',sectors=[])
    def event_support(self,row):
        row['company_analysis']['event_analysis']['economic_graph']['expectation_links']=[
            dict(watch_bias='positive_policy_sympathy',source_url='https://www.korea.kr/test')]
        return row
    def test_the_three_names_sent_in_a_2026_11_23_headwind_are_held_back(self):
        # Real indicator values from github-artifacts/20261123/preopen-delivery-KRX.
        cases=[('181710.KS','NHN',dict(price=61200.0,ma20=59175.0,ma60=58940.0,volume=282598.0,
                   volume_ratio=0.7741,average_turnover_20d=24619222165.0,return_5d=-0.0783,
                   return_20d=0.193,relative_strength=0.3156,atr14_pct=14.7642),61.4),
               ('032640.KS','LG유플러스',dict(price=14970.0,ma20=15080.5,ma60=14885.8333,
                   volume=634678.0,volume_ratio=0.5439,average_turnover_20d=17886982844.5,
                   return_5d=-0.0373,return_20d=0.0572,relative_strength=0.1798,
                   atr14_pct=5.1341),57.2),
               ('036570.KS','NC',dict(price=179600.0,ma20=196040.0,ma60=209055.0,volume=82477.0,
                   volume_ratio=0.8352,average_turnover_20d=19240693375.0,return_5d=0.0034,
                   return_20d=-0.1239,relative_strength=-0.0013,atr14_pct=6.4707),68.7)]
        rows=[]
        for symbol,name,values,score in cases:
            row=self.row();row['symbol']=symbol;row['name']=name
            row['indicators'].update(values,market_above_ma20=False,obv_rising=False,
                                     breakout=False,breakdown=False,rebound=False,overheated=False)
            row['company_forecast'].update(composite_score=score,base_price=values['price']*1.08,
                                           bull_price=values['price']*1.15)
            rows.append(self.event_support(row))
        result=evaluate_preopen(self.report(rows),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],[])
        self.assertEqual(result['counts'].get('market_headwind'),3)
        for row in rows:
            self.assertEqual(row['preopen_decision'],'market_headwind',row['symbol'])
            self.assertNotIn('preopen_signal',row)
    def test_event_support_does_not_skip_the_regime_for_any_soft_chart_reason(self):
        for chart in ['unconfirmed_uptrend','weak_relative_trend','insufficient_demand_expansion']:
            row=self.row();ind=row['indicators'];ind['market_above_ma20']=False
            if chart=='unconfirmed_uptrend':ind['ma60']=11000
            if chart=='weak_relative_trend':ind['return_20d']=-.05
            if chart=='insufficient_demand_expansion':ind['volume_ratio']=1.5
            result=evaluate_preopen(self.report([self.event_support(row)]),'2026-11-18',AT)
            self.assertEqual(result['recommendations'],[],chart)
            self.assertEqual(row['preopen_decision'],'market_headwind',chart)
            self.assertNotIn('preopen_signal',row)
    def test_an_event_eligible_catalyst_is_not_sent_in_a_headwind(self):
        row=self.row();row['indicators']['market_above_ma20']=False
        row['company_analysis']['price_tick']=100
        row['company_forecast']['catalyst_context']=dict(eligible=True)
        self.event_support(row)
        result=evaluate_preopen(self.report([row]),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],[])
        self.assertEqual(row['preopen_decision'],'market_headwind')
        # The same row outside a headwind is the event track the policy intends.
        row=self.row();row['company_analysis']['price_tick']=100
        row['company_forecast']['catalyst_context']=dict(eligible=True)
        result=evaluate_preopen(self.report([self.event_support(row)]),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],['352820.KS'])
        self.assertEqual(row['preopen_signal']['strategy'],'event_catalyst')
        # An unobserved regime is not a headwind: an archived report stays as it was.
        row=self.row();row['indicators']['ma60']=11000
        row['indicators'].pop('market_above_ma20')
        result=evaluate_preopen(self.report([self.event_support(row)]),'2026-11-18',AT)
        self.assertEqual(result['recommendations'],['352820.KS'])
        self.assertEqual(row['preopen_decision'],'conditional_buy')

if __name__=='__main__':unittest.main()
