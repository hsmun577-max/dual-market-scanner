"""Official KIS industry investor flows. No invented net-flow units or ETF proxy claims."""
from datetime import datetime
from io import BytesIO
from zipfile import ZipFile
from zoneinfo import ZoneInfo
import math
import os
import time
import requests

BASE='https://openapi.koreainvestment.com:9443'


def number(value):
    try:
        if isinstance(value,bool): return None
        value=float(str(value).replace(',',''))
        return value if math.isfinite(value) else None
    except (ValueError,TypeError): return None


def industries(content):
    rows=[]
    for line in content.splitlines():
        code=line[1:5].decode('ascii',errors='ignore')
        name=line[5:45].decode('cp949',errors='replace').strip()
        if not code.isdigit() or code[0] not in '01' or not 5<=int(code[1:])<=99: continue
        if (not name or name in {'제조','제조업'} or any(word in name for word in (
                '대형','중형','소형','종합','KOSPI','KOSDAQ','코스피','코스닥','벤처','우량','중견','신성장','기술성장'))): continue
        rows.append(dict(code=code,name=name,exchange='KSP' if code[0]=='0' else 'KSQ'))
    return rows


class Client:
    _sessions = {}
    def __init__(self):
        key=os.environ['KIS_APP_KEY'].strip(); secret=os.environ['KIS_APP_SECRET'].strip()
        cached=self._sessions.get((key,secret))
        if cached and cached[0]>time.monotonic():
            self.headers=dict(cached[1]); return
        response=requests.post(BASE+'/oauth2/tokenP',json=dict(grant_type='client_credentials',appkey=key,appsecret=secret),timeout=8)
        if response.status_code!=200: raise RuntimeError('KIS authorization unavailable')
        token=response.json().get('access_token')
        if not token: raise RuntimeError('KIS authorization response missing token')
        self.headers=dict(authorization='Bearer '+token,appkey=key,appsecret=secret,custtype='P')
        self._sessions[(key,secret)]=(time.monotonic()+1800,dict(self.headers))
    def get(self,path,tr_id,params):
        time.sleep(.15)
        r=requests.get(BASE+path,headers=dict(self.headers,tr_id=tr_id),params=params,timeout=5)
        if r.status_code!=200: return None
        body=r.json()
        return body.get('output') if body.get('rt_cd')=='0' else None
    def price(self,code):
        return self.get('/uapi/domestic-stock/v1/quotations/inquire-index-price','FHPUP02100000',
                        dict(FID_COND_MRKT_DIV_CODE='U',FID_INPUT_ISCD=code))
    def flow(self,code,exchange,day):
        output=self.get('/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market','FHPTJ04040000',
            dict(FID_COND_MRKT_DIV_CODE='U',FID_INPUT_ISCD=code,FID_INPUT_DATE_1=day,
                 FID_INPUT_ISCD_1=exchange,FID_INPUT_DATE_2=day,FID_INPUT_ISCD_2=code))
        if isinstance(output,dict): output=[output]
        return next((row for row in output or [] if row.get('stck_bsop_date')==day),None)


def snapshot(day=None):
    now=datetime.now(ZoneInfo('Asia/Seoul')); day=day or now.strftime('%Y%m%d')
    r=requests.get('https://new.real.download.dws.co.kr/common/master/idxcode.mst.zip',timeout=15)
    r.raise_for_status()
    with ZipFile(BytesIO(r.content)) as archive:
        entry=next(name for name in archive.namelist() if name.endswith('idxcode.mst'))
        sectors=industries(archive.read(entry))
    client=Client(); benchmarks={ex:client.price(code) for ex,code in [('KSP','0001'),('KSQ','1001')]}
    rows=[]; missing=[]
    for sector in sectors:
        try:
            flow=client.flow(sector['code'],sector['exchange'],day)
            price=client.price(sector['code'])
            if not flow or not isinstance(price,dict): missing.append(sector); continue
            foreign=number(flow.get('frgn_ntby_tr_pbmn')); institution=number(flow.get('orgn_ntby_tr_pbmn'))
            change=number(price.get('bstp_nmix_prdy_ctrt'))
            benchmark=benchmarks.get(sector['exchange']) or {}
            market_change=number(benchmark.get('bstp_nmix_prdy_ctrt'))
            turnover=number(price.get('acml_tr_pbmn')); market_turnover=number(benchmark.get('acml_tr_pbmn'))
            if any(value is None for value in (foreign,institution,change)):
                missing.append(sector); continue
            rows.append(dict(**sector,business_date=day,observed_at=now.isoformat(),
                foreign_net=foreign,institution_net=institution,combined_net=foreign+institution,
                return_pct=change,market_excess_pct=change-market_change if market_change is not None else None,
                turnover_share_pct=100*turnover/market_turnover if turnover is not None and market_turnover and market_turnover>0 else None,
                advances=number(price.get('ascn_issu_cnt')),declines=number(price.get('down_issu_cnt')),
                net_unit='provider_raw_not_displayed',source='KIS industry investor daily API'))
        except (requests.RequestException,ValueError,TypeError): missing.append(sector)
    return dict(business_date=day,observed_at=datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),
                rows=rows,missing=missing,criteria=dict(net_flow=True,relative_strength=True,
                turnover_share=True,breadth_counts=True,persistence=False,twenty_day_intensity=False))


def rank_leading_sectors(data, limit=2):
    """Our observable leadership ranking; not a reproduction of a broker's proprietary model."""
    rows=[]
    for original in data.get('rows',[]):
        row=dict(original)
        up,down=row.get('advances'),row.get('declines')
        if up is None or down is None or up+down<=0:
            continue
        row['breadth']=up/(up+down)
        required=('return_pct','market_excess_pct','turnover_share_pct','combined_net')
        if any(number(row.get(key)) is None for key in required):
            continue
        rows.append(row)
    def percentile(value,key,exchange):
        peers=[r[key] for r in rows if r['exchange']==exchange]
        return (sum(v<value for v in peers)+.5*sum(v==value for v in peers))/len(peers)
    for row in rows:
        row['leadership_score']=sum(weight*percentile(row[key],key,row['exchange'])
            for key,weight in [('market_excess_pct',.40),('turnover_share_pct',.25),('breadth',.20),('combined_net',.15)])
    # A sector with falling prices or predominantly declining constituents is not a leader.
    eligible=[r for r in rows if r['return_pct']>0 and r['market_excess_pct']>0 and r['breadth']>.5]
    ranked=sorted(eligible,key=lambda r:(-r['leadership_score'],-r['market_excess_pct'],r['name']))
    selected=[]; names=set()
    for row in ranked:
        if row['name'] in names: continue
        selected.append(row); names.add(row['name'])
        if len(selected)>=limit: break
    return selected


def format_report(data):
    selected=rank_leading_sectors(data)
    return '\n'.join(['오늘의 주요 섹터']+
        [f"{rank}위 {row['name']}" for rank,row in enumerate(selected,1)]+
        (['확인 불가'] if not selected else []))
