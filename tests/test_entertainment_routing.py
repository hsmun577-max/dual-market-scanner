import unittest
from datetime import datetime,timedelta,timezone
from economic_graph import frames,connect,discover
from event_impact_analysis import direct_company_event

AT=datetime(2026,11,19,1,tzinfo=timezone.utc)

class EntertainmentRoutingTests(unittest.TestCase):
    def event(self,text):
        return dict(event_id='test-policy',url='https://www.korea.kr/test',
            available_at=(AT-timedelta(hours=1)).isoformat(),
            body=text+'\n'+'정책의 구체적 이행 사항은 추후 확인한다. '*40)
    def company(self):
        return dict(country='South Korea',sector='Communication Services',industry='Entertainment',
            business_description='Music production and live concerts in South Korea.',
            profile_available_at=(AT-timedelta(days=1)).isoformat())
    def test_cultural_cooperation_creates_research_watch_without_fake_revenue_permission(self):
        events=[self.event('한국과 중국은 인적·문화교류를 한층 강화해 나가기로 했다.')]
        result=connect(self.company(),frames(events,AT),AT)
        self.assertEqual(result['status'],'expectation_watch_only')
        self.assertEqual(result['links'],[])
        self.assertEqual(result['expectation_links'][0]['direction'],0)
        self.assertEqual(discover({'ANY_MUSIC_ISSUER':self.company()},events,AT),['ANY_MUSIC_ISSUER'])
    def test_negative_or_future_cooperation_not_positive(self):
        events=[self.event('한국과 중국은 문화교류 확대를 추진하지 않는다.')]
        self.assertEqual(discover({'ANY':self.company()},events,AT),[])
        e=self.event('한국과 중국은 문화교류 확대를 추진한다.')
        e['available_at']=(AT+timedelta(seconds=1)).isoformat()
        self.assertEqual(frames([e],AT),[])
    def test_korean_three_syllable_and_short_english_official_aliases(self):
        self.assertTrue(direct_company_event({'headline':'하이브 중국 공연 기대'}, {'entity_aliases':['하이브']}))
        self.assertTrue(direct_company_event({'headline':'JYP concert plans'}, {'entity_aliases':['JYP']}))
        self.assertFalse(direct_company_event({'headline':'XYZJYP unrelated'}, {'entity_aliases':['JYP']}))
class ProfileReuseTests(unittest.TestCase):
    def test_failed_refresh_reuses_profile_without_claiming_new_evidence(self):
        from kis_intraday import restore_business_profile
        observed=(AT-timedelta(days=1)).isoformat()
        cached=dict(business_description='Music production in South Korea',sector='Communication Services',
                    industry='Entertainment',country='South Korea',profile_available_at=observed)
        restored=restore_business_profile({'profile_error':'HTTPError'},cached,AT)
        self.assertEqual(restored['profile_available_at'],observed)
        self.assertEqual(restored['sector'],'Communication Services')
        self.assertNotIn('forward_pe',restored)
        stale={**cached,'profile_available_at':(AT-timedelta(days=181)).isoformat()}
        self.assertNotIn('sector',restore_business_profile({},stale,AT))
        future={**cached,'profile_available_at':(AT+timedelta(seconds=1)).isoformat()}
        self.assertNotIn('sector',restore_business_profile({},future,AT))

class ResearchCapacityTests(unittest.TestCase):
    def test_linked_issuers_beyond_old_eight_slot_cap_are_researched_before_chart_trend(self):
        from economic_graph import select_analysis_candidates
        rows=[]
        for i in range(12):
            rows.append(dict(symbol=f'T{i:02}.KS',name='일반기업',signal={'score':0},
                indicators=dict(price=10000,volume=2000000+i,volume_ratio=2,
                    ma20=12000,ma60=14000,return_5d=-.02,return_20d=-.1,
                    relative_strength=-.1,average_turnover_20d=20000000000+i)))
        selected=select_analysis_candidates(rows,'KR',[r['symbol'] for r in rows])
        self.assertEqual({r['symbol'] for r in selected},{r['symbol'] for r in rows})

class CompositeRoutingTests(unittest.TestCase):
    def test_content_proxy_and_policy_expectation_are_scored_without_fake_earnings(self):
        from company_forecast import build_company_forecast
        company=dict(quarter='2026-09',checked_at=AT.isoformat(),sector='Communication Services',market='KR',
            operating_margin_pct=12,revenue_yoy_pct=20,operating_margin_yoy_pp=3,
            free_cash_flow_margin_pct=8,operating_cash_conversion=1.2,net_debt_to_equity=.3,
            trailing_pe=25,forward_pe=20,filing_status='checked',news_status='ok',
            event_analysis=dict(generated_at=AT.isoformat(),status='insufficient_causal_evidence',
                feed_coverage=[dict(status='checked')],unread_events=[],
                economic_graph=dict(expectation_links=[dict(watch_bias='positive_policy_sympathy',
                    source_url='https://www.korea.kr/test',direction=0,quantified=False)])))
        indicators=dict(return_5d=.03,relative_strength=.06,volume_ratio=2,ma20=98,
                        volatility_20d=.02,market_return_20d=.02)
        sectors=[dict(name='미디어·엔터',market='KR',data_date='2026-11-18',
                      market_excess_pct=.5,turnover_ratio=1.8)]
        result=build_company_forecast(company,indicators,100,AT,sectors=sectors)
        self.assertIsNotNone(result['composite_score'])
        self.assertIn('sector_pressure',result['components'])
        self.assertTrue(result['evidence']['events']['policy_expectation_present'])
        self.assertEqual(result['evidence']['events']['quantified_earnings_events'],0)

if __name__=='__main__': unittest.main()

