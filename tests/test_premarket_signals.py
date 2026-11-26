"""미국 장전 사전 시그널: 게이트와 무관한 감시 풀, 프리마켓 변동·촉매 뉴스, 메시지 블록."""
import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import premarket_signals as signals
from premarket_signals import HEADING, WATCH_SYMBOLS, format_block, premarket_move, select_signals

NY = ZoneInfo('America/New_York')
NOW = datetime(2026, 11, 23, 8, 40, tzinfo=NY)


def quote(change, session='premarket', as_of='2026-11-23T08:35:00-05:00'):
    return dict(change_pct=change, session=session, as_of=as_of, price=100.0, previous_close=95.0)


def article(headline, at='2026-11-23T12:00:00+00:00'):
    return dict(headline=headline, available_at=at, source='Yahoo Finance')


def frame(rows):
    index = pd.DatetimeIndex([datetime.fromisoformat(stamp).replace(tzinfo=NY) for stamp, _ in rows])
    return pd.DataFrame({'Close': [close for _, close in rows], 'Volume': [100.0] * len(rows)},
                        index=index)


class WatchUniverseTests(unittest.TestCase):
    def test_watch_pool_covers_the_mega_cap_tech_names(self):
        self.assertGreaterEqual(len(WATCH_SYMBOLS), 15)
        for ticker in ('NVDA', 'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AVGO', 'AMD', 'MU'):
            self.assertIn(ticker, WATCH_SYMBOLS)
        self.assertEqual(len(WATCH_SYMBOLS), len(set(WATCH_SYMBOLS)))


class PremarketMoveTests(unittest.TestCase):
    def test_todays_premarket_print_is_measured_against_the_prior_close(self):
        data = frame([('2026-11-20T15:55:00', 100.0), ('2026-11-20T18:00:00', 102.0),
                      ('2026-11-23T08:30:00', 106.4)])
        move = premarket_move(data, NOW)
        self.assertEqual(move['session'], 'premarket')
        self.assertAlmostEqual(move['change_pct'], 6.4)

    def test_before_the_premarket_opens_the_prior_after_hours_print_is_used(self):
        data = frame([('2026-11-20T15:55:00', 100.0), ('2026-11-20T19:30:00', 131.48)])
        move = premarket_move(data, datetime(2026, 11, 23, 3, 50, tzinfo=NY))
        self.assertEqual(move['session'], 'after_hours')
        self.assertAlmostEqual(move['change_pct'], 31.48)

    def test_a_frame_without_a_prior_regular_close_is_refused(self):
        data = frame([('2026-11-23T08:30:00', 106.4)])
        self.assertIsNone(premarket_move(data, NOW))

    def test_an_impossible_extended_hours_move_is_refused(self):
        data = frame([('2026-11-20T15:55:00', 100.0), ('2026-11-23T08:30:00', 900.0)])
        self.assertIsNone(premarket_move(data, NOW))


class SignalSelectionTests(unittest.TestCase):
    def test_gated_name_with_reinstatement_news_and_premarket_jump_is_captured(self):
        report = dict(results=[dict(market='US', symbol='NVDA', name='엔비디아',
                                    selection_filter='breakdown_or_rebound',
                                    preopen_decision='breakdown',
                                    indicators=dict(return_1d=-0.14, data_date='2026-11-20'))],
                      theme_sectors=[], market_regime={'US': {'above_ma20': False}})
        watch = signals.attach(report, now=NOW, quotes={'NVDA': quote(6.4)},
                               articles={'NVDA': [article('Jensen Huang reinstated as Nvidia CEO')]},
                               news_fetcher=lambda symbol, now: [])
        self.assertEqual([item['symbol'] for item in watch['items']], ['NVDA'])
        item = watch['items'][0]
        self.assertAlmostEqual(item['change_pct'], 6.4)
        self.assertEqual(item['catalysts'], ['ceo'])
        self.assertIn('reinstated', item['headline'])
        # 매수 계획·판정은 만들지 않는다.
        self.assertNotIn('preopen_signal', report['results'][0])
        self.assertNotIn('entry_limit', item)

    def test_a_name_missing_from_the_scan_is_still_watched(self):
        watch = signals.attach(dict(results=[], theme_sectors=[], market_regime={}), now=NOW,
                               quotes={'AVGO': quote(5.2)},
                               articles={'AVGO': [article('Broadcom wins a major AI chip order')]},
                               news_fetcher=lambda symbol, now: [])
        self.assertEqual([item['symbol'] for item in watch['items']], ['AVGO'])
        self.assertEqual(watch['items'][0]['catalysts'], ['deal'])

    def test_small_move_without_news_is_not_a_signal(self):
        items = select_signals({'NVDA': quote(2.4)}, {'NVDA': [article('Nvidia holds a developer event')]},
                               now=NOW)
        self.assertEqual(items, [])

    def test_news_alone_is_a_signal_without_a_move_label(self):
        items = select_signals({'NVDA': quote(1.2)},
                               {'NVDA': [article('Trump tariff plan hits chipmakers; Nvidia shares steady')]},
                               now=NOW)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['move_pct'])
        self.assertEqual(items[0]['catalysts'], ['policy'])
        text = '\n'.join(format_block(items))
        self.assertNotIn('±5%', text)
        self.assertIn('(NVDA) · 촉매: 정책·규제', text)
        self.assertIn('촉매: 정책·규제', text)

    def test_old_news_is_ignored(self):
        items = select_signals({'NVDA': quote(1.2)},
                               {'NVDA': [article('Nvidia CEO steps down', at='2026-11-21T12:00:00+00:00')]},
                               now=NOW)
        self.assertEqual(items, [])

    def test_biggest_premarket_moves_come_first_and_are_capped(self):
        quotes = {'NVDA': quote(6.0), 'TSLA': quote(-9.0), 'AMD': quote(5.4), 'MU': quote(12.0),
                  'AAPL': quote(-5.1), 'META': quote(5.0)}
        items = select_signals(quotes, {}, now=NOW, limit=5)
        self.assertEqual([item['symbol'] for item in items], ['MU', 'TSLA', 'NVDA', 'AMD', 'AAPL'])

    def test_a_failed_quote_fetch_is_reported_and_not_raised(self):
        watch = signals.collect(now=NOW, quote_fetcher=lambda symbols, now: {},
                                news_fetcher=lambda symbol, now: [])
        self.assertEqual(watch['items'], [])
        self.assertEqual(watch['quotes_seen'], 0)
        self.assertIsNone(watch['error'])


class MessageBlockTests(unittest.TestCase):
    def items(self):
        return select_signals({'NVDA': quote(6.4)},
                              {'NVDA': [article('Jensen Huang reinstated as Nvidia CEO')]}, now=NOW)

    def test_block_heading_and_lines(self):
        lines = format_block(self.items())
        # One heading line carries the 'not a buy signal' note (2026-11-25 short form).
        self.assertEqual(lines[0], f'{HEADING} · 매수 신호 아님')
        self.assertIn('엔비디아 (NVDA) · 프리마켓 +6.4%', lines[1])
        self.assertIn('촉매: CEO·경영진', lines[1])
        self.assertIn('Jensen Huang reinstated', lines[2])
        self.assertEqual(format_block([]), [])

    def test_intraday_message_puts_the_block_before_the_buy_heading(self):
        from kis_intraday import format_notification
        text = format_notification('US-late', [], datetime(2026, 11, 23, 9, 15, tzinfo=NY),
                                   None, sectors=[], themes=None, premarket=self.items())
        self.assertIn(HEADING, text)
        self.assertLess(text.index(HEADING), text.index('분석 결과'))

    def test_daily_summary_renders_the_prepared_watch(self):
        import scanner
        report = dict(results=[], sectors=[], market_regime={'US': {'above_ma20': False}},
                      premarket_watch=dict(items=self.items()),
                      buy_evaluation=dict(completed=True, decision_completed=2, data_excluded=0))
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake'}), \
                patch.object(scanner.requests, 'post') as post, \
                patch('scanner.delivery_status', return_value=(True, 'test')):
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            scanner.send_telegram_summary(report)
        message = post.call_args.kwargs['json']['text']
        self.assertIn(HEADING, message)
        self.assertIn('엔비디아 (NVDA)', message)
        self.assertLess(message.index(HEADING), message.index('분석 결과'))


if __name__ == '__main__':
    unittest.main()
