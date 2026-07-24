import time
from data_fetcher import get_unofficial_client

wb_un = get_unofficial_client()
t0 = time.time()
chain = wb_un.get_options(stock="TSLA")
t1 = time.time()
print(f"Time to fetch TSLA options: {t1-t0:.4f}s")
