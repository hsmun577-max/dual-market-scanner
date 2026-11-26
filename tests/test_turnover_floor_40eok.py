"""국내 최소 거래대금 50억 → 40억 (2026-11-26 혜선 님 지시 "거래대금 기준 40억으로 낮춰").

한 값만 내리면 티어 경계가 어긋난다. 모멘텀 경로의 하한이 40억이 되면
(1) 40억~50억 종목은 수급·펀더멘탈 티어가 아니라 모멘텀 경로로 가야 하고,
(2) 그 위에 얹힌 중기 +30% 눌림 밴드의 상단도 40억에서 멈춰야 하며,
(3) 급등 긴급 이슈의 당일 거래대금 문턱도 같은 규모여야 한다.
미국장($10M)과 매매·손절·점수 문턱은 이 변경에 포함되지 않는다.
"""
import unittest

from candidate_selection import (MIN_AVERAGE_TURNOVER, RESEARCH_MIN_AVERAGE_TURNOVER,
                                 liquidity_rejection_reason, quality_watch_reason)
from mid_term_watch import MAX_TURNOVER, MIN_TURNOVER, reason as mid_term_reason
from urgent_issues import MIN_TODAY_VALUE, _liquid


def row(market="KR", average=4_000_000_000, today=None, **overrides):
    """A listed stock with a 20-day average turnover and a same-day traded value."""
    price = 10_000.0 if market == "KR" else 100.0
    today = average if today is None else today
    indicators = dict(price=price, volume=today / price, volume_ratio=1.8,
                      ma20=price * 0.95, ma60=price * 0.9, return_5d=0.03,
                      return_20d=0.06, relative_strength=0.02,
                      average_turnover_20d=average, obv_rising=True,
                      breakdown=False, rebound=False, overheated=False,
                      market_above_ma20=True)
    indicators.update(overrides.pop("indicators", {}))
    base = dict(market=market,
                symbol="005930.KS" if market == "KR" else "AAPL",
                name="삼성전자" if market == "KR" else "Apple Inc",
                asset_type="stock", indicators=indicators)
    base.update(overrides)
    return base


class FloorValueTests(unittest.TestCase):
    def test_the_korean_floor_is_forty_eok_and_the_us_floor_is_untouched(self):
        self.assertEqual(MIN_AVERAGE_TURNOVER["KR"], 4_000_000_000)
        self.assertEqual(MIN_AVERAGE_TURNOVER["US"], 10_000_000)
        # The research tier's own 10억 bottom did not move with the momentum floor.
        self.assertEqual(RESEARCH_MIN_AVERAGE_TURNOVER["KR"], 1_000_000_000)
        self.assertEqual(MIN_TODAY_VALUE["KR"], 4_000_000_000)
        self.assertEqual(MIN_TODAY_VALUE["US"], 10_000_000)


class LiquidityTests(unittest.TestCase):
    def test_forty_eok_passes_and_just_under_it_is_refused(self):
        self.assertIsNone(liquidity_rejection_reason(row(average=4_000_000_000), "KR"))
        self.assertEqual(liquidity_rejection_reason(row(average=3_999_999_999), "KR"),
                         "insufficient_liquidity")

    def test_the_band_that_used_to_be_dropped_now_reaches_the_momentum_path(self):
        # 45억 averaged 20-day turnover was `insufficient_liquidity` while the floor was 50억.
        self.assertIsNone(liquidity_rejection_reason(row(average=4_500_000_000), "KR"))
        self.assertIsNone(liquidity_rejection_reason(row(average=5_000_000_000), "KR"))

    def test_the_same_day_value_also_has_to_clear_the_lower_floor(self):
        self.assertIsNone(liquidity_rejection_reason(
            row(average=4_500_000_000, today=4_000_000_000), "KR"))
        self.assertEqual(liquidity_rejection_reason(
            row(average=4_500_000_000, today=3_999_999_999), "KR"), "insufficient_liquidity")

    def test_the_us_floor_keeps_its_ten_million_dollars(self):
        self.assertIsNone(liquidity_rejection_reason(row("US", average=10_000_000), "US"))
        self.assertEqual(liquidity_rejection_reason(row("US", average=9_999_999), "US"),
                         "insufficient_liquidity")


class TierHandoffTests(unittest.TestCase):
    def test_the_quality_tier_now_ends_at_forty_eok(self):
        from candidate_selection import MIN_AVERAGE_TURNOVER as floor
        self.assertIsNone(quality_watch_reason(row(average=floor["KR"] - 1), "KR"))
        self.assertEqual(quality_watch_reason(row(average=floor["KR"]), "KR"),
                         "above_momentum_turnover")
        self.assertEqual(quality_watch_reason(row(average=4_500_000_000), "KR"),
                         "above_momentum_turnover")

    def test_the_mid_term_band_top_follows_the_momentum_floor(self):
        self.assertEqual(MAX_TURNOVER, MIN_AVERAGE_TURNOVER["KR"])
        self.assertEqual(MIN_TURNOVER, 1_000_000_000)
        pullback = dict(return_20d=-0.15, rsi14=41.0, relative_strength=0.02)
        # 39억: below the momentum floor, so it is read on the mid-term band.
        self.assertIsNone(mid_term_reason(row(average=3_900_000_000, indicators=pullback), "KR"))
        # 41억·45억: now the momentum path's; the mid-term band must not claim them too.
        for average in (4_100_000_000, 4_500_000_000):
            self.assertEqual(mid_term_reason(row(average=average, indicators=pullback), "KR"),
                             "turnover_outside_research_band")


class UrgentIssueLiquidityTests(unittest.TestCase):
    def test_the_surge_day_value_uses_the_same_forty_eok_bar(self):
        self.assertTrue(_liquid(row(average=None, today=4_000_000_000), "KR"))
        self.assertFalse(_liquid(row(average=3_000_000_000, today=3_999_999_999), "KR"))
        # The 20-day average branch of the same check follows the same floor.
        self.assertTrue(_liquid(row(average=4_000_000_000, today=1_000_000_000), "KR"))


if __name__ == "__main__":
    unittest.main()
