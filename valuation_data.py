"""Observed valuation fallbacks. Peer multiples are relative pricing, not future earnings."""
from datetime import datetime,timedelta,timezone
import math
import os
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
            for source,target in [('per','trailing_pe'),('pbr','price_to_book'),
                                  ('eps','trailing_eps'),('bps','book_value_per_share'),('aspr_unit','price_tick')]:
                value=finite(row.get(source))
                if value is not None: values[target]=value
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
            for key,row in companies.items():
                if key==symbol or row.get('source_symbol')==company.get('source_symbol') and row.get('source_symbol'):
                    continue
                if tuple(row.get(k) for k in ('market','country','industry'))!=group or not usable(row):
                    continue
                value=finite(row.get(field))
                identity=row.get('source_symbol') or key
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
