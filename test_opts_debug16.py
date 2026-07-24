from data_fetcher import fetch_one
from reversal_scanner import _analyze_options_setup

import warnings
warnings.filterwarnings('ignore')

sym = "NVDA"
df = fetch_one(sym, days=280, interval="1d", includePrePost="false")

import reversal_scanner
# Bypass technical requirements so it processes options
original_detect = reversal_scanner.detect_unusual_options
reversal_scanner.detect_unusual_options = lambda s: (True, False, "TEST")

# Patch reversal_scanner._analyze_options_setup inline
def patched_analyze(sym, df, iv_history, rsi_bull_thresh=35, rsi_bear_thresh=65):
    from data_fetcher import fetch_options_chain, _fetch_yahoo_options_chain, _fetch_yahoo_options_for_expiration
    import time
    
    chain_meta = fetch_options_chain(sym)
    if not chain_meta: return None
    
    last_price = float(df['Close'].iloc[-1])
    direction = "bullish"
    side = "calls"
    
    valid_exps = []
    now = time.time()
    for exp in chain_meta.get("expirations", []):
        dte = (exp - now) / 86400
        if 20 <= dte <= 60:
            valid_exps.append(exp)
            
    for exp_ts in valid_exps:
        chain = chain_meta.get("allChains", {}).get(exp_ts)
        has_data = False
        if chain:
            for c in chain.get("calls", [])[:10]:
                if c.get("bid") is not None or c.get("ask") is not None:
                    has_data = True
                    break
        if not chain or not has_data:
            print(f"Webull missing data for exp {exp_ts}, falling back to Yahoo...")
            if "yahoo_meta" not in chain_meta:
                chain_meta["yahoo_meta"] = _fetch_yahoo_options_chain(sym)
                
            yahoo_meta = chain_meta.get("yahoo_meta")
            if not yahoo_meta: continue
            
            closest_yahoo_exp = None
            min_diff = 999999
            for y_exp in yahoo_meta.get("expirations", []):
                diff = abs(y_exp - exp_ts)
                if diff < min_diff:
                    min_diff = diff
                    closest_yahoo_exp = y_exp
                    
            if closest_yahoo_exp and min_diff < 86400 * 4: # within 4 days
                print(f"Matched Webull {exp_ts} -> Yahoo {closest_yahoo_exp}")
                chain = _fetch_yahoo_options_for_expiration(sym, closest_yahoo_exp)
            
        if not chain: continue
        contracts = chain.get(side, [])
        for c in contracts:
            strike = c.get("strike", 0)
            vol = c.get("volume", 0) or 0
            oi = c.get("openInterest", 0) or 0
            bid = c.get("bid", 0) or 0
            ask = c.get("ask", 0) or 0
            if vol >= 50 and oi >= 100:
                mid = (bid + ask) / 2
                if mid > 0:
                    spread_pct = ((ask - bid) / mid) * 100
                    dist_pct = (strike - last_price) / last_price
                    if -0.07 <= dist_pct <= 0.05:
                        print(f"FOUND VALID: {c['contractSymbol']} Strike {strike} Vol {vol} OI {oi} Spread {spread_pct:.1f}%")
                        return True
    return False

res = patched_analyze(sym, df, iv_history={})
print(f"Result for {sym}: {res}")
