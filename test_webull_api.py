import os
import sys
from dotenv import load_dotenv
from data_fetcher import fetch_one, test_connection

load_dotenv()

print("=" * 60)
print("🔍 TESTING UNIFIED DATA FETCHER & WEBULL CONNECTION")
print("=" * 60)

# Run full connection test diagnostics
print("\n[Test 1] Running full connection diagnostics...")
diag = test_connection("AAPL")
print("Diagnostics result:")
for k, v in diag.items():
    print(f"  {k}: {v}")

# Run fetch_one call
print("\n[Test 2] Testing fetch_one('AAPL') with automatic fallback...")
try:
    df = fetch_one("AAPL", days=10)
    if df is not None:
        print("✅ SUCCESS: DataFrame retrieved!")
        print(f"  Rows count: {len(df)}")
        print(f"  Last date: {df.index[-1]}")
        print(f"  Close price: {df['Close'].iloc[-1]:.2f}")
        print(f"  52w High (Attr): {df.attrs.get('fiftyTwoWeekHigh')}")
        print(f"  52w Low (Attr): {df.attrs.get('fiftyTwoWeekLow')}")
        print(f"  Previous Close (Attr): {df.attrs.get('previousClose')}")
        print("\nHead of DataFrame:")
        print(df.head(2))
    else:
        print("❌ FAILED: DataFrame is None")
except Exception as e:
    print("❌ ERROR during fetch_one:", e)
    import traceback
    traceback.print_exc()

print("=" * 60)
