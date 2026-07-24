from data_fetcher import get_unofficial_client
wb_un = get_unofficial_client()
chain = wb_un.get_options("NVDA", expireDate="2026-05-29")
for c in chain:
    if c['strikePrice'] == '220':
        print(c)
