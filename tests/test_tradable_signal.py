from datetime import datetime,timedelta,timezone
import unittest
from tradable_signal import validate_entry,format_requested_report

AT=datetime(2026,11,20,11,10,tzinfo=timezone(timedelta(hours=9)))

def fixture():
    row=dict(symbol='123456.KS',name='가상 검증종목',preopen_signal=dict(status='conditional_buy',
        expires_session='2026-11-20',entry_limit=10000,reference_price=10000,reference_date='2026-11-19'),
        company_forecast=dict(assessment_status='complete',missing_dimensions=[],composite_score=65,
            generated_at=(AT-timedelta(minutes=1)).isoformat(),base_price=10600,bull_price=11000))
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

    def test_a_caveat_does_not_block_but_a_withheld_score_does(self):
        # 2026-11-25: a dimension that was simply not observed (밸류에이션·이벤트) is a
        # caveat the score already discounts; it no longer refuses a plan the same run
        # published. What still blocks is a withheld score: the integrity gaps in
        # company_forecast.BLOCKING_GAPS.
        r,q=fixture();r['company_forecast'].update(missing_dimensions=['valuation_outlook'],
                                                   caveats=['valuation_outlook'],
                                                   assessment_status='partial')
        signal=validate_entry(r,q,AT,True)
        self.assertTrue(signal['trade_ready'])
        for changes in [dict(composite_score=None),
                        dict(score_withheld_reasons=['current_financial_period']),
                        dict(base_price=None)]:
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
        # Short form (2026-11-25): name, score, price, target, stop, and the 30-second
        # validity of the quote — the long sell-rule paragraph is gone.
        for phrase in ['매수 신호','종합 65.0','매수 10,000원','목표 11,000원','손절 −3%','11:10:30']:
            self.assertIn(phrase,text)

    def test_empty_orderbook_is_not_executable(self):
        r,q=fixture();q['ask_size']=0
        self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

    def test_stop_and_take_profit_use_destination_price_tick(self):
        r,q=fixture();r['preopen_signal']['entry_limit']=19950
        r['company_forecast'].update(base_price=21200,bull_price=22000)
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
        self.assertIn('손절 −3%',format_requested_report([],[s],dict(completed=True),AT))
        r,q=fixture();r['asset_type']='etf';s=validate_entry(r,q,AT,True)
        self.assertIn('손절 −2%',format_requested_report([],[s],dict(completed=True),AT))

    def test_execution_check_preserves_verified_catalyst_policy(self):
        r,q=fixture();r['preopen_signal'].update(strategy='event_catalyst',entry_floor=9900)
        r['company_forecast'].update(base_price=10600,bull_price=10700,catalyst_context=dict(eligible=True))
        self.assertTrue(validate_entry(r,q,AT,True)['trade_ready'])
        r['company_forecast']['catalyst_context']['eligible']=False
        self.assertFalse(validate_entry(r,q,AT,True)['trade_ready'])

    def test_a_closed_session_still_names_the_plan_and_says_it_is_not_a_signal(self):
        # A request after the close can confirm nothing, but the analysis did produce a
        # plan: the message names it, its band and why it is on hold, without ever
        # calling it a buy.
        r,q=fixture();signal=validate_entry(r,q,AT,False)
        self.assertEqual(signal['reason'],'market_closed')
        message=format_requested_report([],[signal],dict(completed=True),AT,plans=[dict(
            symbol='123456.KS',name='가상 검증종목',market='KR',label='실적·호재 촉매',
            entry_limit=10000,entry_floor=9800,composite_score=65.0,
            catalyst_summary='2026년 3분기 영업이익 전년 대비 +253% (DART 실적 공시)',
            hold_reason='market_closed')])
        self.assertIn('조건부 계획',message)
        self.assertIn('매수 신호 아님',message)
        self.assertIn('가상 검증종목 (123456)',message)
        self.assertIn('매수 9,800원~10,000원',message)
        self.assertIn('종합 65.0',message)
        # The session state is said once by the block heading, not under every plan.
        self.assertEqual(message.count('체결 기준 미확인'),1)
        self.assertIn('+253%',message)

    def test_a_plan_without_a_korean_label_shows_its_strategy_in_korean(self):
        from tradable_signal import plan_line
        text = plan_line(dict(symbol='009150.KS', name='삼성전기', market='KR',
                              strategy='earnings_catalyst', entry_limit=778000,
                              entry_floor=732000, composite_score=65.2))
        self.assertIn('[실적·호재 촉매]', text)
        self.assertNotIn('earnings_catalyst', text)

    def test_a_plan_line_carries_no_session_note_at_all(self):
        # The closed session is a fact about the whole block; repeated under five plans
        # it was the loudest thing in the message.
        from tradable_signal import plan_line
        text = plan_line(dict(symbol='009150.KS', name='삼성전기', market='KR',
                              strategy='earnings_catalyst', entry_limit=778000,
                              entry_floor=732000, composite_score=65.2,
                              live_quote=dict(status='market_closed'),
                              hold_reason='market_closed'))
        self.assertEqual(text.count('장 마감'), 0)
        self.assertIn('매수 732,000원~778,000원', text)

    def test_a_bracketed_plan_label_is_not_doubled(self):
        from tradable_signal import plan_line
        text = plan_line(dict(symbol='NKTR', name='넥타', market='US',
                              label='[반등 눌림 후보]', strategy='bottom_rebound',
                              entry_limit=12.5, composite_score=48.0))
        self.assertIn('[반등 눌림 후보]', text)
        self.assertNotIn('[[', text)

    def test_the_plan_list_never_appears_instead_of_a_confirmed_signal(self):
        r,q=fixture();signal=validate_entry(r,q,AT,True)
        message=format_requested_report([],[signal],dict(completed=True),AT,plans=[dict(
            symbol='999999.KS',name='보류종목',market='KR',label='트렌드',entry_limit=5000,
            hold_reason='waiting_for_entry_price')])
        self.assertIn('매수 신호',message)
        self.assertNotIn('보류종목',message)

if __name__=='__main__':unittest.main()
