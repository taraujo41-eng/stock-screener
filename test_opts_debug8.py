from data_fetcher import fetch_options_chain
import time

chain_meta = fetch_options_chain("NVDA")
last_price = 214.25
for exp_ts, chain in chain_meta.get("allChains", {}).items():
    dte = (exp_ts - time.time()) / 86400
    if not (20 <= dte <= 60): continue
    for c in chain.get("calls", []):
        strike = c.get("strike", 0)
        dist_pct = (strike - last_price) / last_price
        if -0.07 <= dist_pct <= 0.05:
            print(f"DTE {dte:.0f} Strike {strike} Vol {c.get('volume')} OI {c.get('openInterest')} Bid {c.get('bid')} Ask {c.get('ask')} IV {c.get('impliedVolatility')}")
