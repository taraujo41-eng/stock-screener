from reversal_scanner import momentum_15m_watchlist_scan
try:
    df = momentum_15m_watchlist_scan(["NVDA", "AAPL"])
    print(df)
except Exception as e:
    import traceback
    traceback.print_exc()
