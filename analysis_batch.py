"""Preserve completed issuer analyses when an independent issuer fails."""
from concurrent.futures import ThreadPoolExecutor, as_completed

def collect_company_results(fetch, symbols, max_workers=4):
    completed={}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending={pool.submit(fetch,symbol):symbol for symbol in symbols}
        for future in as_completed(pending):
            symbol=pending[future]
            try:
                result=future.result()
                if result[0]!=symbol:raise ValueError('Issuer result mismatch')
                completed[symbol]=result
            except Exception as exc:
                completed[symbol]=(symbol,dict(analysis_status='failed',
                    analysis_error=type(exc).__name__,news_status='unavailable',filing_status='unavailable'),[])
            print(f'ISSUER_ANALYSIS_PROGRESS={symbol}:{completed[symbol][1].get("analysis_status","collected")}',flush=True)
    return [completed[symbol] for symbol in symbols]
