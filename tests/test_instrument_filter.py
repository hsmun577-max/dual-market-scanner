"""상품 유형 필터: 채권·만기매칭형 ETF가 주식처럼 통과하던 구멍 (2026-11-24).

2026-11-24 스캔에서 미국 11종목이 차트를 통과해 시장 국면 판정까지 올라갔는데, 그중 4종목이
채권 ETF였다(`IBDR`, `IBDW` = iShares iBonds TERM CORPORATE, `CGSD` = SHORT DURATION,
`DFGX` = FIXED INCOME). 이름 표기 때문에 `CASH_OR_BOND` 정규식이 잡지 못했다:

* \"IBONDS\"는 'Bonds'가 단어 경계로 시작하지 않아 `\\bbonds?\\b`에 걸리지 않는다.
* 만기매칭형은 'TERM CORPORATE'로만 채권임을 밝힌다.

여기에 옵션 구조가 상환을 결정하는 상품(`CAOS` TAIL RISK, `CBOX` COLLATERAL)도 같은 날
주식처럼 올라왔다. 이 테스트는 그 이름들이 계속 걸러지는지, 그리고 진짜 주식·지수 ETF는
걸리지 않는지를 함께 고정한다.
"""
import unittest

from candidate_selection import (instrument_rejection_reason, liquidity_rejection_reason,
                                 rank_market_candidates)


def indicators(**extra):
    values = dict(price=50.0, volume=1_000_000.0, volume_ratio=1.9, ma20=48.0, ma60=46.0,
                  return_5d=0.02, return_20d=0.05, relative_strength=0.03, obv_rising=True,
                  breakout=False, breakdown=False, rebound=False, overheated=False,
                  market_above_ma20=True, average_turnover_20d=5.0e7)
    values.update(extra)
    return values


class BondNameTests(unittest.TestCase):
    def test_ibonds_term_corporate_is_a_bond_product(self):
        for symbol, name in (("IBDR", "ISHARES IBONDS DEC 2026 TERM CORPORATE"),
                             ("IBDW", "ISHARES IBONDS DEC 2031 TERM CORPORATE"),
                             ("IBDS", "ISHARES IBONDS DEC 2027 TERM CORPORATE"),
                             ("IBHF", "ISHARES IBONDS 2026 TERM HIGH YIELD AND INCOME"),
                             ("IBID", "ISHARES IBONDS OCT 2027 TERM TIPS")):
            self.assertEqual(instrument_rejection_reason(symbol, name), "cash_or_bond_product", name)

    def test_other_fixed_income_naming_is_caught(self):
        for symbol, name in (
                ("CGSD", "CAPITAL GROUP SHORT DURATION INCOME"),
                ("DFGX", "DIMENSIONAL GLOBAL EX-US CORE FIXED INCOME"),
                ("FIXD", "FIRST TRUST TCW OPPORTUNISTIC FIXED INCOME"),
                ("LMBS", "FIRST TRUST LOW DURATION OPPORTUNITIES"),
                ("BRHY", "BLACKROCK HIGH YIELD"),
                ("JMUB", "JP MORGAN MUNICIPAL"),
                ("AVGB", "AVANTIS CREDIT"),
                ("SECU", "ISHARES SECURITIZED INCOME ACTIVE"),
                ("SJB", "PROSHARES HIGH YIELD -1X"),
                ("445290.KS", "KODEX 국채10년")):
            self.assertEqual(instrument_rejection_reason(symbol, name), "cash_or_bond_product", name)

    def test_option_structures_are_not_screened_as_stocks(self):
        for symbol, name in (("CAOS", "ALPHA ARCHITECT TAIL RISK"),
                             ("CBOX", "CALAMOS TAX-AWARE COLLATERAL"),
                             ("ACSP", "PROSHARES S&P 500 AUTOCALLABLE INCOME"),
                             ("JEPQ", "JP MORGAN NASDAQ EQUITY PREMIUM INCOME")):
            if symbol == "JEPQ":
                # 커버드콜 계열은 별도 판단 영역이다(같은 날 지시 범위 밖).
                self.assertIsNone(instrument_rejection_reason(symbol, name), name)
                continue
            self.assertEqual(instrument_rejection_reason(symbol, name), "complex_product", name)

    def test_real_equities_and_index_etfs_still_pass(self):
        for symbol, name in (("ROKU", "로쿠"), ("AES", "AES"), ("ALH", "얼라이언스 런드리 홀딩스"),
                             ("005930.KS", "삼성전자"), ("263860.KQ", "지니언스"),
                             ("PEY", "INVESCO HIGH YIELD EQUITY DIVIDEND ACHIEVERS"),
                             ("BTCK", "7RCC SPOT BITCOIN AND CARBON CREDIT FUTURES")):
            self.assertIsNone(instrument_rejection_reason(symbol, name), name)


class RankingTests(unittest.TestCase):
    def test_a_bond_etf_cannot_enter_the_candidate_list(self):
        rows = [dict(market="US", symbol="IBDR", name="ISHARES IBONDS DEC 2026 TERM CORPORATE",
                     asset_type="stock_or_etf", indicators=indicators(), signal=dict(score=70)),
                dict(market="US", symbol="ROKU", name="로쿠", asset_type="stock_or_etf",
                     indicators=indicators(), signal=dict(score=60))]
        ranked = rank_market_candidates(rows, "US")
        self.assertEqual([item["symbol"] for item in ranked], ["ROKU"])
        self.assertEqual(liquidity_rejection_reason(rows[0], "US"), "cash_or_bond_product")


if __name__ == "__main__":
    unittest.main()
