import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import scanner
from research_asset import FUND_CANDIDATE_LIMIT, promoted_fund_symbols

AT = datetime.now(timezone.utc)


def fund_row(market, symbol, name, volume, asset_type='etf', **overrides):
    """A fund that passes every trend, demand and liquidity gate of the first screen."""
    price = 10000.0 if market == 'KR' else 100.0
    values = dict(price=price, volume=volume, volume_ratio=3.0,
                  average_turnover_20d=price * volume * 1.5,
                  ma20=price * .95, ma60=price * .90, return_5d=.06, return_20d=.10,
                  relative_strength=.05, obv_rising=True, breakout=True, breakdown=False,
                  rebound=False, overheated=False, market_above_ma20=True,
                  atr14_pct=4.0, volatility_20d=.04, data_date='2026-11-20')
    values.update(overrides.pop('indicators', {}))
    return dict(market=market, symbol=symbol, name=name, asset_type=asset_type,
                signal=dict(score=6, stage='장중 조건부 진입 후보', reasons=['거래량 증가']),
                fundamentals='미검증', news_and_filings='미검증', **overrides,
                indicators=values)


def fund_snapshot(market):
    """What company_context returns for a listed fund: a profile, never an income statement."""
    return dict(quote_type='ETF', market=market, checked_at=AT.isoformat(),
                available_at=AT.isoformat(), filing_status='checked',
                news_status='no_recent_articles',
                event_analysis=dict(generated_at=AT.isoformat(),
                                    status='no_material_events_identified',
                                    feed_coverage=[dict(status='checked')], unread_events=[]))


def analyse(report):
    """Run the daily preparation with the provider calls replaced by fund profiles."""
    def context(label, keys, checked):
        market = 'US' if label.startswith('US') else 'KR'
        return {key: fund_snapshot(market) for key in keys}, []
    with patch('kis_intraday.company_context', side_effect=context), \
         patch('event_impact_analysis.world_events', return_value=([], [])), \
         patch('economic_graph.load_profiles', return_value={}):
        scanner.prepare_selected_analysis(report)
    return report


class FundPromotionTests(unittest.TestCase):
    def korean_etfs(self, count=10):
        return [fund_row('KR', f'{100000 + index}.KS', f'KODEX 테스트{index}',
                         volume=1_000_000 - index * 1000) for index in range(count)]

    def test_the_eight_most_traded_funds_of_a_market_reach_the_candidate_path(self):
        rows = self.korean_etfs(10)
        report = analyse(dict(results=rows, sectors=[]))
        promoted = [row for row in rows if row.get('analysis_kind') == 'fund_candidate']
        self.assertEqual(len(promoted), FUND_CANDIDATE_LIMIT)
        # Ranked by volume: the first eight in volume order, none of the last two.
        self.assertEqual([row['symbol'] for row in promoted], [row['symbol'] for row in rows[:8]])
        self.assertEqual([row['fund_rank'] for row in promoted], list(range(1, 9)))
        for row in promoted:
            self.assertIn('company_analysis', row)
            self.assertIsNotNone(row['company_forecast']['composite_score'])
            self.assertEqual(row['company_forecast']['instrument'], 'listed_fund')
        for row in rows[8:]:
            self.assertEqual(row['analysis_kind'], 'fund_screen_only')
            self.assertNotIn('company_analysis', row)
            self.assertNotIn('company_forecast', row)
            self.assertNotIn('preopen_signal', row)
        self.assertEqual(report['research_scope']['funds_promoted'], FUND_CANDIDATE_LIMIT)
        self.assertEqual(report['research_scope']['funds_screened'], 2)
        self.assertEqual(report['research_scope']['fund_candidate_limit'], FUND_CANDIDATE_LIMIT)
        self.assertFalse(report['research_scope']['funds_graded_as_operating_companies'])

    def test_the_ninth_fund_keeps_the_fund_screen_and_is_never_evaluated(self):
        rows = self.korean_etfs(9)
        report = analyse(dict(results=rows, sectors=[]))
        ninth = rows[-1]
        self.assertEqual(ninth['analysis_kind'], 'fund_screen_only')
        self.assertNotIn('preopen_decision', ninth)
        decisions = report['buy_evaluation']['recommendations']
        self.assertNotIn(ninth['symbol'], decisions)

    def test_a_fund_refused_as_a_product_does_not_take_one_of_the_eight_slots(self):
        # A cash/bond ETF and a covered-call or weekly-pay structure are refused on the
        # name by the first screen, so promoting them could not produce a candidate. They
        # are not ranked, and the eighth equity fund keeps its place.
        rows = [fund_row('KR', '069500.KS', 'KODEX CD1년금리플러스액티브(합성)', volume=9_000_000),
                fund_row('US', 'COSW', 'ROUNDHILL COST WEEKLYPAY', volume=8_000_000)]
        equity = self.korean_etfs(9)
        analyse(dict(results=rows + equity, sectors=[]))
        self.assertEqual(rows[0]['analysis_kind'], 'fund_screen_only')
        self.assertEqual(rows[1]['analysis_kind'], 'fund_screen_only')
        self.assertEqual(sum(1 for row in equity[:8] if row.get('analysis_kind') == 'fund_candidate'), 8)
        self.assertEqual(equity[8]['analysis_kind'], 'fund_screen_only')

    def test_a_fund_identified_only_by_its_quote_type_is_ranked_with_the_market(self):
        # English-named US listings carry no Korean brand prefix, so they are only known
        # as funds once the profile arrives. The top-eight is settled then: the ninth is
        # demoted after its profile, and the report says so.
        rows = [fund_row('US', f'ETF{index}', f'ISHARES TEST {index}', volume=5_000_000 - index * 1000,
                         asset_type='stock_or_etf') for index in range(9)]
        report = analyse(dict(results=rows, sectors=[]))
        promoted = [row['symbol'] for row in rows if row.get('analysis_kind') == 'fund_candidate']
        self.assertEqual(promoted, [row['symbol'] for row in rows[:8]])
        ninth = rows[8]
        self.assertEqual(ninth['analysis_kind'], 'fund_screen_only')
        self.assertTrue(ninth.get('fund_demoted_after_profile'))
        self.assertNotIn('company_analysis', ninth)
        self.assertEqual(report['research_scope']['funds_demoted_after_profile'], 1)


class FundCandidateGateTests(unittest.TestCase):
    def market(self, sqqq_volume=60_000_000, **values):
        """Eight traded US funds around SQQQ: seven above it and one far below."""
        above = [fund_row('US', f'ETF{index}', f'ISHARES TEST {index}', volume=70_000_000 + index * 1000,
                          asset_type='leveraged_etf') for index in range(7)]
        sqqq = fund_row('US', 'SQQQ', '프로셰어즈 나스닥100 인버스 3배 ETF', volume=sqqq_volume,
                        asset_type='inverse_etf', indicators=values)
        tail = fund_row('US', 'ETFTAIL', 'ISHARES TAIL', volume=1_000, asset_type='etf')
        return above, sqqq, tail

    def test_sqqq_in_eighth_place_is_planned_when_the_ordinary_gates_pass(self):
        above, sqqq, tail = self.market()
        report = analyse(dict(results=[*above, sqqq, tail], sectors=[]))
        self.assertEqual(sqqq['analysis_kind'], 'fund_candidate')
        self.assertEqual(sqqq['fund_rank'], FUND_CANDIDATE_LIMIT)
        self.assertIn('company_analysis', sqqq)
        # The plan itself is the evidence that the fund reached the candidate path and
        # passed its own gates: nothing else puts a symbol in the recommendations.
        self.assertIn('SQQQ', report['buy_evaluation']['recommendations'])
        self.assertEqual(sqqq['preopen_signal']['status'], 'conditional_buy')
        self.assertIn('ETF', scanner.telegram_candidate_line(sqqq))
        self.assertEqual(tail['analysis_kind'], 'fund_screen_only')
        self.assertNotIn('preopen_signal', tail)

    def test_sqqq_in_ninth_place_stays_on_the_fund_screen(self):
        above, sqqq, tail = self.market(sqqq_volume=1_000)
        report = analyse(dict(results=[*above, sqqq, tail], sectors=[]))
        self.assertEqual(sqqq['analysis_kind'], 'fund_screen_only')
        self.assertNotIn('company_analysis', sqqq)
        self.assertNotIn('preopen_signal', sqqq)
        self.assertNotIn('SQQQ', report['buy_evaluation']['recommendations'])

    def test_sqqq_inside_the_eight_is_still_held_by_the_market_gate(self):
        above, sqqq, tail = self.market(market_above_ma20=False)
        analyse(dict(results=[*above, sqqq, tail], sectors=[]))
        self.assertEqual(sqqq['analysis_kind'], 'fund_candidate')
        self.assertEqual(sqqq['preopen_decision'], 'market_headwind')
        self.assertNotIn('preopen_signal', sqqq)

    def test_a_promoted_fund_keeps_the_breakdown_and_extension_gates(self):
        above, broken, tail = self.market(breakdown=True)
        analyse(dict(results=[*above, broken, tail], sectors=[]))
        self.assertEqual(broken['preopen_decision'], 'breakdown')
        self.assertNotIn('preopen_signal', broken)
        above, extended, tail = self.market(return_5d=.20)
        analyse(dict(results=[*above, extended, tail], sectors=[]))
        self.assertEqual(extended['preopen_decision'], 'extended_price')
        self.assertNotIn('preopen_signal', extended)

    def test_a_fund_score_does_not_read_missing_issuer_statements_as_evidence(self):
        above, sqqq, tail = self.market()
        analyse(dict(results=[*above, sqqq, tail], sectors=[]))
        forecast = sqqq['company_forecast']
        self.assertEqual(forecast['instrument'], 'listed_fund')
        self.assertEqual(forecast['assessment_basis'], 'fund_price_demand_sector_events')
        self.assertIsNotNone(forecast['composite_score'])
        for gap in ('financial_statements', 'current_financial_period', 'cash_flow_quality',
                    'balance_sheet', 'earnings_quality'):
            self.assertNotIn(gap, forecast['missing_dimensions'])
            self.assertNotIn(gap, forecast['caveats'])
        self.assertEqual(sqqq['fundamentals'], 'ETF 상장상품 · 발행사 재무제표 없음')


class FundProfileWithoutQuoteTypeTests(unittest.TestCase):
    """A cached profile may predate the provider's quote_type; the scan still knows."""

    def test_a_korean_fund_profile_without_quote_type_is_scored_on_the_fund_model(self):
        rows = [fund_row('KR', '069500.KS', 'KODEX 200', volume=3_000_000)]
        snapshot = fund_snapshot('KR')
        snapshot.pop('quote_type')  # restored profile cache built before it was read
        with patch('kis_intraday.company_context',
                   side_effect=lambda label, keys, checked: (
                       {key: dict(snapshot) for key in keys}, [])), \
             patch('event_impact_analysis.world_events', return_value=([], [])), \
             patch('economic_graph.load_profiles', return_value={}):
            scanner.prepare_selected_analysis(dict(results=rows, sectors=[]))
        row = rows[0]
        forecast = row['company_forecast']
        self.assertEqual(row['company_analysis']['instrument_class'], 'listed_fund')
        self.assertEqual(forecast['instrument'], 'listed_fund')
        self.assertIsNotNone(forecast['composite_score'])
        self.assertNotIn('financial_statements', forecast['score_withheld_reasons'])
        self.assertNotIn('current_financial_period', forecast['score_withheld_reasons'])

    def test_a_fund_below_the_price_floor_does_not_take_a_candidate_slot(self):
        # KODEX 200선물인버스2X was the most traded Korean fund on 2026-11-24 at 140원.
        # The entry screen refuses it as low_price before any profile is read, so its slot
        # belongs to the next fund that could actually be planned.
        cheap = fund_row('KR', '252670.KS', 'KODEX 200선물인버스2X', volume=9_000_000,
                         indicators=dict(price=140.0, average_turnover_20d=1.2e12))
        others = [fund_row('KR', f'{100000 + index}.KS', f'KODEX 테스트{index}',
                           volume=1_000_000 - index * 1000) for index in range(9)]
        report = analyse(dict(results=[cheap, *others], sectors=[]))
        self.assertEqual(cheap['analysis_kind'], 'fund_screen_only')
        self.assertNotIn('company_analysis', cheap)
        self.assertNotIn('fund_rank', cheap)
        promoted = [row['symbol'] for row in others if row.get('analysis_kind') == 'fund_candidate']
        self.assertEqual(promoted, [row['symbol'] for row in others[:FUND_CANDIDATE_LIMIT]])
        self.assertEqual(report['research_scope']['funds_promoted'], FUND_CANDIDATE_LIMIT)


class FundRankingTests(unittest.TestCase):
    def test_ranking_is_per_market_and_by_volume(self):
        rows = [fund_row('KR', f'{index}.KS', f'KODEX {index}', volume=volume)
                for index, volume in enumerate([100, 900, 500])]
        rows += [fund_row('US', f'ETF{index}', f'ISHARES {index}', volume=volume)
                 for index, volume in enumerate([20, 90])]
        promoted = promoted_fund_symbols(rows)
        self.assertEqual(promoted['KR'], ['1.KS', '2.KS', '0.KS'])
        self.assertEqual(promoted['US'], ['ETF1', 'ETF0'])

class IntradayWatchListTests(unittest.TestCase):
    def test_the_intraday_watch_list_skips_a_fund_screened_at_fund_level(self):
        # The Korean intraday run picks its watch list from the saved daily scan. A fund
        # the scan left on the fund screen must not come back through that door, so only
        # ``fund_candidate`` funds ride along with the stocks.
        import json
        import os
        import tempfile
        from datetime import date
        from pathlib import Path

        import kis_intraday

        promoted = fund_row('KR', '069500.KS', 'KODEX 200', volume=3_000_000)
        promoted['analysis_kind'] = 'fund_candidate'
        demoted = fund_row('KR', '233740.KS', 'KODEX 코스닥150레버리지', volume=2_000_000)
        demoted['analysis_kind'] = 'fund_screen_only'
        stock = fund_row('KR', '005930.KS', '삼성전자', volume=1_000_000, asset_type='stock_or_etf')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'opening-scan'
            target.mkdir()
            (target / 'latest.json').write_text(
                json.dumps(dict(results=[promoted, demoted, stock])), encoding='utf-8')
            here = os.getcwd()
            os.chdir(folder)
            try:
                picked = kis_intraday.daily_candidates(date(2026, 11, 20))
            finally:
                os.chdir(here)
        self.assertIn('069500', picked)
        self.assertIn('005930', picked)
        self.assertNotIn('233740', picked)


if __name__ == '__main__':
    unittest.main()
