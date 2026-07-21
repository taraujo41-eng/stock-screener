import time
import json
from reversal_scanner import options_watchlist_scan

print("="*60)
print("  TESTING OPTIONS WATCHLIST SCAN")
print("="*60)

test_tickers = ["NVDA", "AAPL", "AMD", "TSLA", "MSFT"]
t0 = time.time()
df_res = options_watchlist_scan(watchlist=test_tickers, extended_hours=False)
t1 = time.time()

print(f"\nCompleted scan in {t1-t0:.2f}s")
if df_res.empty:
    print("No options plays matched strict criteria for test tickers.")
else:
    print(f"Found {len(df_res)} options setup(s):")
    results = df_res.to_dict(orient="records")
    for r in results:
        print(f"\n  Ticker: {r.get('Ticker')} | Direction: {r.get('Direction')} | Catalyst Score: {r.get('Catalyst Score')}")
        print(f"  Contract: {r.get('Contract')} | Mid: ${r.get('Mid')} | IV: {r.get('IV')}% | DTE: {r.get('DTE')}d")
        print(f"  Unusual Flow: {r.get('Unusual Flow')} | Tags: {r.get('Catalyst Tags')}")
