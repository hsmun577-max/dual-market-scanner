"""The mid-term +30% pullback list: conditions, its own field, its own message section.

The study (``evidence/dual-market-scanner/상승초입-조기포착-전략연구.md`` 2부, 추천 1):
``20일 수익률 ≤ −10%`` + ``RSI(14) ≤ 45`` + ``20일 평균 거래대금 10억~50억`` +
``코스피 대비 20일 상대강도 > 0`` → 60 sessions held +9.54%, +30% touch 58.4%, max
drawdown −15.7% (977 trades). It is a 20~60 session hold with a −15% stop, so it must never
be mixed into the same-day buy alerts: these tests hold it in ``mid_term_watch`` and in its
own message section, with ``entry_signal`` false and ``preopen_signal`` untouched.
"""
from datetime import datetime
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from mid_term_watch import LABEL, attach, format_block, reason
from scanner import send_telegram_summary

KST = ZoneInfo("Asia/Seoul")
AT = datetime(2026, 11, 24, 8, 30, tzinfo=KST)


def values(**overrides):
    base = dict(price=5_000.0, volume=900_000, volume_ratio=0.8, ma20=5_400.0, ma60=5_600.0,
                return_5d=-0.03, return_20d=-0.15, rsi14=41.0, relative_strength=0.02,
                average_turnover_20d=3_000_000_000, obv_rising=False, breakdown=False,
                overheated=False, market_above_ma20=False, data_date="2026-11-21")
    base.update(overrides)
    return base


def row(**overrides):
    base = dict(market="KR", symbol="012510.KS", name="더존비즈온", asset_type="stock",
                signal=dict(confirmed=False, score=0), indicators=values())
    base.update(overrides)
    return base


def report(*items):
    return dict(results=list(items), preopen_session="2026-11-24", sectors=[])


class ConditionTests(unittest.TestCase):
    def test_the_studied_pullback_condition_is_the_candidate(self):
        self.assertIsNone(reason(row(), "KR"))

    def test_each_condition_is_read_on_its_own(self):
        for change, expected in (
                (dict(return_20d=-0.05), "pullback_too_shallow"),
                (dict(rsi14=48.0), "rsi_above_limit"),
                (dict(average_turnover_20d=8_000_000_000), "turnover_outside_research_band"),
                (dict(average_turnover_20d=500_000_000), "turnover_outside_research_band"),
                (dict(relative_strength=-0.01), "weak_relative_strength"),
                (dict(relative_strength=None), "missing_indicators"),
                (dict(rsi14=None), "missing_indicators"),
                (dict(price=700.0), "low_price")):
            self.assertEqual(reason(row(indicators=values(**change)), "KR"), expected, change)

    def test_the_band_edges_are_inclusive(self):
        self.assertIsNone(reason(row(indicators=values(average_turnover_20d=1_000_000_000)), "KR"))
        self.assertIsNone(reason(row(indicators=values(average_turnover_20d=5_000_000_000)), "KR"))
        self.assertIsNone(reason(row(indicators=values(return_20d=-0.10, rsi14=45.0)), "KR"))
        self.assertEqual(reason(row(indicators=values(return_20d=-0.099)), "KR"),
                         "pullback_too_shallow")

    def test_the_turnover_floor_is_the_research_band_not_the_momentum_floor(self):
        # 10억~50억 is below the momentum path's 50억 floor; that is the point of the band.
        from candidate_selection import MIN_AVERAGE_TURNOVER, liquidity_rejection_reason
        item = row()
        self.assertEqual(liquidity_rejection_reason(item, "KR"), "insufficient_liquidity")
        self.assertIsNone(reason(item, "KR"))
        self.assertGreaterEqual(MIN_AVERAGE_TURNOVER["KR"], 5_000_000_000)

    def test_funds_special_securities_and_other_markets_are_not_the_condition(self):
        self.assertEqual(reason(row(asset_type="etf", name="KODEX 200"), "KR"), None)
        from research_asset import is_fund
        self.assertTrue(is_fund(row(asset_type="etf", name="KODEX 200")))
        self.assertEqual(reason(row(name="우리종금우"), "KR"), "special_security")
        self.assertEqual(reason(row(name="미래에셋스팩1호"), "KR"), "special_security")
        self.assertEqual(reason(row(), "US"), "unsupported_market")


class AttachTests(unittest.TestCase):
    def test_the_list_is_separate_and_never_an_entry_signal(self):
        report_ = report(row(), row(symbol="000990.KS", name="DB하이텍",
                                    indicators=values(relative_strength=0.05)))
        entries = attach(report_, AT)
        self.assertEqual([entry["symbol"] for entry in entries], ["000990.KS", "012510.KS"])
        for entry in entries:
            self.assertFalse(entry["entry_signal"])
            self.assertEqual(entry["hold_sessions"], [20, 60])
            self.assertEqual(entry["stop_pct"], 0.15)
        self.assertEqual(report_["mid_term_watch"], entries)
        self.assertNotIn("preopen_signal", report_["results"][0])
        self.assertNotIn("buy_evaluation", report_)
        self.assertEqual(report_["mid_term_watch_scope"]["qualified"], 2)
        self.assertEqual(report_["mid_term_watch_scope"]["entry_signal"], False)

    def test_the_limit_and_the_exclusion_counts_are_recorded(self):
        rows = [row(symbol=f"{index:06d}.KS", indicators=values(relative_strength=index / 100))
                for index in range(1, 12)]
        report_ = report(*rows)
        entries = attach(report_, AT, limit=8)
        self.assertEqual(len(entries), 8)
        # Highest relative strength first, so the strongest pullback leads the list.
        self.assertEqual(entries[0]["symbol"], "000011.KS")
        scope = report_["mid_term_watch_scope"]
        self.assertEqual(scope["qualified"], 11)
        self.assertEqual(scope["listed"], 8)
        self.assertEqual(scope["turnover_band"], [1_000_000_000, 5_000_000_000])

    def test_excluded_rows_are_counted_with_their_reason(self):
        report_ = report(row(), row(symbol="1.KS", indicators=values(rsi14=60.0)),
                         row(symbol="2.KS", asset_type="etf", name="KODEX 200"))
        attach(report_, AT)
        self.assertEqual(report_["mid_term_watch_scope"]["excluded"]["rsi_above_limit"], 1)
        self.assertEqual(report_["mid_term_watch_scope"]["excluded"]["fund"], 1)

    def test_an_empty_scan_keeps_an_empty_list(self):
        report_ = report()
        self.assertEqual(attach(report_, AT), [])
        self.assertEqual(format_block([]), [])
        self.assertEqual(report_["mid_term_watch"], [])


class MessageTests(unittest.TestCase):
    def message(self, report_):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"}), \
             patch("scanner.delivery_status", return_value=(True, "test")), \
             patch("scanner.requests.post") as post:
            post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
            send_telegram_summary(report_)
        return post.call_args.kwargs["json"]["text"]

    def test_the_alert_keeps_the_pullback_list_out_of_the_message(self):
        # 2026-11-24 지시: "매수종목뽑을때 분석자료에 포함하라고 한건데". 목록은 분석 자료에만
        # 남기고 알림 본문에는 빼다.
        report_ = report(row())
        attach(report_, AT)
        text = self.message(report_)
        self.assertNotIn(LABEL, text)
        self.assertNotIn("더존비즈온", text)
        self.assertNotIn("20~60일 보유", text)
        # 계산과 기록은 그대로 남는다.
        self.assertEqual([entry["symbol"] for entry in report_["mid_term_watch"]], ["012510.KS"])
        self.assertEqual(report_["mid_term_watch"][0]["return_20d"], -0.15)

    def test_the_pullback_list_is_never_read_as_a_buy_signal(self):
        report_ = report(row())
        attach(report_, AT)
        text = self.message(report_)
        self.assertIn("분석", text)
        self.assertNotIn("더존비즈온", text)

    def test_without_an_attached_list_the_message_has_no_section(self):
        text = self.message(report(row()))
        self.assertNotIn(LABEL, text)


class BlockTests(unittest.TestCase):
    def test_the_block_states_the_premise_once_and_one_line_per_name(self):
        report_ = report(row())
        attach(report_, AT)
        block = format_block(report_["mid_term_watch"])
        self.assertEqual(block[0], LABEL)
        self.assertIn("당일 매수 신호 아님", block[1])
        self.assertEqual(block[2].count("\n"), 1)
        self.assertTrue(block[2].startswith("• 더존비즈온"))

    def test_an_entry_marked_as_a_signal_is_never_printed_here(self):
        item = dict(symbol="012510.KS", name="더존비즈온", entry_signal=True)
        self.assertEqual(format_block([item]), [])


if __name__ == "__main__":
    unittest.main()
