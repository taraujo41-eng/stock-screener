from data_fetcher import fetch_one
import pandas as pd
import numpy as np

# Fetch 15m data for the last 5 days
df = fetch_one("NVDA", days=5, interval="15m", includePrePost="false")
if df is not None and not df.empty:
    print(df.tail(10))
    
    # Extract date part
    df['date_only'] = df.index.date
    
    # Group by date to find daily High/Low
    daily_stats = df.groupby('date_only').agg({'High': 'max', 'Low': 'min', 'Volume': 'sum'})
    print("\nDaily Stats:")
    print(daily_stats)
    
    # Find Prior Day High/Low
    if len(daily_stats) >= 2:
        # The second to last row is the prior day
        prior_day = daily_stats.iloc[-2]
        pdh = prior_day['High']
        pdl = prior_day['Low']
        
        # Current candle
        current_candle = df.iloc[-1]
        c_high = current_candle['High']
        c_low = current_candle['Low']
        c_close = current_candle['Close']
        
        print(f"\nPrior Day High: {pdh}, Prior Day Low: {pdl}")
        print(f"Current Candle: High {c_high}, Low {c_low}, Close {c_close}")
        
        if c_high > pdh:
            print("BREAKOUT!")
        elif c_low < pdl:
            print("BREAKDOWN!")
        else:
            print("Inside day/No break")
