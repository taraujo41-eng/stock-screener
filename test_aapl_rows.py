import sys
sys.path.append("/Users/tonyaraujo/APP/stock-scanner")

from data_fetcher import fetch_one

df = fetch_one("AAPL", days=10)
print(df.tail(5))
