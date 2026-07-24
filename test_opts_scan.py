import time
from reversal_scanner import options_watchlist_scan
from app import WATCHLIST
t0 = time.time()
res = options_watchlist_scan(WATCHLIST, extended_hours=False)
t1 = time.time()
print(f"Time: {t1-t0:.2f}s")
