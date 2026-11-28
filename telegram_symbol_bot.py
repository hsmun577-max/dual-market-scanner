"""Receive and reply to stock queries in the scanner's configured Telegram chat."""
import argparse
import json
import os
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from telegram_text import split_telegram_text

HELP = "종목명이나 티커 하나를 보내주세요. 예) 삼성전자, 005930, NVDA"
RECEIPT = "🔎 종목 분석 요청을 받았습니다. 조회가 끝나면 이 대화에 결과를 답장하겠습니다."
ANALYSIS_TIMEOUT = 240


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token, chat_id):
        if not token or not str(chat_id).lstrip("-").isdigit():
            raise TelegramError("Telegram bot or chat configuration is missing")
        self.url = "https://api.telegram.org/bot" + token
        self.chat_id = int(chat_id)

    def call(self, method, **data):
        request = Request(f"{self.url}/{method}", data=json.dumps(data).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise TelegramError(f"Telegram {method} HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise TelegramError(f"Telegram {method} response unavailable") from None
        if payload.get("ok") is not True:
            raise TelegramError(f"Telegram {method} rejected")
        return payload["result"]

    def updates(self, offset=None, limit=25):
        parameters = dict(timeout=0, limit=limit, allowed_updates=["message"])
        if offset is not None:
            parameters["offset"] = offset
        return self.call("getUpdates", **parameters)

    def query(self, update):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        author = message.get("from") or {}
        text = message.get("text")
        if (chat.get("id") != self.chat_id or author.get("is_bot")
                or not isinstance(text, str) or not text.strip()):
            return None
        text = text.strip()
        command, _, argument = text.partition(" ")
        if command.split("@")[0].lower() == "/analyze":
            return argument.strip()
        if chat.get("type") == "private":
            return text
        # In a configured group only explicit commands count as a request.
        return None

    def acknowledge(self, update):
        # Telegram advances the queue only when an offset higher than the update is read.
        self.updates(update["update_id"] + 1, limit=1)

    def reply(self, update, text):
        for chunk in split_telegram_text(text):
            self.call("sendMessage", chat_id=self.chat_id, text=chunk,
                      reply_parameters={"message_id": update["message"]["message_id"],
                                        "allow_sending_without_reply": True})


def has_requests(bot):
    updates = bot.updates()
    for update in updates:
        if bot.query(update) is not None:
            return True
    if updates:
        bot.acknowledge(updates[-1])
    return False


def handle_requests(bot, analyse, limit=3):
    processed = 0
    for update in bot.updates():
        query = bot.query(update)
        if query is not None:
            if not query or query in {"/start", "/help"} or len(query) > 80:
                text = HELP
            else:
                bot.reply(update, RECEIPT)
                try:
                    text = analyse(query)
                except subprocess.TimeoutExpired:
                    print("Symbol analysis time limit reached", flush=True)
                    text = "🟡 자료 조회가 4분을 넘겨 매수 조건을 확인하지 못했습니다. 같은 종목을 다시 요청할 수 있습니다."
                except Exception as exc:
                    print(f"Symbol analysis unavailable ({type(exc).__name__})", flush=True)
                    text = "🟡 분석 자료를 가져오지 못했습니다. 잠시 후 같은 종목명을 다시 보내주세요."
            bot.reply(update, text)
            processed += 1
        bot.acknowledge(update)
        if processed >= limit:
            break
    return processed


def analyse_with_deadline(query):
    # Isolate network calls so a stalled data provider cannot keep the Telegram queue busy.
    # The worker reads the request over stdin and emits only the finished report.
    env = {key: value for key, value in os.environ.items()
           if key not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}}
    result = subprocess.run([sys.executable, "-m", "telegram_symbol_analysis"],
                            input=query, encoding="utf-8", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=ANALYSIS_TIMEOUT, check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    bot = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"))
    if args.check:
        pending = has_requests(bot)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"pending={str(pending).lower()}", file=output)
    else:
        print(f"Telegram stock replies: {handle_requests(bot, analyse_with_deadline)}", flush=True)


if __name__ == "__main__":
    main()
