from datetime import datetime
from unittest.mock import Mock

from scheduled_event_watch import attach, format_block

REFERENCE = "2026-11-23"
AT = datetime.fromisoformat("2026-11-23T16:00:00+09:00")


def scan():
    return {"results": [
        dict(symbol="277810.KQ", name="레인보우로보틱스", market="KR",
             selection_filter="unconfirmed_uptrend",
             indicators=dict(data_date=REFERENCE, relative_strength=.296, volume_ratio=2.07,
                             ma20=361725, ma60=400783.33)),
        dict(symbol="108490.KQ", name="로보티즈", market="KR",
             indicators=dict(data_date=REFERENCE, average_turnover_20d=9_000_000_000)),
    ]}


def test_event_discovers_a_chart_rejection_when_theme_has_same_session_buying():
    report = scan()
    flows = {
        "277810.KQ": dict(date=REFERENCE, institution_qty=31369, foreign_qty=-57438),
        "108490.KQ": dict(date=REFERENCE, institution_qty=89872),
    }
    watch, = attach(report, AT, REFERENCE, read_flows=lambda symbol, session: flows[symbol])
    assert watch["symbol"] == "277810.KQ"
    assert watch["status"] == "observation_only"
    assert watch["buying_peers"] == ["로보티즈"]
    assert "preopen_signal" not in report["results"][0]
    assert "외인 -57,438주" in "\n".join(format_block([watch], "KR"))
    assert format_block([watch], "US") == []


def test_old_peer_flow_cannot_confirm_current_buying():
    def flow(symbol, session):
        return dict(date=session if symbol == "277810.KQ" else "2026-11-20",
                    institution_qty=1000)
    assert attach(scan(), AT, REFERENCE, read_flows=flow) == []


def test_lookahead_and_expired_event_do_not_query_flows():
    reader = Mock()
    for when, day in [("2026-11-22T06:19:59+09:00", "2026-11-20"),
                      ("2027-01-06T18:00:00+09:00", "2027-01-05")]:
        report = scan()
        for row in report["results"]:
            row["indicators"]["data_date"] = day
        assert attach(report, datetime.fromisoformat(when), day, read_flows=reader) == []
    reader.assert_not_called()
