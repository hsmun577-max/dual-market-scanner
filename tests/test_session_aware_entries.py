import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from candidate_selection import candidate_rejection_reason
from economic_graph import select_analysis_candidates
from kis_intraday import evaluate_first_pullback, update_bar
from scanner import attach_inverse_regime, completed_daily_prices


class ClosedDailyBarTests(unittest.TestCase):
    def test_korean_close_changes_the_last_usable_bar(self):
        zone = ZoneInfo("Asia/Seoul")
        data = pd.DataFrame({"Close": [100, 110]},
                            index=pd.to_datetime(["2026-11-23", "2026-11-24"]))
        before = datetime(2026, 11, 24, 15, 29, tzinfo=zone)
        after = before + timedelta(minutes=1)
        self.assertEqual(completed_daily_prices(data, "KR", before)["Close"].tolist(), [100])
        self.assertEqual(completed_daily_prices(data, "KR", after)["Close"].tolist(), [100, 110])

    def test_us_early_close_follows_the_exchange_calendar(self):
        zone = ZoneInfo("America/New_York")
        data = pd.DataFrame({"Close": [100, 110]},
                            index=pd.to_datetime(["2026-11-25", "2026-11-27"]))
        before = datetime(2026, 11, 27, 12, 59, tzinfo=zone)
        after = before + timedelta(minutes=1)
        self.assertEqual(completed_daily_prices(data, "US", before)["Close"].tolist(), [100])
        self.assertEqual(completed_daily_prices(data, "US", after)["Close"].tolist(), [100, 110])


class InverseBenchmarkTests(unittest.TestCase):
    def test_inverse_requires_its_own_same_session_index(self):
        values = dict(price=20, volume=2_000_000, volume_ratio=2, ma20=19, ma60=18,
                      return_5d=.04, return_20d=.08, relative_strength=.10, obv_rising=True,
                      average_turnover_20d=22_000_000, market_above_ma20=False,
                      data_date="2026-11-24")
        row = dict(symbol="SQQQ", indicators=values)
        index = pd.bdate_range(end="2026-11-24", periods=21)
        falling = pd.DataFrame({"Close": list(range(120, 99, -1))}, index=index)
        attach_inverse_regime(values, "SQQQ", falling)
        self.assertEqual(values["inverse_underlying"], "^NDX")
        self.assertIsNone(candidate_rejection_reason(row, "US"))
        attach_inverse_regime(values, "SQQQ", falling.iloc[:-1])
        self.assertEqual(candidate_rejection_reason(row, "US"), "inverse_underlying_unverified")
        rising = falling.sort_values("Close").set_axis(index)
        attach_inverse_regime(values, "SQQQ", rising)
        self.assertEqual(candidate_rejection_reason(row, "US"), "market_headwind")


class ResearchSeatsTests(unittest.TestCase):
    def test_busy_event_list_does_not_displace_the_three_entry_strategies(self):
        pool = [dict(symbol=f"T{i}", indicators=dict(average_turnover_20d=100_000_000 + i))
                for i in range(82)]
        with patch("candidate_selection.liquidity_rejection_reason", return_value=None), \
             patch("candidate_selection.rank_market_candidates", return_value=pool[:12]), \
             patch("candidate_selection.rank_recovery_candidates", return_value=pool[12:24]), \
             patch("catalyst_track.watch_candidates", return_value=pool[64:70]), \
             patch("rebound_track.watch_candidates", return_value=pool[70:76]), \
             patch("early_entry_track.watch_candidates", return_value=pool[76:82]):
            selected = select_analysis_candidates(pool, "US", [r["symbol"] for r in pool[32:64]],
                                                  pinned=[r["symbol"] for r in pool[24:32]])
        self.assertEqual(len(selected), 48)
        for reason in ("catalyst_watch", "rebound_watch", "early_entry_watch"):
            self.assertGreaterEqual(sum(row.get("selection_reason") == reason for row in selected), 3)
        self.assertTrue(any(row in selected for row in pool[32:64]))


class FirstPullbackTests(unittest.TestCase):
    def setUp(self):
        self.opens = datetime(2026, 11, 24, 9, tzinfo=ZoneInfo("Asia/Seoul"))
        self.at = self.opens + timedelta(minutes=25)
        self.bars = {}
        tape = [[(9950, 20), (9900, 80)], [(10000, 5), (9920, 50), (9880, 45)],
                [(9980, 80), (10020, 100), (10040, 20)],
                [(10000, 30), (10010, 20)], [(10006, 20), (10022, 20)]]
        for slot, trades in enumerate(tape):
            for tick, (price, size) in enumerate(trades):
                at = self.opens + timedelta(minutes=5 * slot, seconds=tick)
                update_bar(self.bars, slot, at, price, size)
        self.quote = dict(bid=10015, ask=10025, spread_pct=.1,
                          event_time=(self.at - timedelta(seconds=1)).isoformat())

    def test_completed_pullback_and_fresh_quote_confirm(self):
        status, metrics, limit = evaluate_first_pullback(self.bars, self.quote, self.at, 100)
        self.assertEqual(status, "first_pullback_confirmed")
        self.assertEqual(metrics[0], 2)
        self.assertAlmostEqual(limit, 10050)

    def test_an_opening_breakout_alone_is_not_a_buy(self):
        before_pullback = {slot: self.bars[slot] for slot in range(3)}
        self.assertEqual(evaluate_first_pullback(before_pullback, self.quote, self.at, 100)[0],
                         "missing_pullback_bars")

    def test_ohlcv_without_trade_value_cannot_confirm_vwap(self):
        self.bars[0].pop("trade_value")
        self.assertEqual(evaluate_first_pullback(self.bars, self.quote, self.at, 100)[0],
                         "vwap_unverified")

    def test_broken_support_does_not_confirm(self):
        self.bars[3]["low"] = 9900
        self.assertEqual(evaluate_first_pullback(self.bars, self.quote, self.at, 100)[0],
                         "first_pullback_not_supported")


if __name__ == "__main__":
    unittest.main()
