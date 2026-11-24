"""Recent earnings and disclosure evidence for the mid-term pullback list.

The +30% pullback list (`mid_term_watch`) is chosen on the session alone: 20-day return,
RSI, turnover band and relative strength. The 2026-11-24 scan showed what that misses —
``상지건설(042940)`` sat at the top of the list while it was carrying a third-quarter
operating loss (−16억) and an 82억 rights offering whose new shares listed 11/20. The owner
asked for exactly two things on top of the chart condition:

    "중기 +30% 눌림 발굴에서 적자·유증 종목은 제외하고,
     종목마다 최근 실적·재료 1줄 요약이 같이 뜨도록 스캐너를 업그레이드해 줘."

Two rules, then, and one line of context per name:

* **적자** — the most recent *reported* period (the latest quarter, or the annual report when
  that is the newest filing) shows operating income < 0 or net income < 0.
* **유상증자** — a rights offering (주주배정·일반공모·제3자배정·신주모집/상장) appears in the
  last :data:`LOOKBACK_DAYS` of disclosures or issuer news. A bonus issue (무상증자) is not
  a rights offering and must not trip the rule.

Where the numbers come from, and why not from DART alone:

* Naver's own financial summary (``m.stock.naver.com``) carries 매출액·영업이익·당기순이익 per
  quarter and per year with an ``isConsensus`` flag, needs no key, and answers in one request.
  Only columns flagged ``isConsensus == "N"`` are read: an estimate for a quarter that has
  not been filed yet is not a reported loss (상지건설's 2026.09 column is exactly that).
* DART is the disclosure authority and the repo already talks to it, but the key lives in
  Actions Secrets, so it is used *when available* for the rights-offering check and for a
  clean filing headline. Without a key the same check runs on issuer news instead, so the
  filter degrades rather than disappears.
* Missing evidence never excludes a name. A provider that times out leaves the row in the
  list, counted as ``unverified`` — the alternative is a silent, provider-dependent list.

Cost is why this runs on the pullback pool and not the universe: the scan already narrows
~13,000 rows to a few dozen with price/volume/RSI/relative strength, so only those rows pay
for the two or three requests each (:data:`EVIDENCE_CAP` bounds it).
"""

import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from zoneinfo import ZoneInfo

from event_impact_analysis import HEADERS, issuer_name_in_text, issuer_title_subject

KST = ZoneInfo("Asia/Seoul")
# How far back a rights offering still counts. 상지건설's offering was decided in early
# November and its new shares listed 11/20; a 90-day window covers the decision, the
# subscription and the listing without reaching into last year's capital raises.
LOOKBACK_DAYS = 90
# The material line quotes a filing or article from this window. A headline from four months
# ago is not "최근 재료".
MATERIAL_DAYS = 45
# The pullback pool is 30~45 rows in a normal scan (37 on 2026-11-24). The cap keeps a
# pathological day from turning the screen into a crawl.
EVIDENCE_CAP = 45
DEFAULT_WORKERS = 6
MAX_HEADLINE_CHARS = 52

FINANCE_API = "https://m.stock.naver.com/api/stock/{code}/finance/{period}"
NEWS_API = "https://m.stock.naver.com/api/news/stock/{code}"
DART_LIST_API = "https://opendart.fss.or.kr/api/list.json"

# A rights offering, in disclosure titles and in news alike. The bonus-issue guard is
# applied first, because "무상증자" would otherwise look like a capital raise to any loose
# pattern and the owner asked for that false positive to stay out.
_RIGHTS_CORE = re.compile(r"유상증자|제3자배정|주주배정|실권주|일반공모|공모증자|신주모집|신주발행|신주상장|증자결정")
_BONUS_ISSUE = re.compile(r"무상증자")
# Routine filings that name a capital event only as boilerplate, or that say nothing about
# the company's business. They are not the "재료" line.
_NOISE_FILING = re.compile(
    r"임원\s*[ㆍ·]\s*주요주주|주식등의대량보유|의결권대리행사|투자설명서|증권발행실적보고서|"
    r"일괄신고서|정정신고|감사보고서|연결감사보고서|사업보고서|반기보고서|분기보고서|"
    r"기업설명회|IR\s*개최|주주총회소집|주주명부|배당금|현금ㆍ현물배당|"
    r"특정증권등소유상황보고서|소유상황보고서")
# Headline furniture that carries no catalyst: photo tags, market round-ups, quote sheets.
_NOISE_NEWS = re.compile(
    r"^\s*\[?(?:포토|영상|속보|사설|칼럼|인터뷰|재테크|특징주\s*\]?)|오늘의\s*메모|"
    r"주요\s*공시|공시\s*모음|마감\s*시황|장\s*마감|개장\s*시황|코스피\s*[·ㆍ]?\s*코스닥",
    re.I)
# A headline that names a business event is worth more than one that merely mentions the
# company, so the material line prefers these.
_MATERIAL_WORDS = re.compile(
    r"수주|공급\s*계약|단일판매|잠정\s*실적|영업(?:이익|손실)|흑자|적자|유상증자|무상증자|합병|분할|"
    r"최대주주|인수|양수|신규\s*시설|증설|투자|허가|승인|임상|기술\s*수출|특허|자사주|수혜|급증|계약")

_DART_CODES = {}
_DART_LOCK = threading.Lock()


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _amount(columns, title):
    """One financial cell, in 억원, or ``None`` when the provider has no figure."""
    column = (columns or {}).get(title)
    if not isinstance(column, dict):
        return None
    raw = str(column.get("value") or "").replace(",", "").strip()
    if raw in {"", "-", "N/A"}:
        return None
    value = _number(raw)
    if value is None:
        return None
    if column.get("cx") == "minus":
        value = -abs(value)
    return value


def _period_label(key, basis):
    year, month = key[:4], key[4:6]
    if basis == "annual":
        return f"{year}년"
    quarter = {"03": 1, "06": 2, "09": 3, "12": 4}.get(month)
    return f"{quarter}Q{year[2:]}" if quarter else f"{year}.{month}"


def parse_finance(document, basis="quarter"):
    """The newest *reported* period of a Naver finance summary.

    ``trTitleList`` marks each column ``isConsensus``: ``"Y"`` is the market's estimate for a
    period that has not been filed, so it is skipped here. Reading it would have reported
    상지건설's unreported 2026.09 quarter as a blank and, on another day, an estimated profit
    as a reported one.
    """
    info = (document or {}).get("financeInfo") or {}
    reported = [row for row in info.get("trTitleList") or []
                if str(row.get("isConsensus", "")).upper() == "N" and row.get("key")]
    rows = {str(row.get("title") or ""): row.get("columns") or {}
            for row in info.get("rowList") or []}
    for column in sorted(reported, key=lambda row: str(row["key"]), reverse=True):
        key = str(column["key"])
        if not re.fullmatch(r"\d{6}", key):
            continue
        operating = _amount(rows.get("영업이익"), key)
        net = _amount(rows.get("당기순이익"), key)
        if operating is None and net is None:
            continue
        prior_key = str(int(key[:4]) - 1) + key[4:]
        return {
            "period": f"{key[:4]}-{key[4:6]}",
            "label": _period_label(key, basis),
            "basis": basis,
            "revenue": _amount(rows.get("매출액"), key),
            "operating_income": operating,
            "net_income": net,
            "prior_operating_income": _amount(rows.get("영업이익"), prior_key),
            "prior_net_income": _amount(rows.get("당기순이익"), prior_key),
            "source": "네이버 금융 분기 재무" if basis == "quarter" else "네이버 금융 연간 재무",
        }
    return None


def _get(url, params=None, timeout=6):
    import requests
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout,
                            allow_redirects=False)
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    return response.json()


def financials(code):
    """``(record, status)`` — the latest reported quarter, else the latest annual report.

    The quarter is asked for first because it is what "최근 실적" means to the owner; the
    annual report is the fallback for an issuer whose quarters the provider does not carry.
    """
    for basis in ("quarter", "annual"):
        try:
            document = _get(FINANCE_API.format(code=code, period=basis))
        except Exception:  # noqa: BLE001 - a provider failure is data, not a crash
            continue
        record = parse_finance(document, basis)
        if record:
            return record, "checked"
    return None, "unavailable"


def _news_items(document):
    if isinstance(document, list):
        for entry in document:
            if isinstance(entry, dict) and isinstance(entry.get("items"), list):
                for item in entry["items"]:
                    if isinstance(item, dict):
                        yield item
            elif isinstance(entry, dict) and entry.get("titleFull"):
                yield entry


def _published(item):
    raw = str(item.get("datetime") or "").strip()
    try:
        if re.fullmatch(r"\d{12}", raw):
            return datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=KST) + timedelta(minutes=1)
        if re.fullmatch(r"\d{14}", raw):
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None
    return None


def mentions(text, name, code=None):
    """Whether the text names the issuer.

    ``issuer_name_in_text`` refuses aliases shorter than three characters, which would mute
    two-character names (삼기) that the list does carry. Those get the same boundary rule
    here, and the six-digit code in a round-up column ("실권주청약=상지건설(042940)") counts
    as a name too.
    """
    text = str(text or "")
    aliases = {str(name or "").strip(), re.sub(r"\s+Ent\.$", "", str(name or "").strip())}
    for alias in filter(None, aliases):
        if issuer_name_in_text(text, alias):
            return True
        if len(alias) == 2 and re.fullmatch(r"[가-힣]{2}", alias):
            if re.search(r"(?<![가-힣a-zA-Z0-9])" + re.escape(alias) + r"(?![가-힣a-zA-Z0-9])", text):
                return True
    if code and re.search(r"(?<!\d)" + re.escape(str(code)) + r"(?!\d)", text):
        return True
    return False


def parse_news(document, name, at, days=LOOKBACK_DAYS, code=None):
    """Issuer news inside the window, newest first.

    The ticker feed carries market round-ups and other companies' stories, so a row counts
    only when the issuer is named in the headline that was published (the body is read too,
    because "오늘의 메모" style columns list the issuer by name inside the text).
    """
    rows = []
    for item in _news_items(document):
        title = _clean(item.get("titleFull") or item.get("title"))
        if not title:
            continue
        published = _published(item)
        if published is None or not at - timedelta(days=days) <= published <= at + timedelta(minutes=5):
            continue
        body = _clean(item.get("body"))
        text = issuer_title_subject(title) + " " + body
        if not mentions(text, name, code):
            continue
        rows.append({"headline": title, "body": body, "published_at": published.isoformat(),
                     "source": item.get("officeName") or "네이버 종목 뉴스",
                     "url": item.get("mobileNewsUrl") or (
                         f"https://n.news.naver.com/article/{item.get('officeId')}/{item.get('articleId')}"
                         if item.get("officeId") and item.get("articleId") else None)})
    rows.sort(key=lambda row: row["published_at"], reverse=True)
    return rows


def news(code, name, at, days=LOOKBACK_DAYS):
    try:
        document = _get(NEWS_API.format(code=code), params={"page": 1, "pageSize": 30})
    except Exception:  # noqa: BLE001
        return [], "unavailable"
    return parse_news(document, name, at, days, code=code), "checked"


def dart_codes(api_key):
    """The stock-code → DART corp-code map, downloaded once per process.

    The screen reads several issuers at once, so the download is taken under a lock: without
    it the first six threads would each stream the same several-megabyte file.
    """
    with _DART_LOCK:
        if "map" not in _DART_CODES:
            from kis_intraday import dart_company_codes
            _DART_CODES["map"] = dart_company_codes(api_key)
        return _DART_CODES["map"]


def parse_dart_filings(document, at, days=LOOKBACK_DAYS):
    rows = []
    for row in (document or {}).get("list") or []:
        day = str(row.get("rcept_dt") or "")
        receipt = str(row.get("rcept_no") or "")
        title = _clean(row.get("report_nm"))
        if not re.fullmatch(r"\d{8}", day) or not re.fullmatch(r"\d{14}", receipt) or not title:
            continue
        try:
            filed = datetime.strptime(day, "%Y%m%d").replace(tzinfo=KST)
        except ValueError:
            continue
        if not at - timedelta(days=days) <= filed <= at:
            continue
        rows.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "title": title,
                     "source": "DART", "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}"})
    rows.sort(key=lambda row: row["date"], reverse=True)
    return rows


def filings(code, at, api_key=None, days=LOOKBACK_DAYS):
    """Recent DART disclosures, or an explicit ``missing_key``/``unavailable`` status."""
    import os
    key = api_key if api_key is not None else os.getenv("DART_API_KEY", "").strip()
    if not key:
        return [], "missing_key"
    try:
        corp_code = dart_codes(key).get(code)
    except Exception:  # noqa: BLE001
        return [], "unavailable"
    if not corp_code:
        return [], "unmapped_issuer"
    local = at.astimezone(KST)
    try:
        document = _get(DART_LIST_API, params={
            "crtfc_key": key, "corp_code": corp_code,
            "bgn_de": (local - timedelta(days=days)).strftime("%Y%m%d"),
            "end_de": local.strftime("%Y%m%d"), "page_count": "100"}, timeout=8)
    except Exception:  # noqa: BLE001
        return [], "unavailable"
    if str(document.get("status")) not in {"000", "013"}:
        return [], "unavailable"
    return parse_dart_filings(document, at, days), "checked"


def _clean(text):
    """Provider text without the markup a headline page would have rendered."""
    return " ".join(unescape(str(text or "")).split())


def _sentences(text):
    """The body split where a round-up column separates its items."""
    return [part for part in re.split(r"[\n\r]|(?<=[.!?])\s+|▲|ㆍ(?=[가-힣]{2,}[=:])", text) if part.strip()]


def rights_offering(record):
    """The newest rights-offering filing or article in the record, or ``None``.

    A filing title is read whole, because the whole title is the announcement. An article
    body is read one sentence at a time: a ticker feed carries ``오늘의 메모``-style columns
    where the issuer's line ("▲실권주청약=상지건설(042940)") sits beside other companies',
    and reading the whole body made 다우기술 look like it was issuing shares because a
    *different* company in the same article was.
    """
    name, code = record.get("name"), str(record.get("symbol") or "").split(".")[0]
    for row in record.get("filings") or []:
        title = _clean(row.get("title"))
        if _BONUS_ISSUE.search(title) and not re.search(r"유상증자", title):
            continue
        if _RIGHTS_CORE.search(title):
            return {**row, "kind": "filing"}
    for row in record.get("news") or []:
        headline = _clean(row.get("headline"))
        if _BONUS_ISSUE.search(headline) and not re.search(r"유상증자", headline):
            continue
        if _RIGHTS_CORE.search(headline):
            return {**row, "kind": "news"}
        for sentence in _sentences(_clean(row.get("body"))):
            if not mentions(sentence, name, code):
                continue
            if _BONUS_ISSUE.search(sentence) and not re.search(r"유상증자", sentence):
                continue
            if _RIGHTS_CORE.search(sentence):
                return {**row, "kind": "news"}
    return None


def _read(reader, *args):
    """One provider read that reports its failure instead of raising it."""
    try:
        return reader(*args)
    except Exception:  # noqa: BLE001 - a provider failure is data, not a crash
        return None, None


def collect(symbol, name, at=None, api_key=None):
    """Everything the two rules and the summary need about one issuer.

    Never raises: a provider that fails leaves its own status behind and the caller counts
    the row as unverified instead of losing the scan.
    """
    at = at or datetime.now(timezone.utc)
    code = str(symbol or "").split(".")[0].strip()
    record = {"symbol": symbol, "name": name, "checked_at": at.isoformat()}
    record["financial"], record["financial_status"] = _read(financials, code)
    record["news"], record["news_status"] = _read(news, code, name, at)
    record["filings"], record["filing_status"] = _read(filings, code, at, api_key)
    record["financial"] = record["financial"] or None
    record["news"] = record["news"] or []
    record["filings"] = record["filings"] or []
    record["financial_status"] = record["financial_status"] or "unavailable"
    record["news_status"] = record["news_status"] or "unavailable"
    record["filing_status"] = record["filing_status"] or "unavailable"
    record["rights_offering"] = rights_offering(record)
    checked = [record["financial_status"], record["news_status"], record["filing_status"]]
    record["status"] = "checked" if "checked" in checked else "unavailable"
    record["verified"] = (record["financial_status"] == "checked"
                          and "checked" in (record["news_status"], record["filing_status"]))
    return record


def rejection(record):
    """Why the mid-term list must drop the name, or ``None`` when it stays."""
    if not isinstance(record, dict):
        return None
    financial = record.get("financial") or {}
    operating = _number(financial.get("operating_income"))
    net = _number(financial.get("net_income"))
    if operating is not None and operating < 0:
        return "operating_loss"
    if net is not None and net < 0:
        return "net_loss"
    rights = record.get("rights_offering")
    if rights is None:
        # A record built by hand (a replay, a test) may not have run the check yet.
        rights = rights_offering(record)
    if rights:
        return "rights_offering"
    return None


def _won(value):
    if value is None:
        return None
    value = _number(value)
    if value is None:
        return None
    if abs(value) >= 10_000:
        return f"{value / 10_000:+.1f}조"
    return f"{value:+,.0f}억"


def _change(current, prior):
    if current is None or prior is None:
        return None
    if prior > 0 and current > 0:
        return f"{(100 * (current / prior - 1)):+.0f}%"
    if prior <= 0 < current:
        return "흑자전환"
    if prior > 0 >= current:
        return "적자전환"
    if prior < 0 and current < 0:
        return "적자 확대" if current < prior else "적자 축소"
    return None


def earnings_line(financial):
    """``3Q26 영업이익 299억 (+22%)`` — the reported figure, not an estimate."""
    if not financial:
        return None
    label = financial.get("label") or financial.get("period") or ""
    operating = _number(financial.get("operating_income"))
    if operating is not None:
        change = _change(operating, _number(financial.get("prior_operating_income")))
        text = f"{label} 영업이익 {_won(operating)}".strip()
        return f"{text} ({change})" if change else text
    net = _number(financial.get("net_income"))
    if net is not None:
        change = _change(net, _number(financial.get("prior_net_income")))
        text = f"{label} 순이익 {_won(net)}".strip()
        return f"{text} ({change})" if change else text
    return None


def _clip(text, limit=MAX_HEADLINE_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _month_day(value):
    day = str(value or "")[:10]
    return f"{int(day[5:7])}/{int(day[8:10])}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""


def restates(financial, text):
    """Whether a headline only repeats the figure the earnings part already carries.

    동국제강's line read "3Q26 영업이익 +299억 (+22%) · 10/30 동국제강 3Q 영업익 299억,
    전년比 22.0%↑" — the same fact twice. A headline that reports a *different* quarter
    (삼기's 3분기 잠정 실적 next to a confirmed 2분기 statement) does not repeat it and is
    kept, because it is newer information than the statement period.
    """
    operating = _number((financial or {}).get("operating_income"))
    if operating is None or not re.search(r"영업(?:이익|익)", str(text or "")):
        return False
    for figure in re.findall(r"\d[\d,]*(?:\.\d+)?", str(text or "")):
        value = _number(figure.replace(",", ""))
        if value is not None and abs(abs(value) - abs(operating)) <= 0.5:
            return True
    prior = _number((financial or {}).get("prior_operating_income"))
    if operating > 0 and prior and prior > 0:
        change = 100 * (operating / prior - 1)
        for figure in re.findall(r"(\d+(?:\.\d+)?)\s*%", str(text or "")):
            value = _number(figure)
            if value is not None and abs(value - abs(change)) <= 1.0:
                return True
    return False


def material(record, at=None, days=MATERIAL_DAYS, financial=None):
    """The one line of recent material: a filing or an article, whichever says more.

    A disclosure outranks an article of the same standing (same materiality score and day),
    because the filing is the company's own statement of the event and the article is a
    reading of it.
    """
    at = at or datetime.now(timezone.utc)
    floor = (at.astimezone(KST) - timedelta(days=days)).date().isoformat()
    options = []
    for row in (record or {}).get("filings") or []:
        title = _clean(row.get("title"))
        day = str(row.get("date") or "")[:10]
        if not title or day < floor or _NOISE_FILING.search(title):
            continue
        if financial is not None and restates(financial, title):
            continue
        options.append((1 if _MATERIAL_WORDS.search(title) else 0, 1, day, title, _month_day(day)))
    for row in (record or {}).get("news") or []:
        headline = _clean(row.get("headline"))
        day = str(row.get("published_at") or "")[:10]
        if not headline or day < floor or _NOISE_NEWS.search(headline):
            continue
        if financial is not None and restates(financial, headline):
            continue
        options.append((1 if _MATERIAL_WORDS.search(headline) else 0, 0, day, headline, _month_day(day)))
    if not options:
        return None
    _, _, _, text, stamp = max(options, key=lambda option: option[:3])
    line = _clip(text)
    return f"{stamp} {line}".strip() if stamp else line


def summary(record, at=None):
    """The single line shown under a mid-term name: earnings, then recent material."""
    financial = (record or {}).get("financial")
    parts = [earnings_line(financial), material(record, at, financial=financial)]
    return " · ".join(part for part in parts if part) or None


def screen(rows, at=None, collect_evidence=None, cap=EVIDENCE_CAP, workers=DEFAULT_WORKERS):
    """Read evidence for the strongest ``cap`` rows, keep the ones that pass both rules.

    The rows stay in their incoming order (strongest relative strength first), so the list a
    caller builds from ``kept`` is still the ranked one, only without the excluded names.
    """
    at = at or datetime.now(timezone.utc)
    collector = collect_evidence or collect
    candidates = list(rows or [])[:cap]
    results = [None] * len(candidates)

    def read(index, item):
        try:
            return collector(item.get("symbol"), item.get("name"), at)
        except Exception:  # noqa: BLE001 - a broken provider must not stop the screen
            return {"symbol": item.get("symbol"), "name": item.get("name"),
                    "status": "unavailable", "verified": False}

    if workers and len(candidates) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
            for index, record in enumerate(pool.map(
                    lambda pair: read(*pair), list(enumerate(candidates)))):
                results[index] = record
    else:
        for index, item in enumerate(candidates):
            results[index] = read(index, item)

    kept, excluded, unverified = [], {}, 0
    for item, record in zip(candidates, results):
        reason = rejection(record)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        if not (record or {}).get("verified"):
            unverified += 1
        kept.append({"item": item, "evidence": record})
    return {"kept": kept, "excluded": excluded, "screened": len(candidates),
            "unverified": unverified}
