from datetime import datetime, timezone, timedelta
import unittest
import pandas as pd
from sector_flow import intraday_sector_metrics, format_sector_header
from candidate_selection import rank_recovery_candidates, rank_market_candidates
from scanner import sector_metrics
from kis_intraday import format_notification, KST


class SectorAndRecoveryTests(unittest.TestCase):
    def test_relative_leaders_are_visible_below_absolute_threshold(self):
        rows=[dict(market='KR',name='반도체',early=False,active=False,starting=False,
                   source='daily_vs_20d',data_date='2026-11-17',turnover_ratio=1.1,
                   return_pct=1.,market_excess_pct=.5)]
        text='\n'.join(format_sector_header(rows,'KR'))
        self.assertIn('반도체',text)
        self.assertIn('상대 강세',text)
        self.assertIn('실시간 아님',text)
        self.assertNotIn('없음',text)

    def test_missing_sector_data_is_never_no_money_flow(self):
        text='\n'.join(format_sector_header([],'KR'))
        self.assertIn('데이터 확인 실패',text)
        self.assertNotIn('없음',text)

    def frames(self):
        starts=[stamp for day in pd.bdate_range('2026-11-09','2026-11-18')
                for stamp in pd.date_range(str(day.date())+' 09:00',periods=12,freq='5min',tz='Asia/Seoul')]
        data=pd.DataFrame(dict(Open=100.,Close=100.,Volume=100.),index=starts)
        benchmark=data.copy()
        data.loc[starts[-3:],['Open','Close','Volume']]=[[100,101,300],[101,102,300],[102,103,300]]
        benchmark.loc[starts[-3:],'Close']=100.2
        return data,benchmark

    def test_intraday_uses_same_time_baseline_and_detects_onset(self):
        data,benchmark=self.frames()
        now=datetime(2026,11,18,10,tzinfo=KST)
        row=intraday_sector_metrics(data,benchmark,now)
        self.assertAlmostEqual(row['turnover_ratio'],3.06)
        self.assertTrue(row['starting'])
        self.assertTrue(row['active'])
        self.assertEqual(row['data_as_of'],now.isoformat())

    def test_unfinished_and_future_bars_are_excluded(self):
        data,benchmark=self.frames()
        now=datetime(2026,11,18,10,tzinfo=KST)
        before=intraday_sector_metrics(data,benchmark,now)
        data.loc[pd.Timestamp(now),:]=[100,999,999999]
        self.assertEqual(intraday_sector_metrics(data,benchmark,now),before)

    def test_stale_intraday_data_is_not_reported_as_current(self):
        data,benchmark=self.frames()
        with self.assertRaises(ValueError):
            intraday_sector_metrics(data,benchmark,datetime(2026,11,18,12,tzinfo=KST))

    def test_daily_early_marker_precedes_old_two_times_threshold(self):
        idx=pd.bdate_range('2026-10-19',periods=23)
        data=pd.DataFrame({'Close':[100.]*22+[102.],'Adj Close':[100.]*22+[102.],
                           'Volume':[100.]*22+[150.]},index=idx)
        benchmark=data.assign(Close=100.,**{'Adj Close':100.})
        row=sector_metrics(data,benchmark)
        self.assertTrue(row['early'])
        self.assertTrue(row['starting'])
        self.assertFalse(row['active'])

    def test_sector_summary_is_first_in_live_message(self):
        rows=[dict(market='KR',name='반도체',symbol='091160.KS',early=True,active=True,starting=True,
                   source='daily_vs_20d',data_date='2026-11-17',turnover_ratio=2.3,
                   return_pct=1.,market_excess_pct=.5)]
        text=format_notification('KRX',[('005930',2.,.1,10000.,10020.,9819.6,(10500.,'과거 저항'))],
                                 datetime(2026,11,18,9,15,tzinfo=KST),sectors=rows)
        self.assertTrue(text.startswith('섹터 매수세'))
        self.assertLess(text.index('반도체'),text.index('매수 신호'))
        self.assertIn('대표 ETF',text)

    def test_recovery_does_not_require_already_positive_long_trend(self):
        item=dict(market='KR',symbol='TEST',name='일반기업',signal=dict(score=4),indicators=dict(
            price=82000.,ma20=85180.,ma60=102553.3333,volume=1411679.,volume_ratio=2.4186,
            return_5d=.0186,return_20d=-.2626,relative_strength=-.1572,
            obv_rising=True,breakout=False,breakdown=False,rebound=True,overheated=False))
        self.assertEqual(rank_market_candidates([item],'KR'),[])
        self.assertEqual(rank_recovery_candidates([item],'KR'),[item])


if __name__=='__main__':
    unittest.main()
