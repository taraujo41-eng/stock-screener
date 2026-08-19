"""
Comprehensive Screener for Watchlist based on criteria:
1. Base Equity & Size Filter:
   - Market Cap > $10 Billion ($10,000,000,000)
   - Optionable: True
   - Average Daily Volume (30-day / 3-month) > 3,000,000 shares
   - Price > $20.00
2. Options Chain Health:
   - Daily Options Volume > 5,000 contracts
   - Front-Month Open Interest > 1,000 contracts
3. Directional Movement (Volatility):
   - ATR (14-day) > $1.50 OR ATR% > 1.5% of stock price
"""

import os
import json
import time
import requests
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from data_fetcher import get_unofficial_client, get_stock_ticker_id, fetch_batch_concurrent
from reversal_scanner import compute_atr, get_us_tickers

def collect_universe():
    candidates = set()
    # 1. Existing watchlist
    try:
        with open('watchlist.json') as f:
            candidates.update(json.load(f))
    except Exception:
        pass

    # 2. S&P 500 / Nasdaq fallback
    try:
        with open('sp500_nasdaq_fallback.json') as f:
            candidates.update(json.load(f))
    except Exception:
        pass

    # 3. Scanner curated list
    try:
        candidates.update(get_us_tickers())
    except Exception:
        pass

    # 4. Nasdaq traded symbols
    try:
        url = 'http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt'
        df_nasdaq = pd.read_csv(url, sep='|')
        df_nasdaq = df_nasdaq[(df_nasdaq['Test Issue'] == 'N') & (df_nasdaq['ETF'] == 'N')]
        syms = df_nasdaq['Symbol'].dropna().str.strip().tolist()
        syms = [s for s in syms if s.isalpha() and 1 <= len(s) <= 5]
        candidates.update(syms)
    except Exception as e:
        print(f"Nasdaq traded fetch error: {e}")

    exclude = {"TRUE", "NONE", "NULL", "CTEST", "NTEST", "ZTEST"}
    return sorted([s for s in candidates if s not in exclude and s.isalpha() and 1 <= len(s) <= 5])

def run_screener():
    wb = get_unofficial_client()
    candidates = collect_universe()
    print(f"Starting screen on {len(candidates)} candidates...")

    # ── STAGE 1: Base Equity & Size Filter ──
    print("\n--- STAGE 1: Base Equity & Size Filter (Price > $20, MCap > $10B, AvgVol > 3M) ---")
    def _screen_quote(sym):
        try:
            q = wb.get_quote(stock=sym)
            if not q or not isinstance(q, dict):
                return None
            price = float(q.get('close') or 0)
            mcap = float(q.get('marketValue') or 0)
            avg_vol = float(q.get('avgVol3M') or q.get('avgVol10D') or q.get('volume') or 0)
            
            # Criteria:
            # Price > $20
            # Market Cap > $10 Billion
            # Avg Daily Volume > 3,000,000
            if price > 20.0 and mcap > 10_000_000_000 and avg_vol > 3_000_000:
                return {
                    'symbol': sym,
                    'price': round(price, 2),
                    'marketCap': mcap,
                    'marketCap_B': round(mcap / 1e9, 2),
                    'avgVolume': int(avg_vol),
                    'dayVolume': int(float(q.get('volume') or 0))
                }
        except Exception:
            pass
        return None

    stage1_passed = {}
    with ThreadPoolExecutor(max_workers=35) as pool:
        futures = {pool.submit(_screen_quote, sym): sym for sym in candidates}
        for f in as_completed(futures):
            res = f.result()
            if res:
                stage1_passed[res['symbol']] = res

    print(f"Stage 1 passed: {len(stage1_passed)} stocks")

    stage1_symbols = sorted(stage1_passed.keys())

    # ── STAGE 2: ATR (14-Day) Volatility Filter ──
    print("\n--- STAGE 2: Volatility Filter (ATR-14 > $1.50 OR ATR% > 1.5%) ---")
    daily_data = fetch_batch_concurrent(stage1_symbols, days=60, max_workers=20, interval="1d")

    stage2_passed = {}
    for sym in stage1_symbols:
        df = daily_data.get(sym)
        if df is None or len(df) < 15:
            continue
        try:
            atr_series = compute_atr(df, 14)
            atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0
            price = stage1_passed[sym]['price']
            atr_pct = (atr_val / price) * 100.0 if price > 0 else 0

            # Criteria: ATR > $1.50 OR ATR > 1.5%
            if atr_val > 1.50 or atr_pct > 1.5:
                info = dict(stage1_passed[sym])
                info['atr_14'] = round(atr_val, 2)
                info['atr_pct'] = round(atr_pct, 2)
                stage2_passed[sym] = info
        except Exception as e:
            print(f"ATR error for {sym}: {e}")

    print(f"Stage 2 passed (Volatility): {len(stage2_passed)} stocks")

    stage2_symbols = sorted(stage2_passed.keys())

    # ── STAGE 3: Options Chain Health ──
    print("\n--- STAGE 3: Options Chain Health (Optionable=True, OptVol > 5k, Front-Month OI > 1k) ---")
    headers = wb.build_req_headers()

    def _screen_options(sym):
        try:
            tid = get_stock_ticker_id(wb, sym)
            if not tid:
                return None
            data = {'count': -1, 'direction': 'all', 'tickerId': tid}
            res = requests.post(wb._urls.options_exp_dat_new(), json=data, headers=headers, timeout=5)
            if res.status_code != 200:
                return None
            res_json = res.json()
            exp_list = res_json.get('expireDateList', [])
            if not exp_list:
                return None # Not optionable

            total_opt_vol = 0
            front_month_oi = 0
            total_oi = 0

            for idx, entry in enumerate(exp_list):
                exp_data = entry.get('data', [])
                for item in exp_data:
                    vol = int(float(item.get('volume') or 0))
                    oi = int(float(item.get('openInterest') or 0))
                    total_opt_vol += vol
                    total_oi += oi
                    if idx == 0: # Front-month / nearest expiration
                        front_month_oi += oi

            # Criteria:
            # Optionable: Yes
            # Options Volume (Daily) > 5,000 contracts
            # Open Interest (Front-Month) > 1,000 contracts
            if total_opt_vol > 5000 and front_month_oi > 1000:
                info = dict(stage2_passed[sym])
                info['opt_volume'] = total_opt_vol
                info['front_month_oi'] = front_month_oi
                info['total_oi'] = total_oi
                info['expirations_count'] = len(exp_list)
                return info
        except Exception:
            pass
        return None

    final_passed = {}
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {pool.submit(_screen_options, sym): sym for sym in stage2_symbols}
        for f in as_completed(futures):
            res = f.result()
            if res:
                final_passed[res['symbol']] = res

    print(f"\n==========================================")
    print(f"FINAL PASSED STOCKS: {len(final_passed)} stocks")
    print(f"==========================================")

    # Convert to DataFrame and sort by Market Cap or Option Volume
    results_list = list(final_passed.values())
    results_list.sort(key=lambda x: x['symbol'])

    with open('screened_stocks_results.json', 'w') as f:
        json.dump(results_list, f, indent=2)

    passed_tickers = [x['symbol'] for x in results_list]
    print(f"Passed Tickers List ({len(passed_tickers)}):")
    print(passed_tickers)

    return results_list

if __name__ == '__main__':
    run_screener()
