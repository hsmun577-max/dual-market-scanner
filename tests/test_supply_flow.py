import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import supply_flow

AT = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def window(*flows):
    """(foreign_qty, institution_qty, close, volume, holding) rows, oldest first."""
    rows = []
    for index, (foreign, institution, close, volume, holding) in enumerate(flows):
        rows.append({"date": f"2026-11-{17 + index}",
                     "close": close, "volume": volume,
                     "foreign_qty": foreign, "institution_qty": institution,
                     "foreign_holding_pct": holding})
    return rows


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class SupplyScoreTests(unittest.TestCase):
    def test_steady_foreign_and_institution_buying_reads_as_inflow(self):
        rows = window((-0, -0, 30000, 1_000_000, 23.71),
                      (4000, 3000, 30100, 1_000_000, 23.75),
                      (5000, 2500, 30200, 1_000_000, 23.80),
                      (3500, 4000, 30150, 1_000_000, 23.83),
                      (4200, 3800, 30050, 1_000_000, 23.86))
        reading = supply_flow.score_rows(rows)
        self.assertGreater(reading["score"], supply_flow.INFLOW_SCORE)
        self.assertEqual(supply_flow.direction_of(reading["score"]), "inflow")
        self.assertEqual(reading["current_buy_streak"], 4)
        self.assertGreater(reading["net_value"], 0)
        # quantity x close, not a provider amount field
        self.assertAlmostEqual(reading["net_value"], (4000 + 3000 + 5000 + 2500 + 3500 + 4000 + 4200 + 3800)
                               * 30000, delta=3e8)

    def test_net_selling_reads_as_outflow(self):
        rows = window((-6000, -7000, 31000, 1_000_000, 23.71),
                      (-3000, -4000, 30500, 1_000_000, 23.60),
                      (-2000, -1000, 30050, 1_000_000, 23.52))
        reading = supply_flow.score_rows(rows)
        self.assertLess(reading["score"], supply_flow.OUTFLOW_SCORE)
        self.assertEqual(supply_flow.direction_of(reading["score"]), "outflow")

    def test_a_single_large_print_scores_below_persistent_buying(self):
        one_day = window((0, 0, 30000, 1_000_000, 23.7),
                         (0, 0, 30000, 1_000_000, 23.7),
                         (0, 0, 30000, 1_000_000, 23.7),
                         (0, 0, 30000, 1_000_000, 23.7),
                         (20000, 20000, 30000, 1_000_000, 23.9))
        persistent = window((8000, 8000, 30000, 1_000_000, 23.7),
                            (8000, 8000, 30000, 1_000_000, 23.7),
                            (8000, 8000, 30000, 1_000_000, 23.7),
                            (8000, 8000, 30000, 1_000_000, 23.7),
                            (8000, 8000, 30000, 1_000_000, 23.7))
        self.assertGreater(supply_flow.score_rows(persistent)["score"],
                           supply_flow.score_rows(one_day)["score"])

    def test_rows_without_a_price_do_not_carry_a_flow(self):
        self.assertIsNone(supply_flow.score_rows([]))
        self.assertIsNone(supply_flow.score_rows([{"date": "2026-11-21", "close": None,
                                                   "volume": 10, "foreign_qty": 5,
                                                   "institution_qty": 5}]))
        self.assertIsNone(supply_flow.direction_of(None))


class NaverSourceTests(unittest.TestCase):
    payload = [{"itemCode": "004000", "bizdate": "20261121", "closePrice": "31,000",
                "accumulatedTradingVolume": "35,492", "foreignerPureBuyQuant": "-2,271",
                "organPureBuyQuant": "-7,418", "foreignerHoldRatio": "23.73%"},
               {"itemCode": "004000", "bizdate": "20261120", "closePrice": "30,200",
                "accumulatedTradingVolume": "12,000", "foreignerPureBuyQuant": "+1,299",
                "organPureBuyQuant": "+400", "foreignerHoldRatio": "23.80%"}]

    def test_naver_trend_parses_signed_quantities_and_percentages(self):
        with patch("supply_flow.requests.get", return_value=Response(self.payload)):
            rows = supply_flow.naver_trend("004000", sessions=2)
        self.assertEqual([row["date"] for row in rows], ["2026-11-20", "2026-11-21"])
        self.assertEqual(rows[0]["close"], 30200.0)
        self.assertEqual(rows[0]["foreign_qty"], 1299.0)
        self.assertEqual(rows[1]["institution_qty"], -7418.0)
        self.assertEqual(rows[1]["foreign_holding_pct"], 23.73)

    def test_domestic_evidence_uses_naver_when_kis_has_no_credentials(self):
        with patch("supply_flow._kis_client", return_value=None), \
             patch("supply_flow.requests.get", return_value=Response(self.payload)):
            evidence = supply_flow.kr_evidence("004000.KS", at=AT)
        self.assertEqual(evidence["status"], "measured")
        self.assertEqual(evidence["source"], "Naver Finance deal trend")
        self.assertEqual(evidence["direction"], "outflow")
        self.assertEqual(evidence["as_of"], "2026-11-21")
        self.assertIn("004000", evidence["symbol"])

    def test_kis_rows_are_read_from_output2_and_win_over_naver(self):
        class Client:
            def __init__(self):
                self.calls = []

            def get_body(self, path, tr_id, params):
                self.calls.append((path, tr_id, params))
                return {"rt_cd": "0", "output2": [
                    {"stck_bsop_date": "20261121", "stck_clpr": "31000", "acml_vol": "35492",
                     "frgn_ntby_qty": "5000", "orgn_ntby_qty": "4000",
                     "frgn_ntby_tr_pbmn": "155000", "orgn_ntby_tr_pbmn": "124000"},
                    {"stck_bsop_date": "20261120", "stck_clpr": "30200", "acml_vol": "12000",
                     "frgn_ntby_qty": "1200", "orgn_ntby_qty": "900"}]}
        client = Client()
        with patch("supply_flow.requests.get") as naver:
            evidence = supply_flow.kr_evidence("004000", at=AT, client=client)
        naver.assert_not_called()
        self.assertEqual(evidence["source"], "KIS inquire-investor")
        self.assertEqual(evidence["window"]["current_buy_streak"], 2)
        self.assertEqual(client.calls[0][1], "FHKST01010900")
        self.assertEqual(client.calls[0][2]["FID_INPUT_ISCD"], "004000")

    def test_an_empty_kis_answer_falls_through_to_naver(self):
        class Empty:
            def get_body(self, path, tr_id, params):
                return {"rt_cd": "0", "output2": []}

        with patch("supply_flow.requests.get", return_value=Response(self.payload)):
            evidence = supply_flow.kr_evidence("004000", at=AT, client=Empty())
        self.assertEqual(evidence["source"], "Naver Finance deal trend")
        self.assertEqual(evidence["attempts"][0]["reason"], "empty_response")

    def test_no_source_at_all_is_reported_as_unavailable(self):
        with patch("supply_flow._kis_client", return_value=None), \
             patch("supply_flow.requests.get", return_value=Response({}, status=500)):
            evidence = supply_flow.kr_evidence("004000", at=AT)
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["reason"], "no_per_issuer_domestic_flow_source")
        self.assertEqual(supply_flow.share_line(evidence), "수급 미확인")


class OverseasSourceTests(unittest.TestCase):
    def test_a_measured_holding_share_is_named_as_holders_not_a_daily_flow(self):
        evidence = supply_flow.us_evidence("AAPL", {"institutional_holding_pct": 62.0,
                                                    "profile_available_at": "2026-11-22T00:00:00+00:00"},
                                           at=AT)
        self.assertEqual(evidence["status"], "measured")
        self.assertEqual(evidence["basis"], "quarterly_institutional_holding_share")
        self.assertIn("기관 보유 62.0%", supply_flow.share_line(evidence))

    def test_without_a_holding_share_the_price_and_volume_proxy_is_labelled(self):
        evidence = supply_flow.us_evidence("AAPL", {}, {"return_5d": .06, "volume_ratio": 2.0,
                                                        "obv_rising": True, "data_date": "2026-11-21"},
                                           at=AT)
        self.assertEqual(evidence["status"], "proxy")
        self.assertEqual(evidence["direction"], "inflow")
        self.assertIn("대용지표", supply_flow.share_line(evidence))

    def test_no_observation_is_unavailable_rather_than_neutral(self):
        evidence = supply_flow.us_evidence("AAPL", {}, {}, at=AT)
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["reason"], "no_per_issuer_overseas_flow_source")


class ShareLineTests(unittest.TestCase):
    def test_line_carries_amount_ratio_and_streak(self):
        rows = window((4000, 3000, 30000, 1_000_000, 23.7),
                      (5000, 2500, 30000, 1_000_000, 23.8),
                      (3500, 4000, 30000, 1_000_000, 23.9))
        evidence = {"status": "measured", "market": "KR", "symbol": "004000",
                    "window": supply_flow.score_rows(rows)}
        line = supply_flow.share_line(evidence)
        self.assertIn("억원", line)
        self.assertIn("거래대금 대비", line)
        self.assertIn("연속 순매수", line)


if __name__ == "__main__":
    unittest.main()
