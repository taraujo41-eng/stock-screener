from data_fetcher import fetch_options_chain
import time

chain_meta = fetch_options_chain("NVDA")
all_chains = chain_meta.get("allChains", {})
found = 0
for exp_ts, chain in all_chains.items():
    for c in chain.get("calls", []):
        if found < 5:
            print(c)
            found += 1
