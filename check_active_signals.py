import os
import sys
import time
from dotenv import load_dotenv

# Load parent directory to allow imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from data_fetcher import fetch_batch_concurrent
import indicator_bot

# Set up settings locally for check_active_signals
os.environ["BB_LENGTH"] = "20"
os.environ["BB_MULT"] = "3.0"
os.environ["RSI_LENGTH"] = "14"
os.environ["LOOKBACK"] = "15"
os.environ["CANDLE_INTERVAL_3SIGMA"] = "15m"

print("=" * 70)
print("🔍 DIAGNOSTIC: DAILY BB BANDS + 15m REGULAR HOURS STRATEGY")
print("=" * 70)

tickers = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "BRK-B", "UNH", "JNJ",
    "JPM", "XOM", "V", "PG", "AVGO", "COST", "AMD", "NFLX", "ADBE", "CRM",
    "QCOM", "TXN", "INTC", "CSCO", "AMGN", "HON", "SBUX", "DIS", "HD", "NKE",
    "MRK", "PEP", "KO", "PM", "PFE", "WMT", "BAC", "T", "VZ", "CAT",
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "USO", "UNG", "XLF", "XLK"
]

print(f"Scanned list: {len(tickers)} tickers.")

# 1. Precalculate Daily Bands first using Webull
start_daily = time.time()
print("Step 1: Pre-calculating Daily Bollinger Bands (1d chart) via Webull...")

indicator_bot.precalculate_daily_bands(tickers)
print(f"Daily bands calculated in {time.time() - start_daily:.1f} seconds.\n")

# 2. Fetch 15m regular hours data and evaluate in parallel
print("Step 2: Fetching 15m regular hours candles and evaluating...")
start_15m = time.time()
results = fetch_batch_concurrent(
    tickers=tickers,
    days=15,
    max_workers=20,
    interval="15m",
    includePrePost="false",
    process_fn=indicator_bot.evaluate_ticker_process
)
print(f"15m evaluation completed in {time.time() - start_15m:.1f} seconds.\n")

print("=" * 70)
print("🎯 ACTIVE REVERSAL TRIGGERS (15m Price Piercing Daily Bollinger Bands)")
print("=" * 70)

bullish_triggers = []
bearish_triggers = []

for ticker, res in results.items():
    if res:
        trigger_info = f"  • {ticker}: Price ${res['price']:.2f} | VWAP Target: ${res['vwap']:.2f}"
        if res['action'] == 'BUY':
            bullish_triggers.append(trigger_info)
        else:
            bearish_triggers.append(trigger_info)

print(f"\n🟢 Bullish Reversals (Buy Calls / Stocks) [{len(bullish_triggers)} found]:")
if bullish_triggers:
    for t in bullish_triggers:
        print(t)
else:
    print("  No bullish setups found.")

print(f"\n🔴 Bearish Reversals (Buy Puts / Shorts) [{len(bearish_triggers)} found]:")
if bearish_triggers:
    for t in bearish_triggers:
        print(t)
else:
    print("  No bearish setups found.")
print("=" * 70)
