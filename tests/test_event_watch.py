from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch
from kis_intraday import event_watch_symbols
AT=datetime(2026,11,18,13,tzinfo=timezone.utc)


def records(exchange,source):
    company=dict(company_name='New Content Firm',business_description='Produces films and online games in South Korea',
        country='South Korea',exchange=exchange,source_symbol=source,
        profile_available_at=(AT-timedelta(days=1)).isoformat())
    event=dict(event_id='https://www.korea.kr/e',url='https://www.korea.kr/e',
        available_at=(AT-timedelta(minutes=1)).isoformat(),
        body='한국과 중국은 한국 영화와 온라인게임에 최혜국 대우와 시장 개방을 합의했다.\n'+'정책 배경 자료입니다. '*50)
    return company,event


class EventWatchTests(unittest.TestCase):
    def test_first_seen_during_live_fetch_is_eligible_after_fetch(self):
        company,event=records('NMS','NEW')
        event['available_at']=(AT+timedelta(minutes=1)).isoformat()
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return (AT+timedelta(minutes=2)).astimezone(tz)
        with patch('event_impact_analysis.world_events',return_value=([event],[])), patch('kis_intraday.datetime',Clock):
            self.assertEqual(event_watch_symbols('US-late',AT,{'NEW':company}),['DNASNEW'])

    def test_exchange_identifier_comes_from_issuer_exchange(self):
        for exchange,prefix in [('NMS','DNAS'),('NYQ','DNYS'),('ASE','DAMS')]:
            company,event=records(exchange,'NEW')
            self.assertEqual(event_watch_symbols('US-late',AT,{'NEW':company},[event]),[prefix+'NEW'])

    def test_unknown_exchange_is_not_guessed(self):
        company,event=records('UNKNOWN','NEW')
        self.assertEqual(event_watch_symbols('US-late',AT,{'NEW':company},[event]),[])

    def test_korean_exchange_suffix_is_preserved_in_name_mapping(self):
        from kis_intraday import KR_SYMBOL_NAMES
        company,event=records('KOE','123456.KQ')
        self.assertEqual(event_watch_symbols('KRX',AT,{'123456.KQ':company},[event]),['123456'])
        self.assertIn('123456.KQ',KR_SYMBOL_NAMES)

    def test_korean_company_is_not_subscribed_as_a_us_symbol(self):
        company,event=records('KOE','123456.KQ')
        self.assertEqual(event_watch_symbols('US-late',AT,{'123456.KQ':company},[event]),[])


if __name__=='__main__': unittest.main()
