import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import headwind_watch
from headwind_watch import format_block, format_line, observation, qualifies, select
from preopen_signals import evaluate_preopen
from scanner import send_telegram_summary

AT = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)


def stock_row(symbol='352820.KS', name='하이브', market='KR'):
    return dict(symbol=symbol, market=market, name=name,
                signal=dict(confirmed=False, score=0),
                indicators=dict(price=10000, volume=2000000, volume_ratio=2.4, return_1d=.032,
                                average_turnover_20d=20000000000, ma20=9900, ma60=9800,
                                return_5d=.03, return_20d=.15, relative_strength=.124,
                                obv_rising=True, breakout=True, data_date='2026-11-18',
                                market_above_ma20=False),
                company_analysis=dict(event_analysis=dict(economic_graph={})),
                company_forecast=dict(assessment_status='complete', generated_at=AT.isoformat(),
                                      composite_score=70, base_price=10600, bull_price=11000,
                                      missing_dimensions=[]))


def report(rows):
    return dict(results=rows, preopen_session='2026-11-19', sectors=[])


class QualifiesTests(unittest.TestCase):
    """D안의 조건 자체: 개별주 + 20일 RS ≥ +10%p + 거래량 ≥ 2.0배."""

    def test_the_boundary_is_inside(self):
        row = stock_row()
        row['indicators'].update(relative_strength=0.10, volume_ratio=2.0)
        self.assertTrue(qualifies(row))

    def test_just_below_either_threshold_is_out(self):
        for field, value in (('relative_strength', 0.0999), ('volume_ratio', 1.999)):
            row = stock_row()
            row['indicators'][field] = value
            self.assertFalse(qualifies(row), field)

    def test_products_are_not_individual_stocks(self):
        # 같은 필터를 쓴 백테스트에서 ETF는 7건 0승 7패였다 — 상품은 관찰 후보로도 올리지 않는다.
        for symbol, name in (('069500.KS', 'KODEX 200'), ('SQQQ', 'ProShares UltraPro Short QQQ'),
                             ('233740.KS', 'KODEX 코스닥150레버리지')):
            market = 'US' if symbol == 'SQQQ' else 'KR'
            self.assertFalse(qualifies(stock_row(symbol, name, market)), symbol)

    def test_missing_or_boolean_values_are_not_read_as_strong(self):
        for relative, volume in ((None, 2.4), (0.124, None), (True, 2.4), (0.124, True),
                                 (float('nan'), 2.4), (0.124, float('inf'))):
            row = stock_row()
            row['indicators'].update(relative_strength=relative, volume_ratio=volume)
            self.assertFalse(qualifies(row), (relative, volume))

    def test_an_unknown_market_is_not_watched(self):
        row = stock_row(market='JP')
        self.assertFalse(qualifies(row))


class EvaluateTests(unittest.TestCase):
    """Relative strength under a falling index stays visible when there is no buy plan."""

    def test_a_strong_name_under_a_falling_index_can_be_watched_after_a_score_rejection(self):
        row = stock_row()
        row['company_forecast']['composite_score'] = 50
        result = evaluate_preopen(report([row]), '2026-11-18', AT)
        self.assertEqual(result['recommendations'], [])
        self.assertEqual(result['counts'].get('composite_score_below_policy'), 1)
        self.assertEqual(row['preopen_decision'], 'composite_score_below_policy')
        self.assertNotIn('preopen_signal', row)
        self.assertEqual(len(result['headwind_watch']), 1)
        item = result['headwind_watch'][0]
        self.assertEqual((item['symbol'], item['gate']), ('352820.KS', 'market_headwind'))
        self.assertEqual(item['volume_ratio'], 2.4)
        self.assertEqual(item['relative_strength'], 0.124)
        self.assertEqual(item['reference_session'], '2026-11-18')

    def test_a_weak_name_held_by_the_headwind_is_not_listed(self):
        row = stock_row()
        row['indicators']['relative_strength'] = .05
        row['company_forecast']['composite_score'] = 50
        result = evaluate_preopen(report([row]), '2026-11-18', AT)
        self.assertEqual(row['preopen_decision'], 'composite_score_below_policy')
        self.assertEqual(result['headwind_watch'], [])

    def test_a_name_that_never_reaches_the_gate_is_not_listed(self):
        # 거래량 미달처럼 자기 차트에서 먼저 걸린 종목은 '게이트 때문에 보류'된 것이 아니다.
        row = stock_row()
        row['indicators']['volume_ratio'] = 1.5
        result = evaluate_preopen(report([row]), '2026-11-18', AT)
        self.assertEqual(row['preopen_decision'], 'insufficient_demand_expansion')
        self.assertEqual(result['headwind_watch'], [])

    def test_a_name_that_gets_a_plan_is_not_a_watch_candidate(self):
        row = stock_row()
        row['indicators']['market_above_ma20'] = True
        result = evaluate_preopen(report([row]), '2026-11-18', AT)
        self.assertEqual(result['recommendations'], ['352820.KS'])
        self.assertEqual(result['headwind_watch'], [])

    def test_the_watch_list_survives_the_multi_market_merge(self):
        kr = stock_row()
        kr['company_forecast']['composite_score'] = 50
        us = stock_row('NVDA', 'NVIDIA', 'US')
        us['indicators'].update(price=180.0, ma20=175.0, ma60=170.0, data_date='2026-11-18')
        us['company_forecast'].update(generated_at=AT.isoformat(), composite_score=50)
        merged = dict(results=[kr, us], preopen_session='2026-11-19', sectors=[])
        from preopen_signals import evaluate_preopen_markets
        with patch('preopen_signals.session_dates', return_value={'KR': '2026-11-18',
                                                                  'US': '2026-11-18'}):
            evaluate_preopen_markets(merged, AT)
        markets = {item['market'] for item in merged['buy_evaluation']['headwind_watch']}
        self.assertEqual(markets, {'KR', 'US'})
        self.assertEqual(merged['buy_evaluation']['recommendations'], [])


class FormatTests(unittest.TestCase):
    def item(self, **overrides):
        row = stock_row()
        row['indicators'].update(overrides.pop('indicators', {}))
        row.update(overrides)
        return observation(row)

    def test_the_block_names_itself_and_shows_the_watch_numbers_only(self):
        lines = format_block({'KR': [self.item()]}, 'KR')
        self.assertEqual(lines[0], '[하락장 관찰 후보] · 매수 신호 아님')
        self.assertIn('하락장 관찰 후보', headwind_watch.HEADING)
        self.assertIn('• 하이브 (352820) · +3.2% · RS +12.4%p · 거래량 2.40배 · 10,000원 (11/18 종가)',
                      lines)
        self.assertEqual(lines[-1], headwind_watch.NOTE)
        # 진입가·손절·목표가 같은 계획 형식은 싣지 않는다.
        joined = '\n'.join(lines)
        for word in ('진입', '손절', '목표'):
            self.assertNotIn(word, joined)

    def test_a_foreign_price_uses_dollars(self):
        item = self.item(symbol='NVDA', name='NVIDIA', market='US')
        item['reference_price'] = 180.0
        self.assertIn('$180.00', format_line(item))

    def test_the_block_filters_to_the_market_it_is_sent_to(self):
        items = {'KR': [self.item()], 'US': [self.item(symbol='NVDA', name='NVIDIA', market='US')]}
        kr = format_block(items, 'KR')
        self.assertIn('하이브', '\n'.join(kr))
        self.assertNotIn('NVIDIA', '\n'.join(kr))
        self.assertNotIn('[미국]', kr)

    def test_more_than_five_names_are_counted_not_silently_dropped(self):
        rows = []
        for i in range(7):
            row = stock_row(f'00000{i}.KS', f'종목{i}')
            rows.append(row)
        rows = [observation(row) for row in rows]
        lines = format_block({'KR': rows}, 'KR')
        self.assertEqual(sum(1 for line in lines if line.startswith('• ')), 5)
        self.assertTrue(any('외 2건' in line for line in lines))

    def test_an_empty_list_sends_nothing(self):
        self.assertEqual(format_block([], 'KR'), [])
        self.assertEqual(format_block(None, 'KR'), [])

    def test_select_reads_a_stored_report_and_ranks_by_strength(self):
        strong = stock_row('000001.KS', '강한종목')
        weaker = stock_row('000002.KS', '약한종목')
        weaker['indicators']['relative_strength'] = 0.105
        planned = stock_row('000003.KS', '계획종목')
        for row in (strong, weaker, planned):
            row['preopen_decision'] = 'market_headwind'
        planned['preopen_decision'] = 'conditional_buy'
        chosen = select(dict(results=[strong, weaker, planned]))
        self.assertEqual([item['symbol'] for item in chosen['KR']], ['000001.KS', '000002.KS'])


class MessageTests(unittest.TestCase):
    def test_the_telegram_message_carries_the_watch_block(self):
        row = stock_row()
        row['company_forecast']['composite_score'] = 50
        message = report([row])
        evaluate_preopen(message, '2026-11-18', AT)
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake',
                                       'DELIVERY_MARKET': 'KRX'}), patch(
                'scanner.delivery_status', return_value=(True, 'test')), patch(
                'scanner.requests.post') as post:
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            send_telegram_summary(message)
        text = post.call_args.kwargs['json']['text']
        self.assertIn('[하락장 관찰 후보] · 매수 신호 아님', text)
        self.assertIn('하이브 (352820)', text)
        # 매수 후보 자리에는 여전히 아무것도 없다.
        self.assertIn('매수 조건 충족 없음', text)

    def test_a_message_without_watch_candidates_keeps_the_block_out(self):
        row = stock_row()
        row['indicators']['market_above_ma20'] = True
        message = report([row])
        evaluate_preopen(message, '2026-11-18', AT)
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake',
                                       'DELIVERY_MARKET': 'KRX'}), patch(
                'scanner.delivery_status', return_value=(True, 'test')), patch(
                'scanner.requests.post') as post:
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            send_telegram_summary(message)
        self.assertNotIn('하락장 관찰 후보', post.call_args.kwargs['json']['text'])


if __name__ == '__main__':
    unittest.main()
