from data_fetcher import get_unofficial_client
import requests
import json
import time

wb_un = get_unofficial_client()
t0 = time.time()
headers = wb_un.build_req_headers()
data = {'count': -1, 'direction': 'all', 'tickerId': wb_un.get_ticker("NVDA")}
res = requests.post(wb_un._urls.options_exp_dat_new(), json=data, headers=headers, timeout=wb_un.timeout)
res_json = res.json()
t1 = time.time()

exps = res_json.get('expireDateList', [])
print(f"Time: {t1-t0:.4f}s, Expirations: {len(exps)}")
if exps:
    print(f"First exp date: {exps[0]['from']['date']}, items: {len(exps[0]['data'])}")
