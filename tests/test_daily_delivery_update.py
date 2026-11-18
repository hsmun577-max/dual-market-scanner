import os
import unittest
from unittest.mock import patch, Mock
import scanner
from telegram_text import split_telegram_text


class DailyDeliveryTests(unittest.TestCase):
    def row(self):
        return dict(market='KR',symbol='005930.KS',name='삼성전자',asset_type='stock_or_etf',
            signal=dict(score=6,stage='장중 조건부 진입 후보',reasons=['거래량 증가']),
            indicators=dict(price=10000.,volume=2000000.,volume_ratio=2.,ma20=9500.,ma60=9000.,
                return_5d=.05,return_20d=.1,relative_strength=.05,obv_rising=True,breakout=True,
                breakdown=False,rebound=False,overheated=False,data_date='2026-11-17'),
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
        with patch('kis_intraday.company_context',return_value=({'005930':snapshot},[])) as context:
            scanner.prepare_selected_analysis(report)
        context.assert_called_once()
        self.assertEqual(row['fundamentals'],'영업흑자')
        text=scanner.telegram_candidate_line(row)
        self.assertIn('영업이익률 +12.0%',text)
        self.assertIn('매출 전년 대비 +20.0%',text)
        self.assertIn('DART 연결',text)
        self.assertNotIn('미검증',text)
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'fake','TELEGRAM_CHAT_ID':'fake'}), patch.object(scanner.requests,'post') as post:
            post.return_value=Mock(status_code=200,json=lambda:{'ok':True})
            scanner.send_telegram_summary(report)
        messages=[call.kwargs['json']['text'] for call in post.call_args_list]
        self.assertTrue(messages[0].startswith('섹터 매수세'))
        self.assertIn('DART 연결','\n'.join(messages))
        self.assertTrue(all(len(message)<=3800 for message in messages))

    def test_long_reports_are_split_and_failures_are_not_relabelled_verified(self):
        self.assertTrue(all(len(part)<=3800 for part in split_telegram_text('가'*9000)))
        row=self.row()
        row['company_analysis']={'filing_status':'missing_dart_key','news_status':'unavailable'}
        text=scanner.telegram_candidate_line(row)
        self.assertIn('DART API 키 미설정',text)
        self.assertIn('재무자료 조회 실패',text)
        self.assertNotIn('분석 완료',text)


if __name__=='__main__':
    unittest.main()
