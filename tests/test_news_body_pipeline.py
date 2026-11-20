import unittest
from datetime import datetime,timezone
from unittest.mock import Mock,patch
from event_impact_analysis import ArticleBody,article_text,canonical_article_url,issuer_name_in_text,issuer_title_subject,_BODY_CACHE
from issuer_news import parse_issuer_news
AT=datetime(2026,11,19,10,tzinfo=timezone.utc)

class NewsBodyTests(unittest.TestCase):
    def test_platform_attribution_is_not_naver_company_news(self):
        title='엔비디아 3분기 매출 증가 : 네이버 블로그 - Naver Blog'
        self.assertFalse(issuer_name_in_text(issuer_title_subject(title),'네이버'))
        self.assertTrue(issuer_name_in_text(issuer_title_subject('네이버, 블로그 사업 매출 증가 - Naver Blog'),'네이버'))
    def test_three_syllable_company_not_another_company_or_unrelated_suffix(self):
        for title in ('디에스엠 실적','에스엠랩 매출 감소','갤럭시아에스엠 영업손실'):
            self.assertFalse(issuer_name_in_text(title,'에스엠'))
        self.assertTrue(issuer_name_in_text('에스엠은 공연을 확대한다','에스엠'))
        self.assertTrue(issuer_name_in_text('[특징주] 에스엠, 중국 공연 기대','에스엠'))
    def test_publisher_div_article_is_read_without_navigation_or_script(self):
        parser=ArticleBody()
        parser.feed('<nav><p>무관한 메뉴</p></nav><div class="view_cont"><div>한중 문화교류 확대</div>'
                    '<script>잘못된 광고</script><p>공연 논의</p></div><footer>무관</footer>')
        text=''.join(parser.parts)
        self.assertIn('한중 문화교류 확대',text);self.assertIn('공연 논의',text)
        self.assertNotIn('무관',text);self.assertNotIn('광고',text)
    def test_canonical_government_link_deduplicates_tracking_only(self):
        self.assertEqual(canonical_article_url('https://www.korea.kr/news/policyNewsView.do?newsId=123&pWise=main'),
                         'https://www.korea.kr/news/policyNewsView.do?newsId=123')
    def test_full_div_body_is_downloaded_once_per_canonical_link(self):
        _BODY_CACHE.clear()
        response=Mock(status_code=200,encoding='utf-8',headers={'Content-Type':'text/html'})
        response.iter_content.return_value=[('<div class="view_cont">'+'문화교류 강화 관련 원문입니다. '*40+'</div>').encode()]
        with patch('event_impact_analysis.requests.get',return_value=response) as get:
            first=article_text('https://www.korea.kr/news/policyNewsView.do?newsId=123&pWise=main')
            second=article_text('https://www.korea.kr/news/policyNewsView.do?newsId=123&pWise=sub')
        self.assertEqual(first[1],'public_body');self.assertEqual(first,second);self.assertEqual(get.call_count,1)
        _BODY_CACHE.clear()
    def test_direct_news_schema_preserves_date_and_excludes_other_companies(self):
        def row(title,day='20261119180000'):
            return dict(title=title,datetime=day,officeId='001',articleId='0012345678',officeName='발행사')
        document={'result':[{'items':[row('디에스엠 매출'),row('에스엠랩 실적'),row('에스엠 공연 확대'),
                                     row('에스엠 미래뉴스','20261120180000')]}]}
        parsed=parse_issuer_news(document,'에스엠',AT)
        self.assertEqual(len(parsed),1)
        self.assertEqual(parsed[0]['url'],'https://n.news.naver.com/article/001/0012345678')
        self.assertEqual(parsed[0]['headline'],'에스엠 공연 확대')

if __name__=='__main__':unittest.main()
