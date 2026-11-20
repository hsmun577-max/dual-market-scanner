from datetime import datetime,timedelta,timezone
import unittest
from valuation_data import expand_domestic_peer_valuations

AT=datetime(2026,11,20,2,tzinfo=timezone.utc)

class PeerExpansionTests(unittest.TestCase):
    def inputs(self):
        company=dict(market='KR',country='South Korea',industry='Widgets',company_name='Own',
            source_symbol='000010.KS',trailing_pe=-2,price_to_book=1,valuation_observed_at=AT.isoformat())
        registry={f'{i:06}.KS':dict(country='South Korea',industry='Widgets',company_name=f'Peer {i}',
            profile_available_at=AT.isoformat()) for i in range(20,24)}
        return {'000010':company},registry

    def test_comparables_are_not_restricted_to_chart_shortlist(self):
        c,r=self.inputs()
        def provider(symbols):return {s:dict(price_to_book=3,trailing_pe=-1,
            valuation_observed_at=(AT+timedelta(seconds=5)).isoformat()) for s in symbols}
        expand_domestic_peer_valuations(c,r,AT,provider)
        self.assertEqual(c['000010']['peer_valuation']['metric'],'price_to_book')
        self.assertEqual(c['000010']['peer_valuation']['peer_count'],4)
        self.assertEqual(c['000010']['peer_valuation']['observed_at'],(AT+timedelta(seconds=5)).isoformat())

    def test_wrong_industry_future_profiles_and_unavailable_quotes_are_not_filled(self):
        c,r=self.inputs();r['000020.KS']['industry']='Other'
        r['000021.KS']['profile_available_at']=(AT+timedelta(days=1)).isoformat()
        def provider(symbols):return {s:dict(price_to_book=3,valuation_observed_at=AT.isoformat()) for s in symbols}
        expand_domestic_peer_valuations(c,r,AT,provider)
        self.assertNotIn('peer_valuation',c['000010'])

    def test_same_issuer_different_share_classes_do_not_inflate_peer_count(self):
        c,r=self.inputs()
        for row in r.values():row['company_name']='Same issuer'
        expand_domestic_peer_valuations(c,r,AT,lambda symbols:{s:dict(price_to_book=3,
            valuation_observed_at=AT.isoformat()) for s in symbols})
        self.assertNotIn('peer_valuation',c['000010'])

    def test_future_quote_cannot_be_backdated_into_historical_valuation(self):
        c,r=self.inputs()
        expand_domestic_peer_valuations(c,r,AT,lambda symbols:{s:dict(price_to_book=3,
            valuation_observed_at=(AT+timedelta(days=1)).isoformat()) for s in symbols})
        self.assertNotIn('peer_valuation',c['000010'])

if __name__=='__main__':unittest.main()
