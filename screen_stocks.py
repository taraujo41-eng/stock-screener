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
4. Technicals Filter:
   - 20 EMA, 50 SMA, 200 SMA positioning & Trend Posture
   - RSI in tradable zone (20 <= RSI <= 85)
5. Market Sentiment Alignment:
   - SPY & QQQ Regime + VIX Volatility Index
   - Tagged as Bullish Aligned, Bearish Aligned, or Counter-Trend Reversal Setup
"""

import os
import json
import time
import requests
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from data_fetcher import get_unofficial_client, get_stock_ticker_id, fetch_batch_concurrent
from reversal_scanner import compute_atr, compute_ema, compute_sma, compute_rsi, get_us_tickers, get_market_sentiment, detect_supply_demand_zones

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

    stage3_passed = {}
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {pool.submit(_screen_options, sym): sym for sym in stage2_symbols}
        for f in as_completed(futures):
            res = f.result()
            if res:
                stage3_passed[res['symbol']] = res

    print(f"Stage 3 passed (Options Chain Health): {len(stage3_passed)} stocks")

    # ── STAGE 4: Technicals Filter (Trend, Key Moving Averages, RSI) ──
    print("\n--- STAGE 4: Technicals Filter (Moving Averages Posture & RSI Health) ---")
    stage4_passed = {}
    for sym, item in stage3_passed.items():
        df = daily_data.get(sym)
        if df is None or len(df) < 20:
            continue
        try:
            last_p = float(df['Close'].iloc[-1])
            rsi_s = compute_rsi(df['Close'], 14)
            rsi_val = float(rsi_s.iloc[-1]) if len(rsi_s) > 0 and not np.isnan(rsi_s.iloc[-1]) else 50.0

            ema20_s = compute_ema(df['Close'], 20)
            ema20 = float(ema20_s.iloc[-1]) if len(ema20_s) > 0 else None

            sma50_s = compute_sma(df['Close'], 50)
            sma50 = float(sma50_s.iloc[-1]) if len(sma50_s) >= 50 and not np.isnan(sma50_s.iloc[-1]) else None

            sma200_s = compute_sma(df['Close'], 200)
            sma200 = float(sma200_s.iloc[-1]) if len(sma200_s) >= 200 and not np.isnan(sma200_s.iloc[-1]) else None

            ema20_dist = round(((last_p - ema20) / ema20) * 100, 2) if ema20 else 0.0
            sma50_dist = round(((last_p - sma50) / sma50) * 100, 2) if sma50 else 0.0
            sma200_dist = round(((last_p - sma200) / sma200) * 100, 2) if sma200 else 0.0

            # Technical Trend Posture
            bull_cnt = 0
            bear_cnt = 0
            if ema20 and last_p > ema20: bull_cnt += 1
            elif ema20 and last_p < ema20: bear_cnt += 1

            if sma50 and last_p > sma50: bull_cnt += 1
            elif sma50 and last_p < sma50: bear_cnt += 1

            if sma200 and last_p > sma200: bull_cnt += 1
            elif sma200 and last_p < sma200: bear_cnt += 1

            if ema20 and sma50 and ema20 > sma50: bull_cnt += 1
            elif ema20 and sma50 and ema20 < sma50: bear_cnt += 1

            if bull_cnt >= 3:
                tech_trend = "Strong Bullish" if bull_cnt == 4 else "Bullish"
            elif bear_cnt >= 3:
                tech_trend = "Strong Bearish" if bear_cnt == 4 else "Bearish"
            else:
                tech_trend = "Neutral"

            # Criteria: Tradable RSI and not catastrophic breakdown below 200 SMA
            if 20.0 <= rsi_val <= 85.0:
                if sma200 is None or last_p >= (sma200 * 0.70):
                    in_d, in_s, d_zone, s_zone = detect_supply_demand_zones(df)
                    info = dict(item)
                    info['rsi_14'] = round(rsi_val, 1)
                    info['ema20_dist'] = ema20_dist
                    info['sma50_dist'] = sma50_dist
                    info['sma200_dist'] = sma200_dist
                    info['technical_trend'] = tech_trend
                    info['in_demand_zone'] = in_d
                    info['in_supply_zone'] = in_s
                    info['demand_zone'] = d_zone['range_str'] if d_zone else None
                    info['supply_zone'] = s_zone['range_str'] if s_zone else None
                    stage4_passed[sym] = info
        except Exception as e:
            print(f"Technical filter error for {sym}: {e}")

    print(f"Stage 4 passed (Technicals): {len(stage4_passed)} stocks")

    # ── STAGE 5: Market Sentiment Alignment ──
    print("\n--- STAGE 5: Market Sentiment Alignment (SPY / QQQ / VIX Regime) ---")
    mkt_sentiment = get_market_sentiment()
    is_mkt_bullish = mkt_sentiment.get("is_bullish", True)
    sentiment_label = mkt_sentiment.get("sentiment", "Bullish")
    sentiment_score = mkt_sentiment.get("score", 65)
    print(f"Market Sentiment Regime: {mkt_sentiment.get('label', sentiment_label)} ({sentiment_score}/100)")

    final_passed = {}
    for sym, item in stage4_passed.items():
        info = dict(item)
        t_trend = info.get('technical_trend', 'Neutral')
        if is_mkt_bullish and 'Bullish' in t_trend:
            align = "Bullish Aligned"
        elif (not is_mkt_bullish) and 'Bearish' in t_trend:
            align = "Bearish Aligned"
        else:
            align = "Counter-Trend Reversal Setup"

        info['market_sentiment'] = sentiment_label
        info['market_sentiment_score'] = sentiment_score
        info['sentiment_alignment'] = align
        final_passed[sym] = info

    print(f"\n==========================================")
    print(f"FINAL PASSED STOCKS (All Criteria): {len(final_passed)} stocks")
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
