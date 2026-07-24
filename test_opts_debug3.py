from data_fetcher import fetch_one
from reversal_scanner import _analyze_options_setup, fetch_options_chain
import time

sym = "NVDA"
df = fetch_one(sym, days=280, interval="1d", includePrePost="false")
chain_meta = fetch_options_chain(sym)

last_price = float(df['Close'].iloc[-1])
print(f"NVDA Last Price: {last_price}")

all_chains = chain_meta.get("allChains", {})
found_any = False
for exp_ts, chain in all_chains.items():
    dte = (exp_ts - time.time()) / 86400
    if not (20 <= dte <= 60):
        continue
        
    for c in chain.get("calls", []):
        strike = c.get("strike", 0)
        vol = c.get("volume", 0) or 0
        oi = c.get("openInterest", 0) or 0
        bid = c.get("bid", 0) or 0
        ask = c.get("ask", 0) or 0
        
        dist_pct = (strike - last_price) / last_price
        if -0.07 <= dist_pct <= 0.05:
            mid = (bid + ask) / 2
            spread = ((ask - bid) / mid) * 100 if mid > 0 else 999
            if vol >= 50 and oi >= 100:
                print(f"Strike {strike} DTE {dte:.0f} - Vol {vol} OI {oi} Spread {spread:.1f}%")
                found_any = True

if not found_any:
    print("No contracts met basic Vol/OI/Dist criteria even without spread filter!")
