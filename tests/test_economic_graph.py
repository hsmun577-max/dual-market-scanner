from copy import deepcopy
from datetime import datetime,timedelta,timezone
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from economic_graph import profile,frames,connect,discover,select_analysis_candidates,load_profiles,save_profiles
AT=datetime(2026,11,18,10,tzinfo=timezone.utc)


def company(text='Produces films and online games in South Korea'):
    return dict(company_name='Example Media',business_description=text,country='South Korea',
                profile_available_at=(AT-timedelta(days=1)).isoformat())


def event(text='한국과 중국은 한국 영화와 온라인게임에 최혜국 대우 및 시장 개방을 합의했다.'):
    return dict(event_id='https://www.korea.kr/example',url='https://www.korea.kr/example',
                headline='Bilateral agreement',available_at=(AT-timedelta(minutes=1)).isoformat(),
                body=text+'\n'+'공통 배경 설명 자료입니다. '*50)


def stock(symbol):
    return dict(symbol=symbol,market='KR',name='일반기업',signal={'score':1},indicators=dict(
        price=10000.,volume=2000000.,volume_ratio=2.,ma20=12000.,ma60=15000.,return_5d=-.02,
        return_20d=-.2,relative_strength=-.1,obv_rising=False,breakout=False,breakdown=False,rebound=False,overheated=False))


class EconomicGraphTests(unittest.TestCase):
    def test_customer_use_of_games_does_not_make_a_hardware_issuer_a_game_publisher(self):
        hardware=company('Manufactures video game peripherals and television broadcasting receivers')
        hardware.update(sector='Technology',industry='Computer Hardware')
        self.assertEqual(connect(hardware,frames([event()],AT),AT)['links'],[])

    def test_event_research_capacity_uses_liquidity_not_ticker_alphabet(self):
        small=stock('AAA.KS'); large=stock('ZZZ.KS')
        large['indicators']['volume']=small['indicators']['volume']*10
        self.assertEqual(select_analysis_candidates([small,large],'KR',['AAA.KS','ZZZ.KS'],limit=1),[large])

    def test_market_presence_is_not_eligibility_for_another_countrys_agreement(self):
        foreign=company('Produces online games in China and the United States')
        foreign['country']='United States'
        self.assertEqual(connect(foreign,frames([event()],AT),AT)['links'],[])

    def test_generic_product_region_links_do_not_need_ticker_rules(self):
        registry={name:company() for name in ('NEW_A','NEW_B')}
        self.assertEqual(discover(registry,[event()],AT),['NEW_A','NEW_B'])
        link=connect(company(),frames([event()],AT),AT)['links'][0]
        self.assertEqual(link['matched_products'],['games','screen_content'])
        self.assertFalse(link['quantified'])

    def test_film_access_is_not_automatically_concert_permission(self):
        concert=company('Organizes concerts and live entertainment in South Korea')
        result=connect(concert,frames([event()],AT),AT)
        self.assertEqual(result['links'],[])
        self.assertEqual(result['status'],'expectation_watch_only')
        self.assertEqual(result['expectation_links'][0]['direction'],0)
        self.assertEqual(discover({'NEW_CONCERT':concert},[event()],AT),['NEW_CONCERT'])

    def test_unrelated_region_and_product_do_not_match(self):
        unrelated=company('An airline operating in Japan'); unrelated['country']='Japan'
        self.assertEqual(connect(unrelated,frames([event()],AT),AT)['links'],[])

    def test_negated_policy_does_not_create_positive_discovery(self):
        self.assertEqual(discover({'TEST':company()},[event('한국 영화의 중국 시장 개방은 확정된 것이 아니다.')],AT),[])

    def test_future_events_and_stale_profiles_are_excluded(self):
        e=event(); e['available_at']=(AT+timedelta(seconds=1)).isoformat()
        self.assertEqual(frames([e],AT),[])
        c=company(); c['profile_available_at']=(AT-timedelta(days=181)).isoformat()
        self.assertFalse(profile(c,AT)['usable'])

    def test_weak_chart_is_analyzed_before_buy_eligibility(self):
        row=stock('TEST.KS')
        selected=select_analysis_candidates([row],'KR')
        self.assertEqual(selected,[row])
        from candidate_selection import rank_market_candidates
        self.assertEqual(rank_market_candidates([row],'KR'),[])

    def test_event_slots_are_reserved_above_other_liquid_names(self):
        pool=[stock('Z.KS'),stock('A.KS')]
        self.assertEqual(select_analysis_candidates(pool,'KR',['Z.KS'],limit=1)[0]['symbol'],'Z.KS')

    def test_registry_merge_preserves_profiles_without_secrets(self):
        with TemporaryDirectory() as folder:
            path=str(Path(folder)/'profiles.json')
            one=company(); one['token']='must not persist'
            save_profiles({'A':one},path); save_profiles({'B':company()},path)
            saved=load_profiles(path)
            self.assertEqual(set(saved),{'A','B'})
            self.assertNotIn('token',saved['A'])


if __name__=='__main__': unittest.main()
