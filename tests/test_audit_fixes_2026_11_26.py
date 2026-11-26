"""2026-11-26 전수점검에서 고친 결함의 회귀 테스트.

점검 노트: 다섯 항목 중 코드로 닫은 네 가지 —
1. 국내 종목은 상장 시장의 지수(코스피/코스닥)로 판정되고, 지수 기준일이 종목 봉에 맞춰진다.
2. 메시지의 '목표'는 매도 규칙(+6% · 5거래일)이고 예측 상승 시나리오는 참고로만 붙는다.
3. 상위 5건만 실릴 때 빠진 종목 수를 알린다.
4. 규칙 기반 가설(event_support)이 추세 확인 두 사유를 통째로 우회하지 못한다.
"""
import unittest
from datetime import datetime, timezone

import pandas as pd

import scanner
from candidate_selection import EVENT_SUPPORT_LIFTABLE_REASONS, index_symbol_for


def frame(closes, volumes=None, end='2026-11-25'):
    index = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({'Open': closes, 'High': [value * 1.01 for value in closes],
                         'Low': [value * 0.99 for value in closes], 'Close': closes,
                         'Volume': volumes or [100000.0] * len(closes)}, index=index)


class IndexSelectionTests(unittest.TestCase):
    def test_a_kosdaq_listing_is_judged_by_the_kosdaq_index(self):
        self.assertEqual(index_symbol_for('277810.KQ', 'KR'), '^KQ11')
        self.assertEqual(index_symbol_for('005930.KS', 'KR'), '^KS11')
        self.assertEqual(index_symbol_for('069500.KS', 'KR'), '^KS11')
        self.assertEqual(index_symbol_for('AAPL', 'US'), 'SPY')

    def test_scan_uses_the_listings_own_index_for_the_regime(self):
        # 코스피는 20일선 아래, 코스닥은 위. 같은 날 두 종목의 국면 판정이 달라야 한다.
        selection_now = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
        prices = {
            '277810.KQ': frame([50000.0] * 64 + [52000.0]),
            '005930.KS': frame([70000.0] * 64 + [71000.0]),
            '^KS11': frame([2500.0] * 60 + [2400.0] * 5),
            '^KQ11': frame([800.0] * 60 + [820.0] * 5),
        }
        benchmarks = {'^KS11': prices['^KS11'], '^KQ11': prices['^KQ11']}
        found, errors = scanner.scan_market(
            'KR', ['277810.KQ', '005930.KS'], benchmarks,
            names={'277810.KQ': '레인보우로보틱스', '005930.KS': '삼성전자'},
            prices=prices, selection_now=selection_now)
        self.assertEqual(errors, [])
        by_symbol = {row['symbol']: row['indicators'] for row in found}
        self.assertTrue(by_symbol['277810.KQ']['market_above_ma20'])
        self.assertFalse(by_symbol['005930.KS']['market_above_ma20'])
        # 코스닥 20일선: 15×800 + 5×820 → 805, 코스피 20일선: 15×2500 + 5×2400 → 2,475
        self.assertAlmostEqual(by_symbol['277810.KQ']['market_ma20'], 805.0)
        self.assertAlmostEqual(by_symbol['005930.KS']['market_ma20'], 2475.0)


class RegimeDateTests(unittest.TestCase):
    def stock(self):
        return frame([10000.0] * 69 + [10100.0], end='2026-11-25')

    def test_a_newer_index_bar_does_not_move_the_twenty_day_average(self):
        # 지수에 아직 끝나지 않은 11/26 봉이 섞여 들어오면 20일선이 오늘 값으로 오염된다.
        index_with_partial = frame([2500.0] * 20 + [2400.0] * 5, end='2026-11-26')
        data = scanner.clean_prices(self.stock())
        with_partial = scanner.calculate_indicators(data, index_with_partial)
        without = scanner.calculate_indicators(data, index_with_partial.iloc[:-1])
        self.assertEqual(with_partial['market_ma20'], without['market_ma20'])
        self.assertEqual(with_partial['market_ma20_date'], '2026-11-25')
        self.assertEqual(with_partial['market_lag_sessions'], 0)

    def test_an_index_one_session_behind_says_so_instead_of_looking_current(self):
        # 2026-11-26 발송본: 지수가 11/25 봉을 건너뛰어 20일선이 11/24 기준이었다.
        index_lagging = frame([2500.0] * 20 + [2400.0] * 4, end='2026-11-24')
        data = scanner.clean_prices(self.stock())
        values = scanner.calculate_indicators(data, index_lagging)
        self.assertEqual(values['market_ma20_date'], '2026-11-24')
        self.assertEqual(values['market_lag_sessions'], 1)
        self.assertFalse(values['market_above_ma20'])

    def test_the_message_names_the_lagging_index_basis(self):
        rows = dict(
            market_regime={'KR': dict(index_symbol='^KS11', above_ma20=False,
                                      index_date='2026-11-24', lag_sessions=1,
                                      headwind_skipped=2, observed=True,
                                      indices={'^KS11': dict(above_ma20=False),
                                               '^KQ11': dict(above_ma20=True)})})
        note = '\n'.join(scanner.market_regime_note(rows, 'KR'))
        self.assertIn('시장 국면(KOSPI): 하락 국면', note)
        self.assertIn('코스닥 상승 국면', note)
        self.assertIn('지수 기준일 11/24(1세션 지연)', note)


class PlanTargetTests(unittest.TestCase):
    def row(self, bull=1045793.0, strategy='earnings_catalyst'):
        return dict(market='KR', symbol='009150.KS', name='삼성전기',
                    indicators=dict(price=755000.0, data_date='2026-11-25'),
                    company_forecast=dict(composite_score=60.9, base_price=800000.0,
                                          bull_price=bull),
                    preopen_signal=dict(status='conditional_buy', strategy=strategy,
                                        entry_limit=778000, entry_floor=732000,
                                        reference_date='2026-11-25',
                                        take_profit_pct=6, stop_pct=3))

    def test_the_target_is_the_sell_rule_and_the_scenario_is_only_a_reference(self):
        line = scanner.telegram_candidate_line(self.row())
        self.assertIn('목표 +6%(5거래일)', line)
        self.assertIn('강세 참고 1,045,793원', line)
        self.assertNotIn('목표 1,045,793원', line)

    def test_a_plain_trend_plan_also_states_the_sell_rule(self):
        line = scanner.telegram_candidate_line(self.row(bull=900000.0, strategy='trend'))
        self.assertIn('목표 +6%(5거래일)', line)
        self.assertIn('강세 참고 900,000원', line)


class TruncationTests(unittest.TestCase):
    def report(self, count):
        rows = []
        for index in range(count):
            rows.append(dict(market='KR', symbol=f'00000{index}.KS', name=f'종목{index}',
                             indicators=dict(price=10000.0, volume_ratio=1.0),
                             company_forecast=dict(composite_score=70.0 - index),
                             preopen_signal=dict(status='conditional_buy', strategy='trend',
                                                 entry_limit=10000, entry_floor=9000,
                                                 reference_date='2026-11-25')))
        return dict(results=rows, sectors=[], theme_sectors=[], market_regime={},
                    preopen_session='2026-11-26',
                    buy_evaluation=dict(completed=True, decision_completed=count))

    def test_more_than_five_plans_say_how_many_are_missing(self):
        from telegram_text import rest_note
        self.assertEqual(rest_note(6, 5), '외 1건')
        self.assertEqual(rest_note(8, 5), '외 3건')
        self.assertEqual(rest_note(5, 5), '')

    def test_the_preopen_message_names_the_plan_it_did_not_show(self):
        from unittest.mock import Mock, patch
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake',
                                       'DELIVERY_MARKET': 'KRX'}), \
                patch('scanner.delivery_status', return_value=(True, 'test')), \
                patch('scanner.requests.post') as post:
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            scanner.send_telegram_summary(self.report(6))
        text = post.call_args.kwargs['json']['text']
        self.assertIn('외 1건', text)


class EventSupportScopeTests(unittest.TestCase):
    def test_the_hypothesis_cannot_lift_the_two_trend_refusals(self):
        self.assertNotIn('unconfirmed_uptrend', EVENT_SUPPORT_LIFTABLE_REASONS)
        self.assertNotIn('weak_relative_trend', EVENT_SUPPORT_LIFTABLE_REASONS)
        self.assertIn('insufficient_demand_expansion', EVENT_SUPPORT_LIFTABLE_REASONS)


if __name__ == '__main__':
    unittest.main()
