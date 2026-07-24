import sys
sys.path.append("/Users/tonyaraujo/APP/stock-scanner")

from reversal_scanner import get_us_tickers, fetch_quotes
import requests

print("1. Fetching US tickers...")
tickers = get_us_tickers()
print(f"Total tickers: {len(tickers)}")

print("\n2. Fetching quotes for first 5 tickers...")
first_5 = list(tickers)[:5]
quotes = fetch_quotes(first_5)
print(f"Quotes: {quotes}")

print("\n3. Testing single fetch for AAPL...")
from data_fetcher import fetch_one
df = fetch_one("AAPL", days=30)
if df is not None:
    print(f"AAPL Close: {df['Close'].iloc[-1]} (Rows: {len(df)})")
else:
    print("AAPL Fetch failed!")
