"""세부 테마·이슈 섹터 집계와 하락장 상대강세 포착, 그리고 메시지 계약 확인."""
import unittest
from datetime import datetime

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
        self.assertEqual(lines[0], '세부 테마 · 11/23 종가 · KOSPI -3.31% 대비 상대강도')
        self.assertEqual(lines[1], '1. 휴머노이드·로봇·액추에이터 +4.4% · 거래대금 2.9배 · 상승 5/8')
        self.assertEqual(lines[2], '   주도 휴림로봇 +30.0% · 로보티즈 +5.4%')
        self.assertEqual(lines[3], '   이슈: 로봇 공급 계약 체결')

    def test_intraday_block_says_so(self):
        self.assertIn('장중', format_theme_block(self.snapshot(intraday=True), 'KR')[0])

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
        self.assertEqual(sector_section([], None, 'KR'), ['오늘의 주요 섹터', '확인 불가'])

    def test_theme_block_replaces_the_coarse_word_list_under_the_same_heading(self):
        snapshot = dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23', intraday=False,
                        rows=[dict(name='휴머노이드·로봇', return_pct=4.4, up=5, total=8, turnover_ratio=2.9,
                                   state='주도', issue='이슈: 로봇 공급 계약')])
        lines = sector_section([], snapshot, 'KR')
        self.assertEqual(lines[0], '오늘의 주요 섹터')
        self.assertIn('휴머노이드·로봇', '\n'.join(lines[1:]))
        self.assertNotIn('확인 불가', lines)

    def test_official_industry_override_keeps_its_line_and_gains_the_detail(self):
        snapshot = dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23', intraday=False,
                        rows=[dict(name='휴머노이드·로봇', return_pct=4.4, up=5, total=8, turnover_ratio=2.9,
                                   state='주도', leaders=[])])
        lines = sector_section([], snapshot, 'KR', override=['오늘의 주요 섹터', '1위 은행'])
        self.assertEqual(lines[:2], ['오늘의 주요 섹터', '1위 은행'])
        self.assertIn('휴머노이드·로봇', '\n'.join(lines[2:]))


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
        rendered = {
            robots[0][0]: frame([100.0] * 21 + [130.0], [100.0] * 21 + [400.0]),
            robots[1][0]: frame([100.0] * 21 + [105.0]),
            robots[2][0]: frame([100.0] * 21 + [98.0]),
            '^KS11': frame([100.0] * 21 + [96.69]),
        }
        snapshot = theme_sector_snapshot('KR', NOW, downloader=lambda symbols, market: rendered,
                                         headlines=lambda *args: [])
        self.assertEqual(snapshot['benchmark_return_pct'], -3.31)
        self.assertEqual(snapshot['quoted'], 3)
        self.assertEqual(snapshot['rows'][0]['name'], '휴머노이드·로봇·액추에이터')
        self.assertGreater(snapshot['rows'][0]['market_excess_pct'], 6)
        self.assertIn('이슈: 뉴스 미확인', format_theme_block(snapshot, 'KR')[3])


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
    def test_live_message_keeps_the_sector_heading_first_and_shows_the_theme(self):
        from datetime import timezone
        from zoneinfo import ZoneInfo

        from kis_intraday import format_notification
        snapshot = dict(market='KR', benchmark_return_pct=-3.31, data_date='2026-11-23', intraday=False,
                        rows=[dict(name='휴머노이드·로봇·액추에이터', return_pct=4.4, up=5, total=8,
                                   turnover_ratio=2.9, state='주도', issue='이슈: 로봇 공급 계약 체결',
                                   leaders=[dict(symbol='090710.KQ', name='휴림로봇', return_pct=30.0)])])
        text = format_notification('KRX', [], datetime(2026, 11, 23, 9, 15, tzinfo=ZoneInfo('Asia/Seoul')),
                                   None, sectors=[], themes=snapshot)
        self.assertTrue(text.startswith('오늘의 주요 섹터'))
        self.assertIn('휴머노이드·로봇·액추에이터', text)
        self.assertIn('휴림로봇 +30.0%', text)
        self.assertLess(text.index('휴머노이드'), text.index('분석 결과'))


if __name__ == '__main__':
    unittest.main()
