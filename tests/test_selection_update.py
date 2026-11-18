import unittest
from candidate_selection import candidate_rejection_reason, rank_market_candidates
from kis_intraday import select_intraday_leaders, evaluate_candidate, KST
from datetime import datetime


def stock(market="KR", symbol="005930.KS", name="일반기업"):
    price, volume = (10000, 2000000) if market == "KR" else (100, 400000)
    return dict(market=market,symbol=symbol,name=name,asset_type="stock_or_etf",
                signal=dict(score=6,stage="장중 조건부 진입 후보"),
                indicators=dict(price=price,volume=volume,volume_ratio=2,ma20=price*.95,
                    ma60=price*.9,return_5d=.05,return_20d=.10,relative_strength=.05,
                    obv_rising=True,breakout=True,breakdown=False,rebound=False,overheated=False))


class SelectionRegressionTests(unittest.TestCase):
    def test_cash_spac_and_preferred_products_are_filtered(self):
        for market,symbol,name in [("KR","481050.KS","KODEX CD1년금리플러스액티브(합성)"),
                                  ("KR","423160.KS","KODEX KOFR금리액티브(합성)"),
                                  ("US","EGHA","EGH 애퀴지션"),
                                  ("US","FRMEP","퍼스트 머천츠 우선주 A(7.500%)"),
                                  ("US","COSW","ROUNDHILL COST WEEKLYPAY")]:
            self.assertIsNotNone(candidate_rejection_reason(stock(market,symbol,name),market))

    def test_equity_etfs_remain_eligible(self):
        self.assertIsNone(candidate_rejection_reason(stock(name="KODEX 반도체"),"KR"))
        self.assertIsNone(candidate_rejection_reason(stock("US","SPY","SPDR S&P 500 ETF"),"US"))

    def test_volume_spike_cannot_replace_regular_liquidity(self):
        item=stock()
        item["indicators"]["volume_ratio"]=100
        self.assertEqual(candidate_rejection_reason(item,"KR"),"insufficient_liquidity")

    def test_us_zero_volume_and_penny_stock_are_filtered(self):
        item=stock("US","TEST","Normal company")
        item["indicators"]["volume"]=0
        self.assertEqual(candidate_rejection_reason(item,"US"),"insufficient_liquidity")
        item=stock("US","TEST","Normal company")
        item["indicators"]["price"]=.9
        self.assertEqual(candidate_rejection_reason(item,"US"),"low_price")

    def test_rebounds_and_chasing_are_filtered(self):
        item=stock()
        item["indicators"]["rebound"]=True
        self.assertEqual(candidate_rejection_reason(item,"KR"),"breakdown_or_rebound")
        item=stock()
        item["indicators"]["return_5d"]=.1467
        self.assertEqual(candidate_rejection_reason(item,"KR"),"extended_price")

    def test_us_ticker_order_does_not_override_relative_strength(self):
        weak=stock("US","AAA","First firm")
        strong=stock("US","ZZZ","Second firm")
        strong["indicators"]["relative_strength"]=.08
        self.assertEqual(rank_market_candidates([weak,strong],"US",1)[0]["symbol"],"ZZZ")

    def test_no_padding_with_ineligible_candidates(self):
        bad=stock("US","X","Blank acquisition")
        self.assertEqual(rank_market_candidates([bad],"US",5),[])

    def test_dynamic_leaders_share_product_and_chasing_filters(self):
        rows=[dict(mksc_shrn_iscd=code,hts_kor_isnm=name,acml_tr_pbmn=turnover,prdy_ctrt=gain)
              for code,name,turnover,gain in [("005930","삼성전자",2000000000,2),
                  ("423160","KODEX KOFR금리액티브",9000000000,1),
                  ("000001","급등기업",9000000000,12),("000002","저유동기업",100000000,2)]]
        self.assertEqual(select_intraday_leaders(rows),{"005930":"삼성전자"})

    def test_live_entry_rejects_excluded_product_before_breakout(self):
        import kis_intraday
        from unittest.mock import patch
        with patch.dict(kis_intraday.US_SYMBOL_NAMES,{"TEST":"SPAC acquisition"}):
            status,candidate=evaluate_candidate({},None,datetime(2026,11,18,9,tzinfo=KST),
                                                "US-early","DNASTEST",[],True)
        self.assertEqual(status,"special_security")
        self.assertIsNone(candidate)


if __name__=="__main__":
    unittest.main()
