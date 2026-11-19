import unittest
from datetime import datetime,timedelta,timezone
from economic_graph import frames,connect,discover
from event_impact_analysis import direct_company_event

AT=datetime(2026,11,19,1,tzinfo=timezone.utc)

class EntertainmentRoutingTests(unittest.TestCase):
    def event(self,text):
        return dict(event_id='test-policy',url='https://www.korea.kr/test',
            available_at=(AT-timedelta(hours=1)).isoformat(),
            body=text+'\\n'+'정책의 구체적 이행 사항은 추후 확인한다. '*40)
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
if __name__=='__main__': unittest.main()
