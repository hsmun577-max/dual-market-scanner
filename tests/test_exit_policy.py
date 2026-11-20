import unittest
from datetime import datetime, timedelta, timezone

from backtest_intraday import replay
from exit_policy import (DEFAULT_EXIT_POLICY, ExitPolicy, ExitTracker, evaluate_series,
                         policy_snapshot)
from scanner import build_risk_plan
from tradable_signal import format_requested_report, validate_entry

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 11, 20, 9, 30, tzinfo=KST)


def minutes(value):
    return T0 + timedelta(minutes=value)


class ExitPolicyNumbers(unittest.TestCase):
    def test_policy_is_two_percent_stop_and_six_percent_take_profit(self):
        self.assertEqual(DEFAULT_EXIT_POLICY.stop_pct, 0.02)
        self.assertEqual(DEFAULT_EXIT_POLICY.take_profit_pct, 0.06)
        self.assertEqual(DEFAULT_EXIT_POLICY.take_profit_mode, "surge_trailing")

    def test_snapshot_reports_the_same_numbers_to_plans_and_alerts(self):
        snapshot = policy_snapshot()
        self.assertEqual(snapshot["stop_pct"], 0.02)
        self.assertEqual(snapshot["take_profit_pct"], 0.06)
        self.assertEqual(snapshot["surge_window_minutes"], 30)
        self.assertEqual(snapshot["exit_price_rule"], "signal_price_before_plunge")
        self.assertIn("급등", DEFAULT_EXIT_POLICY.describe())

    def test_invalid_policy_values_are_rejected(self):
        for change in (dict(stop_pct=0), dict(take_profit_pct=1.2), dict(volume_collapse_ratio=1),
                       dict(surge_window=timedelta(0)), dict(hold_floor_pct=0.07)):
            with self.assertRaises(ValueError):
                ExitPolicy(**change)

    def test_scanner_risk_plan_uses_six_percent_not_seven(self):
        plan = build_risk_plan(10000)
        self.assertEqual(plan["stop_price"], 9800)
        self.assertEqual(plan["target_price"], 10600)
        self.assertEqual(plan["take_profit_pct"], 0.06)
        self.assertEqual(plan["risk_reward_ratio"], 3)
        self.assertEqual(plan["exit_policy"], DEFAULT_EXIT_POLICY.snapshot())

    def test_alert_texts_announce_the_surge_hold(self):
        row = dict(symbol="123456.KS", name="가상 검증종목",
                   preopen_signal=dict(status="conditional_buy", expires_session="2026-11-20",
                                       entry_limit=10000, reference_price=10000,
                                       reference_date="2026-11-19"),
                   company_forecast=dict(assessment_status="complete", missing_dimensions=[],
                                         composite_score=65, generated_at=minutes(-1).isoformat(),
                                         base_price=10500, bull_price=11000))
        quote = dict(ask=10000, bid=9990, ask_size=100, bid_size=100,
                     observed_at=T0.isoformat(), trade_at=minutes(-1).isoformat())
        signal = validate_entry(row, quote, T0, True)
        self.assertTrue(signal["trade_ready"])
        self.assertEqual(signal["take_profit_price"], 10600)
        self.assertEqual(signal["take_profit_mode"], "surge_trailing")
        self.assertEqual(signal["exit_policy"]["surge_window_minutes"], 30)
        text = format_requested_report([], [signal], {}, T0)
        self.assertIn("기본 익절", text)
        self.assertIn("트레일링", text)


class FixedExits(unittest.TestCase):
    def tracker(self, **kwargs):
        return ExitTracker(10000, entry_at=T0, baseline_volume=1000, **kwargs)

    def test_slow_rise_sells_at_six_percent(self):
        decision = evaluate_series(10000, [
            dict(at=minutes(10), price=10300, volume=900),
            dict(at=minutes(20), price=10600, volume=900),
        ], entry_at=T0, baseline_volume=1000)
        self.assertEqual(decision["action"], "exit")
        self.assertEqual(decision["reason"], "take_profit")
        self.assertEqual(decision["exit_price"], 10600)
        self.assertEqual(decision["surge_check"], "volume_not_expanded")

    def test_six_percent_reached_late_is_a_take_profit_not_a_surge(self):
        decision = evaluate_series(10000, [
            dict(at=minutes(45), price=10650, volume=5000),
        ], entry_at=T0, baseline_volume=1000)
        self.assertEqual(decision["reason"], "take_profit")
        self.assertEqual(decision["surge_check"], "outside_surge_window")

    def test_two_percent_stop_fills_at_the_observed_price(self):
        decision = evaluate_series(10000, [
            dict(at=minutes(5), price=10200, volume=800),
            dict(at=minutes(10), price=9750, volume=800),
        ], entry_at=T0, baseline_volume=1000)
        self.assertEqual(decision["reason"], "stop_loss")
        self.assertEqual(decision["exit_price"], 9750)

    def test_without_a_volume_baseline_the_default_six_percent_applies(self):
        decision = evaluate_series(10000, [
            dict(at=minutes(5), price=10700, volume=9000),
        ], entry_at=T0)
        self.assertEqual(decision["reason"], "take_profit")
        self.assertEqual(decision["surge_check"], "baseline_volume_missing")


class SurgeTrailing(unittest.TestCase):
    def surge(self):
        tracker = ExitTracker(10000, entry_at=T0, baseline_volume=1000)
        tracker.observe(minutes(5), 10400, volume=2000)
        return tracker.observe(minutes(10), 10700, volume=3000)

    def test_fast_move_on_expanded_volume_holds_instead_of_selling_at_six(self):
        decision = self.surge()
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "surge_hold")
        self.assertEqual(decision["mode"], "trailing")

    def test_collapse_signal_sells_at_the_price_before_the_plunge(self):
        tracker = ExitTracker(10000, entry_at=T0, baseline_volume=1000)
        self.assertEqual(tracker.observe(minutes(5), 10400, volume=2000)["reason"], "fixed_hold")
        self.assertEqual(tracker.observe(minutes(10), 10700, volume=3000)["reason"], "surge_hold")
        self.assertEqual(tracker.observe(minutes(15), 10900, volume=5000)["reason"], "trailing_hold")
        # volume falls away but the price has not rolled over from the peak yet
        self.assertEqual(tracker.observe(minutes(20), 10850, volume=1200)["reason"], "trailing_hold")
        decision = tracker.observe(minutes(25), 10700, volume=600)
        self.assertEqual(decision["action"], "exit")
        self.assertEqual(decision["reason"], "surge_trailing_exit")
        self.assertEqual(decision["exit_price"], 10700)
        self.assertGreater(decision["exit_price"], tracker.target_price)

    def test_volume_collapse_without_a_rollover_keeps_holding(self):
        tracker = ExitTracker(10000, entry_at=T0, baseline_volume=1000)
        tracker.observe(minutes(5), 10700, volume=3000)
        tracker.observe(minutes(10), 10900, volume=5000)
        decision = tracker.observe(minutes(15), 10900, volume=300)
        self.assertEqual(decision["action"], "hold")
        self.assertTrue(decision["volume_collapsed"])
        self.assertFalse(decision["rolled_over"])

    def test_rollover_without_a_volume_collapse_keeps_holding(self):
        tracker = ExitTracker(10000, entry_at=T0, baseline_volume=1000)
        tracker.observe(minutes(5), 10700, volume=3000)
        tracker.observe(minutes(10), 10900, volume=5000)
        decision = tracker.observe(minutes(15), 10700, volume=4900)
        self.assertEqual(decision["action"], "hold")
        self.assertFalse(decision["volume_collapsed"])

    def test_surged_position_falls_back_to_the_six_percent_floor(self):
        tracker = ExitTracker(10000, entry_at=T0, baseline_volume=1000)
        tracker.observe(minutes(5), 10700, volume=3000)
        tracker.observe(minutes(10), 10900, volume=5000)
        decision = tracker.observe(minutes(15), 10500, volume=200)
        self.assertEqual(decision["reason"], "trailing_floor")
        self.assertEqual(decision["exit_price"], 10600)

    def test_missing_volume_never_invents_a_collapse(self):
        tracker = ExitTracker(10000, entry_at=T0, baseline_volume=1000)
        tracker.observe(minutes(5), 10700, volume=3000)
        decision = tracker.observe(minutes(10), 10700, volume=None)
        self.assertEqual(decision["action"], "hold")
        self.assertFalse(decision["volume_observed"])

    def test_tracker_rejects_bad_input_and_double_close(self):
        with self.assertRaises(ValueError):
            ExitTracker(0)
        tracker = ExitTracker(10000)
        with self.assertRaises(ValueError):
            tracker.observe(datetime(2026, 11, 20, 9, 30), 10100)
        with self.assertRaises(ValueError):
            tracker.observe(T0, float("nan"))
        tracker.observe(minutes(5), 9700)
        with self.assertRaises(RuntimeError):
            tracker.observe(minutes(10), 9800)


def sample(quotes, post_bars=()):
    return {
        "market": "KRX",
        "symbol": "005930",
        "signal_at": datetime(2026, 11, 20, 9, 15, tzinfo=KST).isoformat(),
        "daily_eligible": True,
        "bars": [
            {"start": datetime(2026, 11, 20, 9, 0, tzinfo=KST).isoformat(),
             "high": 10000, "low": 9900, "close": 9950, "volume": 100},
            {"start": datetime(2026, 11, 20, 9, 5, tzinfo=KST).isoformat(),
             "high": 10050, "low": 9950, "close": 10020, "volume": 100},
            {"start": datetime(2026, 11, 20, 9, 10, tzinfo=KST).isoformat(),
             "high": 10120, "low": 10000, "close": 10100, "volume": 500},
        ],
        "quotes": quotes,
        "post_bars": list(post_bars),
    }


def quote(at, bid, ask=None):
    return {"at": at.isoformat(), "bid": bid, "ask": ask if ask is not None else bid + 10}


def bar(at, volume):
    return {"start": at.isoformat(), "high": 11000, "low": 10000, "close": 10500, "volume": volume}


class BacktestPolicyMode(unittest.TestCase):
    def surge_sample(self):
        signal_quote = quote(datetime(2026, 11, 20, 9, 14, 55, tzinfo=KST), 10090, 10100)
        return sample([
            signal_quote,
            quote(datetime(2026, 11, 20, 9, 15, 10, tzinfo=KST), 10090, 10100),
            quote(datetime(2026, 11, 20, 9, 20, tzinfo=KST), 10750, 10760),
            quote(datetime(2026, 11, 20, 9, 25, tzinfo=KST), 10900, 10910),
            quote(datetime(2026, 11, 20, 9, 30, tzinfo=KST), 10850, 10860),
            quote(datetime(2026, 11, 20, 9, 35, tzinfo=KST), 10720, 10730),
        ], [
            bar(datetime(2026, 11, 20, 9, 15, tzinfo=KST), 4000),
            bar(datetime(2026, 11, 20, 9, 20, tzinfo=KST), 5000),
            bar(datetime(2026, 11, 20, 9, 25, tzinfo=KST), 1200),
            bar(datetime(2026, 11, 20, 9, 30, tzinfo=KST), 600),
        ])

    def test_surge_is_held_and_closed_on_the_collapse_signal(self):
        result = replay(self.surge_sample(), 10, exit_mode="policy")
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["exit_reason"], "surge_trailing_exit")
        self.assertEqual(result["held_mode"], "trailing")
        self.assertEqual(result["exit_price"], 10720)
        self.assertGreater(result["net_return"], 0.05)
        self.assertGreater(result["exit_price"] / result["entry_price"] - 1, 0.06)

    def test_legacy_mode_keeps_the_two_percent_comparison(self):
        result = replay(self.surge_sample(), 10, exit_mode="fixed")
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["exit_mode"], "fixed")

    def test_in_progress_bar_volume_is_not_used(self):
        signal_quote = quote(datetime(2026, 11, 20, 9, 14, 55, tzinfo=KST), 10090, 10100)
        row = sample([
            signal_quote,
            quote(datetime(2026, 11, 20, 9, 15, 10, tzinfo=KST), 10090, 10100),
            quote(datetime(2026, 11, 20, 9, 20, tzinfo=KST), 10750, 10760),
        ], [bar(datetime(2026, 11, 20, 9, 20, tzinfo=KST), 9000)])
        result = replay(row, 10, exit_mode="policy")
        self.assertEqual(result["exit_reason"], "take_profit")
        self.assertEqual(result["exit_price"], 10100 * 1.06)

    def test_position_still_open_at_the_horizon_is_not_forced_closed(self):
        signal_quote = quote(datetime(2026, 11, 20, 9, 14, 55, tzinfo=KST), 10090, 10100)
        row = sample([
            signal_quote,
            quote(datetime(2026, 11, 20, 9, 15, 10, tzinfo=KST), 10090, 10100),
            quote(datetime(2026, 11, 20, 9, 20, tzinfo=KST), 10305, 10315),
        ])
        fixed = replay(row, 10, exit_mode="fixed")
        policy = replay(row, 10, exit_mode="policy")
        self.assertEqual(fixed["status"], "evaluated")
        self.assertGreater(fixed["net_return"], 0.015)
        self.assertEqual(policy["status"], "held_past_horizon")
        self.assertAlmostEqual(policy["unrealized_return"], 10305 * 0.999 / (10100 * 1.001) - 1)

    def test_unknown_exit_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            replay(self.surge_sample(), 10, exit_mode="whatever")


if __name__ == "__main__":
    unittest.main()
