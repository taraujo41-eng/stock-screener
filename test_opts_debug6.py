from data_fetcher import fetch_one
from reversal_scanner import _analyze_options_setup

import warnings
warnings.filterwarnings('ignore')

sym = "NVDA"
df = fetch_one(sym, days=280, interval="1d", includePrePost="false")
if df is not None:
    res = _analyze_options_setup(sym, df, iv_history={})
    print(f"Result for {sym}: {res}")
else:
    print("Failed to fetch df")
