"""Regression tests for the alert delivery gate and the decision context.

F-01: the intraday alert was judged by the preopen plan window, so every decision
made 15 minutes after an opening was silently dropped. F-08: nothing tested the
send path or the context gate, so that regression survived a green suite.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import kis_intraday as k
from backtest_intraday import replay
from company_forecast import MIN_COMPOSITE_SCORE
from signal_context import context_status

KST = k.KST
DECISION = datetime(2026, 11, 20, 9, 15, tzinfo=KST)  # a Friday XKRX session
CANDIDATES = [("005930", 2.4, 0.1, 100.2, 100.3, 98.0, (105.0, "종합평가 5거래일 시나리오"))]


class Clock(datetime):
    current = DECISION + timedelta(seconds=20)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current


def telegram_ok(*_args, **_kwargs):
    return Mock(status_code=200, json=lambda: {"ok": True})


class IntradayGate(unittest.TestCase):
    def setUp(self):
        k.SENT_INTRADAY.clear()
        Clock.current = DECISION + timedelta(seconds=20)

    def test_decision_at_nine_fifteen_is_delivered_not_suppressed(self):
        with patch.object(k, "datetime", Clock), \
                patch.object(k, "format_notification", return_value="매수 신호 · 테스트"), \
                patch.object(k.requests, "post", side_effect=telegram_ok) as post:
            result = k.notify("KRX", CANDIDATES, DECISION, "token", "chat")
        self.assertTrue(post.called)
        self.assertTrue(result["sent"])
        self.assertFalse(result["suppressed_by_gate"])
        self.assertEqual(result["reason"], "within_intraday_decision_window")
        self.assertEqual(result["confirmed_buys"], 1)

    def test_the_preopen_window_no_longer_governs_an_intraday_alert(self):
        allowed, reason = k.delivery_status("KRX", DECISION)
        self.assertFalse(allowed)
        self.assertEqual(reason, "missed_delivery_window")
        status, detail = k.intraday_delivery_status("KRX", DECISION + timedelta(seconds=20), DECISION)
        self.assertTrue(status, detail)

    def test_preopen_mode_still_uses_the_preopen_window(self):
        with patch.object(k, "datetime", Clock), \
                patch.object(k.requests, "post", side_effect=telegram_ok) as post:
            result = k.notify("KRX", CANDIDATES, DECISION, "token", "chat", mode="preopen")
        self.assertFalse(post.called)
        self.assertTrue(result["suppressed_by_gate"])
        self.assertEqual(result["reason"], "missed_delivery_window")

    def test_a_future_decision_is_not_sent(self):
        allowed, reason = k.intraday_delivery_status("KRX", DECISION - timedelta(minutes=1), DECISION)
        self.assertFalse(allowed)
        self.assertEqual(reason, "before_decision")

    def test_a_stale_decision_is_not_sent(self):
        allowed, reason = k.intraday_delivery_status(
            "KRX", DECISION + k.INTRADAY_DECISION_GRACE + timedelta(seconds=1), DECISION)
        self.assertFalse(allowed)
        self.assertEqual(reason, "decision_expired")

    def test_a_closed_session_day_is_not_sent(self):
        saturday = datetime(2026, 11, 21, 9, 15, tzinfo=KST)
        allowed, reason = k.intraday_delivery_status("KRX", saturday + timedelta(seconds=20), saturday)
        self.assertFalse(allowed)
        self.assertEqual(reason, "closed_session")

    def test_the_same_signal_is_not_sent_twice(self):
        with patch.object(k, "datetime", Clock), \
                patch.object(k, "format_notification", return_value="매수 신호 · 테스트"), \
                patch.object(k.requests, "post", side_effect=telegram_ok) as post:
            first = k.notify("KRX", CANDIDATES, DECISION, "token", "chat")
            second = k.notify("KRX", CANDIDATES, DECISION, "token", "chat")
        self.assertTrue(first["sent"])
        self.assertEqual(post.call_count, 1)
        self.assertTrue(second["suppressed_by_gate"])
        self.assertEqual(second["reason"], "duplicate_signal")

    def test_us_premarket_times_are_inside_the_gate(self):
        opens = datetime(2026, 11, 20, 8, 20, tzinfo=k.ZoneInfo("America/New_York"))
        decision = opens + timedelta(minutes=15)
        allowed, reason = k.intraday_delivery_status(
            "US-late", decision + timedelta(seconds=20), decision)
        self.assertTrue(allowed, reason)


class ContextGate(unittest.TestCase):
    def records(self, **overrides):
        base = dict(kind="market", market="KRX", symbol="*", assessment="supportive",
                    available_at=(DECISION - timedelta(minutes=5)).isoformat())
        return [{**base, **overrides}]

    def test_market_and_event_alone_are_supported(self):
        rows = self.records() + [{**self.records()[0], "kind": "event", "assessment": "clear"}]
        self.assertEqual(context_status(rows, "KRX", "005930", DECISION), "supported")

    def test_supportive_research_is_used_and_does_not_block(self):
        rows = self.records() + [
            {**self.records()[0], "kind": "event", "assessment": "clear"},
            {**self.records()[0], "kind": "research", "symbol": "005930", "assessment": "supportive"},
        ]
        self.assertEqual(context_status(rows, "KRX", "005930", DECISION), "supported")

    def test_negative_research_blocks_the_entry(self):
        rows = self.records() + [
            {**self.records()[0], "kind": "event", "assessment": "clear"},
            {**self.records()[0], "kind": "research", "symbol": "005930", "assessment": "risk"},
        ]
        self.assertEqual(context_status(rows, "KRX", "005930", DECISION), "research_negative")

    def test_research_outside_its_window_is_not_used(self):
        rows = self.records() + [
            {**self.records()[0], "kind": "event", "assessment": "clear"},
            {**self.records()[0], "kind": "research", "symbol": "005930", "assessment": "risk",
             "available_at": (DECISION - timedelta(days=8)).isoformat()},
        ]
        self.assertEqual(context_status(rows, "KRX", "005930", DECISION), "supported")

    def test_unknown_kind_is_ignored_instead_of_raising(self):
        rows = self.records() + [
            {**self.records()[0], "kind": "event", "assessment": "clear"},
            {**self.records()[0], "kind": "news", "assessment": "clear"},
        ]
        self.assertEqual(context_status(rows, "KRX", "005930", DECISION), "supported")


def sample(volume_baseline=None):
    row = dict(market="KRX", symbol="005930", signal_at=DECISION.isoformat(), daily_eligible=True,
               prior_highs=[],
               bars=[dict(start=(DECISION - timedelta(minutes=15)).isoformat(), high=100.0, low=99.0,
                          close=99.9, volume=1000),
                     dict(start=(DECISION - timedelta(minutes=10)).isoformat(), high=100.0, low=99.5,
                          close=100.0, volume=400),
                     dict(start=(DECISION - timedelta(minutes=5)).isoformat(), high=100.3, low=99.9,
                          close=100.2, volume=300)],
               quotes=[dict(at=(DECISION - timedelta(seconds=5)).isoformat(), bid=100.1, ask=100.2),
                       dict(at=(DECISION + timedelta(seconds=10)).isoformat(), bid=100.1, ask=100.2),
                       dict(at=(DECISION + timedelta(minutes=5)).isoformat(), bid=101.0, ask=101.1),
                       dict(at=(DECISION + timedelta(minutes=29)).isoformat(), bid=101.0, ask=101.1),
                       dict(at=(DECISION + timedelta(minutes=30)).isoformat(), bid=101.0, ask=101.1)],
               post_bars=[])
    if volume_baseline is not None:
        row["volume_baseline"] = volume_baseline
    return row


class ReplayUsesTheOperatingVolumeRule(unittest.TestCase):
    def test_recorded_slot_baseline_is_used_and_reported(self):
        result = replay(sample(140.0), 10)
        self.assertEqual(result["volume_basis"], "same_slot_prior_sessions")
        self.assertEqual(result["status"], "evaluated")

    def test_a_sample_without_a_baseline_says_it_used_the_fallback(self):
        result = replay(sample(), 10)
        self.assertEqual(result["volume_basis"], "opening_share_fallback")
        self.assertEqual(result["status"], "no_signal")


class OneCompositeThreshold(unittest.TestCase):
    def forecast(self, score):
        return dict(composite_score=score, base_price=110.0, bull_price=120.0,
                    assessment_status="complete", missing_dimensions=[])

    def evaluate(self, score):
        bars = dict(enumerate(sample()["bars"]))
        bars = {index: {key: bar[key] for key in ("high", "low", "close", "volume")}
                for index, bar in bars.items()}
        quote = dict(event_time=(DECISION - timedelta(seconds=5)).isoformat(),
                     spread_pct=0.1, ask=100.2, bid=100.1)
        evidence = [dict(kind="market", market="KRX", symbol="*", assessment="supportive",
                         available_at=(DECISION - timedelta(minutes=5)).isoformat()),
                    dict(kind="event", market="KRX", symbol="*", assessment="clear",
                         available_at=(DECISION - timedelta(minutes=5)).isoformat())]
        with patch.object(k, "build_company_forecast", return_value=self.forecast(score)):
            return k.evaluate_candidate(bars, quote, DECISION, "KRX", "005930", evidence, False,
                                        company={}, sectors=[], volume_baseline=140.0)[0]

    def test_a_candidate_below_sixty_points_is_refused_intraday_too(self):
        self.assertEqual(self.evaluate(MIN_COMPOSITE_SCORE - 5), "composite_outlook_not_positive")

    def test_a_candidate_at_the_threshold_can_be_bought(self):
        self.assertEqual(self.evaluate(MIN_COMPOSITE_SCORE), "buy_signal")


if __name__ == "__main__":
    unittest.main()
