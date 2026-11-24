from datetime import datetime,timedelta,timezone
import unittest
from tradable_signal import validate_entry,format_requested_report

AT=datetime(2026,11,20,11,10,tzinfo=timezone(timedelta(hours=9)))

def fixture():
    row=dict(symbol='123456.KS',name='가상 검증종목',preopen_signal=dict(status='conditional_buy',
        expires_session='2026-11-20',entry_limit=10000,reference_price=10000,reference_date='2026-11-19'),
        company_forecast=dict(assessment_status='complete',missing_dimensions=[],composite_score=65,
            generated_at=(AT-timedelta(minutes=1)).isoformat(),base_price=10500,bull_price=11000))
    quote=dict(ask=10000,bid=9990,ask_size=100,bid_size=100,observed_at=AT.isoformat(),trade_at=(AT-timedelta(seconds=30)).isoformat())
    return row,quote

class TradableSignalTests(unittest.TestCase):
    def test_complete_contract_has_entry_targets_expiry_and_fixed_risk(self):
        r,q=fixture();s=validate_entry(r,q,AT,True)
        self.assertTrue(s['trade_ready']);self.assertFalse(s['filled'])
        # A single stock is stopped at −3% since the 2026-11-24 instrument split.
        self.assertEqual(s['stop_price'],9700);self.assertEqual(s['take_profit_price'],10600)
        self.assertEqual(s['stop_pct'],0.03);self.assertEqual(s['asset_class'],'stock')
        self.assertEqual(s['valid_until'],(AT+timedelta(seconds=30)).isoformat())

    def test_unavailable_stale_future_and_previous_day_quotes_cannot_be_signals(self):
        for changes in [dict(observed_at=None),dict(observed_at=(AT-timedelta(seconds=11)).isoformat()),
            dict(observed_at=(AT+timedelta(seconds=1)).isoformat()),dict(trade_at=(AT-timedelta(days=1)).isoformat())]:
            r,q=fixture();q.update(changes)
            self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

    def test_incomplete_forecast_and_missing_prices_cannot_be_signals(self):
        for changes in [dict(missing_dimensions=['valuation_outlook']),dict(composite_score=None),dict(base_price=None)]:
            r,q=fixture();r['company_forecast'].update(changes)
            self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

    def test_closed_market_and_expired_plan_are_blocked(self):
        r,q=fixture();self.assertFalse(validate_entry(r,q,AT,False)['trade_ready'])
        r['preopen_signal']['expires_session']='2026-11-19'
        self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

    def test_ask_above_limit_is_waiting_and_never_buy(self):
        r,q=fixture();q.update(ask=10010,bid=10000)
        signal=validate_entry(r,q,AT,True)
        self.assertEqual(signal['reason'],'waiting_for_entry_price')
        text=format_requested_report([], [signal],dict(completed=True),AT)
        self.assertIn('매수 대기',text);self.assertNotIn('매수 신호',text)

    def test_empty_incomplete_report_is_status_not_buy_signal(self):
        text=format_requested_report([],[],dict(completed=False),AT)
        self.assertIn('판정 보류',text);self.assertNotIn('매수 신호',text)

    def test_complete_no_candidates_has_no_false_buy_heading(self):
        self.assertNotIn('매수 신호',format_requested_report([],[],dict(completed=True),AT))

    def test_partial_coverage_does_not_mislabel_completed_rejections_as_unknown(self):
        message=format_requested_report([],[],dict(completed=False,decision_completed=18,data_excluded=26),AT)
        self.assertIn('판정 완료 18종목',message)
        self.assertIn('자료 부족 26종목',message)
        self.assertNotIn('매수 판정 보류',message)
        self.assertNotIn('매수 신호',message)

    def test_only_verified_entry_can_produce_buy_heading(self):
        r,q=fixture();s=validate_entry(r,q,AT,True)
        text=format_requested_report([], [s],dict(completed=True),AT)
        for phrase in ['매수 신호','매수 상한','예상 손절','기본 익절','분석 목표','11:10:30']:self.assertIn(phrase,text)

    def test_empty_orderbook_is_not_executable(self):
        r,q=fixture();q['ask_size']=0
        self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

    def test_stop_and_take_profit_use_destination_price_tick(self):
        r,q=fixture();r['preopen_signal']['entry_limit']=19950
        r['company_forecast'].update(base_price=21000,bull_price=22000)
        q.update(ask=19950,bid=19940)
        signal=validate_entry(r,q,AT,True)
        self.assertEqual(signal['stop_price'],19360)
        self.assertEqual(signal['take_profit_price'],21150)

    def test_a_fund_keeps_the_two_percent_stop_and_a_stock_gets_three(self):
        for fund in (dict(asset_type='etf'),dict(name='KODEX 200'),
                     dict(name='SPDR S&P 500 ETF'),
                     dict(company_analysis=dict(quote_type='ETF'))):
            r,q=fixture();r.update(fund);signal=validate_entry(r,q,AT,True)
            self.assertTrue(signal['trade_ready'])
            self.assertEqual((signal['asset_class'],signal['stop_pct']),('fund',0.02))
            self.assertEqual(signal['stop_price'],9800)

    def test_the_report_names_the_stop_of_each_instrument(self):
        r,q=fixture();s=validate_entry(r,q,AT,True)
        self.assertIn('손절 −3%·익절 +6%',format_requested_report([],[s],dict(completed=True),AT))
        r,q=fixture();r['asset_type']='etf';s=validate_entry(r,q,AT,True)
        self.assertIn('손절 −2%·익절 +6%',format_requested_report([],[s],dict(completed=True),AT))

    def test_execution_check_preserves_verified_catalyst_policy(self):
        r,q=fixture();r['preopen_signal'].update(strategy='event_catalyst',entry_floor=9900)
        r['company_forecast'].update(base_price=10100,bull_price=10700,catalyst_context=dict(eligible=True))
        self.assertTrue(validate_entry(r,q,AT,True)['trade_ready'])
        r['company_forecast']['catalyst_context']['eligible']=False
        self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

if __name__=='__main__':unittest.main()
