
import json
from pathlib import Path
from datetime import datetime, timezone

import yfinance as yf

symbols = {
    "KR": ["005930.KS", "000660.KS", "069500.KS"],
    "US": ["AAPL", "NVDA", "SOXL", "SOXS", "QQQ"]
}

results = []
errors = []

for market, tickers in symbols.items():
    for ticker in tickers:
        try:
            data = yf.download(
                ticker,
                period="3mo",
                interval="1d",
                progress=False,
                auto_adjust=True
            )

            if data.empty:
                raise ValueError("가격 데이터 없음")

            close = data["Close"]
            volume = data["Volume"]

            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
                volume = volume.iloc[:, 0]

            if len(close) < 21:
                raise ValueError("거래일 데이터 부족")

            price = float(close.iloc[-1])
            ma20 = float(close.tail(20).mean())
            avg_volume = float(
                volume.iloc[-21:-1].mean()
            )
            vol_ratio = (
                float(volume.iloc[-1]) / avg_volume
                if avg_volume > 0 else 0
            )

            status = (
                "상승 관찰"
                if price > ma20 and vol_ratio >= 1.2
                else "조건 미충족"
            )

            result = {
                "market": market,
                "symbol": ticker,
                "price": round(price, 2),
                "volume_ratio": round(vol_ratio, 2),
                "status": status
            }
            results.append(result)
            print(result)

        except Exception as e:
            errors.append({
                "symbol": ticker,
                "error": str(e)
            })
            print(f"ERROR {ticker}: {e}")

Path("reports").mkdir(exist_ok=True)

report = {
    "generated_at": datetime.now(
        timezone.utc
    ).isoformat(),
    "attempted": sum(len(v) for v in symbols.values()),
    "successful": len(results),
    "failed": len(errors),
    "results": results,
    "errors": errors
}

with open(
    "reports/latest.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print(
    f"SCAN COMPLETE: {len(results)} success, "
    f"{len(errors)} failed"
)

