from data_fetcher import fetch_one
from reversal_scanner import _analyze_options_setup, fetch_options_chain

import warnings
warnings.filterwarnings('ignore')

sym = "NVDA"
df = fetch_one(sym, days=280, interval="1d", includePrePost="false")
chain_meta = fetch_options_chain(sym)

def debug_analyze():
    valid_exps = []
    import time
    now = time.time()
    for exp in chain_meta.get("expirations", []):
        dte = (exp - now) / 86400
        if 20 <= dte <= 60:
            valid_exps.append(exp)
            
    print(f"Valid exps (20-60 DTE): {len(valid_exps)}")
    
    last_price = float(df['Close'].iloc[-1])
    direction = "bullish"  # NVDA had Bull=3, Bear=3, let's just force bullish for test
    side = "calls"
    
    for exp_ts in valid_exps:
        chain = chain_meta.get("allChains", {}).get(exp_ts)
        if not chain:
            print(f"No chain for {exp_ts}")
            continue
            
        contracts = chain.get(side, [])
        print(f"Exp {exp_ts}: {len(contracts)} contracts")
        
        for c in contracts:
            strike = c.get("strike", 0)
            vol = c.get("volume", 0) or 0
            oi = c.get("openInterest", 0) or 0
            bid = c.get("bid", 0) or 0
            ask = c.get("ask", 0) or 0
            
            # Liquidity
            if vol < 50 or oi < 100:
                continue
                
            mid = (bid + ask) / 2
            if mid <= 0:
                continue
            spread_pct = ((ask - bid) / mid) * 100
            if spread_pct > 15:
                # print(f"Strike {strike}: Spread {spread_pct:.1f}% > 15%")
                continue
                
            dist_pct = (strike - last_price) / last_price
            if -0.07 <= dist_pct <= 0.05:
                print(f"FOUND VALID CONTRACT: Strike {strike}, Vol {vol}, OI {oi}, Spread {spread_pct:.1f}%, Dist {dist_pct*100:.1f}%")
                return True
    return False

debug_analyze()
