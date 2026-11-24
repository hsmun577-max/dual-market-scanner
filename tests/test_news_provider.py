import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import issuer_news
from issuer_news import parse_issuer_news, issuer_news_result
from news_provider import collect_news
from catalyst_track import CATALYST_FRESH_HOURS, freshness_hours

class WindowTests(unittest.TestCase):
    """수집 창은 판정 창(72시간, `catalyst_track.confirmed_news`)보다 좁으면 안 된다."""

    def row(self, day):
        return dict(title='하이브, 3분기 영업이익 253% 급증', titleFull='하이브, 3분기 영업이익 253% 급증',
                    datetime=day, officeId='001', articleId='0012345678')

    def test_the_collection_window_matches_the_catalyst_window(self):
        self.assertEqual(issuer_news.NEWS_WINDOW_HOURS, freshness_hours('confirmed_news'))
        self.assertEqual(CATALYST_FRESH_HOURS, 72)

    def test_an_article_from_two_days_ago_is_still_collected(self):
        at = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
        # 2026-11-17 10:01 KST = 2026-11-17 01:01 UTC: 약 72시간 전.
        news = parse_issuer_news([dict(items=[self.row('202611171001')])], '하이브', at)
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]['available_at'], '2026-11-17T10:02:00+09:00')

    def test_something_older_than_the_window_is_still_dropped(self):
        at = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
        news = parse_issuer_news([dict(items=[self.row('202611161000')])], '하이브', at)
        self.assertEqual(news, [])


class ProviderTests(unittest.TestCase):
    def test_real_minute_schema_and_public_preview_matching(self):
        row=dict(title='넥스 신작 출시',titleFull='넥스, 신작 출시',body='하이브와 공동 개발',
                 datetime='202611200711',officeId='144',articleId='0001150813')
        at=datetime(2026,11,20,0,tzinfo=timezone.utc)
        news=parse_issuer_news([dict(items=[row])],'하이브',at)
        self.assertEqual(len(news),1)
        self.assertEqual(news[0]['available_at'],'2026-11-20T07:12:00+09:00')
        self.assertNotIn('body',news[0])
        self.assertEqual(news[0]['availability_basis'],'source_minute_next_minute')
        self.assertEqual(parse_issuer_news([row],'하이브',datetime(2026,11,19,22,11,30,tzinfo=timezone.utc)),[])

    def test_native_success_prevents_unrelated_yahoo_fallback(self):
        ticker=Mock(); native=[dict(headline='하이브 실적')]
        result=collect_news(ticker,None,Mock(),lambda:native)
        self.assertEqual(result['news'],native)
        ticker.get_news.assert_not_called()

    def test_all_failed_is_not_checked_empty(self):
        ticker=Mock(); ticker.get_news.side_effect=RuntimeError()
        def failed():raise RuntimeError()
        self.assertEqual(collect_news(ticker,None,Mock(),failed)['news_status'],'unavailable')
        self.assertEqual(collect_news(ticker,None,Mock(),lambda:[])['news_status'],'no_recent_articles')

    def test_unknown_schema_is_not_empty_news(self):
        response=Mock(status_code=200);response.json.return_value={'error':'upstream'}
        with patch('issuer_news.requests.get',return_value=response):
            result=issuer_news_result('352820','하이브',datetime.now(timezone.utc))
        self.assertEqual(result['status'],'unavailable')

if __name__=='__main__':unittest.main()
