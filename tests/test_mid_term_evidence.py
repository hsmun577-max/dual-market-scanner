"""The fundamentals screen and the one-line reason on the mid-term pullback list.

Owner's request (2026-11-24): "중기 +30% 눌림 발굴에서 적자·유증 종목은 제외하고, 종목마다
최근 실적·재료 1줄 요약이 같이 뜨도록 스캐너를 업그레이드해 줘." The case behind it is
``상지건설(042940)``: third-quarter operating loss and an 82억 rights offering, listed on the
chart condition alone. These tests pin both rules, the bonus-issue false positive the owner
called out, the article/filing line, and the promise that a dead provider never removes a
name from the list.
"""
from datetime import datetime
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import mid_term_evidence as evidence_module
from mid_term_evidence import (collect, earnings_line, financials, material, parse_finance,
                               parse_news, rejection, rights_offering, screen, summary)
from mid_term_watch import LABEL, attach, format_block
from scanner import send_telegram_summary

KST = ZoneInfo("Asia/Seoul")
AT = datetime(2026, 11, 24, 8, 30, tzinfo=KST)


def finance_document(rows, reported=("202509", "202512", "202603", "202606", "202609"),
                     consensus=("202612",)):
    """A Naver finance summary shaped like the provider's own payload."""
    return {"financeInfo": {
        "trTitleList": ([{"title": key[:4] + "." + key[4:] + ".", "key": key, "isConsensus": "N"}
                         for key in reported]
                        + [{"title": key[:4] + "." + key[4:] + ".", "key": key, "isConsensus": "Y"}
                           for key in consensus]),
        "rowList": [{"title": title, "columns": {key: {"value": value, "cx": cx}
                                                 for key, (value, cx) in cells.items()}}
                    for title, cells in rows]},
        "corporationSummary": {}}


def cell(value, cx=None):
    return (value, cx)


def values(**overrides):
    base = dict(price=5_000.0, volume=900_000, volume_ratio=0.8, ma20=5_400.0, ma60=5_600.0,
                return_5d=-0.03, return_20d=-0.15, rsi14=41.0, relative_strength=0.02,
                average_turnover_20d=3_000_000_000, obv_rising=False, breakdown=False,
                overheated=False, market_above_ma20=False, data_date="2026-11-21")
    base.update(overrides)
    return base


def row(symbol="012510.KS", name="더존비즈온", **overrides):
    base = dict(market="KR", symbol=symbol, name=name, asset_type="stock",
                signal=dict(confirmed=False, score=0), indicators=values(**overrides))
    return base


def report(*items):
    return dict(results=list(items), preopen_session="2026-11-24", sectors=[])


def record(financial=None, filings=(), news=(), verified=True):
    return {"symbol": "042940.KQ", "name": "상지건설", "financial": financial,
            "financial_status": "checked" if financial else "unavailable",
            "filings": list(filings), "news": list(news),
            "filing_status": "checked" if filings else "missing_key",
            "news_status": "checked" if news else "unavailable",
            "status": "checked", "verified": verified}


class FinanceTests(unittest.TestCase):
    def test_the_latest_reported_quarter_is_the_one_read(self):
        document = finance_document(
            [("매출액", {"202509": cell("7,692"), "202606": cell("9,955"), "202609": cell("9,323")}),
             ("영업이익", {"202509": cell("245"), "202606": cell("456"), "202609": cell("299")}),
             ("당기순이익", {"202509": cell("101"), "202606": cell("116"), "202609": cell("62")})])
        parsed = parse_finance(document, "quarter")
        self.assertEqual(parsed["period"], "2026-09")
        self.assertEqual(parsed["label"], "3Q26")
        self.assertEqual(parsed["operating_income"], 299.0)
        self.assertEqual(parsed["net_income"], 62.0)
        # Same quarter a year earlier, for the year-on-year term of the summary line.
        self.assertEqual(parsed["prior_operating_income"], 245.0)

    def test_an_estimate_is_not_a_reported_period(self):
        # 상지건설's 2026.09 column is the market's estimate; the reported loss is 2026.06.
        document = finance_document(
            [("매출액", {"202606": cell("21"), "202609": cell("26")}),
             ("영업이익", {"202606": cell("-18", "minus"), "202609": cell("5")}),
             ("당기순이익", {"202606": cell("-30", "minus"), "202609": cell("3")})],
            reported=("202509", "202512", "202603", "202606"), consensus=("202609",))
        parsed = parse_finance(document, "quarter")
        self.assertEqual(parsed["period"], "2026-06")
        self.assertEqual(parsed["operating_income"], -18.0)
        self.assertEqual(parsed["net_income"], -30.0)

    def test_a_quarter_with_no_figure_is_skipped_for_the_previous_one(self):
        document = finance_document(
            [("매출액", {"202606": cell("21"), "202609": cell("-")}),
             ("영업이익", {"202606": cell("-18", "minus"), "202609": cell("-")}),
             ("당기순이익", {"202606": cell("-30", "minus"), "202609": cell("-")})])
        self.assertEqual(parse_finance(document, "quarter")["period"], "2026-06")

    def test_an_annual_report_is_the_fallback_when_quarters_are_missing(self):
        document = finance_document(
            [("매출액", {"202412": cell("204"), "202512": cell("186")}),
             ("영업이익", {"202412": cell("-218", "minus"), "202512": cell("-68", "minus")})],
            reported=("202412", "202512"), consensus=())
        parsed = parse_finance(document, "annual")
        self.assertEqual(parsed["basis"], "annual")
        self.assertEqual(parsed["label"], "2025년")
        self.assertEqual(parsed["operating_income"], -68.0)

    def test_the_quarter_is_asked_for_before_the_annual_report(self):
        called = []

        def fake_get(url, params=None, timeout=6):
            called.append(url.rsplit("/", 1)[-1])
            if url.endswith("/quarter"):
                raise ValueError("HTTP 500")
            return finance_document(
                [("매출액", {"202512": cell("186")}),
                 ("영업이익", {"202512": cell("-68", "minus")})],
                reported=("202512",), consensus=())

        with patch.object(evidence_module, "_get", side_effect=fake_get):
            parsed, status = financials("042940")
        self.assertEqual(called, ["quarter", "annual"])
        self.assertEqual(status, "checked")
        self.assertEqual(parsed["basis"], "annual")

    def test_no_provider_answer_is_unavailable_not_a_crash(self):
        with patch.object(evidence_module, "_get", side_effect=OSError("down")):
            self.assertEqual(financials("042940"), (None, "unavailable"))


class RejectionTests(unittest.TestCase):
    def test_an_operating_loss_is_excluded(self):
        reason = rejection(record(dict(operating_income=-16.0, net_income=4.0)))
        self.assertEqual(reason, "operating_loss")

    def test_a_net_loss_alone_is_excluded(self):
        reason = rejection(record(dict(operating_income=12.0, net_income=-30.0)))
        self.assertEqual(reason, "net_loss")

    def test_a_profitable_reported_quarter_passes(self):
        self.assertIsNone(rejection(record(dict(operating_income=299.0, net_income=62.0))))

    def test_a_rights_offering_filing_is_excluded(self):
        record_ = record(dict(operating_income=5.0), filings=[
            {"date": "2026-11-04", "title": "주요사항보고서(유상증자결정)", "source": "DART"}])
        self.assertEqual(rejection(record_), "rights_offering")

    def test_a_rights_offering_in_the_news_is_excluded_too(self):
        record_ = record(dict(operating_income=5.0), news=[
            {"headline": "상지건설 최대주주, 일반공모 추가 참여… 2576억 자산가치에 베팅",
             "body": "구주주 배정 물량을 전량 소화한 데 이어 실권주 일부를", "published_at": "2026-11-04T11:23+09:00"}])
        self.assertEqual(rejection(record_), "rights_offering")

    def test_a_bonus_issue_is_not_a_rights_offering(self):
        record_ = record(dict(operating_income=5.0), filings=[
            {"date": "2026-11-10", "title": "무상증자결정", "source": "DART"}])
        self.assertIsNone(rejection(record_))
        # ...and a bonus issue does not hide a real rights offering in the same window.
        record_["filings"].insert(0, {"date": "2026-11-12", "title": "증권신고서(지분증권)",
                                      "source": "DART"})
        self.assertIsNone(rejection(record_))

    def test_missing_evidence_never_excludes_a_name(self):
        self.assertIsNone(rejection({}))
        self.assertIsNone(rejection(None))
        self.assertIsNone(rejection(record(None)))


class NewsTests(unittest.TestCase):
    def document(self, *items):
        return [{"total": len(items), "items": [dict(item) for item in items]}] if items else []

    def test_old_articles_and_other_issuers_are_dropped(self):
        document = self.document(
            dict(titleFull="상지건설, 3분기 영업손실 16억", datetime="202611201030",
                 officeId="011", articleId="0004683895", officeName="서울경제"),
            dict(titleFull="상지건설 관련 기사", datetime="202606201030",
                 officeId="011", articleId="0004600001", officeName="서울경제"),
            dict(titleFull="삼성전자 신고가", datetime="202611231030",
                 officeId="011", articleId="0004683999", officeName="서울경제"))
        rows = parse_news(document, "상지건설", AT)
        self.assertEqual([row["headline"] for row in rows], ["상지건설, 3분기 영업손실 16억"])

    def test_a_two_character_name_still_matches_its_own_news(self):
        document = self.document(
            dict(titleFull="삼기, 3분기 영업이익 38억…흑자전환", datetime="202611211030",
                 officeId="011", articleId="0004683900", officeName="서울경제"),
            dict(titleFull="삼기업 신규 상장", datetime="202611211130",
                 officeId="011", articleId="0004683901", officeName="서울경제"))
        rows = parse_news(document, "삼기", AT)
        self.assertEqual([row["headline"] for row in rows], ["삼기, 3분기 영업이익 38억…흑자전환"])

    def test_a_round_up_that_lists_the_code_is_read(self):
        document = self.document(
            dict(titleFull="오늘의 메모[11월 6일]", datetime="202611051042",
                 officeId="011", articleId="0004679001", officeName="서울경제",
                 body="▲실권주청약=상지건설(042940)(주관사 SK)"))
        rows = parse_news(document, "상지건설", AT)
        self.assertEqual(len(rows), 1)
        record_ = record(dict(operating_income=5.0), news=rows)
        self.assertEqual(rights_offering(record_)["kind"], "news")

    def test_another_company_in_the_same_article_is_not_this_issuer(self):
        # 2026-11-24 real case: 다우기술 was listed in an article about 중앙일보's offering.
        body = ("중앙일보 채권단이 제3자배정 유상증자 방식을 승인했다. 다우기술과 CJ 컨소시엄이 "
                "신주를 인수한다. 다우기술은 이번 딜에 참여하는 투자자다.")
        rows = [{"headline": "중앙일보 채권단, 다우·CJ 컨소시엄 승인…내달 워크아웃 종결",
                 "body": body, "published_at": "2026-11-24T09:00+09:00"}]
        record_ = record(dict(operating_income=5.0), news=rows)
        record_["name"] = "다우기술"
        self.assertIsNone(rights_offering(record_))

    def test_a_headline_keeps_its_text_without_html_entities(self):
        document = self.document(
            dict(titleFull="동국제강 3Q 영업익 299억 &quot;봉형강 효자&quot;", datetime="202611201030",
                 officeId="011", articleId="0004683895", officeName="서울경제"))
        rows = parse_news(document, "동국제강", AT)
        self.assertIn('"봉형강 효자"', rows[0]["headline"])


class SummaryTests(unittest.TestCase):
    def test_the_earnings_line_carries_the_reported_figure_and_its_change(self):
        self.assertEqual(earnings_line(dict(label="3Q26", operating_income=299.0,
                                            prior_operating_income=245.0)),
                         "3Q26 영업이익 +299억 (+22%)")
        self.assertEqual(earnings_line(dict(label="3Q26", operating_income=38.0,
                                            prior_operating_income=-12.0)),
                         "3Q26 영업이익 +38억 (흑자전환)")
        self.assertEqual(earnings_line(dict(label="3Q26", operating_income=-18.0,
                                            prior_operating_income=-37.0)),
                         "3Q26 영업이익 -18억 (적자 축소)")
        self.assertEqual(earnings_line(dict(label="3Q26", operating_income=83_000.0,
                                            prior_operating_income=None)),
                         "3Q26 영업이익 +8.3조")

    def test_the_line_is_earnings_then_recent_material(self):
        record_ = record(dict(label="3Q26", operating_income=299.0, prior_operating_income=245.0),
                         filings=[{"date": "2026-11-21",
                                   "title": "단일판매ㆍ공급계약체결              ", "source": "DART"}])
        self.assertEqual(summary(record_, AT),
                         "3Q26 영업이익 +299억 (+22%) · 11/21 단일판매ㆍ공급계약체결")

    def test_a_routine_filing_is_not_the_material_line(self):
        record_ = record(dict(label="3Q26", operating_income=299.0),
                         filings=[{"date": "2026-11-23", "title": "임원ㆍ주요주주특정증권등소유상황보고서",
                                   "source": "DART"}],
                         news=[{"headline": "[포토] 상지건설 주주총회", "published_at": "2026-11-22T09:00+09:00"}])
        self.assertIsNone(material(record_, AT))
        self.assertEqual(summary(record_, AT), "3Q26 영업이익 +299억")

    def test_an_old_headline_is_not_recent_material(self):
        record_ = record(dict(label="3Q26", operating_income=299.0),
                         filings=[{"date": "2026-08-01", "title": "단일판매ㆍ공급계약체결", "source": "DART"}])
        self.assertIsNone(material(record_, AT))

    def test_a_headline_that_repeats_the_earnings_figure_is_not_the_material_line(self):
        financial = dict(label="3Q26", operating_income=299.0, prior_operating_income=245.0)
        record_ = record(financial, news=[{
            "headline": "동국제강 3Q 영업익 299억, 전년比 22.0%↑ \"봉형강 효자\"",
            "published_at": "2026-10-30T09:00+09:00"}])
        self.assertEqual(summary(record_, AT), "3Q26 영업이익 +299억 (+22%)")

    def test_a_headline_about_a_newer_quarter_is_kept_beside_the_statement(self):
        # 삼기: the confirmed quarter in the statements is 2Q26, the article reports 3Q 잠정.
        financial = dict(label="2Q26", operating_income=12.0, prior_operating_income=57.0)
        record_ = record(financial, news=[{
            "headline": "삼기, 3분기 영업익 114억…전년比 181%↑",
            "published_at": "2026-11-16T09:00+09:00"}])
        self.assertEqual(summary(record_, AT),
                         "2Q26 영업이익 +12억 (-79%) · 11/16 삼기, 3분기 영업익 114억…전년比 181%↑")

    def test_no_evidence_at_all_leaves_no_line(self):
        self.assertIsNone(summary(record(None), AT))


class ScreenTests(unittest.TestCase):
    def test_excluded_names_are_counted_and_kept_rows_keep_the_ranking(self):
        rows = [row(symbol=f"{index:06d}.KS", name=f"종목{index}") for index in range(1, 6)]
        losses = {"000003.KS"}

        def collector(symbol, name, at):
            return record(dict(operating_income=-1.0 if symbol in losses else 5.0))

        result = screen(rows, AT, collector, workers=1)
        self.assertEqual([entry["item"]["symbol"] for entry in result["kept"]],
                         ["000001.KS", "000002.KS", "000004.KS", "000005.KS"])
        self.assertEqual(result["excluded"], {"operating_loss": 1})
        self.assertEqual(result["screened"], 5)

    def test_the_cap_bounds_how_many_issuers_are_read(self):
        rows = [row(symbol=f"{index:06d}.KS") for index in range(1, 30)]
        seen = []

        def collector(symbol, name, at):
            seen.append(symbol)
            return record(dict(operating_income=5.0))

        screen(rows, AT, collector, cap=10, workers=1)
        self.assertEqual(len(seen), 10)

    def test_a_failing_collector_keeps_the_name_and_counts_it_unverified(self):
        def collector(symbol, name, at):
            raise TimeoutError("provider down")

        result = screen([row()], AT, collector, workers=1)
        self.assertEqual(len(result["kept"]), 1)
        self.assertEqual(result["unverified"], 1)
        self.assertEqual(result["excluded"], {})

    def test_an_unverified_record_is_counted_even_when_the_collector_returns(self):
        result = screen([row()], AT, lambda symbol, name, at: record(None, verified=False),
                        workers=1)
        self.assertEqual(result["unverified"], 1)


class DartTests(unittest.TestCase):
    def document(self, *rows):
        return {"status": "000", "list": [dict(row) for row in rows]}

    def test_a_disclosure_older_than_the_window_is_dropped_and_the_title_is_clean(self):
        document = self.document(
            {"rcept_dt": "20261104", "rcept_no": "20261104000123",
             "report_nm": "주요사항보고서(유상증자결정)              "},
            {"rcept_dt": "20260401", "rcept_no": "20260401000456",
             "report_nm": "단일판매ㆍ공급계약체결"},
            {"rcept_dt": "20261104", "rcept_no": "short", "report_nm": "깨진 접수번호"})
        rows = evidence_module.parse_dart_filings(document, AT)
        self.assertEqual([row["title"] for row in rows], ["주요사항보고서(유상증자결정)"])
        self.assertEqual(rows[0]["date"], "2026-11-04")
        self.assertEqual(rows[0]["url"], "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261104000123")

    def test_without_a_key_the_check_says_so_instead_of_failing(self):
        with patch.dict("os.environ", {"DART_API_KEY": ""}):
            self.assertEqual(evidence_module.filings("042940", AT), ([], "missing_key"))

    def test_an_issuer_without_a_dart_code_is_reported_not_guessed(self):
        with patch.object(evidence_module, "dart_codes", return_value={}), \
             patch.object(evidence_module, "_get") as get:
            self.assertEqual(evidence_module.filings("042940", AT, api_key="k"), ([], "unmapped_issuer"))
        get.assert_not_called()

    def test_a_disclosure_query_that_fails_is_unavailable(self):
        with patch.object(evidence_module, "dart_codes", return_value={"042940": "00123456"}), \
             patch.object(evidence_module, "_get", side_effect=OSError("down")):
            self.assertEqual(evidence_module.filings("042940", AT, api_key="k"), ([], "unavailable"))

    def test_a_dart_disclosure_is_preferred_over_the_same_day_article(self):
        document = self.document(
            {"rcept_dt": "20261120", "rcept_no": "20261120000123",
             "report_nm": "단일판매ㆍ공급계약체결"})
        with patch.object(evidence_module, "dart_codes", return_value={"042940": "00123456"}), \
             patch.object(evidence_module, "_get", return_value=document):
            rows, status = evidence_module.filings("042940", AT, api_key="k")
        self.assertEqual(status, "checked")
        record_ = record(dict(operating_income=5.0), filings=rows, news=[
            {"headline": "상지건설, 수주 계약 체결", "published_at": "2026-11-20T15:00+09:00"}])
        self.assertTrue(material(record_, AT).startswith("11/20 단일판매"))


class CollectTests(unittest.TestCase):
    def test_collect_never_raises_when_every_provider_is_down(self):
        with patch.object(evidence_module, "financials", side_effect=OSError("down")), \
             patch.object(evidence_module, "news", return_value=([], "unavailable")), \
             patch.object(evidence_module, "filings", return_value=([], "missing_key")):
            record_ = collect("042940.KQ", "상지건설", AT)
        self.assertEqual(record_["status"], "unavailable")
        self.assertFalse(record_["verified"])
        self.assertIsNone(rejection(record_))

    def test_a_reported_loss_and_a_filing_make_a_checked_record(self):
        with patch.object(evidence_module, "financials",
                          return_value=(dict(label="3Q26", operating_income=-16.0), "checked")), \
             patch.object(evidence_module, "news", return_value=([], "checked")), \
             patch.object(evidence_module, "filings", return_value=(
                 [{"date": "2026-11-04", "title": "주요사항보고서(유상증자결정)", "source": "DART"}],
                 "checked")):
            record_ = collect("042940.KQ", "상지건설", AT)
        self.assertTrue(record_["verified"])
        self.assertEqual(rejection(record_), "operating_loss")
        self.assertEqual(record_["rights_offering"]["title"], "주요사항보고서(유상증자결정)")


class AttachTests(unittest.TestCase):
    def rows(self, count=10):
        return [row(symbol=f"{index:06d}.KS", name=f"종목{index}",
                    relative_strength=index / 1000) for index in range(1, count + 1)]

    def collector(self, excluded):
        def collect_row(symbol, name, at):
            if symbol in excluded:
                return record(dict(label="3Q26", operating_income=-16.0, net_income=-30.0),
                              filings=[{"date": "2026-11-04",
                                        "title": "주요사항보고서(유상증자결정)", "source": "DART"}])
            return record(dict(label="3Q26", operating_income=299.0, prior_operating_income=245.0),
                          filings=[{"date": "2026-11-21",
                                    "title": "단일판매ㆍ공급계약체결", "source": "DART"}])
        return collect_row

    def test_an_excluded_name_makes_room_for_the_next_candidate(self):
        report_ = report(*self.rows(10))
        entries = attach(report_, AT, evidence=self.collector({"000009.KS"}))
        self.assertEqual(len(entries), 8)
        symbols = [entry["symbol"] for entry in entries]
        self.assertNotIn("000009.KS", symbols)
        # Strongest relative strength first, so the tenth candidate is the first row...
        self.assertEqual(symbols[0], "000010.KS")
        # ...and the eighth slot is filled by the runner-up the exclusion let through.
        self.assertEqual(symbols[-1], "000002.KS")
        self.assertTrue(all(entry["catalyst_summary"] for entry in entries))
        self.assertFalse(any(entry["entry_signal"] for entry in entries))
        scope = report_["mid_term_watch_scope"]
        self.assertEqual(scope["excluded"]["operating_loss"], 1)
        self.assertEqual(scope["fundamentals_screen"]["excluded"], {"operating_loss": 1})
        self.assertEqual(scope["fundamentals_screen"]["screened"], 10)
        self.assertEqual(scope["fundamentals_screen"]["unverified"], 0)

    def test_the_screen_does_not_touch_the_same_day_buy_decision(self):
        report_ = report(*self.rows(3))
        attach(report_, AT, evidence=self.collector(set()))
        for item in report_["results"]:
            self.assertNotIn("preopen_signal", item)
        self.assertNotIn("buy_evaluation", report_)

    def test_without_an_evidence_reader_the_list_is_the_chart_result(self):
        report_ = report(*self.rows(3))
        entries = attach(report_, AT)
        self.assertEqual([entry["symbol"] for entry in entries],
                         ["000003.KS", "000002.KS", "000001.KS"])
        self.assertNotIn("catalyst_summary", entries[0])
        self.assertNotIn("fundamentals_screen", report_["mid_term_watch_scope"])


class MessageTests(unittest.TestCase):
    def message(self, report_):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "fake"}), \
             patch("scanner.delivery_status", return_value=(True, "test")), \
             patch("scanner.requests.post") as post:
            post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
            send_telegram_summary(report_)
        return post.call_args.kwargs["json"]["text"]

    def test_each_name_carries_its_reason_line_in_the_analysis_data(self):
        report_ = report(row())
        attach(report_, AT, evidence=lambda symbol, name, at: record(
            dict(label="3Q26", operating_income=299.0, prior_operating_income=245.0),
            filings=[{"date": "2026-11-21", "title": "단일판매ㆍ공급계약체결", "source": "DART"}]))
        text = self.message(report_)
        # 재료 한 줄은 분석 자료에 남고, 알림 본문에는 목록 자체를 싣지 않는다.
        self.assertNotIn(LABEL, text)
        self.assertIn("재료: 3Q26 영업이익 +299억 (+22%) · 11/21 단일판매ㆍ공급계약체결",
                      format_block(report_["mid_term_watch"])[2])
        self.assertEqual(report_["mid_term_watch"][0]["catalyst_summary"],
                         "3Q26 영업이익 +299억 (+22%) · 11/21 단일판매ㆍ공급계약체결")

    def test_a_name_with_nothing_found_still_carries_the_line_in_the_data(self):
        report_ = report(row())
        attach(report_, AT, evidence=lambda symbol, name, at: record(None))
        text = self.message(report_)
        self.assertNotIn("재료:", text)
        self.assertIn("재료: 최근 공시·뉴스 없음", format_block(report_["mid_term_watch"])[2])

    def test_the_block_states_the_premise_and_two_lines_per_name(self):
        report_ = report(row())
        attach(report_, AT, evidence=lambda symbol, name, at: record(None))
        block = format_block(report_["mid_term_watch"])
        self.assertEqual(block[0], LABEL)
        self.assertIn("당일 매수 신호 아님", block[1])
        self.assertEqual(block[2].count("\n"), 2)


if __name__ == "__main__":
    unittest.main()
