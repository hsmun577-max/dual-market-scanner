"""Track published catalysts that warrant watching Korean cosmetics stocks."""

import math
import re
from datetime import timedelta
from urllib.parse import urlsplit

from economic_graph import stamp
from event_publication import MIN_VERIFIED_BODY_CHARS
from theme_sectors import BEAUTY_BRANDS, BEAUTY_SUPPLY_CHAIN

OFFICIAL_HOSTS = {"www.korea.kr", "m.korea.kr", "korea.kr", "www.motir.go.kr", "motir.go.kr"}
MAX_EVENT_AGE = timedelta(days=4)
COUNTRY_PAIR = re.compile(r"(?<![가-힣])한\s*[·ㆍ-]?\s*중(?![가-힣])|한국.{0,20}중국")
BEAUTY = re.compile(r"화장품|K\s*-?\s*뷰티", re.I)
EXPORT_GROWTH = re.compile(r"수출.{0,45}(?:\d+(?:\.\d+)?\s*%\s*(?:증가|성장)|역대\s*최대|최고치)")
UNCONFIRMED_EXPORT = re.compile(r"전망|추정|목표|기대")


def catalyst(event, at):
    published = stamp(event.get("available_at"))
    try:
        url = urlsplit(event.get("url") or "")
    except (TypeError, ValueError):
        return None
    if (
        event.get("publication_verified") is not True
        or published is None or not at - MAX_EVENT_AGE <= published <= at
        or url.scheme != "https" or url.hostname not in OFFICIAL_HOSTS
    ):
        return None
    body = " ".join(str(event.get("body") or "").split())
    if len(body) < MIN_VERIFIED_BODY_CHARS:
        return None
    headline = " ".join(str(event.get("headline") or "").split())
    if (
        COUNTRY_PAIR.search(headline)
        and re.search(r"FTA|자유무역협정", headline, re.I)
        and re.search(r"(?:실질\s*)?타결", headline)
        and COUNTRY_PAIR.search(body)
        and re.search(r"서비스.{0,10}투자.{0,35}타결", body)
        and re.search(r"콘텐츠|인적\s*교류|문화\s*교류|관광", body)
    ):
        return "한중 교류 확대 기대"
    if (BEAUTY.search(headline) or re.search(r"K\s*-?\s*소비재", headline, re.I)) and any(
        BEAUTY.search(sentence) and EXPORT_GROWTH.search(sentence) and not UNCONFIRMED_EXPORT.search(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", body)
    ):
        return "공식 화장품 수출 증가 자료"
    return None


def attach(report, events, at, reference_session):
    """Build a watch from completed prices and a published official catalyst."""
    watch = {}
    report["kr_sector_catalyst_watch"] = watch
    relevant = [(stamp(event["available_at"]), event["url"], label)
                for event in events if (label := catalyst(event, at))]
    if not relevant:
        return watch
    known, source, label = max(relevant)
    rows = {}
    for row in report.get("results", ()):
        values = row.get("indicators") or {}
        turnover = values.get("average_turnover_20d")
        if (
            row.get("market") == "KR" and values.get("data_date") == reference_session
            and isinstance(turnover, (int, float)) and math.isfinite(turnover) and turnover > 0
        ):
            rows[row["symbol"]] = turnover
    items = []
    for members in (BEAUTY_BRANDS, BEAUTY_SUPPLY_CHAIN):
        group = sorted((rows[symbol], symbol, name) for symbol, name in members if symbol in rows)
        for _, symbol, name in reversed(group[-2:]):
            items.append(dict(symbol=symbol, name=name))
    if items:
        watch.update(theme="화장품·K뷰티", status="observation_only", catalyst=label,
                     available_at=known.isoformat(), source_url=source,
                     reference_session=reference_session, items=items)
    return watch


def format_block(watch, market):
    if not watch or market not in (None, "KR"):
        return []
    names = " · ".join(item["name"] for item in watch["items"])
    return [f"[{watch['theme']} 사전 관찰]", f"{watch['catalyst']} · {names}"]
