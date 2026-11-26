"""2026-11-26 지시: 섹터 순위를 등락률이 아니라 거래대금 유입으로, 종목은 여러 테마에 중복으로.

증권사 화면(`지금 섹터는?`)의 두 기준을 그대로 옮겼는지 확인한다.

1. 순위: 누적 거래대금 규모 + 평소 대비 급증(장중은 경과시간 보정 페이스)이 핵심이고
   등락률·확산도는 보조다. 하락만 하는 투매 섹터는 뒤로 간다.
2. 묶음: 한 종목이 여러 테마에 속하고, 고정 바스켓 밖 종목은 헤드라인 이슈 라벨로 묶인다.
"""
import unittest
from datetime import datetime

import theme_sectors
from theme_sectors import (
    INTRADAY_LEADERSHIP_WEIGHTS,
    LEADERSHIP_WEIGHTS,
    dynamic_theme_clusters,
    format_theme_block,
    headline_labels,
    outside_basket_quotes,
    rank_theme_sectors,
    theme_membership,
    theme_rows,
    theme_sector_snapshot,
)

NOW = datetime(2026, 11, 26, 10, 45)


def metric(move, turnover_ratio=1.0, turnover=100.0, data_date='2026-11-25', name=None):
    row = dict(return_pct=move, turnover_ratio=turnover_ratio, turnover=turnover,
               data_date=data_date, intraday=False, volume_ratio=1.0)
    if name:
        row['name'] = name
    return row


CHIP = ("HBM·메모리", (("A", "알파전자"), ("B", "베타전자")))
ROBOT = ("휴머노이드·로봇", (("C", "감마로봇"), ("D", "델타로봇")))
DUMP = ("투매테마", (("E", "엡실론"), ("F", "제타")))
RISE = ("상승테마", (("G", "에타"), ("H", "세타")))


class TurnoverFirstRankingTests(unittest.TestCase):
    def test_money_inflow_beats_a_bigger_price_move(self):
        # 등락률은 로봇(+9%)이 훨씬 크지만, 거래대금이 10배 몰린 HBM이 순위 1위다.
        metrics = {'A': metric(1.0, 3.0, 600.0), 'B': metric(1.0, 3.0, 400.0),
                   'C': metric(9.0, 1.0, 60.0), 'D': metric(9.0, 1.0, 40.0)}
        ranked = rank_theme_sectors(theme_rows('KR', (CHIP, ROBOT), metrics, -1.0))
        self.assertEqual([row['name'] for row in ranked], ['HBM·메모리', '휴머노이드·로봇'])
        self.assertGreater(ranked[0]['inflow_score'], ranked[1]['inflow_score'])
        self.assertGreater(ranked[1]['return_pct'], ranked[0]['return_pct'])

    def test_the_core_weights_are_turnover_inflow_not_the_price_move(self):
        self.assertGreaterEqual(LEADERSHIP_WEIGHTS['turnover_ratio'], 0.40)
        self.assertGreaterEqual(INTRADAY_LEADERSHIP_WEIGHTS['turnover_pace_ratio'], 0.40)
        self.assertEqual(LEADERSHIP_WEIGHTS['turnover_ratio'] + LEADERSHIP_WEIGHTS['turnover'],
                         sum(weight for key, weight in LEADERSHIP_WEIGHTS.items()
                             if key in ('turnover', 'turnover_ratio')))
        # 등락률·확산도는 핵심 비중보다 작은 보조다.
        self.assertLess(LEADERSHIP_WEIGHTS['return_pct'] + LEADERSHIP_WEIGHTS['breadth_pct'],
                        LEADERSHIP_WEIGHTS['turnover_ratio'] + LEADERSHIP_WEIGHTS['turnover'])

    def test_intraday_ranks_on_the_turnover_pace(self):
        rows = theme_rows('KR', (CHIP, ROBOT),
                          {'A': metric(1.0, 1.0, 500.0), 'B': metric(1.0, 1.0, 500.0),
                           'C': metric(5.0, 1.0, 100.0), 'D': metric(5.0, 1.0, 100.0)}, 0.0,
                          session_progress=0.5)
        chip = next(row for row in rows if row['name'] == 'HBM·메모리')
        robot = next(row for row in rows if row['name'] == '휴머노이드·로봇')
        self.assertAlmostEqual(chip['turnover_pace_ratio'], 2.0)
        self.assertAlmostEqual(robot['turnover_pace_ratio'], 2.0)
        # 로봇만 장중 페이스가 6배로 치솟은 경우(당일 유입 속도).
        robot['turnover_pace_ratio'] = 6.0
        ranked = rank_theme_sectors([chip, robot], weights=INTRADAY_LEADERSHIP_WEIGHTS)
        self.assertEqual(ranked[0]['name'], '휴머노이드·로봇')

    def test_a_dumping_theme_goes_last_even_with_the_biggest_turnover(self):
        metrics = {'E': metric(-6.0, 5.0, 1000.0), 'F': metric(-5.0, 5.0, 1000.0),
                   'G': metric(1.0, 1.2, 100.0), 'H': metric(1.0, 1.2, 100.0)}
        rows = theme_rows('KR', (DUMP, RISE), metrics, 0.0)
        dumped = next(row for row in rows if row['name'] == '투매테마')
        self.assertTrue(dumped['dumping'])
        self.assertGreater(dumped['turnover_ratio'], 4.0)
        ranked = rank_theme_sectors(rows)
        self.assertEqual([row['name'] for row in ranked], ['상승테마', '투매테마'])

    def test_a_broad_down_day_still_ranks_by_inflow_inside_the_selloff(self):
        # 지수가 -4%면 후보 전부가 투매일 수 있다. 그때도 순서는 유입대로 남는다.
        metrics = {'E': metric(-4.0, 1.0, 100.0), 'F': metric(-4.0, 1.0, 100.0),
                   'G': metric(-4.0, 3.0, 900.0), 'H': metric(-4.0, 3.0, 900.0)}
        ranked = rank_theme_sectors(theme_rows('KR', (DUMP, RISE), metrics, -4.0))
        self.assertTrue(all(row['dumping'] for row in ranked))
        self.assertEqual(ranked[0]['name'], '상승테마')  # 거래대금이 더 몰린 쪽


class MessageOrderTests(unittest.TestCase):
    def row(self, **changes):
        row = dict(name='보안·사이버보안', return_pct=9.2, up=3, total=4, turnover_ratio=4.4,
                   turnover=5350e8, turnover_pace_ratio=4.4, inflow_multiple=4.4,
                   state='주도', dumping=False, symbols=['053800.KQ'],
                   leaders=[dict(symbol='053800.KQ', name='안랩', return_pct=12.0)], issue=None)
        row.update(changes)
        return row

    def test_line_puts_turnover_size_and_multiple_before_the_move(self):
        lines = format_theme_block(dict(market='KR', data_date='2026-11-26', intraday=True,
                                        checked_at=NOW.isoformat(), rows=[self.row()]), 'KR')
        self.assertEqual(lines[1], '1. 보안·사이버보안 거래대금 5,350억(4.4배) · +9.2% · 상승 3/4')

    def test_preopen_line_uses_the_previous_close_multiple_with_the_same_order(self):
        row = self.row(intraday=False, turnover_pace_ratio=None, inflow_multiple=4.4)
        lines = format_theme_block(dict(market='KR', data_date='2026-11-25', intraday=False,
                                        checked_at=NOW.isoformat(), rows=[row]), 'KR')
        self.assertEqual(lines[1], '1. 보안·사이버보안 거래대금 5,350억(4.4배) · +9.2% · 상승 3/4')

    def test_a_dumping_theme_says_so(self):
        lines = format_theme_block(dict(market='KR', data_date='2026-11-25', intraday=False,
                                        checked_at=NOW.isoformat(),
                                        rows=[self.row(dumping=True, state='약세', return_pct=-5.0)]), 'KR')
        self.assertIn('투매', lines[1])
        self.assertNotIn('지수 대비 강세', lines[1])


class SharedMembershipTests(unittest.TestCase):
    def test_a_stock_can_belong_to_two_themes_and_is_counted_once_in_each(self):
        themes = (("기판", (("S", "공유전자"), ("A", "에이"))),
                  ("AI부품", (("S", "공유전자"), ("B", "비"))))
        rows = theme_rows('KR', themes,
                          {'S': metric(2.0, 1.0, 100.0), 'A': metric(4.0, 1.0, 50.0),
                           'B': metric(0.0, 1.0, 50.0)}, 0.0)
        self.assertEqual([row['name'] for row in rows], ['기판', 'AI부품'])
        self.assertEqual(rows[0]['symbols'], ['S', 'A'])
        self.assertEqual(rows[1]['symbols'], ['S', 'B'])
        self.assertAlmostEqual(rows[0]['turnover'], 150.0)
        self.assertAlmostEqual(rows[1]['turnover'], 150.0)

    def test_the_korean_basket_shares_the_board_and_ai_server_members(self):
        membership = theme_membership('KR')
        self.assertTrue({'FC-BGA·PCB·기판', 'AI서버·데이터센터 부품'} <= set(membership['009150.KS']))
        self.assertIn('HBM·메모리 반도체', membership['042700.KS'])
        self.assertIn('AI서버·데이터센터 부품', membership['042700.KS'])

    def test_the_snapshot_downloads_each_shared_symbol_once(self):
        seen = {}

        def downloader(symbols, market):
            seen['symbols'] = list(symbols)
            return {}

        theme_sector_snapshot('KR', NOW, downloader=downloader)
        self.assertEqual(len(seen['symbols']), len(set(seen['symbols'])))
        self.assertGreater(theme_sector_snapshot('KR', datetime(2026, 11, 26, 6, 30),
                                                 downloader=downloader)['shared_members'], 0)


class DynamicClusterTests(unittest.TestCase):
    def quotes(self):
        # 바스켓에 없는 종목(CJ·DL)과 바스켓 종목(안랩)이 같은 재료로 움직인다.
        return {
            '001040.KS': dict(name='CJ', return_pct=4.0, turnover=900.0, data_date='2026-11-25'),
            '000210.KS': dict(name='DL', return_pct=2.0, turnover=700.0, data_date='2026-11-25'),
            '053800.KQ': dict(name='안랩', return_pct=9.0, turnover=300.0, data_date='2026-11-25'),
        }

    def fetch(self, headlines):
        def handler(market, symbol, name, now):
            return [dict(headline=headlines.get(symbol, ''), available_at='')] if headlines.get(symbol) else []
        return handler

    def test_two_stocks_with_the_same_headline_keyword_become_one_cluster(self):
        headlines = {'001040.KS': 'CJ, 전고체 배터리 시제품 공개',
                     '000210.KS': 'DL 전기차 배터리 공급 계약',
                     '053800.KQ': '안랩 랜섬웨어 대응 솔루션 출시'}
        clusters = dynamic_theme_clusters(self.quotes(), 'KR', NOW, self.fetch(headlines))
        # 보안 키워드는 안랩 한 종목뿐이라 클러스터가 아니다(최소 2종목).
        self.assertEqual([cluster['label'] for cluster in clusters], ['2차전지·전고체'])
        battery = clusters[0]
        self.assertEqual(battery['symbols'], ['000210.KS', '001040.KS'])
        self.assertEqual(battery['outside_basket'], 2)
        self.assertEqual(battery['themes'], [])
        self.assertAlmostEqual(battery['return_pct'], 3.0)
        self.assertAlmostEqual(battery['turnover'], 1600.0)

    def test_one_stock_with_a_keyword_is_not_a_cluster(self):
        clusters = dynamic_theme_clusters(self.quotes(), 'KR', NOW,
                                          self.fetch({'001040.KS': 'CJ 수소차 공개'}))
        self.assertEqual(clusters, [])

    def test_the_same_input_gives_the_same_clusters(self):
        headlines = {'A': 'HBM 증설', 'B': 'HBM 수요 급증', 'C': 'HBM 공급 확대'}
        quotes = {'A': dict(name='갑', return_pct=1.0, turnover=10.0),
                  'B': dict(name='을', return_pct=2.0, turnover=20.0),
                  'C': dict(name='병', return_pct=3.0, turnover=30.0)}
        first = dynamic_theme_clusters(quotes, 'KR', NOW, self.fetch(headlines))
        second = dynamic_theme_clusters(dict(reversed(list(quotes.items()))), 'KR', NOW,
                                        self.fetch(headlines))
        self.assertEqual(first, second)
        self.assertEqual([cluster['label'] for cluster in first], ['HBM·메모리'])

    def test_a_news_provider_failure_only_drops_the_cluster(self):
        def broken(*args):
            raise TimeoutError('provider down')

        self.assertEqual(dynamic_theme_clusters(self.quotes(), 'KR', NOW, broken), [])

    def test_headline_labels_read_only_the_listed_words(self):
        self.assertEqual(headline_labels('삼성전기, FC-BGA 기판 증설'), ['기판·FC-BGA'])
        self.assertEqual(headline_labels('SK하이닉스 hbm4 양산'), ['HBM·메모리'])
        self.assertEqual(headline_labels('CJ 수소차 공개'), [])
        self.assertEqual(headline_labels(None), [])


class DynamicClusterMessageTests(unittest.TestCase):
    def snapshot(self, clusters, symbols=('053800.KQ', '150900.KQ')):
        return dict(market='KR', data_date='2026-11-25', intraday=False, checked_at=NOW.isoformat(),
                    rows=[dict(name='보안·사이버보안', return_pct=9.2, up=2, total=2,
                               turnover_ratio=4.4, turnover=5350e8, inflow_multiple=4.4,
                               state='주도', dumping=False, symbols=list(symbols),
                               leaders=[dict(symbol='053800.KQ', name='안랩', return_pct=12.0)])],
                    dynamic_clusters=clusters)

    def test_a_cluster_that_spans_the_basket_gets_one_line(self):
        cluster = dict(label='AI서버·부품', symbols=['001040.KS', '007660.KS'],
                       members=[dict(symbol='001040.KS', name='CJ', return_pct=5.0),
                                dict(symbol='007660.KS', name='이수페타시스', return_pct=7.0)],
                       turnover=2.6e12, outside_basket=1)
        lines = format_theme_block(self.snapshot([cluster]), 'KR')
        self.assertEqual(lines[-1], '동적 이슈: AI서버·부품 — CJ +5.0% · 이수페타시스 +7.0% · 거래대금 26,000억')

    def test_a_cluster_inside_one_shown_theme_is_not_repeated(self):
        cluster = dict(label='보안', symbols=['053800.KQ', '150900.KQ'],
                       members=[dict(symbol='053800.KQ', name='안랩', return_pct=12.0),
                                dict(symbol='150900.KQ', name='파수AI', return_pct=6.0)],
                       turnover=5350e8, outside_basket=0)
        lines = format_theme_block(self.snapshot([cluster]), 'KR')
        self.assertFalse(any('동적 이슈' in line for line in lines))


class OutsideBasketQuoteTests(unittest.TestCase):
    def results(self):
        base = dict(market='KR', asset_type='stock_or_etf', data_date='2026-11-25')
        return [
            dict(base, symbol='005930.KS', name='삼성전자', indicators=dict(
                price=70000.0, volume=100000.0, average_turnover_20d=5.6e9, return_1d=0.02,
                data_date='2026-11-25')),
            dict(base, symbol='001040.KS', name='CJ', indicators=dict(
                price=200000.0, volume=50000.0, average_turnover_20d=5.0e9, return_1d=-0.01,
                data_date='2026-11-25')),
            dict(base, symbol='069500.KS', name='KODEX 200', asset_type='etf', indicators=dict(
                price=30000.0, volume=900000.0, average_turnover_20d=1.0e9, return_1d=0.01,
                data_date='2026-11-25')),
            dict(base, symbol='000660.KS', name='SK하이닉스', indicators=dict(
                price=200000.0, volume=10000.0, return_1d=0.03, data_date='2026-11-25')),
        ]

    def test_only_stocks_outside_the_basket_with_a_full_quote_are_kept(self):
        quotes = outside_basket_quotes(self.results(), 'KR')
        self.assertEqual([quote['symbol'] for quote in quotes], ['001040.KS'])
        self.assertAlmostEqual(quotes[0]['turnover'], 1.0e10)
        self.assertAlmostEqual(quotes[0]['turnover_ratio'], 2.0)
        self.assertAlmostEqual(quotes[0]['return_pct'], -1.0)
        self.assertTrue(quotes[0]['outside_basket'])


class KisTurnoverQuoteTests(unittest.TestCase):
    """장중 알림은 KIS 당일 거래대금 순위를 바스켓 밖 종목으로 쓴다(테마 순위에는 안 끼인다)."""

    def test_rank_rows_outside_the_basket_become_cluster_quotes(self):
        from kis_intraday import INTRADAY_RANK_QUOTES, turnover_quote_rows, turnover_quotes
        rows = [
            dict(mksc_shrn_iscd='001040', hts_kor_isnm='CJ',
                 acml_tr_pbmn='900000000000', prdy_ctrt='4.5'),
            # 테마 바스켓 종목: 테마 지표로 이미 들어가므로 뺐다.
            dict(mksc_shrn_iscd='005930', hts_kor_isnm='삼성전자',
                 acml_tr_pbmn='950000000000', prdy_ctrt='1.0'),
            # ETF는 클러스터 대상이 아니다.
            dict(mksc_shrn_iscd='122630', hts_kor_isnm='KODEX 레버리지',
                 acml_tr_pbmn='800000000000', prdy_ctrt='0.5'),
            # 거래대금이 없는 행은 버린다.
            dict(mksc_shrn_iscd='000210', hts_kor_isnm='DL',
                 acml_tr_pbmn='0', prdy_ctrt='1.0'),
        ]
        quotes = turnover_quote_rows(rows, 'KRX')
        self.assertEqual([quote['symbol'] for quote in quotes], ['001040'])
        self.assertAlmostEqual(quotes[0]['return_pct'], 4.5)
        self.assertAlmostEqual(quotes[0]['turnover'], 9.0e11)
        self.assertTrue(quotes[0]['outside_basket'])
        INTRADAY_RANK_QUOTES['KRX'] = quotes
        self.assertEqual(turnover_quotes('KRX'), quotes)
        self.assertEqual(turnover_quotes('NXT'), [])
        INTRADAY_RANK_QUOTES.clear()


class SnapshotClusterIntegrationTests(unittest.TestCase):
    def frame(self, closes, volumes=None):
        import pandas as pd
        index = pd.bdate_range(end='2026-11-25', periods=len(closes))
        return pd.DataFrame({'Close': closes, 'Volume': volumes or [100.0] * len(closes)},
                            index=index)

    def test_extra_quotes_feed_the_clusters_without_touching_theme_ranking(self):
        robots = dict(theme_sectors.THEME_UNIVERSE['KR'])['휴머노이드·로봇·액추에이터']
        rendered = {robots[0][0]: self.frame([50000.0] * 21 + [62000.0]),
                    robots[1][0]: self.frame([50000.0] * 21 + [51000.0]),
                    '^KS11': self.frame([2500.0] * 21 + [2475.0])}
        headlines = {'001040.KS': 'CJ 로봇 라인 증설',
                     '000210.KS': 'DL 로봇 부품 공급'}
        extra = [dict(symbol='001040.KS', name='CJ', return_pct=3.0, turnover=900e8,
                      turnover_ratio=2.0, data_date='2026-11-25', outside_basket=True),
                 dict(symbol='000210.KS', name='DL', return_pct=1.0, turnover=700e8,
                      turnover_ratio=1.5, data_date='2026-11-25', outside_basket=True)]
        snapshot = theme_sector_snapshot(
            'KR', datetime(2026, 11, 26, 6, 30), mode='previous',
            downloader=lambda symbols, market: rendered,
            headlines=lambda market, symbol, name, now: (
                [dict(headline=headlines[symbol], available_at='')] if symbol in headlines else []),
            extra_quotes=extra)
        self.assertEqual(snapshot['outside_quotes'], 2)
        self.assertEqual([row['name'] for row in snapshot['rows']], ['휴머노이드·로봇·액추에이터'])
        self.assertEqual(len(snapshot['rows']), 1)  # 바스켓 밖 종목은 테마 순위에 끼지 않는다
        self.assertEqual([cluster['label'] for cluster in snapshot['dynamic_clusters']], ['로봇'])


if __name__ == '__main__':
    unittest.main()
