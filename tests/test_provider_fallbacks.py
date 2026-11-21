import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import rest_fallback
from rest_fallback import bars_from_rows
from provider_retry import call, transient
from sec_financials import financials

KST = ZoneInfo("Asia/Seoul")
OPENS = datetime(2026, 11, 20, 9, 0, tzinfo=KST)
CLOSES = OPENS + timedelta(minutes=15)


def domestic_row(day, clock, price, volume, high=None, low=None):
    return {"stck_bsop_date": day, "stck_cntg_hour": clock, "stck_prpr": str(price),
            "cntg_vol": str(volume), "stck_hgpr": str(high or price),
            "stck_lwpr": str(low or price)}


class RestFallbackTests(unittest.TestCase):
    def test_one_minute_rows_become_three_five_minute_bars(self):
        rows = [domestic_row("20261120", "090001", 100, 10, high=101, low=99),
                domestic_row("20261120", "090400", 103, 20, high=104, low=100),
                domestic_row("20261120", "090501", 99, 30, high=100, low=98),
                domestic_row("20261120", "091001", 105, 40),
                domestic_row("20261120", "091501", 120, 50)]
        bars = bars_from_rows(rows, OPENS, CLOSES, KST,
                              date_keys=("stck_bsop_date",), time_keys=("stck_cntg_hour",),
                              price_keys=("stck_prpr",), volume_keys=("cntg_vol",),
                              high_keys=("stck_hgpr",), low_keys=("stck_lwpr",))
        self.assertEqual(sorted(bars), [0, 1, 2])
        self.assertEqual(bars[0]["high"], 104)
        self.assertEqual(bars[0]["low"], 99)
        self.assertEqual(bars[0]["close"], 103)
        self.assertEqual(bars[0]["volume"], 30)
        self.assertEqual(bars[2]["volume"], 40)
        # 09:15 is the decision time, not part of the three opening bars.
        self.assertEqual([row for row, bar in bars.items() if bar["close"] == 120], [])

    def test_rows_outside_the_window_and_without_values_are_dropped(self):
        rows = [domestic_row("20261120", "085900", 100, 10),
                domestic_row("20261120", "090100", 0, 10),
                domestic_row("20261120", "090200", 100, 10)]
        bars = bars_from_rows(rows, OPENS, CLOSES, KST,
                              date_keys=("stck_bsop_date",), time_keys=("stck_cntg_hour",),
                              price_keys=("stck_prpr",), volume_keys=("cntg_vol",),
                              high_keys=("stck_hgpr",), low_keys=("stck_lwpr",))
        self.assertEqual(sorted(bars), [0])

    def test_domestic_quote_requires_a_real_ask_and_bid(self):
        client = Mock(headers={})
        with _patched_get({"output": {"askp1": "101", "bidp1": "100", "stck_prpr": "100"}}):
            quote = rest_fallback.domestic_quote(client, "005930")
        self.assertEqual(quote["ask"], 101)
        self.assertEqual(quote["bid"], 100)
        self.assertAlmostEqual(quote["spread_pct"], (101 - 100) * 200 / 201, places=4)
        with _patched_get({"output": {"askp1": "0", "bidp1": "0"}}):
            self.assertIsNone(rest_fallback.domestic_quote(client, "005930"))


class _patched_get:
    """Patch the REST request inside rest_fallback for one call."""

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        import rest_fallback as module
        self._original = module._get
        self.response = Mock(status_code=200)
        module._get = lambda *args, **kwargs: self.body
        return self.body

    def __exit__(self, *args):
        import rest_fallback as module
        module._get = self._original
        return False


class ProviderRetryTests(unittest.TestCase):
    def test_a_throttle_is_retried_and_the_second_answer_is_used(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("YFRateLimitError: too many requests")
            return "ok"

        self.assertEqual(call(flaky, attempts=3, backoff=(0, 0)), "ok")
        self.assertEqual(len(calls), 2)

    def test_a_missing_symbol_is_not_retried(self):
        calls = []

        def missing():
            calls.append(1)
            raise RuntimeError('HTTP Error 404: No fundamentals data found for symbol: FLIN')

        with self.assertRaises(RuntimeError):
            call(missing, attempts=3, backoff=(0, 0))
        self.assertEqual(len(calls), 1)

    def test_a_locked_cache_is_transient(self):
        self.assertTrue(transient(Exception("OperationalError('database is locked')")))
        self.assertFalse(transient(ValueError("empty download")))


def companyfacts():
    us = "USD"

    def duration(start, end, value):
        return {"start": start, "end": end, "val": value, "fy": 2026, "fp": "Q3", "form": "10-Q"}

    def instant(end, value):
        return {"end": end, "val": value, "fy": 2026, "fp": "Q3", "form": "10-Q"}

    return {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {us: [
            duration("2025-07-01", "2025-09-30", 1000),
            duration("2026-07-01", "2026-09-30", 1200)]}},
        "OperatingIncomeLoss": {"units": {us: [
            duration("2025-07-01", "2025-09-30", 100),
            duration("2026-07-01", "2026-09-30", 180)]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {us: [
            duration("2026-07-01", "2026-09-30", 220)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {us: [
            duration("2026-07-01", "2026-09-30", 40)]}},
        "StockholdersEquity": {"units": {us: [instant("2026-09-30", 2000)]}},
        "LongTermDebtNoncurrent": {"units": {us: [instant("2026-09-30", 600)]}},
        "LongTermDebtCurrent": {"units": {us: [instant("2026-09-30", 100)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {us: [instant("2026-09-30", 300)]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            duration("2025-10-01", "2025-12-31", 0.8),
            duration("2025-12-31", "2026-03-31", 1),
            duration("2026-04-01", "2026-06-30", 1.2),
            duration("2026-07-01", "2026-09-30", 1.4)]}},
    }}}


class SecFinancialsTests(unittest.TestCase):
    def test_reported_quarter_is_read_with_its_comparative_period(self):
        result = financials(companyfacts(), datetime(2026, 11, 20).date())
        self.assertEqual(result["quarter"], "2026-09")
        self.assertEqual(result["revenue_amount"], 1200)
        self.assertEqual(result["operating_margin_pct"], 15)
        self.assertAlmostEqual(result["revenue_yoy_pct"], 20)
        self.assertAlmostEqual(result["operating_margin_yoy_pp"], 5)
        self.assertAlmostEqual(result["operating_cash_conversion"], 220 / 180)
        self.assertAlmostEqual(result["free_cash_flow_margin_pct"], 100 * 180 / 1200)
        self.assertAlmostEqual(result["net_debt_to_equity"], (700 - 300) / 2000)
        self.assertEqual(result["financial_source"], "SEC XBRL companyfacts")

    def test_missing_statement_is_not_invented(self):
        document = companyfacts()
        document["facts"]["us-gaap"].pop("OperatingIncomeLoss")
        self.assertEqual(financials(document, datetime(2026, 11, 20).date()), {})

    def test_a_period_that_has_not_ended_is_not_used(self):
        document = companyfacts()
        for row in document["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]:
            row["end"] = "2027-01-31"
            row["start"] = "2026-11-01"
        self.assertEqual(financials(document, datetime(2026, 11, 20).date()), {})

    def test_trailing_four_quarter_eps_uses_only_reported_quarters(self):
        result = financials(companyfacts(), datetime(2026, 11, 20).date(), price=56.0)
        self.assertEqual(result["trailing_eps"], 4.4)
        self.assertAlmostEqual(result["trailing_pe"], 56 / 4.4)
        self.assertIn("four-quarter EPS", result["valuation_source"])


if __name__ == "__main__":
    unittest.main()
