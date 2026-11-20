from datetime import datetime,timedelta,timezone
import unittest
from execution_quotes import parse_execution_quote

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

if __name__=='__main__':unittest.main()
