from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sector_catalyst_watch import attach, catalyst, format_block
from theme_sectors import theme_membership

AT = datetime(2026, 11, 19, 8, 30, tzinfo=ZoneInfo("Asia/Seoul"))
SESSION = "2026-11-18"


def event(**changes):
    record = dict(
        headline="한-중 FTA 서비스·투자 후속협상 실질타결 선언",
        body=("한-중 FTA 서비스·투자 후속협상의 실질타결을 선언했다. "
              "콘텐츠·관광 분야에서 개방 수준을 높이기로 했다. "
              "이날 정상회담에서 양국이 협상을 마무리했다. ") * 7,
        publication_verified=True,
        available_at=(AT - timedelta(hours=8)).isoformat(),
        url="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156789779",
    )
    return {**record, **changes}


def stock(symbol, turnover, session=SESSION):
    return dict(symbol=symbol, market="KR", indicators=dict(
        data_date=session, average_turnover_20d=turnover
    ), selection_filter="unconfirmed_uptrend")


def test_verified_policy_adds_companies_without_setting_a_buy_signal():
    report = {"results": [
        stock("278470.KS", 50), stock("090430.KS", 40), stock("123690.KS", 5),
        stock("192820.KS", 30), stock("241710.KQ", 20),
        stock("257720.KQ", 60, "2026-11-17"),
    ]}
    watch = attach(report, [event()], AT, SESSION)
    assert watch["status"] == "observation_only"
    assert watch["catalyst"] == "한중 교류 확대 기대"
    assert [x["symbol"] for x in watch["items"]] == [
        "278470.KS", "090430.KS", "192820.KS", "241710.KQ",
    ]
    assert all("preopen_signal" not in r for r in report["results"])
    assert format_block(watch, "US") == []
    assert "사전 관찰" in format_block(watch, "KR")[0]
    assert "화장품·K뷰티" in theme_membership("KR")["278470.KS"]


def test_unknown_or_stale_material_does_not_surface():
    for record in (
        event(publication_verified=False),
        event(available_at=(AT + timedelta(minutes=1)).isoformat()),
        event(available_at=(AT - timedelta(days=5)).isoformat()),
        event(url="https://example.com/cosmetics"),
        event(body="정상회담"),
        event(headline="한-중 FTA 후속협상 예정"),
    ):
        assert catalyst(record, AT) is None
    report = {"results": [stock("278470.KS", 50, "2026-11-17")]}
    assert attach(report, [event()], AT, SESSION) == {}


def test_official_cosmetics_export_growth_is_a_distinct_trigger():
    record = event(
        headline="화장품 수출 동향",
        body=("10월 한국 화장품 수출액은 전년 대비 20% 증가했다. 월간 수출액은 통계 작성 이후 "
              "최고 수준으로 집계됐으며 품목별 수출 실적을 함께 발표했다. ") * 7,
    )
    assert catalyst(record, AT) == "공식 화장품 수출 증가 자료"


def test_expanded_universe_keeps_distinct_listed_members_in_each_theme():
    from theme_sectors import THEME_UNIVERSE

    required = {
        "KR": {"화장품·K뷰티", "통신장비·5G·6G", "증권·거래소", "자동차·부품", "ESS·전력저장"},
        "US": {"화장품·뷰티", "필수소비재·식음료", "리츠·부동산", "우주·위성", "데이터센터·냉각·네트워크"},
    }
    for market, themes in THEME_UNIVERSE.items():
        assert required[market] <= {theme for theme, _ in themes}
        assert len(themes) == len(dict(themes))
        for _, members in themes:
            assert len(members) == len({member[0] for member in members})


def test_a_price_batch_failure_does_not_drop_other_themes(monkeypatch):
    import pandas as pd
    import scanner
    from theme_sectors import _download

    symbols = [f"TEST{i:02}" for i in range(76)]
    calls = []

    def fake_download(items, **_options):
        calls.append(items)
        if len(items) > 1:
            raise RuntimeError("provider timeout")
        return pd.DataFrame({(items[0], "Close"): [100.0]})

    monkeypatch.setattr(scanner, "yf_download", fake_download)
    frame = _download(symbols, "KR", None)
    assert [len(batch) for batch in calls] == [75, 1]
    assert frame[(symbols[-1], "Close")].tolist() == [100.0]


def test_added_headline_keywords_match_a_theme_without_acronym_substrings():
    from theme_sectors import headline_labels

    assert "화장품·K뷰티" in headline_labels("화장품 수출 늘어")
    assert "ESS·전력저장" in headline_labels("ESS 공급 계약")
    assert "ESS·전력저장" not in headline_labels("Business update")
