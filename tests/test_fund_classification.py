"""The exchange master's own instrument class decides the scoring model.

2026-11-25: QQQ and SPY were read as operating companies. Their Korean display names
("인베스코 QQQ", "SPDR S&P 500 ETF") carry no fund marker the scanner recognised, the
issuer profile arrived without ``quote_type``, and every one of them was refused as
``analysis_incomplete`` for an income statement a listed fund never has. The master file
already says which listings are funds, so the scan reads that instead of guessing.

The fixtures are real rows cut from the KIS master files (005930 삼성전자, 122630 KODEX
레버리지, AAPL, QQQ) with their original column layout.
"""
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import scanner
from research_asset import is_fund
from exit_policy import exit_policy_for

FIXTURES = Path(__file__).parent / "fixtures"


def frame(periods=90):
    index = pd.date_range(end=datetime(2026, 11, 24, tzinfo=timezone.utc).date(),
                          periods=periods, freq="D")
    close = np.linspace(90, 100, periods)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Adj Close": close,
                         "Volume": 1_000_000}, index=index)


class MasterParsingTests(unittest.TestCase):
    def test_a_korean_fund_is_read_from_the_group_code(self):
        lines = (FIXTURES / "kospi_sample.mst").read_text(encoding="utf-8").splitlines()
        names, funds = scanner.parse_kr_master(lines, "KS", 227)
        self.assertEqual(set(names), {"005930.KS", "122630.KS"})
        # 삼성전자 is ST, KODEX 레버리지 is EF.
        self.assertEqual(funds, {"122630.KS"})

    def test_a_us_fund_is_read_from_the_security_and_product_type(self):
        lines = (FIXTURES / "nasdaq_sample.cod").read_text(encoding="utf-8").splitlines()
        names, funds = scanner.parse_us_master(lines)
        self.assertIn("AAPL", names)
        self.assertIn("QQQ", names)
        # AAPL is 구분 2 (stock), QQQ is 구분 3 + 상품구분 001 (ETF).
        self.assertEqual(funds, {"QQQ"})

    def test_load_universe_records_the_fund_symbols(self):
        masters = {"kospi_code.mst": (FIXTURES / "kospi_sample.mst").read_text(encoding="utf-8").splitlines(),
                   "kosdaq_code.mst": (FIXTURES / "kosdaq_sample.mst").read_text(encoding="utf-8").splitlines(),
                   "nasmst.cod": (FIXTURES / "nasdaq_sample.cod").read_text(encoding="utf-8").splitlines(),
                   "nysmst.cod": (FIXTURES / "nasdaq_sample.cod").read_text(encoding="utf-8").splitlines(),
                   "amsmst.cod": (FIXTURES / "nasdaq_sample.cod").read_text(encoding="utf-8").splitlines()}
        from unittest.mock import patch
        with patch.object(scanner, "load_master", side_effect=lambda name: masters[name]):
            names = scanner.load_universe()
        self.assertIn("QQQ", names["US"])
        self.assertIn("QQQ", scanner.UNIVERSE_FUNDS["US"])
        self.assertNotIn("AAPL", scanner.UNIVERSE_FUNDS["US"])
        self.assertIn("122630.KS", scanner.UNIVERSE_FUNDS["KR"])
        self.assertNotIn("247540.KQ", scanner.UNIVERSE_FUNDS["KR"])
        self.assertTrue(any(code.endswith(".KQ") for code in scanner.UNIVERSE_FUNDS["KR"]))


class ScanAssetTypeTests(unittest.TestCase):
    def test_a_master_fund_is_stamped_as_a_fund_and_gets_the_fund_stop(self):
        scanner.UNIVERSE_FUNDS["US"].add("QQQ")
        try:
            results, errors = scanner.scan_market(
                "US", ["QQQ"], frame(), names={"QQQ": "인베스코 QQQ"},
                prices={"QQQ": frame()}, selection_now=datetime(2026, 11, 25, tzinfo=timezone.utc))
        finally:
            scanner.UNIVERSE_FUNDS["US"].discard("QQQ")
        self.assertEqual(errors, [])
        row = results[0]
        self.assertEqual(row["asset_type"], "etf")
        self.assertTrue(is_fund(row))
        self.assertEqual(exit_policy_for(row).stop_pct, 0.02)

    def test_an_unclassified_listing_keeps_the_stock_type(self):
        results, _errors = scanner.scan_market(
            "US", ["AAPL"], frame(), names={"AAPL": "애플"},
            prices={"AAPL": frame()}, selection_now=datetime(2026, 11, 25, tzinfo=timezone.utc))
        self.assertEqual(results[0]["asset_type"], "stock_or_etf")
        self.assertFalse(is_fund(results[0]))


if __name__ == "__main__":
    unittest.main()
