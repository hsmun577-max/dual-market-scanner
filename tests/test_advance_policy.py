import unittest
from datetime import datetime,timedelta,timezone
from copy import deepcopy
from economic_graph import frames,connect,discover
from advance_policy import advance_context
from policy_event_index import merge_index

AT=datetime(2026,11,16,0,tzinfo=timezone.utc)

def event():
    return dict(event_id='source',url='https://www.korea.kr/briefing/pressReleaseView.do?newsId=123',
        available_at='2026-11-14T00:00:00+09:00',publication_verified=True,
        availability_basis='date_only_next_local_day',
        body='한국과 중국 외교장관은 다음 주 정상회의를 앞두고 FTA 서비스 협상 진전과 인적교류 확대를 논의하였다.\n'+
             '양국의 협력 추진 과정과 후속 준비 사항에 관한 공식 설명이다. '*30)

def company():
    return dict(country='South Korea',sector='Communication Services',industry='Entertainment',
        business_description='Music production and concerts in South Korea',
        profile_available_at='2026-11-12T00:00:00+09:00')

class AdvanceTests(unittest.TestCase):
    def test_preparation_identifies_exposure_before_outcome_without_price_or_sector_input(self):
        e=event();f=frames([e],AT)
        self.assertEqual(len([r for r in f if r['kind']=='pre_event_policy_watch']),1)
        graph=connect(company(),f,AT)
        self.assertEqual(graph['expectation_links'][0]['stage'],'pre_event')
        self.assertEqual(graph['links'],[])
        self.assertEqual(discover({'ANY_MUSIC_ISSUER':company()},[e],AT),['ANY_MUSIC_ISSUER'])
    def test_unpublished_preparation_and_unrelated_business_are_not_early_signals(self):
        self.assertEqual(frames([event()],datetime(2026,11,13,0,tzinfo=timezone.utc)),[])
        other=company();other.update(industry='Computer Hardware',sector='Technology',business_description='Makes servers in Japan')
        self.assertEqual(discover({'OTHER':other},[event()],AT),[])
    def test_advance_context_requires_unresolved_event_and_no_prior_price_reaction(self):
        c=company();c['event_analysis']={'economic_graph':connect(c,frames([event()],AT),AT)}
        c['price_history']=[dict(close=100,available_at='2026-11-13T16:00:00+09:00')]
        indicators=dict(price=100,atr14_pct=4)
        result=advance_context(c,indicators,AT)
        self.assertTrue(result['eligible'])
        self.assertFalse(result['requires_sector_rally'])
        self.assertFalse(result['requires_ma_uptrend'])
        self.assertFalse(advance_context(c,{**indicators,'price':110},AT)['eligible'])
        c['event_analysis']['economic_graph']['expectation_links'].append(
            dict(stage='announced_expectation',publication_verified=True,
                 available_at='2026-11-15T00:00:00+09:00',matched_regions=['South Korea']))
        self.assertFalse(advance_context(c,indicators,AT)['eligible'])
    def test_policy_index_keeps_first_seen_and_does_not_replace_read_body_with_download_failure(self):
        old={**event(),'first_seen_at':'2026-11-14T01:00:00+09:00'}
        failed={**event(),'body':'','publication_verified':False,'available_at':AT.isoformat()}
        result=merge_index([old],[failed],AT)
        self.assertEqual(result[0]['first_seen_at'],old['first_seen_at'])
        self.assertTrue(result[0]['publication_verified'])
        self.assertGreater(len(result[0]['body']),400)

if __name__=='__main__':unittest.main()
