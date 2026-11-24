"""The live quote a report attaches to the names it prints (2026-11-24 owner request)."""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
import unittest

import live_quote
from scanner import quality_watch_line, telegram_candidate_line

KST = ZoneInfo("Asia/Seoul")
OPEN_AT = datetime(2026, 11, 24, 10, 0, tzinfo=KST)      # KRX session
CLOSED_AT = datetime(2026, 11, 24, 16, 0, tzinfo=KST)    # after the 15:30 close
SATURDAY = datetime(2026, 11, 21, 10, 0, tzinfo=KST)


def plan_row(symbol="095610.KQ", name="테스", market="KR", **extra):
    row = dict(market=market, symbol=symbol, name=name,
               indicators=dict(price=80000.0, volume_ratio=1.75, data_date="2026-11-23"),
               preopen_signal=dict(status="conditional_buy", strategy="earnings_catalyst",
                                   track=None, entry_limit=82000.0, entry_floor=76900.0,
                                   reference_date="2026-11-23", composite_score=64.9,
                                   catalyst=dict(summary="2026년 3분기 영업이익 +192%"),
                                   label="실적·호재 촉매"),
               company_forecast=dict(composite_score=64.9, caveats=[],
                                     base_price=88_000.0, bull_price=96_000.0))
    row.update(extra)
    return row


class SessionStateTests(unittest.TestCase):
    def test_the_korean_session_is_read_from_the_exchange_calendar(self):
        self.assertEqual(live_quote.session_state("KR", OPEN_AT), "open")
        self.assertEqual(live_quote.session_state("KR", CLOSED_AT), "closed")
        self.assertEqual(live_quote.session_state("KR", SATURDAY), "closed")

    def test_an_unknown_timezone_is_not_read_as_open(self):
        self.assertEqual(live_quote.session_state("KR", datetime(2026, 11, 24, 10, 0)),
                         "unknown")


class QuoteFetchTests(unittest.TestCase):
    def test_a_closed_market_is_reported_without_calling_the_provider(self):
        class Client:
            def __getattr__(self, item):
                raise AssertionError("a closed market must not call the provider")

        with patch("rest_fallback.domestic_quote") as domestic:
            record = live_quote.fetch("KR", "095610.KQ", CLOSED_AT, Client())
        domestic.assert_not_called()
        self.assertEqual(record["status"], "market_closed")
        self.assertEqual(record["reason"], "outside_the_trading_session")

    def test_an_open_session_returns_the_order_book_with_its_own_time(self):
        quote = dict(bid=81_900, ask=82_000, spread_pct=0.12, price=81_950,
                     source="KIS REST inquire-price")
        with patch("rest_fallback.domestic_quote", return_value=quote):
            record = live_quote.fetch("KR", "095610.KQ", OPEN_AT, object())
        self.assertEqual(record["status"], "observed")
        self.assertEqual((record["bid"], record["ask"], record["price"]), (81_900, 82_000, 81_950))
        self.assertEqual(record["checked_at"], OPEN_AT.isoformat())
        self.assertEqual(record["meaning"], live_quote.MEANING)

    def test_a_provider_failure_or_a_missing_book_is_not_raised(self):
        with patch("rest_fallback.domestic_quote", return_value=None):
            self.assertEqual(live_quote.fetch("KR", "095610.KQ", OPEN_AT, object())["status"],
                             "unavailable")
        with patch("rest_fallback.domestic_quote", side_effect=ValueError("bad")):
            record = live_quote.fetch("KR", "095610.KQ", OPEN_AT, object())
        self.assertEqual((record["status"], record["reason"]), ("unavailable", "ValueError"))

    def test_a_missing_credential_is_reported_not_raised(self):
        with patch("live_quote._client", return_value=(None, "RuntimeError")):
            record = live_quote.fetch("KR", "095610.KQ", OPEN_AT)
        self.assertEqual(record["status"], "credentials_missing")


class AttachTests(unittest.TestCase):
    def test_plans_and_watch_names_carry_the_quote_and_limitations_say_so(self):
        report = dict(results=[plan_row()], limitations=[
            "완료된 거래일 일봉 기준이며 일중 VWAP·실시간 호가 미포함"],
            quality_watch=[dict(market="KR", symbol="377300.KS", name="카카오페이",
                                grade="supply_inflow_quality", fundamental_score=80.3)])
        quote = dict(bid=81_900, ask=82_000, spread_pct=0.12, price=81_950, source="KIS")
        with patch("rest_fallback.domestic_quote", return_value=quote):
            summary = live_quote.attach(report, OPEN_AT, client=object())
        self.assertEqual(summary["observed"], 2)
        self.assertEqual(report["results"][0]["live_quote"]["status"], "observed")
        self.assertEqual(report["quality_watch"][0]["live_quote"]["ask"], 82_000)
        text = "\n".join(report["limitations"])
        self.assertIn("현재 호가는 KIS REST로 조회", text)
        self.assertNotIn("실시간 호가 미포함", text)

    def test_a_closed_market_says_so_instead_of_a_quote(self):
        report = dict(results=[plan_row()], limitations=["완료된 거래일 일봉 기준이며 일중 VWAP·실시간 호가 미포함"])
        summary = live_quote.attach(report, CLOSED_AT, client=object())
        self.assertEqual(summary["market_closed"], 1)
        self.assertEqual(report["results"][0]["live_quote"]["status"], "market_closed")
        text = "\n".join(report["limitations"])
        self.assertIn("장 마감(또는 휴장)으로 현재 호가는 조회하지 않았고", text)
        self.assertIn("실시간 매수 확정은 아님", text)
        self.assertEqual(text.count("VWAP"), 1)

    def test_a_second_pass_over_the_same_report_does_not_repeat_the_line(self):
        # The requested report loads the scan's report (already rewritten once) and
        # attaches quotes again; the limitation must stay a single line.
        report = dict(results=[plan_row()], limitations=["백테스트 미구현"])
        live_quote.attach(report, CLOSED_AT, client=object())
        live_quote.attach(report, CLOSED_AT, client=object())
        text = "\n".join(report["limitations"])
        self.assertEqual(text.count("장 마감(또는 휴장)으로"), 1)
        self.assertEqual(text.count("VWAP"), 1)

    def test_the_call_count_is_bounded_per_market(self):
        rows = [plan_row(symbol=f"{100000 + index}.KS", name=f"종목{index}") for index in range(20)]
        report = dict(results=rows, limitations=[])
        quote = dict(bid=10_000, ask=10_010, spread_pct=0.1, price=10_005)
        with patch("rest_fallback.domestic_quote", return_value=quote) as domestic:
            summary = live_quote.attach(report, OPEN_AT, limit=3, client=object())
        self.assertEqual(summary["observed"], 3)
        self.assertEqual(domestic.call_count, 3)
        self.assertNotIn("live_quote", rows[-1])


class QuoteLineTests(unittest.TestCase):
    def test_an_observed_quote_is_one_labelled_line(self):
        text = live_quote.quote_line(dict(status="observed", market="KR", price=81_950,
                                          bid=81_900, ask=82_000, spread_pct=0.12,
                                          checked_at=OPEN_AT.isoformat()))
        self.assertIn("현재가 81,950원", text)
        self.assertIn("매수 81,900원 / 매도 82,000원", text)
        self.assertIn("스프레드 0.12%", text)
        self.assertIn("체결 아님", text)

    def test_a_us_quote_is_written_in_dollars(self):
        text = live_quote.quote_line(dict(status="observed", market="US", price=41.2,
                                          bid=41.19, ask=41.21, spread_pct=0.05))
        self.assertIn("$41.20", text)

    def test_a_fallback_state_is_named(self):
        self.assertEqual(live_quote.quote_line(dict(status="market_closed")), "장 마감 · 호가 미확인")
        self.assertEqual(live_quote.quote_line(dict(status="credentials_missing")),
                         "호가 조회 자격증명 없음")
        self.assertIsNone(live_quote.quote_line(None))

    def test_the_candidate_line_carries_the_quote(self):
        row = plan_row()
        row["live_quote"] = dict(status="market_closed")
        self.assertIn("장 마감 · 호가 미확인", telegram_candidate_line(row))
        row["live_quote"] = dict(status="observed", market="KR", price=81_950, bid=81_900,
                                 ask=82_000, spread_pct=0.12, checked_at=OPEN_AT.isoformat())
        text = telegram_candidate_line(row)
        self.assertIn("현재가 81,950원", text)
        self.assertIn("체결 아님", text)

    def test_the_quality_line_carries_the_quote(self):
        entry = dict(name="카카오페이", symbol="377300.KS", grade="supply_inflow_quality",
                     fundamental_score=80.3, market="KR",
                     live_quote=dict(status="observed", market="KR", price=45_000, bid=44_950,
                                     ask=45_050, spread_pct=0.22))
        self.assertIn("현재가 45,000원", quality_watch_line(entry))


if __name__ == "__main__":
    unittest.main()
