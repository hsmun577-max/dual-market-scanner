"""비차트 근거 — 공시 촉매 가산 · 리스크 공시 배제 · 수급 선행 관찰."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import nonchart_evidence as nonchart
from nonchart_evidence import (attach_candidate_evidence, attach_supply_lead_watch,
                               catalyst_line, classify, disclosure_evidence, format_block,
                               format_line, is_aligned, observe_supply, priority,
                               risk_exclusion_line, supply_lead_pool, window_start)
from preopen_signals import evaluate_preopen
from scanner import send_telegram_summary

KST = ZoneInfo("Asia/Seoul")
AT = datetime(2026, 11, 27, 9, 0, tzinfo=KST)


def filing(title, when="2026-11-26T10:00:00", disclosure_id=1):
    return dict(title=title, published_at=datetime.fromisoformat(when).replace(tzinfo=KST),
                disclosure_id=disclosure_id, author="KOSCOM")


def fetcher(rows):
    """공시 수집기를 대신하는 함수 — ``fetch(code, start, at)``."""
    return lambda code, start, at: list(rows)


def stock_row(symbol="005500.KS", name="삼진제약", market="KR"):
    """차트 게이트는 통과하지만 패널의 '정배열'은 아닌 기본 행(수급 관찰 후보)."""
    return dict(symbol=symbol, market=market, name=name,
                signal=dict(confirmed=False, score=0),
                indicators=dict(price=19470, volume=337684, volume_ratio=1.9, return_1d=.01,
                                average_turnover_20d=20000000000, ma5=18800, ma20=19000,
                                ma60=19500, return_5d=.02, return_20d=.05,
                                relative_strength=.03, obv_rising=True, breakout=False,
                                data_date="2026-11-26", market_above_ma20=False),
                company_analysis=dict(event_analysis=dict(economic_graph={})),
                company_forecast=dict(assessment_status="complete", generated_at=AT.isoformat(),
                                      composite_score=70, base_price=20600, bull_price=21000,
                                      missing_dimensions=[]))


def aligned_row(symbol="000660.KS", name="SK하이닉스"):
    row = stock_row(symbol, name)
    row["indicators"].update(price=10000, volume=2000000, ma5=10100, ma20=9900, ma60=9800,
                             market_above_ma20=True)
    return row


def trend_rows(foreign_qty=20000, institution_qty=10000, sessions=5, price=10000,
               volume=100000):
    """기본값: 외인 +20,000주 · 기관 +10,000주 · 거래량 10만주 → 강도 30%인 5세션."""
    return [dict(date=f"2026-11-2{day}", close=price, volume=volume,
                 foreign_qty=foreign_qty, institution_qty=institution_qty,
                 foreign_holding_pct=4.0)
            for day in range(2, 2 + sessions)]


class ClassifyTests(unittest.TestCase):
    """공시 제목 분류 — 촉매는 네 종류만, 리스크는 배제 사유."""

    def test_a_supply_contract_is_a_catalyst(self):
        verdict = classify("(주)테스단일판매ㆍ공급계약체결(자율공시)")
        self.assertEqual((verdict["bucket"], verdict["kind"], verdict["label"]),
                         ("catalyst", "contract", "단일판매·공급계약"))

    def test_a_corrected_contract_is_still_a_contract(self):
        # 건설·조선은 정정 공시가 원공시 못지않게 많다 — 계약 사건의 갱신이다.
        verdict = classify("삼성엔지니어링(주)(정정)단일판매ㆍ공급계약체결")
        self.assertEqual((verdict["bucket"], verdict["kind"]), ("catalyst", "contract"))

    def test_a_cancelled_contract_is_not_a_catalyst(self):
        for title in ("단일판매ㆍ공급계약 해지", "단일판매ㆍ공급계약 취소", "공급계약 중단"):
            self.assertNotEqual(classify(title)["bucket"], "catalyst", title)

    def test_a_market_making_contract_is_not_an_order(self):
        # 유동성공급계약은 '공급계약' 문자열이 들어 있지만 시장조성 계약이다(표본 961건).
        self.assertEqual(classify("유동성공급계약의 체결")["bucket"], "other")

    def test_license_clinical_and_buyback_catalysts(self):
        self.assertEqual(classify("(주)X 기술이전 계약 체결")["kind"], "license")
        self.assertEqual(classify("(주)X 임상 3상 결과 발표")["kind"], "clinical")
        self.assertEqual(classify("(주)X 품목허가 획득")["kind"], "clinical")
        self.assertEqual(classify("(주)X 자기주식취득 결정")["kind"], "buyback")

    def test_a_plain_approval_without_a_medical_context_is_not_a_catalyst(self):
        self.assertNotEqual(classify("정관 변경 승인")["bucket"], "catalyst")
        self.assertEqual(classify("주주총회 승인")["bucket"], "noise")

    def test_earnings_filings_are_not_catalysts(self):
        for title in ("삼성전자(주) 영업(잠정)실적(공정공시)", "(주)X 잠정실적 공시",
                      "(주)X 결산실적공시"):
            verdict = classify(title)
            self.assertEqual(verdict["bucket"], "earnings", title)

    def test_noise_filings_are_read_as_neither(self):
        for title in ("삼성전자(주) 주식선물ㆍ주식옵션 2단계 가격제한폭 확대요건 도달(하락)",
                      "(주)X 현금ㆍ현물배당 결정", "(주)X 의결권대리행사 권유",
                      "(주)X 기업설명회(IR) 개최(안내공시)", "(주)X 결산실적공시 예고(안내공시)"):
            self.assertEqual(classify(title)["bucket"], "noise", title)

    def test_risk_filings_are_recognised(self):
        cases = {
            "(주)X 유상증자결정": "dilution",
            "(주)엘앤에프 전환사채권발행결정": "dilution",
            "(주)Y 교환사채권발행결정": "dilution",
            "(주)X 감자결정": "dilution",
            "(주)X 횡령ㆍ배임혐의 발생": "credibility",
            "(주)X 소송등의제기ㆍ신청(일정금액 이상의 청구)": "credibility",
            "(주)X 감사보고서 의견거절": "credibility",
            "(주)X 관리종목 지정": "credibility",
            "(주)X 최대주주 변경": "control",
            "(주)X 자기주식 처분 결정": "control",
        }
        for title, kind in cases.items():
            verdict = classify(title)
            self.assertEqual((verdict["bucket"], verdict["kind"]), ("risk", kind), title)

    def test_buying_back_its_own_convertible_bond_is_not_dilution(self):
        # '전환사채 만기전 취득'은 부채를 줄이는 일이다 — 발행만 희석으로 읽는다.
        self.assertNotEqual(classify("(주)빛과전자 자기 전환사채 만기전 취득 결정")["bucket"],
                            "risk")

    def test_a_product_delisting_is_not_the_issuer_delisting(self):
        verdict = classify("미래에셋증권(주) 상장지수증권 상장폐지(인버스 2X ETN)")
        self.assertEqual(verdict["bucket"], "other")

    def test_a_share_cancellation_is_not_a_risk(self):
        self.assertNotEqual(classify("삼성SDI(주) 주식 소각 결정")["bucket"], "risk")

    def test_an_empty_title_is_not_classified(self):
        self.assertIsNone(classify(""))
        self.assertIsNone(classify(None))


class WindowTests(unittest.TestCase):
    def test_the_window_is_five_trading_sessions(self):
        # 2026-11-27(금) 판단 → 11/23(월)~11/27(금) 다섯 세션.
        self.assertEqual(window_start(AT).date().isoformat(), "2026-11-23")
        self.assertEqual(window_start(AT).utcoffset().total_seconds(), 9 * 3600)

    def test_the_window_skips_the_holidays(self):
        # 2026-10-09(한글날)과 10/05(개천절 대체)는 휴장이다. 10/12(월) 판단의 다섯 세션은
        # 10/12·10/08·10/07·10/06·10/02 → 창의 첫 세션은 10/02다.
        start = window_start(datetime(2026, 10, 12, 9, 0, tzinfo=KST))
        self.assertEqual(start.date().isoformat(), "2026-10-02")

    def test_a_naive_decision_time_is_refused(self):
        with self.assertRaises(ValueError):
            window_start(datetime(2026, 11, 27, 9, 0))


class EvidenceTests(unittest.TestCase):
    def test_only_filings_published_before_the_decision_are_read(self):
        rows = [filing("(주)X 단일판매ㆍ공급계약체결", "2026-11-26T10:00:00"),
                filing("(주)X 유상증자결정", "2026-11-24T09:00:00"),
                filing("(주)X 임상 2상 결과", "2026-11-27T15:00:00", 2),   # 판단 이후
                filing("(주)X 단일판매ㆍ공급계약체결", "2026-11-13T10:00:00", 3)]  # 창 밖
        evidence = disclosure_evidence("005500", AT, "KR", fetch=fetcher(rows))
        self.assertEqual(evidence["status"], "measured")
        self.assertEqual([item["kind"] for item in evidence["catalysts"]], ["contract"])
        self.assertEqual([item["kind"] for item in evidence["risks"]], ["dilution"])
        self.assertEqual(evidence["catalysts"][0]["published_at"], "2026-11-26T10:00:00+09:00")

    def test_earnings_and_noise_are_counted_but_not_shown(self):
        rows = [filing("(주)X 영업(잠정)실적(공정공시)"),
                filing("(주)X 주식선물ㆍ주식옵션 가격제한폭 확대", disclosure_id=2),
                filing("(주)X 임상 3상 승인", disclosure_id=3)]
        evidence = disclosure_evidence("005500", AT, "KR", fetch=fetcher(rows))
        self.assertEqual(evidence["earnings_count"], 1)
        self.assertEqual(evidence["noise_count"], 1)
        self.assertEqual([item["kind"] for item in evidence["catalysts"]], ["clinical"])

    def test_the_strongest_catalyst_comes_first(self):
        rows = [filing("(주)X 자기주식취득 결정", "2026-11-26T15:00:00"),
                filing("(주)X 단일판매ㆍ공급계약체결", "2026-11-24T09:00:00", 2),
                filing("(주)X 기술이전 계약", "2026-11-25T09:00:00", 3)]
        evidence = disclosure_evidence("005500", AT, "KR", fetch=fetcher(rows))
        self.assertEqual([item["kind"] for item in evidence["catalysts"]],
                         ["contract", "license", "buyback"])

    def test_a_provider_failure_is_unavailable_not_empty(self):
        def boom(code, start, at):
            raise RuntimeError("blocked")
        evidence = disclosure_evidence("005500", AT, "KR", fetch=boom)
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["reason"], "RuntimeError")

    def test_an_overseas_issuer_has_no_disclosure_source(self):
        evidence = disclosure_evidence("NVDA", AT, "US")
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["reason"], "no_disclosure_source_for_market")

    def test_the_naver_list_is_parsed_with_the_korean_clock(self):
        payload = [dict(title="(주)X 단일판매ㆍ공급계약체결", datetime="2026-11-26T10:00:00",
                        disclosureId=7, author="KOSCOM")]
        get = Mock(return_value=Mock(status_code=200, json=lambda: payload))
        rows = nonchart.naver_disclosures("005500", AT - timedelta(days=5), AT, get=get)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"].utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(rows[0]["disclosure_id"], 7)
        self.assertEqual(get.call_count, 1)

    def test_the_list_pages_back_only_while_the_window_is_not_covered(self):
        day = "2026-11-26T10:00:00"
        page_one = [dict(title=f"(주)X 공시 {i}", datetime=day, disclosureId=i)
                    for i in range(3)]
        page_two = [dict(title="(주)X 단일판매ㆍ공급계약체결", datetime="2026-11-20T10:00:00",
                         disclosureId=9)]
        responses = [Mock(status_code=200, json=lambda: page_one),
                     Mock(status_code=200, json=lambda: page_two),
                     Mock(status_code=200, json=lambda: [])]
        get = Mock(side_effect=responses)
        rows = nonchart.naver_disclosures("005500", datetime(2026, 11, 23, tzinfo=KST), AT,
                                          size=3, get=get)
        self.assertEqual(len(rows), 4)
        self.assertEqual(get.call_count, 2)


class CandidateEvidenceTests(unittest.TestCase):
    def test_catalyst_and_risk_flags_are_attached_per_candidate(self):
        catalyst = stock_row()
        risky = stock_row("006400.KS", "삼성SDI")
        quiet = stock_row("005930.KS", "삼성전자")
        report = dict(results=[catalyst, risky, quiet])
        rows = {
            "005500": [filing("(주)삼진제약 임상 3상 결과 발표")],
            "006400": [filing("삼성SDI(주) 유상증자결정")],
            "005930": [filing("삼성전자(주) 주식선물ㆍ주식옵션 가격제한폭 확대")],
        }
        summary = attach_candidate_evidence(
            report, AT, fetch=lambda code, start, at: rows.get(code, []))
        self.assertEqual(summary["catalysts"], 1)
        self.assertEqual(summary["risks"], 1)
        self.assertEqual(catalyst["nonchart_catalyst"]["kind"], "clinical")
        self.assertEqual(risky["nonchart_risk"]["kind"], "dilution")
        self.assertNotIn("nonchart_catalyst", quiet)
        self.assertNotIn("nonchart_risk", quiet)

    def test_a_fund_is_not_read_for_disclosures(self):
        fund = stock_row("069500.KS", "KODEX 200")
        fund["asset_type"] = "etf"
        report = dict(results=[fund])
        with patch.object(nonchart, "disclosure_evidence") as read:
            attach_candidate_evidence(report, AT)
        read.assert_not_called()

    def test_an_unavailable_read_leaves_no_flag(self):
        row = stock_row()
        report = dict(results=[row])
        attach_candidate_evidence(report, AT,
                                  fetch=lambda code, start, at: (_ for _ in ()).throw(OSError()))
        self.assertEqual(row["nonchart_evidence"]["status"], "unavailable")
        self.assertNotIn("nonchart_catalyst", row)


class SupplyPoolTests(unittest.TestCase):
    def test_a_non_aligned_liquid_stock_is_watched(self):
        self.assertEqual(len(supply_lead_pool(dict(results=[stock_row()]))), 1)

    def test_an_aligned_stock_is_not_a_leading_name(self):
        self.assertTrue(is_aligned(aligned_row()["indicators"]))
        self.assertEqual(supply_lead_pool(dict(results=[aligned_row()])), [])

    def test_missing_averages_are_not_read_as_aligned(self):
        row = stock_row()
        row["indicators"]["ma5"] = None
        self.assertFalse(is_aligned(row["indicators"]))
        self.assertEqual(len(supply_lead_pool(dict(results=[row]))), 1)

    def test_products_and_thin_names_stay_out(self):
        fund = stock_row("069500.KS", "KODEX 200")
        fund["asset_type"] = "etf"
        thin = stock_row("900110.KS", "얇은종목")
        thin["indicators"]["average_turnover_20d"] = 500000000
        self.assertEqual(supply_lead_pool(dict(results=[fund, thin])), [])

    def test_a_name_already_planned_is_not_listed_twice(self):
        row = stock_row()
        row["preopen_signal"] = dict(status="conditional_buy")
        self.assertEqual(supply_lead_pool(dict(results=[row])), [])


class ObserveSupplyTests(unittest.TestCase):
    def test_a_three_percent_intensity_qualifies(self):
        # 거래대금 10억, 순매수 3,000주×10,000원 = 3,000만원 → 3.0%.
        row = stock_row()
        item = observe_supply(row, AT, window_start(AT),
                              trend_fetcher=lambda code, sessions: trend_rows(
                                  foreign_qty=3000, institution_qty=0),
                              disclosure_fetcher=fetcher([]))
        self.assertEqual(item["status"], "qualified")
        self.assertEqual(item["intensity_pct"], 3.0)

    def test_below_the_threshold_is_not_an_observation(self):
        row = stock_row()
        item = observe_supply(row, AT, window_start(AT),
                              trend_fetcher=lambda code, sessions: trend_rows(
                                  foreign_qty=2999, institution_qty=0),
                              disclosure_fetcher=fetcher([]))
        self.assertIsNone(item)

    def test_the_accompanying_catalyst_is_carried(self):
        row = stock_row()
        item = observe_supply(row, AT, window_start(AT),
                              trend_fetcher=lambda code, sessions: trend_rows(),
                              disclosure_fetcher=fetcher([
                                  filing("(주)삼진제약 단일판매ㆍ공급계약체결")]))
        self.assertEqual(item["intensity_pct"], 30.0)
        self.assertEqual(item["catalyst"]["label"], "단일판매·공급계약")

    def test_an_unsupplied_flow_is_unread_not_zero(self):
        row = stock_row()
        item = observe_supply(row, AT, window_start(AT),
                              trend_fetcher=lambda code, sessions: [],
                              disclosure_fetcher=fetcher([]))
        self.assertEqual(item["status"], "unread")


class SupplyWatchTests(unittest.TestCase):
    def build(self):
        leading = stock_row()
        risky = stock_row("002790.KS", "아모레퍼시픽")
        weak = stock_row("064350.KS", "현대로템")
        report = dict(results=[leading, risky, weak])
        trends = {"005500": trend_rows(),
                  "002790": trend_rows(foreign_qty=25000),
                  "064350": trend_rows(foreign_qty=1000, institution_qty=0)}
        disclosures = {"002790": [filing("(주)아모레퍼시픽 유상증자결정")]}
        return report, (lambda code, sessions: trends[code]), (
            lambda code, start, at: disclosures.get(code, []))

    def test_the_watch_is_a_record_and_never_a_plan(self):
        report, trend, disclosure = self.build()
        watch = attach_supply_lead_watch(report, AT, trend_fetcher=trend,
                                        disclosure_fetcher=disclosure)
        self.assertEqual([item["symbol"] for item in watch["items"]], ["005500.KS"])
        self.assertEqual(watch["summary"]["qualified"], 1)
        self.assertEqual(watch["summary"]["risk_excluded"], 1)
        self.assertEqual(watch["excluded"][0]["label"], "유상증자·CB·감자")
        for row in report["results"]:
            self.assertNotIn("preopen_signal", row)
        self.assertNotIn("buy_evaluation", report)

    def test_the_ranking_puts_the_strongest_flow_first(self):
        report, trend, disclosure = self.build()
        watch = attach_supply_lead_watch(report, AT, trend_fetcher=trend,
                                        disclosure_fetcher=disclosure)
        item = watch["items"][0]
        self.assertEqual(item["intensity_pct"], 30.0)
        self.assertEqual(item["reference_session"], "2026-11-26")

    def test_the_watch_is_stored_on_the_report(self):
        report, trend, disclosure = self.build()
        attach_supply_lead_watch(report, AT, trend_fetcher=trend, disclosure_fetcher=disclosure)
        self.assertIn("supply_lead_watch", report)
        self.assertEqual(report["supply_lead_watch_at"], AT.isoformat())


class FormatTests(unittest.TestCase):
    def item(self, **overrides):
        row = stock_row(**{key: overrides.pop(key) for key in ("symbol", "name")
                           if key in overrides})
        base = observe_supply(row, AT, window_start(AT),
                              trend_fetcher=lambda code, sessions: trend_rows(),
                              disclosure_fetcher=fetcher([]))
        base.update(overrides)
        return base

    def test_the_block_names_itself_as_no_buy_signal(self):
        lines = format_block(dict(items=[self.item()], market="KR"), "KR")
        self.assertEqual(lines[0], "[수급 선행 관찰] · 매수 신호 아님")
        self.assertEqual(lines[-1], nonchart.NOTE)
        joined = "\n".join(lines)
        self.assertIn("• 삼진제약 (005500) · 5일 수급 +30.0%", joined)
        self.assertIn("19,470원 (11/26 종가)", joined)
        # 계획 형식(진입가·손절·목표가)은 싣지 않는다 — 안내줄의 '진입'은 통계 문구다.
        for word in ("손절", "목표", "매수 상한", "지정가"):
            self.assertNotIn(word, joined)

    def test_a_catalyst_is_shown_in_the_line(self):
        item = self.item()
        item["catalyst"] = dict(label="단일판매·공급계약",
                                published_at="2026-11-26T10:00:00+09:00")
        self.assertIn("촉매 단일판매·공급계약 (11/26)", format_line(item))

    def test_more_than_five_names_are_counted(self):
        items = [self.item(symbol=f"00000{i}.KS", name=f"종목{i}") for i in range(7)]
        lines = format_block(dict(items=items, market="KR"), "KR")
        self.assertEqual(sum(1 for line in lines if line.startswith("• ")), 5)
        self.assertTrue(any("외 2건" in line for line in lines))

    def test_the_block_filters_to_the_market_it_is_sent_to(self):
        self.assertEqual(format_block(dict(items=[self.item()], market="KR"), "US"), [])
        self.assertEqual(format_block(None, "KR"), [])

    def test_the_priority_puts_a_catalyst_first(self):
        plain = stock_row()
        with_catalyst = stock_row("002790.KS", "아모레퍼시픽")
        with_catalyst["nonchart_catalyst"] = dict(label="임상·승인")
        self.assertEqual(priority(with_catalyst), 1)
        self.assertEqual(priority(plain), 0)
        self.assertEqual(priority(None), 0)

    def test_the_catalyst_line_is_one_short_line(self):
        row = stock_row()
        row["nonchart_catalyst"] = dict(label="자사주취득",
                                        published_at="2026-11-25T10:00:00+09:00")
        self.assertEqual(catalyst_line(row), "  공시: 자사주취득 확인 (11/25)\n")
        self.assertEqual(catalyst_line(stock_row()), "")

    def test_the_risk_note_names_what_was_left_out(self):
        line = risk_exclusion_line([dict(name="삼성SDI", label="유상증자·CB·감자"),
                                    dict(name="하이브", label="유상증자·CB·감자"),
                                    dict(name="카카오", label="횡령·배임·소송·의견거절")])
        self.assertIn("3종목", line)
        self.assertIn("삼성SDI", line)
        self.assertIn("외 1종목", line)
        self.assertIsNone(risk_exclusion_line([]))


class RiskExclusionTests(unittest.TestCase):
    """리스크 공시는 매수 후보를 만들지 않고, 그 사유를 남긴다."""

    def test_a_planned_name_is_excluded_and_recorded(self):
        row = aligned_row()
        row["nonchart_risk"] = dict(kind="dilution", label="유상증자·CB·감자",
                                    title="(주)SK하이닉스 유상증자결정",
                                    published_at="2026-11-26T10:00:00+09:00")
        report = dict(results=[row], preopen_session="2026-11-27", sectors=[])
        result = evaluate_preopen(report, "2026-11-26", AT)
        self.assertEqual(row["preopen_decision"], "risk_disclosure_excluded")
        self.assertNotIn("preopen_signal", row)
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["counts"].get("risk_disclosure_excluded"), 1)
        exclusion = result["risk_disclosure_exclusions"][0]
        self.assertEqual((exclusion["symbol"], exclusion["label"]),
                         ("000660.KS", "유상증자·CB·감자"))

    def test_a_name_without_a_risk_disclosure_still_gets_its_plan(self):
        row = aligned_row()
        report = dict(results=[row], preopen_session="2026-11-27", sectors=[])
        result = evaluate_preopen(report, "2026-11-26", AT)
        self.assertEqual(row["preopen_decision"], "conditional_buy")
        self.assertEqual(result["recommendations"], ["000660.KS"])
        self.assertEqual(result["risk_disclosure_exclusions"], [])

    def test_the_risk_list_survives_the_multi_market_merge(self):
        kr = aligned_row()
        kr["nonchart_risk"] = dict(kind="control", label="최대주주 변경·자사주 처분")
        us = aligned_row("NVDA", "NVIDIA")
        us["market"] = "US"
        us["indicators"].update(price=180.0, ma20=175.0, ma60=170.0, data_date="2026-11-26")
        us["company_forecast"]["base_price"] = 200.0
        us["company_forecast"]["bull_price"] = 200.0
        report = dict(results=[kr, us], preopen_session="2026-11-27", sectors=[])
        from preopen_signals import evaluate_preopen_markets
        with patch('preopen_signals.session_dates',
                   return_value={"KR": "2026-11-26", "US": "2026-11-26"}):
            evaluate_preopen_markets(report, AT)
        exclusions = report["buy_evaluation"]["risk_disclosure_exclusions"]
        self.assertEqual([item["symbol"] for item in exclusions], ["000660.KS"])
        self.assertEqual(report["buy_evaluation"]["recommendations"], ["NVDA"])


class MessageTests(unittest.TestCase):
    def test_the_message_carries_the_supply_lead_block_and_the_catalyst_line(self):
        planned = aligned_row(symbol="005930.KS", name="삼성전자")
        other = aligned_row(symbol="000660.KS", name="SK하이닉스")
        other["company_forecast"]["composite_score"] = 90  # 촉매 없는 쪽이 점수는 높다
        planned["nonchart_catalyst"] = dict(kind="contract", label="단일판매·공급계약",
                                            title="삼성전자(주) 단일판매ㆍ공급계약체결",
                                            published_at="2026-11-26T10:00:00+09:00")
        report = dict(results=[other, planned], preopen_session="2026-11-27", sectors=[])
        evaluate_preopen(report, "2026-11-26", AT)
        watch_row = stock_row()
        report["supply_lead_watch"] = dict(
            market="KR", summary={},
            items=[observe_supply(watch_row, AT, window_start(AT),
                                  trend_fetcher=lambda code, sessions: trend_rows(),
                                  disclosure_fetcher=fetcher([]))])
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake',
                                       'TELEGRAM_CHAT_ID': 'fake',
                                       'DELIVERY_MARKET': 'KRX'}), patch(
                'scanner.delivery_status', return_value=(True, 'test')), patch(
                'scanner.requests.post') as post:
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            send_telegram_summary(report)
        text = post.call_args.kwargs['json']['text']
        self.assertIn("[수급 선행 관찰] · 매수 신호 아님", text)
        self.assertIn("삼진제약 (005500)", text)
        self.assertIn("공시: 단일판매·공급계약 확인 (11/26)", text)
        # 촉매 공시가 붙은 후보가 점수와 무관하게 먼저 온다.
        self.assertLess(text.index("삼성전자 (005930)"), text.index("SK하이닉스 (000660)"))

    def test_a_report_without_a_watch_block_keeps_it_out(self):
        row = aligned_row()
        report = dict(results=[row], preopen_session="2026-11-27", sectors=[])
        evaluate_preopen(report, "2026-11-26", AT)
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake',
                                       'TELEGRAM_CHAT_ID': 'fake',
                                       'DELIVERY_MARKET': 'KRX'}), patch(
                'scanner.delivery_status', return_value=(True, 'test')), patch(
                'scanner.requests.post') as post:
            post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
            send_telegram_summary(report)
        text = post.call_args.kwargs['json']['text']
        self.assertNotIn('수급 선행 관찰', text)


if __name__ == '__main__':
    unittest.main()
