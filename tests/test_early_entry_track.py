"""The short-term early-entry track (B: MA5>MA20 1~3 sessions) and its −1% limit entry.

The study (``evidence/dual-market-scanner/상승초입-조기포착-전략연구.md`` 1부) found B alone
worthless (+0.069% over 5 sessions) and B with a measured inflow or a 61+ fundamental score
worth +0.520% (+4.545% at 10 sessions to the close, +1.141% at a −3% stop). The entry is a
limit 1% under the next session's open: +0.530% (CI +0.089~+1.007), filled 83.4% of the
time, against +0.069% bought at the open.

What these tests hold down: the track lifts only the momentum gate's trend confirmation
(``unconfirmed_uptrend``/``weak_relative_trend``), never liquidity, a breakdown, an
extended price or the market-regime gate; it needs the qualifier; and it keeps the owner's
split stop (−3% stock / −2% fund) with the +6% take profit.

The 20-day band the setup allows is +8% since 2026-11-27 (``early_entry_track.MAX_ABOVE_MA20``):
the two-year Korean study found the +3~+8% band it used to refuse carried +0.84%p of excess
return while +8~+15% carried none, so these tests hold +7.92% as a pass and +8.42% as
refused.
"""
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from candidate_selection import candidate_rejection_reason, early_entry_block_reason
from early_entry_track import (ENTRY_OFFSET_PCT, HOLD_SESSIONS, entry_state, limit_price,
                               qualifier, setup, watch_candidates)
from preopen_signals import evaluate_preopen
from scanner import calculate_indicators, send_telegram_summary, telegram_candidate_line
from tradable_signal import format_requested_report, validate_entry

KST = ZoneInfo("Asia/Seoul")
AT = datetime(2026, 11, 23, 8, 50, tzinfo=KST)


def frames(closes, opens=None):
    index = pd.date_range("2026-07-01", periods=len(closes), freq="B")
    opens = opens or [value * 0.99 for value in closes]
    return pd.DataFrame({"Open": opens, "High": [value * 1.012 for value in closes],
                         "Low": [value * 0.985 for value in closes], "Close": closes,
                         "Volume": [1_000_000] * len(closes)}, index=index)


def early_values(**overrides):
    """An early alignment: MA5 above MA20 for its 2nd session, 1.8x volume, bullish bar."""
    values = dict(price=10_300.0, open=10_100.0, prev_close=10_150.0, high=10_330.0,
                  low=10_080.0, ma5=10_250.0, ma20=10_100.0, ma60=10_600.0,
                  ma5_above_ma20_days=2, ma20_cross_up=False, volume_ratio=1.8,
                  return_1d=0.0148, return_5d=0.02, return_20d=-0.05, relative_strength=-0.01,
                  rsi14=52.0, upper_wick_ratio=0.2, obv_rising=True, breakdown=False,
                  overheated=False, market_above_ma20=True, volume=3_000_000,
                  average_turnover_20d=20_000_000_000, data_date="2026-11-20")
    values.update(overrides)
    return values


def row(values=None, **overrides):
    base = dict(market="KR", symbol="053210.KS", name="스카이라이프", asset_type="stock",
                signal=dict(confirmed=False, score=0), indicators=dict(values or early_values()),
                institutional_flows=dict(status="measured", direction="inflow", score=0.4),
                company_analysis=dict(event_analysis=dict(economic_graph={})),
                company_forecast=dict(assessment_status="complete", generated_at=AT.isoformat(),
                                      composite_score=61.0, fundamental_score=58.0,
                                      base_price=11_000.0, bull_price=12_000.0,
                                      missing_dimensions=[], caveats=[],
                                      catalyst_context=dict(eligible=False)))
    base.update(overrides)
    return base


def report(item, session="2026-11-23"):
    return dict(results=[item], preopen_session=session, sectors=[])


class IndicatorTests(unittest.TestCase):
    def test_the_alignment_age_is_the_consecutive_aligned_sessions_ending_today(self):
        # A flat base, then four rising sessions: the averages are aligned today and the
        # age says for how many sessions, which is what makes an entry early or late.
        closes = [10_000.0] * 40 + [10_100.0, 10_200.0, 10_300.0, 10_400.0]
        data = frames(closes)
        values = calculate_indicators(data)
        ma5 = data["Close"].rolling(5).mean()
        ma20 = data["Close"].rolling(20).mean()
        streak = 0
        for position in range(-1, -len(closes), -1):
            if ma5.iloc[position] > ma20.iloc[position]:
                streak += 1
            else:
                break
        self.assertEqual(values["ma5_above_ma20_days"], streak)
        self.assertGreaterEqual(streak, 3)
        self.assertFalse(values["ma20_cross_up"])

    def test_an_inverted_chart_has_no_alignment_age(self):
        closes = [10_000.0] * 40 + [9_900.0, 9_800.0, 9_700.0]
        values = calculate_indicators(frames(closes))
        self.assertLess(values["ma5"], values["ma20"])
        self.assertIsNone(values["ma5_above_ma20_days"])
        self.assertFalse(values["ma20_cross_up"])

    def test_the_first_close_back_above_the_average_is_flagged(self):
        # A short slide then one jump: the close is back over the 20-day average while the
        # 5-day average is still under it — exactly the day the momentum gate calls
        # unconfirmed, and the day the early track may use.
        closes = [10_000.0] * 45 + [9_900.0, 9_800.0, 9_700.0, 9_600.0, 9_500.0, 10_100.0]
        values = calculate_indicators(frames(closes))
        self.assertLess(values["ma5"], values["ma20"])
        self.assertGreater(values["price"], values["ma20"])
        self.assertTrue(values["ma20_cross_up"])
        self.assertIsNone(values["ma5_above_ma20_days"])


class SetupTests(unittest.TestCase):
    def test_the_aligned_volume_expansion_is_read_as_the_early_track(self):
        track = setup(early_values())
        self.assertEqual(track["track"], "early_entry")
        self.assertEqual(track["label"], "[단기 상승 초입]")
        self.assertEqual(track["entry_offset_pct"], -0.01)
        self.assertEqual(track["evidence"]["alignment_days"], 2)

    def test_the_first_cross_day_qualifies_without_the_alignment_age(self):
        track = setup(early_values(ma5=9_900.0, ma5_above_ma20_days=0, ma20_cross_up=True))
        self.assertIsNotNone(track)
        self.assertTrue(track["evidence"]["ma20_cross_up"])

    def test_a_late_alignment_an_old_cross_and_a_cool_bar_are_not_the_setup(self):
        for change in (dict(ma5_above_ma20_days=4), dict(ma5_above_ma20_days=0),
                       dict(ma5_above_ma20_days=None, ma20_cross_up=False),
                       dict(volume_ratio=1.4), dict(open=10_300.0),
                       dict(price=10_950.0), dict(price=10_050.0),
                       dict(overheated=True), dict(breakdown=True)):
            self.assertIsNone(setup(early_values(**change)), change)

    def test_the_band_between_three_and_eight_percent_now_qualifies(self):
        # 10,450 is +3.47% above a 10,100 average: outside the old +3% band, inside +8%.
        track = setup(early_values(price=10_450.0))
        self.assertIsNotNone(track)
        self.assertAlmostEqual(track["evidence"]["above_ma20_pct"], 0.034653, places=5)

    def test_a_price_more_than_eight_percent_above_the_average_is_extended(self):
        # The study put the band's edge at +8%: +7.92% still qualifies, +8.42% does not.
        self.assertIsNotNone(setup(early_values(price=10_900.0)))
        self.assertIsNone(setup(early_values(price=10_950.0)))

    def test_a_missing_candle_or_average_value_is_not_a_pass(self):
        for key in ("price", "open", "ma5", "ma20", "volume_ratio"):
            values = early_values()
            values[key] = None
            self.assertIsNone(setup(values), key)


class QualifierTests(unittest.TestCase):
    def test_a_measured_inflow_or_a_61_fundamental_score_is_the_qualifier(self):
        self.assertEqual(qualifier(row()), "supply_inflow")
        # No measured flow and a 58 fundamental score is neither qualifier.
        self.assertIsNone(qualifier(row(institutional_flows=dict(status="unavailable"))))
        item = row(institutional_flows=dict(status="unavailable"))
        item["company_forecast"]["fundamental_score"] = 64.0
        self.assertEqual(qualifier(item), "fundamental_61")

    def test_a_price_proxy_is_not_a_measured_inflow(self):
        item = row(institutional_flows=dict(status="proxy", direction="inflow", score=0.5))
        self.assertIsNone(qualifier(item))
        item = row(institutional_flows=dict(status="measured", direction="outflow"))
        item["company_forecast"]["fundamental_score"] = 41.0
        self.assertIsNone(qualifier(item))


class EntryLimitTests(unittest.TestCase):
    def test_the_limit_is_one_percent_under_the_open_floored_to_the_tick(self):
        self.assertEqual(limit_price(10_000), 9_900.0)
        self.assertEqual(limit_price(10_400), 10_290.0)
        self.assertEqual(limit_price(9_950), 9_850.0)
        self.assertEqual(limit_price(100, "US"), 99.0)
        self.assertEqual(ENTRY_OFFSET_PCT, -0.01)

    def test_the_open_is_unknown_before_the_session_so_the_plan_waits(self):
        state = entry_state(None)
        self.assertEqual(state["status"], "awaiting_open")
        self.assertIsNone(state["entry_limit"])
        self.assertEqual(entry_state(10_000)["entry_limit"], 9_900.0)

    def test_the_flat_open_reference_is_the_previous_close_less_one_percent(self):
        from early_entry_track import entry_plan
        item = row()
        status, plan = entry_plan(item, item["company_forecast"], "2026-11-20", "2026-11-23",
                                  AT, "KR", next_open=None, qualifier_reason="supply_inflow")
        self.assertEqual(status, "conditional_buy")
        self.assertTrue(plan["awaiting_open"])
        self.assertIsNone(plan["entry_limit"])
        self.assertEqual(plan["flat_open_entry_limit"], 10_190.0)
        self.assertEqual(plan["hold_sessions"], HOLD_SESSIONS)
        self.assertEqual(plan["entry_basis"], "next_session_open_less_1pct")


class PreopenPlanTests(unittest.TestCase):
    def test_the_aligned_name_is_planned_while_the_momentum_gate_refuses_it(self):
        item = row()
        self.assertEqual(candidate_rejection_reason(item, "KR"), "unconfirmed_uptrend")
        result = evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(plan["track"], "early_entry")
        self.assertEqual(plan["strategy"], "early_ma_alignment")
        self.assertEqual(result["recommendations"], ["053210.KS"])
        self.assertEqual(result["early_entry_candidates"], ["053210.KS"])
        self.assertTrue(plan["awaiting_open"])
        self.assertEqual(plan["qualifier"], "supply_inflow")
        # The owner's split stop and +6% take profit travel with the plan.
        self.assertEqual((plan["asset_class"], plan["stop_pct"]), ("stock", 0.03))
        self.assertEqual(plan["exit_policy"]["take_profit_pct"], 0.06)
        self.assertEqual(result["policy"]["early_entry"]["entry_offset_pct"], -0.01)
        self.assertEqual(result["policy"]["early_entry"]["hold_sessions"], 5)

    def test_the_same_chart_without_a_qualifier_is_not_planned(self):
        item = row(institutional_flows=dict(status="unavailable"))
        item["company_forecast"]["fundamental_score"] = 51.0
        result = evaluate_preopen(report(item), "2026-11-20", AT)
        self.assertEqual(item["preopen_decision"], "early_entry_qualifier_missing")
        self.assertEqual(result["recommendations"], [])
        self.assertNotIn("preopen_signal", item)

    def test_the_widened_band_reaches_the_plan(self):
        # +3.47% above the average used to stop here; the plan now goes out.
        item = row(early_values(price=10_450.0))
        result = evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(plan["track"], "early_entry")
        self.assertEqual(plan["evidence"]["above_ma20_pct"], 0.034653)
        self.assertEqual(result["early_entry_candidates"], ["053210.KS"])
        self.assertEqual(result["policy"]["early_entry"]["max_above_ma20_pct"], 8.0)

    def test_the_safety_blocks_and_the_regime_gate_still_hold_it_back(self):
        for change, expected in ((dict(market_above_ma20=False), "market_headwind"),
                                 (dict(average_turnover_20d=1), "insufficient_liquidity"),
                                 (dict(breakdown=True), "breakdown"),
                                 (dict(return_5d=0.13), "extended_price"),
                                 (dict(price=10_950.0), "extended_price"),
                                 (dict(overheated=True), "extended_price")):
            item = row(early_values(**change))
            result = evaluate_preopen(report(item), "2026-11-20", AT)
            self.assertEqual(item["preopen_decision"], expected, change)
            self.assertEqual(result["recommendations"], [], change)

    def test_the_policy_score_and_the_minimum_reward_still_apply(self):
        item = row()
        item["company_forecast"]["composite_score"] = 52.0
        evaluate_preopen(report(item), "2026-11-20", AT)
        self.assertEqual(item["preopen_decision"], "composite_score_below_policy")
        item = row()
        item["company_forecast"]["base_price"] = 10_400.0
        evaluate_preopen(report(item), "2026-11-20", AT)
        self.assertEqual(item["preopen_decision"], "insufficient_expected_reward")

    def test_a_fund_keeps_the_wider_stop_of_its_own_policy(self):
        item = row(asset_type="etf", name="KODEX 200")
        evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertEqual((plan["asset_class"], plan["stop_pct"]), ("fund", 0.02))
        self.assertIn("손절 −2%", telegram_candidate_line(item))

    def test_an_observed_open_fixes_the_real_limit(self):
        item = row(early_values(next_open=10_400.0))
        evaluate_preopen(report(item), "2026-11-20", AT)
        plan = item["preopen_signal"]
        self.assertFalse(plan["awaiting_open"])
        self.assertEqual(plan["entry_limit"], 10_290.0)

    def test_the_block_helper_keeps_the_regime_gate_apart_from_the_chart(self):
        self.assertIsNone(early_entry_block_reason(row(), "KR"))
        self.assertEqual(early_entry_block_reason(row(early_values(market_above_ma20=False)), "KR"),
                         "market_headwind")


class AlertSectionTests(unittest.TestCase):
    def message(self, item):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"}), \
             patch("scanner.delivery_status", return_value=(True, "test")), \
             patch("scanner.requests.post") as post:
            post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
            send_telegram_summary(report(item))
        return post.call_args.kwargs["json"]["text"]

    def test_the_line_recommends_the_open_minus_one_percent_limit(self):
        item = row()
        evaluate_preopen(report(item), "2026-11-20", AT)
        text = self.message(item)
        self.assertIn("단기 상승 초입", text)
        self.assertIn("시가 −1% 지정가", text)
        self.assertIn("갭 없이 열리면 10,190원", text)
        self.assertIn("미체결 시 추격 없음", text)
        self.assertIn("MA5>MA20 2일차", text)
        self.assertIn("수급 유입", text)
        self.assertIn("5거래일 보유", text)
        self.assertIn("손절 −3%", text)

    def test_the_line_uses_the_observed_limit_once_the_open_prints(self):
        item = row(early_values(next_open=10_400.0))
        evaluate_preopen(report(item), "2026-11-20", AT)
        line = telegram_candidate_line(item)
        self.assertIn("시가 −1% 지정가 10,290원 · 시가 확인됨", line)


class ExecutionTests(unittest.TestCase):
    at = datetime(2026, 11, 23, 9, 40, tzinfo=KST)

    def fixture(self, **quote_changes):
        item = row()
        item["company_forecast"]["generated_at"] = (self.at - timedelta(minutes=1)).isoformat()
        evaluate_preopen(dict(results=[item], preopen_session="2026-11-23", sectors=[]),
                         "2026-11-20", self.at)
        quote = dict(ask=10_280.0, bid=10_270.0, ask_size=100, bid_size=100,
                     observed_at=self.at.isoformat(),
                     trade_at=(self.at - timedelta(seconds=30)).isoformat(),
                     day_open=10_400.0, last_price=10_280.0, volume_ratio=2.2)
        quote.update(quote_changes)
        return item, quote

    def test_the_plan_is_trade_ready_at_the_limit_under_the_observed_open(self):
        item, quote = self.fixture()
        signal = validate_entry(item, quote, self.at, True)
        self.assertTrue(signal["trade_ready"])
        self.assertEqual(signal["track"], "early_entry")
        self.assertEqual(signal["entry_limit"], 10_290.0)
        self.assertEqual(signal["stop_pct"], 0.03)
        self.assertEqual(signal["take_profit_pct"], 0.06)
        self.assertLess(signal["stop_price"], signal["indicative_entry"])
        text = format_requested_report([], [signal], dict(completed=True), self.at)
        self.assertIn("단기 상승 초입", text)
        self.assertIn("시가 −1% 지정가 10,290원", text)
        self.assertIn("5거래일 보유", text)

    def test_above_the_limit_it_waits_and_never_chases(self):
        item, quote = self.fixture(ask=10_350.0, bid=10_340.0)
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"],
                         "waiting_for_entry_price")

    def test_an_unverified_open_is_not_traded(self):
        item, quote = self.fixture(day_open=None)
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"],
                         "early_entry_open_unverified")

    def test_a_price_well_under_the_limit_needs_a_new_analysis(self):
        item, quote = self.fixture(ask=10_000.0, bid=9_990.0)
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"],
                         "price_moved_requires_reanalysis")


class SelectionTests(unittest.TestCase):
    def test_an_early_alignment_earns_a_research_slot(self):
        from economic_graph import select_analysis_candidates
        item = row(early_values(return_5d=-0.01, ma60=10_050.0))
        item.pop("company_forecast")
        item.pop("company_analysis")
        item.pop("institutional_flows")
        selected = select_analysis_candidates([item], "KR", (), limit=8)
        self.assertEqual([r["symbol"] for r in selected], ["053210.KS"])
        self.assertEqual(selected[0]["selection_reason"], "early_entry_watch")

    def test_the_watch_list_reads_the_same_chart(self):
        item = row()
        self.assertEqual([r["symbol"] for r in watch_candidates([item], "KR")], ["053210.KS"])
        item["indicators"]["volume_ratio"] = 1.2
        self.assertEqual(watch_candidates([item], "KR"), [])


if __name__ == "__main__":
    unittest.main()
