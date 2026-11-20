import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from evidence_refresh import refresh_report

AT=datetime(2026,11,20,4,tzinfo=timezone.utc)

class RefreshTests(unittest.TestCase):
    def test_expired_or_future_analysis_is_rejected_before_network(self):
        for age in [timedelta(minutes=31),timedelta(minutes=-1)]:
            report=dict(results=[dict(company_analysis=dict(checked_at=(AT-age).isoformat()))])
            with patch('evidence_refresh.world_events') as source:
                with self.assertRaises(ValueError):refresh_report(report,AT,{})
                source.assert_not_called()

    def test_news_refresh_does_not_launder_financial_timestamp(self):
        original=(AT-timedelta(minutes=10)).isoformat()
        row=dict(symbol='352820.KS',name='하이브',indicators=dict(price=100),
                 company_analysis=dict(checked_at=original,financial_source='DART 연결',quarter='2026-09'))
        with patch('evidence_refresh.world_events',return_value=([],[])), patch('evidence_refresh.industry_sector_evidence',return_value=[]), patch('evidence_refresh.issuer_news_result',return_value=dict(status='checked',articles=[],attempts=[])), patch('evidence_refresh.company_news_events',return_value=[]), patch('evidence_refresh.analyze_events',return_value={}), patch('evidence_refresh.build_company_forecast',return_value={}):
            refresh_report(dict(results=[row]),AT,{})
        self.assertEqual(row['company_analysis']['checked_at'],original)
        self.assertEqual(row['company_analysis']['news_checked_at'],AT.isoformat())
        self.assertEqual(row['company_analysis']['financial_source'],'DART 연결')

if __name__=='__main__':unittest.main()
