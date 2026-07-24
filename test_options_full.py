import sys
sys.path.append("/Users/tonyaraujo/APP/stock-scanner")

from reversal_scanner import options_full_market_scan
import time

print("Starting full market options scan...")
start = time.time()
df = options_full_market_scan()
elapsed = time.time() - start
print(f"Completed in {elapsed:.1f}s")

if df.empty:
    print("No options setups found.")
else:
    print(f"Found {len(df)} setups:")
    print(df.head(10).to_string(index=False))
