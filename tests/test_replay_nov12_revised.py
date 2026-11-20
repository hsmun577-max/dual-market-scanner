import unittest
from unittest.mock import patch
import pandas as pd
import replay_nov12_revised as replay

class HistoricalBoundaryTests(unittest.TestCase):
    def test_all_22_disclosure_pages_and_dated_query(self):
        calls=[]
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'status':'000','total_page':22,'list':[{'example':True}]}
        def get(url,params,timeout):
            calls.append(params)
            return Response()
        with patch.object(replay.requests,'get',side_effect=get):
            events,coverage=replay.event_index('example-key')
        self.assertEqual(len(events),44)
        self.assertEqual([r['pages'] for r in coverage],[22,22])
        self.assertTrue(all(c['end_de']=='20261111' for c in calls))
        self.assertFalse(any(r['truncated'] for r in coverage))

    def test_excess_pages_raise_instead_of_silent_partial_universe(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'status':'000','total_page':101,'list':[]}
        with patch.object(replay.requests,'get',return_value=Response()):
            with self.assertRaisesRegex(ValueError,'Incomplete historical'):
                replay.event_index('example-key')

    def test_price_reference_excludes_today(self):
        index=pd.to_datetime(['2026-11-12','2026-11-18','2026-11-19'])
        prices=pd.DataFrame({'Open':[100,110,999],'High':[100,110,999],
            'Low':[100,110,999],'Close':[100,110,999],'Volume':[100,100,100]},index=index)
        history=pd.DataFrame({k:[100]*65 for k in prices.columns},index=pd.bdate_range(end='2026-11-11',periods=65))
        prices=pd.concat([history,prices])
        result=replay.price_reference({'symbol':'EXAMPLE'},{'EXAMPLE':prices})
        self.assertEqual(result['close_to_close_pct'],10)
        self.assertEqual(result['nov18_close'],110)
        self.assertEqual(result['status'],'reference_only_not_strategy')

if __name__=='__main__': unittest.main()
