"""가격 오류 필터: PARA +9,614% 같은 행이 테마 등락률·대표주·상승 비율에 섞이지 않는지 확인한다."""
import unittest
from datetime import datetime

import pandas as pd

import price_sanity
from theme_sectors import format_theme_block, member_metric, rank_theme_sectors, theme_rows, theme_sector_snapshot

NOW = datetime(2026, 11, 23, 21, 0)


def frame(closes, volumes=None, end='2026-11-23'):
    index = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({'Close': closes, 'Volume': volumes or [100.0] * len(closes)}, index=index)


def metric(move, price=100.0, turnover_ratio=1.0, turnover=100.0, data_date='2026-11-23'):
    return dict(return_pct=move, price=price, turnover_ratio=turnover_ratio, turnover=turnover,
                data_date=data_date, intraday=False, volume_ratio=1.0)


ENTERTAINMENT = ("엔터·미디어", (("NFLX", "넷플릭스"), ("PARA", "파라마운트"), ("RBLX", "로블록스")))


class RejectionRuleTests(unittest.TestCase):
    def test_a_daily_move_beyond_the_hard_cap_is_a_data_error(self):
        self.assertEqual(price_sanity.rejection_reason('US', 9614.29, 2.72), 'abnormal_change')
        self.assertEqual(price_sanity.rejection_reason('US', -96.0, 2.72), 'abnormal_change')
        self.assertIsNone(price_sanity.rejection_reason('US', 31.48, 180.0))

    def test_korean_moves_beyond_the_price_limit_are_data_errors(self):
        self.assertEqual(price_sanity.rejection_reason('KR', 31.0, 50000), 'price_limit_exceeded')
        self.assertEqual(price_sanity.rejection_reason('KR', -31.0, 50000), 'price_limit_exceeded')
        self.assertIsNone(price_sanity.rejection_reason('KR', 29.9, 50000))

    def test_a_penny_priced_row_is_a_data_error(self):
        self.assertEqual(price_sanity.rejection_reason('US', 8967.0, 0.03), 'abnormal_change')
        self.assertEqual(price_sanity.rejection_reason('US', None, 0.03), 'low_price')
        self.assertEqual(price_sanity.rejection_reason('KR', None, 500), 'low_price')
        self.assertIsNone(price_sanity.rejection_reason('US', None, 12.5))

    def test_a_nonpositive_price_is_refused(self):
        self.assertEqual(price_sanity.rejection_reason('US', None, 0), 'abnormal_price')
        self.assertEqual(price_sanity.rejection_reason('US', None, -3), 'abnormal_price')

    def test_nothing_to_check_means_no_verdict(self):
        self.assertIsNone(price_sanity.rejection_reason('US'))
        self.assertIsNone(price_sanity.rejection_reason('KR', None, None))


class MemberMetricGuardTests(unittest.TestCase):
    def test_a_ticker_switch_is_refused_instead_of_averaged(self):
        # PARAMOUNT was re-tickered; the provider stitched a 3-cent shell onto it.
        data = frame([0.04] * 21 + [2.72])
        with self.assertRaises(price_sanity.PriceError) as caught:
            member_metric(data, NOW.date(), 'US')
        self.assertEqual(caught.exception.reason, 'abnormal_change')

    def test_a_normal_member_still_reports_its_price(self):
        found = member_metric(frame([180.0] * 21 + [188.0]), NOW.date(), 'US')
        self.assertAlmostEqual(found['return_pct'], 4.44, places=2)
        self.assertEqual(found['price'], 188.0)

    def test_a_market_less_call_keeps_only_the_hard_cap(self):
        self.assertEqual(member_metric(frame([50.0] * 21 + [52.0]), NOW.date())['return_pct'], 4.0)
        with self.assertRaises(price_sanity.PriceError):
            member_metric(frame([1.0] * 21 + [3.0]), NOW.date())


class ThemeAggregationGuardTests(unittest.TestCase):
    def test_the_para_row_no_longer_moves_the_theme(self):
        metrics = {'NFLX': metric(2.41, 58.1), 'PARA': metric(9614.29, 2.72), 'RBLX': metric(10.55, 51.65)}
        row = theme_rows('US', (ENTERTAINMENT,), metrics, 4.71)[0]
        self.assertAlmostEqual(row['return_pct'], 6.48, places=2)  # (2.41 + 10.55) / 2
        self.assertEqual((row['up'], row['total'], row['declared']), (2, 2, 3))
        self.assertEqual(row['excluded'], 1)
        self.assertNotIn('파라마운트', [leader['name'] for leader in row['leaders']])
        self.assertEqual([leader['name'] for leader in row['leaders']], ['로블록스', '넷플릭스'])

    def test_a_theme_whose_only_quotes_are_broken_is_not_reported(self):
        metrics = {'NFLX': metric(9614.29, 0.03), 'PARA': metric(-96.0, 2.72)}
        self.assertEqual(theme_rows('US', (ENTERTAINMENT,), metrics, 1.0), [])

    def test_a_penny_row_cannot_become_a_leader(self):
        metrics = {'NFLX': metric(1.0, 58.1), 'PARA': metric(None, 0.4), 'RBLX': metric(2.0, 51.65)}
        row = theme_rows('US', (ENTERTAINMENT,), metrics, 1.0)[0]
        self.assertEqual(row['total'], 2)
        self.assertNotIn('파라마운트', [leader['name'] for leader in row['leaders']])


class SnapshotGuardTests(unittest.TestCase):
    def test_a_real_frame_with_a_ticker_switch_is_excluded_and_reported(self):
        rendered = {
            'NFLX': frame([58.0] * 21 + [58.1]),
            'CMCSA': frame([0.04] * 21 + [2.72]),
            'RBLX': frame([46.0] * 21 + [51.65]),
            'SPY': frame([700.0] * 21 + [722.29]),
        }
        snapshot = theme_sector_snapshot('US', NOW, downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        names = [row['name'] for row in snapshot['rows']]
        self.assertIn('엔터·미디어', names)
        row = next(row for row in snapshot['rows'] if row['name'] == '엔터·미디어')
        self.assertAlmostEqual(row['return_pct'], 6.22, places=2)  # (0.17 + 12.28) / 2
        self.assertLess(row['return_pct'], 100)
        self.assertEqual(snapshot['excluded'], [dict(symbol='CMCSA', reason='abnormal_change',
                                                     label='비정상 등락')])
        self.assertEqual(snapshot['quoted'], 2)  # CMCSA는 제외, SPY는 벤치마크
        text = '\n'.join(format_theme_block(snapshot, 'US'))
        self.assertNotIn('컴캐스트', text)
        self.assertNotIn('9614', text)

    def test_a_healthy_dataset_reports_no_exclusions(self):
        rendered = {
            'NFLX': frame([58.0] * 21 + [58.1]),
            'CMCSA': frame([80.0] * 21 + [82.0]),
            'SPY': frame([700.0] * 21 + [722.29]),
        }
        snapshot = theme_sector_snapshot('US', NOW, downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        self.assertEqual(snapshot['excluded'], [])
        self.assertEqual(snapshot['quoted'], 2)

    def test_a_korean_penny_row_does_not_reach_a_ranked_theme(self):
        from theme_sectors import KR_THEMES
        robots = dict(KR_THEMES)['휴머노이드·로봇·액추에이터']
        rendered = {
            robots[0][0]: frame([5000.0] * 21 + [5200.0]),
            robots[1][0]: frame([900.0] * 21 + [950.0]),
            robots[2][0]: frame([3000.0] * 21 + [2950.0]),
            '^KS11': frame([2500.0] * 21 + [2417.25]),
        }
        snapshot = theme_sector_snapshot('KR', NOW, downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        self.assertEqual([row['symbol'] for row in snapshot['excluded']], [robots[1][0]])
        row = rank_theme_sectors(snapshot['rows'], limit=1)[0]
        symbols = [leader['symbol'] for leader in row['leaders']]
        self.assertEqual(symbols[0], robots[0][0])
        self.assertNotIn(robots[1][0], symbols)


if __name__ == '__main__':
    unittest.main()
