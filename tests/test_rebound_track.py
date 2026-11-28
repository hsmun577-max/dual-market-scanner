"""The reversal tracks (R2 bottom rebound, R3 oversold rebound) and the dip entry.

The numbers are the owner's: a rebound is bought when the name is pressed down ("상승할
종목이 눌렸을 때 사야 하고"), and a name that already sold off into strength is not bought
("이미 털어먹고 나가는 종목이 눌렸다고 사면 안 돼"). Both tracks therefore enter on a dip
from the next session's open, cancel on a gap down, and hold 5 sessions without the narrow
-2% stop.

Backtest: 565 symbols, 2025-02-28~2026-11-13, 0.28% round-trip cost. R2 +3.19% (59.2% win)
on open -2% with the sell-off filters, R3 +4.82% (75.5% win) on open -3%.
"""
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from rebound_track import (GAP_CANCEL_PCT, HOLD_SESSIONS, entry_state, limit_price, setup,
                          watch_candidates)
from scanner import calculate_indicators, send_telegram_summary, telegram_candidate_line
from preopen_signals import evaluate_preopen
from tradable_signal import format_requested_report, validate_entry

KST = ZoneInfo("Asia/Seoul")
AT = datetime(2026, 11, 23, 8, 50, tzinfo=KST)


def frames(closes, opens=None):
    index = pd.date_range("2026-07-01", periods=len(closes), freq="B")
    opens = opens or [value * 0.995 for value in closes]
    return pd.DataFrame({"Open": opens, "High": [value * 1.005 for value in closes],
                         "Low": [value * 0.995 for value in closes], "Close": closes,
                         "Volume": [1_000_000] * len(closes)}, index=index)


def r2_values(**overrides):
    """A bottom rebound candle: inverse alignment, 5-day break, 2.5x volume, bullish bar."""
    values = dict(price=10_400.0, open=10_000.0, prev_close=10_100.0, ma5=10_300.0,
                  ma20=10_800.0, ma60=11_200.0, volume_ratio=2.8, return_1d=0.03,
                  obv_rising=True, overheated=False, upper_wick_ratio=0.12, rsi14=44.0,
                  breakdown=False, market_above_ma20=False, volume=3_000_000,
                  average_turnover_20d=20_000_000_000, return_5d=0.02, return_20d=-0.06,
                  relative_strength=-0.03, data_date="2026-11-20")
    values.update(overrides)
    return values


def r3_values(**overrides):
    """An oversold bounce: RSI 31, +2.4% on the day, 1.9x volume, a bullish bar."""
    values = dict(price=5_200.0, open=5_050.0, prev_close=5_080.0, ma5=5_250.0,
                  ma20=5_700.0, ma60=6_000.0, volume_ratio=1.9, return_1d=0.024,
                  obv_rising=False, overheated=False, upper_wick_ratio=0.55, rsi14=31.0,
                  breakdown=False, market_above_ma20=False, volume=2_000_000,
                  average_turnover_20d=8_000_000_000, return_5d=-0.08, return_20d=-0.15,
                  relative_strength=-0.09, data_date="2026-11-20")
    values.update(overrides)
    return values


def row(values, **overrides):
    base = dict(market="KR", symbol="090710.KS", name="휴림로봇", asset_type="stock_or_etf",
                signal=dict(confirmed=False, score=0), indicators=dict(values),
                company_analysis=dict(event_analysis=dict(economic_graph={})),
                company_forecast=dict(assessment_status="complete", generated_at=AT.isoformat(),
                                      composite_score=61.0, base_price=11_000.0,
                                      bull_price=12_000.0, missing_dimensions=[], caveats=[],
                                      catalyst_context=dict(eligible=False)))
    base.update(overrides)
    return base


def report(item):
    return dict(results=[item], preopen_session="2026-11-23", sectors=[])


class IndicatorTests(unittest.TestCase):
    def test_the_candle_and_short_average_a_rebound_is_read_from_are_exposed(self):
        closes = [10_000 + (18 if position % 3 else -12) for position in range(80)]
        values = calculate_indicators(frames(closes))
        self.assertAlmostEqual(values["ma5"], sum(closes[-5:]) / 5, places=3)
        self.assertAlmostEqual(values["prev_close"], closes[-2], places=3)
        self.assertAlmostEqual(values["return_1d"], closes[-1] / closes[-2] - 1, places=4)
        self.assertEqual(values["open"], closes[-1] * 0.995)
        self.assertAlmostEqual(values["upper_wick_ratio"],
                               (closes[-1] * 1.005 - closes[-1]) / (closes[-1] * 0.01), places=3)
        self.assertTrue(0 < values["rsi14"] < 100)

    def test_a_falling_series_is_oversold_and_a_rising_one_is_not(self):
        falling = calculate_indicators(frames([10_000 - 20 * position for position in range(80)]))
        rising = calculate_indicators(frames([10_000 + 20 * position for position in range(80)]))
        self.assertLess(falling["rsi14"], 15)
        self.assertGreater(rising["rsi14"], 85)

    def test_a_frame_without_an_open_column_does_not_invent_one(self):
        data = frames([10_000 + position for position in range(80)]).drop(columns=["Open"])
        self.assertIsNone(calculate_indicators(data)["open"])


class R2SetupTests(unittest.TestCase):
    def test_the_bottom_rebound_setup_is_read_as_r2(self):
        track = setup(r2_values())
        self.assertEqual(track["strategy"], "bottom_rebound")
        self.assertEqual(track["label"], "R2 바닥반등")
        self.assertAlmostEqual(track["entry_offset_pct"], -0.02)

    def test_a_long_upper_wick_a_big_day_and_a_thin_volume_are_not_the_setup(self):
        for change in (dict(upper_wick_ratio=0.31), dict(return_1d=0.16),
                       dict(volume_ratio=2.4), dict(return_1d=0.029)):
            self.assertIsNone(setup(r2_values(**change)), change)

    def test_a_sell_off_still_running_or_an_aligned_chart_is_not_the_setup(self):
        for change in (dict(obv_rising=False), dict(overheated=True), dict(ma20=11_300.0),
                       dict(prev_close=10_350.0), dict(price=10_250.0),
                       dict(price=9_900.0)):
            self.assertIsNone(setup(r2_values(**change)), change)

    def test_a_missing_candle_value_is_not_a_pass(self):
        for key in ("open", "prev_close", "ma5", "upper_wick_ratio", "return_1d"):
            values = r2_values()
            values[key] = None
            self.assertIsNone(setup(values), key)


class R3SetupTests(unittest.TestCase):
    def test_the_oversold_bounce_is_read_as_r3(self):
        self.assertEqual(setup(r3_values())["strategy"], "oversold_rebound")
        self.assertAlmostEqual(setup(r3_values())["entry_offset_pct"], -0.03)

    def test_rsi_above_35_or_a_thin_bounce_is_not_r3(self):
        for change in (dict(rsi14=35.5), dict(volume_ratio=1.7), dict(return_1d=0.015),
                       dict(price=5_000.0), dict(overheated=True)):
            self.assertIsNone(setup(r3_values(**change)), change)

    def test_r3_does_not_need_the_bottom_rebound_filters(self):
        # A 55% upper wick and a flat OBV are inside the R3 definition the backtest used.
        self.assertEqual(setup(r3_values(upper_wick_ratio=0.55, obv_rising=False))["strategy"],
                         "oversold_rebound")


class EntryLimitTests(unittest.TestCase):
    def test_the_limit_is_two_percent_below_the_open_for_r2_and_three_for_r3(self):
        self.assertEqual(limit_price("bottom_rebound", 10_000), 9_800.0)
        self.assertEqual(limit_price("oversold_rebound", 10_000), 9_700.0)
        # Floored to the KRX tick of the destination price rather than rounded up.
        self.assertEqual(limit_price("bottom_rebound", 9_950), 9_750.0)
        self.assertEqual(limit_price("oversold_rebound", 9_950), 9_650.0)
        self.assertEqual(limit_price("bottom_rebound", 100, "US"), 98.0)

    def test_the_open_is_unknown_before_the_session_so_the_plan_waits(self):
        state = entry_state("bottom_rebound", 10_000, None)
        self.assertEqual(state["status"], "awaiting_open")
        self.assertIsNone(state["entry_limit"])

    def test_a_gap_down_of_one_percent_cancels_the_entry(self):
        self.assertEqual(entry_state("bottom_rebound", 10_000, 9_900)["status"],
                         "gap_down_cancelled")
        self.assertEqual(entry_state("bottom_rebound", 10_000, 9_899)["status"],
                         "gap_down_cancelled")
        # Exactly -1% is not a gap down; the rule is "below -1%".
        self.assertEqual(entry_state("bottom_rebound", 10_000, 9_900.5)["status"], "limit_set")
        self.assertEqual(GAP_CANCEL_PCT, -0.01)

    def test_a_flat_open_leaves_a_reference_level_for_the_plan(self):
        state = entry_state("bottom_rebound", 10_000, 10_000)
        self.assertEqual(state["status"], "limit_set")
        self.assertEqual(state["entry_limit"], 9_800.0)


class PreopenPlanTests(unittest.TestCase):
    def test_a_rebound_name_is_planned_in_a_downtrend_while_the_momentum_screen_holds(self):
        item = row(r2_values())
        result = evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(plan["track"], "rebound")
        self.assertEqual(plan["strategy"], "bottom_rebound")
        self.assertEqual(result["recommendations"], ["090710.KS"])
        self.assertEqual(result["rebound_candidates"], ["090710.KS"])
        # The plan is a rule until the open prints, with the flat-open level beside it.
        self.assertTrue(plan["awaiting_open"])
        self.assertIsNone(plan["entry_limit"])
        self.assertEqual(plan["flat_open_entry_limit"], 9_890.0)
        self.assertEqual(plan["gap_reference_price"], 10_100.0)
        self.assertEqual(plan["hold_sessions"], HOLD_SESSIONS)
        self.assertEqual(plan["stop_policy"], "narrow_stop_not_recommended")
        self.assertIsNone(plan["stop_pct"])
        self.assertFalse(plan["score_applied"])
        self.assertEqual(result["policy"]["rebound"]["hold_sessions"], 5)

    def test_the_same_chart_without_the_rebound_setup_is_still_held_back(self):
        item = row(r2_values(upper_wick_ratio=0.4))
        result = evaluate_preopen(report(item), "2026-11-20", AT)
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(item["preopen_decision"], "unconfirmed_uptrend")
        self.assertNotIn("preopen_signal", item)
        # An ordinary momentum setup still needs positive projected reward.
        momentum = row(dict(r2_values(), price=11_000.0, ma20=10_800.0, ma60=10_500.0,
                            ma5=10_300.0, prev_close=10_200.0, return_5d=0.04,
                            return_20d=0.09, relative_strength=0.05, market_above_ma20=False))
        momentum["indicators"]["rebound"] = False
        result = evaluate_preopen(report(momentum), "2026-11-20", AT)
        self.assertEqual(momentum["preopen_decision"], "insufficient_expected_reward")
        self.assertEqual(result["recommendations"], [])

    def test_an_observed_gap_down_cancels_the_plan_before_it_is_sent(self):
        # A caller that has already seen the plan session's open puts it on the row.
        item = row(r2_values(next_open=9_950.0))
        result = evaluate_preopen(report(item), "2026-11-20", AT)
        self.assertEqual(item["preopen_decision"], "rebound_gap_down_cancelled")
        self.assertNotIn("preopen_signal", item)
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["rebound_candidates"], [])

    def test_an_observed_open_sets_the_real_limit(self):
        item = row(r2_values(next_open=10_200.0))
        evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertFalse(plan["awaiting_open"])
        self.assertEqual(plan["entry_limit"], 9_990.0)
        self.assertAlmostEqual(plan["gap_observed_pct"], 10_200 / 10_100 - 1, places=4)

    def test_the_r3_plan_uses_the_three_percent_dip(self):
        item = row(r3_values(next_open=5_060.0))
        evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertEqual(plan["strategy"], "oversold_rebound")
        self.assertEqual(plan["entry_limit"], 4_905.0)
        self.assertEqual(plan["flat_open_entry_limit"], 4_925.0)

    def test_a_rebound_plan_needs_no_composite_score_gate(self):
        item = row(r2_values())
        item["company_forecast"]["composite_score"] = 21.0
        evaluate_preopen(report(item), "2026-11-20", AT)
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(item["preopen_signal"]["track"], "rebound")


class AlertSectionTests(unittest.TestCase):
    def message(self, item):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"}), \
             patch("scanner.delivery_status", return_value=(True, "test")), \
             patch("scanner.requests.post") as post:
            post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
            send_telegram_summary(report(item))
        return post.call_args.kwargs["json"]["text"]

    def test_the_alert_has_its_own_section_with_the_track_limit_cancel_and_hold(self):
        item = row(r2_values())
        evaluate_preopen(report(item), "2026-11-20", AT)
        text = self.message(item)
        self.assertIn("[반등 눌림 후보]", text)
        self.assertIn("R2 바닥반등", text)
        self.assertIn("눌림 지정가 시가 −2%", text)
        self.assertIn("갭하락 −1%면 취소", text)
        self.assertIn("5일 보유", text)
        # The momentum heading must not claim the rebound plan as its own buy.
        self.assertNotIn("조건부 매수 계획", text.split("[반등 눌림 후보]")[0])

    def test_the_section_appears_even_when_no_momentum_name_qualifies(self):
        item = row(r2_values())
        evaluate_preopen(report(item), "2026-11-20", AT)
        text = self.message(item)
        self.assertIn("반등 후보 1종목은 아래", text)
        self.assertIn("[반등 눌림 후보]", text)

    def test_the_line_states_the_track_and_the_waived_stop(self):
        item = row(r3_values())
        evaluate_preopen(report(item), "2026-11-20", AT)
        line = telegram_candidate_line(item)
        self.assertIn("R3 과매도반등", line)
        self.assertIn("시가 −3%", line)
        self.assertIn("RSI 31.0", line)
        self.assertIn("5일 보유", line)


class ExecutionTests(unittest.TestCase):
    at = datetime(2026, 11, 23, 9, 40, tzinfo=KST)

    def fixture(self, **quote_changes):
        item = row(r2_values())
        item["company_forecast"]["generated_at"] = (self.at - timedelta(minutes=1)).isoformat()
        evaluate_preopen(dict(results=[item], preopen_session="2026-11-23", sectors=[]),
                         "2026-11-20", self.at)
        quote = dict(ask=9_790.0, bid=9_780.0, ask_size=100, bid_size=100,
                     observed_at=self.at.isoformat(),
                     trade_at=(self.at - timedelta(seconds=30)).isoformat(),
                     day_open=10_000.0, last_price=9_790.0, volume_ratio=2.4)
        quote.update(quote_changes)
        return item, quote

    def test_the_plan_becomes_trade_ready_at_the_dip_from_the_observed_open(self):
        item, quote = self.fixture()
        signal = validate_entry(item, quote, self.at, True)
        self.assertTrue(signal["trade_ready"])
        self.assertEqual(signal["track"], "rebound")
        self.assertEqual(signal["entry_limit"], 9_800.0)
        self.assertIsNone(signal["stop_price"])
        self.assertEqual(signal["hold_sessions"], 5)
        text = format_requested_report([], [signal], dict(completed=True), self.at)
        self.assertIn("R2 바닥반등", text)
        self.assertIn("눌림 지정가 9,800원", text)

    def test_above_the_dip_it_waits_and_on_a_gap_down_it_is_cancelled(self):
        item, quote = self.fixture(ask=10_100.0, bid=10_090.0)
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"],
                         "waiting_for_entry_price")
        item, quote = self.fixture(day_open=9_880.0)
        signal = validate_entry(item, quote, self.at, True)
        self.assertEqual(signal["reason"], "rebound_gap_down_cancelled")
        self.assertFalse(signal["trade_ready"])
        self.assertTrue(signal["detail"])

    def test_an_unverified_open_is_not_traded(self):
        item, quote = self.fixture(day_open=None)
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"],
                         "rebound_open_unverified")


class SelectionTests(unittest.TestCase):
    def test_a_rebound_setup_earns_a_research_slot(self):
        from economic_graph import select_analysis_candidates
        item = row(r3_values())
        item.pop("company_forecast")
        item.pop("company_analysis")
        selected = select_analysis_candidates([item], "KR", (), limit=8)
        self.assertEqual([row["symbol"] for row in selected], ["090710.KS"])
        self.assertEqual(selected[0]["selection_reason"], "rebound_watch")

    def test_the_watch_list_reads_the_same_setup(self):
        item = row(r2_values())
        self.assertEqual([r["symbol"] for r in watch_candidates([item], "KR")], ["090710.KS"])
        item["indicators"]["volume_ratio"] = 2.0
        self.assertEqual(watch_candidates([item], "KR"), [])


if __name__ == "__main__":
    unittest.main()
