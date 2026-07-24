from data_fetcher import get_unofficial_client
wb_un = get_unofficial_client()
dates = wb_un.get_options_expiration_dates("NVDA")
for d in dates:
    if d['days'] >= 20:
        print(f"Checking exp {d['date']} (DTE {d['days']})")
        chain = wb_un.get_options("NVDA", expireDate=d['date'])
        for c in chain:
            if c['strikePrice'] == '220':
                print(c)
                break
        break
