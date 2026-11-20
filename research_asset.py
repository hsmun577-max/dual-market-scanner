"""Separate listed fund scans from issuer earnings analysis."""
import re

def is_fund(row):
    kind=str(row.get('asset_type') or '').lower()
    if kind in {'etf','etn','fund'}:return True
    if row.get('company_analysis',{}).get('quote_type') in {'ETF','MUTUALFUND'}:return True
    return bool(re.match(r'^(?:KODEX|TIGER|ACE|RISE|KBSTAR|ARIRANG|HANARO|SOL|PLUS|KOSEF|KIWOOM|TIMEFOLIO|1Q|WON)\s',str(row.get('name') or '')))
