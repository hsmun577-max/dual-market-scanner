import unittest
from unittest.mock import Mock,patch
import requests
from event_impact_analysis import article_text,_BODY_CACHE

class ArticleRetryTests(unittest.TestCase):
    def setUp(self):_BODY_CACHE.clear()
    def response(self):
        r=Mock(status_code=200,encoding='utf-8',headers={'Content-Type':'text/html'})
        r.iter_content.return_value=[('<article><p>'+'Public issuer earnings data. '*30+'</p></article>').encode()]
        return r
    def test_timeout_retries_once_and_preserves_public_body(self):
        with patch('event_impact_analysis.requests.get',side_effect=[requests.Timeout(),self.response()]) as get:
            body,status=article_text('https://n.news.naver.com/article/001/0012345678')
        self.assertEqual(status,'public_body');self.assertGreater(len(body),400)
        self.assertEqual(get.call_count,2)
    def test_repeated_server_failure_is_still_missing(self):
        with patch('event_impact_analysis.requests.get',return_value=Mock(status_code=503)) as get:
            body,status=article_text('https://n.news.naver.com/article/001/0012345678')
        self.assertEqual(body,'');self.assertEqual(status,'transient_http_503')
        self.assertEqual(get.call_count,2)
    def test_access_denial_is_not_retried(self):
        denied=Mock(status_code=403)
        denied.raise_for_status.side_effect=requests.HTTPError(response=denied)
        with patch('event_impact_analysis.requests.get',return_value=denied) as get:
            body,status=article_text('https://n.news.naver.com/article/001/0012345678')
        self.assertEqual(body,'');self.assertEqual(status,'http_error_403')
        self.assertEqual(get.call_count,1)

if __name__=='__main__':unittest.main()
