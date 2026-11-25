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

    def test_inverse_and_leveraged_products_are_no_longer_refused_as_a_class(self):
        # Owner's call (2026-11-23: "차단만 먼저 풀어") after SQQQ was screened out on a US
        # down day only by this block. The trend, demand and market-regime rules that
        # follow are unchanged, so a listed inverse fund now has to pass them like any
        # other instrument.
        for market,symbol,name,asset_type in [("US","SQQQ","프로셰어즈 나스닥100 인버스 3배 ETF","inverse_etf"),
                                              ("US","TQQQ","프로셰어즈 나스닥100 3배 ETF","leveraged_etf"),
                                              ("US","SOXL","디렉시온 반도체 3배 ETF","leveraged_etf"),
                                              ("US","SOXS","디렉시온 반도체 인버스 3배 ETF","inverse_etf"),
                                              ("KR","252670.KS","KODEX 200선물인버스2X","inverse_etf"),
                                              ("KR","122630.KS","KODEX 레버리지","leveraged_etf")]:
            item=stock(market,symbol,name);item["asset_type"]=asset_type
            if asset_type == "inverse_etf":
                item["indicators"]["inverse_underlying_below_ma20"] = True
            self.assertIsNone(candidate_rejection_reason(item,market),(symbol,name))

    def test_dividend_style_structures_are_still_refused(self):
        item=stock("US","COSW","ROUNDHILL COST WEEKLYPAY")
        self.assertEqual(candidate_rejection_reason(item,"US"),"complex_product")
        item=stock("US","COVX","Example COVERED CALL ETF")
        self.assertEqual(candidate_rejection_reason(item,"US"),"complex_product")

    def test_an_unblocked_inverse_fund_still_needs_the_trend_and_the_market(self):
        # The fund must be in an uptrend while its own underlying is below its average.
        item=stock("US","SQQQ","프로셰어즈 나스닥100 인버스 3배 ETF")
        item["asset_type"]="inverse_etf"
        item["indicators"]["price"]=item["indicators"]["ma20"]*.98
        self.assertEqual(candidate_rejection_reason(item,"US"),"unconfirmed_uptrend")
        item=stock("US","SQQQ","프로셰어즈 나스닥100 인버스 3배 ETF")
        item["asset_type"]="inverse_etf"
        item["indicators"]["inverse_underlying_below_ma20"]=False
        self.assertEqual(candidate_rejection_reason(item,"US"),"market_headwind")

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

    def test_live_price_floor_exception_is_specific_to_kodex_inverse_2x(self):
        at = datetime(2026, 11, 18, 9, tzinfo=KST)
        for symbol, ask, expected in [
            ("252670", 140, "missing_pullback_bars"),
            ("252670", 0, "low_price"),
            ("114800", 140, "low_price"),
            ("005930", 900, "low_price"),
        ]:
            with self.subTest(symbol=symbol, ask=ask):
                status, _ = evaluate_candidate({}, {"ask": ask}, at, "KRX", symbol,
                                               [], False, require_pullback=True)
                self.assertEqual(status, expected)

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
