from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch
from execution_quotes import execution_quote, parse_execution_quote

AT=datetime(2026,11,20,11,10,5,tzinfo=timezone(timedelta(hours=9)))

class ExecutionQuoteTests(unittest.TestCase):
    def book(self):
        return dict(output1=dict(new_mkop_cls_code='20',aspr_acpt_hour='111004',askp1='10000',bidp1='9990',
            askp_rsqn1='100',bidp_rsqn1='100'),output2=dict(vi_cls_code='N'))

    def test_provider_clock_and_completed_trade_are_preserved(self):
        book=self.book()
        minutes=dict(output2=[dict(stck_bsop_date='20261120',stck_cntg_hour='111000',cntg_vol='100'),
            dict(stck_bsop_date='20261120',stck_cntg_hour='110900',cntg_vol='200')])
        quote=parse_execution_quote(book,minutes,AT)
        self.assertEqual(quote['observed_at'],AT.replace(second=4).isoformat())
        self.assertEqual(quote['trade_at'],AT.replace(minute=9,second=0).isoformat())

    def test_auction_and_missing_clock_cannot_be_labelled_live(self):
        minutes=dict(output2=[dict(stck_bsop_date='20261120',stck_cntg_hour='110900',cntg_vol='200')])
        for changed in [dict(new_mkop_cls_code='10',aspr_acpt_hour='111004'),dict(new_mkop_cls_code='20')]:
            with self.assertRaises(ValueError):parse_execution_quote(dict(output1=changed),minutes,AT)

    def test_current_unfinished_and_previous_day_bars_are_not_trade_confirmation(self):
        book=self.book()
        minutes=dict(output2=[dict(stck_bsop_date='20261120',stck_cntg_hour='111000',cntg_vol='100'),
            dict(stck_bsop_date='20261119',stck_cntg_hour='110900',cntg_vol='200')])
        with self.assertRaises(ValueError):parse_execution_quote(book,minutes,AT)

    def test_volatility_interruption_is_not_a_continuous_entry(self):
        book=self.book();book['output2']['vi_cls_code']='Y'
        minutes=dict(output2=[dict(stck_bsop_date='20261120',stck_cntg_hour='110900',cntg_vol='200')])
        with self.assertRaises(ValueError):parse_execution_quote(book,minutes,AT)

    def minute_payload(self, day, volume):
        end = AT.replace(second=0)
        return dict(output2=[
            dict(stck_bsop_date=day, stck_cntg_hour=(end - timedelta(minutes=i)).strftime('%H%M%S'),
                 cntg_vol=str(volume if i else 999999))
            for i in range(30)
        ])

    def test_fetch_uses_previous_session_endpoint_and_matches_completed_minutes(self):
        today = self.minute_payload('20261120', 200)
        prior = self.minute_payload('20261119', 100)
        with patch('execution_quotes.raw_get', side_effect=[prior, today, self.book()]) as get, \
             patch('execution_quotes.datetime', wraps=datetime) as clock:
            clock.now.return_value = AT
            quote = execution_quote('005930.KS', AT, object(), compare_volume=True)
        self.assertEqual([call.args[2] for call in get.call_args_list],
                         ['FHKST03010230', 'FHKST03010200', 'FHKST01010200'])
        self.assertEqual(get.call_args_list[0].args[3]['FID_INPUT_DATE_1'], '20261119')
        self.assertEqual(quote['volume_ratio'], 2.0)
        self.assertEqual(quote['volume_window_minutes'], 29)
        self.assertEqual(quote['volume_reference_date'], '2026-11-19')

    def test_partial_previous_window_is_not_treated_as_lower_volume(self):
        today = self.minute_payload('20261120', 200)
        prior = self.minute_payload('20261119', 100)
        prior['output2'].pop(2)
        quote = parse_execution_quote(self.book(), today, AT, previous_minutes=prior)
        self.assertIsNone(quote['volume_ratio'])


if __name__=='__main__':unittest.main()


def test_support_uses_completed_contiguous_minutes_and_needs_a_recovery():
    case = ExecutionQuoteTests()
    minutes = case.minute_payload('20261120', 200)
    for index, row in enumerate(minutes['output2']):
        price = 10000 if 1 <= index <= 5 or index >= 11 else 9850
        row.update(stck_prpr=str(price), stck_lwpr=str(price - 10))
    assert parse_execution_quote(case.book(), minutes, AT)['pullback_support'] == 9840
    minutes['output2'].pop(7)
    assert parse_execution_quote(case.book(), minutes, AT)['pullback_support'] is None
