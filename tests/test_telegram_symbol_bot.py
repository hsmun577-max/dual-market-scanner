from datetime import datetime
from subprocess import TimeoutExpired
from unittest.mock import Mock, call

import pytest

from telegram_symbol_analysis import KST, matches, render
from telegram_symbol_bot import Telegram, TelegramError, handle_requests, has_requests


def message(update_id, chat_id, text, chat_type="private"):
    return {"update_id": update_id, "message": {
        "message_id": update_id + 100, "text": text,
        "chat": {"id": chat_id, "type": chat_type},
    }}


def test_exact_ticker_and_stock_name_precede_partial_name_matches():
    listings = {
        "KR": {"005930.KS": "삼성전자", "005935.KS": "삼성전자우"},
        "US": {"BRK-B": "버크셔 해서웨이 B", "NVDA": "엔비디아"},
    }
    assert matches("삼성전자", listings) == [("KR", "005930.KS", "삼성전자")]
    assert matches("005930", listings) == [("KR", "005930.KS", "삼성전자")]
    assert matches("BRK.B", listings) == [("US", "BRK-B", "버크셔 해서웨이 B")]
    assert len(matches("삼성", listings)) == 2


def test_only_configured_chat_receives_one_analysis_result_before_acknowledgement():
    bot = Telegram("test", "10")
    bot.call = Mock(side_effect=[
        [message(4, 20, "다른 대화"), message(5, 10, "NVDA")], [], {}, [],
    ])
    analyse = Mock(return_value="관찰")
    assert handle_requests(bot, analyse) == 1
    analyse.assert_called_once_with("NVDA")
    assert bot.call.call_args_list == [
        call("getUpdates", timeout=0, limit=25, allowed_updates=["message"]),
        call("getUpdates", timeout=0, limit=1, allowed_updates=["message"], offset=5),
        call("sendMessage", chat_id=10, text="관찰",
             reply_parameters={"message_id": 105, "allow_sending_without_reply": True}),
        call("getUpdates", timeout=0, limit=1, allowed_updates=["message"], offset=6),
    ]


def test_failed_delivery_leaves_the_request_pending():
    bot = Telegram("test", "10")
    bot.call = Mock(side_effect=[[message(4, 10, "삼성전자")], TelegramError("unavailable")])
    with pytest.raises(TelegramError):
        handle_requests(bot, Mock(return_value="관찰"))
    assert [record.args[0] for record in bot.call.call_args_list] == ["getUpdates", "sendMessage"]


def test_analysis_timeout_reports_status_before_acknowledgement():
    bot = Telegram("test", "10")
    bot.call = Mock(side_effect=[[message(4, 10, "삼성전자")], {}, []])
    analyse = Mock(side_effect=TimeoutExpired("analysis", 240))
    assert handle_requests(bot, analyse) == 1
    fallback, ack = bot.call.call_args_list[1:]
    assert "4분" in fallback.kwargs["text"]
    assert ack.kwargs["offset"] == 5


def test_check_does_not_acknowledge_an_authorized_pending_message():
    bot = Telegram("test", "10")
    bot.call = Mock(return_value=[message(4, 20, "NVDA"), message(5, 10, "삼성전자")])
    assert has_requests(bot)
    bot.call.assert_called_once_with("getUpdates", timeout=0, limit=25, allowed_updates=["message"])


def test_closed_market_plan_displays_yellow_and_verified_disclosure_displays_red():
    closed = datetime(2026, 11, 28, 14, tzinfo=KST)
    row = dict(market="KR",name="삼성전자",symbol="005930.KS",
               indicators={"price":179100, "data_date":"2026-11-27"},
               preopen_signal={"strategy":"trend", "expires_session":"2026-11-30"})
    text = render(row, closed)
    assert text.startswith("🟡")
    assert "정규장 종료·휴장" in text
    assert "2026-11-27" in text
    row["nonchart_risk"] = {"title":"유상증자 결정"}
    assert render(row, closed).startswith("🔴")
