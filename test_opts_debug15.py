from data_fetcher import _fetch_yahoo_options_chain
chain_meta = _fetch_yahoo_options_chain("NVDA")
print(chain_meta.get("expirations", [])[:5])
