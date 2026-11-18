"""Explainable five-session scenarios from financial, price and event evidence.

These are uncalibrated scenarios. Data coverage is not a probability of profit.
"""

import math
from datetime import datetime, timedelta


def _number(mapping, key):
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


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


def build_company_forecast(company, indicators, price, at, events=(), sectors=()):
    """Combine available evidence without treating missing observations as neutral."""
    if at.utcoffset() is None or not math.isfinite(price) or price <= 0:
        raise ValueError("Forecast requires a dated positive price")
    weights = {"growth": .24, "earnings_quality": .18, "balance_sheet": .12,
               "valuation_outlook": .10, "price_and_demand": .14,
               "sector_pressure": .10, "events": .12}
    components = {}
    evidence = {}
    margin = _number(company, "operating_margin_pct")
    growth = _number(company, "revenue_yoy_pct")
    margin_change = _number(company, "operating_margin_yoy_pp")
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
    if free_cash_margin is not None:
        quality.append((.4, math.tanh(free_cash_margin/10)))
    if conversion is not None:
        quality.append((.2, math.tanh(conversion-1)))
    if quality:
        components["earnings_quality"] = sum(w*v for w,v in quality)/sum(w for w,_ in quality)
        evidence["earnings_quality"] = {"operating_margin_pct": margin,
            "free_cash_flow_margin_pct": free_cash_margin, "operating_cash_conversion": conversion}
    debt = _number(company, "net_debt_to_equity")
    if debt is not None:
        components["balance_sheet"] = -math.tanh(debt)
        evidence["balance_sheet"] = {"net_debt_to_equity": debt}
    else:
        liabilities = _number(company,"liabilities_to_equity")
        if liabilities is not None:
            components["balance_sheet"] = -math.tanh(liabilities-1)
            evidence["balance_sheet"] = {"liabilities_to_equity":liabilities}
    trailing = _number(company, "trailing_pe")
    forward = _number(company, "forward_pe")
    if trailing is not None and forward is not None and min(trailing,forward)>0:
        components["valuation_outlook"] = math.tanh(math.log(trailing/forward))
        evidence["valuation_outlook"] = {"trailing_pe":trailing,"forward_pe":forward,
                                        "meaning":"earnings outlook at current pricing, not peer fair value"}
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
    sector_names = {"Technology":{"기술","반도체","AI반도체","IT·전자부품"},
        "Financial Services":{"금융","은행","증권"},"Healthcare":{"헬스케어","바이오"},
        "Industrials":{"산업재","운송","조선","방산"},"Energy":{"에너지"},
        "Basic Materials":{"소재","철강"},"Utilities":{"유틸리티","AI전력설비"},
        "Consumer Defensive":{"필수소비재"},"Consumer Cyclical":{"경기소비재","자동차"},
        "Real Estate":{"부동산"},"Communication Services":{"커뮤니케이션"}}
    matched = []
    for row in sectors:
        if company.get("market") and row.get("market") != company["market"]:
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
    if matched and market_return is not None:
        components["sector_pressure"] = .5*sum(matched)/len(matched)+.5*math.tanh(market_return/.1)
        evidence["sector_pressure"] = {"sector":company.get("sector"),"matching_proxies":len(matched)}
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
    if event_terms or feeds_checked:
        components["events"] = math.tanh(sum(event_terms))
    evidence["events"] = {"quantified_events":len(event_terms),"unquantified_events":unquantified_events,
                          "feeds_checked":feeds_checked,"uncertainty":uncertainty}
    coverage = sum(weights[key] for key in components)
    composite = sum(weights[key]*value for key,value in components.items())/coverage if coverage else 0.
    ma20 = _number(indicators,"ma20")
    extension = max(0,price/ma20-1) if ma20 is not None and ma20>0 else 0.
    # A rise that has already occurred reduces the remaining event/earnings scenario.
    priced_in = min(.8, max(0,(momentum or 0)-.05)/.15 + max(0,extension-.03)/.12)
    daily_volatility = _number(indicators,"volatility_20d")
    atr = _number(indicators,"atr14_pct")
    volatility_source = "observed_daily_returns"
    if daily_volatility is None or daily_volatility<=0:
        daily_volatility = atr/140 if atr is not None and atr>0 else .025
        volatility_source = "ATR_proxy" if atr is not None and atr>0 else "assumed_2.5pct_daily"
    scale = max(.003,daily_volatility)*math.sqrt(5)
    after_pricing = composite*(1-priced_in)-.15*priced_in-.1*uncertainty
    drift = .6*scale*after_pricing
    width = scale*(1+(1-coverage)*.7+uncertainty*.5+priced_in*.3)
    missing = [key for key in weights if key not in components]
    if financial_period_missing(company):
        missing.append("latest_report_not_in_financials")
    if free_cash_margin is None and conversion is None:
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
    complete = not missing
    return {"model":"uncalibrated_multifactor_scenarios_v1","generated_at":at.isoformat(),
        "horizon_sessions":5,"data_coverage":round(coverage,4),
        "components":components,"weights":weights,"evidence":evidence,"priced_in_fraction":round(priced_in,4),
        "assessment_status":"complete" if complete else "incomplete",
        "missing_dimensions":missing,
        "composite_score":round(max(0,min(100,50+50*after_pricing)),1) if complete else None,
        "volatility_source":volatility_source,"direction_score":round(composite,6),
        "base_return_pct":round(drift*100,4),"bear_price":round(max(price*.01,price*(1+drift-width)),4),
        "base_price":round(price*(1+drift),4),"bull_price":round(price*(1+drift+width),4),
        "interpretation":"positive scenario bias" if drift>0 else "negative scenario bias" if drift<0 else "insufficient direction evidence",
        "validation":"out_of_sample_accuracy_not_established"}
