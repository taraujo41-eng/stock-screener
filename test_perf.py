import time
from data_fetcher import get_unofficial_client
import pandas as pd
import requests
from datetime import datetime
import pytz

wb_un = get_unofficial_client()

t0 = time.time()
df1 = wb_un.get_bars("AAPL", interval="m15", count=600, extendTrading=1)
t1 = time.time()

tId = wb_un.get_ticker("AAPL")
params = {'extendTrading': 1}
headers = wb_un.build_req_headers()

t2 = time.time()
resp = requests.get(
    wb_un._urls.bars(tId, "m15", 600, int(time.time())),
    params=params,
    headers=headers,
    timeout=wb_un.timeout
)
result = resp.json()
time_zone = pytz.timezone(result[0]['timeZone'])
records = []
for row in result[0]['data']:
    parts = row.split(',')
    parts = ['0' if v == 'null' else v for v in parts]
    dt = datetime.fromtimestamp(int(parts[0])).astimezone(time_zone)
    records.append({
        "Date": dt,
        "Open": float(parts[1]),
        "High": float(parts[3]),
        "Low": float(parts[4]),
        "Close": float(parts[2]),
        "Volume": int(float(parts[6]))
    })
df2 = pd.DataFrame(records).set_index("Date").sort_index()
t3 = time.time()

print(f"Original SDK: {t1-t0:.4f}s")
print(f"Optimized:    {t3-t2:.4f}s")
