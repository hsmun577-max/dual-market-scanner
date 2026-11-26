"""세부 테마·이슈 섹터 집계와 하락장 상대강세 포착, 그리고 메시지 계약 확인."""
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from theme_sectors import (
    MINIMUM_MEMBERS,
    THEME_UNIVERSE,
    format_theme_block,
    issue_line,
    member_metric,
    rank_theme_sectors,
    sector_section,
    theme_lines,
    theme_rows,
    theme_sector_snapshot,
)

NOW = datetime(2026, 11, 23, 21, 0)


def frame(closes, volumes=None, end='2026-11-23'):
    index = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({'Close': closes, 'Volume': volumes or [100.0] * len(closes)}, index=index)


def metric(move, turnover_ratio=1.0, turnover=100.0, data_date='2026-11-23'):
    return dict(return_pct=move, turnover_ratio=turnover_ratio, turnover=turnover,
                data_date=data_date, intraday=False, volume_ratio=1.0)


ROBOT = ("휴머노이드·로봇", (("A", "알파로봇"), ("B", "베타로봇")))
BANK = ("금융·밸류업", (("C", "감마금융"), ("D", "델타금융")))
CHIP = ("HBM·메모리", (("E", "엡실론전자"), ("F", "제타전자")))


class MemberMetricTests(unittest.TestCase):
    def test_last_session_move_volume_and_turnover_baseline(self):
        row = member_metric(frame([100.0] * 21 + [110.0], [100.0] * 21 + [300.0]), NOW.date())
        self.assertAlmostEqual(row['return_pct'], 10.0)
        self.assertAlmostEqual(row['volume_ratio'], 3.0)
        self.assertAlmostEqual(row['turnover_ratio'], 3.3)
        self.assertTrue(row['intraday'])

    def test_previous_session_is_not_labelled_intraday(self):
        row = member_metric(frame([100.0] * 21 + [101.0], end='2026-11-20'), NOW.date())
        self.assertFalse(row['intraday'])
        self.assertEqual(row['data_date'], '2026-11-20')

    def test_short_history_is_refused_instead_of_guessed(self):
        with self.assertRaises(ValueError):
            member_metric(frame([100.0] * 5), NOW.date())


class ThemeAggregationTests(unittest.TestCase):
    def test_theme_move_is_equal_weight_and_turnover_is_money_weighted(self):
        rows = theme_rows('KR', (ROBOT,), {'A': metric(10.0, 1.0, 100.0), 'B': metric(6.0, 3.0, 900.0)}, -3.5)
        row = rows[0]
        self.assertAlmostEqual(row['return_pct'], 8.0)
        self.assertAlmostEqual(row['market_excess_pct'], 11.5)
        self.assertAlmostEqual(row['turnover_ratio'], 2.5)  # 1000 / (100+300) not the 2.0 mean
        self.assertEqual((row['up'], row['total'], row['breadth_pct']), (2, 2, 100.0))
        self.assertEqual([leader['name'] for leader in row['leaders']], ['알파로봇', '베타로봇'])
        self.assertEqual(row['state'], '주도')

    def test_movement_against_the_index_is_named_relative_strength_not_an_uptrend(self):
        rows = theme_rows('KR', (BANK,), {'C': metric(-1.5), 'D': metric(-2.0)}, -3.5)
        self.assertEqual(rows[0]['state'], '상대강세')
        self.assertLess(rows[0]['return_pct'], 0)

    def test_breadth_counts_only_members_with_a_quote(self):
        rows = theme_rows('KR', (ROBOT,), {'A': metric(1.0)}, -1.0)
        self.assertEqual((rows[0]['up'], rows[0]['total'], rows[0]['declared']), (1, 1, 2))


class RankingTests(unittest.TestCase):
    def test_robot_theme_leads_while_the_index_falls(self):
        metrics = {'A': metric(30.0), 'B': metric(5.4), 'C': metric(-0.3), 'D': metric(-0.5)}
        ranked = rank_theme_sectors(theme_rows('KR', (BANK, ROBOT), metrics, -3.31))
        self.assertEqual([row['name'] for row in ranked], ['휴머노이드·로봇', '금융·밸류업'])
        self.assertEqual(ranked[0]['state'], '주도')

    def test_all_negative_day_still_ranks_the_least_fallen_theme_first(self):
        metrics = {'A': metric(-4.0), 'B': metric(-5.0), 'C': metric(-1.0), 'D': metric(-1.4),
                   'E': metric(-2.0), 'F': metric(-2.6)}
        ranked = rank_theme_sectors(theme_rows('KR', (ROBOT, BANK, CHIP), metrics, -3.31))
        self.assertEqual(ranked[0]['name'], '금융·밸류업')
        self.assertEqual(ranked[0]['state'], '상대강세')
        self.assertTrue(all(row['return_pct'] < 0 for row in ranked))
        text = '\n'.join(format_theme_block(dict(market='KR', benchmark_return_pct=-3.31,
                                                 data_date='2026-11-23', intraday=False, rows=ranked)))
        self.assertIn('지수 대비 강세', text)
        self.assertNotIn('주도', text)  # 하락장 상대강세를 상승으로 쓰지 않는다
        self.assertIn('상대 선두', text)

    def test_a_single_quoted_member_cannot_become_a_theme(self):
        rows = theme_rows('KR', (ROBOT, BANK), {'A': metric(3.0), 'C': metric(-1.0), 'D': metric(-1.0)}, -1.0)
        self.assertEqual([row['name'] for row in rank_theme_sectors(rows)], ['금융·밸류업'])
        self.assertEqual(MINIMUM_MEMBERS, 2)

    def test_limit_is_respected(self):
        rows = theme_rows('KR', (ROBOT, BANK, CHIP),
                          {'A': metric(3.0), 'B': metric(2.0), 'C': metric(-1.0), 'D': metric(-1.0),
                           'E': metric(-2.0), 'F': metric(-2.0)}, -1.0)
        self.assertEqual(len(rank_theme_sectors(rows, limit=2)), 2)


class IssueLineTests(unittest.TestCase):
    def row(self):
        return dict(name='휴머노이드·로봇', return_pct=4.4, up=5, total=8, turnover_ratio=2.9, state='주도',
                    leaders=[dict(symbol='090710.KQ', name='휴림로봇', return_pct=30.0),
                             dict(symbol='108490.KQ', name='로보티즈', return_pct=5.4)])

    def test_headline_comes_from_the_existing_news_collector(self):
        def headlines(market, symbol, name, now, alias=None):
            self.assertEqual((market, symbol, name), ('KR', '090710.KQ', '휴림로봇'))
            return [dict(headline='휴림로봇, 한화로보틱스와 로봇 공급 &quot;계약&quot; 체결', available_at='')]
        self.assertEqual(issue_line(self.row(), 'KR', NOW, headlines, [5]),
                         '이슈: 휴림로봇, 한화로보틱스와 로봇 공급 "계약" 체결')

    def test_no_article_falls_back_to_supply_and_breadth(self):
        text = issue_line(self.row(), 'KR', NOW, lambda *args: [], [5])
        self.assertEqual(text, '이슈: 뉴스 미확인 · 상승 5/8종목 · 거래대금 2.9배 수급')

    def test_lookup_budget_is_not_spent_on_an_exhausted_run(self):
        def headlines(*args):
            raise AssertionError('budget이 없는데 뉴스를 조회했다')
        self.assertTrue(issue_line(self.row(), 'KR', NOW, headlines, [0]).startswith('이슈: 뉴스 미확인'))

    def test_shortened_provider_title_drops_the_dangling_bracket(self):
        def headlines(*args):
            return [dict(headline='휴림로봇 주가 ‘불기둥’...한화로보틱스와 로봇 공급 계약 [오늘, 이...',
                         available_at='')]
        self.assertEqual(issue_line(self.row(), 'KR', NOW, headlines, [5]),
                         '이슈: 휴림로봇 주가 ‘불기둥’...한화로보틱스와 로봇 공급 계약')

    def test_long_headline_is_shortened(self):
        def headlines(*args):
            return [dict(headline='가' * 200, available_at='')]
        self.assertLessEqual(len(issue_line(self.row(), 'KR', NOW, headlines, [5])), 64 + len('이슈: '))


class BlockFormatTests(unittest.TestCase):
    def snapshot(self, **changes):
        row = dict(name='휴머노이드·로봇·액추에이터', return_pct=4.4, up=5, total=8, turnover_ratio=2.9,
                   state='주도', issue='이슈: 로봇 공급 계약 체결',
                   leaders=[dict(symbol='090710.KQ', name='휴림로봇', return_pct=30.0),
                            dict(symbol='108490.KQ', name='로보티즈', return_pct=5.4)])
        snapshot = dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23',
                        intraday=False, rows=[row], error=None)
        snapshot.update(changes)
        return snapshot

    def test_theme_line_carries_name_move_turnover_and_breadth(self):
        lines = format_theme_block(self.snapshot(), 'KR')
        # 2026-11-26 지시: 거래대금(규모·유입 배수)이 등락률보다 먼저 온다.
        self.assertEqual(lines[0], '세부 테마 · 전일(11/23) 마감 기준 · 거래대금 유입 순 · KOSPI -3.31% 대비 상대강도')
        self.assertEqual(lines[1], '1. 휴머노이드·로봇·액추에이터 거래대금 2.9배 · +4.4% · 상승 5/8')
        self.assertEqual(lines[2], '   주도 휴림로봇 +30.0% · 로보티즈 +5.4%')
        self.assertEqual(lines[3], '   이슈: 로봇 공급 계약 체결')

    def test_intraday_block_says_so(self):
        head = format_theme_block(self.snapshot(intraday=True), 'KR')[0]
        self.assertIn('장중', head)
        self.assertIn('기준', head)

    def test_the_close_block_says_it_is_the_previous_session(self):
        head = format_theme_block(self.snapshot(), 'KR')[0]
        self.assertIn('전일(11/23) 마감 기준', head)
        self.assertNotIn('종가', head)

    def test_missing_theme_data_is_reported_not_hidden(self):
        self.assertEqual(format_theme_block(dict(market='KR', rows=[], error='시세 미확보'), 'KR'),
                         ['세부 테마: 시세 미확보'])
        self.assertEqual(format_theme_block(None, 'KR'), [])

    def test_two_markets_get_their_own_block(self):
        us = dict(market='US', benchmark_return_pct=-1.0, data_date='2026-11-20', intraday=False,
                  rows=[dict(name='에너지·원유', return_pct=-0.3, up=4, total=5, turnover_ratio=1.2,
                             state='상대강세', leaders=[])])
        lines = theme_lines([self.snapshot(), us], limit=3)
        self.assertIn('KOSPI -3.31% 대비', lines[0])
        self.assertIn('S&P500 -1.00% 대비', next(line for line in lines if 'S&P500' in line))


class SectorSectionTests(unittest.TestCase):
    def test_without_theme_data_the_old_header_is_unchanged(self):
        etf = [dict(market='KR', name='반도체', return_pct=1.0, market_excess_pct=0.5, turnover_ratio=1.1)]
        self.assertEqual(sector_section(etf, None, 'KR'), ['오늘의 주요 섹터', '1위 반도체'])
        # No reading at all is no block (2026-11-25): a heading plus '확인 불가' was noise.
        self.assertEqual(sector_section([], None, 'KR'), [])

    def test_theme_block_replaces_the_coarse_word_list_entirely(self):
        # 세부 테마가 있으면 '오늘의 주요 섹터 / 1위 기계·장비' 대분류 목록은 싣지 않는다.
        # 같은 내용을 두 번 말하는 것이고, 세부 테마가 더 구체적이다(2026-11-24 지시).
        snapshot = dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23', intraday=False,
                        rows=[dict(name='휴머노이드·로봇', return_pct=4.4, up=5, total=8, turnover_ratio=2.9,
                                   state='주도', issue='이슈: 로봇 공급 계약')])
        lines = sector_section([], snapshot, 'KR')
        self.assertTrue(lines[0].startswith('세부 테마'))
        self.assertNotIn('오늘의 주요 섹터', lines)
        self.assertIn('휴머노이드·로봇', '\n'.join(lines[1:]))
        self.assertNotIn('확인 불가', lines)

    def test_official_industry_override_is_dropped_once_the_theme_block_exists(self):
        snapshot = dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23', intraday=False,
                        rows=[dict(name='휴머노이드·로봇', return_pct=4.4, up=5, total=8, turnover_ratio=2.9,
                                   state='주도', leaders=[])])
        lines = sector_section([], snapshot, 'KR', override=['오늘의 주요 섹터', '1위 은행'])
        self.assertNotIn('1위 은행', lines)
        self.assertIn('휴머노이드·로봇', '\n'.join(lines))


class SnapshotTests(unittest.TestCase):
    def test_no_prices_reports_the_gap_and_never_invents_a_theme(self):
        snapshot = theme_sector_snapshot('KR', NOW, downloader=lambda symbols, market: None)
        self.assertEqual(snapshot['rows'], [])
        self.assertEqual(snapshot['error'], '시세 미확보')
        self.assertIsNone(snapshot['data_date'])

    def test_a_provider_error_is_reported_not_raised(self):
        def broken(symbols, market):
            raise TimeoutError('provider down')
        snapshot = theme_sector_snapshot('KR', NOW, downloader=broken)
        self.assertEqual((snapshot['rows'], snapshot['error']), ([], '시세 미확보'))

    def test_real_member_frames_flow_into_a_ranked_theme(self):
        robots = dict(THEME_UNIVERSE['KR'])['휴머노이드·로봇·액추에이터']
        # 한국 종목은 원 단위다. 1,000원 미만 종가는 가격 오류로 걸러지므로 실제 눈금으로 둔다.
        rendered = {
            robots[0][0]: frame([50000.0] * 21 + [62000.0], [100.0] * 21 + [400.0]),
            robots[1][0]: frame([50000.0] * 21 + [52500.0]),
            robots[2][0]: frame([50000.0] * 21 + [49000.0]),
            '^KS11': frame([2500.0] * 21 + [2417.25]),
        }
        snapshot = theme_sector_snapshot('KR', NOW, downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        self.assertEqual(snapshot['benchmark_return_pct'], -3.31)
        self.assertEqual(snapshot['quoted'], 3)
        self.assertEqual(snapshot['rows'][0]['name'], '휴머노이드·로봇·액추에이터')
        self.assertGreater(snapshot['rows'][0]['market_excess_pct'], 6)
        # 기사가 없을 때의 '뉴스 미확인 · 상승 3/3 · 거래대금' 요약은 위 테마 줄과 숫자가 겹쳐
        # 메시지에는 싣지 않는다. 기록에는 남는다.
        self.assertIn('뉴스 미확인', snapshot['rows'][0]['issue'])
        self.assertFalse(any('뉴스 미확인' in line for line in format_theme_block(snapshot, 'KR')))


class ThemeUniverseTests(unittest.TestCase):
    def test_both_markets_carry_enough_themes_and_well_formed_symbols(self):
        for market, themes in THEME_UNIVERSE.items():
            self.assertGreaterEqual(len(themes), 10, market)
            for name, members in themes:
                self.assertTrue(name and members, market)
                self.assertGreaterEqual(len(members), 2, name)
                for member in members:
                    symbol, label = member[0], member[1]
                    self.assertTrue(label.strip(), symbol)
                    if market == 'KR':
                        code, suffix = symbol.split('.')
                        self.assertEqual((len(code), suffix in ('KS', 'KQ')), (6, True), symbol)
                    else:
                        self.assertEqual((symbol.isupper(), len(symbol) <= 5), (True, True), symbol)

    def test_korean_symbols_are_unique_inside_a_theme(self):
        for name, members in THEME_UNIVERSE['KR']:
            symbols = [member[0] for member in members]
            self.assertEqual(len(symbols), len(set(symbols)), name)


class MessageIntegrationTests(unittest.TestCase):
    def snapshot(self):
        return dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23', intraday=False,
                    rows=[dict(name='휴머노이드·로봇·액추에이터', return_pct=4.4, up=5, total=8,
                               turnover_ratio=2.9, state='주도', issue='이슈: 로봇 공급 계약 체결',
                               leaders=[dict(symbol='090710.KQ', name='휴림로봇', return_pct=30.0)])])

    def daily_message(self, report):
        import scanner
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake'}), \
                patch.object(scanner.requests, 'post') as post, \
                patch('scanner.delivery_status', return_value=(True, 'test')), \
                patch('urgent_issues.select_urgent_issues', return_value={}):
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            scanner.send_telegram_summary(report)
        return post.call_args.kwargs['json']['text']

    def test_the_daily_message_drops_the_coarse_sector_list_when_themes_exist(self):
        sectors = [dict(market='KR', name='기계·장비', return_pct=2.0, market_excess_pct=1.0,
                        turnover_ratio=1.4)]
        text = self.daily_message(dict(results=[], sectors=sectors, market_regime={},
                                       theme_sectors=[self.snapshot()],
                                       buy_evaluation=dict(completed=True, decision_completed=1)))
        self.assertNotIn('오늘의 주요 섹터', text)
        self.assertNotIn('1위 기계·장비', text)
        self.assertIn('세부 테마', text)
        self.assertIn('휴머노이드·로봇·액추에이터', text)

    def test_without_theme_data_the_daily_message_keeps_the_sector_list(self):
        sectors = [dict(market='KR', name='기계·장비', return_pct=2.0, market_excess_pct=1.0,
                        turnover_ratio=1.4)]
        text = self.daily_message(dict(results=[], sectors=sectors, market_regime={},
                                       theme_sectors=[],
                                       buy_evaluation=dict(completed=True, decision_completed=1)))
        self.assertIn('오늘의 주요 섹터', text)
        self.assertIn('1위 기계·장비', text)

    def test_live_message_keeps_the_sector_heading_first_and_shows_the_theme(self):
        from datetime import timezone
        from zoneinfo import ZoneInfo

        from kis_intraday import format_notification
        snapshot = self.snapshot()
        text = format_notification('KRX', [], datetime(2026, 11, 23, 9, 15, tzinfo=ZoneInfo('Asia/Seoul')),
                                   None, sectors=[], themes=snapshot)
        self.assertTrue(text.startswith('세부 테마'))
        self.assertNotIn('오늘의 주요 섹터', text)
        self.assertIn('휴머노이드·로봇·액추에이터', text)
        self.assertIn('휴림로봇 +30.0%', text)
        self.assertLess(text.index('휴머노이드'), text.index('분석 결과'))


class SessionAlignmentTests(unittest.TestCase):
    """2026-11-26: 11/25 구성종목 등락에 11/24 지수 등락(+6.32%)이 붙었다.

    Yahoo에 11/25 지수 봉이 없었는데 테마 경로는 두 날짜를 맞추지 않고 각자의 마지막 봉을
    읽었다. 이제 구성종목은 자기 세션(최빈값)으로 집계하고, 지수가 그 세션 봉을 갖고 있을
    때만 상대강도를 내며, 없으면 지수의 마지막 세션과 지연을 밝히고 상대강도는 비운다.
    """

    NOW_26 = datetime(2026, 11, 26, 6, 38)

    def rendered(self, robots, index_end='2026-11-24', index_closes=None):
        return {
            robots[0][0]: frame([50000.0] * 21 + [62000.0, 68000.0], end='2026-11-25'),
            robots[1][0]: frame([50000.0] * 21 + [52500.0, 53000.0], end='2026-11-25'),
            robots[2][0]: frame([50000.0] * 21 + [49000.0, 49000.0], end='2026-11-25'),
            '^KS11': frame(index_closes or [2500.0] * 21 + [2417.25], end=index_end),
        }

    def test_the_index_move_is_read_on_the_same_session_as_the_members(self):
        robots = dict(THEME_UNIVERSE['KR'])['휴머노이드·로봇·액추에이터']
        snapshot = theme_sector_snapshot('KR', self.NOW_26,
                                         downloader=lambda symbols, market: self.rendered(robots),
                                         headlines=lambda *args: [])
        # 구성종목은 11/25(자기 마지막 봉)로 집계하고, 지수는 11/24 봉까지밖에 없다.
        self.assertEqual(snapshot['data_date'], '2026-11-25')
        self.assertEqual(snapshot['benchmark_date'], '2026-11-24')
        self.assertEqual(snapshot['benchmark_lag_sessions'], 1)
        self.assertFalse(snapshot['benchmark_aligned'])
        self.assertAlmostEqual(snapshot['benchmark_return_pct'], -3.31, places=2)
        row = snapshot['rows'][0]
        self.assertAlmostEqual(row['return_pct'], 3.54, places=2)  # 11/25: +9.68 · +0.95 · 0
        self.assertNotAlmostEqual(row['return_pct'], 9.0, places=2)  # 11/24 값이 섞이면 안 된다
        # 다른 세션의 지수 등락으로 상대강도를 만들지 않는다.
        self.assertIsNone(row['benchmark_return_pct'])
        self.assertIsNone(row['market_excess_pct'])
        self.assertEqual(row['data_date'], '2026-11-25')
        head = format_theme_block(snapshot, 'KR')[0]
        self.assertIn('KOSPI 11/24 -3.31%', head)
        self.assertIn('지수 1세션 지연', head)
        self.assertNotIn('상대강도', head)

    def test_a_member_without_the_session_bar_is_left_out_of_the_average(self):
        robots = dict(THEME_UNIVERSE['KR'])['휴머노이드·로봇·액추에이터']
        rendered = self.rendered(robots)
        # 한 종목만 11/20에 멈춰 있다(지수·다른 종목은 11/24까지). 그 종목의 등락을 섞으면 테마 평균이 다른 세션 값이 된다.
        rendered[robots[2][0]] = frame([50000.0] * 21 + [60000.0], end='2026-11-20')
        snapshot = theme_sector_snapshot('KR', self.NOW_26,
                                         downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        row = snapshot['rows'][0]
        self.assertEqual((row['total'], row['up']), (2, 2))
        self.assertAlmostEqual(row['return_pct'], 5.31, places=2)  # 11/25: (+9.68 +0.95) / 2
        self.assertEqual(snapshot['unaligned'],
                         [dict(symbol=robots[2][0], data_date='2026-11-20')])

    def test_a_missing_index_leaves_strength_unstated_instead_of_wrong(self):
        robots = dict(THEME_UNIVERSE['KR'])['휴머노이드·로봇·액추에이터']
        rendered = self.rendered(robots)
        rendered.pop('^KS11')
        snapshot = theme_sector_snapshot('KR', self.NOW_26,
                                         downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        self.assertIsNone(snapshot['benchmark_return_pct'])
        self.assertIsNone(snapshot['rows'][0]['market_excess_pct'])
        self.assertNotIn('상대강도', format_theme_block(snapshot, 'KR')[0])


if __name__ == '__main__':
    unittest.main()


class SessionModeTests(unittest.TestCase):
    """2026-11-26 지시: 장전은 전일 마감 기준, 장중은 당일 실시간 등락·거래대금 기준.

    증권사 10:45 화면(보안주·사이버보안 1위, FC-BGA·PCB·기판 2위)과 07:50 알림의 순위가
    달랐던 것은 장전 메시지가 전일 종가를 쓰면서도 그 사실을 흐리게 적었고, 장중 순위가
    전일 종가 기준 그대로였기 때문이다.
    """

    TODAY = datetime(2026, 11, 26, 10, 45)

    def frame(self, previous_pct, today_pct, base=10000.0, volume_today=1000.0,
              end='2026-11-26'):
        previous = base * (1 + previous_pct)
        today = previous * (1 + today_pct)
        return frame([base] * 21 + [previous, today], [1000.0] * 22 + [volume_today], end=end)

    def prices(self, today_volume=3000.0):
        return {
            # 보안: 오늘 +12%, 거래대금 평소의 3배 → 장중 1위
            '053800.KQ': self.frame(0.02, 0.12, 50000.0, today_volume),
            '150900.KQ': self.frame(0.01, 0.10, 10000.0, today_volume),
            # FC-BGA: 오늘 +3%, 거래대금 평소 수준
            '009150.KS': self.frame(-0.01, 0.03, 700000.0, 1000.0),
            '323280.KQ': self.frame(0.00, 0.02, 5000.0, 1000.0),
            '^KS11': self.frame(0.0, -0.02, 2500.0, 1000.0),
        }

    def snapshot(self, mode, now=None):
        return theme_sector_snapshot('KR', now or self.TODAY, mode=mode,
                                     downloader=lambda symbols, market: self.prices(),
                                     headlines=lambda *args: [])

    def test_the_preopen_snapshot_reads_the_previous_close_and_says_so(self):
        snapshot = self.snapshot('previous')
        self.assertEqual(snapshot['data_date'], '2026-11-25')
        self.assertFalse(snapshot['intraday'])
        head = format_theme_block(snapshot, 'KR')[0]
        self.assertIn('전일(11/25) 마감 기준', head)
        self.assertNotIn('장중', head)
        security = next(row for row in snapshot['rows'] if row['name'] == '보안·사이버보안')
        self.assertAlmostEqual(security['return_pct'], 1.5, places=2)  # 전일 +2%·+1%
        self.assertIsNone(security['turnover_pace_ratio'])

    def test_the_intraday_snapshot_ranks_on_the_live_move_and_turnover_pace(self):
        snapshot = self.snapshot('intraday')
        self.assertEqual(snapshot['data_date'], '2026-11-26')
        self.assertTrue(snapshot['intraday'])
        self.assertEqual(snapshot['mode'], 'intraday')
        head = format_theme_block(snapshot, 'KR')[0]
        self.assertIn('장중 10:45 기준', head)
        self.assertIn('당일 등락·거래대금', head)
        # 경과 시간(105/390분)을 반영한 거래대금 페이스가 원배수보다 크다.
        security = next(row for row in snapshot['rows'] if row['name'] == '보안·사이버보안')
        self.assertAlmostEqual(security['turnover_ratio'], 3.41, places=2)
        self.assertAlmostEqual(security['turnover_pace_ratio'], 12.67, places=1)
        self.assertEqual(snapshot['rows'][0]['name'], '보안·사이버보안')
        line = next(line for line in format_theme_block(snapshot, 'KR') if line.startswith('1. '))
        self.assertIn('거래대금', line)
        self.assertIn('억', line)

    def test_the_session_clock_is_only_inside_the_regular_session(self):
        from theme_sectors import session_progress
        self.assertIsNone(session_progress(datetime(2026, 11, 26, 7, 50), 'KR'))
        self.assertIsNone(session_progress(datetime(2026, 11, 26, 15, 30), 'KR'))
        self.assertAlmostEqual(session_progress(datetime(2026, 11, 26, 10, 45), 'KR'),
                               105 / 390, places=6)
        self.assertAlmostEqual(session_progress(datetime(2026, 11, 26, 12, 0), 'US'),
                               150 / 390, places=6)
        self.assertIsNone(session_progress(datetime(2026, 11, 26, 8, 0), 'US'))

    def test_the_broker_sectors_exist_with_their_leading_names(self):
        themes = dict(THEME_UNIVERSE['KR'])
        self.assertIn('보안·사이버보안', themes)
        self.assertNotIn('AI 소프트웨어·보안', themes)
        security = dict(themes['보안·사이버보안'])
        self.assertEqual(security['053800.KQ'], '안랩')
        self.assertEqual(security['150900.KQ'], '파수AI')
        board = dict(themes['FC-BGA·PCB·기판'])
        self.assertEqual(board['323280.KQ'], '태성')
        self.assertEqual(board['090460.KS'], '비에이치')
        self.assertEqual(board['009150.KS'], '삼성전기')
