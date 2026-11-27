from theme_priority import format_block, prioritize

DAY = "2026-11-23"


def stock(symbol, change, benchmark=-.015):
    return dict(symbol=symbol, market="KR", name=symbol, indicators=dict(
        data_date=DAY, price=10000, volume=10_000_000, volume_ratio=2,
        ma20=9500, ma60=9000, return_5d=.03, return_20d=.08,
        relative_strength=.20, return_1d=change, market_return_1d=benchmark,
        average_turnover_20d=50_000_000_000))


def snapshot(members, **changes):
    theme = dict(name="로봇", data_date=DAY, symbols=members, total=len(members),
                 benchmark_return_pct=-1.5, market_excess_pct=2.5, inflow_multiple=1.4)
    theme.update(changes)
    return dict(theme_sectors=[dict(market="KR", data_date=DAY, benchmark_aligned=True,
                                    rows=[theme])])


def test_validated_theme_and_joint_buying_prioritize_research_only():
    stronger = [stock(f"10000{i}.KQ", change) for i, change in enumerate([-.01, .02, .03])]
    weak = stock("200000.KQ", -.035)
    pool = [weak, *stronger]
    original = [weak, stronger[2]]
    report = snapshot([row["symbol"] for row in pool])

    def flows(symbol, session):
        return dict(date=session, institution_qty=1 if symbol in {"100000.KQ", "100001.KQ"} else -1)

    result = prioritize(report, pool, original, "KR", DAY, read_flows=flows)
    assert result == [*stronger, weak]
    info = report["theme_research_priority"]["KR"]
    assert [row["priority"] for row in info] == [2, 2, 1]
    assert "기관 동반 순매수" in "\n".join(format_block(report["theme_research_priority"], "KR"))
    assert not any(row.get("preopen_signal") or row.get("company_forecast") for row in result)


def test_weak_or_stale_theme_cannot_change_research_order():
    rows = [stock(f"10000{i}.KQ", change) for i, change in enumerate([.02, -.04, -.04])]
    report = snapshot([row["symbol"] for row in rows])
    assert prioritize(report, rows, rows, "KR", DAY) == rows
    assert report["theme_research_priority"]["KR"] == []
    report["theme_sectors"][0]["data_date"] = "2026-11-20"
    assert prioritize(report, rows, rows, "KR", DAY) == rows


def test_stale_institutional_flow_does_not_raise_priority():
    rows = [stock(f"10000{i}.KQ", .02) for i in range(3)]
    report = snapshot([row["symbol"] for row in rows])
    prioritize(report, rows, rows, "KR", DAY,
               read_flows=lambda symbol, session: dict(date="2026-11-20", institution_qty=50))
    assert [row["priority"] for row in report["theme_research_priority"]["KR"]] == [1, 1, 1]
