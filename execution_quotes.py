"""Read-only KIS quote + completed minute checks for a proposed entry."""
from datetime import datetime,timedelta
import math
import time
import requests
from industry_flow import Client,BASE

def raw_get(client,path,tr,params):
    time.sleep(.15)
    response=requests.get(BASE+path,headers=dict(client.headers,tr_id=tr),params=params,timeout=8)
    if response.status_code!=200:raise RuntimeError('KIS read request failed')
    body=response.json()
    if body.get('rt_cd')!='0':raise RuntimeError('KIS read response unavailable')
    return body

def _number(value):
    try:
        if isinstance(value, bool):
            return None
        result = float(str(value).replace(',', ''))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def parse_execution_quote(book,minutes,at):
    """Use provider time, not the local request time, to establish quote freshness."""
    orderbook=book.get('output1') or {};detail=book.get('output2') or {}
    market_code=orderbook.get('new_mkop_cls_code') or (detail.get('new_mkop_cls_code') if isinstance(detail,dict) else '')
    if str(market_code).strip()!='20':raise ValueError('Continuous trading not verified')
    if not isinstance(detail,dict) or str(detail.get('vi_cls_code','')).upper()!='N':
        raise ValueError('Volatility interruption status not verified normal')
    hour=str(orderbook.get('aspr_acpt_hour') or '')
    if len(hour)!=6 or not hour.isdigit():raise ValueError('Provider quote time unavailable')
    observed=datetime.combine(at.date(),datetime.strptime(hour,'%H%M%S').time(),tzinfo=at.tzinfo)
    completed=[];today=[];prior={}
    for row in minutes.get('output2') or []:
        try:
            minute=datetime.strptime(str(row['stck_bsop_date'])+str(row['stck_cntg_hour']),'%Y%m%d%H%M%S').replace(tzinfo=at.tzinfo)
        except (ValueError,TypeError,KeyError):continue
        volume=_number(row.get('cntg_vol'))
        if volume is None or volume<=0:continue
        if minute.date()==at.date():
            today.append((minute,volume,_number(row.get('stck_prpr')),_number(row.get('stck_oprc'))))
            if minute+timedelta(minutes=1)<=at:completed.append(minute)
        else:
            prior.setdefault(minute.date(),[]).append((minute,volume))
    if not completed:raise ValueError('No completed current-day traded minute')
    today.sort()
    # The session's own opening price and last trade: a catalyst entry is refused while
    # the stock trades under its open, so the quote has to carry the session's tone.
    day_open=next((opening for _minute,_volume,_price,opening in today if opening),None)
    last_price=next((price for _minute,_volume,price,_opening in reversed(today) if price),None)
    cumulative=sum(volume for _minute,volume,_price,_opening in today)
    cutoff=today[-1][0].time()
    baseline=[]
    for rows in prior.values():
        window=[volume for minute,volume in rows if minute.time()<=cutoff]
        # A partial prior session would understate the baseline and fake a volume surge.
        if len(window)<max(1,len(today)//2):continue
        baseline.append(sum(window))
    volume_ratio=cumulative/(sum(baseline)/len(baseline)) if baseline else None
    return dict(ask=float(orderbook['askp1']),bid=float(orderbook['bidp1']),
        ask_size=float(orderbook['askp_rsqn1']),bid_size=float(orderbook['bidp_rsqn1']),
        observed_at=observed.isoformat(),trade_at=max(completed).isoformat(),
        day_open=day_open,last_price=last_price,cumulative_volume=cumulative,
        volume_ratio=volume_ratio,
        source='KIS orderbook plus completed current-day minute',market_state='continuous',filled=False)

def execution_quote(symbol,at,client=None):
    client=client or Client();code=symbol.split('.')[0]
    minutes=raw_get(client,'/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice','FHKST03010200',
        dict(FID_COND_MRKT_DIV_CODE='J',FID_INPUT_ISCD=code,FID_INPUT_HOUR_1=at.strftime('%H%M%S'),
            FID_PW_DATA_INCU_YN='Y',FID_ETC_CLS_CODE=''))
    book=raw_get(client,'/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn','FHKST01010200',
        dict(FID_COND_MRKT_DIV_CODE='J',FID_INPUT_ISCD=code))
    return parse_execution_quote(book,minutes,datetime.now(at.tzinfo))
