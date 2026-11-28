"""On-demand analysis using the scanner's existing evidence and entry policies."""
from contextlib import redirect_stdout
from datetime import datetime
from functools import lru_cache
import json
import os
import sys
import unicodedata
from zoneinfo import ZoneInfo

import requests

import kis_intraday
import live_quote
import scanner
from candidate_selection import is_inverse
from company_forecast import build_company_forecast
from execution_quotes import execution_quote
from issuer_consensus import consensus_for
from nonchart_evidence import attach_candidate_evidence
from preopen_signals import evaluate_preopen, next_session
from research_asset import is_fund
from supply_flow import evidence_for, share_line
from tradable_signal import HOLD_REASON_LABELS, TRACK_LABELS, validate_entry

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
DECISION_LABELS = {
    "analysis_incomplete": "재무·공시 등 필수 분석 자료 부족",
    "price_session_mismatch": "최신 거래일 일봉 미확인",
    "analysis_stale_or_missing": "최신 분석 자료 미확인",
    "unconfirmed_uptrend": "상승 추세 확인 대기",
    "weak_relative_trend": "시장 대비 상대강도 부족",
    "unconfirmed_demand": "거래량·수급 확인 대기",
    "insufficient_demand_expansion": "거래량 확대 기준 미달",
    "insufficient_liquidity": "거래대금 기준 미달",
    "extended_price": "단기 급등·이격으로 추격 보류",
    "composite_score_below_policy": "종합점수 기준 미달",
    "catalyst_score_below_policy": "호재형 종합점수 기준 미달",
    "breakdown": "가격 지지 이탈",
    "breakdown_or_rebound": "하락 또는 반등 추세 확인 대기",
    "early_entry_qualifier_missing": "초기 진입에 필요한 수급·기업 점수 미달",
    "inverse_underlying_unverified": "인버스 기초지수 방향 미확인",
    "market_headwind": "인버스 기초지수 방향 조건 미달",
    "last_trade_unverified_or_stale": "최근 체결 시각 미확인",
    "spread_too_wide": "매수·매도 호가 차이 과다",
    "filings_unverified": "최신 공시 확인 대기",
    "us_live_confirmation_unavailable": "미국 장중 돌파·눌림 지지 확인 대기",
}


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


@lru_cache(maxsize=1)
def universe():
    try:
        return scanner.load_universe()
    except (requests.RequestException, ValueError, OSError):
        print("Exchange listing unavailable; using the scanner watchlist", flush=True)
        return {"KR": scanner.KR_SYMBOL_NAMES.copy(), "US": scanner.US_SYMBOL_NAMES.copy()}


def matches(query, listings):
    key = normalized(query)
    ticker = key.upper().removeprefix("$")
    exact_codes, exact_names, partial = [], [], []
    for market, names in listings.items():
        for symbol, name in names.items():
            row = (market, symbol, name)
            if (ticker == symbol or (market == "KR" and ticker == symbol.split(".")[0])
                    or (market == "US" and ticker.replace(".", "-") == symbol)):
                exact_codes.append(row)
            elif normalized(name) == key:
                exact_names.append(row)
            elif len(key) >= 2 and key in normalized(name):
                partial.append(row)
    return exact_codes or exact_names or partial


def now_for(market):
    return datetime.now(KST if market == "KR" else NY)


def price_text(value, market):
    return f"{value:,.0f}원" if market == "KR" else f"${value:,.2f}"


def render(row, at, entry=None, reason=None):
    market, symbol, name = (row[key] for key in ("market","symbol","name"))
    values = row.get("indicators") or {}
    plan = row.get("preopen_signal") or {}
    forecast = row.get("company_forecast") or {}
    risk = row.get("nonchart_risk") or {}
    state = live_quote.session_state(market, at)
    state_label = {"open":"정규장 개장", "preopen":"개장 전", "closed":"정규장 종료·휴장",
                   "unknown":"개장 여부 미확인"}[state]
    if risk:
        title = "🔴 위험 공시로 제외"
    elif entry and entry.get("trade_ready"):
        title = "🟢 매수 조건 충족"
    else:
        title = "🟡 조건부 관찰" if plan else "🟡 매수 대기"
    lines = [f"{title} | {name} ({symbol})",
             f"{at.astimezone(KST):%m/%d %H:%M:%S} KST · {state_label}"]
    if values.get("price"):
        lines.append(f"최근 종가 {price_text(values['price'], market)} ({values.get('data_date') or '일자 미확인'})")
    if risk:
        lines.append(str(risk.get("title") or risk.get("label") or "위험 공시 확인")[:220])
    score = forecast.get("composite_score")
    reasons = (row.get("signal") or {}).get("reasons") or []
    if score is not None:
        lines.append(f"종합 {score:.1f}점 · 차트 {(row.get('signal') or {}).get('score',0)}점")
    if reasons:
        lines.append("차트 " + " · ".join(reasons[:7]))
    catalyst = (plan.get("catalyst") or {}).get("summary") or (row.get("nonchart_catalyst") or {}).get("title")
    if catalyst:
        lines.append(f"확인된 재료 {str(catalyst)[:180]}")
    if row.get("supply_line"):
        lines.append(row["supply_line"])
    if plan:
        label = TRACK_LABELS.get(plan.get("strategy"), "조건부 진입")
        lines.append(f"{label} · 대상 거래일 {plan.get('expires_session') or '미확인'}")
        if entry and entry.get("trade_ready"):
            lines.append(f"확인 매도호가 {price_text(entry['indicative_entry'], market)}")
            if entry.get("stop_price") and entry.get("take_profit_price"):
                lines.append(f"손절 {price_text(entry['stop_price'], market)} · 익절 기준 {price_text(entry['take_profit_price'], market)}")
            valid = entry.get("valid_until")
            if valid:
                expires = datetime.fromisoformat(valid).astimezone(KST)
                lines.append(f"호가 유효 시각 {expires:%H:%M:%S} KST")
        elif plan.get("entry_floor") and plan.get("entry_limit"):
            lines.append(f"관찰 가격 {price_text(plan['entry_floor'], market)}~{price_text(plan['entry_limit'], market)}")
        elif plan.get("entry_limit"):
            lines.append(f"진입 상한 {price_text(plan['entry_limit'], market)}")
        elif plan.get("entry_offset_pct") is not None:
            lines.append(f"시가 대비 {plan['entry_offset_pct']:+.0%} 눌림 관찰")
    if reason and not risk:
        label = DECISION_LABELS.get(reason) or HOLD_REASON_LABELS.get(reason)
        lines.append(label or "기존 스캐너 매수 조건 확인 대기")
    return "\n".join(lines)


def analyse(query):
    candidates = matches(query, universe())
    if not candidates:
        return "🟡 종목을 확인하지 못했습니다. 정확한 종목명·6자리 종목코드·미국 티커를 보내주세요."
    if len(candidates) > 1:
        shown = ", ".join(f"{name} ({symbol})" for _, symbol, name in candidates[:5])
        return f"🟡 같은 이름이 여러 종목에 해당합니다. 종목코드나 티커를 보내주세요.\n{shown}"

    market, symbol, name = candidates[0]
    overseas = market == "US"
    key = "DNAS" + symbol if overseas else symbol.split(".")[0]
    if overseas:
        kis_intraday.US_SYMBOL_NAMES[symbol] = name
    else:
        # company_context uses the suffix to pick the KOSPI or KOSDAQ benchmark.
        kis_intraday.KR_SYMBOL_NAMES[symbol] = name
    companies, risks = kis_intraday.company_context("US-late" if overseas else "KRX",
                                                    [key], now_for(market))
    company = companies.get(key) or {}
    values = company.get("price_indicators") or {}
    row = dict(symbol=symbol, name=name, market=market, indicators=values,
               company_analysis=company)
    at = now_for(market)
    if not values.get("price"):
        return render(row, at, reason="price_session_mismatch")
    inverse = is_inverse(symbol, name)
    if inverse:
        row["asset_type"] = "inverse_etf"
    elif symbol in scanner.UNIVERSE_FUNDS[market]:
        row["asset_type"] = "etf"
    if is_fund(row):
        company["instrument_class"] = "listed_fund"
    row["signal"] = scanner.classify_signal(values, inverse=inverse)
    row["risk_evidence"] = [item for item in risks if item.get("symbol") == key]
    sectors = scanner.sector_flow_snapshot(market, at)
    supply = evidence_for(row, company, at)
    row["institutional_flows"] = supply
    row["supply_line"] = share_line(supply)
    consensus = consensus_for(row, company, at=at, price=values["price"])
    report = {"results":[row], "sectors":sectors}
    if not overseas:
        attach_candidate_evidence(report, now_for(market))
    at = now_for(market)
    row["company_forecast"] = build_company_forecast(company, values, values["price"], at,
        row["risk_evidence"], sectors, supply=supply, consensus=consensus)
    reference = scanner.last_completed_session(market, at).isoformat()
    report["preopen_session"] = next_session(reference, market)
    evaluate_preopen(report, reference, at, market=market)
    plan = row.get("preopen_signal")
    if not plan:
        return render(row, at, reason=row.get("preopen_decision"))
    if live_quote.session_state(market, at) != "open":
        return render(row, at)
    if overseas:
        return render(row, at, reason="us_live_confirmation_unavailable")
    if not is_fund(row) and company.get("filing_status") != "checked":
        return render(row, at, reason="filings_unverified")
    try:
        quote = execution_quote(symbol, at, compare_volume=plan.get("strategy")=="earnings_catalyst")
    except (RuntimeError, requests.RequestException, KeyError, TypeError, ValueError):
        quote = None
    at = now_for(market)
    entry = validate_entry(row, quote, at, live_quote.session_state(market, at)=="open")
    return render(row, at, entry, reason=None if entry.get("trade_ready") else entry.get("reason"))


if __name__ == "__main__":
    query = sys.stdin.read()
    with open(os.devnull, "w") as diagnostics, redirect_stdout(diagnostics):
        reply = analyse(query)
    print(json.dumps(reply))
