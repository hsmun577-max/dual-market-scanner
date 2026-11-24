"""자산군 상승 흐름 헤더·시장 분위기 계약.

사용자 지시(2026-11-24): "상승흐름에 들어간 투자처 분석해서 흐름이 바뀌면 알려줘",
"항상 상위에 상승흐름인자산 제목으로 적어줘", "추세만보지말고 시장분위기분석해서 적어".
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import asset_regime
import kis_intraday
import scanner
import tradable_signal

NOW = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)


def dates(count, end=date(2026, 11, 21)):
    return [(end - timedelta(days=count - 1 - index)).isoformat() for index in range(count)]


def rising(start=100.0, count=80, step=0.004):
    return [round(start * (1 + step) ** index, 4) for index in range(count)]


def falling(start=100.0, count=80, step=0.004):
    return [round(start * (1 - step) ** index, 4) for index in range(count)]


def series(closes, end=date(2026, 11, 21)):
    return dict(closes=list(closes), dates=dates(len(closes), end))


def rising_series(**overrides):
    return dict(closes=rising(count=80), dates=dates(80), **overrides)


def sideway_then_rally():
    """오래 내려온 뒤 최근에 20일선을 넘어선 모양: 20일선 위, 그러나 60일선 아래(역배열)."""
    values = [round(200 * (1 - 0.0065) ** index, 4) for index in range(70)]
    last = values[-1]
    values.extend(round(last * (1 + 0.02) ** index, 4) for index in range(1, 11))
    return values


def uptrend_symbols():
    return {'^KS11', '^KQ11', 'SPY', 'QQQ', 'DX-Y.NYB', 'KRW=X', 'GLD', 'SLV',
            'CPER', 'DBA', 'VNQ', 'BTC-USD'}


def market_series(vix=None, ten_year=None, hyg=None, ief=None, falling_symbols=(),
                  mixed=()):
    """자산군·심리 지표를 한 번에 만든다. `falling_symbols`만 하락 추세, `mixed`는 반등 모양."""
    data = {}
    symbols = [symbol for spec in asset_regime.GROUPS for symbol, _label in spec['instruments']]
    symbols.extend(asset_regime.SENTIMENT_SYMBOLS)
    for symbol in dict.fromkeys(symbols):
        if symbol in falling_symbols:
            data[symbol] = series(falling())
        elif symbol in mixed:
            data[symbol] = series(sideway_then_rally())
        else:
            data[symbol] = rising_series()
    data['^VIX'] = series(vix if vix is not None else [28.0] * 79 + [25.6])
    data['^TNX'] = series(ten_year if ten_year is not None else [4.90] * 79 + [4.92])
    if hyg is not None:
        data['HYG'] = series(hyg)
    if ief is not None:
        data['IEF'] = series(ief)
    return data


class TrendTests(unittest.TestCase):
    def test_aligned_average_and_price_above_it_is_an_uptrend(self):
        self.assertEqual(asset_regime.state_of(110, 105, 100), 'uptrend')

    def test_price_above_the_fast_average_in_an_inverted_stack_is_a_rebound(self):
        self.assertEqual(asset_regime.state_of(110, 100, 105), 'rebound')

    def test_price_below_the_fast_average_is_a_downtrend(self):
        self.assertEqual(asset_regime.state_of(95, 100, 105), 'downtrend')

    def test_short_history_is_not_judged(self):
        record = asset_regime.trend(rising(count=59))
        self.assertEqual(record['state'], 'unavailable')
        self.assertEqual(record['reason'], 'insufficient_history')

    def test_trend_carries_the_twenty_day_return(self):
        record = asset_regime.trend(rising(count=80))
        self.assertEqual(record['state'], 'uptrend')
        self.assertGreater(record['return_20d_pct'], 0)
        self.assertEqual(record['previous_state'], 'uptrend')

    def test_a_rebound_shape_is_read_as_rebound(self):
        record = asset_regime.trend(sideway_then_rally())
        self.assertEqual(record['state'], 'rebound')


class GroupTests(unittest.TestCase):
    def test_two_of_three_members_is_a_group_uptrend(self):
        self.assertEqual(asset_regime.group_state(['uptrend', 'uptrend', 'downtrend']), 'uptrend')

    def test_one_of_three_members_is_not_a_group_uptrend(self):
        self.assertNotEqual(asset_regime.group_state(['uptrend', 'downtrend', 'downtrend']), 'uptrend')

    def test_two_member_group_needs_both(self):
        self.assertNotEqual(asset_regime.group_state(['uptrend', 'downtrend']), 'uptrend')
        self.assertEqual(asset_regime.group_state(['uptrend', 'uptrend']), 'uptrend')

    def test_missing_members_do_not_count_against_the_group(self):
        self.assertEqual(asset_regime.group_state(['uptrend', 'unavailable']), 'uptrend')

    def test_no_observation_is_unavailable(self):
        self.assertEqual(asset_regime.group_state([None, 'unavailable']), 'unavailable')


class BlockTests(unittest.TestCase):
    def build(self, previous=None, context=None, data=None):
        return asset_regime.assess(now=NOW, previous=previous, context=context,
                                   downloader=lambda symbols: market_series() if data is None else data)

    def test_flow_line_names_the_groups_in_uptrend_by_strength(self):
        block = self.build(data=market_series(falling_symbols={'TLT', 'IEF', 'USO', 'CPER', 'DBA'}))
        line = block['header'][0]
        self.assertTrue(line.startswith('📈 지금 상승 흐름: '))
        self.assertIn('국내주식(', line)
        self.assertIn('금·은(', line)
        self.assertNotIn('미국채', line)
        self.assertNotIn('원자재', line)

    def test_flow_line_percentage_is_the_twenty_day_return(self):
        data = market_series()
        data['SPY'] = series(rising(count=80, step=0.01))
        data['QQQ'] = series(rising(count=80, step=0.01))
        block = self.build(data=data)
        us = next(row for row in block['groups'] if row['label'] == '미국주식')
        self.assertAlmostEqual(us['return_20d_pct'], (1.01 ** 20 - 1) * 100, delta=0.2)

    def test_every_group_down_says_all_adjusting_with_the_rebound_note(self):
        symbols = [symbol for spec in asset_regime.GROUPS for symbol, _label in spec['instruments']]
        block = self.build(data=market_series(falling_symbols=set(symbols) - {'GLD'}, mixed={'GLD'}))
        line = block['header'][0]
        self.assertTrue(line.startswith('📉 전 자산 조정 구간 (현금·단기채 유리'))
        self.assertIn('단기 반등: 금', line)

    def test_all_adjusting_without_rebound_has_no_note(self):
        symbols = [symbol for spec in asset_regime.GROUPS for symbol, _label in spec['instruments']]
        block = self.build(data=market_series(falling_symbols=set(symbols)))
        self.assertEqual(block['header'][0], '📉 전 자산 조정 구간 (현금·단기채 유리)')

    def test_missing_prices_say_unavailable_instead_of_inventing_flows(self):
        block = self.build(data={})
        self.assertTrue(block['header'][0].startswith('❔ 지금 상승 흐름: 확인 불가'))
        self.assertTrue(block['errors'])

    def test_one_failed_symbol_does_not_break_the_block(self):
        data = market_series()
        del data['BTC-USD']
        block = self.build(data=data)
        self.assertIn('series_missing:BTC-USD', block['errors'])
        self.assertEqual(next(row for row in block['groups'] if row['label'] == '비트코인')['state'],
                         'unavailable')

    def test_json_carries_the_group_detail_and_window(self):
        block = self.build()
        self.assertEqual(block['window'], dict(ma_fast=20, ma_slow=60, return_days=20))
        self.assertEqual(block['reference_date'], '2026-11-21')
        bitcoin = next(row for row in block['groups'] if row['label'] == '비트코인')
        self.assertEqual(bitcoin['instruments'][0]['symbol'], 'BTC-USD')
        self.assertIn('ma20', bitcoin['instruments'][0])


class SentimentTests(unittest.TestCase):
    def build(self, **kwargs):
        return asset_regime.assess(now=NOW, downloader=lambda symbols: market_series(**kwargs))

    def test_fear_falling_into_caution_reads_as_short_rebound_attempt(self):
        block = self.build()
        mood = block['sentiment']
        self.assertEqual(mood['vix_state'], '경계')
        self.assertEqual(mood['vix_phrase'], '공포 진정')
        self.assertEqual(mood['regime_label'], '경계 속 단기 반등 시도')
        self.assertTrue(block['header'][1].startswith('🌡️ 시장 분위기: 경계 속 단기 반등 시도 ('))

    def test_sentiment_line_carries_vix_rate_and_dollar_evidence(self):
        line = self.build()['header'][1]
        self.assertIn('VIX 25.6', line)
        self.assertIn('미10년물 4.92%', line)
        self.assertIn('고금리 부담', line)
        self.assertIn('달러 ', line)
        self.assertIn('강세', line)

    def test_yields_over_five_percent_are_a_rate_alert(self):
        block = self.build(ten_year=[4.8] * 79 + [5.06])
        self.assertIn('금리 경계', block['header'][1])
        self.assertAlmostEqual(block['sentiment']['us10y'], 5.06, places=2)

    def test_ten_year_feed_quoted_in_tenths_is_normalised(self):
        block = self.build(ten_year=[49.0] * 79 + [49.2])
        self.assertAlmostEqual(block['sentiment']['us10y'], 4.92, places=2)

    def test_risk_appetite_uses_hyg_over_ief(self):
        block = self.build(hyg=rising(count=80, step=0.006), ief=rising(count=80, step=0.0005))
        self.assertEqual(block['sentiment']['hyg_ief_trend'], 'risk_on')

    def test_safe_haven_preference_wins_when_high_yield_lags(self):
        block = self.build(hyg=falling(count=80, step=0.003), ief=rising(count=80, step=0.002),
                           vix=[16.0] * 79 + [15.8])
        self.assertEqual(block['sentiment']['hyg_ief_trend'], 'risk_off')
        self.assertIn('위험회피', block['sentiment']['regime_label'])

    def test_fear_spike_reads_as_widening_fear(self):
        block = self.build(vix=[24.0] * 79 + [33.0])
        self.assertEqual(block['sentiment']['vix_state'], '공포')
        self.assertEqual(block['sentiment']['vix_phrase'], '공포 확대')
        self.assertIn('공포', block['sentiment']['regime_label'])

    def test_calm_market_with_flows_up_reads_as_risk_on_expansion(self):
        block = self.build(hyg=rising(count=80, step=0.006), ief=rising(count=80, step=0.0005),
                           vix=[13.0] * 79 + [12.9])
        self.assertEqual(block['sentiment']['regime_label'], '위험선호 확산')

    def test_no_indicators_says_unavailable(self):
        block = asset_regime.assess(now=NOW, downloader=lambda symbols: {})
        self.assertEqual(block['sentiment']['regime_label'], '확인 불가')
        self.assertIn('확인 불가', block['header'][1])

    def test_headline_context_is_kept_short_and_appended(self):
        block = asset_regime.assess(
            now=NOW, context='이슈: 반도체 수출 규제 완화 기대에 필라델피아 반도체 지수 급등',
            downloader=lambda symbols: market_series())
        self.assertIn('이슈: ', block['header'][1])
        self.assertLessEqual(len(block['sentiment']['context']), asset_regime.MAX_HEADLINE)

    def test_failed_download_never_raises(self):
        def explode(symbols):
            raise RuntimeError('provider down')
        block = asset_regime.assess(now=NOW, downloader=explode)
        self.assertEqual(len(block['header']), 2)
        self.assertIn('series_unavailable', ' '.join(block['errors']))


class ChangeTests(unittest.TestCase):
    def block(self, data=None, previous=None):
        return asset_regime.assess(now=NOW, previous=previous,
                                   downloader=lambda symbols: data or market_series())

    def test_a_group_that_just_entered_the_uptrend_is_reported(self):
        previous = dict(observed_at='2026-11-21T22:00:00+00:00', groups=[
            dict(label='금·은', state='downtrend'), dict(label='국내주식', state='uptrend')])
        block = self.block(previous=previous)
        change = block['changes']
        self.assertEqual(change['basis'], 'previous_scan')
        self.assertIn('금·은', change['newly_entered'])
        self.assertNotIn('국내주식', change['newly_entered'])
        self.assertTrue(block['header'][2].startswith('⚡ 흐름 변화: '))

    def test_a_group_that_left_the_uptrend_is_reported(self):
        previous = dict(observed_at='2026-11-21T22:00:00+00:00', groups=[
            dict(label='원자재', state='uptrend'), dict(label='국내주식', state='uptrend')])
        data = market_series()
        for symbol in ('^KS11', '^KQ11'):
            data[symbol] = series(falling())
        block = self.block(data=data, previous=previous)
        self.assertIn('국내주식', block['changes']['exited'])
        self.assertIn('국내주식 상승 이탈', block['header'][2])

    def test_no_change_has_no_third_line(self):
        previous = dict(observed_at='2026-11-21T22:00:00+00:00',
                        groups=[dict(label=row['label'], state=row['state'])
                                for row in self.block()['groups']])
        block = self.block(previous=previous)
        self.assertEqual(len(block['header']), 2)

    def test_without_a_previous_scan_it_compares_with_the_previous_close(self):
        data = market_series()
        # 20일선을 오늘 처음 넘어선 지표: 어제까지는 아래, 오늘은 위.
        crosses = [100.0] * 79
        crosses[-1] = 130.0
        data['^KS11'] = series(crosses)
        data['^KQ11'] = series(crosses)
        block = self.block(data=data)
        self.assertEqual(block['changes']['basis'], 'previous_bar')
        self.assertIn('국내주식', block['changes']['newly_entered'])
        self.assertIn('직전 거래일', block['header'][2])

    def test_stale_previous_block_without_groups_is_ignored(self):
        block = self.block(previous={'observed_at': '2026-11-21T22:00:00+00:00'})
        self.assertEqual(block['changes']['basis'], 'previous_bar')


class HeaderWiringTests(unittest.TestCase):
    def block(self):
        return asset_regime.assess(now=NOW, downloader=lambda symbols: market_series())

    def test_intraday_alert_starts_with_the_regime_block(self):
        text = kis_intraday.format_notification(
            'KRX', [], datetime(2026, 11, 24, 9, 15, tzinfo=kis_intraday.KST),
            None, [dict(market='KR', rank=1, name='반도체', return_pct=1.2)], None,
            regime=self.block())
        lines = text.split('\n')
        self.assertTrue(lines[0].startswith('📈 지금 상승 흐름: '))
        self.assertTrue(lines[1].startswith('🌡️ 시장 분위기: '))
        self.assertIn('오늘의 주요 섹터', text)

    def test_intraday_alert_without_a_regime_block_is_unchanged(self):
        text = kis_intraday.format_notification(
            'KRX', [], datetime(2026, 11, 24, 9, 15, tzinfo=kis_intraday.KST),
            None, [dict(market='KR', rank=1, name='반도체', return_pct=1.2)], None)
        self.assertTrue(text.startswith('오늘의 주요 섹터'))

    def test_exit_watch_starts_with_the_regime_block(self):
        text = kis_intraday.format_exit_watch(
            'KRX', [], datetime(2026, 11, 24, 9, 30, tzinfo=kis_intraday.KST),
            regime=self.block())
        self.assertTrue(text.startswith('📈 지금 상승 흐름: '))
        self.assertIn('매도 규칙 점검', text)

    def test_daily_summary_starts_with_the_regime_block(self):
        report = dict(sector_header_override=['오늘의 주요 섹터', '1위 반도체'],
                      results=[], buy_evaluation=dict(completed=True),
                      asset_regime=self.block())
        sent = {}

        class Reply:
            status_code = 200

            @staticmethod
            def json():
                return {'ok': True}

        def post(url, json=None, **kwargs):
            sent['text'] = json['text']
            return Reply()

        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 't', 'TELEGRAM_CHAT_ID': 'c'}), \
                patch('scanner.delivery_status', return_value=(True, 'ok')), \
                patch('scanner.requests.post', side_effect=post):
            result = scanner.send_telegram_summary(report)
        self.assertTrue(result['sent'])
        self.assertTrue(sent['text'].startswith('📈 지금 상승 흐름: '))
        self.assertIn('오늘의 주요 섹터', sent['text'])

    def test_daily_summary_without_a_block_keeps_the_sector_header_first(self):
        report = dict(sector_header_override=['오늘의 주요 섹터', '1위 반도체'],
                      results=[], buy_evaluation=dict(completed=True))
        sent = {}

        class Reply:
            status_code = 200

            @staticmethod
            def json():
                return {'ok': True}

        def post(url, json=None, **kwargs):
            sent['text'] = json['text']
            return Reply()

        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 't', 'TELEGRAM_CHAT_ID': 'c'}), \
                patch('scanner.delivery_status', return_value=(True, 'ok')), \
                patch('scanner.requests.post', side_effect=post):
            scanner.send_telegram_summary(report)
        self.assertTrue(sent['text'].startswith('오늘의 주요 섹터'))

    def test_requested_report_starts_with_the_regime_block(self):
        text = tradable_signal.format_requested_report(
            ['오늘의 주요 섹터'], [], dict(completed=True), NOW, regime=self.block())
        self.assertTrue(text.startswith('📈 지금 상승 흐름: '))
        self.assertIn('오늘의 주요 섹터', text)

    def test_attach_stores_the_block_on_the_report(self):
        report = dict(theme_sectors=[])
        block = asset_regime.attach(report, now=NOW, save=False,
                                    downloader=lambda symbols: market_series())
        self.assertIs(report['asset_regime'], block)
        self.assertEqual(len(block['header']), 2)

    def test_headline_from_the_theme_block_is_used_as_context(self):
        report = dict(theme_sectors=[dict(market='KR', rows=[
            dict(name='반도체', issue='이슈: 수출 규제 완화 기대')])])
        self.assertEqual(asset_regime.headlines_from_report(report), '이슈: 수출 규제 완화 기대')

    def test_report_without_theme_news_has_no_context(self):
        report = dict(theme_sectors=[dict(market='KR', rows=[
            dict(name='반도체', issue='이슈: 뉴스 미확인')])])
        self.assertIsNone(asset_regime.headlines_from_report(report))

    def test_previous_state_is_read_from_a_saved_report(self):
        import json
        import tempfile
        from pathlib import Path
        block = self.block()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'latest.json'
            path.write_text(json.dumps({'asset_regime': block}), encoding='utf-8')
            loaded = asset_regime.load_previous([path, Path(folder) / 'missing.json'])
        self.assertEqual(loaded['observed_at'], block['observed_at'])

    def test_saved_state_is_written_and_read_back(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'asset-regime-state.json'
            self.assertIsNotNone(asset_regime.save_state(self.block(), path=path))
            self.assertTrue(path.is_file())


if __name__ == '__main__':
    unittest.main()
