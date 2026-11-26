import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import scanner
from candidate_selection import quality_watch_reason, RESEARCH_MIN_AVERAGE_TURNOVER
from company_forecast import QUALITY_FUNDAMENTAL_SCORE, build_company_forecast, quality_grade
from issuer_consensus import consensus_for

AT = datetime(2026, 11, 23, 9, 5, tzinfo=timezone.utc)


def kr_row(turnover=18.4e8, price=30050.0, **overrides):
    """롯데정밀화학 as the screen sees it: 18.4억 traded, so the momentum path drops it."""
    values = dict(price=price, volume=turnover / price, volume_ratio=.9,
                  average_turnover_20d=turnover, ma20=price * 1.02, ma60=price * 1.05,
                  return_5d=-.02, return_20d=-.06, relative_strength=-.03,
                  obv_rising=False, breakout=False, breakdown=False, rebound=False,
                  overheated=False, market_above_ma20=False, atr14_pct=3.0,
                  volatility_20d=.028, data_date='2026-11-21')
    values.update(overrides.pop('indicators', {}))
    row = dict(market='KR', symbol='004000.KS', name='롯데정밀화학', asset_type='stock_or_etf',
               signal=dict(score=4, stage='장전 관찰 후보'), indicators=values,
               selection_filter='insufficient_liquidity')
    row.update(overrides)
    return row


def lotte_company(**overrides):
    """The issuer as the statement and consensus reads see it (2026-11-23 fill)."""
    company = dict(quarter='2026-09', checked_at=AT.isoformat(), operating_margin_pct=4.24,
                   revenue_yoy_pct=4.9, operating_margin_yoy_pp=1.2, trailing_pe=4.01,
                   forward_pe=4.03, trailing_pe_source='Naver Finance',
                   forward_pe_source='Naver Finance', price_to_book=.30,
                   filing_status='checked', news_status='no_recent_articles',
                   event_analysis=dict(generated_at=AT.isoformat(),
                                       status='no_material_events_identified',
                                       feed_coverage=[dict(status='checked')], unread_events=[]))
    company.update(overrides)
    return company


def indicators(price=30050.0):
    return dict(price=price, ma20=price * 1.02, return_5d=-.02, return_20d=-.06,
                relative_strength=-.03, volume_ratio=.9, volatility_20d=.028,
                atr14_pct=3.0, data_date='2026-11-21')


def consensus(target, price=30050.0):
    return {"status": "measured", "market": "KR", "source": "Naver Finance broker consensus",
            "as_of": "2026-11-23", "target_price": target, "price": price,
            "upside_pct": 100 * (target / price - 1), "recommendation_mean": 4.0,
            "analyst_count": 4}


def flow(direction, score):
    return {"status": "measured", "market": "KR", "symbol": "004000.KS",
            "direction": direction, "score": score, "source": "Naver Finance deal trend",
            "as_of": "2026-11-21"}


class QualityTierTests(unittest.TestCase):
    def test_a_below_floor_turnover_name_is_read_on_the_quality_tier(self):
        self.assertIsNone(quality_watch_reason(kr_row(), 'KR'))
        self.assertLess(18.4e8, 4e9)
        self.assertGreater(18.4e8, RESEARCH_MIN_AVERAGE_TURNOVER['KR'])

    def test_the_tier_never_takes_a_name_the_momentum_path_already_has(self):
        self.assertEqual(quality_watch_reason(kr_row(turnover=60e8), 'KR'), 'above_momentum_turnover')

    def test_illiquid_cheap_and_special_instruments_are_refused(self):
        self.assertEqual(quality_watch_reason(kr_row(turnover=5e8), 'KR'), 'below_research_turnover')
        self.assertEqual(quality_watch_reason(kr_row(price=500.0), 'KR'), 'low_price')
        self.assertEqual(quality_watch_reason(kr_row(indicators={'breakdown': True}), 'KR'), 'breakdown')
        self.assertEqual(quality_watch_reason(kr_row(indicators={'ma20': 0.0}), 'KR'),
                         'invalid_average_price')
        self.assertEqual(quality_watch_reason(kr_row(name='롯데정밀화학우'), 'KR'), 'special_security')
        missing = kr_row()
        missing['indicators'].pop('ma20')
        self.assertEqual(quality_watch_reason(missing, 'KR'), 'missing_indicators')

    def test_a_falling_or_extended_price_is_not_a_value_entry(self):
        self.assertEqual(quality_watch_reason(kr_row(indicators={'ma20': 30050 / .80}), 'KR'),
                         'price_off_average')
        self.assertEqual(quality_watch_reason(kr_row(indicators={'ma20': 30050 / 1.30}), 'KR'),
                         'price_off_average')


class AxisTests(unittest.TestCase):
    def test_fundamental_and_demand_axes_are_reported_apart_from_the_composite(self):
        row = kr_row()
        forecast = build_company_forecast(lotte_company(), indicators(), 30050.0, AT,
                                          supply=flow('outflow', -.4),
                                          consensus=consensus(57333))
        self.assertIsNotNone(forecast['composite_score'])
        # the price and demand dimension is not part of the fundamental axis
        self.assertNotIn('price_and_demand', forecast['axes']['fundamental']['components'])
        self.assertIn('price_and_demand', forecast['axes']['demand']['components'])
        self.assertIn('institutional_demand', forecast['axes']['demand']['components'])
        self.assertGreaterEqual(forecast['fundamental_score'], QUALITY_FUNDAMENTAL_SCORE)
        self.assertAlmostEqual(forecast['target_upside_pct'], 100 * (57333 / 30050 - 1), places=1)
        self.assertLess(forecast['fundamental_coverage'], 1.0)  # no balance sheet observed
        self.assertGreaterEqual(forecast['fundamental_coverage'], .5)
        self.assertEqual(row['selection_filter'], 'insufficient_liquidity')

    def test_a_broker_target_raises_the_fundamental_reading(self):
        low = build_company_forecast(lotte_company(), indicators(), 30050.0, AT,
                                     supply=flow('outflow', -.4), consensus=consensus(32000))
        high = build_company_forecast(lotte_company(), indicators(), 30050.0, AT,
                                      supply=flow('outflow', -.4), consensus=consensus(57333))
        self.assertGreater(high['fundamental_score'], low['fundamental_score'])

    def test_measured_inflow_is_what_promotes_a_quality_name(self):
        waiting = build_company_forecast(lotte_company(), indicators(), 30050.0, AT,
                                         supply=flow('outflow', -.4), consensus=consensus(57333))
        inflow = build_company_forecast(lotte_company(), indicators(), 30050.0, AT,
                                        supply=flow('inflow', .35), consensus=consensus(57333))
        self.assertEqual(waiting['quality_grade'], 'supply_waiting_quality')
        self.assertEqual(inflow['quality_grade'], 'supply_inflow_quality')
        self.assertGreater(inflow['demand_score'], waiting['demand_score'])

    def test_a_thin_or_weak_fundamental_reading_is_not_graded(self):
        thin = build_company_forecast(dict(quarter='2026-09', operating_margin_pct=6.0),
                                      indicators(), 30050.0, AT,
                                      supply=flow('inflow', .4),
                                      consensus={"status": "unavailable"})
        self.assertLess(thin['fundamental_coverage'], .5)
        self.assertIsNone(thin['quality_grade'])
        weak = lotte_company(operating_margin_pct=-8.0, revenue_yoy_pct=-25.0,
                             operating_margin_yoy_pp=-6.0, trailing_pe=40.0, forward_pe=60.0)
        self.assertIsNone(quality_grade(
            build_company_forecast(weak, indicators(), 30050.0, AT,
                                   supply=flow('inflow', .4),
                                   consensus=consensus(31000))['axes']['fundamental'],
            flow('inflow', .4)))

    def test_an_unmeasured_flow_leaves_the_name_waiting(self):
        forecast = build_company_forecast(lotte_company(), indicators(), 30050.0, AT,
                                          supply={"status": "unavailable", "reason": "no_source"},
                                          consensus=consensus(57333))
        self.assertEqual(forecast['quality_grade'], 'supply_waiting_quality')
        self.assertNotIn('institutional_demand', forecast['axes']['demand']['components'])


class ConsensusTests(unittest.TestCase):
    def test_a_domestic_broker_target_becomes_a_gap_to_the_price(self):
        with patch("issuer_consensus.naver_consensus",
                   return_value={"target_price": 57333.0, "recommendation_mean": 4.0,
                                 "compiled_at": "2026-11-23"}):
            reading = consensus_for(kr_row(), {}, at=AT, price=30050.0)
        self.assertEqual(reading['status'], 'measured')
        self.assertAlmostEqual(reading['upside_pct'], 90.8, places=1)
        self.assertEqual(reading['as_of'], '2026-11-23')

    def test_no_target_is_reported_rather_than_assumed(self):
        with patch("issuer_consensus.naver_consensus",
                   return_value={"target_price": None, "recommendation_mean": None}):
            reading = consensus_for(kr_row(), {}, at=AT, price=30050.0)
        self.assertEqual(reading['status'], 'unavailable')
        self.assertEqual(reading['reason'], 'no_analyst_target_observed')


class QualityWatchResearchTests(unittest.TestCase):
    def report(self, rows):
        return dict(results=rows, sectors=[], preopen_session='2026-11-24')

    def test_a_read_name_is_graded_and_never_becomes_an_entry(self):
        row = kr_row()
        with patch("kis_intraday.company_context",
                   return_value=({'004000': lotte_company()}, [])), \
             patch("supply_flow.evidence_for", return_value=flow('outflow', -.4)), \
             patch("issuer_consensus.consensus_for", return_value=consensus(57333)):
            entries = scanner.quality_watch_research(self.report([row]), AT)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry['grade'], 'supply_waiting_quality')
        self.assertFalse(entry['entry_signal'])
        self.assertAlmostEqual(entry['average_turnover'], 18.4e8)
        self.assertEqual(row['selection_filter'], 'insufficient_liquidity')
        self.assertNotIn('preopen_signal', row)
        self.assertNotIn('company_analysis', row)

    def test_measured_inflow_promotes_the_same_name_to_the_candidate_label(self):
        row = kr_row()
        with patch("kis_intraday.company_context",
                   return_value=({'004000': lotte_company()}, [])), \
             patch("supply_flow.evidence_for", return_value=flow('inflow', .35)), \
             patch("issuer_consensus.consensus_for", return_value=consensus(57333)):
            entries = scanner.quality_watch_research(self.report([row]), AT)
        self.assertEqual(entries[0]['grade'], 'supply_inflow_quality')

    def test_a_liquid_or_weak_name_is_not_read_on_this_tier(self):
        liquid = kr_row(turnover=60e8, symbol='005930.KS', name='삼성전자')
        weak = kr_row(symbol='000000.KS', name='적자기업')
        with patch("kis_intraday.company_context",
                   return_value=({'004000': lotte_company(), '000000': lotte_company(
                       operating_margin_pct=-12.0, revenue_yoy_pct=-30.0,
                       operating_margin_yoy_pp=-9.0, trailing_pe=50.0, forward_pe=90.0)}, [])), \
             patch("supply_flow.evidence_for", return_value=flow('inflow', .35)), \
             patch("issuer_consensus.consensus_for", return_value=consensus(31000)):
            entries = scanner.quality_watch_research(self.report([liquid, weak]), AT)
        self.assertEqual([entry['symbol'] for entry in entries], [])

    def test_report_line_says_the_inflow_grade_and_that_it_is_not_a_buy(self):
        entry = dict(name='롯데정밀화학', symbol='004000.KS', grade='supply_inflow_quality',
                     fundamental_score=65.7, target_upside_pct=90.8,
                     analyst_target={'target_price': 57333.0},
                     supply_line='외인·기관 +1.2억원 · 거래대금 대비 +1.42% · 5일 중 4일 순매수',
                     average_turnover=18.4e8)
        text = scanner.quality_watch_line(entry)
        # The block heading already says '수급 유입 우량주', so the name line does not
        # repeat it (2026-11-25 short form); a waiting row keeps its own label.
        self.assertIn('롯데정밀화학 (004000)', text)
        self.assertIn('펀더멘탈 66', text)
        self.assertIn('외인·기관 +1.2억', text)
        self.assertIn('57,333원', text)
        self.assertNotIn('수급 대기 우량주', text)
        # The block's meaning is said once in its heading; the 12:34 alert repeated
        # '수급 유입 · 매수 후보 검토 · 매수 신호 아님' under every name.
        self.assertNotIn('매수 후보 검토', text)
        self.assertNotIn('매수 신호 아님', text)

    def test_a_waiting_record_keeps_its_own_basis_label(self):
        text = scanner.quality_watch_line(dict(name='관찰종목', symbol='000000.KS',
                                               grade='supply_waiting_quality',
                                               fundamental_score=64.0))
        # The label says which grade it is; the block heading (not every line) says the
        # whole list is a review and not a buy signal.
        self.assertIn('수급 대기', text)
        self.assertNotIn('수급 유입', text)


class ReuseSameSessionQualityWatchTests(unittest.TestCase):
    """A same-session scan already read the 10억~40억 tier; a later report reuses it."""

    def test_a_same_session_record_is_not_read_again(self):
        report = dict(results=[], sectors=[], industry_snapshot=dict(rows=[]),
                      quality_watch=[dict(market='KR', symbol='004000.KS',
                                          name='롯데정밀화학', grade='supply_inflow_quality')],
                      quality_watch_at=datetime.now(timezone.utc).isoformat())
        with patch('kis_intraday.company_context') as context, \
             patch('event_impact_analysis.world_events', return_value=([], [])), \
             patch('economic_graph.load_profiles', return_value={}):
            scanner.prepare_selected_analysis(report)
        context.assert_not_called()
        self.assertEqual(report['quality_watch'][0]['symbol'], '004000.KS')

    def test_a_previous_session_record_is_read_again(self):
        report = dict(results=[], sectors=[], industry_snapshot=dict(rows=[]),
                      quality_watch=[dict(market='KR', symbol='004000.KS')],
                      quality_watch_at='2026-11-20T10:00:00+09:00')
        with patch('kis_intraday.company_context', return_value=({}, [])), \
             patch('event_impact_analysis.world_events', return_value=([], [])), \
             patch('economic_graph.load_profiles', return_value={}):
            scanner.prepare_selected_analysis(report)
        self.assertEqual(report['quality_watch'], [])


class DailyReportWiringTests(unittest.TestCase):
    """The quality tier is read inside the daily preparation and reaches the message."""

    def prepare(self, report, supply=None):
        with patch("kis_intraday.company_context",
                   return_value=({'004000': lotte_company()}, [])), \
             patch("supply_flow.evidence_for",
                   return_value=supply or flow('outflow', -.4)), \
             patch("supply_flow.share_line",
                   return_value="외인·기관 -1.2억원 · 거래대금 대비 -0.42%"), \
             patch("issuer_consensus.consensus_for", return_value=consensus(57333)), \
             patch("event_impact_analysis.world_events", return_value=([], [])), \
             patch("economic_graph.load_profiles", return_value={}):
            scanner.prepare_selected_analysis(report)
        return report

    def daily_report(self, supply=None):
        return self.prepare(dict(results=[kr_row()], sectors=[],
                                 industry_snapshot=dict(rows=[]), preopen_session='2026-11-24',
                                 market_regime={'KR': {'above_ma20': False}}), supply)

    def sent_text(self, report):
        sent = {}

        class Reply:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True}

        def post(url, json=None, **kwargs):
            sent['text'] = json['text']
            return Reply()

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}), \
             patch("scanner.delivery_status", return_value=(True, "ok")), \
             patch("scanner.requests.post", side_effect=post):
            result = scanner.send_telegram_summary(report)
        self.assertTrue(result['sent'])
        return sent['text']

    def test_the_daily_preparation_carries_a_quality_watch_entry(self):
        row = kr_row()
        report = self.prepare(dict(results=[row], sectors=[], industry_snapshot=dict(rows=[]),
                                   preopen_session='2026-11-24',
                                   market_regime={'KR': {'above_ma20': False}}))
        self.assertEqual(len(report['quality_watch']), 1)
        self.assertEqual(report['quality_watch'][0]['grade'], 'supply_waiting_quality')
        self.assertEqual(report['quality_watch_scope']['candidates'], 1)
        self.assertNotIn('preopen_signal', row)

    def test_a_waiting_name_stays_in_the_report_but_out_of_the_message(self):
        report = self.daily_report()  # outflow: fundamentally fine, not yet bought
        self.assertEqual(report['quality_watch'][0]['grade'], 'supply_waiting_quality')
        text = self.sent_text(report)
        self.assertNotIn('[수급 대기 우량주]', text)
        self.assertNotIn('[수급 유입 우량주]', text)
        self.assertNotIn('롯데정밀화학', text)

    def test_the_message_carries_the_inflow_block_and_not_a_buy(self):
        text = self.sent_text(self.daily_report(flow('inflow', .35)))
        self.assertIn("[수급 유입 우량주]", text)
        self.assertIn("롯데정밀화학 (004000)", text)
        # The heading says it once instead of every entry repeating it.
        self.assertEqual(text.count("매수 신호 아님"), 1)
        self.assertNotIn("[수급 대기 우량주]", text)
        # the live-confirmation label, not the "순매수 확인" of the block subtitle
        self.assertNotIn("· 매수 확인", text)

    def test_a_mixed_quality_watch_sends_only_the_inflow_names(self):
        report = dict(results=[], sectors=[], quality_watch=[
            dict(name='롯데정밀화학', symbol='004000.KS', grade='supply_inflow_quality',
                 fundamental_score=66.0),
            dict(name='관찰종목', symbol='000000.KS', grade='supply_waiting_quality',
                 fundamental_score=64.0)])
        text = self.sent_text(report)
        self.assertIn("[수급 유입 우량주]", text)
        self.assertIn("롯데정밀화학 (004000)", text)
        self.assertNotIn("관찰종목", text)
        self.assertNotIn("수급 대기 우량주", text)


if __name__ == '__main__':
    unittest.main()
