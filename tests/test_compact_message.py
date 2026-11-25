"""The short alert the owner asked for on 2026-11-25.

\"띄울거면 간단하게적어 종목, 호재,목표가 이정도만\" — the 07:50 preopen alert had carried
twelve identical '장 마감 · 호가 미확인' rows (before the 09:00 open), the same three-line
sell-rule paragraph under each of five plans, and a repeated market-regime exception, and
Telegram cut it into two parts in the middle of a company.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import scanner
from telegram_text import split_telegram_text

KST = ZoneInfo("Asia/Seoul")
AT = datetime(2026, 11, 25, 8, 50, tzinfo=KST)


def row(symbol="009150.KS", name="삼성전기", score=65.2, floor=732000, limit=778000,
        bull=960000, stop=0.03, strategy="earnings_catalyst", **extra):
    base = dict(market="KR", symbol=symbol, name=name,
                signal=dict(confirmed=False),
                indicators=dict(price=760000.0, volume_ratio=1.9, ma20=700000.0,
                                data_date="2026-11-24"),
                company_forecast=dict(composite_score=score, base_price=850000.0,
                                      bull_price=bull, caveats=["valuation_outlook"]),
                preopen_signal=dict(status="conditional_buy", strategy=strategy,
                                    entry_limit=limit, entry_floor=floor,
                                    reference_date="2026-11-24",
                                    expires_session="2026-11-25",
                                    take_profit_pct=6, stop_pct=stop,
                                    catalyst=dict(summary="2026년 3분기 영업이익 전년 대비 +110% (DART 실적 공시)",
                                                  observed_at="2026-11-13T18:00:00+09:00"),
                                    market_regime_lifted=True))
    base.update(extra)
    return base


def report(*rows, **extra):
    base = dict(results=list(rows), sectors=[], preopen_session="2026-11-25",
                market_regime={"KR": {"above_ma20": False, "observed": True,
                                      "headwind_skipped": 3}})
    base.update(extra)
    return base


def message(report_obj, market="KRX"):
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake",
                                   "TELEGRAM_CHAT_ID": "fake",
                                   "DELIVERY_MARKET": market}), \
            patch("scanner.delivery_status", return_value=(True, "test")), \
            patch("scanner.requests.post") as post:
        post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
        scanner.send_telegram_summary(report_obj)
    return post.call_args.kwargs["json"]["text"]


class ShortPlanTests(unittest.TestCase):
    def test_a_plan_is_name_hope_and_price(self):
        text = scanner.telegram_candidate_line(row())
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("삼성전기 (009150)", lines[0])
        self.assertIn("종합 65.2", lines[0])
        self.assertTrue(lines[1].strip().startswith("호재:"))
        self.assertIn("매수 732,000원~778,000원", lines[2])
        self.assertIn("목표 960,000원", lines[2])
        self.assertIn("손절 −3%", lines[2])
        self.assertIn("(11/24 종가)", lines[2])

    def test_the_repeated_boilerplate_is_gone(self):
        text = message(report(row()))
        for gone in ["단기 급등 시 6% 매도 대신 트레일링 보유",
                     "거래량 급감·고점 대비 하락 신호 시 그 가격에 청산",
                     "장초반 시가 아래·동시간 거래량 1.2배 미달이면 진입하지 않음",
                     "지수 20일선 아래 · 확정 공시 근거로 예외 적용"]:
            self.assertNotIn(gone, text)

    def test_the_guard_and_the_exception_are_said_once(self):
        text = message(report(row(), row(symbol="095610.KQ", name="테스")))
        self.assertEqual(text.count("장초반 시가 아래"), 1)
        self.assertEqual(text.count("지수 20일선 아래"), 1)
        self.assertIn("자료 미확보 종목은 점수에 감점 반영", text)

    def test_a_closed_or_unopened_market_is_not_said_under_every_name(self):
        report_obj = report(row(), row(symbol="095610.KQ", name="테스"))
        for item in report_obj["results"]:
            item["live_quote"] = dict(status="preopen", market="KR")
        report_obj["live_quotes"] = {"preopen": 2, "observed": 0, "market_closed": 0}
        text = message(report_obj)
        self.assertNotIn("장 마감", text)
        self.assertEqual(text.count("개장 전"), 1)

    def test_a_korean_alert_does_not_carry_the_us_regime_count(self):
        text = message(report(results=[row()],
                              market_regime={"KR": {"above_ma20": False, "observed": True,
                                                    "headwind_skipped": 3},
                                             "US": {"above_ma20": False, "observed": True,
                                                    "headwind_skipped": 8}}))
        self.assertIn("시장 국면(KOSPI)", text)
        self.assertNotIn("S&P500", text)


class SplitTests(unittest.TestCase):
    def test_a_split_never_cuts_inside_a_company_block(self):
        blocks = []
        for index in range(40):
            blocks.append(f"• 회사{index} (00000{index})\n  호재: 근거 {index}\n  매수 10,000원 · 목표 11,000원 · 손절 −3%")
        text = "\n\n".join(blocks)
        parts = split_telegram_text(text, limit=500)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 500)
            for block in part.split("\n\n"):
                lines = [line for line in block.splitlines() if line.strip()]
                self.assertEqual(len(lines) % 3, 0, part)

    def test_a_single_oversized_block_is_still_split(self):
        parts = split_telegram_text("x" * 900, limit=200)
        self.assertTrue(all(len(part) <= 200 for part in parts))
        self.assertEqual("".join(parts), "x" * 900)


if __name__ == "__main__":
    unittest.main()
