from data_fetcher import fetch_one
from reversal_scanner import _analyze_options_setup

import warnings
warnings.filterwarnings('ignore')

sym = "NVDA"
df = fetch_one(sym, days=280, interval="1d", includePrePost="false")

# Patch the detect_unusual_options logic locally so it returns True just to see what contract it picks
import reversal_scanner
original_detect = reversal_scanner.detect_unusual_options
reversal_scanner.detect_unusual_options = lambda s: (True, False, "TEST")

res = _analyze_options_setup(sym, df, iv_history={})
print(f"Result for {sym}: {res}")
