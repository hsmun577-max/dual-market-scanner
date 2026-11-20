"""Regression tests for the structural defects that kept signals from being produced.

* sector excess return must survive an unfinished or lagging benchmark index bar
* a local index bar one session behind must not abandon the market verdict
* the third opening five-minute bar must be judged against its own clock slot
"""

import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

from kis_intraday import (KR_SYMBOL_NAMES, MARKET_INDEX_MAX_LAG_DAYS, MIN_VOLUME_RATIO, KST,
                          evaluate_breakout, evaluate_candidate, market_evidence,
                          volume_baselines, volume_reference)
from scanner import sector_metrics

OPENS = datetime(2026, 11, 20, 9, 0, tzinfo=KST)
QUOTE = dict(event_time=(OPENS + timedelta(minutes=15, seconds=-1)).isoformat(),
             spread_pct=0.1, ask=100.2, bid=100.1)
EVIDENCE = [dict(kind=kind, market="KRX", symbol="*", assessment=grade,
                 available_at=(OPENS - timedelta(minutes=1)).isoformat())
            for kind, grade in (("market", "supportive"), ("event", "clear"))]


def daily(closes, volumes):
    index = pd.bdate_range("2026-09-01", periods=len(closes))
    values = np.asarray(closes, dtype=float)
    return pd.DataFrame({"Open": values, "High": values, "Low": values, "Close": values,
                         "Adj Close": values, "Volume": np.asarray(volumes, dtype=float)},
                        index=index)


def sector_rows():
    closes = [100.0] * 28 + [101.0, 102.0]
    volumes = [100.0] * 29 + [300.0]
    frame = daily(closes, volumes)
    return frame, frame.index


def benchmark_frame(index, closes=None):
    values = [100.0] * len(index) if closes is None else closes
    return pd.DataFrame({"Close": np.asarray(values, dtype=float),
                         "Adj Close": np.asarray(values, dtype=float)}, index=index)


class SectorBenchmarkAlignment(unittest.TestCase):
    def test_unfinished_index_bar_does_not_erase_the_excess_return(self):
        frame, index = sector_rows()
        benchmark = benchmark_frame(index)
        benchmark.loc[index[-1], "Close"] = np.nan
        metrics = sector_metrics(frame, benchmark)
        self.assertIsNotNone(metrics["market_excess_pct"])
        self.assertAlmostEqual(metrics["market_excess_pct"], 1.0, places=4)
        self.assertEqual(metrics["data_date"], index[-2].date().isoformat())
        self.assertEqual(metrics["benchmark_lag_sessions"], 1)

    def test_index_one_session_behind_is_compared_on_the_last_shared_session(self):
        frame, index = sector_rows()
        metrics = sector_metrics(frame, benchmark_frame(index[:-1]))
        self.assertAlmostEqual(metrics["market_excess_pct"], 1.0, places=4)
        self.assertEqual(metrics["data_date"], index[-2].date().isoformat())
        self.assertEqual(metrics["benchmark_lag_sessions"], 1)

    def test_matching_sessions_keep_the_latest_sector_session(self):
        frame, index = sector_rows()
        metrics = sector_metrics(frame, benchmark_frame(index))
        self.assertAlmostEqual(metrics["market_excess_pct"], 102 / 101 * 100 - 100, places=4)
        self.assertEqual(metrics["data_date"], index[-1].date().isoformat())
        self.assertEqual(metrics["benchmark_lag_sessions"], 0)
        self.assertAlmostEqual(metrics["turnover_ratio"], 102 * 300 / 10000, places=2)

    def test_benchmark_without_a_usable_close_is_an_error_not_a_null_score(self):
        frame, index = sector_rows()
        benchmark = benchmark_frame(index)
        benchmark["Close"] = np.nan
        with self.assertRaises(ValueError):
            sector_metrics(frame, benchmark)


def market_frame(last_sessions, sessions=25):
    frames = {}
    for ticker, last in last_sessions.items():
        index = pd.bdate_range(end=last, periods=sessions)
        frames[ticker] = pd.DataFrame({"Close": np.linspace(100, 110, sessions)}, index=index)
    return pd.concat(frames, axis=1)


class LocalIndexFallback(unittest.TestCase):
    def test_one_session_lag_still_produces_a_market_record(self):
        previous = date(2026, 11, 19)
        data = market_frame({"^KS11": date(2026, 11, 18), "^KQ11": previous})
        with patch("kis_intraday.yf.download", return_value=data):
            records = market_evidence("KRX", previous, OPENS)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "market")
        self.assertIn(records[0]["assessment"], ("supportive", "neutral"))
        self.assertEqual(records[0]["snapshot"]["^KS11"]["date"], "2026-11-18")

    def test_bar_older_than_the_lag_limit_is_not_market_context(self):
        previous = date(2026, 11, 19)
        stale = previous - timedelta(days=MARKET_INDEX_MAX_LAG_DAYS + 1)
        data = market_frame({"^KS11": stale, "^KQ11": stale})
        with patch("kis_intraday.yf.download", return_value=data):
            self.assertEqual(market_evidence("KRX", previous, OPENS), [])

    def test_one_usable_and_one_missing_local_index_stays_unavailable(self):
        previous = date(2026, 11, 19)
        data = market_frame({"^KS11": previous})
        with patch("kis_intraday.yf.download", return_value=data):
            self.assertEqual(market_evidence("KRX", previous, OPENS), [])


OPENING_BARS = {0: dict(high=100.0, low=99.0, close=99.9, volume=1000),
                1: dict(high=100.0, low=99.5, close=100.0, volume=400),
                2: dict(high=100.3, low=99.9, close=100.2, volume=300)}


class OpeningVolumeReference(unittest.TestCase):
    def test_fallback_reference_is_the_measured_opening_share(self):
        reference, basis = volume_reference(OPENING_BARS)
        self.assertEqual(basis, "opening_share_fallback")
        self.assertAlmostEqual(reference, 350.0)

    def test_same_session_average_no_longer_judges_the_third_bar(self):
        # 300 against the first two bars' 700 average failed the old 1.5x test.
        status, metrics = evaluate_breakout(OPENING_BARS, QUOTE, OPENS + timedelta(minutes=15))
        self.assertEqual(status, "low_volume")
        self.assertIsNone(metrics)

    def test_third_bar_against_its_own_slot_history_confirms_the_breakout(self):
        status, metrics = evaluate_breakout(OPENING_BARS, QUOTE, OPENS + timedelta(minutes=15),
                                            volume_baseline=190.0)
        self.assertEqual(status, "base_breakout")
        self.assertAlmostEqual(metrics[0], 300 / 190.0)
        self.assertEqual(volume_reference(OPENING_BARS, 190.0)[1], "same_slot_prior_sessions")

    def test_a_quiet_third_bar_still_fails_against_its_history(self):
        status, _ = evaluate_breakout(OPENING_BARS, QUOTE, OPENS + timedelta(minutes=15),
                                      volume_baseline=250.0)
        self.assertEqual(status, "low_volume")
        self.assertLess(300 / 250.0, MIN_VOLUME_RATIO)

    def test_candidate_verdict_uses_the_supplied_slot_baseline(self):
        closes = OPENS + timedelta(minutes=15)
        without, _ = evaluate_candidate(OPENING_BARS, QUOTE, closes, "KRX", "005930",
                                        EVIDENCE, False, company={})
        with_baseline, _ = evaluate_candidate(OPENING_BARS, QUOTE, closes, "KRX", "005930",
                                              EVIDENCE, False, company={}, volume_baseline=140.0)
        self.assertEqual(without, "low_volume")
        self.assertEqual(with_baseline, "composite_assessment_incomplete")


def volume_history(days, values):
    ticker = "005930" + (".KQ" if "005930.KQ" in KR_SYMBOL_NAMES else ".KS")
    index = pd.DatetimeIndex([datetime.combine(day, time(9, 10), tzinfo=KST) for day in days])
    return pd.concat({ticker: pd.DataFrame({"Volume": values}, index=index)}, axis=1)


class SameSlotBaseline(unittest.TestCase):
    def days(self, count):
        return [date(2026, 11, 20) - timedelta(days=offset) for offset in range(count, 0, -1)]

    def test_average_of_prior_sessions_in_the_same_clock_slot(self):
        prices = volume_history(self.days(5), [100.0, 300.0, 200.0, 200.0, 200.0])
        baselines = volume_baselines(prices, ["005930"], False, OPENS)
        self.assertAlmostEqual(baselines["005930"], 200.0)

    def test_thin_history_leaves_the_symbol_to_the_fallback(self):
        prices = volume_history(self.days(2), [200.0, 200.0])
        self.assertEqual(volume_baselines(prices, ["005930"], False, OPENS), {})

    def test_missing_history_frame_is_not_an_error(self):
        self.assertEqual(volume_baselines(None, ["005930"], False, OPENS), {})


if __name__ == "__main__":
    unittest.main()
