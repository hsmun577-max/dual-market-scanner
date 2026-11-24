import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, Mock
import scanner
from telegram_text import split_telegram_text


class DailyDeliveryTests(unittest.TestCase):
    def test_daily_selection_excludes_partial_current_session_and_keeps_previous(self):
        import pandas as pd
        from datetime import datetime, timezone
        frame = pd.DataFrame({'Close':[4200,4505]}, index=pd.to_datetime(['2026-11-18','2026-11-19']))
        now = datetime(2026,11,19,1,56,tzinfo=timezone.utc)
        selected = scanner.completed_daily_prices(frame,'KR',now)
        self.assertEqual(list(selected.Close),[4200])
        self.assertEqual(len(frame),2)

    def test_daily_selection_includes_us_close_after_midnight_utc(self):
        import pandas as pd
        from datetime import datetime, timezone
        frame = pd.DataFrame({'Close':[1,2]},index=pd.to_datetime(['2026-11-17','2026-11-18']))
        now = datetime(2026,11,19,1,0,tzinfo=timezone.utc)
        self.assertEqual(list(scanner.completed_daily_prices(frame,'US',now).Close),[1,2])

    def test_actual_yahoo_field_names_and_pretty_aliases(self):
        import pandas as pd
        from datetime import date
        from kis_intraday import company_financials
        columns=pd.to_datetime(['2026-09-30','2025-09-30'])
        raw=pd.DataFrame([[1200,1000],[180,100]],index=['TotalRevenue','OperatingIncome'],columns=columns)
        actual=company_financials(raw,date(2026,11,18))
        pretty=company_financials(raw.rename(index={'TotalRevenue':'Total Revenue','OperatingIncome':'Operating Income'}),date(2026,11,18))
        self.assertEqual(actual,pretty)
        self.assertEqual(actual['operating_margin_pct'],15)
        self.assertAlmostEqual(actual['revenue_yoy_pct'],20)

    def row(self):
        return dict(market='KR',symbol='005930.KS',name='삼성전자',asset_type='stock_or_etf',
            signal=dict(score=6,stage='장중 조건부 진입 후보',reasons=['거래량 증가']),
            indicators=dict(price=10000.,volume=2000000.,volume_ratio=2.,ma20=9500.,ma60=9000.,
                return_5d=.05,return_20d=.1,relative_strength=.05,obv_rising=True,breakout=True,
                breakdown=False,rebound=False,overheated=False,data_date=scanner.last_completed_session('KR', datetime.now(timezone.utc)).isoformat()),
            fundamentals='미검증',news_and_filings='미검증')

    def test_actual_daily_candidates_are_enriched_before_sending(self):
        row=self.row()
        report=dict(results=[row],sectors=[dict(market='KR',name='반도체',symbol='091160.KS',
                    early=True,starting=True,active=True,turnover_ratio=2.4,return_pct=1.,
                    market_excess_pct=.5,data_date='2026-11-17')],successful=1,attempted=1)
        snapshot=dict(quarter='2026-09',operating_margin_pct=12.,revenue_yoy_pct=20.,
            financial_source='DART 연결',checked_at='2026-11-18T10:00:00+09:00',
            filings=[dict(filing_date='2026-11-16',form='분기보고서')],filing_status='checked',
            news=[dict(headline='삼성전자 실적 증가',source='공식자료')])
        with patch('kis_intraday.company_context',return_value=({'005930':snapshot},[])) as context, patch('event_impact_analysis.world_events',return_value=([],[])), patch('economic_graph.load_profiles',return_value={}):
            scanner.prepare_selected_analysis(report)
        context.assert_called_once()
        self.assertEqual(row['fundamentals'],'영업흑자')
        self.assertEqual(row['company_analysis']['operating_margin_pct'],12.)
        self.assertEqual(row['company_analysis']['revenue_yoy_pct'],20.)
        # Financial statements, filings and price evidence are present: valuation and cash
        # flow gaps are caveats, so the row keeps a score and the daily alert carries the
        # conditional plan instead of ending as "analysis incomplete".
        self.assertIsNotNone(row['company_forecast']['composite_score'])
        self.assertEqual(row['company_forecast']['assessment_status'],'partial')
        self.assertIsNotNone(row.get('preopen_signal'))
        self.assertEqual(row['preopen_signal']['status'],'conditional_buy')
        text=scanner.telegram_candidate_line(row)
        self.assertIn('조건부 매수',text)
        self.assertIn('자료 일부 미확보',text)
        self.assertNotIn('미검증',text)
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'fake'}), patch.object(scanner.requests,'post') as post, patch('scanner.delivery_status',return_value=(True,'test')):
            post.return_value=Mock(status_code=200,json=lambda:{'ok':True})
            scanner.send_telegram_summary(report)
        messages=[call.kwargs['json']['text'] for call in post.call_args_list]
        self.assertTrue(messages[0].startswith('오늘의 주요 섹터'))
        self.assertNotIn('DART 연결','\n'.join(messages))
        self.assertIn('조건부 매수 계획','\n'.join(messages))
        self.assertIn('삼성전자','\n'.join(messages))
        self.assertTrue(all(len(message)<=3800 for message in messages))

    def test_a_row_without_financial_statements_is_still_refused_and_reported(self):
        row=self.row()
        report=dict(results=[row],sectors=[],successful=1,attempted=1)
        snapshot=dict(quarter='2026-09',checked_at='2026-11-18T10:00:00+09:00',
            filing_status='checked',news_status='no_recent_articles')
        with patch('kis_intraday.company_context',return_value=({'005930':snapshot},[])), \
             patch('event_impact_analysis.world_events',return_value=([],[])), \
             patch('economic_graph.load_profiles',return_value={}):
            scanner.prepare_selected_analysis(report)
        self.assertIsNone(row['company_forecast']['composite_score'])
        self.assertEqual(row['company_forecast']['assessment_status'],'incomplete')
        self.assertEqual(scanner.telegram_candidate_line(row),'')
        self.assertFalse(report['buy_evaluation']['completed'])
        self.assertGreater(report['buy_evaluation']['data_excluded'],0)
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'fake'}), patch.object(scanner.requests,'post') as post, patch('scanner.delivery_status',return_value=(True,'test')):
            post.return_value=Mock(status_code=200,json=lambda:{'ok':True})
            scanner.send_telegram_summary(report)
        message=post.call_args.kwargs['json']['text']
        self.assertIn('분석 미완료',message)
        self.assertNotIn('삼성전자',''.join(message.split('\n')[1:]))

    def test_long_reports_are_split_and_failures_are_not_relabelled_verified(self):
        self.assertTrue(all(len(part)<=3800 for part in split_telegram_text('가'*9000)))
        row=self.row()
        row['company_analysis']={'filing_status':'missing_dart_key','news_status':'unavailable'}
        text=scanner.telegram_candidate_line(row)
        self.assertEqual(text,'')
        self.assertEqual(row['company_analysis']['filing_status'],'missing_dart_key')
        self.assertNotIn('분석 완료',text)

    def test_scheduled_report_separates_completed_decisions_from_missing_sources(self):
        report=dict(results=[],sectors=[],buy_evaluation=dict(completed=False,decision_completed=18,data_excluded=26))
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'fake'}), patch.object(scanner.requests,'post') as post, patch('scanner.delivery_status',return_value=(True,'test')):
            post.return_value=Mock(status_code=200,json=lambda:{'ok':True})
            scanner.send_telegram_summary(report)
        message=post.call_args.kwargs['json']['text']
        self.assertIn('판정 완료 18종목',message)
        self.assertIn('자료 부족 26종목',message)
        self.assertNotIn('매수 판정 보류',message)


if __name__=='__main__':
    unittest.main()
