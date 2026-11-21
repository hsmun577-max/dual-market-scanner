"""The market-regime, demand-expansion and valuation-source rules, pinned with numbers.

Backtest behind these thresholds: evidence/dual-market-scanner/정확도-개선-검증.md
"""
from datetime import datetime
import unittest

import pandas as pd

from candidate_selection import (MIN_DEMAND_VOLUME_RATIO, MIN_EXPANSION_VOLUME_RATIO,
                                 candidate_rejection_reason)
from company_forecast import build_company_forecast
from kis_intraday import KST
from scanner import calculate_indicators, market_regime_note, telegram_candidate_line

AT = datetime(2026, 11, 18, 9, 15, tzinfo=KST)


def stock(**overrides):
    indicators = dict(price=10000., volume=2_000_000., volume_ratio=2.0, ma20=9500., ma60=9000.,
                      return_5d=.05, return_20d=.10, relative_strength=.05, obv_rising=True,
                      breakout=True, breakdown=False, rebound=False, overheated=False)
    for key in list(overrides):
        if key in ('market', 'symbol', 'name', 'asset_type', 'signal'):
            continue
        indicators[key] = overrides.pop(key)
    row = dict(market="KR", symbol="005930.KS", name="일반기업", asset_type="stock_or_etf",
               signal=dict(score=6, stage="장중 조건부 진입 후보"), indicators=indicators)
    row.update(overrides)
    return row


def company(**overrides):
    values = dict(quarter='2026-09', checked_at='2026-11-18T08:00:00+09:00',
                  operating_margin_pct=12., revenue_yoy_pct=20., operating_margin_yoy_pp=3.,
                  free_cash_flow_margin_pct=8., operating_cash_conversion=1.2,
                  net_debt_to_equity=.3, trailing_pe=25., forward_pe=20., sector='Technology',
                  filing_status='checked', news_status='ok',
                  event_analysis=dict(generated_at='2026-11-18T08:00:00+09:00',
                                      status='no_material_events_identified',
                                      feed_coverage=[dict(status='checked')], unread_events=[]))
    values.update(overrides)
    return values


INDICATORS = dict(price=100., ma20=98., return_5d=.03, return_20d=.08, relative_strength=.06,
                  volume_ratio=2., volatility_20d=.02, market_return_20d=.02)
SECTORS = [dict(name='반도체', data_date='2026-11-17', market_excess_pct=.5, turnover_ratio=1.8)]


def prices(days=80, last=100.0, slope=0.0):
    index = pd.date_range('2026-07-01', periods=days, freq='B')
    closes = [last - slope * (days - 1 - position) for position in range(days)]
    return pd.DataFrame({'Close': closes, 'High': [value * 1.01 for value in closes],
                         'Low': [value * .99 for value in closes],
                         'Volume': [1_000_000] * days}, index=index)


class MarketRegimeTests(unittest.TestCase):
    def test_a_candidate_that_passes_every_own_test_is_held_back_by_its_market(self):
        item = stock(market_above_ma20=False)
        self.assertEqual(candidate_rejection_reason(item, "KR"), "market_headwind")

    def test_a_market_above_its_average_does_not_reject(self):
        self.assertIsNone(candidate_rejection_reason(stock(market_above_ma20=True), "KR"))

    def test_an_unobserved_market_is_not_read_as_a_headwind(self):
        # Archived rows and a failed benchmark download carry no value; neither may
        # silently remove every candidate.
        self.assertIsNone(candidate_rejection_reason(stock(), "KR"))
        self.assertIsNone(candidate_rejection_reason(stock(market_above_ma20=None), "KR"))

    def test_indicators_expose_the_index_trend(self):
        rising = calculate_indicators(prices(slope=.5), prices(last=200.0, slope=.5))
        self.assertTrue(rising["market_above_ma20"])
        falling = calculate_indicators(prices(slope=.5), prices(last=200.0, slope=-.5))
        self.assertFalse(falling["market_above_ma20"])
        self.assertIsNone(calculate_indicators(prices(), None)["market_above_ma20"])

    def test_report_line_shows_the_regime_and_what_it_held_back(self):
        lines = market_regime_note({"market_regime": {
            "KR": {"above_ma20": False, "observed": True, "headwind_skipped": 7},
            "US": {"above_ma20": True, "observed": True, "headwind_skipped": 0}}})
        self.assertIn("KOSPI", lines[0])
        self.assertIn("하락 국면", lines[0])
        self.assertIn("7종목", lines[0])
        self.assertIn("상승 국면", lines[1])
        self.assertIn("확인 불가", market_regime_note(
            {"market_regime": {"KR": {"above_ma20": None, "observed": False}}})[0])


class DemandExpansionTests(unittest.TestCase):
    def test_a_breakout_on_thin_volume_no_longer_qualifies(self):
        self.assertGreater(MIN_EXPANSION_VOLUME_RATIO, 1.5)
        item = stock(volume_ratio=1.5, breakout=True)
        self.assertEqual(candidate_rejection_reason(item, "KR"), "insufficient_demand_expansion")
        item = stock(volume_ratio=1.5, breakout=False)
        self.assertEqual(candidate_rejection_reason(item, "KR"), "insufficient_demand_expansion")

    def test_expansion_at_the_policy_ratio_qualifies(self):
        self.assertIsNone(candidate_rejection_reason(stock(volume_ratio=1.8), "KR"))

    def test_no_demand_at_all_is_still_reported_separately(self):
        self.assertLess(MIN_DEMAND_VOLUME_RATIO, MIN_EXPANSION_VOLUME_RATIO)
        item = stock(volume_ratio=1.1)
        self.assertEqual(candidate_rejection_reason(item, "KR"), "unconfirmed_demand")


class ValuationSourceTests(unittest.TestCase):
    def test_kis_trailing_with_yahoo_forward_is_not_divided(self):
        # The live defect: Samsung Electronics 27.56 (KIS) against a Yahoo forward of 2.35
        # scored a constant +4.9 points on every Korean large cap.
        mixed = build_company_forecast(
            company(trailing_pe=27.56, forward_pe=2.35,
                    trailing_pe_source='KIS inquire-price',
                    forward_pe_source='Yahoo Finance observed multiples'),
            INDICATORS, 100., AT, sectors=SECTORS)
        self.assertNotIn('valuation_outlook', mixed['components'])
        self.assertEqual(mixed['evidence']['valuation_outlook_excluded']['reason'], 'mixed_sources')
        self.assertIn('valuation_outlook', mixed['caveats'])
        matched = build_company_forecast(
            company(trailing_pe=27.56, forward_pe=2.35,
                    trailing_pe_source='Yahoo Finance observed multiples',
                    forward_pe_source='Yahoo Finance observed multiples'),
            INDICATORS, 100., AT, sectors=SECTORS)
        self.assertNotIn('valuation_outlook', matched['components'])
        self.assertEqual(matched['evidence']['valuation_outlook_excluded']['reason'],
                         'implausible_forward_multiple')
        self.assertLess(mixed['composite_score'], 70)

    def test_same_source_and_plausible_multiples_still_score(self):
        result = build_company_forecast(
            company(trailing_pe=25., forward_pe=20.,
                    trailing_pe_source='Yahoo Finance observed multiples',
                    forward_pe_source='Yahoo Finance observed multiples'),
            INDICATORS, 100., AT, sectors=SECTORS)
        self.assertIn('valuation_outlook', result['components'])
        self.assertNotIn('valuation_outlook_excluded', result['evidence'])
        self.assertEqual(result['evidence']['valuation_outlook']['trailing_pe_source'],
                         'Yahoo Finance observed multiples')

    def test_peer_multiple_fills_the_axis_when_the_pair_is_unusable(self):
        peer = dict(metric='trailing_pe', own_multiple=9., peer_median=12., peer_count=4,
                    peer_symbols=['000001.KS'] * 4, industry='반도체',
                    observed_at='2026-11-18T08:30:00+09:00', score=.3)
        result = build_company_forecast(
            company(trailing_pe=9., forward_pe=.3, peer_valuation=peer,
                    trailing_pe_source='KIS inquire-price',
                    forward_pe_source='Yahoo Finance observed multiples'),
            INDICATORS, 100., AT, sectors=SECTORS)
        self.assertIn('valuation_outlook', result['components'])
        self.assertAlmostEqual(result['components']['valuation_outlook'], .3)
        self.assertEqual(result['evidence']['valuation_outlook']['metric'], 'trailing_pe')


class InstrumentLabelTests(unittest.TestCase):
    def test_a_fund_candidate_says_so_before_its_entry_plan(self):
        item = stock(name="KODEX 200")
        item['signal'] = dict(confirmed=True, entry_limit=10000., score=6)
        item['company_forecast'] = dict(composite_score=61.5, base_price=10500., bull_price=11000.)
        self.assertIn("· ETF", telegram_candidate_line(item))
        plain = stock(name="삼성전자")
        plain['signal'] = dict(confirmed=True, entry_limit=60000., score=6)
        plain['company_forecast'] = dict(composite_score=61.5, base_price=63000., bull_price=66000.)
        self.assertNotIn("ETF", telegram_candidate_line(plain))


if __name__ == "__main__":
    unittest.main()
