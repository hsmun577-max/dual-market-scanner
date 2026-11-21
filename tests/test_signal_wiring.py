import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import kis_intraday as scanner
from preopen_signals import evaluate_preopen, evaluate_preopen_markets, session_dates

AT = datetime(2026, 11, 20, 6, 45, tzinfo=timezone.utc)


def forecast(score=70, status="complete", caveats=(), base_price=10600.0):
    return dict(generated_at=AT.isoformat(), composite_score=score, assessment_status=status,
                caveats=list(caveats), missing_dimensions=[], base_price=base_price,
                bull_price=11000.0, catalyst_context=dict(eligible=False))


def row(market="KR", symbol="005930.KS", data_date="2026-11-19", score=70):
    return dict(market=market, symbol=symbol, name="삼성전자" if market == "KR" else "애플",
                signal=dict(confirmed=False, score=0),
                indicators=dict(price=10000.0, volume=2000000.0, volume_ratio=2.0,
                                average_turnover_20d=20000000000.0, ma20=9900.0, ma60=9800.0,
                                return_5d=0.03, return_20d=0.1, relative_strength=0.05,
                                obv_rising=True, breakout=True, data_date=data_date),
                company_analysis=dict(event_analysis=dict(economic_graph={})),
                company_forecast=forecast(score))


class MultiMarketSessionTests(unittest.TestCase):
    def test_each_market_is_planned_from_its_own_completed_session(self):
        report = dict(results=[row("KR", "005930.KS", "2026-11-19"),
                               row("US", "AAPL", "2026-11-18")], sectors=[])
        merged, parts = evaluate_preopen_markets(report, AT)
        self.assertEqual(sorted(parts), ["KR", "US"])
        self.assertEqual(parts["KR"]["previous_session"], "2026-11-19")
        self.assertEqual(parts["US"]["previous_session"], "2026-11-18")
        self.assertEqual(merged["recommendations"], ["005930.KS", "AAPL"])
        self.assertEqual(report["results"][0]["preopen_signal"]["reference_date"], "2026-11-19")
        self.assertEqual(report["results"][1]["preopen_signal"]["reference_date"], "2026-11-18")
        self.assertEqual(merged["evaluated"], 2)
        self.assertFalse(merged["live_execution_confirmed"])

    def test_a_stale_row_cannot_borrow_the_other_markets_session(self):
        report = dict(results=[row("KR", "005930.KS", "2026-11-19"),
                               row("KR", "000660.KS", "2026-11-14")], sectors=[])
        evaluate_preopen_markets(report, AT)
        self.assertEqual(report["results"][1]["preopen_decision"], "price_session_mismatch")
        self.assertNotIn("preopen_signal", report["results"][1])

    def test_sessions_come_from_the_scanned_bars(self):
        report = dict(results=[row("KR", "005930.KS", "2026-11-19")])
        self.assertEqual(session_dates(report, AT), {"KR": "2026-11-19"})

    def test_a_callers_session_is_not_replaced_by_the_per_market_map(self):
        report = dict(results=[row("KR", "005930.KS", "2026-11-19")], sectors=[],
                      preopen_session="2026-11-19")
        evaluate_preopen_markets(report, AT)
        self.assertEqual(report["preopen_session"], "2026-11-19")
        self.assertEqual(report["preopen_sessions"], {"KR": "2026-11-19"})
        self.assertEqual(report["results"][0]["preopen_signal"]["expires_session"], "2026-11-19")

    def test_a_partial_score_is_planned_and_labelled(self):
        report = dict(results=[row("KR", "005930.KS")], sectors=[])
        report["results"][0]["company_forecast"] = forecast(70, "partial", ["valuation_outlook"])
        result = evaluate_preopen(report, "2026-11-19", AT)
        self.assertEqual(result["recommendations"], ["005930.KS"])
        self.assertEqual(result["caveats"], {"valuation_outlook": 1})
        self.assertEqual(report["results"][0]["preopen_signal"]["caveats"], ["valuation_outlook"])


class ExitWatchTests(unittest.TestCase):
    def candidate(self):
        return ("005930", 2.0, 0.1, 10100.0, 10000.0, 9800.0, (10600.0, "시나리오"))

    def bars(self, prices, volumes=None):
        volumes = volumes or [100] * len(prices)
        return {slot: dict(high=price, low=price, close=price, volume=volume)
                for slot, (price, volume) in enumerate(zip(prices, volumes, strict=True))}

    def test_the_six_percent_level_is_sold_when_the_rise_was_not_a_surge(self):
        decisions = scanner.exit_watch([self.candidate()], {"005930": self.bars([10000, 10000])},
                                       {"005930": self.bars([10400, 10700])}, AT)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"]["reason"], "take_profit")
        self.assertEqual(decisions[0]["decision"]["exit_price"], 10600.0)
        text = scanner.format_exit_watch("KRX", decisions, AT)
        self.assertIn("기본 익절", text)
        self.assertIn("실제 체결은 확인되지 않았습니다", text)

    def test_a_surge_on_volume_holds_past_six_percent_and_exits_on_the_collapse(self):
        after = self.bars([10700, 11000, 10700], volumes=[400, 400, 60])
        decisions = scanner.exit_watch([self.candidate()], {"005930": self.bars([10000, 10000])},
                                       {"005930": after}, AT)
        decision = decisions[0]["decision"]
        self.assertEqual(decision["action"], "exit")
        self.assertEqual(decision["reason"], "surge_trailing_exit")
        # The exit price is the observed price when the collapse appeared, above the +6%
        # floor and below the peak: the price before the plunge, not its bottom.
        self.assertEqual(decision["exit_price"], 10700.0)
        self.assertLess(decision["exit_price"], decision["peak"])
        self.assertGreaterEqual(decision["exit_price"], decision["floor_price"])

    def test_without_post_signal_bars_no_sell_claim_is_made(self):
        self.assertEqual(scanner.exit_watch([self.candidate()], {"005930": self.bars([10000])}, {}, AT), [])

    def test_the_stop_level_is_reported_when_the_bid_falls_through_it(self):
        decisions = scanner.exit_watch([self.candidate()], {"005930": self.bars([10000, 10000])},
                                       {"005930": self.bars([9900, 9700])}, AT)
        self.assertEqual(decisions[0]["decision"]["reason"], "stop_loss")
        self.assertIn("손절", scanner.format_exit_watch("KRX", decisions, AT))

    def test_the_exit_message_never_claims_a_fill(self):
        decisions = scanner.exit_watch([self.candidate()], {"005930": self.bars([10000, 10000])},
                                       {"005930": self.bars([10100])}, AT)
        text = scanner.format_exit_watch("KRX", decisions, AT)
        self.assertIn("보유", text)
        for wording in ("체결가", "매수 완료", "보유 중"):
            self.assertNotIn(wording, text)


class Clock(datetime):
    """Clock anchored at the session the tests collect, one second before the open."""

    current = datetime(2026, 11, 18, 8, 59, 59, tzinfo=scanner.KST)

    @classmethod
    def reset(cls):
        cls.current = datetime(2026, 11, 18, 8, 59, 59, tzinfo=scanner.KST)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


class Socket:
    """A feed that accepts subscriptions, then reports the clock moving to the decision."""

    def __init__(self, opens):
        self.pending = list(scanner.FEEDS["KRX"])
        self.minutes = iter([5, 6, 15, 46])
        self.opens = opens

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        pass

    async def pong(self, message):
        pass

    async def recv(self, **kwargs):
        await asyncio.sleep(0.01)
        if self.pending:
            return json.dumps({"header": {"tr_id": self.pending.pop(), "tr_key": "005930"},
                               "body": {"rt_cd": "0"}})
        Clock.current = self.opens + timedelta(minutes=next(self.minutes))
        return json.dumps({"header": {"tr_id": "PINGPONG"}})


class LateSubscriptionTests(unittest.TestCase):
    opens = datetime(2026, 11, 18, 9, tzinfo=scanner.KST)

    def test_a_reconnect_after_the_open_keeps_symbols_that_already_have_bars(self):
        complete = {0: {}, 1: {}, 2: {}}
        self.assertFalse(scanner.late_subscription(
            "0", self.opens + timedelta(minutes=5), self.opens, complete))

    def test_an_ack_after_the_open_without_bars_is_rejected(self):
        self.assertTrue(scanner.late_subscription(
            "0", self.opens + timedelta(minutes=5), self.opens, {0: {}, 1: {}}))
        self.assertTrue(scanner.late_subscription(
            "1", self.opens - timedelta(seconds=1), self.opens, {}))


class ReconnectTests(unittest.TestCase):
    opens = datetime(2026, 11, 18, 9, tzinfo=scanner.KST)

    def setUp(self):
        Clock.reset()

    def run_collect(self, connect, rest):
        published = []

        def candidate(*args, **kwargs):
            return "buy_signal", ("005930", 2.0, 0.1, 100.0, 100.5, 98.5, (110.0, "시나리오"))

        with TemporaryDirectory() as directory, \
                patch.object(scanner, "datetime", Clock), \
                patch.object(scanner, "RECONNECT_BACKOFF_SECONDS", (0, 0, 0, 0)), \
                patch.object(scanner, "connect", connect), \
                patch.object(scanner, "evaluate_candidate", side_effect=candidate), \
                patch("rest_fallback.market_snapshot", return_value=rest):
            path = Path(directory) / "KRX.jsonl"
            asyncio.run(scanner.collect("fake", "KRX", ["005930"], self.opens,
                                        lambda rows: published.append(rows), path, []))
            return published, path.read_text(encoding="utf-8") if path.exists() else ""

    def test_a_dropped_feed_is_reopened_with_the_bars_already_collected(self):
        calls = []

        def connect(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection closed")
            return Socket(self.opens)

        published, _ = self.run_collect(connect, ({}, {}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][0][0], "005930")

    def test_the_rest_bars_publish_a_decision_when_the_feed_stays_down(self):
        def connect(*args, **kwargs):
            raise OSError("connection closed")

        bars = {"005930": {0: dict(high=100.0, low=99.0, close=100.0, volume=10),
                           1: dict(high=100.0, low=99.0, close=100.0, volume=10),
                           2: dict(high=101.0, low=100.0, close=101.0, volume=30)}}
        quotes = {"005930": dict(bid=100.9, ask=101.0, spread_pct=0.1,
                                 event_time=Clock.now(scanner.KST).isoformat())}
        published, samples = self.run_collect(connect, (bars, quotes))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][0][0], "005930")
        self.assertIn('"005930"', samples)

    def test_without_any_prices_the_run_reports_the_failure(self):
        def connect(*args, **kwargs):
            raise OSError("connection closed")

        with self.assertRaises(RuntimeError):
            self.run_collect(connect, ({}, {}))


if __name__ == "__main__":
    unittest.main()
