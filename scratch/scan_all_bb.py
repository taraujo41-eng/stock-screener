import os
import sys
import time
from dotenv import load_dotenv

# Load parent directory to allow imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from reversal_scanner import get_us_tickers
from data_fetcher import fetch_batch_concurrent
import indicator_bot

print("Fetching US tickers...")
tickers = sorted(list(get_us_tickers()))
print(f"Total tickers: {len(tickers)}")

# Temporarily patch fetch_batch_concurrent to force skip_webull=True for this fast test
original_fetch = fetch_batch_concurrent
def patched_fetch(*args, **kwargs):
    kwargs['skip_webull'] = True
    return original_fetch(*args, **kwargs)

indicator_bot.fetch_batch_concurrent = patched_fetch

print("\nStep 1: Pre-calculating Daily Bollinger Bands...")
indicator_bot.precalculate_daily_bands(tickers)

print("\nStep 2: Fetching 15m candles and evaluating...")
results = patched_fetch(
    tickers=tickers,
    days=15,
    max_workers=30,
    interval="15m",
    includePrePost="false",
    process_fn=indicator_bot.evaluate_ticker_process
)

print("\n" + "="*70)
print("🎯 MATCHING TICKERS (15m Close <= Daily Lower BB or >= Daily Upper BB)")
print("="*70)

bullish = []
bearish = []

for ticker, res in results.items():
    if res:
        info = f"{ticker}: Close ${res['price']:.2f}"
        if res['action'] == 'BUY':
            bullish.append(info)
        else:
            bearish.append(info)

print(f"\n🟢 Bullish Matches ({len(bullish)} found):")
for item in sorted(bullish):
    print(f"  • {item}")

print(f"\n🔴 Bearish Matches ({len(bearish)} found):")
for item in sorted(bearish):
    print(f"  • {item}")
print("="*70)
