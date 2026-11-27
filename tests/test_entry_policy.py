import unittest

from entry_policy import MIN_NET_REWARD_RISK, entry_policy_snapshot, minimum_gross_reward, reward_check, score_floor


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
        # 2026-11-27: the floor is 1.42, so a stock needs 4.986% and an ETF 3.566% (shown as 4.99% / 3.57%).
        for row, target, minimum in [(STOCK, 104.99, 4.986), (FUND, 103.57, 3.566)]:
            with self.subTest(row=row):
                check = reward_check(row, 100., target)
                self.assertTrue(check['allowed'])
                self.assertEqual(check['minimum_gross_reward_pct'], minimum)
                self.assertFalse(reward_check(row, 100., target - .01)['allowed'])

    def test_five_percent_base_reward_is_not_rejected(self):
        """혜선 님 \"기대수익 5프로여도 배제하지마\" (2026-11-27): 5.0%는 net 1.4242로 통과한다."""
        self.assertEqual(MIN_NET_REWARD_RISK, 1.42)
        check = reward_check(STOCK, 100., 105.)
        self.assertTrue(check['allowed'])
        self.assertEqual(check['gross_reward_pct'], 5.)
        self.assertEqual(check['net_reward_risk'], 1.4242)
        self.assertEqual(check['minimum_gross_reward_pct'], 4.986)
        # The reward cap is still the default +6% take-profit, so a plan does not get credit beyond it.
        self.assertEqual(reward_check(STOCK, 100., 110.)['gross_reward_pct'], 6.)
        snapshot = entry_policy_snapshot()
        self.assertEqual(snapshot['min_net_reward_risk'], 1.42)
        self.assertEqual(snapshot['minimum_gross_reward_pct'], {'stock': 4.99, 'fund': 3.57})
        self.assertAlmostEqual(minimum_gross_reward(.03) * 100, 4.986)

    def test_wide_spread_reduces_net_reward_and_the_profit_cap_is_real(self):
        self.assertTrue(reward_check(STOCK, 100., 106., 99.9)['allowed'])
        wide = reward_check(STOCK, 100., 110., 99.5)
        self.assertEqual(wide['assumed_round_trip_cost_pct'], .6)
        self.assertEqual(wide['gross_reward_pct'], 6.)
        self.assertEqual(wide['net_reward_risk'], 1.5)
        # A 0.61% spread (0.71% round trip) still clears 1.42 at the +6% cap; 0.62% does not.
        self.assertTrue(reward_check(STOCK, 100., 110., 99.39)['allowed'])
        self.assertFalse(reward_check(STOCK, 100., 110., 99.38)['allowed'])
        self.assertFalse(reward_check(STOCK, 100., None)['allowed'])


if __name__ == '__main__':
    unittest.main()
