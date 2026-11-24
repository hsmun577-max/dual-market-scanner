"""DART 공시 목록: 주기보고서의 공시일 복원과 3건 절단 제거 (2026-11-24).

테스(095610.KQ) 사례가 기준이다. 3분기 영업이익이 전년 대비 +192.5%였는데도 재료가 잡히지
않았고, 원인은 실적이 아니라 목록이었다:

* `dart_disclosures`가 최근 3건만 남겨서, IR·지분·수주 공시가 자리를 채우면 분기보고서가
  목록에서 사라졌다.
* 주기보고서는 400일 창으로 따로 받아 오면서 접수번호 앞 8자리(공시일)를 버려, 재무제표는
  정확한데 그 공시 시점만은 증거로 쓸 수 없었다.

이 테스트는 두 결함을 함께 고정한다. 점수·문턱·시장 게이트는 건드리지 않는다.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from catalyst_track import (CATALYST_FRESH_HOURS, EARNINGS_CATALYST_FRESH_DAYS,
                            EARNINGS_CATALYST_FRESH_HOURS, confirmed_catalyst, freshness_hours)
from kis_intraday import (DART_DISCLOSURE_WINDOW_DAYS, DART_MAX_FILINGS, dart_disclosures,
                          dart_financial_reports, dart_receipt_date, merge_periodic_filings)

KST = timezone(timedelta(hours=9))
# 2026-11-24 09:30 KST: the session the 3Q reports were ten days old in.
NOW = datetime(2026, 11, 24, 9, 30, tzinfo=KST)


def response(body):
    item = Mock(status_code=200)
    item.json.return_value = body
    item.raise_for_status.return_value = None
    return item


def disclosure(day, receipt, title):
    return dict(rcept_dt=day, rcept_no=receipt, report_nm=title)


def recent_events(count=5):
    """IR·지분·수주 공시: 3분기 보고서보다 최근이라 목록 앞자리를 채운다."""
    titles = ["주요사항보고서(자기주식취득결정)", "주요사항보고서(전환사채권발행결정)",
              "기업설명회(IR)개최", "임원ㆍ주요주주특정증권등소유상황보고서",
              "단일판매ㆍ공급계약체결"]
    return [disclosure("20261120", f"20261120{i:06d}", titles[i - 1])
            for i in range(1, count + 1)]


def quarterly():
    return disclosure("20261114", "20261114000123", "분기보고서 (2026.09)")


class ReceiptDateTests(unittest.TestCase):
    def test_the_receipt_number_carries_the_filing_date(self):
        self.assertEqual(dart_receipt_date("20261114000123"), "2026-11-14")
        for bad in ("", "2026-11-14", "20261399000123", "202611", None):
            self.assertIsNone(dart_receipt_date(bad), bad)


class FinancialReportTests(unittest.TestCase):
    def test_a_periodic_report_keeps_its_disclosure_date(self):
        body = {"status": "000", "list": [
            dict(rcept_no="20261114000123", report_nm="분기보고서 (2026.09)"),
            dict(rcept_no="20260315000456", report_nm="사업보고서 (2025.12)"),
            dict(rcept_no="bad", report_nm="분기보고서 (2025.09)")]}
        with patch("kis_intraday.requests.get", return_value=response(body)):
            reports = dart_financial_reports("key", "00126380", NOW)
        self.assertEqual([item["filing_date"] for item in reports], ["2026-11-14", "2026-03-15"])
        self.assertEqual(reports[0]["form"], "분기보고서 (2026.09)")
        self.assertTrue(reports[0]["url"].endswith("20261114000123"))


class DisclosureListTests(unittest.TestCase):
    def listed(self, rows):
        with patch("kis_intraday.requests.get", return_value=response({"status": "000", "list": rows})), \
                patch("kis_intraday.dart_contract_details", return_value={}):
            return dart_disclosures("key", "00126380", NOW)

    def test_the_quarterly_report_survives_three_or_more_event_filings(self):
        shown, _risks = self.listed([*recent_events(), quarterly()])
        forms = [filing["form"] for filing in shown]
        # 예전 코드는 3건만 남겨 분기보고서가 목록 밖으로 밀려났다.
        self.assertIn("분기보고서 (2026.09)", forms)
        self.assertGreater(len(shown), 3)
        self.assertLessEqual(len(shown), DART_MAX_FILINGS + 1)
        # 날짜 내림차순 계약은 유지한다(가장 최근 공시가 먼저).
        dates = [filing["filing_date"] for filing in shown]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_the_window_covers_a_quarter(self):
        with patch("kis_intraday.requests.get", return_value=response({"status": "000", "list": []})) as get:
            dart_disclosures("key", "00126380", NOW)
        params = get.call_args.kwargs["params"]
        expected = (NOW - timedelta(days=DART_DISCLOSURE_WINDOW_DAYS)).strftime("%Y%m%d")
        # 14일 창은 45일 전 분기보고서를 보지 못했다.
        self.assertEqual(params["bgn_de"], expected)
        self.assertGreaterEqual(DART_DISCLOSURE_WINDOW_DAYS, 60)

    def test_no_disclosure_is_not_a_failure(self):
        self.assertEqual(self.listed([]), ([], []))


class MergeTests(unittest.TestCase):
    def test_the_periodic_report_is_merged_once_and_dated(self):
        existing = [dict(filing_date="2026-11-20", form="기업설명회(IR)개최",
                         url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261120000001",
                         source="DART")]
        reports = [dict(filing_date="2026-11-14", form="분기보고서 (2026.09)",
                        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261114000123",
                        source="DART")]
        merged = merge_periodic_filings(existing, reports)
        self.assertEqual([filing["filing_date"] for filing in merged], ["2026-11-20", "2026-11-14"])
        self.assertEqual(len(merge_periodic_filings(merged, reports)), 2)

    def test_a_report_without_a_date_is_not_merged(self):
        self.assertEqual(merge_periodic_filings([], [dict(form="분기보고서 (2026.09)", url="u")]), [])


class TesCatalystTests(unittest.TestCase):
    """테스: 공시 날짜가 복원되면 +192.5% 실적 재료가 10일 뒤에도 읽힌다."""

    def company(self, filings):
        return dict(market="KR", quarter="2026-09", operating_income_amount=2.461e10,
                    operating_income_yoy_pct=192.5, operating_margin_pct=12.3,
                    financial_source="DART 연결", financial_filing_url=(
                        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261114000123"),
                    filings=filings, news=[], event_analysis=dict(economic_graph={}))

    def merged_filings(self):
        rows = [*recent_events(), quarterly()]
        with patch("kis_intraday.requests.get", return_value=response({"status": "000", "list": rows})), \
                patch("kis_intraday.dart_contract_details", return_value={}):
            shown, _risks = dart_disclosures("key", "00126380", NOW)
        return shown

    def test_the_restored_date_makes_the_earnings_jump_readable(self):
        found = confirmed_catalyst(self.company(self.merged_filings()), NOW, "KR")
        self.assertTrue(found["eligible"])
        self.assertEqual(found["kind"], "earnings_surprise")
        self.assertEqual(found["metrics"]["operating_income_yoy_pct"], 192.5)
        self.assertIn("192", found["summary"])
        self.assertEqual(found["source"], "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261114000123")

    def test_without_the_quarterly_report_there_is_no_date_to_read(self):
        # 예전 목록(3건 절단)과 같은 상태: 실적 급증인데 근거 공시가 없다.
        old_list = sorted(self.merged_filings(),
                          key=lambda filing: filing["filing_date"], reverse=True)[:3]
        self.assertFalse(any("분기보고서" in filing["form"] for filing in old_list))
        self.assertFalse(confirmed_catalyst(self.company(old_list), NOW, "KR")["eligible"])

    def test_the_earnings_window_is_a_quarter_but_a_contract_is_not(self):
        self.assertEqual(EARNINGS_CATALYST_FRESH_DAYS, 21)
        self.assertEqual(EARNINGS_CATALYST_FRESH_HOURS, 504)
        self.assertEqual(freshness_hours("earnings_surprise"), EARNINGS_CATALYST_FRESH_HOURS)
        self.assertEqual(freshness_hours("confirmed_contract"), CATALYST_FRESH_HOURS)
        self.assertEqual(freshness_hours("confirmed_news"), CATALYST_FRESH_HOURS)
        self.assertEqual(freshness_hours("unknown"), CATALYST_FRESH_HOURS)


if __name__ == "__main__":
    unittest.main()
