import json
from unittest.mock import Mock,patch
import unittest
from event_impact_analysis import article_text,public_structured_body,_BODY_CACHE

class PublicArticleLimitTests(unittest.TestCase):
    def response(self,document):
        response=Mock(status_code=200,encoding='utf-8',headers={'Content-Type':'text/html'})
        response.iter_content.return_value=[document.encode()];return response

    def test_large_public_yahoo_shell_does_not_hide_small_article_body(self):
        _BODY_CACHE.clear();text='Published corporate earnings and business analysis. '*30
        doc='<script>'+'x'*700000+'</script><div class="caas-body">'+text+'</div>'
        with patch('event_impact_analysis.requests.get',return_value=self.response(doc)):
            body,status=article_text('https://finance.yahoo.com/news/limit-regression')
        self.assertEqual(status,'public_body');self.assertIn('earnings',body)
        self.assertLess(len(body),3000);_BODY_CACHE.clear()

    def test_other_publishers_keep_existing_size_bound(self):
        _BODY_CACHE.clear()
        with patch('event_impact_analysis.requests.get',return_value=self.response('<p>'+'x'*700000+'</p>')):
            self.assertEqual(article_text('https://www.sec.gov/limit-regression')[1],'body_too_large')

    def test_public_structured_article_and_premium_restriction(self):
        article=dict(**{'@type':'NewsArticle'},isAccessibleForFree=True,articleBody='Public earnings news. '*40)
        html='<script type="application/ld+json">'+json.dumps(article)+'</script>'
        self.assertEqual(public_structured_body(html,'https://finance.yahoo.com/news/example')[1],'public_body')
        article['isAccessibleForFree']=False
        html='<script type="application/ld+json">'+json.dumps(article)+'</script>'
        self.assertEqual(public_structured_body(html,'https://finance.yahoo.com/news/example'),('','access_restricted'))

if __name__=='__main__':unittest.main()
