"""보유·매도 감시: 계획이 나간 뒤 다음 세션들의 +6% · 트레일링 · 손절 · 5거래일 만기.

2026-11-26 전수점검 6항 — 장중 신호 뒤 30분만 감시하고 다음 날 이후의 매도 기준을 보는
코드가 없었다. 체결 확인이 없으므로 계산은 '계획 지정가에 샀다면'이고, 그 가정을 메시지가
밝힌다.
"""
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import exit_watch

NOW = datetime(2026, 11, 27, 9, 5, tzinfo=exit_watch.KST)


def frame(closes, volumes=None, end='2026-11-26', highs=None):
    index = pd.bdate_range(end=end, periods=len(closes))
    values = closes
    return pd.DataFrame({
        'Open': values,
        'High': highs or [value * 1.004 for value in values],
        'Low': [value * 0.996 for value in values],
        'Close': values,
        'Volume': volumes or [1000.0] * len(values),
    }, index=index)


def report(rows=None, session='2026-11-25'):
    return dict(preopen_session=session, results=rows or [])


def plan_row(symbol='009150.KS', name='삼성전기', market='KR', entry=778000,
             status='conditional_buy', strategy='earnings_catalyst', **extra):
    row = dict(market=market, symbol=symbol, name=name,
               indicators=dict(price=entry, data_date='2026-11-24'),
               preopen_signal=dict(status=status, entry_limit=entry,
                                   expires_session='2026-11-26',
                                   reference_date='2026-11-24',
                                   strategy=strategy, take_profit_pct=6, stop_pct=3))
    row.update(extra)
    return row


def position(**extra):
    base = dict(symbol='009150.KS', name='삼성전기', market='KR', entry=10000.0,
                entry_session='2026-11-26', asset_class='stock',
                entry_assumption='plan_limit_not_confirmed_fill')
    base.update(extra)
    return base


class PositionTests(unittest.TestCase):
    def test_only_conditional_plans_in_the_message_market_become_positions(self):
        rows = [plan_row(),
                plan_row(symbol='036570.KS', name='NC', status='extended_price'),
                plan_row(symbol='AAPL', market='US', name='애플', entry=250.0)]
        positions = exit_watch.positions_from_report(report(rows), 'KR')
        self.assertEqual([item['symbol'] for item in positions], ['009150.KS'])
        self.assertEqual(positions[0]['entry_session'], '2026-11-26')
        self.assertEqual(positions[0]['entry'], 778000)
        self.assertEqual(positions[0]['stop_pct'], 0.03)
        self.assertEqual(positions[0]['entry_assumption'], 'plan_limit_not_confirmed_fill')

    def test_a_fund_carries_the_two_percent_stop(self):
        rows = [plan_row(symbol='069500.KS', name='KODEX 200', entry=40000, asset_type='etf')]
        positions = exit_watch.positions_from_report(report(rows), 'KR')
        self.assertEqual(positions[0]['stop_pct'], 0.02)
        self.assertEqual(positions[0]['asset_class'], 'fund')

    def test_a_plan_without_a_price_band_is_not_a_position(self):
        row = plan_row()
        row['preopen_signal']['entry_limit'] = None
        row['preopen_signal']['flat_open_entry_limit'] = None
        self.assertEqual(exit_watch.positions_from_report(report([row]), 'KR'), [])


class EvaluationTests(unittest.TestCase):
    def test_plus_six_percent_on_ordinary_volume_sells_at_the_target(self):
        closes = [10000.0] * 20 + [10650.0]
        result = exit_watch.evaluate_position(position(), frame(closes), NOW)
        self.assertEqual(result['status'], 'exited')
        self.assertEqual(result['exit_reason'], 'take_profit')
        self.assertEqual(result['exit_price'], 10600.0)
        self.assertAlmostEqual(result['exit_return_pct'], 6.0)

    def test_the_stop_is_read_from_the_same_instrument_split(self):
        result = exit_watch.evaluate_position(position(), frame([10000.0] * 20 + [9600.0]), NOW)
        self.assertEqual(result['status'], 'exited')
        self.assertEqual(result['exit_reason'], 'stop_loss')
        self.assertEqual(result['exit_price'], 9600.0)
        self.assertEqual(result['stop_price'], 9700.0)

    def test_five_sessions_without_target_or_stop_close_at_that_close(self):
        # 진입 세션(11/26)부터 다섯 세션: 고가도 목표 아래로 둔다.
        closes = [10000.0] * 20 + [10100.0, 10200.0, 10300.0, 10400.0, 10500.0]
        highs = [value * 1.001 for value in closes]
        result = exit_watch.evaluate_position(
            position(), frame(closes, highs=highs, end='2026-12-02'), NOW)
        self.assertEqual(result['status'], 'exited')
        self.assertEqual(result['exit_reason'], 'hold_period_end')
        self.assertEqual(result['sessions_held'], 5)
        self.assertEqual(result['exit_price'], 10500.0)

    def test_a_surge_on_expanded_volume_holds_instead_of_selling_at_six(self):
        closes = [10000.0] * 20 + [10700.0]
        volumes = [1000.0] * 20 + [2000.0]
        result = exit_watch.evaluate_position(position(), frame(closes, volumes), NOW)
        self.assertEqual(result['status'], 'open')
        self.assertEqual(result['mode'], 'trailing')
        self.assertEqual(result['sessions_held'], 1)
        self.assertAlmostEqual(result['current_return_pct'], 7.0)

    def test_a_trailing_hold_closes_when_the_floor_is_breached(self):
        closes = [10000.0] * 20 + [10700.0, 10300.0]
        volumes = [1000.0] * 20 + [2000.0, 1500.0]
        result = exit_watch.evaluate_position(
            position(), frame(closes, volumes, end='2026-11-27'), NOW)
        self.assertEqual(result['status'], 'exited')
        self.assertEqual(result['exit_reason'], 'trailing_floor_breached')

    def test_no_session_after_the_plan_is_not_a_loss(self):
        result = exit_watch.evaluate_position(position(), frame([10000.0] * 20, end='2026-11-25'), NOW)
        self.assertEqual(result['status'], 'awaiting_session')
        self.assertEqual(result['sessions_held'], 0)

    def test_missing_prices_leave_the_position_unread_not_closed(self):
        result = exit_watch.evaluate_position(position(), None, NOW)
        self.assertEqual(result['status'], 'awaiting_session')


class BlockTests(unittest.TestCase):
    def test_the_block_never_presents_an_assumed_entry_as_a_fill(self):
        lines = exit_watch.format_block([exit_watch.evaluate_position(
            position(), frame([10000.0] * 20 + [10300.0]), NOW)])
        text = '\n'.join(lines)
        self.assertIn('체결 미확인', lines[0])
        self.assertIn('계획 진입가 가정', lines[0])
        self.assertIn('009150', text)
        self.assertIn('목표 10,600원', text)
        self.assertIn('손절 9,700원', text)
        self.assertIn('계획 지정가에 샀다고 가정', text)

    def test_an_exit_names_the_rule_the_date_and_the_price(self):
        row = exit_watch.evaluate_position(position(), frame([10000.0] * 20 + [10650.0]), NOW)
        text = '\n'.join(exit_watch.format_block([row]))
        self.assertIn('목표 +6% 도달', text)
        self.assertIn('청산', text)
        self.assertIn('+6.0%', text)

    def test_a_cut_list_says_how_many_are_not_shown(self):
        rows = [exit_watch.evaluate_position(position(symbol=f'00000{index}.KS', name=f'종목{index}'),
                                             frame([10000.0] * 20 + [10300.0]), NOW)
                for index in range(7)]
        text = '\n'.join(exit_watch.format_block(rows, limit=5))
        self.assertIn('외 2건', text)

    def test_no_positions_is_no_block(self):
        self.assertEqual(exit_watch.format_block([]), [])


class ReportIntegrationTests(unittest.TestCase):
    def test_watch_reads_the_previous_report_and_drops_stale_plans(self):
        rows = [plan_row(), plan_row(symbol='036570.KS', name='NC', entry=180000)]
        prices = {'009150.KS': frame([10000.0] * 20 + [10300.0]),
                  '036570.KS': frame([180000.0] * 20 + [180000.0 * 1.07])}
        lines, evaluated = exit_watch.watch(
            report(rows), 'KR', now=NOW,
            downloader=lambda symbols, market: {symbol: prices[symbol] for symbol in symbols})
        self.assertEqual(len(evaluated), 2)
        text = '\n'.join(lines)
        self.assertIn('삼성전기', text)
        self.assertIn('NC', text)

        old = dict(rows[0])
        old['preopen_signal'] = dict(old['preopen_signal'], expires_session='2026-10-30')
        lines, evaluated = exit_watch.watch(report([old]), 'KR', now=NOW, downloader=lambda *_: prices)
        self.assertEqual((lines, evaluated), ([], []))

    def test_the_preopen_message_carries_the_sell_watch_block(self):
        import scanner
        from unittest.mock import Mock, patch
        lines = [exit_watch.format_block([exit_watch.evaluate_position(
            position(), frame([10000.0] * 20 + [10300.0]), NOW)])]
        report_obj = dict(results=[], sectors=[], market_regime={}, theme_sectors=[],
                          preopen_session='2026-11-26', exit_watch_lines=lines[-1],
                          buy_evaluation=dict(completed=True, decision_completed=0))
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake',
                                       'DELIVERY_MARKET': 'KRX'}), \
                patch('scanner.delivery_status', return_value=(True, 'test')), \
                patch('scanner.requests.post') as post:
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            scanner.send_telegram_summary(report_obj)
        text = post.call_args.kwargs['json']['text']
        self.assertIn('보유·매도 감시', text)
        self.assertIn('체결 미확인', text)


class ArtifactTests(unittest.TestCase):
    def test_the_delivery_attaches_the_previous_sessions_watch(self):
        from unittest.mock import patch
        import preopen_delivery
        prior = report([plan_row()])
        lines = ['[보유·매도 감시] · 체결 미확인 · 계획 진입가 가정 · 11/26 09:05 KST']
        holder = {}
        with patch('exit_watch.previous_analysis', return_value=prior) as finder, \
                patch('exit_watch.watch', return_value=(lines, [dict(symbol='009150.KS')])) as watch:
            result = preopen_delivery.attach_exit_watch(holder, 'KR', 'KRX')
        self.assertEqual(result, lines)
        finder.assert_called_once_with('KRX', limit=6)
        self.assertEqual(holder['exit_watch_lines'], lines)
        self.assertEqual(watch.call_args.args[1], 'KR')

    def test_a_missing_previous_artifact_leaves_the_alert_alone(self):
        from unittest.mock import patch
        import preopen_delivery
        holder = {}
        with patch('exit_watch.previous_analysis', return_value=None):
            self.assertEqual(preopen_delivery.attach_exit_watch(holder, 'KR', 'KRX'), [])
        self.assertNotIn('exit_watch_lines', holder)

    def test_the_previous_artifact_is_read_from_the_latest_successful_run(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(list(args))
            if args[:3] == ['gh', 'run', 'list']:
                return SimpleNamespace(returncode=0, stdout=json.dumps([
                    dict(databaseId=9, createdAt='2026-11-25T20:40:00Z')]), stderr='')
            if args[:3] == ['gh', 'run', 'download']:
                folder = Path(args[args.index('-D') + 1])
                (folder / 'preopen-analysis.json').write_text(
                    json.dumps(dict(preopen_session='2026-11-25', results=[])), encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            return SimpleNamespace(returncode=1, stdout='', stderr='')

        analysis = exit_watch.previous_analysis('KRX', runner=runner)
        self.assertEqual(analysis['preopen_session'], '2026-11-25')
        self.assertIn('-n', calls[1])
        self.assertEqual(calls[1][calls[1].index('-n') + 1], 'preopen-delivery-KRX')

    def test_a_missing_artifact_is_skipped_not_fatal(self):
        def runner(args, **kwargs):
            if args[:3] == ['gh', 'run', 'list']:
                return SimpleNamespace(returncode=0, stdout=json.dumps([
                    dict(databaseId=9, createdAt='2026-11-25T20:40:00Z')]), stderr='')
            return SimpleNamespace(returncode=1, stdout='', stderr='no artifact')

        self.assertIsNone(exit_watch.previous_analysis('KRX', runner=runner))

    def test_no_gh_output_is_no_analysis(self):
        self.assertIsNone(exit_watch.previous_analysis(
            'KRX', runner=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout='', stderr='')))


if __name__ == '__main__':
    unittest.main()
