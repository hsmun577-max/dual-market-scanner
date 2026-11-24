"""Explainable five-session scenarios from financial, price and event evidence.

These are uncalibrated scenarios. Data coverage is not a probability of profit.
"""

import math
from datetime import datetime, timedelta
from catalyst_policy import SECTOR_PROXIES,catalyst_context
from research_asset import is_fund_company
import re


# Entry policy: the five-session base scenario must cover this much before a candidate
# is worth the 2% stop. Both entry paths (live intraday and preopen) read it from here so
# the floor and the scenario formula below cannot drift apart again.
MIN_BASE_REWARD_PCT = 3.3
# Evidence gaps that withhold a score. They are integrity or completeness failures of the
# financial statements themselves: a newer filing than the quarter scored, statements too
# old for the period, or evidence stamped in the future. A missing supporting dimension
# (valuation multiples, sector flow, event bodies, cash-flow detail) is a caveat that
# reduces the edge, not a reason to discard the whole issuer.
BLOCKING_GAPS = frozenset({"current_financial_period", "latest_report_not_in_financials",
                           "future_company_evidence"})
# One composite threshold for every entry path (live intraday, preopen plan and the
# execution check), so a candidate cannot alert and then be refused. Lowered from 60 to 53
# by the user's policy decision; the 3.3% base-reward floor still gates every entry.
MIN_COMPOSITE_SCORE = 53.0
# A revenue growth rate beyond this is a period-basis or unit mix-up, not growth.
MAX_VERIFIED_REVENUE_GROWTH_PCT = 300.0
# Scenario shape. One five-session sigma at this calibration score, growing with the
# square root of the evidence bias and capped at two sigmas. The earlier
# 0.6 x (daily volatility x sqrt(5)) x bias coefficient left a candidate at this score at
# about 0.7%, so no realistic quality candidate could ever reach the 3.3% floor.
# This is the scenario calibration anchor, not the entry gate: it stays at 60 so lowering
# MIN_COMPOSITE_SCORE does not inflate every candidate's base/bear/bull prices.
SCENARIO_THRESHOLD_SCORE = 60.0
# A listed fund has no issuer income statement: growth, earnings quality and balance sheet
# are not missing evidence for it, they do not exist. What the instrument is priced from
# is its own price and demand, the sector proxy it tracks and its event exposure, so those
# three carry the whole fund score. Owner's decision 2026-11-23: the eight most traded ETFs
# of each market are candidate-grade instruments; graded as operating companies they were
# stopped at "analysis incomplete" for every ETF (QQQ, SPY, BIL...) and never planned.
FUND_WEIGHTS = {"price_and_demand": .50, "sector_pressure": .20, "events": .30}
COMPANY_WEIGHTS = {"growth": .24, "earnings_quality": .18, "balance_sheet": .12,
                   "valuation_outlook": .10, "price_and_demand": .14,
                   "sector_pressure": .10, "events": .12}
# A broker screen (Reuters focus, 2026-11-23 on 롯데정밀화학) scores what the company is
# worth apart from who is buying it, and the owner asked for that separation
# ("수급확인도필요해"). The two axes below are reported beside the composite score, whose
# own weights and entry gate are deliberately left untouched: the report now says which
# half of the evidence is strong, it does not re-tune the buy threshold here. A dimension
# missing from an axis is dropped from that axis's coverage rather than scored 0.
FUNDAMENTAL_AXIS = {"growth": .20, "earnings_quality": .20, "balance_sheet": .12,
                    "valuation_outlook": .18, "analyst_target": .30}
DEMAND_AXIS = {"price_and_demand": .50, "sector_pressure": .20,
               "institutional_demand": .20, "events": .10}
# The gap between the broker mean target and the price saturates here: a +80% consensus
# gap is as strong as this axis can read, so a data error on a tiny price cannot dominate.
TARGET_UPSIDE_SATURATION_PCT = 80.0
# A '수급 대기 우량주' must clear this on the fundamental axis alone. The 12-month
# backtest of 2025-11-24~2026-11-09 (621 issuers, next-open entry, -2%/+6%, 5 sessions,
# 0.28% cost) put the per-trade mean of the 10억↑ quality track at -0.06% at 61 points
# against -0.11% at 58, and at +0.06% when the measured foreign/institution flow was
# already an inflow. The floor is therefore 61.
QUALITY_FUNDAMENTAL_SCORE = 61.0
# ...and an axis built from fewer than this share of its weights is not called a grade.
QUALITY_MIN_COVERAGE = .50
# composite_score = 50 + 50 * after_pricing, so this is the bias at the threshold.
SCENARIO_EVIDENCE_BIAS = (SCENARIO_THRESHOLD_SCORE - 50) / 50
SCENARIO_SIGMA_CAP = 2.0


def _number(mapping, key):
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


# A forward multiple this far from trailing is a basis or unit mismatch, not an outlook.
# Two providers are compared (KIS trailing PE, Yahoo forward PE for Korean issuers), and a
# Yahoo forward EPS that is off by a factor of ten (Samsung Electronics 27.56 trailing vs
# 2.35 forward) turned the axis into a constant +4.9 points for every Korean large cap.
FORWARD_TO_TRAILING_BOUNDS = (1 / 3, 3)


def valuation_outlook_component(company):
    """Earnings-outlook axis from two multiples, only when they are comparable.

    Returns ``(score, evidence, exclusion)``. Nothing is scored when the trailing and
    forward multiples come from different sources or when the forward multiple is more
    than 3x away from trailing; the caller then falls back to peer multiples, which are
    same-source, instead of dividing one provider by another.
    """
    trailing = _number(company, "trailing_pe")
    forward = _number(company, "forward_pe")
    if trailing is None or forward is None or min(trailing, forward) <= 0:
        return None, None, None
    trailing_source = company.get("trailing_pe_source")
    forward_source = company.get("forward_pe_source")
    pair = {"trailing_pe": trailing, "forward_pe": forward,
            "trailing_pe_source": trailing_source, "forward_pe_source": forward_source}
    if trailing_source and forward_source and trailing_source != forward_source:
        return None, None, {"reason": "mixed_sources", **pair}
    ratio = trailing / forward
    low, high = FORWARD_TO_TRAILING_BOUNDS
    if not low <= ratio <= high:
        return None, None, {"reason": "implausible_forward_multiple",
                            "trailing_to_forward": round(ratio, 4), **pair}
    return (math.tanh(math.log(ratio)),
            {**pair, "meaning": "earnings outlook at current pricing, not peer fair value"},
            None)


def analyst_target_component(consensus):
    """Score from the broker mean target gap, damped by the recommendation mean.

    Returns ``(score, evidence)``; ``(None, None)`` when no target was observed. The
    recommendation is only a small correction: on Naver's scale a higher number is a
    better rating, on Yahoo's a lower one, so each is mapped to -1..1 before it is used.
    """
    if not consensus or consensus.get("status") != "measured":
        return None, None
    upside = _number(consensus, "upside_pct")
    if upside is None:
        return None, None
    base = math.tanh(upside / TARGET_UPSIDE_SATURATION_PCT)
    rating = _number(consensus, "recommendation_mean")
    terms = [(.8, base)]
    if rating is not None:
        direction = 1 if consensus.get("market") == "KR" else -1
        terms.append((.2, max(-1., min(1., direction * (rating - 3) / 2))))
    score = sum(weight * value for weight, value in terms) / sum(weight for weight, _ in terms)
    return score, {"target_price": consensus.get("target_price"),
                   "price": consensus.get("price"), "upside_pct": upside,
                   "recommendation_mean": rating, "analyst_count": consensus.get("analyst_count"),
                   "as_of": consensus.get("as_of"), "source": consensus.get("source")}


def institutional_demand_component(supply):
    """Score from the measured per-issuer flow; a proxy counts at half strength."""
    if not supply or supply.get("status") not in {"measured", "proxy"}:
        return None
    value = _number(supply, "score")
    if value is None:
        return None
    return value if supply.get("status") == "measured" else .5 * value


def axis_reading(components, weights):
    """Weighted reading of one axis over the dimensions it actually holds."""
    present = {key: value for key, value in components.items() if key in weights}
    coverage = sum(weights[key] for key in present)
    value = sum(weights[key] * present[key] for key in present)
    return {"score": round(50 + 50 * max(-1., min(1., value)), 1) if coverage > 0 else None,
            "direction": round(value, 6) if coverage > 0 else None,
            "coverage": round(coverage, 4), "components": present}


def quality_grade(fundamental, institutional_flow):
    """'수급 유입 우량주' / '수급 대기 우량주' / None from the two axes.

    A quality name needs the fundamental axis alone (no price, no flow) at or above
    ``QUALITY_FUNDAMENTAL_SCORE`` over at least half its weights. What separates the two
    grades is whether the measured flow is already an inflow or not yet.
    """
    score = fundamental.get("score") if fundamental else None
    if score is None or fundamental.get("coverage", 0) < QUALITY_MIN_COVERAGE:
        return None
    if score < QUALITY_FUNDAMENTAL_SCORE:
        return None
    evidence = institutional_flow or {}
    if evidence.get("status") == "measured" and evidence.get("direction") == "inflow":
        return "supply_inflow_quality"
    return "supply_waiting_quality"


def financial_period_missing(company):
    """A newer periodic filing must not silently leave an older quarter scored."""
    try:
        quarter = datetime.fromisoformat(company.get("quarter", "")[:7] + "-01").date()
    except ValueError:
        return False
    for filing in company.get("filings", []):
        form = filing.get("form", "")
        period = filing.get("report_period")
        if period and form in {"10-Q", "10-K", "20-F"}:
            if period[:7] > company.get("quarter", "")[:7]:
                return True
        day = (filing.get("accepted_at") or filing.get("filing_date") or "")[:10]
        try:
            filed = datetime.fromisoformat(day).date()
        except ValueError:
            continue
        # Conservative fallback when the index does not expose reportDate.
        if form in {"10-Q", "10-K", "20-F"} and (filed-quarter).days > 120:
            return True
    return False


def build_company_forecast(company, indicators, price, at, events=(), sectors=(),
                           supply=None, consensus=None):
    """Combine available evidence without treating missing observations as neutral.

    A listed fund is read on the fund model (:data:`FUND_WEIGHTS`) instead of the issuer
    model: its score comes from its own price and demand, its sector proxy and its event
    exposure, and the issuer dimensions are not applied to it at all rather than counted
    as missing evidence that would withhold the score.
    """
    if at.utcoffset() is None or not math.isfinite(price) or price <= 0:
        raise ValueError("Forecast requires a dated positive price")
    fund = is_fund_company(company)
    weights = dict(FUND_WEIGHTS) if fund else dict(COMPANY_WEIGHTS)
    components = {}
    evidence = {}
    margin = _number(company, "operating_margin_pct")
    growth = _number(company, "revenue_yoy_pct")
    margin_change = _number(company, "operating_margin_yoy_pp")
    growth_unverified = False
    if growth is not None and abs(growth) > MAX_VERIFIED_REVENUE_GROWTH_PCT:
        # Beyond this the figure is a period/scale mix-up, not a growth rate.
        growth_unverified = True
        growth = None
    if growth is not None or margin_change is not None:
        terms = []
        if growth is not None:
            terms.append((.6, math.tanh(growth/30)))
        if margin_change is not None:
            terms.append((.4, math.tanh(margin_change/5)))
        components["growth"] = sum(w*v for w,v in terms)/sum(w for w,_ in terms)
        evidence["growth"] = {"revenue_yoy_pct": growth, "operating_margin_yoy_pp": margin_change}
    quality = []
    if margin is not None:
        quality.append((.4, math.tanh(margin/15)))
    free_cash_margin = _number(company, "free_cash_flow_margin_pct")
    conversion = _number(company, "operating_cash_conversion")
    operating_cash_margin=_number(company,'operating_cash_flow_margin_pct')
    if free_cash_margin is not None:
        quality.append((.4, math.tanh(free_cash_margin/10)))
    if conversion is not None:
        quality.append((.2, math.tanh(conversion-1)))
    if free_cash_margin is None and conversion is None and operating_cash_margin is not None:
        quality.append((.4,math.tanh(operating_cash_margin/10)))
    if quality:
        components["earnings_quality"] = sum(w*v for w,v in quality)/sum(w for w,_ in quality)
        evidence["earnings_quality"] = {"operating_margin_pct": margin,
            "free_cash_flow_margin_pct": free_cash_margin, "operating_cash_conversion": conversion,
            'operating_cash_flow_margin_pct':operating_cash_margin}
    debt = _number(company, "net_debt_to_equity")
    if debt is not None:
        components["balance_sheet"] = -math.tanh(debt)
        evidence["balance_sheet"] = {"net_debt_to_equity": debt}
    else:
        liabilities = _number(company,"liabilities_to_equity")
        if liabilities is not None:
            components["balance_sheet"] = -math.tanh(liabilities-1)
            evidence["balance_sheet"] = {"liabilities_to_equity":liabilities}
    valuation, valuation_evidence, valuation_excluded = valuation_outlook_component(company)
    if valuation_excluded is not None:
        evidence["valuation_outlook_excluded"] = valuation_excluded
    if valuation is not None:
        components["valuation_outlook"] = valuation
        evidence["valuation_outlook"] = valuation_evidence
    elif company.get('peer_valuation'):
        peer = company['peer_valuation']
        peer_at = datetime.fromisoformat(peer['observed_at'])
        value = _number(peer, 'score')
        if (peer_at.utcoffset() is not None and at-timedelta(hours=24)<=peer_at<=at
                and peer.get('peer_count',0)>=3 and value is not None):
            components['valuation_outlook'] = max(-1,min(1,value))
            evidence['valuation_outlook'] = peer
    momentum = _number(indicators, "return_5d")
    strength = _number(indicators, "relative_strength")
    volume = _number(indicators, "volume_ratio")
    terms = []
    if momentum is not None:
        terms.append((.5,math.tanh(momentum/.06)))
    if strength is not None:
        terms.append((.3,math.tanh(strength/.10)))
    if volume is not None and volume>0 and momentum is not None:
        terms.append((.2,math.tanh(math.log(volume))*math.tanh(momentum/.03)))
    if terms:
        components["price_and_demand"] = sum(w*v for w,v in terms)/sum(w for w,_ in terms)
        evidence["price_and_demand"] = {"return_5d":momentum,"relative_strength":strength,"volume_ratio":volume}
    sector_names = SECTOR_PROXIES
    matched = []
    official_matched=[]
    for row in sectors:
        if company.get("market") and row.get("market") != company["market"]:
            continue
        if row.get('sector_kind')=='official_industry':
            normal=lambda text:re.sub(r'[^0-9a-z가-힣]','',str(text).lower())
            own=normal(company.get('domestic_industry_name'))
            exchange='KSQ' if str(company.get('source_symbol','')).endswith('.KQ') else 'KSP'
            seen=datetime.fromisoformat(row['data_as_of']) if row.get('data_as_of') else None
            rank=_number(row,'leadership_score')
            if (own and own==normal(row.get('name')) and row.get('exchange')==exchange
                    and row.get('data_date') in {indicators.get('data_date'),at.astimezone(seen.tzinfo).date().isoformat() if seen else ''}
                    and seen and seen.utcoffset() is not None and at-timedelta(minutes=15)<=seen<=at
                    and rank is not None and 0<=rank<=1):
                official_matched.append(2*rank-1)
            continue
        if row.get("name") not in sector_names.get(company.get("sector"),set()):
            continue
        stamp = row.get("data_as_of")
        if stamp and datetime.fromisoformat(stamp)>at:
            continue
        if not stamp and row.get("data_date", "9999")>at.date().isoformat():
            continue
        excess = _number(row,"market_excess_pct")
        ratio = _number(row,"turnover_ratio")
        if excess is not None and ratio is not None and ratio>0:
            matched.append(math.tanh(excess)*min(1,ratio/2))
    market_return = _number(indicators,"market_return_20d")
    sector_method='dated_sector_etf_price_and_turnover'
    if not matched and official_matched:
        matched=official_matched
        sector_method='official_matching_industry_relative_leadership'
    if matched and market_return is not None:
        components["sector_pressure"] = .5*sum(matched)/len(matched)+.5*math.tanh(market_return/.1)
        evidence["sector_pressure"] = {"sector":company.get("sector"),"matching_proxies":len(matched),
            'method':sector_method,'domestic_industry':company.get('domestic_industry_name')}
    event_terms = []
    uncertainty = 0.
    unquantified_events = 0
    for filing in company.get("filings",[]):
        ratio = _number(filing.get("contract",{}),"revenue_ratio_pct")
        day = filing.get("filing_date", "")
        try:
            age = (at.date()-datetime.fromisoformat(day).date()).days
        except ValueError:
            continue
        if ratio is not None and 0 <= age <= 14:
            if not any(word in filing.get("form", "") for word in ("해지","취소","정정")):
                conversion_quality = .5+.5*math.tanh((margin or 0)/15)
                event_terms.append(.6*math.tanh(ratio/20)*conversion_quality)
                uncertainty=max(uncertainty,.3)
    for row in events:
        if row.get("available_at"):
            stamp=datetime.fromisoformat(row["available_at"])
            if stamp.utcoffset() is None or not at-timedelta(hours=24)<=stamp<=at:
                continue
        if row.get("kind")=="market":
            snapshot = row.get("snapshot",{})
            for symbol,scale in (("^VIX",10),("KRW=X",3),("DX-Y.NYB",2),("CL=F",10)):
                movement = _number(snapshot.get(symbol,{}),"change_pct")
                if movement is not None:
                    uncertainty=max(uncertainty,min(1,abs(movement)/scale))
        if row.get("assessment") in {"risk","caution"}:
            impact = _number(row,"estimated_revenue_impact_pct")
            if impact is not None:
                event_terms.append(math.tanh(impact/20))
            else:
                unquantified_events += 1
            uncertainty=max(uncertainty,.8 if row["assessment"]=="risk" else .4)
    feeds_checked = (company.get("filing_status")=="checked"
                     and company.get("news_status") in {"ok","no_recent_articles"})
    causal = company.get("event_analysis", {})
    causal_score = _number(causal, "score")
    causal_stamp = datetime.fromisoformat(causal["generated_at"]) if causal.get("generated_at") else None
    causal_valid = causal_stamp is not None and causal_stamp.utcoffset() is not None and at-timedelta(hours=24)<=causal_stamp<=at
    if causal_valid and causal_score is not None:
        event_terms.append(max(-1, min(1, causal_score)))
        # Automatic causal rules remain hypotheses: preserve uncertainty in the range.
        uncertainty = max(uncertainty, .4)
    expectation_links = causal.get('economic_graph', {}).get('expectation_links', []) if causal_valid else []
    positive_expectations = [row for row in expectation_links
        if row.get('watch_bias') == 'positive_policy_sympathy' and row.get('source_url')]
    if positive_expectations:
        # One bounded sentiment term, regardless of how many copies mention the policy.
        # This is market expectation, not permission, contract value or a revenue forecast.
        event_terms.append(.1)
        uncertainty = max(uncertainty, .6)
    advance_expectations=[row for row in expectation_links
        if row.get('watch_bias')=='pre_event_business_exposure' and row.get('publication_verified') is True]
    if advance_expectations:
        event_terms.append(.03)
        uncertainty=max(uncertainty,.8)
    if event_terms:
        components["events"] = math.tanh(sum(event_terms))
    elif (causal_valid and causal.get("status") == "no_material_events_identified" and feeds_checked):
        components["events"] = 0.
    evidence["events"] = {"event_direction_terms":len(event_terms),"quantified_earnings_events":0,"unquantified_events":unquantified_events,
                          "policy_expectation_present":bool(positive_expectations),
                          "pre_event_exposure_present":bool(advance_expectations),
                          "feeds_checked":feeds_checked,"uncertainty":uncertainty,
                          "causal_analysis":causal if causal_valid else {"status":"unavailable_or_stale"},
                          "meaning":"rule based event direction; not quantified earnings impact"}
    unread_required = sum(1 for row in causal.get("unread_events", []) if row.get("required", True))
    if unread_required:
        # A body we could not read removes positive event credit; it does not erase the
        # financial and price evidence we did verify. Keep the score and widen it.
        uncertainty = max(uncertainty, min(.8, .25 + .1 * unread_required))
    # A missing dimension credits nothing: the raw sum keeps its weight at zero instead
    # of renormalising the rest upward. An issuer with three of seven dimensions scored
    # therefore keeps a small edge, not the full strength of its strongest component.
    coverage = sum(weights[key] for key in components)
    composite = sum(weights[key]*value for key,value in components.items())
    ma20 = _number(indicators,"ma20")
    extension = max(0,price/ma20-1) if ma20 is not None and ma20>0 else 0.
    # A rise that has already occurred reduces the remaining event/earnings scenario.
    priced_in = min(.8, max(0,(momentum or 0)-.05)/.15 + max(0,extension-.03)/.12)
    catalyst=catalyst_context(company,indicators,price,at,sectors)
    if catalyst['eligible']:
        priced_in=min(.8,catalyst['post_available_return_pct']/(1.5*indicators['atr14_pct']))
    daily_volatility = _number(indicators,"volatility_20d")
    atr = _number(indicators,"atr14_pct")
    volatility_source = "observed_daily_returns"
    if daily_volatility is None or daily_volatility<=0:
        daily_volatility = atr/140 if atr is not None and atr>0 else .025
        volatility_source = "ATR_proxy" if atr is not None and atr>0 else "assumed_2.5pct_daily"
    scale = max(.003,daily_volatility)*math.sqrt(5)
    after_pricing = composite*(1-priced_in)-.15*priced_in-.1*uncertainty
    bias = after_pricing/SCENARIO_EVIDENCE_BIAS
    sigma_multiple = math.copysign(min(SCENARIO_SIGMA_CAP, math.sqrt(abs(bias))), bias)
    drift = scale*sigma_multiple
    width = scale*(1+(1-coverage)*.7+uncertainty*.5+priced_in*.3)
    # The entry price stays inside the bear/bull range: the wider of the volatility
    # spread and the base scenario, so a strong candidate's bear case is not a gain.
    width = max(width, abs(drift))
    missing = [key for key in weights if key not in components]
    if not causal_valid:
        missing.append("causal_event_analysis")
    elif unread_required:
        missing.append("unread_event_bodies")
    if causal_valid and not any(row.get("status")=="checked" for row in causal.get("feed_coverage", [])):
        missing.append("world_event_coverage")
    if not fund:
        # Statement completeness only means something for an issuer. A fund has no
        # statement to be current, so these gaps are not attached to it.
        if financial_period_missing(company):
            missing.append("latest_report_not_in_financials")
        if growth_unverified or company.get("revenue_scale_check") == "mismatch_with_yfinance":
            missing.append("growth_scale_unverified")
        if (debt is None and company.get("leverage_status") == "rejected_out_of_range"):
            missing.append("leverage_unverified")
        if free_cash_margin is None and conversion is None and operating_cash_margin is None:
            missing.append("cash_flow_quality")
        quarter = company.get("quarter", "")[:7]
        try:
            quarter_at = datetime.fromisoformat(quarter+"-01").date()
            if not 0 <= (at.date()-quarter_at).days <= 200:
                missing.append("current_financial_period")
        except ValueError:
            missing.append("current_financial_period")
    available = company.get("available_at") or company.get("checked_at")
    if available and datetime.fromisoformat(available)>at:
        missing.append("future_company_evidence")
    blocking = [gap for gap in missing if gap in BLOCKING_GAPS]
    if not fund and "growth" not in components and "earnings_quality" not in components:
        # No income statement at all would make the score a chart reading, not a company
        # assessment; that case stays unscored whatever else was collected. A fund is the
        # one instrument whose score is meant to be a price reading of what it tracks.
        blocking.append("financial_statements")
    status = "incomplete" if blocking else "partial" if missing else "complete"
    # The two broker-screen axes: what the company is worth against what the brokers
    # think it is worth, and who is buying it right now.
    quality_components = {}
    analyst_score, analyst_evidence = analyst_target_component(consensus)
    if analyst_score is not None:
        quality_components["analyst_target"] = analyst_score
    demand_score = institutional_demand_component(supply)
    if demand_score is not None:
        quality_components["institutional_demand"] = demand_score
    fundamental = axis_reading({**components, **quality_components}, FUNDAMENTAL_AXIS)
    demand_axis = axis_reading({**components, **quality_components}, DEMAND_AXIS)
    grade = None if fund else quality_grade(fundamental, supply)
    return {"model":"uncalibrated_multifactor_scenarios_v1","generated_at":at.isoformat(),
        "fundamental_score":fundamental["score"],"fundamental_coverage":fundamental["coverage"],
        "demand_score":demand_axis["score"],"demand_coverage":demand_axis["coverage"],
        "axes":{"fundamental":fundamental,"demand":demand_axis},
        "analyst_target":analyst_evidence,
        "target_upside_pct":(analyst_evidence or {}).get("upside_pct"),
        "analyst_consensus":consensus or {"status":"unavailable"},
        "institutional_flow":supply or {"status":"unavailable"},
        "quality_grade":grade, "quality_axis_min":QUALITY_FUNDAMENTAL_SCORE,
        "instrument":"listed_fund" if fund else "operating_company",
        **({"assessment_basis":"fund_price_demand_sector_events"} if fund else {}),
        "horizon_sessions":5,"data_coverage":round(coverage,4),
        "components":components,"weights":weights,"evidence":evidence,"priced_in_fraction":round(priced_in,4),
        "catalyst_context":catalyst,
        "assessment_status":status,
        "score_withheld_reasons":sorted(set(blocking)),
        "caveats":[gap for gap in missing if gap not in blocking],
        "missing_dimensions":missing,
        "composite_score":round(max(0,min(100,50+50*after_pricing)),1) if not blocking else None,
        "volatility_source":volatility_source,"direction_score":round(composite,6),
        "scenario_sigma_multiple":round(sigma_multiple,4),
        "min_base_reward_pct":MIN_BASE_REWARD_PCT,
        "base_return_pct":round(drift*100,4),"bear_price":round(max(price*.01,price*(1+drift-width)),4),
        "base_price":round(price*(1+drift),4),"bull_price":round(price*(1+drift+width),4),
        "interpretation":"positive scenario bias" if drift>0 else "negative scenario bias" if drift<0 else "insufficient direction evidence",
        "validation":"out_of_sample_accuracy_not_established"}
