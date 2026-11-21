"""Observed valuation fallbacks. Peer multiples are relative pricing, not future earnings."""
from datetime import datetime,timedelta,timezone
import math
import os
import re
from statistics import median

def finite(value):
    try:
        result=float(str(value).replace(',',''))
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):
        return None

def domestic_valuations(symbols, client=None):
    if client is None and not (os.getenv('KIS_APP_KEY') and os.getenv('KIS_APP_SECRET')):
        return {}
    try:
        if client is None:
            from industry_flow import Client
            client=Client()
    except Exception:
        return {}
    result={}
    for symbol in dict.fromkeys(symbols):
        try:
            row=client.get('/uapi/domestic-stock/v1/quotations/inquire-price','FHKST01010100',
                {'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':symbol})
            if not isinstance(row,dict): continue
            observed=datetime.now(timezone.utc).isoformat()
            values={'valuation_source':'KIS inquire-price','valuation_observed_at':observed}
            if row.get('bstp_kor_isnm'):
                values['domestic_industry_name']=row['bstp_kor_isnm'].strip()
            for source,target in [('per','trailing_pe'),('pbr','price_to_book'),
                                  ('eps','trailing_eps'),('bps','book_value_per_share'),('aspr_unit','price_tick')]:
                value=finite(row.get(source))
                if value is not None:
                    values[target]=value
                    if target in ('trailing_pe','price_to_book'):
                        # Per-field source: a Yahoo forward PE must not be divided by this one.
                        values[target+'_source']='KIS inquire-price'
            result[symbol]=values
        except Exception:
            continue
    return result

def apply_peer_valuations(companies, at):
    """Require three other issuers in the same market/country/industry, dated within a day."""
    def usable(row):
        try:
            seen=datetime.fromisoformat(row.get('valuation_observed_at',''))
            return seen.utcoffset() is not None and at-timedelta(hours=24)<=seen<=at
        except (ValueError,TypeError): return False
    for symbol,company in companies.items():
        company.pop('peer_valuation',None)
        group=tuple(company.get(k) for k in ('market','country','industry'))
        if not all(group) or not usable(company): continue
        for field in ('trailing_pe','price_to_book'):
            own=finite(company.get(field))
            if own is None or own<=0: continue
            peers=[]; seen_peers=set()
            own_name=str(company.get('company_name') or '').strip().casefold()
            for key,row in companies.items():
                if key==symbol or row.get('source_symbol')==company.get('source_symbol') and row.get('source_symbol'):
                    continue
                if tuple(row.get(k) for k in ('market','country','industry'))!=group or not usable(row):
                    continue
                value=finite(row.get(field))
                name=str(row.get('company_name') or '').strip().casefold()
                if own_name and name==own_name:continue
                identity=name or row.get('source_symbol') or key
                if value is not None and value>0 and identity not in seen_peers:
                    peers.append((key,value)); seen_peers.add(identity)
            if len(peers)<3: continue
            reference=median(value for _,value in peers)
            company['peer_valuation']=dict(metric=field,own_multiple=own,peer_median=reference,
                peer_count=len(peers),peer_symbols=[key for key,_ in peers],industry=group[2],
                observed_at=at.isoformat(),score=math.tanh(math.log(reference/own)),
                interpretation='relative_peer_multiple_not_intrinsic_or_forward_value')
            break
    return companies


def expand_domestic_peer_valuations(companies, registry, at, provider=None, limit=48):
    """Fetch dated quotes for comparable issuers outside the technical candidate shortlist."""
    provider=provider or domestic_valuations
    apply_peer_valuations(companies,at)
    groups={tuple(c.get(k) for k in ('market','country','industry')) for c in companies.values()
        if c.get('market')=='KR' and not c.get('peer_valuation')
        and not (finite(c.get('trailing_pe')) and finite(c.get('forward_pe'))
                 and min(float(c['trailing_pe']),float(c['forward_pe']))>0)}
    groups={g for g in groups if all(g)}
    targets=[];profiles={};seen=set()
    own_symbols={c.get('source_symbol') or k for k,c in companies.items()}
    for group in sorted(groups):
        picked=0
        for symbol,row in sorted(registry.items()):
            if not re.fullmatch(r'\d{6}\.(KS|KQ)',symbol) or symbol in own_symbols:continue
            if ('KR',row.get('country'),row.get('industry'))!=group:continue
            try:dated=datetime.fromisoformat(row.get('profile_available_at') or row.get('available_at') or '')
            except (TypeError,ValueError):continue
            if dated.utcoffset() is None or not at-timedelta(days=180)<=dated<=at:continue
            name=str(row.get('company_name') or '').strip().casefold()
            if not name or (group,name) in seen:continue
            seen.add((group,name));profiles[symbol]=dict(row,market='KR',source_symbol=symbol)
            targets.append(symbol);picked+=1
            if picked>=6 or len(targets)>=limit:break
        if len(targets)>=limit:break
    values=provider([s.split('.')[0] for s in targets]) if targets else {}
    extended=dict(companies);evaluation_at=at
    for symbol in targets:
        if symbol.split('.')[0] in values:
            value=values[symbol.split('.')[0]]
            try:seen=datetime.fromisoformat(value.get('valuation_observed_at',''))
            except (ValueError,TypeError):continue
            if seen.utcoffset() is None or not at-timedelta(hours=24)<=seen<=at+timedelta(minutes=2):continue
            evaluation_at=max(evaluation_at,seen)
            extended['peer:'+symbol]={**profiles[symbol],**value}
    apply_peer_valuations(extended,evaluation_at)
    for company in companies.values():
        company['peer_lookup']=dict(extra_issuers_requested=len(targets),extra_quotes_received=len(extended)-len(companies))
    return companies
