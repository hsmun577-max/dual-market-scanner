"""Read-only KIS quote + completed minute checks for a proposed entry."""
from datetime import datetime,timedelta
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
    completed=[]
    for row in minutes.get('output2') or []:
        try:
            minute=datetime.strptime(str(row['stck_bsop_date'])+str(row['stck_cntg_hour']),'%Y%m%d%H%M%S').replace(tzinfo=at.tzinfo)
            if minute.date()==at.date() and minute+timedelta(minutes=1)<=at and float(row['cntg_vol'])>0:
                completed.append(minute)
        except (ValueError,TypeError,KeyError):continue
    if not completed:raise ValueError('No completed current-day traded minute')
    return dict(ask=float(orderbook['askp1']),bid=float(orderbook['bidp1']),
        ask_size=float(orderbook['askp_rsqn1']),bid_size=float(orderbook['bidp_rsqn1']),
        observed_at=observed.isoformat(),trade_at=max(completed).isoformat(),
        source='KIS orderbook plus completed current-day minute',market_state='continuous',filled=False)

def execution_quote(symbol,at,client=None):
    client=client or Client();code=symbol.split('.')[0]
    minutes=raw_get(client,'/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice','FHKST03010200',
        dict(FID_COND_MRKT_DIV_CODE='J',FID_INPUT_ISCD=code,FID_INPUT_HOUR_1=at.strftime('%H%M%S'),
            FID_PW_DATA_INCU_YN='Y',FID_ETC_CLS_CODE=''))
    book=raw_get(client,'/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn','FHKST01010200',
        dict(FID_COND_MRKT_DIV_CODE='J',FID_INPUT_ISCD=code))
    return parse_execution_quote(book,minutes,datetime.now(at.tzinfo))
