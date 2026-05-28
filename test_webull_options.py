import os
import sys
from dotenv import load_dotenv
from data_fetcher import fetch_options_chain

load_dotenv()

print("=" * 60)
print("🔍 TESTING WEBULL REAL-TIME OPTIONS DATA")
print("=" * 60)

try:
    print("Fetching option chain for AAPL...")
    chain = fetch_options_chain("AAPL")
    if chain is not None:
        print("✅ SUCCESS: Option chain retrieved!")
        print(f"  Ticker: {chain['ticker']}")
        print(f"  Underlying Price: {chain['underlyingPrice']}")
        print(f"  Expirations Count: {len(chain['expirations'])}")
        print(f"  First Expiration Timestamp: {chain['expirations'][0]}")
        
        # Print sample call/put contracts from first chain
        first_chain = chain["firstChain"]
        calls = first_chain.get("calls", [])
        puts = first_chain.get("puts", [])
        print(f"  Calls found: {len(calls)}")
        print(f"  Puts found: {len(puts)}")
        
        if calls:
            print("\nSample Call Contract:")
            c = calls[0]
            for k, v in c.items():
                print(f"    {k}: {v}")
                
        if puts:
            print("\nSample Put Contract:")
            p = puts[0]
            for k, v in p.items():
                print(f"    {k}: {v}")
    else:
        print("❌ FAILED: Option chain is None (fell back to Yahoo and also failed or returned None)")
except Exception as e:
    print("❌ ERROR:", e)
    import traceback
    traceback.print_exc()

print("=" * 60)
