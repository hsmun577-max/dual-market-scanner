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
        if not name or any(word in name for word in ('대형','중형','소형','종합','제조업','KOSPI','KOSDAQ','코스피','코스닥','벤처','우량','중견','신성장')): continue
        rows.append(dict(code=code,name=name,exchange='KSP' if code[0]=='0' else 'KSQ'))
    return rows


class Client:
    def __init__(self):
        key=os.environ['KIS_APP_KEY'].strip(); secret=os.environ['KIS_APP_SECRET'].strip()
        response=requests.post(BASE+'/oauth2/tokenP',json=dict(grant_type='client_credentials',appkey=key,appsecret=secret),timeout=8)
        if response.status_code!=200: raise RuntimeError('KIS authorization unavailable')
        token=response.json().get('access_token')
        if not token: raise RuntimeError('KIS authorization response missing token')
        self.headers=dict(authorization='Bearer '+token,appkey=key,appsecret=secret,custtype='P')
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


def format_report(data):
    day=data['business_date']; label=f'{day[:4]}-{day[4:6]}-{day[6:]}'
    lines=[f'주요 섹터 수급 · {label}',f"조회 {datetime.fromisoformat(data['observed_at']):%H:%M} KST · 당일 누적·잠정",
           '외국인+기관 순매수 기준 · 업종별 중복 합산 금지']
    for exchange,title in [('KSP','코스피'),('KSQ','코스닥')]:
        rows=[row for row in data['rows'] if row['exchange']==exchange]
        selected=sorted(rows,key=lambda row:(-row['combined_net'],-row['return_pct']))[:3]
        lines.append(''); lines.append(title)
        if not selected: lines.append('수급 자료 미확보 · 유입 없음으로 판단하지 않음')
        for rank,row in enumerate(selected,1):
            state='순매수 유입' if row['combined_net']>0 else '순매도 우위' if row['combined_net']<0 else '순매수 중립'
            groups=f"외인 {'순매수' if row['foreign_net']>0 else '순매도' if row['foreign_net']<0 else '중립'} / 기관 {'순매수' if row['institution_net']>0 else '순매도' if row['institution_net']<0 else '중립'}"
            extra=[]
            if row['market_excess_pct'] is not None: extra.append(f"시장대비 {row['market_excess_pct']:+.1f}%p")
            if row['turnover_share_pct'] is not None: extra.append(f"거래대금 비중 {row['turnover_share_pct']:.1f}%")
            if row['advances'] is not None and row['declines'] is not None: extra.append(f"상승 {row['advances']:.0f} / 하락 {row['declines']:.0f}종목")
            lines.extend([f"{rank}. {row['name']} · {state} · {row['return_pct']:+.1f}%",groups,' · '.join(extra)])
    lines.append('\n지속·가속 여부는 이번 단일 조회로 확정하지 않음')
    return '\n'.join(lines)
