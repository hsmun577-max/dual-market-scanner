"""The confirmed-catalyst track, including the 지니언스 3Q earnings case (2026-11-12).

The reference case is real: on 2026-11-11 지니언스 (263860.KQ) closed at 25,900원 on 4.24x
volume with the 20-day average above the 60-day one, and the first screen rejected it as a
plain ``rebound`` under a market headwind. The next morning at 08:18 the 3Q operating-profit
jump (+253% year over year) was disclosed and the stock gapped up. These tests hold the
track to that evidence and to the safety blocks it may not lift.

The market regime is no longer one of the things it lifts (2026-11-26): the same disclosure
and the same candle are planned on a day the index is above its 20-day average, and refused
when it is below. Entries taken under a headwind were the only negative bucket in both
reviews, so the closure is held here in both directions.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch

import pandas as pd

from entry_policy import MIN_CATALYST_COMPOSITE_SCORE
from catalyst_track import (confirmed_catalyst, entry_gate,
                            liftable_reason, quantified_catalyst)
from execution_quotes import parse_execution_quote
from preopen_signals import evaluate_preopen
from unittest.mock import Mock

import scanner
from scanner import telegram_candidate_line
from tradable_signal import format_requested_report, validate_entry

KST = ZoneInfo("Asia/Seoul")
# 2026-11-12 08:50 KST: ten minutes before the KRX opening, when the preopen plan is made.
AT = datetime(2026, 11, 12, 8, 50, tzinfo=KST)
FILING_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261112000111"


def company(**extra):
    """지니언스 3Q: 25,900원 close, 4.24x volume, aligned averages, headwind, rebound."""
    base = dict(market="KR", quarter="2026-09", operating_income_amount=4.1e9,
                operating_margin_pct=18.5, revenue_amount=2.2e10, revenue_yoy_pct=41.0,
                operating_income_prior_amount=1.161e9, operating_income_yoy_pct=253.1,
                financial_source="DART 연결", price_tick=50,
                financial_filing_url=FILING_URL,
                filings=[dict(filing_date="2026-11-12", form="분기보고서 (2026.09)",
                              url=FILING_URL, source="DART",
                              available_at="2026-11-12T08:05:00+09:00")],
                news=[], event_analysis=dict(economic_graph={}))
    base.update(extra)
    return base


def row(**extra):
    base = dict(market="KR", symbol="263860.KQ", name="지니언스", asset_type="stock_or_etf",
                signal=dict(confirmed=False, score=0),
                indicators=dict(price=25900.0, volume=2000000.0, volume_ratio=4.24,
                                average_turnover_20d=3.0e10, ma20=24500.0, ma60=23800.0,
                                return_5d=0.036, return_20d=-0.02, relative_strength=-0.01,
                                obv_rising=True, breakout=False, breakdown=False, rebound=True,
                                overheated=False, market_above_ma20=False, atr14_pct=4.5,
                                data_date="2026-11-11"),
                company_analysis=company(),
                company_forecast=dict(generated_at=AT.isoformat(), composite_score=51.5,
                                      assessment_status="complete", missing_dimensions=[],
                                      caveats=[], base_price=27400.0, bull_price=30500.0,
                                      catalyst_context=dict(eligible=False)))
    base.update(extra)
    return base


def lifted_row(**extra):
    """The 지니언스 case on a day the index is above its 20-day average.

    Same disclosure, same rebound candle; only the market regime differs. This is the shape
    the track still promotes, so the evidence checks keep a subject now that a headwind is
    no longer lifted.
    """
    item = row(**extra)
    item["indicators"]["market_above_ma20"] = True
    return item


def report(row):
    return dict(results=[row], preopen_session="2026-11-12", sectors=[])


class JinianceRegressionTests(unittest.TestCase):
    def message(self, item):
        """The alert the preopen delivery would send for this one row."""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake",
                                       "TELEGRAM_CHAT_ID": "fake"}), \
                patch("scanner.delivery_status", return_value=(True, "test")), \
                patch("scanner.requests.post") as post:
            post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
            scanner.send_telegram_summary(report(item))
        return post.call_args.kwargs["json"]["text"]

    def test_the_third_quarter_earnings_jump_lifts_a_rebound_when_the_index_is_above_its_average(self):
        item = lifted_row()
        self.assertEqual(liftable_reason(item, "KR"), "rebound")
        result = evaluate_preopen(report(item), "2026-11-11", AT)
        signal = item["preopen_signal"]
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(result["recommendations"], ["263860.KQ"])
        self.assertEqual(result["catalyst_candidates"], ["263860.KQ"])
        self.assertEqual(signal["strategy"], "earnings_catalyst")
        # 이 계획은 하락장 예외가 아니다(2026-11-26에 닫힘).
        self.assertFalse(signal["market_regime_lifted"])
        self.assertEqual(signal["catalyst"]["kind"], "earnings_surprise")
        self.assertIn("253", signal["catalyst"]["summary"])
        self.assertEqual(signal["catalyst"]["observed_at"], "2026-11-12T08:05:00+09:00")
        # The owner's sell policy travels with the track: 지니언스 is a single stock, so the
        # split stop gives it −3% (a fund would keep −2%).
        self.assertEqual(signal["stop_pct"], 0.03)
        self.assertEqual(signal["asset_class"], "stock")
        self.assertEqual(signal["take_profit_pct"], 0.06)
        self.assertEqual(signal["take_profit_mode"], "surge_trailing")
        # The entry is a prior-close band: no gap-up chase.
        self.assertLessEqual(signal["entry_limit"], 25900)
        self.assertLess(signal["entry_floor"], signal["entry_limit"])

    def test_the_bull_case_alone_cannot_cover_the_catalyst_reward_floor(self):
        item = lifted_row()
        item['company_forecast']['base_price'] = 27000.0
        result = evaluate_preopen(report(item), '2026-11-11', AT)
        self.assertEqual(result['recommendations'], [])
        self.assertEqual(item['preopen_decision'], 'insufficient_expected_reward')

    def test_the_alert_names_the_track_and_carries_its_evidence(self):
        item = lifted_row()
        evaluate_preopen(report(item), "2026-11-11", AT)
        text = telegram_candidate_line(item)
        self.assertIn("[실적·호재 촉매]", text)
        self.assertIn("호재:", text)
        self.assertIn("253", text)
        self.assertIn("손절 −3%", text)
        # The entry guard moved to the block heading, where it is said once (2026-11-25).
        self.assertIn("장초반 시가 아래", self.message(item))

    def test_the_same_confirmed_disclosure_is_eligible_under_a_market_headwind(self):
        item = row()
        self.assertFalse(item["indicators"]["market_above_ma20"])
        self.assertEqual(liftable_reason(item, "KR"), "rebound")
        result = evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(result["recommendations"], ["263860.KQ"])
        recorded, = result["catalyst_observation"]
        self.assertEqual(recorded["kind"], "earnings_surprise")
        self.assertFalse(recorded["market_above_ma20"])

    def test_the_alert_no_longer_announces_a_regime_exception(self):
        # 예외가 닫혔으니 '지수 20일선 아래는 이 종목만 예외' 줄도 다시 나오면 안 된다.
        item = lifted_row()
        evaluate_preopen(report(item), "2026-11-11", AT)
        text = self.message(item)
        self.assertIn("확정 공시 근거", text)
        self.assertIn("장초반 시가 아래", text)
        self.assertEqual(text.count("장초반 시가 아래"), 1)
        self.assertNotIn("20일선 아래는 이 종목만 예외", text)

    def test_a_plan_outside_a_headwind_does_not_claim_an_exception(self):
        item = lifted_row()
        evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertFalse(item["preopen_signal"]["market_regime_lifted"])
        self.assertNotIn("예외 적용", telegram_candidate_line(item))

    def test_an_ordinary_conditional_plan_is_still_held_by_the_headwind(self):
        # Only a confirmed disclosure may lift a chart rejection, and even that no longer
        # lifts the regime. A name with no catalyst keeps the regime refusal when its chart
        # is otherwise held back.
        item = row()
        item["company_analysis"] = company(filings=[], financial_filing_url="")
        item["indicators"].update(dict(price=26000.0, rebound=False, breakdown=False,
                                       ma20=24500.0, ma60=23800.0))
        result = evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertNotIn("preopen_signal", item)
        self.assertNotIn("263860.KQ", result["recommendations"])

    def test_a_listed_fund_keeps_the_two_percent_stop_on_the_same_track(self):
        from preopen_signals import catalyst_plan
        item = row(asset_type="etf")
        status, plan = catalyst_plan(item, item["company_forecast"], dict(eligible=True),
                                     AT, "2026-11-11", "2026-11-12", 51.5, "earnings_catalyst")
        self.assertEqual(status, "conditional_buy")
        self.assertEqual((plan["asset_class"], plan["stop_pct"]), ("fund", 0.02))
        self.assertEqual(plan["exit_policy"]["stop_pct"], 0.02)

    def test_a_plan_without_an_atr_band_still_shows_the_single_entry_price(self):
        item = lifted_row()
        item["indicators"].pop("atr14_pct")
        evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertIsNone(item["preopen_signal"]["entry_floor"])
        text = telegram_candidate_line(item)
        self.assertIn("[실적·호재 촉매]", text)
        self.assertIn("매수 25,900원", text)

    def test_a_verified_headline_with_the_figure_is_enough_when_the_statement_is_not_in_yet(self):
        url = "https://www.yna.co.kr/view/AKR202611120001"
        item = lifted_row()
        item["company_analysis"] = company(
            operating_income_yoy_pct=None, financial_source="DART 연결", filings=[], news=[
                dict(headline="지니언스, 3분기 영업익 253% 급증…어닝 서프라이즈", url=url,
                     available_at="2026-11-12T08:18:00+09:00", source="연합뉴스")],
            event_analysis=dict(economic_graph={}, assessed_events=[
                dict(url=url, headline="지니언스, 3분기 영업익 253% 급증…어닝 서프라이즈",
                     available_at="2026-11-12T08:18:00+09:00", body_status="public_body",
                     relevant=False, source="연합뉴스")]))
        result = evaluate_preopen(report(item), "2026-11-11", AT)
        signal = item["preopen_signal"]
        self.assertEqual(signal["strategy"], "earnings_catalyst")
        self.assertEqual(signal["catalyst"]["kind"], "confirmed_news")
        self.assertIn("253", signal["catalyst"]["summary"])
        self.assertEqual(result["catalyst_candidates"], ["263860.KQ"])

    def test_an_unread_body_is_not_a_verified_publication(self):
        url = "https://www.yna.co.kr/view/AKR202611120002"
        item = row()
        item["company_analysis"] = company(
            operating_income_yoy_pct=None, filings=[], news=[
                dict(headline="지니언스, 3분기 영업익 253% 급증", url=url,
                     available_at="2026-11-12T08:18:00+09:00")],
            event_analysis=dict(economic_graph={}, assessed_events=[
                dict(url=url, headline="지니언스, 3분기 영업익 253% 급증",
                     available_at="2026-11-12T08:18:00+09:00", body_status="unavailable")]))
        evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertEqual(item["preopen_decision"], "breakdown_or_rebound")
        self.assertNotIn("preopen_signal", item)


class SpeculationAndFreshnessTests(unittest.TestCase):
    def test_expectations_and_mou_are_not_catalysts_even_with_a_number(self):
        for headline in ("A사, 1,200억 규모 공급계약 추진",
                         "A사, 3분기 영업익 253% 증가 전망",
                         "A사, 1,500억 규모 MOU 체결",
                         "A사, 800억 수주 논의 중",
                         "A사, 실적 호조 기대"):
            self.assertFalse(quantified_catalyst(headline), headline)
        for headline in ("지니언스, 3분기 영업익 253% 급증",
                         "A사, 1,200억원 규모 공급계약 체결",
                         "A사, 3분기 매출 12.5% 증가"):
            self.assertTrue(quantified_catalyst(headline), headline)

    def test_a_stale_or_future_filing_cannot_support_todays_plan(self):
        # 실적 재료 창은 21일(분기 공시 주기)이다. 그보다 오래된 공시와 아직공개되지 않은 공시는
        # 오늘의 계획을 지지하지 못한다.
        for stamp in ("2026-10-20T08:00:00+09:00", "2026-11-12T09:30:00+09:00"):
            item = row()
            item["company_analysis"] = company(filings=[dict(
                filing_date="2026-11-12", form="분기보고서 (2026.09)", url=FILING_URL,
                available_at=stamp)])
            self.assertFalse(confirmed_catalyst(item["company_analysis"], AT, "KR")["eligible"])
            evaluate_preopen(report(item), "2026-11-11", AT)
            self.assertEqual(item["preopen_decision"], "breakdown_or_rebound")
            self.assertNotIn("preopen_signal", item)

    def test_a_quarterly_report_stays_usable_for_the_earnings_window(self):
        # 2026-11-24 테스·이수페타시스가 72시간 때문에 탈락한 자리: 8일 전 분기보고서는 살아 있다.
        for days in (1, 7, 20):
            item = row()
            at = AT + timedelta(days=days)
            item["company_forecast"]["generated_at"] = at.isoformat()
            item["company_analysis"] = company(filings=[dict(
                filing_date="2026-11-12", form="분기보고서 (2026.09)", url=FILING_URL)])
            found = confirmed_catalyst(item["company_analysis"], at, "KR")
            self.assertTrue(found["eligible"], days)
            self.assertEqual(found["kind"], "earnings_surprise")

    def test_a_loss_is_not_an_earnings_surprise(self):
        self.assertFalse(confirmed_catalyst(company(operating_income_amount=-1.2e9), AT, "KR")["eligible"])
        self.assertFalse(confirmed_catalyst(company(operating_income_yoy_pct=31.0), AT, "KR")["eligible"])
        self.assertTrue(confirmed_catalyst(
            company(operating_income_yoy_pct=None, operating_income_turnaround=True), AT, "KR")["eligible"])


class ObservationLogTests(unittest.TestCase):
    """재료 관찰 로그: 점수·문턱·진입은 그대로, 재료가 있는 종목만 기록한다."""

    def observation(self, item):
        evaluation = evaluate_preopen(report(item), "2026-11-11", AT)
        return evaluation, evaluation.get("catalyst_observation") or []

    def test_a_lifted_name_is_recorded_with_its_evidence_and_score(self):
        item = lifted_row()
        evaluation, observations = self.observation(item)
        self.assertEqual(len(observations), 1)
        found = observations[0]
        self.assertEqual(found["symbol"], "263860.KQ")
        self.assertEqual(found["kind"], "earnings_surprise")
        self.assertEqual(found["filing_date"], "2026-11-12")
        self.assertEqual(found["observed_at"], "2026-11-12T08:05:00+09:00")
        self.assertAlmostEqual(found["elapsed_days"], 0.0, places=1)
        self.assertEqual(found["blocked_reason"], "conditional_buy")
        self.assertAlmostEqual(found["composite_score"], 51.5)
        self.assertAlmostEqual(found["volume_ratio"], 4.24)
        self.assertTrue(found["volume_1_2x"])
        self.assertTrue(found["market_above_ma20"])
        self.assertEqual(found["liftable_reason"], "rebound")
        self.assertIn("영업이익", found["summary"])
        # 기록만 추가되고 판정·추천은 그대로다.
        self.assertEqual(evaluation["catalyst_candidates"], ["263860.KQ"])
        self.assertEqual(item["preopen_decision"], "conditional_buy")

    def test_a_catalyst_plan_records_its_falling_benchmark(self):
        item = row()
        evaluation, observations = self.observation(item)
        self.assertEqual(evaluation["recommendations"], ["263860.KQ"])
        observed, = observations
        self.assertEqual(observed["kind"], "earnings_surprise")
        self.assertEqual(observed["blocked_reason"], "conditional_buy")
        self.assertFalse(observed["market_above_ma20"])

    def test_a_name_the_screen_blocked_is_recorded_with_why(self):
        item = lifted_row()
        item["company_forecast"]["composite_score"] = MIN_CATALYST_COMPOSITE_SCORE - 1
        evaluation, observations = self.observation(item)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["blocked_reason"], "catalyst_score_below_policy")
        self.assertNotIn("preopen_signal", item)
        self.assertEqual(evaluation["catalyst_candidates"], [])

    def test_a_name_without_a_confirmed_catalyst_is_not_logged(self):
        item = row()
        item["company_analysis"] = company(operating_income_yoy_pct=31.0)
        _evaluation, observations = self.observation(item)
        self.assertEqual(observations, [])

    def test_a_volume_shortfall_is_visible_in_the_record(self):
        item = lifted_row()
        item["indicators"]["volume_ratio"] = 1.05
        _evaluation, observations = self.observation(item)
        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0]["volume_1_2x"])
        self.assertAlmostEqual(observations[0]["volume_ratio"], 1.05)


class SafetyBlockTests(unittest.TestCase):
    def test_extended_price_is_never_lifted_by_the_catalyst(self):
        for change in (dict(overheated=True), dict(return_5d=0.15), dict(price=26500.0)):
            item = row()
            item["indicators"].update(change)
            self.assertIsNone(liftable_reason(item, "KR"), change)
            evaluate_preopen(report(item), "2026-11-11", AT)
            self.assertEqual(item["preopen_decision"], "extended_price")
            self.assertNotIn("preopen_signal", item)

    def test_breakdown_low_price_and_liquidity_are_never_lifted(self):
        for change, expected in ((dict(breakdown=True, rebound=False), "breakdown"),
                                 (dict(price=500.0, ma20=480.0, ma60=470.0), "low_price"),
                                 (dict(average_turnover_20d=1.0), "insufficient_liquidity"),
                                 (dict(volume=0.0), "insufficient_liquidity")):
            item = row()
            item["indicators"].update(change)
            self.assertIsNone(liftable_reason(item, "KR"), change)
            evaluate_preopen(report(item), "2026-11-11", AT)
            self.assertEqual(item["preopen_decision"], expected)
            self.assertNotIn("preopen_signal", item)

    def test_a_listed_fund_is_screened_and_never_lifted(self):
        item = row()
        item["name"] = "KODEX 반도체"
        item["symbol"] = "091160.KS"
        self.assertIsNone(liftable_reason(item, "KR"))

    def test_a_catalyst_does_not_override_the_score_floor(self):
        item = lifted_row()
        item["company_forecast"]["composite_score"] = MIN_CATALYST_COMPOSITE_SCORE - 1
        evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertEqual(item["preopen_decision"], "catalyst_score_below_policy")
        self.assertNotIn("preopen_signal", item)


class MarketDirectionTests(unittest.TestCase):

    def aligned_row(self, market_above_ma20):
        """A chart that passes every first-screen test except possibly the market regime."""
        item = row()
        item["indicators"].update(dict(price=25900.0, rebound=False, breakdown=False,
                                       ma20=24500.0, ma60=23800.0, return_5d=0.03,
                                       return_20d=0.05, relative_strength=0.02,
                                       obv_rising=True, volume_ratio=2.4,
                                       market_above_ma20=market_above_ma20))
        return item

    def test_a_chart_that_passes_its_own_filters_can_plan_under_a_falling_index(self):
        from candidate_selection import candidate_rejection_reason
        item = self.aligned_row(False)
        item["company_forecast"]["composite_score"] = 60.0
        self.assertIsNone(candidate_rejection_reason(item, "KR"))
        self.assertIsNone(liftable_reason(item, "KR"))
        result = evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertEqual(item["preopen_signal"]["strategy"], "trend")
        self.assertEqual(result["recommendations"], ["263860.KQ"])

    def test_the_same_chart_is_an_ordinary_plan_on_a_day_the_index_is_above_its_average(self):
        item = self.aligned_row(True)
        self.assertIsNone(liftable_reason(item, "KR"))
        item["company_forecast"]["composite_score"] = 60.0
        result = evaluate_preopen(report(item), "2026-11-11", AT)
        self.assertEqual(item["preopen_decision"], "conditional_buy")
        self.assertEqual(result["recommendations"], ["263860.KQ"])
        # 정배열 경로로 들어온 계획이지 촉매 트랙이 아니다.
        self.assertEqual(item["preopen_signal"]["strategy"], "trend")

    def test_aligned_stock_under_falling_index_uses_the_normal_research_track(self):
        from catalyst_track import watch_candidates, watchable_reason
        item = self.aligned_row(False)
        self.assertIsNone(watchable_reason(item, "KR"))
        self.assertEqual(watch_candidates([item], "KR"), [])

    def test_a_safety_block_is_not_even_watched(self):
        from catalyst_track import watchable_reason
        for change in (dict(overheated=True), dict(breakdown=True, rebound=False),
                       dict(average_turnover_20d=1.0)):
            item = self.aligned_row(False)
            item["indicators"].update(change)
            self.assertIsNone(watchable_reason(item, "KR"), change)


class SellOnNewsTests(unittest.TestCase):
    def test_a_catalyst_name_is_blocked_while_it_trades_under_its_open(self):
        tone = entry_gate(26300, 26500, 2.0)
        self.assertFalse(tone["allowed"])
        self.assertEqual(tone["reason"], "catalyst_sell_on_news")
        self.assertIn("시가", tone["detail"])

    def test_thin_same_time_volume_is_blocked_even_above_the_open(self):
        tone = entry_gate(26600, 26500, 0.9)
        self.assertEqual(tone["reason"], "catalyst_demand_shortfall")
        self.assertFalse(entry_gate(26600, 26500, None)["allowed"])
        self.assertEqual(entry_gate(26600, 26500, None)["reason"], "catalyst_volume_unverified")
        self.assertEqual(entry_gate(26300, None, 2.0)["reason"], "catalyst_session_unverified")
        self.assertTrue(entry_gate(26600, 26500, 1.7)["allowed"])
        self.assertTrue(entry_gate(26500, 26500, 1.7)["allowed"])


class IntradayGateTests(unittest.TestCase):
    closes = datetime(2026, 11, 12, 9, 15, tzinfo=KST)

    def bars(self, open_price, closes, volumes):
        return {slot: dict(open=open_price if slot == 0 else closes[slot - 1],
                           high=max(closes[slot], open_price), low=min(closes[slot], open_price),
                           close=closes[slot], volume=volumes[slot], last=self.closes.isoformat())
                for slot in range(3)}

    def quote(self, ask):
        return dict(ask=ask, bid=ask - 10, spread_pct=0.04, event_time=self.closes.isoformat())

    def evaluate(self, bars, ask, volume_baseline, company=None):
        from kis_intraday import evaluate_candidate
        return evaluate_candidate(bars, self.quote(ask), self.closes, "KRX", "263860", [],
                                  False, company=company, volume_baseline=volume_baseline)

    def test_intraday_entry_is_refused_when_the_session_is_selling_the_news(self):
        status, _candidate = self.evaluate(
            self.bars(26500, [26000, 26200, 26300], [100, 100, 100]), 26300, 1.0, company())
        self.assertEqual(status, "catalyst_sell_on_news")

    def test_intraday_entry_is_refused_when_same_time_volume_does_not_expand(self):
        status, _candidate = self.evaluate(
            self.bars(26500, [26600, 26700, 26800], [100, 100, 5]), 26800, 10.0, company())
        self.assertEqual(status, "catalyst_demand_shortfall")

    def test_a_name_without_a_confirmed_catalyst_is_not_gated(self):
        status, _candidate = self.evaluate(
            self.bars(26500, [26000, 26200, 26300], [100, 100, 100]), 26300, 1.0,
            dict(market="KR", filings=[], news=[], event_analysis=dict(economic_graph={})))
        self.assertEqual(status, "no_price_breakout")

    def test_a_held_open_on_expanding_volume_passes_the_gate(self):
        bars = self.bars(26500, [26600, 26700, 26800], [100, 100, 500])
        status, _candidate = self.evaluate(bars, 26800, 1.0, company())
        self.assertNotIn(status, ("catalyst_sell_on_news", "catalyst_demand_shortfall"))


class ExecutionCheckTests(unittest.TestCase):
    at = datetime(2026, 11, 12, 9, 10, tzinfo=KST)

    def fixture(self):
        item = dict(symbol="263860.KQ", name="지니언스", preopen_signal=dict(
            status="conditional_buy", expires_session="2026-11-12", entry_limit=25900,
            entry_floor=25300, reference_price=25900, reference_date="2026-11-11",
            strategy="earnings_catalyst", stop_pct=0.02, take_profit_pct=0.06,
            catalyst=dict(eligible=True, kind="earnings_surprise",
                          summary="2026년 3분기 영업이익 전년 대비 +253% (DART 실적 공시)",
                          observed_at="2026-11-12T08:05:00+09:00")),
            company_forecast=dict(assessment_status="complete", missing_dimensions=[],
                                  composite_score=51.5,
                                  generated_at=(self.at - timedelta(minutes=1)).isoformat(),
                                  base_price=27400, bull_price=30500))
        quote = dict(ask=25900, bid=25890, ask_size=100, bid_size=100,
                     observed_at=self.at.isoformat(),
                     trade_at=(self.at - timedelta(seconds=30)).isoformat(),
                     day_open=26500, last_price=26600, volume_ratio=1.7)
        return item, quote

    def test_the_execution_check_reads_the_same_score_floor_as_the_plan(self):
        item, quote = self.fixture()
        signal = validate_entry(item, quote, self.at, True)
        self.assertTrue(signal["trade_ready"])
        self.assertEqual(signal["catalyst"]["kind"], "earnings_surprise")
        self.assertIn("호재:", format_requested_report([], [signal], dict(completed=True), self.at))
        item["company_forecast"]["composite_score"] = MIN_CATALYST_COMPOSITE_SCORE - 1
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"], "score_below_policy")

    def test_an_unverified_catalyst_is_refused_at_the_execution_check(self):
        item, quote = self.fixture()
        item["preopen_signal"]["catalyst"]["eligible"] = False
        self.assertEqual(validate_entry(item, quote, self.at, True)["reason"], "catalyst_not_verified")

    def test_the_entry_is_refused_below_the_open_or_on_thin_volume(self):
        for change, reason in ((dict(last_price=26200), "catalyst_sell_on_news"),
                               (dict(volume_ratio=0.8), "catalyst_demand_shortfall"),
                               (dict(volume_ratio=None), "catalyst_volume_unverified"),
                               (dict(day_open=None), "catalyst_session_unverified")):
            item, quote = self.fixture()
            quote.update(change)
            signal = validate_entry(item, quote, self.at, True)
            self.assertFalse(signal["trade_ready"], change)
            self.assertEqual(signal["reason"], reason, change)
            self.assertTrue(signal["detail"])


class QuoteEvidenceTests(unittest.TestCase):
    def test_the_quote_carries_the_open_last_price_and_same_time_volume(self):
        at = datetime(2026, 11, 12, 9, 10, tzinfo=KST)
        book = dict(output1=dict(new_mkop_cls_code="20", aspr_acpt_hour="091005",
                                 askp1="25900", bidp1="25890",
                                 askp_rsqn1="100", bidp_rsqn1="100"),
                    output2=dict(vi_cls_code="N"))
        minutes = dict(output2=[
            dict(stck_bsop_date="20261112", stck_cntg_hour=f"09{minute:02d}00",
                 stck_prpr="26500" if minute == 0 else "26100",
                 stck_oprc="26500" if minute == 0 else "26550", cntg_vol="20")
            for minute in range(10)
        ])
        prior = dict(output2=[
            dict(stck_bsop_date="20261111", stck_cntg_hour=f"09{minute:02d}00", cntg_vol="10")
            for minute in range(10)
        ])
        quote = parse_execution_quote(book, minutes, at, previous_minutes=prior)
        self.assertEqual(quote["day_open"], 26500)
        self.assertEqual(quote["last_price"], 26100)
        self.assertEqual(quote["cumulative_volume"], 200)
        # Today's cumulative 200 against the prior session's same-time 100.
        self.assertEqual(quote["volume_ratio"], 2.0)
        tone = entry_gate(quote["last_price"], quote["day_open"], quote["volume_ratio"])
        self.assertEqual(tone["reason"], "catalyst_sell_on_news")


class SelectionTests(unittest.TestCase):
    def test_a_rejected_name_gets_a_research_slot_but_a_blocked_one_does_not(self):
        from economic_graph import select_analysis_candidates
        weak = row()
        # Negative 5-day return: not a recovery candidate either, so only the catalyst
        # budget can bring this name into the analysis set.
        weak["indicators"].update(rebound=False, return_5d=-0.01, return_20d=-0.03,
                                  relative_strength=-0.02)
        overheated = row(symbol="000660.KQ")
        overheated["indicators"].update(overheated=True)
        broken = row(symbol="035420.KQ")
        broken["indicators"].update(breakdown=True)
        selected = select_analysis_candidates([weak, overheated, broken], "KR", (), limit=10)
        reasons = {item["symbol"]: item.get("selection_reason") for item in selected}
        self.assertIn("263860.KQ", reasons)
        self.assertEqual(reasons["263860.KQ"], "catalyst_watch")
        self.assertNotEqual(reasons.get("000660.KQ"), "catalyst_watch")
        self.assertNotEqual(reasons.get("035420.KQ"), "catalyst_watch")
        # 하락장이라 계획은 없다. 그러나 이 이름은 여전히 리서치 풀에 남는다(2026-11-26).
        self.assertEqual(liftable_reason(weak, "KR"), "weak_relative_trend")


class MarketMergeTests(unittest.TestCase):
    def test_a_us_results_release_uses_the_same_track_and_merges_across_markets(self):
        from preopen_signals import evaluate_preopen_markets
        us = dict(market="US", symbol="TEST", name="Test Corp", asset_type="stock_or_etf",
                  signal=dict(confirmed=False, score=0),
                  indicators=dict(price=100.0, volume=400000.0, volume_ratio=3.0,
                                  average_turnover_20d=4.0e7, ma20=95.0, ma60=90.0,
                                  return_5d=0.02, return_20d=-0.01, relative_strength=-0.02,
                                  obv_rising=True, breakout=False, breakdown=False,
                                  rebound=True, overheated=False, market_above_ma20=True,
                                  atr14_pct=3.0, data_date="2026-11-11"),
                  company_analysis=dict(market="US", quarter="2026-09",
                                        operating_income_amount=4.0e7,
                                        operating_income_yoy_pct=120.0, price_tick=0.01,
                                        filings=[dict(form="8-K", items=["2.02"],
                                                      accepted_at="2026-11-11T12:00:00+00:00",
                                                      url="https://www.sec.gov/Archives/x")],
                                        news=[], event_analysis=dict(economic_graph={})),
                  company_forecast=dict(generated_at=AT.isoformat(), composite_score=50.0,
                                        assessment_status="complete", missing_dimensions=[],
                                        caveats=[], base_price=106.0, bull_price=112.0,
                                        catalyst_context=dict(eligible=False)))
        kr = lifted_row()
        merged, parts = evaluate_preopen_markets(dict(results=[kr, us], sectors=[]), AT)
        self.assertEqual(us["preopen_signal"]["strategy"], "earnings_catalyst")
        self.assertEqual(us["preopen_decision"], "conditional_buy")
        self.assertEqual(sorted(parts), ["KR", "US"])
        self.assertEqual(sorted(merged["catalyst_candidates"]), ["263860.KQ", "TEST"])


class StatementFieldTests(unittest.TestCase):
    def test_the_income_statement_yields_the_operating_profit_change(self):
        from kis_intraday import company_financials
        statement = pd.DataFrame(
            {pd.Timestamp("2026-09-30"): [2.2e10, 4.1e9],
             pd.Timestamp("2025-09-30"): [1.6e10, 1.161e9]},
            index=["TotalRevenue", "OperatingIncome"])
        result = company_financials(statement, AT.date())
        self.assertAlmostEqual(result["operating_income_yoy_pct"], 253.1, places=1)
        self.assertAlmostEqual(result["revenue_yoy_pct"], 37.5, places=1)

    def test_the_dart_quarter_reading_yields_the_same_change(self):
        import kis_intraday
        receipt = "20261112000111"
        payload = dict(status="000", list=[
            dict(account_id="ifrs-full_Revenue", sj_div="IS", rcept_no=receipt,
                 thstrm_amount="22,000,000,000", frmtrm_q_amount="16,000,000,000",
                 thstrm_add_amount="60,000,000,000", frmtrm_add_amount="45,000,000,000"),
            dict(account_id="dart_OperatingIncomeLoss", sj_div="IS", rcept_no=receipt,
                 thstrm_amount="4,100,000,000", frmtrm_q_amount="1,161,000,000")])
        filings = [dict(form="분기보고서 (2026.09)", url=FILING_URL)]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch.object(kis_intraday.requests, "get", return_value=Response()):
            snapshot = kis_intraday.dart_quarterly_financials("key", "00123456", filings)
        self.assertAlmostEqual(snapshot["operating_income_yoy_pct"], 253.1, places=1)
        self.assertEqual(snapshot["operating_income_prior_amount"], 1_161_000_000)
        item = row()
        item["company_analysis"] = company(**snapshot)
        self.assertEqual(confirmed_catalyst(item["company_analysis"], AT, "KR")["kind"],
                         "earnings_surprise")


if __name__ == "__main__":
    unittest.main()
