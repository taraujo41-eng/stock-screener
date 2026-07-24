from data_fetcher import _fetch_yahoo_options_chain, _fetch_yahoo_options_for_expiration
import time
chain_meta = _fetch_yahoo_options_chain("NVDA")
for exp in chain_meta.get("expirations", []):
    dte = (exp - time.time()) / 86400
    if 20 <= dte <= 60:
        print(f"Fetching Yahoo DTE {dte:.0f} (exp {exp})")
        chain = _fetch_yahoo_options_for_expiration("NVDA", exp)
        for c in chain.get("calls", [])[:2]:
            print(c)
        break
