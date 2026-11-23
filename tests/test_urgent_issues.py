"""당일 급등 '긴급 이슈 참고' 블록: 게이트에 막힌 대형 이슈 급등만 별도로 보여 준다."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from urgent_issues import HEADING, format_urgent_block, select_urgent_issues

NOW = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)


def article(headline, at='2026-11-23T19:46:34+00:00', source='Yahoo Finance'):
    return dict(headline=headline, available_at=at, source=source, url='https://example.test/a')


def row(symbol, market='US', name='엔비디아', change=-0.14, price=125.53, previous=145.98,
        average=45_000_000_000, volume=626_000_000, selection=None, decision=None, **extra):
    indicators = dict(price=price, prev_close=previous, volume=volume, volume_ratio=2.4,
                      return_1d=change, data_date='2026-11-20', ma20=price * 1.2, ma60=price * 1.4,
                      return_5d=-0.05, return_20d=-0.1, relative_strength=-0.2,
                      breakdown=True, rebound=False, overheated=False, obv_rising=True)
    if average is not None:
        indicators['average_turnover_20d'] = average
    item = dict(market=market, symbol=symbol, name=name, asset_type='stock_or_etf',
                indicators=indicators, selection_filter=selection, preopen_decision=decision)
    item.update(extra)
    return item


def company(news=(), aliases=(), name='NVIDIA Corporation'):
    return dict(news=list(news), filings=[], entity_aliases=list(aliases), company_name=name)


def theme(market='US', leaders=(), issue='', data_date='2026-11-23', ratio=1.8):
    return dict(market=market, data_date=data_date, rows=[dict(
        market=market, name='AI 반도체·GPU', return_pct=15.0, up=9, total=9,
        turnover_ratio=ratio, state='주도', issue=issue,
        leaders=[dict(symbol=symbol, name=name, return_pct=change, alias=alias)
                 for symbol, name, change, alias in leaders])])


def report(rows=(), themes=(), regime=None, **extra):
    data = dict(results=list(rows), theme_sectors=list(themes), market_regime=regime or {})
    data.update(extra)
    return data


class UrgentIssueSelectionTests(unittest.TestCase):
    def nvda_case(self):
        nvda = row('NVDA', name='엔비디아', selection='breakdown_or_rebound', decision='breakdown',
                   company_analysis=company(
                       [article('Nvidia gains nearly $1T as Trump says nobody could revive the stock')],
                       aliases=['엔비디아']))
        snapshot = theme(leaders=[('NVDA', '엔비디아', 31.48, 'NVIDIA'),
                                  ('DELL', '델', 20.46, 'Dell')],
                         issue='이슈: US stocks surge, cheering return of Nvidia CEO and shrugging of…')
        return nvda, snapshot

    def test_ceo_return_case_is_captured_as_a_reference_not_a_buy(self):
        nvda, snapshot = self.nvda_case()
        data = report([nvda], [snapshot], {'US': {'above_ma20': False}})
        issues = select_urgent_issues(data, now=NOW)
        self.assertEqual([item['symbol'] for item in issues['US']], ['NVDA'])
        item = issues['US'][0]
        self.assertAlmostEqual(item['change_pct'], 31.5)
        self.assertIn('시장 역풍', item['reason_labels'])
        self.assertIn('이탈/반등 구간', item['reason_labels'])
        self.assertIn('CEO', item['headline'])
        # 참고 블록일 뿐 매수 계획을 만들지 않는다.
        self.assertNotIn('preopen_signal', nvda)
        self.assertNotIn('entry_limit', item)
        # 델은 테마 대표주지만 헤드라인이 델을 언급하지 않아 이슈 확인이 없다.
        self.assertNotIn('DELL', [item['symbol'] for item in issues['US']])

    def test_buy_candidates_are_not_repeated_in_the_reference_block(self):
        nvda, snapshot = self.nvda_case()
        nvda['preopen_signal'] = dict(status='conditional_buy', entry_limit=125.53)
        issues = select_urgent_issues(report([nvda], [snapshot]), now=NOW)
        self.assertNotIn('US', issues)

    def test_abnormal_price_errors_are_refused(self):
        para = row('PARA', name='파라마운트', change=-0.243, price=0.028, previous=0.037,
                   average=22_000_000, volume=193_000_000, selection='low_price')
        snapshot = theme(leaders=[('PARA', '파라마운트', 9614.29, 'Paramount'),
                                  ('RBLX', '로블록스', 10.55, 'Roblox')],
                         issue="이슈: Roblox Engagement Hasn't Been Materially Affected by GTA VI")
        numeric = row('BOOM', name='붐테크', change=96.14, price=12.0, previous=0.12,
                      average=50_000_000, volume=9_000_000, selection='insufficient_liquidity')
        issues = select_urgent_issues(report([para, numeric], [snapshot],
                                             {'US': {'above_ma20': False}}), now=NOW)
        self.assertEqual(issues, {})

    def test_korean_gain_above_the_price_limit_is_a_data_error(self):
        data = report([row('347770.KQ', market='KR', name='핌스', change=4.0, price=3795.0,
                           previous=759.0, average=12_000_000, volume=1000.0,
                           selection='insufficient_liquidity')],
                      [theme(market='KR', leaders=[('347770.KQ', '핌스', 400.0, None)],
                             issue='이슈: 핌스 급등')],
                      {'KR': {'above_ma20': False}})
        self.assertEqual(select_urgent_issues(data, now=NOW), {})

    def test_a_surge_without_news_is_not_raised(self):
        pump = row('PUMP', name='펌프테크', change=0.42, price=12.0, previous=8.45,
                   average=50_000_000, volume=9_000_000, selection='breakdown_or_rebound')
        unrelated = row('MEGA', name='메가테크', change=0.30, price=30.0, previous=23.0,
                        average=50_000_000, volume=4_000_000, selection='breakdown_or_rebound',
                        company_analysis=company([article('Freightos Limited Q3 earnings call summary')]))
        issues = select_urgent_issues(report([pump, unrelated], [],
                                             {'US': {'above_ma20': False}}), now=NOW)
        self.assertEqual(issues, {})

    def test_a_verified_news_headline_is_enough(self):
        explained = row('MEGA', name='메가테크', change=0.30, price=30.0, previous=23.0,
                        average=50_000_000, volume=4_000_000, selection='breakdown_or_rebound',
                        company_analysis=company(
                            [article('Mega Tech surges 30% after winning a $2B defense contract')],
                            aliases=['메가테크'], name='Mega Tech Inc'))
        issues = select_urgent_issues(report([explained], [], {'US': {'above_ma20': False}}), now=NOW)
        self.assertEqual([item['symbol'] for item in issues['US']], ['MEGA'])
        self.assertIn('Mega Tech surges', issues['US'][0]['headline'])

    def test_a_mention_without_a_surge_explanation_is_not_news(self):
        merely_mentioned = row('MENU', name='메뉴테크', change=0.30, price=30.0, previous=23.0,
                              average=50_000_000, volume=4_000_000, selection='breakdown_or_rebound',
                              company_analysis=company(
                                  [article('Is Menu Tech Stock a Buy at $30?')],
                                  aliases=['메뉴테크'], name='Menu Tech Inc'))
        issues = select_urgent_issues(report([merely_mentioned], [],
                                             {'US': {'above_ma20': False}}), now=NOW)
        self.assertEqual(issues, {})

    def test_at_most_three_per_market_largest_first(self):
        rows = []
        leaders = []
        for symbol, name, move in (('AAAT', '에이에이테크', 12.0), ('BBBT', '비비비테크', 18.0),
                                   ('CCCT', '씨씨씨테크', 25.0), ('DDDT', '디디디테크', 15.0)):
            rows.append(row(symbol, name=name, change=move / 100, price=30.0, previous=30.0 / (1 + move / 100),
                            average=50_000_000, volume=5_000_000, selection='breakdown_or_rebound',
                            company_analysis=company([article(f'{name} surges {move:.0f}% on a new contract')],
                                                      aliases=[name], name=name)))
            leaders.append((symbol, name, move, None))
        data = report(rows, [theme(leaders=leaders, issue='이슈: AI 반도체·GPU 강세')],
                      {'US': {'above_ma20': False}})
        items = select_urgent_issues(data, now=NOW)['US']
        self.assertEqual([item['symbol'] for item in items], ['CCCT', 'BBBT', 'DDDT'])

    def test_a_theme_snapshot_without_scan_rows_still_reports(self):
        # 장중 알림(kis_intraday)은 results 없이 테마 스냅샷만 넘긴다. 스캔 행이 없다고
        # 대표주 이슈가 사라지면 안 된다.
        snapshot = theme(leaders=[('NVDA', '엔비디아', 31.48, 'NVIDIA'),
                                  ('PARA', '파라마운트', 9614.29, 'Paramount')],
                         issue='이슈: US stocks surge, cheering return of Nvidia CEO and shrugging of…')
        issues = select_urgent_issues(dict(theme_sectors=[snapshot]), now=NOW)
        self.assertEqual([item['symbol'] for item in issues['US']], ['NVDA'])
        self.assertEqual(issues['US'][0]['reason_labels'], ['매수 후보 미포함'])
        self.assertAlmostEqual(issues['US'][0]['change_pct'], 31.5)

    def test_the_block_states_it_is_not_a_buy_signal(self):
        nvda, snapshot = self.nvda_case()
        issues = select_urgent_issues(report([nvda], [snapshot], {'US': {'above_ma20': False}}), now=NOW)
        lines = format_urgent_block(issues)
        self.assertEqual(lines[0], HEADING)
        self.assertIn('매수 신호 아님', lines[0])
        text = '\n'.join(lines)
        self.assertIn('엔비디아 (NVDA) +31.5%', text)
        self.assertIn('보류: 시장 역풍', text)
        self.assertIn('이슈: US stocks surge', text)
        self.assertEqual(format_urgent_block({}), [])

    def test_buy_evaluation_counts_and_signals_are_untouched(self):
        nvda, snapshot = self.nvda_case()
        evaluation = dict(counts={'breakdown_or_rebound': 1}, recommendations=[],
                          decision_completed=1, completed=False)
        data = report([nvda], [snapshot], {'US': {'above_ma20': False}}, buy_evaluation=evaluation)
        select_urgent_issues(data, now=NOW)
        self.assertEqual(data['buy_evaluation'], evaluation)
        self.assertNotIn('preopen_signal', nvda)
        self.assertIsNone(nvda.get('urgent_issue'))

    def test_missing_report_sections_do_not_raise(self):
        self.assertEqual(select_urgent_issues({}, now=NOW), {})
        self.assertEqual(select_urgent_issues(dict(results=[], theme_sectors=[None]), now=NOW), {})


class DailyMessageIntegrationTests(unittest.TestCase):
    def test_daily_summary_appends_the_reference_block_after_the_buy_section(self):
        report_data = report([], [theme(market='KR', leaders=[('090710.KQ', '휴림로봇', 30.0, None)],
                                        issue="이슈: 휴림로봇 주가 '불기둥'...한화로보틱스와 로봇 공급 계약")],
                             {'KR': {'above_ma20': False}, 'US': {'above_ma20': False}},
                             buy_evaluation=dict(completed=True, decision_completed=3, data_excluded=0,
                                                 counts={'market_headwind': 3}))
        report_data['results'] = [row('090710.KQ', market='KR', name='휴림로봇', change=0.30,
                                      price=5720.0, previous=4400.0, average=3_700_000_000,
                                      volume=10_323_666.0, selection='insufficient_liquidity')]
        import scanner
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake'}), \
                patch.object(scanner.requests, 'post') as post, \
                patch('scanner.delivery_status', return_value=(True, 'test')):
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            scanner.send_telegram_summary(report_data)
        message = post.call_args.kwargs['json']['text']
        self.assertIn(HEADING, message)
        self.assertIn('휴림로봇 (090710) +30.0%', message)
        self.assertIn('시장 역풍', message)
        self.assertLess(message.index('분석 결과'), message.index(HEADING))
        self.assertEqual(report_data['urgent_issues']['KR'][0]['symbol'], '090710.KQ')


if __name__ == '__main__':
    unittest.main()
