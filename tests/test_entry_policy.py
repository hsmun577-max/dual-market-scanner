import unittest

from entry_policy import reward_check, score_floor


STOCK = {'symbol': '005930.KS', 'asset_type': 'stock_or_etf'}
FUND = {'symbol': '069500.KS', 'asset_type': 'etf'}


class EntryPolicyTests(unittest.TestCase):
    def test_score_floors_by_instrument_and_strategy(self):
        self.assertEqual(score_floor(STOCK), 56)
        self.assertEqual(score_floor(FUND), 53)
        self.assertEqual(score_floor(STOCK, 'early_ma_alignment'), 53)
        self.assertEqual(score_floor(STOCK, 'earnings_catalyst'), 50)
        self.assertEqual(score_floor(STOCK, 'event_catalyst'), 56)

    def test_minimum_base_reward_tracks_the_stop_and_round_trip_cost(self):
        for row, target, minimum in [(STOCK, 105.25, 5.25), (FUND, 103.75, 3.75)]:
            with self.subTest(row=row):
                check = reward_check(row, 100., target)
                self.assertTrue(check['allowed'])
                self.assertEqual(check['minimum_gross_reward_pct'], minimum)
                self.assertFalse(reward_check(row, 100., target - .01)['allowed'])

    def test_wide_spread_reduces_net_reward_and_the_profit_cap_is_real(self):
        self.assertTrue(reward_check(STOCK, 100., 106., 99.9)['allowed'])
        wide = reward_check(STOCK, 100., 110., 99.5)
        self.assertEqual(wide['assumed_round_trip_cost_pct'], .6)
        self.assertEqual(wide['gross_reward_pct'], 6.)
        self.assertEqual(wide['net_reward_risk'], 1.5)
        self.assertFalse(reward_check(STOCK, 100., 110., 99.49)['allowed'])
        self.assertFalse(reward_check(STOCK, 100., None)['allowed'])


if __name__ == '__main__':
    unittest.main()
