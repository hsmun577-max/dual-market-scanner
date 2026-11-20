from copy import deepcopy
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import unittest
from industry_flow import rank_leading_sectors,industry_sector_evidence
from company_forecast import build_company_forecast
from event_impact_analysis import analyze_events
from kis_intraday import dart_quarterly_financials
from research_asset import is_fund

AT=datetime(2026,11,20,11,20,tzinfo=timezone(timedelta(hours=9)))

class EvidenceRepairTests(unittest.TestCase):
    def company(self):
        return dict(market='KR',source_symbol='000010.KS',domestic_industry_name='화학',sector='Consumer Defensive',
            quarter='2026-09',operating_margin_pct=10,revenue_yoy_pct=20,operating_margin_yoy_pp=2,
            operating_cash_flow_margin_pct=5,net_debt_to_equity=.2,trailing_pe=20,forward_pe=15,
            filing_status='checked',news_status='no_recent_articles',event_analysis=dict(generated_at=AT.isoformat(),
                status='no_material_events_identified',feed_coverage=[dict(status='checked')],unread_events=[]))

    def sector(self):
        return dict(name='화학',exchange='KSP',business_date='20261120',observed_at=AT.isoformat(),
            combined_net=100,return_pct=-1,market_excess_pct=3,turnover_share_pct=10,advances=10,declines=30)

    def test_bear_market_ranking_does_not_erase_observed_sectors(self):
        a=self.sector();b=dict(a,name='음식료·담배',combined_net=200,return_pct=.4,market_excess_pct=4.4)
        ranked=rank_leading_sectors(dict(rows=[a,b]))
        self.assertEqual(len(ranked),2)
        self.assertTrue(all(r['confirmed_institutional_inflow'] for r in ranked))
        self.assertTrue(all(r['leadership_basis']!='broad_uptrend' for r in ranked))

    def test_official_industry_fallback_is_observed_not_fabricated_etf_volume(self):
        c=self.company();sectors=industry_sector_evidence(dict(rows=[self.sector()]))
        i=dict(data_date='2026-11-19',price=100,ma20=99,return_5d=.03,relative_strength=.02,
            volume_ratio=1.5,market_return_20d=-.1,volatility_20d=.03)
        f=build_company_forecast(c,i,100,AT,sectors=sectors)
        self.assertEqual(f['assessment_status'],'complete')
        self.assertEqual(f['evidence']['sector_pressure']['method'],'official_matching_industry_relative_leadership')
        self.assertNotIn('turnover_ratio',sectors[0])
        for changes in [dict(domestic_industry_name='금융'),dict(source_symbol='000010.KQ')]:
            changed=dict(c,**changes)
            self.assertIn('sector_pressure',build_company_forecast(changed,i,100,AT,sectors=sectors)['missing_dimensions'])

    def test_stale_or_future_official_sector_frame_is_not_accepted(self):
        c=self.company();i=dict(data_date='2026-11-19',price=100,ma20=99,market_return_20d=.02)
        for changes in [dict(observed_at=(AT-timedelta(minutes=16)).isoformat()),dict(business_date='20261121')]:
            sectors=industry_sector_evidence(dict(rows=[dict(self.sector(),**changes)]))
            self.assertIn('sector_pressure',build_company_forecast(c,i,100,AT,sectors=sectors)['missing_dimensions'])

    def test_operating_cash_margin_uses_matching_ytd_sales_with_loss(self):
        receipt='20261113000001'
        def row(account,name,kind,amount,**more):return dict(account_id=account,account_nm=name,sj_div=kind,
            thstrm_amount=str(amount),rcept_no=receipt,**more)
        data=[row('ifrs-full_Revenue','매출액','IS',1000,thstrm_add_amount='2500'),
            row('dart_OperatingIncomeLoss','영업이익','IS',-100,thstrm_add_amount='-200'),
            row('ifrs-full_CashFlowsFromUsedInOperatingActivities','영업활동현금흐름','CF',100),
            row('legacy_custom_equity','자본 총계','BS',1000),row('ifrs_Liabilities','부채총계','BS',500)]
        with patch('kis_intraday.requests.get') as get:
            get.return_value.json.return_value=dict(status='000',list=data)
            value=dart_quarterly_financials('fake','00000000',[dict(form='분기보고서 (2026.09)',url='https://dart.fss.or.kr/?rcpNo='+receipt)])
        self.assertEqual(value['operating_cash_flow_margin_pct'],4)
        self.assertEqual(value['liabilities_to_equity'],.5)
        self.assertNotIn('operating_cash_conversion',value)
        # Equity also appears in the statement of changes in equity. A later,
        # blank SCE row must not overwrite the balance-sheet account.
        data.extend([row('ifrs-full_Equity','자본총계','BS',1000),
                     row('ifrs-full_Equity','자본변동','SCE','')])
        with patch('kis_intraday.requests.get') as get:
            get.return_value.json.return_value=dict(status='000',list=data)
            checked=dart_quarterly_financials('fake','00000000',[dict(form='분기보고서 (2026.09)',url='https://dart.fss.or.kr/?rcpNo='+receipt)])
        self.assertEqual(checked['liabilities_to_equity'],.5)

    def test_fund_is_not_an_operating_company_but_ordinary_stock_is_preserved(self):
        self.assertTrue(is_fund(dict(name='KODEX 200',asset_type='stock_or_etf')))
        self.assertTrue(is_fund(dict(name='TIGER 미국나스닥100',asset_type='stock_or_etf')))
        self.assertFalse(is_fund(dict(name='삼성전자',asset_type='stock_or_etf')))

    def test_unrelated_unread_world_article_does_not_block_quiet_news(self):
        c=dict(company_name='Alpha Systems',business_description='semiconductor equipment manufacturer',
            news_status='no_recent_articles',filing_status='checked')
        e=dict(url='https://www.ecb.europa.eu/example.pdf',event_id='ecb',scope='world',
            headline='Smart supervision for small banks',available_at=(AT-timedelta(hours=1)).isoformat(),body='',body_status='not_html')
        result=analyze_events(c,[e],AT,[dict(status='checked')])
        self.assertEqual(result['status'],'no_material_events_identified')
        self.assertFalse(result['unread_events'][0]['required'])

    def test_exposed_world_event_body_still_blocks_assessment(self):
        c=dict(company_name='Alpha Air',business_description='airline operator',news_status='no_recent_articles',filing_status='checked')
        e=dict(url='https://www.eia.gov/example',event_id='oil',scope='world',headline='Energy costs increased',
            available_at=(AT-timedelta(hours=1)).isoformat(),body='',body_status='download_failed')
        result=analyze_events(c,[e],AT,[dict(status='checked')])
        self.assertEqual(result['status'],'insufficient_causal_evidence')
        self.assertTrue(result['unread_events'][0]['required'])

if __name__=='__main__':unittest.main()
