import unittest
from datetime import datetime,timedelta,timezone
from catalyst_policy import catalyst_context
from company_forecast import MIN_COMPOSITE_SCORE, build_company_forecast
from preopen_signals import evaluate_preopen
from event_publication import publication_metadata

AT=datetime(2026,11,19,0,40,tzinfo=timezone.utc)

def fixture():
    c=dict(market='KR',country='South Korea',sector='Communication Services',
        quarter='2026-09',available_at=(AT-timedelta(minutes=1)).isoformat(),
        operating_margin_pct=15,revenue_yoy_pct=25,operating_margin_yoy_pp=4,
        free_cash_flow_margin_pct=10,operating_cash_conversion=1.5,net_debt_to_equity=.1,
        trailing_pe=15,forward_pe=12,filing_status='checked',news_status='ok',price_tick=100,
        price_history=[
            dict(date='2026-11-17',close=150000,available_at='2026-11-18T00:00:00+09:00'),
            dict(date='2026-11-18',close=166600,available_at='2026-11-19T00:00:00+09:00')],
        event_analysis=dict(generated_at=AT.isoformat(),status='insufficient_causal_evidence',
            feed_coverage=[dict(status='checked')],unread_events=[],
            economic_graph=dict(links=[],expectation_links=[
                dict(event_id='policy',source_url='https://www.korea.kr/news/policyNewsView.do?newsId=123',
                    available_at='2026-11-19T00:00:00+09:00',publication_verified=True,
                    body='정부가 다음 주 한중 문화교류 확대 방안을 발표할 예정이라고 밝혔다. ' * 12,
                    body_status='public_body',
                    availability_basis='date_only_next_local_day',watch_bias='positive_policy_sympathy')])))
    i=dict(price=166600,ma20=157540,ma60=165923,return_5d=.1272,return_20d=-.0891,
        relative_strength=.0274,volume=661721,volume_ratio=2.3603,average_turnover_20d=43588543690,
        atr14_pct=5.9381,volatility_20d=.0439,market_return_20d=-.1165,
        obv_rising=True,breakout=False,breakdown=False,rebound=True,overheated=False,data_date='2026-11-18')
    sectors=[dict(market='KR',name='미디어·엔터',data_date='2026-11-18',
        return_pct=3,market_excess_pct=2,turnover_ratio=2)]
    return c,i,sectors

class CatalystTests(unittest.TestCase):
    def test_dated_new_event_does_not_treat_pre_event_five_day_rise_as_event_reaction(self):
        c,i,s=fixture();f=build_company_forecast(c,i,i['price'],AT,sectors=s)
        self.assertTrue(f['catalyst_context']['eligible'])
        self.assertEqual(f['catalyst_context']['post_available_return_pct'],0)
        self.assertEqual(f['priced_in_fraction'],0)
        row=dict(market='KR',symbol='UNRELATED_CODE.KS',name='기업',indicators=i,company_analysis=c,
                 company_forecast=f,signal=dict(confirmed=False,score=0))
        report=dict(results=[row],preopen_session='2026-11-19')
        evaluate_preopen(report,'2026-11-18',AT)
        self.assertEqual(row['preopen_decision'],'conditional_buy')
        self.assertEqual(row['preopen_signal']['strategy'],'event_catalyst')
        self.assertGreaterEqual(row['preopen_signal']['entry_limit'],i['price'])
        self.assertLessEqual(row['preopen_signal']['entry_limit'],i['price']*(1+i['atr14_pct']/100))
        self.assertEqual(row['preopen_signal']['entry_limit']%100,0)
        self.assertFalse(row['preopen_signal']['confirmed'])
    def test_unverified_stale_future_or_unsupported_sector_does_not_bypass_risk(self):
        for change in ['unverified','stale','future','weak_sector','no_history','price_extended']:
            c,i,s=fixture();link=c['event_analysis']['economic_graph']['expectation_links'][0]
            if change=='unverified':link['publication_verified']=False
            if change=='stale':link['available_at']=(AT-timedelta(days=3)).isoformat()
            if change=='future':link['available_at']=(AT+timedelta(minutes=1)).isoformat()
            if change=='weak_sector':s[0]['turnover_ratio']=1
            if change=='no_history':c['price_history']=[]
            if change=='price_extended':i['ma20']=130000
            self.assertFalse(catalyst_context(c,i,i['price'],AT,s)['eligible'],change)
    def test_no_repeated_fresh_catalyst_after_price_has_already_reacted(self):
        c,i,s=fixture();i['price']=200000;i['ma20']=190000
        # More than 1.5 ATR after the same dated event, even though MA extension is modest.
        self.assertFalse(catalyst_context(c,i,200000,AT,s)['eligible'])
    def test_not_limited_to_entertainment_or_named_tickers(self):
        c,i,s=fixture();c['sector']='Technology';s[0]['name']='반도체'
        self.assertTrue(catalyst_context(c,i,i['price'],AT,s)['eligible'])
    def test_weak_business_still_reduces_full_composite(self):
        c,i,s=fixture();healthy=build_company_forecast(c,i,i['price'],AT,sectors=s)
        c.update(operating_margin_pct=-20,revenue_yoy_pct=-40,operating_margin_yoy_pp=-10,
                 free_cash_flow_margin_pct=-20,operating_cash_conversion=.1,net_debt_to_equity=3)
        weak=build_company_forecast(c,i,i['price'],AT,sectors=s)
        self.assertLess(weak['composite_score'],MIN_COMPOSITE_SCORE)
        self.assertGreater(healthy['composite_score'],weak['composite_score'])

class PublicationTests(unittest.TestCase):
    def test_official_revision_is_available_before_validation_cutoff(self):
        html='<dt>작성일</dt><dd>2026-11-13</dd><dt>수정일</dt><dd>2026-11-14</dd>'
        metadata=publication_metadata(html,'https://www.mofa.go.kr/www/brd/m_4080/view.do?seq=1',AT)
        self.assertTrue(metadata['publication_verified'])
        self.assertEqual(metadata['source_published_date'],'2026-11-13')
        self.assertEqual(metadata['available_at'],'2026-11-15T00:00:00+09:00')
    def test_date_only_source_is_known_next_local_day_and_does_not_refresh_on_reread(self):
        html='<span class="date">2026.11.18</span>'
        url='https://www.korea.kr/news/policyNewsView.do?newsId=123'
        a=publication_metadata(html,url,AT)
        b=publication_metadata(html,url,AT+timedelta(days=1))
        self.assertEqual(a['available_at'],b['available_at'])
        self.assertEqual(a['available_at'],'2026-11-19T00:00:00+09:00')
        self.assertTrue(a['publication_verified'])
    def test_future_or_missing_published_time_remains_unverified(self):
        for html in ['<p>no publication metadata</p>',
                     '<meta property="article:published_time" content="2026-11-20T01:00:00+09:00">']:
            self.assertFalse(publication_metadata(html,'https://www.korea.kr/a',AT)['publication_verified'])

if __name__=='__main__':unittest.main()
