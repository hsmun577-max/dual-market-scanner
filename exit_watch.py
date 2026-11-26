"""보유·매도 감시: 계획이 나간 뒤 다음 세션들에서 +6% · 트레일링 · −2/−3% 손절을 본다.

왜 필요한가: 장중 신호 뒤 30분(`kis_intraday.collect(followup_minutes=30)`)만 감시하고,
5거래일 보유 규칙의 익절·트레일링·손절을 **다음 날 이후에** 보는 코드가 없었다. 장전
조건부 계획(07:50·08:50)은 매도 감시에 아예 들어가지 않아, "샀다면 언제 팔라"는 알림이
나가지 않았다(2026-11-26 전수점검 6항).

무엇을 할 수 있고 무엇을 못 하는가:

* 할 수 있는 것 — 계획의 진입 지정가를 기준으로, 계획 세션 이후 일봉의 고가·저가·종가·거래량을
  사용자의 매도 규칙(`exit_policy`)에 그대로 넣어 목표·손절·트레일링·5거래일 만기 중 어느 것이
  먼저 닿았는지 계산한다.
* 못 하는 것 — 이 저장소는 계좌를 읽지 않으므로 **실제 체결 여부를 모른다**. 그래서 모든 줄은
  "계획 지정가에 샀다면"의 가정 위에서 계산하고, 그 가정을 메시지 머리줄에 밝힌다. 체결되지
  않은 계획이 섞여 있을 수 있고, 부분 체결·추가 매수도 반영할 수 없다.

일봉으로 계산하므로 트레일링의 '단기 급등' 판정 창은 한 세션으로 넓힌다(장중 30분 창은
분봉이 있어야 한다). 그 사실도 결과에 `surge_window_source`로 남긴다.
"""

import argparse
from datetime import datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from zoneinfo import ZoneInfo

from exit_policy import FUND_STOP_PCT, ExitTracker, exit_policy_for
from theme_sectors import session_progress

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
# 사용자의 보유 기간: 다섯 거래일이 지나면 목표·손절에 닿지 않았어도 그날 종가로 정리한다.
HOLD_SESSIONS = 5
SESSION_CLOSE = {"KR": time(15, 30), "US": time(16, 0)}
# 일봉 감시에서 '단기 급등' 창: 한 세션 안의 급등을 그 세션 종가에서 판정한다.
DAILY_SURGE_WINDOW = timedelta(hours=8)
HEADING = "[보유·매도 감시] · 체결 미확인 · 계획 진입가 가정"
FOOTNOTE = "※ 계좌 체결을 읽지 않으므로 계획 지정가에 샀다고 가정한 계산입니다. 실제 체결가·수량과 다를 수 있습니다."
EXIT_REASON_LABELS = {
    "take_profit": "목표 +6% 도달",
    "surge_trailing_exit": "급등 후 거래량 급감·고점 이탈",
    "trailing_floor_breached": "트레일링 바닥(+6%) 아래로",
    "stop_loss": "손절 도달",
    "hold_period_end": "5거래일 만기",
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _day(value):
    """'2026-11-25' 또는 날짜 객체 → date. 모르는 값은 None."""
    if value is None:
        return None
    if hasattr(value, "date") and not isinstance(value, str):
        return value.date() if hasattr(value, "hour") else value
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def positions_from_report(report, market=None):
    """분석 보고서의 조건부 계획을 보유 감시 대상으로 바꾼다.

    진입은 계획이 가리키는 세션의 지정가(`entry_limit`)이고, 그 가격에 샀다는 것은 가정이다.
    반등·단기 초입 트랙도 자기 진입가를 그대로 쓴다. 만기 세션은 진입 세션부터 다섯 거래일이다.
    """
    report = report or {}
    positions = []
    entry_session = _day(report.get("preopen_session"))
    for row in report.get("results") or []:
        plan = (row or {}).get("preopen_signal") or {}
        if plan.get("status") != "conditional_buy":
            continue
        if market and row.get("market") != market:
            continue
        entry = _number(plan.get("entry_limit")) or _number(plan.get("flat_open_entry_limit"))
        if entry is None or entry <= 0:
            continue
        session = _day(plan.get("expires_session")) or entry_session
        if session is None:
            continue
        policy = exit_policy_for(row)
        positions.append(dict(
            symbol=row.get("symbol"), name=row.get("name") or row.get("symbol"),
            market=row.get("market") or market or "KR", strategy=plan.get("strategy"),
            entry=entry, entry_session=session.isoformat(),
            reference_date=plan.get("reference_date"),
            asset_class="fund" if policy.stop_pct == FUND_STOP_PCT else "stock",
            stop_pct=policy.stop_pct, take_profit_pct=policy.take_profit_pct,
            hold_sessions=HOLD_SESSIONS,
            entry_assumption="plan_limit_not_confirmed_fill"))
    return positions


def _policy_for(position):
    """그 종목의 매도 규칙 + 일봉 판정용 급등 창."""
    base = exit_policy_for({"asset_type": "etf" if position.get("asset_class") == "fund" else "stock"})
    try:
        from dataclasses import replace
        return replace(base, surge_window=DAILY_SURGE_WINDOW)
    except (TypeError, ValueError):
        return base


def _baseline_volume(frame, entry_session):
    """진입 세션 직전 20세션 평균 거래량(급등 판정의 기준)."""
    sessions = []
    for stamp, row in frame.iterrows():
        day = _day(stamp)
        volume = _number(row.get("Volume"))
        if day is not None and volume is not None and day < entry_session:
            sessions.append((day, volume))
    sessions.sort()
    sample = [volume for _, volume in sessions[-20:]]
    return sum(sample) / len(sample) if sample else None


def evaluate_position(position, frame, now=None):
    """계획 세션 이후 일봉으로 지금 상태(보유/청산)를 낸다.

    판정은 종가 기준이다. 저가가 손절선을 찍고 종가가 회복하면 계속 보유로 본다(장중 체결
    가정을 하지 않는다). 목표·트레일링 바닥은 `ExitTracker`가 정하고, 만기 세션 종가가
    남으면 5거래일 규칙으로 정리한다.
    """
    now = now or datetime.now(KST)
    entry_session = _day(position.get("entry_session"))
    policy = _policy_for(position)
    close_time = SESSION_CLOSE.get("KR" if str(position.get("market")) == "KR" else "US",
                                   time(15, 30))
    zone = KST if str(position.get("market")) == "KR" else NY
    result = dict(symbol=position.get("symbol"), name=position.get("name"),
                  market=position.get("market"), strategy=position.get("strategy"),
                  entry=position.get("entry"), entry_session=position.get("entry_session"),
                  stop_pct=policy.stop_pct, take_profit_pct=policy.take_profit_pct,
                  stop_price=round(position["entry"] * (1 - policy.stop_pct), 4),
                  target_price=round(position["entry"] * (1 + policy.take_profit_pct), 4),
                  status="awaiting_session", sessions_held=0,
                  surge_window_source="one_daily_session",
                  entry_assumption=position.get("entry_assumption"))
    if entry_session is None or frame is None or getattr(frame, "empty", True):
        return result
    bars = []
    for stamp, row in frame.iterrows():
        day = _day(stamp)
        if day is None or day < entry_session:
            continue
        closing = _number(row.get("Close"))
        if closing is None or closing <= 0:
            continue
        bars.append((day, closing, _number(row.get("High")), _number(row.get("Low")),
                     _number(row.get("Volume"))))
    bars.sort(key=lambda item: item[0])
    if not bars:
        return result
    tracker = ExitTracker(position["entry"], policy,
                          entry_at=datetime.combine(bars[0][0], time(9, 30) if zone is NY
                                                    else time(9, 0), tzinfo=zone),
                          baseline_volume=_baseline_volume(frame, entry_session))
    decision = None
    held = 0
    for index, (day, closing, high, low, volume) in enumerate(bars, 1):
        at = datetime.combine(day, close_time, tzinfo=zone)
        decision = tracker.observe(at, closing, volume=volume, high=high, low=low)
        held = index
        if decision["action"] == "exit":
            break
        if index >= HOLD_SESSIONS:
            # 만기: 목표에도 손절에도 닿지 않은 채 5거래일이 지나면 그 종가로 정리한다.
            decision = dict(decision, action="exit", reason="hold_period_end",
                            exit_price=closing, price=closing, at=at.isoformat(),
                            mode=decision.get("mode"), hold_rule_sessions=HOLD_SESSIONS)
            break
    last = bars[-1]
    result.update(sessions_held=held, current_price=last[1],
                  current_return_pct=round((last[1] / position["entry"] - 1) * 100, 2))
    if decision is None:
        return result
    result["mode"] = decision.get("mode")
    if decision["action"] == "exit":
        price = _number(decision.get("exit_price")) or last[1]
        result.update(status="exited", exit_reason=decision.get("reason"),
                      exit_at=decision.get("at"), exit_price=price,
                      exit_return_pct=round((price / position["entry"] - 1) * 100, 2),
                      sessions_held=held)
        # 마지막 봉이 아직 끝나지 않은 오늘 봉이면 그 청산은 잠정이다(장중에 다시 오를 수 있다).
        if last[0] == now.date() and session_progress(now, position.get("market")) is not None:
            result.update(provisional=True, exit_at=now.isoformat())
    else:
        result.update(status="open")
    return result


def _price_text(value, market):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    suffix = "$" if str(market) == "US" else "원"
    return f"{value:,.2f}{suffix}" if str(market) == "US" else f"{value:,.0f}원"


def format_block(rows, at=None, limit=5):
    """매도 감시 블록. 계획 줄과 같은 3줄 형식(종목 · 현재 상태 · 다음 기준)을 쓴다."""
    rows = [row for row in (rows or []) if row]
    if not rows:
        return []
    at = at or datetime.now(KST)
    ordered = sorted(rows, key=lambda row: (row.get("status") != "open",
                                            -(row.get("current_return_pct") or 0)))
    lines = [f"{HEADING} · {at:%m/%d %H:%M} KST"]
    for row in ordered[:limit]:
        market = row.get("market")
        code = str(row.get("symbol") or "").split(".")[0]
        entry_day = str(row.get("entry_session") or "")[5:].replace("-", "/")
        lines.append(f"• {row.get('name')} ({code}) · {entry_day} 계획 "
                     f"{_price_text(row.get('entry'), market)}")
        if row.get("status") == "exited":
            reason = EXIT_REASON_LABELS.get(row.get("exit_reason"), row.get("exit_reason") or "")
            when = str(row.get("exit_at") or "")[:10][5:].replace("-", "/")
            price = _price_text(row.get("exit_price"), market)
            gain = row.get("exit_return_pct")
            gain_text = f" ({gain:+.1f}%)" if isinstance(gain, (int, float)) else ""
            state = " · 장중 잠정" if row.get("provisional") else ""
            lines.append(f"  {reason} · {when} {price} 청산{gain_text}{state}")
        elif row.get("status") == "open":
            lines.append(f"  현재 {_price_text(row.get('current_price'), market)}"
                         f" ({row.get('current_return_pct'):+.1f}%) · "
                         f"{row.get('sessions_held')}/{HOLD_SESSIONS}거래일")
            lines.append(f"  목표 {_price_text(row.get('target_price'), market)} · "
                         f"손절 {_price_text(row.get('stop_price'), market)}"
                         + (" · 트레일링 보유" if row.get("mode") == "trailing" else ""))
        else:
            lines.append("  아직 진입 세션 봉이 없습니다 · 다음 거래일 확인")
    if len(rows) > limit:
        lines.append(f"   외 {len(rows) - limit}건")
    lines.append(FOOTNOTE)
    return lines


def watch(report, market=None, now=None, downloader=None, limit=5, max_age_days=5):
    """보고서 한 부를 받아 매도 감시 줄과 계산 결과를 돌려준다.

    오래된 계획은 싣지 않는다: 진입 세션은 15일, 청산 기록은 `max_age_days`일까지.
    """
    positions = positions_from_report(report, market)
    if not positions:
        return [], []
    now = now or datetime.now(KST)
    today = now.date()
    positions = [position for position in positions
                 if _day(position["entry_session"])
                 and (today - _day(position["entry_session"])).days <= 15]
    if not positions:
        return [], []
    symbols = list(dict.fromkeys(position["symbol"] for position in positions))
    frame_by_symbol = {}
    try:
        if downloader is not None:
            prices = downloader(symbols, market or positions[0]["market"])
        else:
            from scanner import yf_download
            prices = yf_download(symbols, period="2mo", interval="1d", auto_adjust=False,
                                 group_by="ticker", progress=False, threads=6, timeout=12,
                                 multi_level_index=True)
        for symbol in symbols:
            try:
                frame = prices[symbol]
            except (KeyError, IndexError, TypeError):
                continue
            if frame is not None and not getattr(frame, "empty", True):
                frame_by_symbol[symbol] = frame
    except Exception as exc:  # 시세 실패가 알림을 막지 않는다
        print(f"exit watch prices unavailable ({type(exc).__name__})")
    rows = [evaluate_position(position, frame_by_symbol.get(position["symbol"]), now)
            for position in positions]
    recent = []
    for row in rows:
        if row.get("status") == "exited":
            exited = _day(row.get("exit_at"))
            if exited is not None and (today - exited).days > max_age_days:
                continue
        recent.append(row)
    return format_block(recent, now, limit), recent


def previous_analysis(market, workflow="preopen-delivery.yml", runner=None, limit=10):
    """직전 성공 실행의 `preopen-analysis.json`을 gh로 내려받아 읽는다.

    이 저장소는 실행 사이 상태를 깃에 커밋하지 않고 산출물(artifact)로만 넘기므로
    (`composite-signal-check`도 같은 방식), 직전 계획은 그 산출물에서 읽는다.
    찾지 못하면 None을 돌려주고 호출부는 매도 감시를 생략한다(알림을 막지 않는다).
    """
    runner = runner or subprocess.run
    name = f"preopen-delivery-{market}"
    try:
        listing = runner(
            ["gh", "run", "list", "--workflow", workflow, "--status", "success",
             "--limit", str(limit), "--json", "databaseId,createdAt"],
            capture_output=True, text=True, timeout=60)
        runs = json.loads(getattr(listing, "stdout", "") or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"exit watch artifact listing unavailable ({type(exc).__name__})")
        return None
    for run in sorted(runs or [], key=lambda item: str(item.get("createdAt") or ""), reverse=True):
        folder = tempfile.mkdtemp(prefix="exit-watch-")
        try:
            got = runner(["gh", "run", "download", str(run.get("databaseId")), "-n", name,
                          "-D", folder], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"exit watch artifact download failed ({type(exc).__name__})")
            continue
        if getattr(got, "returncode", 1) != 0:
            continue
        for path in Path(folder).rglob("preopen-analysis.json"):
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
    return None


def _send(lines, parts_limit=3800):
    from telegram_text import split_telegram_text
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram skipped: configuration not set")
        return 0
    sent = 0
    for part in split_telegram_text("\n".join(lines), limit=parts_limit):
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": part}, timeout=10)
        if response.status_code != 200:
            raise RuntimeError(f"Telegram returned HTTP {response.status_code}")
        sent += 1
    return sent


def main():
    parser = argparse.ArgumentParser(description="계획 진입가 기준 보유·매도 감시")
    parser.add_argument("--analysis", help="장전/장중 분석 JSON (preopen-analysis.json 등)")
    parser.add_argument("--from-github", action="store_true",
                        help="직전 성공 실행의 preopen-analysis 산출물을 gh로 내려받아 쓴다")
    parser.add_argument("--workflow", default="preopen-delivery.yml")
    parser.add_argument("--market", default=None,
                        help="KR/US, 또는 산출물 이름(NXT·KRX·US_PRE·US)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--send", action="store_true", help="텔레그램으로 보낸다")
    parser.add_argument("--out", default="exit-watch.json")
    args = parser.parse_args()
    # `preopen-delivery`의 산출물 이름은 NXT·KRX·US_PRE·US이고, 보고서 안의 시장은 KR·US다.
    value = str(args.market or "").upper()
    report_market = "US" if value.startswith("US") else ("KR" if value else None)
    if args.from_github:
        if not args.market:
            raise SystemExit("--from-github requires --market")
        report = previous_analysis(args.market, args.workflow)
        if report is None:
            print("EXIT_WATCH=no_previous_analysis")
            return
    elif args.analysis:
        report = json.loads(Path(args.analysis).read_text(encoding="utf-8-sig"))
    else:
        raise SystemExit("give --analysis or --from-github")
    lines, rows = watch(report, report_market, limit=args.limit)
    Path(args.out).write_text(json.dumps(dict(generated_at=datetime.now(KST).isoformat(),
                                              positions=rows), ensure_ascii=False, indent=2),
                              encoding="utf-8")
    if not lines:
        print("EXIT_WATCH=no_conditional_plans")
        return
    print("\n".join(lines))
    if args.send:
        print(f"EXIT_WATCH=sent_parts:{_send(lines)}")


if __name__ == "__main__":
    main()
