from data_fetcher import fetch_one
from reversal_scanner import _analyze_options_setup

import warnings
warnings.filterwarnings('ignore')

sym = "NVDA"
df = fetch_one(sym, days=280, interval="1d", includePrePost="false")
last_price = float(df['Close'].iloc[-1])

from data_fetcher import _fetch_yahoo_options_for_expiration
import time
chain = _fetch_yahoo_options_for_expiration(sym, 1781755200) # One of the valid exps
found = False
for c in chain.get("calls", []):
    strike = c.get("strike", 0)
    vol = c.get("volume", 0) or 0
    oi = c.get("openInterest", 0) or 0
    bid = c.get("bid", 0) or 0
    ask = c.get("ask", 0) or 0
    mid = (bid + ask) / 2
    spread_pct = ((ask - bid) / mid) * 100 if mid > 0 else 999
    dist_pct = (strike - last_price) / last_price
    if -0.07 <= dist_pct <= 0.05 and vol >= 50 and oi >= 100:
        print(f"Strike {strike} Vol {vol} OI {oi} Bid {bid} Ask {ask} Spread {spread_pct:.1f}%")
        found = True

if not found: print("No contracts found")
