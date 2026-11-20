import unittest
from analysis_batch import collect_company_results

class BatchTests(unittest.TestCase):
    def test_one_provider_failure_keeps_other_issuers(self):
        def fetch(symbol):
            if symbol=='bad':raise RuntimeError('provider unavailable')
            return symbol,dict(news_status='ok'),[]
        results=collect_company_results(fetch,['good','bad','other'])
        self.assertEqual([r[0] for r in results],['good','bad','other'])
        self.assertEqual(results[0][1]['news_status'],'ok')
        self.assertEqual(results[1][1]['analysis_status'],'failed')
        self.assertEqual(results[2][1]['news_status'],'ok')

    def test_wrong_issuer_is_not_reused(self):
        result=collect_company_results(lambda _:('another',dict(news_status='ok'),[]),['requested'])
        self.assertEqual(result[0][0],'requested')
        self.assertEqual(result[0][1]['news_status'],'unavailable')

if __name__=='__main__':unittest.main()
