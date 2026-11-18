from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
from profile_universe import refresh,merge_profiles
AT=datetime.now(timezone.utc)


class ProfileIndexTests(unittest.TestCase):
    def test_empty_provider_responses_are_not_unsupported_stocks(self):
        entries=[(str(i),str(i)) for i in range(20)]
        profiles,status=refresh(entries,{},lambda symbol:{},AT)
        self.assertEqual(len(status),10)
        self.assertTrue(all(value=='provider_data_unavailable' for value in status.values()))
        self.assertEqual(profiles,{})

    def test_bootstrap_korean_symbol_uses_official_exchange_suffix(self):
        with TemporaryDirectory() as folder:
            source=Path(folder)/'sample.json'; output=Path(folder)/'merged.json'
            source.write_text(json.dumps({'123456':dict(profile_available_at=AT.isoformat())}))
            with patch('profile_universe.load_universe',return_value={'KR':{'123456.KQ':'회사'}}):
                merge_profiles([source],str(output))
            result=json.loads(output.read_text())
            self.assertEqual(result['123456.KQ']['source_symbol'],'123456.KQ')

    def test_fresh_profile_is_reused_without_fetch(self):
        old=dict(business_description='existing description',profile_available_at=AT.isoformat())
        def fetch(symbol): raise AssertionError('network must not be called')
        profiles,status=refresh([('A','A')],{'A':old},fetch,AT)
        self.assertEqual(status,{'A':'cached'})
        self.assertEqual(profiles['A'],old)

    def test_failed_refresh_does_not_relabel_old_data_fresh(self):
        old=dict(business_description='existing description',profile_available_at=(AT-timedelta(days=100)).isoformat())
        def fetch(symbol): raise RuntimeError('unavailable')
        profiles,status=refresh([('A','A')],{'A':old},fetch,AT)
        self.assertEqual(profiles['A']['profile_available_at'],old['profile_available_at'])
        self.assertTrue(status['A'].startswith('provider_error'))

    def test_missing_and_unsupported_are_distinct(self):
        data={'A':{'quoteType':'ETF'},'B':{'quoteType':'EQUITY'},
              'C':{'quoteType':'EQUITY','longBusinessSummary':'description '*30,'country':'South Korea'}}
        profiles,status=refresh([(x,x) for x in data],{},data.get,AT)
        self.assertEqual(status['A'],'unsupported_instrument')
        self.assertEqual(status['B'],'missing_business_description')
        self.assertEqual(status['C'],'refreshed')
        self.assertEqual(set(profiles),{'C'})

    def test_merge_keeps_newer_evidence_and_canonical_symbol(self):
        with TemporaryDirectory() as folder:
            a=Path(folder)/'a.json'; b=Path(folder)/'b.json'; out=Path(folder)/'combined.json'
            a.write_text(json.dumps({'DNASA':dict(source_symbol='A',profile_available_at=AT.isoformat(),value='new')}))
            b.write_text(json.dumps({'A':dict(source_symbol='A',profile_available_at=(AT-timedelta(days=1)).isoformat(),value='old')}))
            self.assertEqual(merge_profiles([a,b],str(out)),1)
            self.assertEqual(json.loads(out.read_text())['A']['value'],'new')


if __name__=='__main__': unittest.main()
