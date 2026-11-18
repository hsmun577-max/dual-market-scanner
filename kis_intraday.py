"""Check three completed five-minute bars and notify Telegram."""

import argparse
import asyncio
import json
import math
import os
import re
from html import unescape
from time import sleep
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from websockets.exceptions import WebSocketException

from kis_feed_probe import FEEDS, FEED_URL, KST, connect, parse_records, request_approval, subscription
from signal_context import context_status
from company_forecast import build_company_forecast
from event_impact_analysis import article_text, world_events, analyze_events
from candidate_selection import instrument_rejection_reason, rank_market_candidates, rank_recovery_candidates
from sector_flow import format_sector_header
from telegram_text import split_telegram_text
from scanner import BENCHMARKS, KR_SYMBOL_NAMES, US_SYMBOL_NAMES, sector_flow_snapshot, calculate_indicators, classify_signal, clean_prices, rank_korean_candidates


# Common shares from the current watchlist. NXT subscription acceptance is checked per session.
SYMBOLS = tuple(symbol.split(".")[0] for symbol in list(KR_SYMBOL_NAMES)[:9])
US_SYMBOLS = ("AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA")
OPEN_TIMES = {"NXT": time(8), "KRX": time(9), "US-early": time(4), "US-late": time(8, 20)}
BAR_SECONDS = 300
MAX_QUOTE_AGE = 10
MAX_SPREAD_PCT = 0.5
MIN_VOLUME_RATIO = 1.5
MAX_BREAKOUT_EXTENSION = 0.005


def collection_open(scheduled, ready_at):
    """Keep the scheduled window or use the next complete five-minute boundary."""
    for minutes in (0, 5, 10, 15):
        start = scheduled + timedelta(minutes=minutes)
        if ready_at + timedelta(minutes=1) <= start:
            return start
    raise RuntimeError("개장 후 15분 이내에 완전한 5분봉 수집을 시작하지 못했습니다")


def wait_for_connection_window(opens):
    connection_at = opens - timedelta(minutes=3)
    delay = (connection_at - datetime.now(opens.tzinfo)).total_seconds()
    if delay > 0:
        print(f"KIS 연결 예정: {connection_at.isoformat()}", flush=True)
        sleep(delay)
    if datetime.now(opens.tzinfo) >= opens:
        raise RuntimeError("시세 수집 시작 시각을 지나 첫 봉을 확보할 수 없습니다")


def breakout_v2(bars, quote, close_time):
    """Volume confirmation and a capped entry above the broken resistance."""
    metrics = candidate_metrics(bars, quote, close_time)
    if metrics is None or metrics[0] < 2.0:
        return None
    resistance = max(bars[0]["high"], bars[1]["high"])
    limit = resistance * (1 + MAX_BREAKOUT_EXTENSION)
    if not resistance < quote["ask"] <= limit or metrics[2] > limit:
        return None
    return limit


def protective_stop(bars, entry_limit, bid, overseas):
    """Place the stop below the completed breakout candle's low."""
    support = Decimal(str(bars[2]["low"]))
    step = Decimal("0.01" if overseas else "1")
    stop = (support * Decimal("0.999")).quantize(step, rounding=ROUND_DOWN)
    if not Decimal(str(entry_limit)) * Decimal("0.98") <= stop < Decimal(str(bid)):
        return None
    return float(stop)


MARKET_INDICES = {
    "KR": ("^KS11", "^KQ11"),
    "US": ("^GSPC", "^IXIC", "^RUT"),
}
GLOBAL_MARKET_TICKERS = ("^STOXX50E", "^N225", "000001.SS", "^HSI",
                         "KRW=X", "DX-Y.NYB", "^TNX", "^VIX", "CL=F")
RISK_HEADLINE = re.compile(
    r"\b(tariffs?|export controls?|sanctions?|invasion|airstrikes?|"
    r"trading halt|recall|bankruptcy|guidance cut)\b|관세|수출\s*규제|제재|공습|거래정지|리콜|실적\s*하향",
    re.IGNORECASE,
)


def price_projection(bars, entry_limit, prior_highs, overseas):
    """Project the opening range and account for nearby prior-session highs."""
    if any("low" not in bars[slot] for slot in (0, 1)):
        return None
    ceiling = max(bars[slot]["high"] for slot in (0, 1))
    floor = min(bars[slot]["low"] for slot in (0, 1))
    if ceiling <= floor:
        return None
    projected = ceiling + (ceiling - floor)
    overhead = min((high for high in prior_highs if high > ceiling), default=None)
    decimals = 2 if overseas else 0
    if overhead is not None and overhead <= projected:
        reason = f"최근 20거래일 고점 {overhead:,.{decimals}f}에서 저항 예상"
    else:
        reason = f"돌파 기준 {ceiling:,.{decimals}f} + 직전 가격 범위 {ceiling - floor:,.{decimals}f}"
    target = min(projected, overhead) if overhead is not None else projected
    step = Decimal("0.01" if overseas else "1")
    target = float(Decimal(str(target)).quantize(step, rounding=ROUND_DOWN))
    return (target, reason) if target > entry_limit else None


def prior_resistance(symbols, previous_session, overseas):
    tickers = [symbol[4:] for symbol in symbols] if overseas else [
        symbol + (".KQ" if symbol + ".KQ" in KR_SYMBOL_NAMES else ".KS") for symbol in symbols
    ]
    try:
        prices = yf.download(tickers, period="3mo", auto_adjust=False, group_by="ticker",
                             multi_level_index=True, progress=False, threads=4, timeout=8)
        levels = {}
        for symbol, ticker in zip(symbols, tickers):
            if ticker in prices.columns.get_level_values(0):
                highs = prices[ticker]["High"].dropna()
                highs = highs.loc[highs.index.date <= previous_session].tail(20)
                if len(highs) == 20 and highs.index[-1].date() == previous_session:
                    levels[symbol] = [float(value) for value in highs]
        return levels
    except (KeyError, ValueError, OSError) as exc:
        print(f"Historical resistance unavailable ({type(exc).__name__})")
        return {}


def market_evidence(market, previous_session, available_at):
    """Require broad local confirmation and record global market conditions."""
    local = MARKET_INDICES["US" if market.startswith("US-") else "KR"]
    tickers = (*MARKET_INDICES["KR"], *MARKET_INDICES["US"], *GLOBAL_MARKET_TICKERS)
    try:
        data = yf.download(list(tickers), period="3mo", auto_adjust=False,
                           group_by="ticker", threads=4, progress=False, timeout=8)
        supportive = []
        snapshot = {}
        for ticker in tickers:
            if ticker not in data.columns.get_level_values(0):
                continue
            closes = data[ticker]["Close"].dropna()
            closes = closes.loc[closes.index.date <= previous_session]
            if len(closes) < 2:
                continue
            day = closes.index[-1].date()
            snapshot[ticker] = {"date": day.isoformat(), "close": float(closes.iloc[-1]),
                                "change_pct": float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100)}
            if ticker in local and day == previous_session and len(closes) >= 20:
                supportive.append(bool(closes.iloc[-1] > closes.iloc[-20:].mean()))
    except Exception as exc:
        print(f"{market} market context unavailable ({type(exc).__name__})")
        return []
    print(f"{market} market snapshot {json.dumps(snapshot)}")
    if len(supportive) != len(local):
        print(f"{market} market context unavailable: incomplete local index coverage")
        return []
    # At least two of three US indices, and both Korean indices, must agree.
    state = "supportive" if sum(supportive) > len(local) / 2 else "neutral"
    return [{"kind": "market", "market": market, "symbol": "*",
             "available_at": available_at.isoformat(), "assessment": state,
             "snapshot": snapshot}]


def recent_company_news(articles, now):
    records = {}
    for item in articles:
        article = item.get("content") or {}
        title = " ".join((article.get("title") or "").split())
        try:
            published = datetime.fromisoformat(str(article.get("pubDate") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if not title or published.utcoffset() is None or not now - timedelta(hours=24) <= published < now:
            continue
        link = (article.get("canonicalUrl") or {}).get("url")
        provider = (article.get("provider") or {}).get("displayName") or "Yahoo Finance"
        records.setdefault(title.casefold(), {"headline": title, "available_at": published.isoformat(),
                                              "source": provider, "url": link})
    return sorted(records.values(), key=lambda item: datetime.fromisoformat(item["available_at"]), reverse=True)


def headline_risks(market, symbol, articles, now):
    return [
        {**item, "kind": "event", "market": market, "symbol": symbol, "assessment": "risk"}
        for item in recent_company_news(articles, now) if RISK_HEADLINE.search(item["headline"])
    ]


def korean_company_news(symbol, now):
    """Use dated Korean headlines when Yahoo has no domestic coverage."""
    from email.utils import parsedate_to_datetime
    from xml.etree import ElementTree

    name = KR_SYMBOL_NAMES.get(symbol + ".KS") or KR_SYMBOL_NAMES.get(symbol + ".KQ")
    if not name or name == symbol:
        return []
    response = requests.get("https://news.google.com/rss/search", params={
        "q": f'"{name}" (실적 OR 수주 OR 공시 OR 매출 OR 투자 OR 규제) when:1d',
        "hl": "ko", "gl": "KR", "ceid": "KR:ko",
    }, timeout=8)
    response.raise_for_status()
    articles = []
    for item in ElementTree.fromstring(response.content).findall("./channel/item"):
        title = item.findtext("title", "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
        except (ValueError, TypeError):
            continue
        if name not in title or published.utcoffset() is None or not now - timedelta(hours=24) <= published < now:
            continue
        if not re.search(r"실적|수주|공시|매출|영업|공급|계약|배당|증자|자사주|인수|합병|파산|회생|거래정지|상장폐지|규제|제재|관세|수출|가이던스|contract|earnings", title, re.IGNORECASE):
            continue
        articles.append({"headline": title, "available_at": published.isoformat(),
                         "source": item.findtext("source", "Google News"),
                         "url": item.findtext("link", "")})
    return sorted({item["headline"]: item for item in articles}.values(),
                  key=lambda item: item["available_at"], reverse=True)


def dart_financial_reports(api_key, corp_code, now):
    """Find periodic reports independently of the recent event disclosure window."""
    response = requests.get("https://opendart.fss.or.kr/api/list.json", params={
        "crtfc_key": api_key, "corp_code": corp_code, "pblntf_ty": "A",
        "bgn_de": (now - timedelta(days=400)).strftime("%Y%m%d"),
        "end_de": now.strftime("%Y%m%d"), "page_count": 100,
    }, timeout=8)
    response.raise_for_status()
    body = response.json()
    if body.get("status") not in {"000", "013"}:
        raise ValueError("DART financial report index unavailable")
    return [{"form": row.get("report_nm", ""),
             "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + row["rcept_no"]}
            for row in body.get("list", []) if re.fullmatch(r"\d{14}", row.get("rcept_no", ""))]


def company_financials(statement, today):
    revenue_key = next((key for key in ("TotalRevenue", "Total Revenue") if key in statement.index), None)
    income_key = next((key for key in ("OperatingIncome", "Operating Income") if key in statement.index), None)
    if revenue_key is None or income_key is None:
        return {}
    quarters = sorted((col for col in statement.columns if col.date() < today), reverse=True)
    for quarter in quarters:
        sales, income = (float(statement.loc[key, quarter]) for key in (revenue_key, income_key))
        if not math.isfinite(sales) or sales <= 0 or not math.isfinite(income):
            continue
        result = {"quarter": quarter.date().isoformat(), "operating_margin_pct": 100 * income / sales,
                  "revenue_amount": sales, "operating_income_amount": income}
        prior = next((col for col in quarters if 350 <= (quarter - col).days <= 380), None)
        if prior is not None:
            prior_sales = float(statement.loc[revenue_key, prior])
            if math.isfinite(prior_sales) and prior_sales > 0:
                result["revenue_yoy_pct"] = 100 * (sales / prior_sales - 1)
                prior_income = float(statement.loc[income_key, prior])
                if math.isfinite(prior_income):
                    result["operating_margin_yoy_pp"] = 100 * (income / sales - prior_income / prior_sales)
        return result
    return {}


SEC_EVENT_ITEMS = {
    "1.03": "파산 또는 법정관리",
    "2.04": "채무 조기상환 또는 채무불이행 관련 공시",
    "3.01": "상장 기준 미충족 또는 상장폐지 관련 공시",
    "4.02": "기존 재무제표 신뢰 불가 공시",
}


def sec_submission_context(recent, cik, now):
    filings, risks = [], []
    for form, accepted, accession, document, items in zip(
        *(recent[key] for key in ("form", "acceptanceDateTime", "accessionNumber", "primaryDocument", "items"))
    ):
        try:
            stamp = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if stamp.utcoffset() is None or not now - timedelta(days=30) <= stamp < now:
            continue
        if form not in {"8-K", "8-K/A", "10-Q", "10-K", "6-K", "20-F"}:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"
        item_codes = str(items or "").split(",")
        item_codes = [code.strip() for code in item_codes]
        listing = {"form": form, "accepted_at": stamp.isoformat(), "url": url, "items": item_codes,
                   "source": "SEC EDGAR"}
        filings.append(listing)
        if form in {"8-K", "8-K/A"} and now - stamp <= timedelta(hours=24):
            risks.extend({**listing, "available_at": stamp.isoformat(), "headline": SEC_EVENT_ITEMS[code]}
                         for code in item_codes if code in SEC_EVENT_ITEMS)
    filings.sort(key=lambda filing: datetime.fromisoformat(filing["accepted_at"]), reverse=True)
    return filings[:3], risks


def us_company_filings(ticker, now):
    # Resolve the issuer's CIK from the ticker-specific filing index.
    index = ticker.get_sec_filings() or []
    urls = [str(item.get("edgarUrl") or "") for item in index]
    ciks = [match[1] for url in urls if (match := re.search(r"_(\d{1,10})$", url))]
    if ciks:
        cik = ciks[0]
        try:
            response = requests.get(
                f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                headers={"User-Agent": "Dual Market Scanner https://github.com/hsmun577-max/dual-market-scanner"},
                timeout=5,
            )
            response.raise_for_status()
            return sec_submission_context(response.json()["filings"]["recent"], cik, now)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            print(f"SEC submission lookup unavailable ({type(exc).__name__})")
    # Date-only index entries are informational and cannot block an intraday decision.
    fallback = []
    for item in index:
        day = str(item.get("date") or "")
        if (now - timedelta(days=30)).date().isoformat() <= day < now.date().isoformat():
            if item.get("type") in {"8-K", "8-K/A", "10-Q", "10-K", "6-K", "20-F"} and item.get("edgarUrl"):
                fallback.append({"form": item["type"], "filing_date": day,
                                 "url": item["edgarUrl"], "source": "Yahoo Finance SEC index"})
    return sorted(fallback, key=lambda item: item["filing_date"], reverse=True)[:3], []


DART_RISK_TITLE = re.compile(r"회생절차개시신청|파산신청|횡령[ㆍ·및 ]*배임|상장폐지결정|감사의견거절|채무불이행")


def dart_company_codes(api_key):
    from io import BytesIO
    from xml.etree import ElementTree
    from zipfile import BadZipFile, ZipFile

    response = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                            params={"crtfc_key": api_key}, timeout=10)
    response.raise_for_status()
    try:
        with ZipFile(BytesIO(response.content)) as archive:
            root = ElementTree.fromstring(archive.read("CORPCODE.xml"))
    except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise ValueError("DART corporate code file unavailable") from exc
    return {row.findtext("stock_code", "").strip(): row.findtext("corp_code", "")
            for row in root.findall("list") if row.findtext("stock_code", "").strip()}


class DartTableParser:
    """Read disclosure table cells even when the source has unmatched XML tags."""

    def rows(self, document):
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows = []
                self.row = None
                self.cell = None

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self.row = []
                    self.cell = None
                elif tag in {"td", "th", "tu"} and self.row is not None:
                    self.cell = []
                    self.row.append(self.cell)

            def handle_endtag(self, tag):
                if tag in {"td", "th", "tu"}:
                    self.cell = None
                elif tag == "tr" and self.row is not None:
                    self.rows.append(["".join(cell).strip() for cell in self.row])
                    self.row = self.cell = None

            def handle_data(self, data):
                if self.cell is not None:
                    self.cell.append(data)

        try:
            decoded = document.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = document.decode("cp949", errors="replace")
        parser = Parser()
        parser.feed(decoded)
        parser.close()
        return parser.rows


def supply_contract_metrics(document):
    metrics = {}
    for cells in DartTableParser().rows(document):
        for label, value in zip(cells, cells[1:]):
            label = re.sub(r"\s+", "", label)
            value = value.replace(",", "").strip()
            if label.endswith("계약금액(원)") and re.fullmatch(r"[0-9]+", value):
                metrics["amount_krw"] = int(value)
            if label.endswith("매출액대비(%)"):
                value = value.removesuffix("%").strip()
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
                    metrics["revenue_ratio_pct"] = float(value)
    return metrics


def dart_contract_details(api_key, receipt):
    from io import BytesIO
    from xml.etree import ElementTree
    from zipfile import BadZipFile, ZipFile

    response = requests.get("https://opendart.fss.or.kr/api/document.xml",
                            params={"crtfc_key": api_key, "rcept_no": receipt}, timeout=6)
    response.raise_for_status()
    try:
        with ZipFile(BytesIO(response.content)) as archive:
            document = next((item for item in archive.infolist()
                             if item.filename.rsplit("/", 1)[-1] == f"{receipt}.xml"
                             and item.file_size <= 4_000_000), None)
            return supply_contract_metrics(archive.read(document)) if document else {}
    except (BadZipFile, ElementTree.ParseError):
        return {}


def dart_contract_candidates(api_key, ranked, now):
    """Promote up to two same-day contract issuers with confirmed trading activity."""
    if not ranked:
        return []
    day = now.strftime("%Y%m%d")
    candidates = []
    receipts = set()
    attempts = 0
    for page in range(1, 4):
        response = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": api_key, "bgn_de": day, "end_de": day, "pblntf_ty": "I",
                    "page_no": page, "page_count": 100},
            timeout=6,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") == "013":
            break
        if body.get("status") != "000":
            raise ValueError("DART daily exchange disclosure query failed")
        for item in body.get("list", []):
            code = item.get("stock_code", "").strip()
            title = item.get("report_nm", "")
            receipt = item.get("rcept_no", "")
            if (code not in ranked or code in candidates or item.get("rcept_dt") != day
                    or not re.fullmatch(r"\d{14}", receipt) or receipt in receipts
                    or "단일판매" not in title or "공급계약" not in title
                    or any(word in title for word in ("정정", "해지", "취소"))):
                continue
            receipts.add(receipt)
            attempts += 1
            details = dart_contract_details(api_key, receipt)
            if details.get("amount_krw", 0) > 0 and details.get("revenue_ratio_pct", 0) >= 5:
                candidates.append(code)
                print(f"DART contract watch {code} receipt={receipt} ratio={details['revenue_ratio_pct']:g}%")
            if len(candidates) == 2 or attempts == 3:
                return candidates
        if page >= int(body.get("total_page") or 0):
            break
    return candidates


def dart_disclosures(api_key, corp_code, now):
    response = requests.get(
        "https://opendart.fss.or.kr/api/list.json",
        params={"crtfc_key": api_key, "corp_code": corp_code, "bgn_de": (now - timedelta(days=14)).strftime("%Y%m%d"),
                "end_de": now.strftime("%Y%m%d"), "page_count": "100"},
        timeout=6,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") == "013":
        return [], []
    if body.get("status") != "000":
        raise ValueError("DART disclosure query failed")
    filings, risks = [], []
    today = now.strftime("%Y%m%d")
    for row in body.get("list", []):
        day, receipt, title = (row.get(k, "") for k in ("rcept_dt", "rcept_no", "report_nm"))
        if not re.fullmatch(r"\d{8}", day) or day > today or not re.fullmatch(r"\d{14}", receipt) or not title:
            continue
        filing = {"filing_date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "form": title,
                  "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}", "source": "DART"}
        filings.append(filing)
        if day == today and DART_RISK_TITLE.search(title):
            risks.append({**filing, "available_at": now.isoformat(), "headline": title})
    filings.sort(key=lambda record: record["filing_date"], reverse=True)
    shown = filings[:3]
    for filing in shown:
        if "단일판매" in filing["form"] and "공급계약" in filing["form"]:
            try:
                filing["contract"] = dart_contract_details(api_key, filing["url"].rsplit("=", 1)[-1])
            except requests.RequestException as exc:
                print(f"DART contract document unavailable ({type(exc).__name__})")
            break
    return shown, risks


def dart_quarterly_financials(api_key, corp_code, filings):
    """Use three-month income statement fields from the matching DART filing."""
    codes = {"03": "11013", "06": "11012", "09": "11014"}
    report = next((item for item in filings
                   if re.search(r"(분기보고서|반기보고서).*\((\d{4})\.(03|06|09)\)", item["form"])), None)
    if report is None:
        return {}
    match = re.search(r"(분기보고서|반기보고서).*\((\d{4})\.(03|06|09)\)", report["form"])
    year, month = match.group(2, 3)
    response = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                            params={"crtfc_key": api_key, "corp_code": corp_code, "bsns_year": year,
                                    "reprt_code": codes[month], "fs_div": "CFS"}, timeout=6)
    response.raise_for_status()
    body = response.json()
    if body.get("status") not in {"000", "013"}:
        raise ValueError("DART quarterly financial query failed")
    financial_scope = "연결"
    if body.get("status") == "013" or not body.get("list"):
        response = requests.get("https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                                params={"crtfc_key": api_key, "corp_code": corp_code, "bsns_year": year,
                                        "reprt_code": codes[month], "fs_div": "OFS"}, timeout=6)
        response.raise_for_status()
        body = response.json()
        financial_scope = "별도"
        if body.get("status") not in {"000", "013"}:
            raise ValueError("DART separate financial query failed")
    receipt = report["url"].rsplit("=", 1)[-1]
    accounts = {row["account_id"]: row for row in body.get("list", [])
                if row.get("sj_div") in {"IS", "CIS"} and row.get("rcept_no") == receipt and row.get("account_id")}
    revenue = accounts.get("ifrs-full_Revenue") or accounts.get("ifrs_Revenue")
    income = accounts.get("dart_OperatingIncomeLoss")
    if not revenue or not income:
        return {}

    def amount(row, key):
        try:
            value = float(str(row.get(key, "")).replace(",", ""))
            return value if math.isfinite(value) else None
        except ValueError:
            return None

    sales = amount(revenue, "thstrm_amount")
    operating = amount(income, "thstrm_amount")
    if sales is None or sales <= 0 or operating is None:
        return {}
    snapshot = {"quarter": f"{year}-{month}", "operating_margin_pct": 100 * operating / sales,
                "financial_source": f"DART {financial_scope}", "financial_filing_url": report["url"],
                "revenue_amount": sales, "operating_income_amount": operating}
    prior_sales = amount(revenue, "frmtrm_q_amount")
    if prior_sales is not None and prior_sales > 0:
        snapshot["revenue_yoy_pct"] = 100 * (sales / prior_sales - 1)
        prior_operating = amount(income, "frmtrm_q_amount")
        if prior_operating is not None:
            snapshot["operating_margin_yoy_pp"] = 100 * (operating / sales - prior_operating / prior_sales)
    all_accounts = {row.get("account_id"):row for row in body.get("list",[])
                    if row.get("rcept_no")==receipt and row.get("account_id")}
    cfo = all_accounts.get("ifrs-full_CashFlowsFromUsedInOperatingActivities")
    if cfo:
        cash = amount(cfo,"thstrm_amount")
        ytd_income = amount(income,"thstrm_add_amount") if month!="03" else operating
        if cash is not None and ytd_income is not None and ytd_income>0:
            snapshot["operating_cash_conversion"] = cash/ytd_income
            snapshot["cashflow_period"] = f"{year} year-to-date through {month}"
    liabilities = all_accounts.get("ifrs-full_Liabilities")
    equity = all_accounts.get("ifrs-full_Equity")
    if liabilities and equity:
        total_liabilities, total_equity = amount(liabilities,"thstrm_amount"), amount(equity,"thstrm_amount")
        if total_liabilities is not None and total_equity is not None and total_equity>0:
            snapshot["liabilities_to_equity"] = total_liabilities/total_equity
    return snapshot


def company_quality_metrics(ticker, snapshot, now):
    """Read operating cash, leverage and earnings expectations with dated statements."""
    result = {}
    try:
        info = ticker.get_info() or {}
        for source, target in (("trailingPE","trailing_pe"),("forwardPE","forward_pe"),
                               ("forwardEps","forward_eps"),("trailingEps","trailing_eps")):
            value = info.get(source)
            if isinstance(value,(int,float)) and math.isfinite(value):
                result[target] = float(value)
        for source,target in (("sector","sector"),("industry","industry"),
                              ("quoteType","quote_type"),("financialCurrency","financial_currency"),
                              ("longBusinessSummary","business_description"),("longName","company_name"),
                              ("exchange","exchange")):
            if info.get(source):
                result[target] = info[source]
        if info.get("country"):
            result["country"] = info["country"]
        result["profile_available_at"] = datetime.now(now.tzinfo).isoformat()
        result["profile_source"] = "Yahoo Finance issuer profile"
    except Exception as exc:
        result["profile_error"] = type(exc).__name__
    if result.get("quote_type") == "ETF":
        return result

    def value(frame, keys, column):
        key = next((key for key in keys if key in frame.index), None)
        if key is None:
            return None
        number = float(frame.loc[key,column])
        return number if math.isfinite(number) else None

    quarter = snapshot.get("quarter","")[:7]
    try:
        flow = ticker.get_cash_flow(freq="quarterly")
        column = next((column for column in sorted(flow.columns, reverse=True)
                       if column.date() < now.date() and str(column.date())[:7] == quarter), None)
        if column is not None:
            cash = value(flow,("OperatingCashFlow","Operating Cash Flow"),column)
            free = value(flow,("FreeCashFlow","Free Cash Flow"),column)
            revenue, income = snapshot.get("revenue_amount"), snapshot.get("operating_income_amount")
            if free is not None and revenue is not None and revenue>0:
                result["free_cash_flow_margin_pct"] = 100*free/revenue
            if cash is not None and income is not None and income>0:
                result["operating_cash_conversion"] = cash/income
            result["cashflow_quarter"] = str(column.date())
    except Exception as exc:
        result["cashflow_error"] = type(exc).__name__
    if result.get("sector") in {"Financial Services","Real Estate"}:
        result["leverage_comparison"] = "industry_specific_model_required"
        return result
    try:
        balance = ticker.get_balance_sheet(freq="quarterly")
        columns = sorted((column for column in balance.columns if column.date()<now.date()), reverse=True)
        if columns:
            column = columns[0]
            debt = value(balance,("TotalDebt","Total Debt"),column)
            cash = value(balance,("CashCashEquivalentsAndShortTermInvestments",
                                  "Cash Cash Equivalents And Short Term Investments",
                                  "CashAndCashEquivalents","Cash And Cash Equivalents"),column)
            equity = value(balance,("StockholdersEquity","Stockholders Equity",
                                    "CommonStockEquity","Common Stock Equity"),column)
            if debt is not None and cash is not None and equity is not None and equity>0:
                result["net_debt_to_equity"] = (debt-cash)/equity
            result["balance_sheet_quarter"] = str(column.date())
    except Exception as exc:
        result["balance_sheet_error"] = type(exc).__name__
    return result


def company_context(market, symbols, now):
    from concurrent.futures import ThreadPoolExecutor

    overseas = market.startswith("US-")
    global_events, global_coverage = world_events(now)
    dart_key = "" if overseas else os.getenv("DART_API_KEY", "").strip()
    corp_codes = {}
    if dart_key:
        try:
            corp_codes = dart_company_codes(dart_key)
        except (requests.RequestException, ValueError) as exc:
            print(f"{market} DART issuer map unavailable ({type(exc).__name__})")
    elif not overseas:
        print(f"{market} DART_API_KEY missing; domestic disclosures unavailable")

    history_prices = None
    benchmark_symbol = BENCHMARKS["US" if overseas else "KR"]
    mapped_tickers = {symbol: (symbol[4:] if overseas else symbol + (
        ".KQ" if symbol + ".KQ" in KR_SYMBOL_NAMES else ".KS")) for symbol in symbols}
    try:
        history_prices = yf.download([*mapped_tickers.values(), benchmark_symbol], period="6mo",
                                     auto_adjust=False, group_by="ticker", progress=False,
                                     threads=4, timeout=8, multi_level_index=True)
    except Exception as exc:
        print(f"{market} company price history unavailable ({type(exc).__name__})")

    def fetch(symbol):
        suffix = ".KQ" if symbol + ".KQ" in KR_SYMBOL_NAMES else ".KS"
        ticker = yf.Ticker(symbol[4:] if overseas else symbol + suffix)
        snapshot, risks = {"filing_status": "unavailable" if overseas else (
            "missing_dart_key" if not dart_key else "issuer_mapping_unavailable")}, []
        try:
            articles = ticker.get_news(count=12)
            snapshot["news"] = recent_company_news(articles, now)[:3]
            snapshot["news_status"] = "ok" if snapshot["news"] else "no_recent_articles"
            risks.extend(headline_risks(market, symbol, articles, now))
        except Exception as exc:
            print(f"{market} {symbol} news unavailable ({type(exc).__name__})")
        if not overseas and not snapshot.get("news"):
            try:
                articles = korean_company_news(symbol, now)
                snapshot["news"] = articles[:3]
                risks.extend({**item, "kind": "event", "market": market, "symbol": symbol,
                              "assessment": "risk"} for item in articles
                             if RISK_HEADLINE.search(item["headline"]))
                snapshot["news_status"] = "ok" if articles else "no_recent_articles"
            except Exception as exc:
                snapshot["news_status"] = "unavailable"
                print(f"{market} {symbol} Korean news unavailable ({type(exc).__name__})")
        else:
            snapshot["news_status"] = "ok" if snapshot.get("news") else snapshot.get("news_status", "unavailable")
        try:
            snapshot.update(company_financials(ticker.get_income_stmt(freq="quarterly"), now.date()))
        except Exception as exc:
            print(f"{market} {symbol} financials unavailable ({type(exc).__name__})")
        try:
            dates = sorted(str(day)[:10] for day in (ticker.calendar or {}).get("Earnings Date", []))
            snapshot["earnings_dates"] = dates
            if now.date().isoformat() in dates:
                risks.append({"kind": "event", "market": market, "symbol": symbol,
                              "available_at": now.isoformat(), "assessment": "risk",
                              "source": "Yahoo Finance earnings calendar",
                              "headline": "당일 실적 발표 예정"})
        except Exception as exc:
            print(f"{market} {symbol} earnings calendar unavailable ({type(exc).__name__})")
        if overseas:
            try:
                snapshot["filings"], filing_risks = us_company_filings(ticker, now)
                snapshot["filing_status"] = "checked"
                risks.extend({**record, "kind": "event", "market": market, "symbol": symbol,
                              "assessment": "risk"} for record in filing_risks)
            except Exception as exc:
                print(f"{market} {symbol} filing index unavailable ({type(exc).__name__})")
        elif corp_codes.get(symbol):
            try:
                snapshot["filings"], filing_risks = dart_disclosures(dart_key, corp_codes[symbol], now)
                snapshot["filing_status"] = "checked"
                risks.extend({**record, "kind": "event", "market": market, "symbol": symbol,
                              "assessment": "risk"} for record in filing_risks)
            except (requests.RequestException, ValueError, TypeError) as exc:
                snapshot["filing_status"] = "unavailable"
                print(f"{market} {symbol} DART unavailable ({type(exc).__name__})")
            try:
                reports = dart_financial_reports(dart_key, corp_codes[symbol], now)
                dart_financials = dart_quarterly_financials(dart_key, corp_codes[symbol], reports)
                if dart_financials:
                    snapshot.pop("revenue_yoy_pct", None)
                    snapshot.pop("operating_margin_yoy_pp", None)
                    snapshot.update(dart_financials)
            except (requests.RequestException, ValueError, TypeError) as exc:
                print(f"{market} {symbol} DART financials unavailable ({type(exc).__name__})")
        snapshot["market"] = "US" if overseas else "KR"
        snapshot.update(company_quality_metrics(ticker, snapshot, now))
        snapshot["source_symbol"] = mapped_tickers[symbol]
        company_events = []
        for news in snapshot.get("news", [])[:3]:
            body, body_status = article_text(news.get("url"))
            company_events.append({**news, "body": body, "body_status": body_status,
                                   "event_id": news.get("url"), "scope": "company"})
        snapshot["event_analysis"] = analyze_events(snapshot, [*global_events, *company_events],
                                                     datetime.now(now.tzinfo), global_coverage)
        if history_prices is not None:
            try:
                data = history_prices[mapped_tickers[symbol]]
                benchmark = history_prices[benchmark_symbol]
                local_day = now.astimezone(ZoneInfo("America/New_York") if overseas else KST).date()
                data = clean_prices(data.loc[data.index.date < local_day])
                benchmark = clean_prices(benchmark.loc[benchmark.index.date < local_day])
                snapshot["price_indicators"] = calculate_indicators(data, benchmark)
            except (KeyError, ValueError, TypeError) as exc:
                snapshot["price_history_error"] = type(exc).__name__
        snapshot["available_at"] = datetime.now(now.tzinfo).isoformat()
        snapshot["checked_at"] = now.isoformat()
        snapshot["financial_status"] = "ok" if "operating_margin_pct" in snapshot else "unavailable"
        return symbol, snapshot, risks

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, symbols))
    snapshots = {symbol: snapshot for symbol, snapshot, _ in results}
    from economic_graph import save_profiles
    save_profiles(snapshots)
    for symbol, snapshot, risks in results:
        print(f"{market} {symbol} company context {json.dumps(snapshot, ensure_ascii=False)}", flush=True)
        for risk in risks:
            print(f"{market} {symbol} risk evidence {json.dumps(risk, ensure_ascii=False)}", flush=True)
    return snapshots, [risk for _, _, risks in results for risk in risks]


def domestic_research(market, symbols, now):
    url = "https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=1800"
    verified = {("2026-11-12", "035420", "매수")}
    try:
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", response.text, re.I | re.S):
            cells = " ".join(unescape(re.sub(r"<[^>]+>", " ", row)).split())
            match = re.search(r"(\d{4}-\d{2}-\d{2}).*?\((\d{6})\s*/\s*([^()]+)\)", cells)
            if match:
                verified.add(match.groups())
        if len(verified) == 1:
            print(f"{market} domestic research listing returned no parseable rows")
    except requests.RequestException as exc:
        print(f"{market} domestic research listing unavailable ({type(exc).__name__})")
    evidence = []
    for day, symbol, grade in sorted(verified):
        stamp = datetime.fromisoformat(day).replace(tzinfo=KST) + timedelta(days=1)
        if symbol in symbols and now - timedelta(days=7) <= stamp < now:
            evidence.append({"kind": "research", "market": market, "symbol": symbol,
                             "available_at": stamp.isoformat(), "source": url,
                             "assessment": "supportive" if grade.strip() == "매수" else "neutral"})
    print(f"{market} recent domestic brokerage records: {len(evidence)}")
    return evidence


def research_evidence(market, symbols, now):
    if not market.startswith("US-"):
        return domestic_research(market, symbols, now)
    evidence = []
    for symbol in symbols:
        ticker = symbol[4:]
        try:
            grades = yf.Ticker(ticker).get_upgrades_downgrades().sort_index(ascending=False)
            for published, grade in grades.head(20).iterrows():
                stamp = published.to_pydatetime()
                # Yahoo's grade timestamps are UTC when the index has no timezone.
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=ZoneInfo("UTC"))
                if not now - timedelta(days=7) <= stamp < now:
                    continue
                opinion = str(grade.get("ToGrade", "")).strip().casefold()
                if not opinion or opinion == "nan":
                    continue
                strong = {"buy", "strong buy", "outperform", "overweight", "market outperform"}
                evidence.append({"kind": "research", "market": market, "symbol": symbol,
                                 "available_at": stamp.isoformat(),
                                 "assessment": "supportive" if opinion in strong else "neutral"})
        except Exception as exc:
            print(f"{ticker} research context unavailable ({type(exc).__name__})")
    print(f"{market} recent brokerage rating records: {len(evidence)}")
    return evidence


def scheduled_event_evidence(market, now, decision_at):
    """Screen the holding window and preceding 30 minutes against BLS releases."""
    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    # Verified BLS November/December lists and Fed FOMC calendar.
    fallback = (
        "2026-11-13T08:30", "2026-11-17T08:30",
        "2026-11-18T14:00", "2026-11-20T10:00", "2026-12-01T10:00",
        "2026-12-02T10:00", "2026-12-04T08:30", "2026-12-08T08:30",
        "2026-12-09T14:00", "2026-12-10T08:30", "2026-12-15T08:30",
        "2026-12-16T10:00", "2026-12-17T08:30", "2026-12-18T10:00",
        "2026-12-30T10:00",
    )
    eastern = ZoneInfo("America/New_York")
    try:
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        body = re.sub(r"\r?\n[ \t]", "", response.text)
        starts = []
        for event in body.split("BEGIN:VEVENT")[1:]:
            match = re.search(r"(?m)^DTSTART(?:;TZID=America/New_York)?:([0-9]{8}T[0-9]{6})(Z?)\r?$", event)
            if not match:
                raise ValueError("Unexpected BLS calendar timestamp")
            zone = ZoneInfo("UTC") if match[2] else eastern
            starts.append(datetime.strptime(match[1], "%Y%m%dT%H%M%S").replace(tzinfo=zone))
        if not starts or not min(starts) < decision_at < max(starts):
            raise ValueError("BLS calendar does not cover decision")
    except (requests.RequestException, ValueError) as exc:
        day = decision_at.astimezone(eastern).date().isoformat()
        if not "2026-11-14" <= day < "2026-12-31":
            print(f"{market} scheduled-event context unavailable ({type(exc).__name__})")
            return []
        print(f"{market} using checked BLS/Fed calendar through 2026-12-30")
        starts = [datetime.fromisoformat(day).replace(tzinfo=eastern) for day in fallback]
    fed = [datetime.fromisoformat(day).replace(tzinfo=eastern)
           for day in ("2026-11-18T14:00", "2026-12-09T14:00")]
    starts.extend(fed)
    risk = any(decision_at - timedelta(minutes=30) <= at <= decision_at + timedelta(minutes=31)
               for at in starts)
    state = "risk" if risk else "clear"
    print(f"{market} scheduled BLS events: {state}")
    return [{"kind": "event", "market": market, "symbol": "*",
             "available_at": now.isoformat(), "assessment": state}]


def has_premarket_liquidity(rows, end):
    """Require trades in all three completed five-minute periods."""
    first = end - timedelta(minutes=15)
    buckets = set()
    seen = set()
    traded_value = 0.0
    for row in rows:
        try:
            stamp = datetime.strptime(row["xymd"] + row["xhms"], "%Y%m%d%H%M%S").replace(tzinfo=end.tzinfo)
            volume = int(row["evol"])
            price = float(row["last"])
        except (KeyError, TypeError, ValueError):
            continue
        if first <= stamp < end and stamp not in seen and volume > 0 and 5 <= price < float("inf"):
            seen.add(stamp)
            traded_value += price * volume
            buckets.add(int((stamp - first).total_seconds()) // 300)
    return len(buckets) == 3 and traded_value >= 100_000


def us_premarket_leaders(app_key, app_secret, now):
    base = "https://openapi.koreainvestment.com:9443"
    try:
        auth = requests.post(base + "/oauth2/tokenP", timeout=5, json={
            "grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret,
        })
        auth.raise_for_status()
        headers = {"authorization": "Bearer " + auth.json()["access_token"],
                   "appkey": app_key, "appsecret": app_secret, "custtype": "P"}
        rank = requests.get(base + "/uapi/overseas-stock/v1/ranking/volume-surge",
                            headers=dict(headers, tr_id="HHDFS76270000"),
                            params={"EXCD": "NAS", "MINX": "3", "VOL_RANG": "2", "AUTH": "", "KEYB": ""},
                            timeout=5)
        rank.raise_for_status()
        payload = rank.json()
        if payload.get("rt_cd") != "0":
            print("US-late premarket ranking unavailable:", payload.get("msg_cd"))
            return []
        tickers = []
        for row in payload.get("output2") or []:
            ticker = str(row.get("symb") or "")
            name = row.get("knam") or row.get("enam") or row.get("name") or US_SYMBOL_NAMES.get(ticker, "")
            if instrument_rejection_reason(ticker, name) is None:
                tickers.append(ticker)
                if name:
                    US_SYMBOL_NAMES[ticker] = name
        tickers = list(dict.fromkeys(tickers))
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"US-late premarket ranking unavailable ({type(exc).__name__})")
        return []

    end = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    leaders = []
    for ticker in [t for t in tickers if re.fullmatch(r"[A-Z]{1,6}", t) and t not in US_SYMBOLS][:12]:
        try:
            chart = requests.get(base + "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
                                 headers=dict(headers, tr_id="HHDFS76950200"),
                                 params={"AUTH": "", "EXCD": "NAS", "SYMB": ticker, "NMIN": "1",
                                         "PINC": "0", "NEXT": "", "NREC": "30", "FILL": "", "KEYB": ""},
                                 timeout=5)
            chart.raise_for_status()
            body = chart.json()
            if body.get("rt_cd") == "0" and has_premarket_liquidity(body.get("output2") or [], end):
                leaders.append("DNAS" + ticker)
                if len(leaders) == 4:
                    break
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            print(f"US-late {ticker} liquidity check unavailable ({type(exc).__name__})")
    print(f"US-late 신규 프리장 감시: {', '.join(leaders) or '없음'}", flush=True)
    return leaders


def korean_intraday_leaders(market, app_key, app_secret):
    base = "https://openapi.koreainvestment.com:9443"
    auth = requests.post(base + "/oauth2/tokenP", timeout=8, json={
        "grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret,
    })
    auth.raise_for_status()
    response = requests.get(
        base + "/uapi/domestic-stock/v1/quotations/volume-rank",
        headers={"authorization": "Bearer " + auth.json()["access_token"], "appkey": app_key,
                 "appsecret": app_secret, "tr_id": "FHPST01710000", "custtype": "P"},
        params={"FID_COND_MRKT_DIV_CODE": {"KRX": "J", "NXT": "NX"}[market],
                "FID_COND_SCR_DIV_CODE": "20171", "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "1", "FID_BLNG_CLS_CODE": "3",
                "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
        timeout=8,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("rt_cd") != "0" or not isinstance(body.get("output"), list):
        raise RuntimeError("KIS 장중 거래대금 순위 응답 오류")
    ranked = select_intraday_leaders(body["output"], limit=30)
    dart_key = os.getenv("DART_API_KEY", "").strip()
    catalysts = []
    if dart_key:
        try:
            catalysts = dart_contract_candidates(dart_key, ranked, datetime.now(KST))
            print(f"{market} 당일 공급계약 우선 감시: {', '.join(catalysts) or '없음'}")
        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"{market} DART 계약 후보 조회 실패 ({type(exc).__name__})")
    selected = list(dict.fromkeys([*catalysts, *ranked]))[:len(SYMBOLS)]
    return {symbol: ranked[symbol] for symbol in selected}


def select_intraday_leaders(rows, limit=len(SYMBOLS)):
    leaders = {}
    for row in rows:
        symbol = str(row.get("mksc_shrn_iscd") or "")
        try:
            turnover = int(row.get("acml_tr_pbmn") or 0)
            gain = float(row.get("prdy_ctrt") or 0)
        except (ValueError, TypeError):
            continue
        name = str(row.get("hts_kor_isnm") or symbol)
        if (re.fullmatch(r"[0-9A-Z]{6}", symbol) and turnover >= 1_000_000_000 and 0 < gain <= 8
                and instrument_rejection_reason(symbol, name) is None):
            leaders.setdefault(symbol, name)
        if len(leaders) >= limit:
            break
    return leaders


def daily_candidates(previous_session, overseas=False):
    report = Path("opening-scan/latest.json")
    if not overseas and report.is_file():
        try:
            results = json.loads(report.read_text(encoding="utf-8"))["results"]
            fresh = [item for item in results if item["market"] == "KR"
                     and item["symbol"].endswith((".KS", ".KQ"))
                     and item["indicators"]["data_date"] == previous_session.isoformat()]
            if fresh:
                from economic_graph import select_analysis_candidates
                event_symbols = [item["symbol"] for item in fresh
                    if any(link.get("direction",0)>0 for link in item.get("company_analysis",{}).get(
                        "event_analysis",{}).get("economic_graph",{}).get("links",[]))
                    or item.get("company_analysis",{}).get("event_analysis",{}).get(
                        "economic_graph",{}).get("expectation_links")]
                candidates = select_analysis_candidates(fresh,"KR",event_symbols,limit=len(SYMBOLS))
                for item in candidates:
                    KR_SYMBOL_NAMES[item["symbol"]] = item.get("name") or item["symbol"]
                    KR_SYMBOL_NAMES[f"{item['symbol'].split('.')[0]}.KS"] = KR_SYMBOL_NAMES[item["symbol"]]
                print(f"국내 직전 거래일 스캔에서 장중 감시 종목 {len(candidates)}개 선정")
                return [item["symbol"].split(".")[0] for item in candidates]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"직전 거래일 스캔 사용 실패 ({type(exc).__name__})")
    if not overseas:
        print("직전 거래일 전 종목 스캔 자료 없음: 기존 감시 목록 사용")
    tickers = list(US_SYMBOLS) if overseas else [f"{symbol}.KS" for symbol in SYMBOLS]
    market = "US" if overseas else "KR"
    benchmark_symbol = BENCHMARKS[market]
    prices = yf.download(
        list(dict.fromkeys([*tickers, benchmark_symbol])), period="1y", interval="1d", auto_adjust=True,
        group_by="ticker", progress=False, threads=6, timeout=15,
    )
    try:
        benchmark = clean_prices(prices[benchmark_symbol].loc[prices[benchmark_symbol].index.date <= previous_session])
    except (KeyError, ValueError):
        print("시장 비교 자료 미확보: 일봉 후보 선정 보류")
        return []
    candidates = []
    for ticker in tickers:
        try:
            history = prices[ticker]
            history = clean_prices(history.loc[history.index.date <= previous_session])
            if history.index[-1].date() != previous_session:
                continue
            indicators = calculate_indicators(history, benchmark)
            signal = classify_signal(indicators)
            if signal["stage"] in {"장전 관찰 후보", "장중 조건부 진입 후보"}:
                candidates.append({"market": market, "symbol": ticker,
                                   "name": (US_SYMBOL_NAMES if overseas else KR_SYMBOL_NAMES).get(ticker, ticker),
                                   "indicators": indicators, "signal": signal})
        except (KeyError, ValueError):
            print(f"일봉 자료 제외: {ticker}")
    selected = rank_market_candidates(candidates, market, limit=len(SYMBOLS))
    return [f"DNAS{item['symbol']}" if overseas else item["symbol"].split(".")[0] for item in selected]

def update_bar(bars, slot, event_time, price, size):
    bar = bars.get(slot)
    if bar is None:
        bars[slot] = {"high": price, "low": price, "close": price, "last": event_time, "volume": size}
        return
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["volume"] += size
    if event_time >= bar["last"]:
        bar["close"], bar["last"] = price, event_time


def evaluate_breakout(bars, quote, close_time):
    if set(bars) != {0, 1, 2}:
        return "missing_bars", None
    if quote is None:
        return "missing_quote", None
    age = (close_time - datetime.fromisoformat(quote["event_time"])).total_seconds()
    if not 0 <= age <= MAX_QUOTE_AGE:
        return "stale_quote", None
    if quote["spread_pct"] > MAX_SPREAD_PCT:
        return "wide_spread", None
    volume = (bars[0]["volume"] + bars[1]["volume"]) / 2
    if volume <= 0 or bars[2]["volume"] / volume < MIN_VOLUME_RATIO:
        return "low_volume", None
    if bars[2]["close"] <= max(bars[0]["high"], bars[1]["high"]):
        return "no_price_breakout", None
    return "base_breakout", (bars[2]["volume"] / volume, quote["spread_pct"], bars[2]["close"])


def candidate_metrics(bars, quote, close_time):
    return evaluate_breakout(bars, quote, close_time)[1]


def evaluate_candidate(bars, quote, closes, window, symbol, evidence, overseas, observation_only=False, prior_highs=(), company=None, sectors=()):
    ticker = symbol[4:] if overseas else symbol
    name = US_SYMBOL_NAMES.get(ticker, ticker) if overseas else (
        KR_SYMBOL_NAMES.get(symbol + ".KS") or KR_SYMBOL_NAMES.get(symbol + ".KQ") or symbol)
    reason = instrument_rejection_reason(symbol, name)
    if reason:
        return reason, None
    if overseas and quote is not None and quote.get("ask", 0) < 5:
        return "low_price", None
    status, metrics = evaluate_breakout(bars, quote, closes)
    if metrics is None:
        return status, None
    if observation_only or evidence is None:
        status = context_status(evidence or [], window, symbol, closes)
        return status if status != "supported" else "observation_only", (symbol, *metrics, None, None, None)
    limit = breakout_v2(bars, quote, closes)
    if limit is None:
        return "strict_breakout_failed", None
    status = context_status(evidence, window, symbol, closes)
    if status != "supported":
        return status, None
    company = company if company is not None else {}
    applicable = [row for row in evidence if row.get("symbol") in (symbol, "*")
                  and row.get("market") in (window, "*")]
    forecast = build_company_forecast(company, company.get("price_indicators", {}),
                                      limit, closes, applicable, sectors)
    company["forecast"] = forecast
    if forecast["composite_score"] is None:
        return "composite_assessment_incomplete", None
    if forecast["composite_score"] <= 50 or forecast["base_price"] <= limit:
        return "composite_outlook_not_positive", None
    # Require the base scenario to cover 2% risk at 1.5:1, plus 0.3% friction.
    # This is a risk policy, not a historically optimized profitability claim.
    if forecast["base_price"] / limit - 1 < 0.033:
        return "insufficient_expected_reward", None
    stop = limit * 0.98
    if quote["bid"] <= stop:
        return "entry_below_fixed_stop", None
    projection = (forecast["base_price"], "종합평가 5거래일 시나리오")
    return "buy_signal", (symbol, *metrics, limit, stop, projection)


async def collect(approval, market, symbols, opens, on_signal, samples_path, evidence=None, observation_only=False, resistance=None, refresh_context=None, companies=None, sector_snapshot=None, followup_minutes=30):
    resistance = resistance or {}
    closes = opens + timedelta(minutes=15)
    feeds = FEEDS[market]
    pending = {(tr_id, symbol) for tr_id in feeds for symbol in symbols}
    rejected = set()
    bars = {symbol: {} for symbol in symbols}
    quotes = {}
    followup = {symbol: [] for symbol in symbols}
    evaluated = False
    refresh_task = None
    next_refresh = opens + timedelta(minutes=5)
    deadline = closes
    async with connect(FEED_URL, open_timeout=15, close_timeout=3) as socket:
        for tr_id, symbol in sorted(pending):
            await socket.send(json.dumps(subscription(approval, tr_id, symbol)))
        while True:
            received = datetime.now(KST)
            if refresh_task is not None and refresh_task.done():
                try:
                    refreshed, events = refresh_task.result()
                    if not evaluated:
                        if companies is not None:
                            companies.update(refreshed)
                        evidence.extend(events)
                        Path("signal-context.json").write_text(json.dumps(evidence), encoding="utf-8")
                        print(f"{samples_path.stem} 기업·뉴스 갱신 완료 {received.isoformat()}", flush=True)
                    else:
                        print(f"{samples_path.stem} 판정 후 도착한 기업·뉴스 자료 제외", flush=True)
                except Exception as exc:
                    print(f"{samples_path.stem} 기업·뉴스 갱신 실패 ({type(exc).__name__})", flush=True)
                refresh_task = None
            if refresh_context is not None and not evaluated and received < closes and received >= next_refresh and refresh_task is None:
                refresh_task = asyncio.create_task(asyncio.to_thread(refresh_context))
                next_refresh = received + timedelta(minutes=5)
            seconds_left = (deadline - received).total_seconds()
            if seconds_left <= 0:
                if evaluated:
                    break
                rejected.update(symbol for _, symbol in pending)
                print(f"{market} 구독 검증 {len(symbols) - len(rejected)}/{len(symbols)}")
                candidates = []
                decisions = {}
                composite_scores = {}
                for symbol in symbols:
                    if symbol in rejected:
                        decisions[symbol] = "subscription_failed"
                        continue
                    status, candidate = evaluate_candidate(
                        bars[symbol], quotes.get(symbol), closes, samples_path.stem, symbol,
                        evidence, market == "US", observation_only, resistance.get(symbol, ()),
                        company=(companies or {}).get(symbol),
                        sectors=(sector_snapshot or {}).get("rows", []),
                    )
                    decisions[symbol] = status
                    composite_scores[symbol] = (companies or {}).get(symbol, {}).get("forecast", {}).get("composite_score")
                    if candidate is not None:
                        candidates.append(candidate)
                counts = {status: sum(value == status for value in decisions.values())
                          for status in sorted(set(decisions.values()))}
                print(f"{samples_path.stem} 판정 집계 {json.dumps(counts)}", flush=True)
                for symbol, status in decisions.items():
                    print(f"{samples_path.stem} {symbol} 판정 {status}", flush=True)
                samples_path.with_name(f"{samples_path.stem}-decisions-replay.json").write_text(
                    json.dumps({"signal_at": closes.isoformat(), "counts": counts, "symbols": decisions, "composite_scores": composite_scores},
                               indent=2), encoding="utf-8",
                )
                if len(rejected) == len(symbols):
                    raise RuntimeError("개장 전 구독 승인에 성공한 종목이 없습니다")
                notification = asyncio.create_task(asyncio.to_thread(
                    on_signal, sorted(candidates, key=lambda row: (-(composite_scores.get(row[0]) or 0), -row[1], row[0]))
                ))
                evaluated = True
                deadline = closes + timedelta(minutes=followup_minutes, seconds=30)
                continue
            try:
                raw = await asyncio.wait_for(socket.recv(decode=True), min(seconds_left, 60))
            except TimeoutError:
                continue
            received = datetime.now(KST)
            if raw.startswith("{"):
                status = json.loads(raw)
                header = status.get("header", {})
                if header.get("tr_id") == "PINGPONG":
                    await socket.pong(raw.encode("utf-8"))
                    continue
                pair = (header.get("tr_id"), header.get("tr_key"))
                if pair in pending:
                    pending.remove(pair)
                    if status.get("body", {}).get("rt_cd") != "0" or received >= opens:
                        rejected.add(pair[1])
                continue
            for item in parse_records(raw, feeds, received):
                symbol = item["symbol"]
                if market == "US" and not symbol.startswith(("DNAS","DNYS","DAMS")):
                    matches = [key for key in bars if key[4:]==symbol]
                    if len(matches)!=1:
                        continue
                    symbol = matches[0]
                event_time = datetime.fromisoformat(item["event_time"])
                if symbol not in bars or symbol in rejected or not opens <= event_time:
                    continue
                if not -1 <= item["lag_seconds"] <= MAX_QUOTE_AGE:
                    continue
                if evaluated:
                    if item["kind"] == "quote" and event_time >= closes and received <= deadline:
                        followup[symbol].append({"at": received.isoformat(), "bid": item["bid"], "ask": item["ask"]})
                elif received < closes and event_time < closes and item["kind"] == "trade":
                    slot = int((event_time - opens).total_seconds()) // BAR_SECONDS
                    update_bar(bars[symbol], slot, event_time, item["price"], item["size"])
                elif received < closes and event_time < closes and item["kind"] == "quote" and (
                    symbol not in quotes or item["event_time"] >= quotes[symbol]["event_time"]
                ):
                    quotes[symbol] = item

    with samples_path.open("w", encoding="utf-8") as output:
        for symbol in symbols:
            if symbol in rejected or set(bars[symbol]) != {0, 1, 2}:
                continue
            quote = quotes.get(symbol)
            before = [{"at": quote["event_time"], "bid": quote["bid"], "ask": quote["ask"]}] if quote else []
            row = {
                "market": samples_path.stem,
                "symbol": symbol,
                "signal_at": closes.isoformat(),
                "daily_eligible": True,
                "prior_highs": resistance.get(symbol, []),
                "bars": [
                    {"start": (opens + timedelta(minutes=5 * slot)).isoformat(),
                     **{key: bar[key] for key in ("high", "low", "close", "volume")}}
                    for slot, bar in sorted(bars[symbol].items())
                ],
                "quotes": before + followup[symbol],
            }
            output.write(json.dumps(row, allow_nan=False) + "\n")
    await notification
    if refresh_task is not None:
        try:
            await refresh_task
        except Exception as exc:
            print(f"{samples_path.stem} 기업·뉴스 갱신 종료 ({type(exc).__name__})", flush=True)


def event_impact(evidence, market, symbol, decision_at):
    """Score recent company-specific evidence without counting duplicate headlines."""
    grades = set()
    for row in evidence or ():
        if row["kind"] != "event" or row["symbol"] != symbol or row["market"] not in (market, "*"):
            continue
        stamp = datetime.fromisoformat(row["available_at"])
        if decision_at - timedelta(hours=24) <= stamp < decision_at:
            grades.add(row["assessment"])
    return (1 if "positive" in grades else 0) - (2 if "risk" in grades else 0)


def confirmed_contract_events(companies, market, observed_at):
    return [
        {"kind": "event", "market": market, "symbol": symbol,
         "available_at": observed_at.isoformat(), "assessment": "positive", "source": "DART",
         "headline": filing["form"], "url": filing["url"]}
        for symbol, company in companies.items()
        for filing in company.get("filings", [])
        if filing.get("source") == "DART"
        and filing["filing_date"] == observed_at.date().isoformat()
        and not any(word in filing["form"] for word in ("정정", "해지", "취소"))
        and filing.get("contract", {}).get("amount_krw", 0) > 0
        and filing["contract"].get("revenue_ratio_pct", 0) >= 5
    ]


def research_label(evidence, market, symbol, decision_at):
    opinions = []
    for row in evidence:
        if row["kind"] != "research" or row["market"] not in (market, "*") or row["symbol"] != symbol:
            continue
        timestamp = datetime.fromisoformat(row["available_at"])
        if decision_at - timedelta(days=7) <= timestamp < decision_at:
            opinions.append((timestamp, row["assessment"]))
    if not opinions:
        return "최근 자료 미확보"
    latest = max(at for at, _ in opinions)
    supportive = all(grade == "supportive" for at, grade in opinions if at == latest)
    return ("우호" if supportive else "우호 의견 미확인") + f" · {latest.astimezone(KST):%m/%d}"


def format_notification(market, candidates, closes, companies=None, sectors=None):
    companies = companies or {}
    overseas = market.startswith("US-")
    confirmed = [row for row in candidates if row[4] is not None
                 and companies.get(row[0], {}).get("forecast", {}).get("composite_score") is not None]
    timestamp = f"{closes:%m/%d %H:%M} ET" if overseas else f"{closes:%m/%d %H:%M} KST"
    lines = format_sector_header(sectors, "US" if overseas else "KR") + ["", f"매수 신호 · {timestamp}"]
    if not confirmed:
        lines.append("확인된 매수 신호 없음")
    for symbol, ratio, spread, price, entry_limit, stop, projection in confirmed[:5]:
        ticker = symbol[4:] if overseas else symbol
        name = US_SYMBOL_NAMES.get(ticker, ticker) if overseas else (
            KR_SYMBOL_NAMES.get(symbol+".KS") or KR_SYMBOL_NAMES.get(symbol+".KQ") or symbol)
        forecast = companies[symbol]["forecast"]
        step = Decimal("0.01" if overseas else "1")
        limit = Decimal(str(entry_limit)).quantize(step, rounding=ROUND_DOWN)
        entry_label = f"$" + f"{limit:,.2f}" if overseas else f"{limit:,.0f}원"
        base, upside = forecast["base_price"], forecast["bull_price"]
        target_label = f"$" + f"{base:,.2f} / $" + f"{upside:,.2f}" if overseas else f"{base:,.0f} / {upside:,.0f}원"
        lines.extend(["", f"• {name} ({ticker}) · 종합 {forecast['composite_score']:.1f}/100",
                      f"진입 상한 {entry_label} · {(closes+timedelta(seconds=30)):%H:%M:%S}까지",
                      f"5거래일 분석 목표 · 기준/상승 {target_label}",
                      "손절 −2% · 기본 익절 +6% (실제 체결가 기준)"])
    return "\n".join(lines)

def notify(market, candidates, closes, token, chat_id, companies=None, sectors=None):
    message = format_notification(market, candidates, closes, companies, sectors)
    for message in split_telegram_text(message):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10, allow_redirects=False,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Telegram 발송 실패 (HTTP {response.status_code})")
            if response.json().get("ok") is not True:
                raise RuntimeError("Telegram이 발송을 확인하지 않았습니다")
        except (requests.RequestException, ValueError, AttributeError):
            raise RuntimeError("Telegram 발송 응답 오류") from None
        print(f"Telegram sent: {market} intraday candidates")


def filter_us_updates(candidates, today, path):
    if not path.is_file():
        print("04:15 비교 결과 없음: 이번 후보를 첫 관찰로 처리합니다")
        return candidates
    early = json.loads(path.read_text(encoding="utf-8"))
    if early["date"] != today:
        print("04:15 비교 결과 거래일 불일치: 이번 후보를 첫 관찰로 처리합니다")
        return candidates
    earlier = {row[0]: row for row in early["candidates"]}
    return [
        row for row in candidates
        if row[0] not in earlier
        or (row[4] is not None and (len(earlier[row[0]]) < 5 or earlier[row[0]][4] is None))
        or (row[3] > earlier[row[0]][3] and row[1] >= earlier[row[0]][1])
    ]


def main():
    import exchange_calendars

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market", choices=OPEN_TIMES)
    parser.add_argument("--context", type=Path, help="JSON array of timestamped evidence records")
    parser.add_argument("--intraday-leaders", action="store_true")
    args = parser.parse_args()
    evidence = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else []
    market = args.market
    overseas = market.startswith("US-")
    local_zone = ZoneInfo("America/New_York") if overseas else KST
    now = datetime.now(local_zone)
    calendar = exchange_calendars.get_calendar("XNYS" if overseas else "XKRX")
    today = now.date().isoformat()
    if not calendar.is_session(today):
        print(f"{market} 휴장일")
        return
    regular_open = calendar.session_open(today).tz_convert(local_zone).time()
    if regular_open != (time(9, 30) if overseas else OPEN_TIMES["KRX"]):
        print("개장 시각 변경 거래일은 장중 알림을 건너뜁니다")
        return
    opens = datetime.combine(now.date(), OPEN_TIMES[market], local_zone)
    if args.intraday_leaders:
        if market != "KRX":
            parser.error("--intraday-leaders requires KRX")
        opens += timedelta(minutes=65)
    scheduled_open = opens
    max_lead = 230 if overseas else (170 if args.intraday_leaders else 100)
    if not opens - timedelta(minutes=max_lead) <= now <= opens + timedelta(minutes=14):
        raise SystemExit(f"수집 창을 놓쳤거나 너무 이릅니다. 현재 {now.isoformat()}, 목표 {opens.isoformat()}")

    keys = [os.getenv(name, "").strip() for name in (
        "KIS_APP_KEY", "KIS_APP_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    )]
    if not all(keys):
        raise SystemExit("KIS 및 Telegram 비밀값 4개를 설정하세요")
    app_key, app_secret, token, chat_id = keys
    snapshot = Path("us-early-candidates.json")
    try:
        if overseas:
            context_at = scheduled_open - timedelta(minutes=25)
            delay = (context_at - datetime.now(local_zone)).total_seconds()
            if delay > 0:
                print(f"{market} 프리장 자료 수집 대기: {context_at.isoformat()}", flush=True)
                sleep(delay)
        previous_session = calendar.previous_session(today).date()
        if args.intraday_leaders:
            lookup_at = scheduled_open - timedelta(minutes=15)
            sleep(max(0, (lookup_at - datetime.now(local_zone)).total_seconds()))
            try:
                leaders = korean_intraday_leaders(market, app_key, app_secret)
            except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
                raise RuntimeError(f"KIS 장중 순위 조회 실패 ({type(exc).__name__})") from None
            symbols = list(leaders)
            for symbol, name in leaders.items():
                KR_SYMBOL_NAMES[symbol + ".KS"] = name
            print(f"{market} 장중 거래대금 감시: {', '.join(symbols) or '후보 없음'}")
        elif overseas:
            # The prior daily screen must not prevent premarket observation.
            symbols = [f"DNAS{ticker}" for ticker in US_SYMBOLS]
            if market == "US-late":
                symbols.extend(us_premarket_leaders(app_key, app_secret, datetime.now(local_zone)))
            print(f"{market} 프리장 체결·호가 감시 {len(symbols)}종목 (일봉 통과 조건 없음)")
        else:
            symbols = daily_candidates(previous_session)
        economic_symbols = event_watch_symbols(market,datetime.now(local_zone))
        symbols = list(dict.fromkeys([*economic_symbols,*symbols]))[:18]
        if symbols:
            opens = collection_open(opens, datetime.now(local_zone))
        companies = {}
        sector_snapshot = {"rows": sector_flow_snapshot("US" if overseas else "KR", datetime.now(local_zone))}
        if symbols:
            observed_market = market_evidence(market, previous_session, datetime.now(local_zone))
            evidence.extend(observed_market)
            evidence.extend(research_evidence(market, symbols, datetime.now(local_zone)))
            companies, company_risks = company_context(market, symbols, datetime.now(local_zone))
            evidence.extend(company_risks)
            evidence.extend(confirmed_contract_events(companies, market, datetime.now(local_zone)))
            evidence.extend(scheduled_event_evidence(
                market, datetime.now(local_zone), opens + timedelta(minutes=15)))
            if args.context or evidence:
                Path("signal-context.json").write_text(json.dumps(evidence), encoding="utf-8")
        available_kinds = {row["kind"] for row in evidence if row["market"] in (market, "*")}
        missing_kinds = {"market", "event"} - available_kinds
        if missing_kinds:
            print(f"{market} missing context: {', '.join(sorted(missing_kinds))}; observation alerts only")
        def publish(candidates):
            if market == "US-early":
                snapshot.write_text(json.dumps({"date": today, "candidates": candidates[:5]}), encoding="utf-8")
            elif market == "US-late":
                candidates = filter_us_updates(candidates, today, snapshot)
            if candidates:
                decision_at = opens + timedelta(minutes=15)
                for symbol, *_ in candidates:
                    company = companies.setdefault(symbol, {})
                    company["research_label"] = research_label(evidence, market, symbol, decision_at)
                notify(market, candidates, decision_at, token, chat_id, companies, sectors=sector_snapshot["rows"])
            else:
                print(f"{market} 장중 알림 조건 충족 없음")

        if symbols:
            resistance = prior_resistance(symbols, previous_session, overseas)
            def refresh_companies():
                checked_at = datetime.now(local_zone)
                sector_snapshot["rows"] = sector_flow_snapshot("US" if overseas else "KR", checked_at)
                refreshed, events = company_context(market, symbols, checked_at)
                events.extend(confirmed_contract_events(refreshed, market, checked_at))
                return refreshed, events

            wait_for_connection_window(opens)
            approval = request_approval(app_key, app_secret)
            opens = collection_open(scheduled_open, datetime.now(local_zone))
            if opens != scheduled_open:
                print(f"{market} 대체 수집 시작 {opens.isoformat()} · 알림 {opens + timedelta(minutes=15):%H:%M}")
            asyncio.run(collect(approval, "US" if overseas else market, symbols, opens, publish, Path(f"{market}.jsonl"), evidence, observation_only=bool(missing_kinds), resistance=resistance, refresh_context=refresh_companies, companies=companies, sector_snapshot=sector_snapshot))
        else:
            print(f"{market} 감시 후보 없음")
            publish([])
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, ValueError, TimeoutError, WebSocketException) as exc:
        raise SystemExit(f"{market} 장중 실행 실패 ({type(exc).__name__})") from None




def event_watch_symbols(market, at, registry=None, events=None, limit=6, turnover=None):
    """Translate generic event candidates into supported feed identifiers."""
    from economic_graph import discover,load_profiles
    from event_impact_analysis import world_events
    registry=load_profiles() if registry is None else registry
    if events is None:
        events=world_events(at)[0]
        # Discovery runs after retrieval; do not exclude events first observed during it.
        at=datetime.now(at.tzinfo)
    selected=[]
    if turnover is None:
        turnover={}
        try:
            from candidate_selection import average_turnover
            report=json.loads(Path('opening-scan/latest.json').read_text(encoding='utf-8-sig'))
            for row in report['results']:
                turnover[row['symbol']]=average_turnover(row['indicators'])
        except (OSError,ValueError,KeyError,TypeError):
            pass
    prefixes={'NMS':'DNAS','NGM':'DNAS','NCM':'DNAS','NAS':'DNAS',
              'NYQ':'DNYS','NYS':'DNYS','ASE':'DAMS','AMS':'DAMS'}
    candidates=discover(registry,events,at,limit=None)
    candidates.sort(key=lambda key:(-turnover.get(registry[key].get('source_symbol') or key,0),key))
    for key in candidates:
        company=registry[key]
        symbol=company.get('source_symbol') or key
        if market.startswith('US-'):
            if symbol.endswith(('.KS','.KQ')): continue
            prefix=prefixes.get(company.get('exchange'))
            if prefix:
                US_SYMBOL_NAMES[symbol]=company.get('display_name') or company.get('company_name') or symbol
                selected.append(prefix+symbol)
        elif symbol.endswith(('.KS','.KQ')):
            KR_SYMBOL_NAMES[symbol]=company.get('company_name') or symbol
            selected.append(symbol.split('.')[0])
        if len(selected)>=limit: break
    return selected


if __name__ == "__main__":
    main()
