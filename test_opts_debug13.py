from data_fetcher import _fetch_yahoo_options_for_expiration
chain = _fetch_yahoo_options_for_expiration("NVDA", 1781755200)
for c in chain.get("calls", [])[:5]:
    print(c)
