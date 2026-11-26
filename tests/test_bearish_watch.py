import pandas as pd

import bearish_watch
from backtest_bearish_watch import sim
from scanner import calculate_indicators

SESSION = "2026-11-25"


def stock(symbol, **overrides):
    values = dict(
        data_date=SESSION,
        market_ma20_date=SESSION,
        market_lag_sessions=0,
        market_above_ma20=False,
        price=28,
        volume=2_000_000,
        volume_ratio=1.1,
        average_turnover_20d=20_000_000,
        ma20=27,
        ma60=25,
        return_1d=0.0,
        return_5d=0.02,
        return_20d=0.05,
        market_return_5d=-0.01,
        relative_strength=0.06,
        obv_rising=True,
        compression=True,
    )
    return dict(symbol=symbol, name=symbol, market="US", indicators={**values, **overrides})


def test_only_one_fresh_observation_per_market_and_no_buy_plan():
    original = {"status": "conditional_buy"}
    low = stock("AAA")
    high = stock("BBB", return_5d=0.03)
    high["preopen_signal"] = original
    report = {"results": [low, high, stock("OLD", data_date="2026-11-24")]}
    observed = bearish_watch.attach(report, SESSION)
    assert observed["symbol"] == "BBB"
    assert observed["status"] == "observation_only"
    assert high["preopen_signal"] is original
    assert "preopen_signal" not in low
    assert bearish_watch.format_block(observed, "KR") == []
    assert "지수 대비 +4.0%p" in bearish_watch.format_block(observed, "US")[-1]
    assert bearish_watch.attach(report, "2026-11-26") == {}
    assert bearish_watch.attach(report, None) == {}


def test_weak_or_unobserved_prices_do_not_pass():
    for indicators in (
        {"market_above_ma20": None},
        {"market_lag_sessions": 1},
        {"market_return_5d": None},
        {"return_1d": -0.01},
        {"return_5d": -0.001},
        {"return_5d": 0.07, "market_return_5d": 0.065},
        {"breakdown": True},
    ):
        assert bearish_watch.resilience_setup(stock("ABC", **indicators), "US") is None
    fund = stock("SQQQ")
    assert bearish_watch.resilience_setup(fund, "US") is None


def test_benchmark_return_uses_the_same_six_sessions():
    dates = pd.bdate_range(end=SESSION, periods=65)
    close = pd.Series(range(100, 165), index=dates)
    history = pd.DataFrame(dict(Open=close, Close=close, High=close + 1, Low=close - 1, Volume=10))
    benchmark = history[["Close"]].copy()
    assert calculate_indicators(history, benchmark)["market_return_5d"] == round(164 / 159 - 1, 4)
    benchmark = benchmark.drop(dates[-2])
    assert calculate_indicators(history, benchmark)["market_return_5d"] is None


def test_replay_applies_the_stop_first_and_does_not_need_bars_after_exit():
    bars = pd.DataFrame([dict(open=100, high=107, low=96, close=105)], index=["2026-11-25"])
    status, path = sim(bars, ["2026-11-24", "2026-11-25", "2026-11-26"], 100, 0.005)
    assert status == "stop"
    assert round(path["2026-11-25"] * 100, 2) == -3.48
