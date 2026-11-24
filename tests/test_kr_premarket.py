"""국내 장전 사전 시그널: 감시 풀, NXT 세션 판정, 장전 ±5%·촉매·간밤 미국 연동, 메시지 블록."""
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from kr_premarket import (
    HEADING,
    PREMARKET_MOVE_PCT,
    WATCH_CODES,
    WATCH_NAMES,
    collect,
    format_block,
    quote_from_output,
    select_signals,
    session_of,
    us_links,
)
from price_sanity import PriceError

KST = __import__('zoneinfo').ZoneInfo('Asia/Seoul')
MORNING = datetime(2026, 11, 24, 8, 15, tzinfo=KST)


def quote(change, session='nxt_premarket', at='2026-11-24T08:15:00+09:00', price=78000.0):
    return dict(change_pct=change, session=session, observed_at=at, price=price,
                previous_close=price / (1 + change / 100))


def article(headline, at='2026-11-24T07:40:00+09:00'):
    return dict(headline=headline, available_at=at, source='네이버 뉴스')


US_ROWS = [dict(market='US', rows=[dict(name='AI 반도체·GPU', return_pct=15.0),
                                   dict(name='로봇·자동화', return_pct=7.16),
                                   dict(name='은행', return_pct=1.2)])]


class WatchUniverseTests(unittest.TestCase):
    def test_watch_pool_covers_the_megacap_names(self):
        self.assertGreaterEqual(len(WATCH_CODES), 15)
        names = set(WATCH_NAMES.values())
        for name in ('삼성전자', 'SK하이닉스', '현대차', '기아', 'LG에너지솔루션', '삼성SDI',
                     'POSCO홀딩스', '셀트리온', '삼성바이오로직스', 'NAVER', '카카오',
                     '한화에어로스페이스', 'HD현대중공업', '두산에너빌리티', '레인보우로보틱스', 'KB금융'):
            self.assertIn(name, names)
        self.assertEqual(len(WATCH_CODES), len(set(WATCH_CODES)))
        for code in WATCH_CODES:
            self.assertEqual((len(code), code.isdigit()), (6, True), code)


class SessionTests(unittest.TestCase):
    def test_nxt_premarket_and_after_hours_are_named(self):
        self.assertEqual(session_of(MORNING), 'nxt_premarket')
        self.assertEqual(session_of(datetime(2026, 11, 24, 16, 30, tzinfo=KST)), 'nxt_after_hours')

    def test_outside_the_nxt_sessions_no_premarket_price_is_claimed(self):
        self.assertIsNone(session_of(datetime(2026, 11, 24, 9, 15, tzinfo=KST)))
        self.assertIsNone(session_of(datetime(2026, 11, 24, 7, 20, tzinfo=KST)))
        self.assertIsNone(session_of(datetime(2026, 11, 24, 21, 0, tzinfo=KST)))

    def test_weekend_has_no_session(self):
        self.assertIsNone(session_of(datetime(2026, 11, 28, 8, 30, tzinfo=KST)))

    def test_utc_input_is_read_in_korean_time(self):
        self.assertEqual(session_of(datetime(2026, 11, 23, 23, 15, tzinfo=timezone.utc)), 'nxt_premarket')


class QuoteParseTests(unittest.TestCase):
    def test_kis_strings_are_read_and_measured_against_the_regular_close(self):
        output = dict(stck_prpr='78,000', prdy_vrss='4,700', prdy_ctrt='6.41')
        found = quote_from_output(output, 73300)
        self.assertAlmostEqual(found['change_pct'], 6.41, places=2)
        self.assertEqual(found['previous_close'], 73300)
        self.assertEqual(found['base'], 'previous_regular_close')

    def test_without_a_regular_close_the_provider_change_is_used_and_named(self):
        found = quote_from_output(dict(stck_prpr='78000', prdy_vrss='4700', prdy_ctrt='6.41'))
        self.assertAlmostEqual(found['change_pct'], 6.41)
        self.assertEqual(found['base'], 'nxt_previous_close')

    def test_a_move_beyond_the_korean_price_limit_is_a_price_error(self):
        with self.assertRaises(PriceError) as caught:
            quote_from_output(dict(stck_prpr='96000', prdy_ctrt='40.0'))
        self.assertEqual(caught.exception.reason, 'price_limit_exceeded')

    def test_a_penny_priced_ticker_is_refused(self):
        with self.assertRaises(PriceError) as caught:
            quote_from_output(dict(stck_prpr='500', prdy_ctrt='2.0'))
        self.assertEqual(caught.exception.reason, 'low_price')

    def test_a_zero_or_missing_price_is_refused(self):
        for output in ({'stck_prpr': '0'}, {}, {'stck_prpr': '없음'}):
            with self.assertRaises(PriceError) as caught:
                quote_from_output(output)
            self.assertEqual(caught.exception.reason, 'abnormal_price')


class UsLinkTests(unittest.TestCase):
    def test_a_korean_theme_is_linked_to_its_us_counterpart(self):
        links = us_links('005930', US_ROWS)
        self.assertEqual([link['name'] for link in links], ['AI 반도체·GPU'])
        self.assertAlmostEqual(links[0]['return_pct'], 15.0)

    def test_a_quiet_us_theme_is_not_an_overnight_context(self):
        self.assertEqual(us_links('105560', US_ROWS), [])
        self.assertEqual(us_links('035420', US_ROWS), [])

    def test_a_stale_us_snapshot_is_not_called_overnight(self):
        stale = [dict(market='US', data_date='2026-11-18',
                      rows=[dict(name='AI 반도체·GPU', return_pct=15.0)])]
        self.assertEqual(us_links('005930', stale, today=datetime(2026, 11, 24).date()), [])
        fresh = [dict(market='US', data_date='2026-11-23',
                      rows=[dict(name='AI 반도체·GPU', return_pct=15.0)])]
        self.assertEqual(len(us_links('005930', fresh, today=datetime(2026, 11, 24).date())), 1)


class SelectionTests(unittest.TestCase):
    def test_a_premarket_jump_is_a_signal_without_any_news(self):
        items = select_signals({'005930': quote(6.4)}, {}, MORNING)
        self.assertEqual([item['code'] for item in items], ['005930'])
        self.assertEqual(items[0]['move_pct'], 6.4)
        self.assertNotIn('preopen_signal', items[0])

    def test_a_small_move_is_not_a_signal(self):
        self.assertEqual(select_signals({'005930': quote(2.4)}, {}, MORNING), [])

    def test_a_downside_jump_counts_too(self):
        items = select_signals({'000660': quote(-5.6)}, {}, MORNING)
        self.assertEqual(items[0]['move_pct'], -5.6)

    def test_news_alone_is_a_signal_with_the_move_marked_as_unknown(self):
        items = select_signals({'012450': quote(1.2)},
                               {'012450': [article('한화에어로스페이스, 유럽과 2조원 규모 공급 계약 체결')]},
                               MORNING)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['move_pct'])
        self.assertEqual(items[0]['catalysts'], ['deal'])
        text = '\n'.join(format_block(items, 'nxt_premarket'))
        self.assertIn('NXT 장전 ±5% 미만', text)
        self.assertIn('촉매: 수주·M&A', text)

    def test_old_news_is_ignored(self):
        items = select_signals({'012450': quote(1.2)},
                               {'012450': [article('한화에어로스페이스 수주', at='2026-11-20T09:00:00+09:00')]},
                               MORNING)
        self.assertEqual(items, [])

    def test_a_quiet_headline_is_not_a_catalyst(self):
        items = select_signals({'005930': quote(1.2)},
                               {'005930': [article('삼성전자, 분기 배당금 지급 공시')]}, MORNING)
        self.assertEqual(items, [])

    def test_overnight_us_theme_alone_puts_a_name_on_the_block(self):
        items = select_signals({'005930': quote(1.2)}, {}, MORNING, us_rows=US_ROWS)
        # 삼성전자·SK하이닉스·삼성전기가 모두 AI 반도체 축이라 미 동반은 3종목까지 붙는다.
        codes = [item['code'] for item in items]
        self.assertIn('005930', codes)
        self.assertTrue(all(item['move_pct'] is None for item in items))
        self.assertEqual(items[0]['us_links'][0]['name'], 'AI 반도체·GPU')
        text = '\n'.join(format_block(items, 'nxt_premarket'))
        self.assertIn('간밤 미국 AI 반도체·GPU +15.0%', text)

    def test_us_link_only_names_are_capped(self):
        items = select_signals({}, {}, MORNING, us_rows=US_ROWS, limit=5, us_link_only=2)
        self.assertEqual(len(items), 2)

    def test_a_real_move_ranks_above_a_us_link_only_name(self):
        quotes = {'005930': quote(1.1), '000660': quote(5.4)}
        items = select_signals(quotes, {}, MORNING, us_rows=US_ROWS)
        self.assertEqual(items[0]['code'], '000660')

    def test_the_biggest_moves_come_first_and_are_capped(self):
        quotes = {'005930': quote(6.0), '000660': quote(-9.0), '005380': quote(5.4),
                  '000270': quote(-5.1), '034020': quote(7.0), '105560': quote(8.0)}
        items = select_signals(quotes, {}, MORNING, limit=5)
        self.assertEqual([item['code'] for item in items],
                         ['000660', '105560', '034020', '005930', '005380'])

    def test_a_naive_check_time_is_refused(self):
        with self.assertRaises(ValueError):
            select_signals({}, {}, datetime(2026, 11, 24, 8, 15))


class CollectTests(unittest.TestCase):
    def test_outside_the_session_no_quote_request_is_made(self):
        calls = []
        watch = collect(now=datetime(2026, 11, 24, 9, 30, tzinfo=KST),
                        quote_fetcher=lambda symbols, now, previous: calls.append(symbols) or {},
                        news_fetcher=lambda code, name, now: [])
        self.assertEqual(calls, [])
        self.assertIsNone(watch['session'])
        self.assertEqual(watch['items'], [])

    def test_a_quote_error_on_one_symbol_is_recorded_and_the_rest_survives(self):
        def quotes(symbols, now, previous):
            return {'005930': quote(6.4),
                    '000660': dict(error='low_price', session='nxt_premarket')}
        watch = collect(now=MORNING, quote_fetcher=quotes, news_fetcher=lambda code, name, now: [])
        self.assertEqual([item['code'] for item in watch['items']], ['005930'])
        self.assertEqual(watch['quotes_seen'], 1)
        self.assertEqual(watch['rejected'], [dict(symbol='000660', reason='low_price')])

    def test_a_failed_quote_fetch_is_reported_not_raised(self):
        def broken(symbols, now, previous):
            raise RuntimeError('KIS 자격증명 없음')
        watch = collect(now=MORNING, quote_fetcher=broken, news_fetcher=lambda code, name, now: [])
        self.assertEqual((watch['items'], watch['error']), ([], 'RuntimeError'))

    def test_news_still_reaches_the_block_when_quotes_are_unavailable(self):
        watch = collect(now=MORNING, quotes={},
                        news_fetcher=lambda code, name, now: (
                            [article('삼성전자, 4분기 영업이익 잠정 실적 공시')] if code == '005930' else []))
        self.assertEqual([item['code'] for item in watch['items']], ['005930'])
        self.assertIsNone(watch['items'][0]['change_pct'])


class MessageBlockTests(unittest.TestCase):
    def items(self):
        return select_signals({'005930': quote(6.4)},
                              {'005930': [article('삼성전자, 엔비디아에 HBM4 공급 계약 체결')]},
                              MORNING, us_rows=US_ROWS)

    def test_block_heading_lines_and_no_buy_language(self):
        lines = format_block(self.items(), 'nxt_premarket')
        self.assertEqual(lines[0], HEADING)
        self.assertIn('매수 신호 아님', lines[1])
        self.assertTrue(lines[2].startswith('• 삼성전자 (005930) · NXT 장전 +6.4% (08:15)'))
        self.assertIn('촉매: 수주·M&A', lines[2])
        self.assertIn('간밤 미국 AI 반도체·GPU +15.0%', lines[2])
        self.assertIn('HBM4 공급 계약', lines[3])
        self.assertEqual(format_block([]), [])

    def test_without_a_session_print_the_line_says_so_instead_of_inventing_a_move(self):
        # 07:20처럼 NXT 개장 전에는 체결가가 없다. 그때 0%나 전일 종가를 '장전'이라 쓰지 않는다.
        items = select_signals({}, {'012450': [article('한화에어로스페이스, 폴란드와 계약 체결')]}, MORNING)
        text = '\n'.join(format_block(items, None))
        self.assertIn('장전 시세 미확인', text)
        self.assertIn('촉매: 수주·M&A', text)

    def test_intraday_message_puts_the_block_before_the_buy_heading(self):
        from kis_intraday import format_notification
        text = format_notification('NXT', [], datetime(2026, 11, 24, 8, 15, tzinfo=KST),
                                   None, sectors=[], themes=None, kr_premarket=self.items())
        self.assertIn(HEADING, text)
        self.assertLess(text.index(HEADING), text.index('분석 결과'))

    def test_daily_summary_renders_the_prepared_watch(self):
        import scanner
        watch = collect(now=MORNING, quotes={'005930': quote(6.4)}, us_rows=US_ROWS,
                        news_fetcher=lambda code, name, now: [])
        report = dict(results=[], sectors=[], market_regime={},
                      kr_premarket_watch=watch,
                      buy_evaluation=dict(completed=True, decision_completed=2, data_excluded=0))
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake'}), \
                patch.object(scanner.requests, 'post') as post, \
                patch('scanner.delivery_status', return_value=(True, 'test')), \
                patch('urgent_issues.select_urgent_issues', return_value={}):
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            scanner.send_telegram_summary(report)
        message = post.call_args.kwargs['json']['text']
        self.assertIn(HEADING, message)
        self.assertIn('삼성전자 (005930)', message)
        self.assertLess(message.index(HEADING), message.index('분석 결과'))


if __name__ == '__main__':
    unittest.main()
