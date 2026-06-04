"""
Stock Reversal Scanner — Full Market Edition
Scans the entire US stock market for bullish/bearish reversal setups.

Modes:
  • Watchlist scan  — fast, scans a custom list
  • Full market scan — fetches all US tickers, pre-filters, then analyzes

Data source: Yahoo Finance chart API via data_fetcher.py
(works on cloud servers — no yfinance library dependency)
"""

import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from io import StringIO
import time
import json
import os
import warnings
from data_fetcher import (
    fetch_batch, fetch_batch_concurrent, test_connection,
    fetch_options_chain, fetch_options_for_expiration, fetch_news
)

warnings.filterwarnings("ignore")

# =====================================================================
# Global progress tracker  (read by the web server)
# =====================================================================

scan_progress = {
    "status": "idle",       # idle | running | done | error
    "phase": "",            # fetching_tickers | downloading | analyzing | complete
    "phase_label": "",
    "current": 0,
    "total": 0,
    "found": 0,
    "ticker": "",
    "pct": 0,
    "eta_seconds": 0,
    "debug_log": [],
}

def _reset_progress():
    scan_progress.update({
        "status": "idle", "phase": "", "phase_label": "",
        "current": 0, "total": 0, "found": 0,
        "ticker": "", "pct": 0, "eta_seconds": 0,
        "debug_log": [],
    })

def _update_progress(phase, label, current, total, ticker="", found=None):
    scan_progress["status"] = "running"
    scan_progress["phase"] = phase
    scan_progress["phase_label"] = label
    scan_progress["current"] = current
    scan_progress["total"] = total
    scan_progress["ticker"] = ticker
    scan_progress["pct"] = int((current / total) * 100) if total else 0
    if found is not None:
        scan_progress["found"] = found


def detect_news_catalyst(ticker, lookback_hours=48):
    """
    Fetch news for a ticker and check if any articles published within lookback_hours
    contain catalyst-related keywords.
    Returns (has_catalyst, catalyst_tag, article_info)
    """
    try:
        articles = fetch_news(ticker, limit=5)
        if not articles:
            return False, None, None
            
        import pytz
        now = datetime.now(pytz.UTC)
        cutoff = now - timedelta(hours=lookback_hours)
        
        # High impact catalyst terms to search for
        CATALYST_KEYWORDS = [
            "earnings", "revenue", "eps", "profit", "dividend", "financials", "guidance", # Earnings
            "fda", "clinical", "trial", "phase", "drug", "treatment", "biotech", "approval", # Biotech
            "merger", "acquisition", "acquire", "buyout", "takeover", "deal", "merge", # M&A
            "partnership", "collaborate", "collaboration", "joint venture", "contract", # Deals
            "upgrade", "downgrade", "rating", "initiate", "buy", "sell", "neutral", # Analyst ratings
            "sec", "investigation", "lawsuit", "settlement", "regulatory", "sue", # Legal
            "ceo", "cfo", "resign", "appoint", "hire", "executive", "board" # Management
        ]
        
        for art in articles:
            pub_time = art.get("publish_time")
            if not pub_time or pub_time < cutoff:
                continue
                
            title = art.get("title", "")
            title_lower = title.lower()
            
            # Check for keyword matches
            matched_keywords = [kw for kw in CATALYST_KEYWORDS if f" {kw}" in f" {title_lower} " or f"-{kw}" in title_lower]
            if matched_keywords:
                # Truncate headline to keep it tidy in frontend pills
                snippet = title[:45] + "..." if len(title) > 45 else title
                # Clean up characters that might interfere with pill split delimiter (e.g. pipe)
                snippet = snippet.replace("|", "/")
                
                # Format time as a string for JSON serialization
                pub_time_str = pub_time.strftime("%b %d, %Y at %I:%M %p UTC") if hasattr(pub_time, "strftime") else str(pub_time)
                
                article_info = {
                    "title": title,
                    "publisher": art.get("publisher", "Unknown"),
                    "publish_time": pub_time_str,
                    "url": art.get("url", "")
                }
                return True, f"News: {snippet}", article_info
                
    except Exception as e:
        print(f"  Error detecting news catalyst for {ticker}: {e}")
        
    return False, None, None


# =====================================================================
# Fetch comprehensive US ticker list
# =====================================================================

def get_us_tickers():
    """Fetch large-cap US stock tickers (S&P 500 + NASDAQ 100)."""
    tickers = set()
    headers = {"User-Agent": "Mozilla/5.0"}

    # ── Source 1: S&P 500 ──
    try:
        html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers).text
        tables = pd.read_html(StringIO(html), attrs={"id": "constituents"})
        sp = tables[0]
        for sym in sp["Symbol"]:
            clean = str(sym).strip().replace(".", "-")
            if clean:
                tickers.add(clean)
        print(f"  Source 1 (S&P 500): fetched {len(sp)} tickers")
    except Exception as e:
        print(f"  Source 1 (S&P 500): failed ({e})")

    # ── Source 2: NASDAQ 100 ──
    try:
        html = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=headers).text
        tables = pd.read_html(StringIO(html), attrs={"id": "constituents"})
        ndx = tables[0]
        added = 0
        for sym in ndx["Ticker"]:
            clean = str(sym).strip().replace(".", "-")
            if clean and clean not in tickers:
                tickers.add(clean)
                added += 1
        print(f"  Source 2 (NASDAQ 100): +{added} unique tickers")
    except Exception as e:
        print(f"  Source 2 (NASDAQ 100): failed ({e})")

    # ── Source 3: Major ETFs ──
    etfs = {
        "SPY", "QQQ", "IWM", "DIA", "VTI", "VEU", "VWO", "GLD", "SLV", "USO",
        "XLF", "XLK", "XLE", "XLI", "XLV", "XLP", "XLU", "XLB", "XLY", "XLRE",
        "XBI", "SMH", "KRE", "KBE", "GDX", "GDXJ", "TLT", "IEF", "LQD", "HYG",
        "ARKK", "ARKG", "ARKF", "EEM", "EFA", "EWJ", "FXI", "VGK", "TQQQ", "SQQQ",
        "SOXL", "SOXS", "LABU", "LABD", "UVXY", "VIXY", "UNG", "BOIL", "KOLD"
    }
    added_etfs = 0
    for sym in etfs:
        if sym not in tickers:
            tickers.add(sym)
            added_etfs += 1
    print(f"  Source 3 (Major ETFs): added {added_etfs} unique ETFs (Total list: {len(etfs)})")
    if "SPY" in tickers: print("  ✓ Verified: SPY is in the scan list")
    if "QQQ" in tickers: print("  ✓ Verified: QQQ is in the scan list")

    # Remove known non-equity / test symbols
    exclude = {"TRUE", "NONE", "NULL", "CTEST", "NTEST", "ZTEST"}
    tickers -= exclude

    print(f"  Final Ticker Count: {len(tickers)}")
    return sorted(tickers)


# =====================================================================
# Technical Indicators  (no external TA library needed)
# =====================================================================

def compute_rsi(series, length=14):
    """Wilder-style RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# =====================================================================
# VWAP and RVOL helpers
# =====================================================================

def compute_vwap(df):
    """Calculate daily VWAP."""
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    dates = df.index.date
    vwap = (typical_price * df['Volume']).groupby(dates).cumsum() / df['Volume'].groupby(dates).cumsum()
    return vwap

def compute_rvol(df):
    """Calculate daily relative volume (Total Volume Today / Average Daily Volume)."""
    dates = df.index.date
    daily_volume = df['Volume'].groupby(dates).sum()
    if len(daily_volume) < 2:
        return 1.0
    
    today_vol = float(daily_volume.iloc[-1])
    avg_vol = float(daily_volume.iloc[:-1].mean())
    if avg_vol == 0:
        return 0.0
    return today_vol / avg_vol

def compute_sma(series, length=200):
    """Simple Moving Average."""
    return series.rolling(window=length).mean()

def compute_macd(series, fast=12, slow=26, signal=9):
    """Calculate MACD Line, Signal Line, and Histogram."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def detect_rsi_divergence(price_series, rsi_series, lookback=20):
    """
    Check for RSI Divergence in the last `lookback` periods.
    Tightened: requires magnitude >= 5 RSI points, zone thresholds,
    and swing points at least 5 bars apart.
    Returns (bull_div, bear_div)
    """
    if len(price_series) < lookback + 2:
        return False, False
        
    curr_price = float(price_series.iloc[-1])
    curr_rsi = float(rsi_series.iloc[-1])
    
    # The lookback window (excluding last 2 candles for distinct swing)
    window_price = price_series.iloc[-(lookback+2):-2]
    window_rsi = rsi_series.iloc[-(lookback+2):-2]
    
    # Find the index position of the swing low/high (must be >= 5 bars from current)
    lowest_idx = window_price.values.argmin()
    highest_idx = window_price.values.argmax()
    
    bars_from_low = len(window_price) - lowest_idx
    bars_from_high = len(window_price) - highest_idx
    
    lowest_price_in_window = float(window_price.iloc[lowest_idx])
    highest_price_in_window = float(window_price.iloc[highest_idx])
    
    rsi_at_low = float(window_rsi.iloc[lowest_idx])
    rsi_at_high = float(window_rsi.iloc[highest_idx])

    # Bullish Divergence: Price lower low + RSI higher low
    # Requires: RSI < 35 (true oversold), magnitude >= 3 pts, swing >= 5 bars apart
    rsi_bull_magnitude = curr_rsi - rsi_at_low
    bull_div = (
        (curr_price < lowest_price_in_window) and
        (curr_rsi > rsi_at_low) and
        (rsi_bull_magnitude >= 3) and
        (curr_rsi < 35) and
        (bars_from_low >= 5)
    )
    
    # Bearish Divergence: Price higher high + RSI lower high
    # Requires: RSI > 65 (true overbought), magnitude >= 3 pts, swing >= 5 bars apart
    rsi_bear_magnitude = rsi_at_high - curr_rsi
    bear_div = (
        (curr_price > highest_price_in_window) and
        (curr_rsi < rsi_at_high) and
        (rsi_bear_magnitude >= 3) and
        (curr_rsi > 65) and
        (bars_from_high >= 5)
    )
    
    return bull_div, bear_div

# =====================================================================
# Breakout / Squeeze Indicators
# =====================================================================

def compute_atr(df, length=14):
    """Average True Range."""
    high = df['High']
    low = df['Low']
    prev_close = df['Close'].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=length).mean()

def compute_bollinger_bands(series, length=20, num_std=2):
    """Returns (upper, middle, lower) Bollinger Bands."""
    middle = series.rolling(window=length).mean()
    std = series.rolling(window=length).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower

def compute_keltner_channels(df, length=20, atr_mult=1.5):
    """Returns (upper, middle, lower) Keltner Channels."""
    middle = df['Close'].ewm(span=length, adjust=False).mean()
    atr = compute_atr(df, length)
    upper = middle + atr_mult * atr
    lower = middle - atr_mult * atr
    return upper, middle, lower

def detect_squeeze(df, bb_length=20, kc_length=20, kc_mult=1.5):
    """
    TTM Squeeze: returns True when Bollinger Bands are inside Keltner Channels.
    Also returns the momentum histogram direction.
    Returns (is_squeeze, momentum_positive)
    """
    bb_upper, bb_mid, bb_lower = compute_bollinger_bands(df['Close'], bb_length)
    kc_upper, kc_mid, kc_lower = compute_keltner_channels(df, kc_length, kc_mult)

    # Squeeze is ON when BB is inside KC
    squeeze_on = (bb_lower.iloc[-1] > kc_lower.iloc[-1]) and (bb_upper.iloc[-1] < kc_upper.iloc[-1])

    # Momentum: linear regression of (close - avg(highest high, lowest low, close ema)) — simplified
    # We use a simpler proxy: close relative to midline of KC
    momentum = df['Close'].iloc[-1] - kc_mid.iloc[-1]
    prev_momentum = df['Close'].iloc[-2] - kc_mid.iloc[-2] if len(df) > 1 else 0

    return squeeze_on, (momentum > 0), (momentum > prev_momentum)

def compute_adr_pct(df, length=14):
    """Average Daily Range as a percentage of price."""
    if len(df) < length:
        return 0.0
    daily_range = df['High'] - df['Low']
    avg_range = float(daily_range.iloc[-length:].mean())
    last_price = float(df['Close'].iloc[-1])
    if last_price == 0:
        return 0.0
    return (avg_range / last_price) * 100

def compute_ema(series, length):
    """Exponential Moving Average."""
    return series.ewm(span=length, adjust=False).mean()

def detect_triangle(df, lookback=20):
    """
    Detect ascending/descending triangle patterns.
    Ascending: flat resistance (highs), rising lows
    Descending: flat support (lows), falling highs
    Returns (ascending, descending)
    """
    if len(df) < lookback + 2:
        return False, False

    window = df.iloc[-lookback:]
    highs = window['High'].values
    lows = window['Low'].values

    # Linear regression slopes
    x = np.arange(lookback)

    # Highs slope
    high_slope = np.polyfit(x, highs, 1)[0]
    # Lows slope
    low_slope = np.polyfit(x, lows, 1)[0]

    last_price = float(df['Close'].iloc[-1])
    high_range = (highs.max() - highs.min()) / last_price if last_price > 0 else 1
    low_range = (lows.max() - lows.min()) / last_price if last_price > 0 else 1

    # Ascending triangle: flat highs (small slope, tight range) + rising lows
    ascending = (
        abs(high_slope / last_price) < 0.001 and  # Flat resistance
        high_range < 0.03 and                       # Highs within 3%
        low_slope / last_price > 0.0005             # Rising lows
    )

    # Descending triangle: flat lows + falling highs
    descending = (
        abs(low_slope / last_price) < 0.001 and   # Flat support
        low_range < 0.03 and                        # Lows within 3%
        high_slope / last_price < -0.0005           # Falling highs
    )

    return ascending, descending

def count_distribution_accumulation(df, lookback=10):
    """
    Count high-volume up days (accumulation) and high-volume down days (distribution)
    in the last `lookback` trading days.
    Returns (accumulation_days, distribution_days)
    """
    if len(df) < lookback + 1:
        return 0, 0

    window = df.iloc[-lookback:]
    vol_avg = float(df['Volume'].iloc[-(lookback + 20):-lookback].mean()) if len(df) > lookback + 20 else float(df['Volume'].mean())

    accum = 0
    distrib = 0
    for i in range(len(window)):
        row = window.iloc[i]
        is_up = row['Close'] > row['Open']
        is_high_vol = row['Volume'] > vol_avg * 1.2

        if is_up and is_high_vol:
            accum += 1
        elif not is_up and is_high_vol:
            distrib += 1

    return accum, distrib


# =====================================================================
# Candlestick Patterns & Trend Context
# =====================================================================

def detect_patterns(df):
    """Identify Hammer, Shooting Star, Engulfing, and Tail patterns.
    Tightened: requires minimum range >= 0.3% of price to filter noise."""
    if len(df) < 2:
        return {"hammer": False, "star": False, "bull_engulf": False, "bear_engulf": False, "bottoming_tail": False, "topping_tail": False}
    
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    last_price = float(curr['Close'])
    
    body = abs(curr['Close'] - curr['Open'])
    total_range = curr['High'] - curr['Low']
    if total_range == 0: total_range = 0.001
    
    # Minimum range filter: candle must span >= 0.3% of price (no noise)
    min_range = last_price * 0.003
    if total_range < min_range:
        return {"hammer": False, "star": False, "bull_engulf": False, "bear_engulf": False, "bottoming_tail": False, "topping_tail": False}
    
    upper_wick = curr['High'] - max(curr['Close'], curr['Open'])
    lower_wick = min(curr['Close'], curr['Open']) - curr['Low']
    
    # 1. Hammer (Small body, long lower wick, tiny upper wick)
    is_hammer = (lower_wick > 2 * body) and (upper_wick < 0.15 * total_range) and (body > 0)
    
    # 2. Shooting Star (Small body, long upper wick, tiny lower wick)
    is_star = (upper_wick > 2 * body) and (lower_wick < 0.15 * total_range) and (body > 0)
    
    # 3. Bullish Engulfing (Green candle wraps previous Red candle)
    is_bull_engulf = (curr['Close'] > curr['Open']) and (prev['Close'] < prev['Open']) and \
                     (curr['Close'] >= prev['Open']) and (curr['Open'] <= prev['Close'])
    
    # 4. Bearish Engulfing (Red candle wraps previous Green candle)
    is_bear_engulf = (curr['Close'] < curr['Open']) and (prev['Close'] > prev['Open']) and \
                     (curr['Close'] <= prev['Open']) and (curr['Open'] >= prev['Close'])
    
    # 5. Bottoming Tail (Lower wick >= 75%, body <= 25%)
    is_bottoming_tail = (lower_wick >= 0.75 * total_range) and (body <= 0.25 * total_range)
    
    # 6. Topping Tail (Upper wick >= 75%, body <= 25%)
    is_topping_tail = (upper_wick >= 0.75 * total_range) and (body <= 0.25 * total_range)
    
    return {
        "hammer": is_hammer,
        "star": is_star,
        "bull_engulf": is_bull_engulf,
        "bear_engulf": is_bear_engulf,
        "bottoming_tail": is_bottoming_tail,
        "topping_tail": is_topping_tail
    }

def get_trend_context(df, days=5):
    """Check if the prior trend was bullish or bearish."""
    if len(df) < days + 1:
        return "neutral"
    
    # Compare current price to price 5 days ago
    start_price = df['Close'].iloc[-(days+1)]
    end_price = df['Close'].iloc[-2] # Look at the trend UP TO yesterday
    
    change = ((end_price - start_price) / start_price) * 100
    
    if change < -2.0: return "downtrend"
    if change > 2.0: return "uptrend"
    return "flat"

# =====================================================================
# Options Strategy: Directional Selection
# =====================================================================

def find_best_option(ticker, signal_type, last_price):
    """
    Find the ideal contract:
    - 30-60 DTE
    - Delta 0.40-0.70 (Approx by ITM/ATM strikes)
    - High Volume & OI (>500)
    - Tight Spread (<10%)
    """
    try:
        chain_meta = fetch_options_chain(ticker)
        if not chain_meta: return None
        
        now = time.time()
        # 1. Filter for 30-60 DTE
        valid_exps = []
        for exp in chain_meta["expirations"]:
            dte = (exp - now) / 86400
            if 25 <= dte <= 65: # Allow slight buffer around 30-60
                valid_exps.append(exp)
        
        if not valid_exps: return None
        
        # We'll check the most liquid looking expiration in our range
        best_contract = None
        
        for exp_ts in valid_exps:
            chain = fetch_options_for_expiration(ticker, exp_ts)
            if not chain: continue
            
            contracts = chain.get("calls" if signal_type == "bullish" else "puts", [])
            
            for c in contracts:
                strike = c.get("strike")
                vol = c.get("volume", 0)
                oi = c.get("openInterest", 0)
                bid = c.get("bid", 0)
                ask = c.get("ask", 0)
                iv = c.get("impliedVolatility", 0)
                
                # Liquidity Filter
                if vol < 300 or oi < 300: continue # Adjusted slightly lower for scan
                
                mid = (bid + ask) / 2
                if mid <= 0: continue
                spread_pct = ((ask - bid) / mid) * 100
                if spread_pct > 12: continue # Tight spread rule
                
                # Delta Approximation (0.40-0.70)
                # For Calls: 0.70 delta is ~3% ITM, 0.40 delta is ~2% OTM
                # For Puts: Inverse
                dist_pct = (strike - last_price) / last_price
                
                is_valid_strike = False
                if signal_type == "bullish":
                    # Call: Strike should be between -4% (ITM) and +1% (ATM/OTM)
                    if -0.05 <= dist_pct <= 0.01: is_valid_strike = True
                else:
                    # Put: Strike should be between -1% (OTM/ATM) and +5% (ITM)
                    if -0.01 <= dist_pct <= 0.05: is_valid_strike = True
                
                if not is_valid_strike: continue
                
                # Pick the contract with the highest Volume + OI (Liquidity King)
                score = vol + oi
                if not best_contract or score > best_contract["score"]:
                    dte_days = int((exp_ts - now) / 86400)
                    best_contract = {
                        "symbol": c.get("contractSymbol"),
                        "strike": strike,
                        "type": "CALL" if signal_type == "bullish" else "PUT",
                        "exp": datetime.fromtimestamp(exp_ts).strftime("%b %d"),
                        "dte": dte_days,
                        "mid": round(mid, 2),
                        "iv": round(iv * 100, 1),
                        "score": score
                    }
            
            if best_contract: break # Found a solid candidate in this expiration
            
        return best_contract
    except Exception:
        return None


# =====================================================================
# Unusual Options Activity Detector
# =====================================================================

def detect_unusual_options(sym):
    """
    Detect unusual options activity by analyzing the front-month chain.
    
    Checks for:
      1. Contracts with Volume/OI ratio > 2.0 (unusual flow)
      2. Call vs Put volume skew (directional bias)
      3. High absolute volume on individual contracts
    
    Returns: (bull_unusual, bear_unusual, detail_str)
    """
    try:
        chain_meta = fetch_options_chain(sym)
        if not chain_meta or not chain_meta.get("firstChain"):
            return False, False, ""
        
        chain = chain_meta["firstChain"]
        calls = chain.get("calls", [])
        puts = chain.get("puts", [])
        
        if not calls and not puts:
            return False, False, ""
        
        # --- Aggregate volume and find unusual contracts ---
        total_call_vol = 0
        total_put_vol = 0
        unusual_call_contracts = 0
        unusual_put_contracts = 0
        max_call_vol_oi = 0.0
        max_put_vol_oi = 0.0
        
        for c in calls:
            vol = c.get("volume", 0) or 0
            oi = c.get("openInterest", 0) or 0
            total_call_vol += vol
            if oi > 50 and vol > 100:  # Minimum thresholds to avoid noise
                ratio = vol / oi
                if ratio > 2.0:
                    unusual_call_contracts += 1
                    max_call_vol_oi = max(max_call_vol_oi, ratio)
        
        for p in puts:
            vol = p.get("volume", 0) or 0
            oi = p.get("openInterest", 0) or 0
            total_put_vol += vol
            if oi > 50 and vol > 100:
                ratio = vol / oi
                if ratio > 2.0:
                    unusual_put_contracts += 1
                    max_put_vol_oi = max(max_put_vol_oi, ratio)
        
        # --- Determine directional bias ---
        total_vol = total_call_vol + total_put_vol
        if total_vol < 500:  # Not enough options activity to matter
            return False, False, ""
        
        call_pct = total_call_vol / total_vol if total_vol > 0 else 0.5
        
        # Bullish unusual: heavy call flow + unusual call contracts
        bull_unusual = (
            (unusual_call_contracts >= 2 and call_pct > 0.60) or
            (unusual_call_contracts >= 3) or
            (max_call_vol_oi >= 5.0 and call_pct > 0.55)
        )
        
        # Bearish unusual: heavy put flow + unusual put contracts
        bear_unusual = (
            (unusual_put_contracts >= 2 and call_pct < 0.40) or
            (unusual_put_contracts >= 3) or
            (max_put_vol_oi >= 5.0 and call_pct < 0.45)
        )
        
        # Build detail string
        details = []
        if bull_unusual:
            details.append(f"Calls {call_pct*100:.0f}%")
            if max_call_vol_oi >= 3.0:
                details.append(f"V/OI {max_call_vol_oi:.1f}x")
        if bear_unusual:
            details.append(f"Puts {(1-call_pct)*100:.0f}%")
            if max_put_vol_oi >= 3.0:
                details.append(f"V/OI {max_put_vol_oi:.1f}x")
        
        detail_str = ", ".join(details) if details else ""
        
        return bull_unusual, bear_unusual, detail_str
        
    except Exception as e:
        print(f"  Options activity check failed for {sym}: {e}")
        return False, False, ""


# =====================================================================
# Analyze a single stock DataFrame
# =====================================================================

def _analyze_stock(sym, df, rsi_bull_thresh=35, rsi_bear_thresh=65, swing_tolerance=0.03, skip_options=False):
    """
    Multi-confirmation scoring system for reversal analysis.
    Each indicator contributes points — minimum 4 required to fire.
    skip_options=True skips expensive options API calls (for use in batch scans).
    """
    try:
        if len(df) < 20: return None
        
        curr = df.iloc[-1]
        last_price = float(curr['Close'])
        
        # 1. Metadata
        fiftyTwoWeekHigh = df.attrs.get("fiftyTwoWeekHigh")
        fiftyTwoWeekLow = df.attrs.get("fiftyTwoWeekLow")
        previousClose = df.attrs.get("previousClose")
        if not previousClose: previousClose = df['Close'].iloc[-2]
        
        # 2. RVOL (20-day avg)
        rvol = compute_rvol(df)
        
        # 3. RSI
        rsi_series = compute_rsi(df['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])
        
        # 4. SMA 200
        sma200_series = compute_sma(df['Close'], 200)
        sma200 = float(sma200_series.iloc[-1]) if not np.isnan(sma200_series.iloc[-1]) else None
        
        # 5. Range Positioning (Close vs High/Low of the day)
        day_range = curr['High'] - curr['Low']
        if day_range == 0: day_range = 0.01
        range_pos = (curr['Close'] - curr['Low']) / day_range  # 0 to 1
        
        # 6. Candlestick Patterns & Trend
        patterns = detect_patterns(df)
        trend = get_trend_context(df, days=5)
        
        # 7. Support/Resistance Distance
        near_200sma = abs((last_price - sma200) / sma200) < 0.05 if sma200 else False
        near_52w_low = abs((last_price - fiftyTwoWeekLow) / fiftyTwoWeekLow) < 0.05 if fiftyTwoWeekLow else False
        near_52w_high = abs((last_price - fiftyTwoWeekHigh) / fiftyTwoWeekHigh) < 0.05 if fiftyTwoWeekHigh else False

        # 8. MACD (tightened: require 3+ same-sign histogram bars before cross, and significance)
        macd_line, signal_line, macd_hist = compute_macd(df['Close'])
        macd_magnitude_threshold = last_price * 0.001  # 0.1% of price

        prior_neg_bars = sum(1 for i in range(-5, -1) if i + len(macd_hist) >= 0 and float(macd_hist.iloc[i]) < 0)
        is_macd_bull_cross = (
            (float(macd_hist.iloc[-1]) > 0) and (float(macd_hist.iloc[-2]) < 0) and
            (float(macd_line.iloc[-1]) < 0) and
            (abs(float(macd_line.iloc[-1])) > macd_magnitude_threshold) and
            (prior_neg_bars >= 3)
        )

        prior_pos_bars = sum(1 for i in range(-5, -1) if i + len(macd_hist) >= 0 and float(macd_hist.iloc[i]) > 0)
        is_macd_bear_cross = (
            (float(macd_hist.iloc[-1]) < 0) and (float(macd_hist.iloc[-2]) > 0) and
            (float(macd_line.iloc[-1]) > 0) and
            (abs(float(macd_line.iloc[-1])) > macd_magnitude_threshold) and
            (prior_pos_bars >= 3)
        )

        # 9. RSI Divergence (already tightened in detector)
        bull_div, bear_div = detect_rsi_divergence(df['Close'], rsi_series, lookback=20)

        # 10. Rubber Band Extension (20 SMA)
        sma20_series = compute_sma(df['Close'], 20)
        sma20 = float(sma20_series.iloc[-1]) if not np.isnan(sma20_series.iloc[-1]) else None
        bull_ext = (last_price < sma20 * 0.92) if sma20 else False
        bear_ext = (last_price > sma20 * 1.08) if sma20 else False

        # 11. Volume on current bar vs 20-day average
        vol_sma20 = float(df['Volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else 0
        vol_above_avg = float(curr['Volume']) > vol_sma20 if vol_sma20 > 0 else False

        # 12. Parabolic regime detection — suppress reversal signals on massive moves
        price_5d_ago = float(df['Close'].iloc[-6]) if len(df) >= 6 else last_price
        move_5d_pct = ((last_price - price_5d_ago) / price_5d_ago) * 100
        is_parabolic_bull = move_5d_pct > 15   # stock surged → suppress bearish reversals
        is_parabolic_bear = move_5d_pct < -15  # stock crashed → suppress bullish reversals

        # 13. RSI overbought/oversold streak (duration filter)
        overbought_streak = 0
        for i in range(-1, -min(len(rsi_series), 10) - 1, -1):
            if float(rsi_series.iloc[i]) > 70:
                overbought_streak += 1
            else:
                break

        oversold_streak = 0
        for i in range(-1, -min(len(rsi_series), 10) - 1, -1):
            if float(rsi_series.iloc[i]) < 30:
                oversold_streak += 1
            else:
                break

        # 14. Current candle direction (for RVOL context)
        is_green_candle = curr['Close'] > curr['Open']

        # ═══════════════════════════════════════════════════════
        # WEIGHTED SCORING SYSTEM
        # ═══════════════════════════════════════════════════════
        
        MIN_SCORE = 5  # Lowered from 7 to include A-grade signals

        # --- BULLISH SCORE ---
        bull_score = 0
        bull_tags = []

        has_bull_pattern = patterns['hammer'] or patterns['bull_engulf'] or patterns['bottoming_tail']
        if has_bull_pattern and trend == "downtrend":
            bull_score += 3
            if patterns['hammer']: bull_tags.append("Hammer +3")
            if patterns['bull_engulf']: bull_tags.append("Bull Engulfing +3")
            if patterns['bottoming_tail']: bull_tags.append("Bottoming Tail +3")
        elif has_bull_pattern:
            bull_score += 2
            if patterns['hammer']: bull_tags.append("Hammer +2")
            if patterns['bull_engulf']: bull_tags.append("Bull Engulfing +2")
            if patterns['bottoming_tail']: bull_tags.append("Bottoming Tail +2")

        if rsi_val < 25 and oversold_streak >= 3:
            bull_score += 2; bull_tags.append(f"RSI {rsi_val:.0f} ({oversold_streak}d) +2")
        elif rsi_val < 30 and oversold_streak >= 3:
            bull_score += 1; bull_tags.append(f"RSI {rsi_val:.0f} ({oversold_streak}d) +1")

        if bull_div:
            bull_score += 3; bull_tags.append("RSI Divergence +3")
        if is_macd_bull_cross:
            bull_score += 2; bull_tags.append("MACD Cross +2")
        if bull_ext:
            bull_score += 1; bull_tags.append("Extension >8% +1")
        if rvol > 1.5 and not is_green_candle:
            bull_score += 1; bull_tags.append(f"RVOL {rvol:.1f}x +1")
        if has_bull_pattern:
            if near_52w_low:
                bull_score += 1; bull_tags.append("Near 52w Low +1")
            elif near_200sma:
                bull_score += 1; bull_tags.append("Near 200 SMA +1")
            elif trend == "downtrend":
                bull_score += 1; bull_tags.append("Prior Downtrend +1")
        if vol_above_avg and has_bull_pattern:
            bull_score += 1; bull_tags.append("Vol > Avg +1")

        # --- BEARISH SCORE ---
        bear_score = 0
        bear_tags = []

        has_bear_pattern = patterns['star'] or patterns['bear_engulf'] or patterns['topping_tail']
        if has_bear_pattern and trend == "uptrend":
            bear_score += 3
            if patterns['star']: bear_tags.append("Shooting Star +3")
            if patterns['bear_engulf']: bear_tags.append("Bear Engulfing +3")
            if patterns['topping_tail']: bear_tags.append("Topping Tail +3")
        elif has_bear_pattern:
            bear_score += 2
            if patterns['star']: bear_tags.append("Shooting Star +2")
            if patterns['bear_engulf']: bear_tags.append("Bear Engulfing +2")
            if patterns['topping_tail']: bear_tags.append("Topping Tail +2")

        if rsi_val > 75 and overbought_streak >= 3:
            bear_score += 2; bear_tags.append(f"RSI {rsi_val:.0f} ({overbought_streak}d) +2")
        elif rsi_val > 70 and overbought_streak >= 3:
            bear_score += 1; bear_tags.append(f"RSI {rsi_val:.0f} ({overbought_streak}d) +1")

        if bear_div:
            bear_score += 3; bear_tags.append("RSI Divergence +3")
        if is_macd_bear_cross:
            bear_score += 2; bear_tags.append("MACD Cross +2")
        if bear_ext:
            bear_score += 1; bear_tags.append("Extension >8% +1")
        if rvol > 1.5 and is_green_candle:
            bear_score += 1; bear_tags.append(f"RVOL {rvol:.1f}x +1")
        if has_bear_pattern:
            if near_52w_high:
                bear_score += 1; bear_tags.append("Near 52w High +1")
            elif near_200sma:
                bear_score += 1; bear_tags.append("Near 200 SMA +1")
            elif trend == "uptrend":
                bear_score += 1; bear_tags.append("Prior Uptrend +1")
        if vol_above_avg and has_bear_pattern:
            bear_score += 1; bear_tags.append("Vol > Avg +1")

        # --- UNUSUAL OPTIONS ACTIVITY (check if either side has potential) ---
        # Only fetch options data if the stock already shows some technical signals
        # to keep scan times reasonable (1 API call per check)
        # skip_options=True bypasses this entirely (used in full market scans)
        if not skip_options and (bull_score >= 4 or bear_score >= 4):
            bull_unusual, bear_unusual, opts_detail = detect_unusual_options(sym)
            if bull_unusual:
                bull_score += 2
                tag = f"Unusual Opts +2"
                if opts_detail:
                    tag = f"Unusual Opts ({opts_detail}) +2"
                bull_tags.append(tag)
            if bear_unusual:
                bear_score += 2
                tag = f"Unusual Opts +2"
                if opts_detail:
                    tag = f"Unusual Opts ({opts_detail}) +2"
                bear_tags.append(tag)

        news_details = None
        # --- NEWS CATALYST ---
        if bull_score >= 3 or bear_score >= 3:
            has_news, news_tag, news_item = detect_news_catalyst(sym)
            if has_news and news_tag:
                news_details = news_item
                if bull_score >= 3:
                    bull_score += 2
                    bull_tags.append(f"{news_tag} (+2)")
                if bear_score >= 3:
                    bear_score += 2
                    bear_tags.append(f"{news_tag} (+2)")

        # ═══════════════════════════════════════════════════════
        # SIGNAL DECISION — requires minimum score
        # ═══════════════════════════════════════════════════════

        is_bullish = bull_score >= MIN_SCORE
        is_bearish = bear_score >= MIN_SCORE

        # Parabolic regime gate — kill opposite-direction signals
        if is_parabolic_bull:
            is_bearish = False  # Can't short a parabolic rally
        if is_parabolic_bear:
            is_bullish = False  # Can't buy a parabolic crash

        # Candle pattern requirement — pure indicator signals are unreliable
        # Exception: score >= 7 (multiple strong independent signals) - Lowered from 9
        if is_bullish and not has_bull_pattern and bull_score < 7:
            is_bullish = False
        if is_bearish and not has_bear_pattern and bear_score < 7:
            is_bearish = False

        if not is_bullish and not is_bearish:
            return None

        # Use the stronger direction
        if is_bullish and is_bearish:
            if bull_score >= bear_score:
                is_bearish = False
            else:
                is_bullish = False

        score = bull_score if is_bullish else bear_score
        tags = bull_tags if is_bullish else bear_tags

        # Confidence grade
        if score >= 7:
            grade = "A+"
        elif score >= 5:
            grade = "A"
        else:
            grade = "B"

        reasons = f"[{' | '.join(tags)}]"

        # --- FIND BEST OPTION CONTRACT (only for A+ grade) ---
        opt = None
        if grade == "A+":
            opt = find_best_option(sym, "bullish" if is_bullish else "bearish", last_price)
        opt_str = f"{opt['exp']} ${opt['strike']} {opt['type']} (@${opt['mid']}, IV: {opt['iv']}%)" if opt else "—"

        # Filter out B-grades
        if grade not in ["A", "A+"]:
            return None

        return {
            "Ticker": sym,
            "Last Price": round(last_price, 2),
            "Volume": int(curr['Volume']),
            "RSI": round(rsi_val, 1),
            "Score": score,
            "Grade": grade,
            "Bullish Signals": reasons if is_bullish else "—",
            "Bearish Signals": reasons if is_bearish else "—",
            "Suggested Option": opt_str,
            "News Details": news_details
        }
    except Exception as e:
        print(f"  Error analyzing {sym}: {e}")
    return None


# =====================================================================
# Watchlist scanner  (original, fast)
# =====================================================================

def reversal_scanner(tickers, min_volume=500_000, min_price=5.0,
                     rsi_bull_thresh=35, rsi_bear_thresh=65,
                     swing_tolerance=0.05, extended_hours=False):
    """Scan a watchlist using direct Yahoo Finance API (cloud-safe)."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    log = scan_progress["debug_log"]

    results = []
    total = len(tickers)
    log.append(f"Starting watchlist scan: {total} tickers")

    # ── Phase 1: Download all tickers ────────────────────────
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)
    print(f"\n[Phase 1] Downloading {total} tickers via direct API...")

    def _on_dl_progress(i, tot, sym):
        _update_progress("downloading", f"Downloading {sym}...", i, tot,
                         ticker=sym, found=0)

    interval = "15m" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    # Need enough bars for 200 SMA on daily chart (same as full market scan)
    fetch_days = 60 if extended_hours else 280

    stock_data = fetch_batch_concurrent(
        tickers, days=fetch_days, max_workers=4,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=includePrePost
    )

    log.append(f"Downloaded: {len(stock_data)}/{total} tickers have data")
    print(f"  Downloaded {len(stock_data)}/{total} tickers")

    if not stock_data:
        log.append("No data returned — API may be blocking this server")
        scan_progress.update({
            "status": "done", "phase": "complete",
            "phase_label": "No data — API may be blocking this server",
            "current": total, "total": total,
            "found": 0, "pct": 100, "eta_seconds": 0,
        })
        return pd.DataFrame()

    # ── Phase 2: Analyze each ticker ─────────────────────────
    print(f"\n[Phase 2] Analyzing {len(stock_data)} tickers...")
    skipped_filter = 0

    for i, (sym, df) in enumerate(stock_data.items()):
        _update_progress("analyzing", f"Analyzing {sym}...", i, len(stock_data),
                         ticker=sym, found=len(results))

        try:
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())

            last_price = float(df['Close'].iloc[-1])

            if recent_vol < min_volume or last_price < min_price:
                print(f"  [{i+1}/{len(stock_data)}] {sym}... skip (vol={recent_vol:.0f}, price={last_price:.2f})")
                skipped_filter += 1
                continue

            result = _analyze_stock(sym, df, rsi_bull_thresh, rsi_bear_thresh, swing_tolerance)
            if result:
                results.append(result)
                print(f"  [{i+1}/{len(stock_data)}] {sym}... ✓ signal")
            else:
                print(f"  [{i+1}/{len(stock_data)}] {sym}... no signal")
        except Exception as e:
            print(f"  [{i+1}/{len(stock_data)}] {sym}... error ({e})")
            continue

    # ── Done ─────────────────────────────────────────────────
    total_time = time.time() - start_time
    summary = (f"Done in {total_time:.1f}s: {len(results)} signals, "
               f"{skipped_filter} filtered out")
    log.append(summary)
    print(f"\n[Done] {summary}")

    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Volume", ascending=False)


# =====================================================================
# Full market scanner  (batch download, pre-filter, then analyze)
# =====================================================================

def full_market_scan(min_volume=500_000, min_price=5.0,
                     rsi_bull_thresh=35, rsi_bear_thresh=65,
                     swing_tolerance=0.05, extended_hours=False):
    """
    Scan the entire US stock market:
      1. Fetch all US ticker symbols
      2. Download price data concurrently via direct API
      3. Pre-filter by volume & price
      4. Run full reversal analysis on candidates
    """
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    log = scan_progress["debug_log"]

    # ── Phase 1: Get ticker list ────────────────────────────
    _update_progress("fetching_tickers", "Fetching ticker list...", 0, 1)
    print("\n[Phase 1] Fetching US ticker list...")
    all_tickers = get_us_tickers()

    if not all_tickers:
        scan_progress["status"] = "error"
        scan_progress["phase_label"] = "Failed to fetch ticker list"
        return pd.DataFrame()

    total_tickers = len(all_tickers)
    log.append(f"Found {total_tickers} US tickers")
    print(f"\n[Phase 2] Downloading {total_tickers} tickers via direct API...")

    # ── Phase 2: Download all tickers concurrently ──────────
    def _on_dl_progress(done, tot, sym):
        _update_progress("downloading",
                         f"Downloading... ({done}/{tot})",
                         done, tot,
                         ticker=sym, found=0)
        elapsed = time.time() - start_time
        if done > 0:
            rate = elapsed / done
            remaining = (tot - done) * rate
            scan_progress["eta_seconds"] = int(remaining)

    interval = "15m" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    # 260+ days ensures we have a full year of data for the 200 SMA
    fetch_days = 60 if extended_hours else 280 

    found_signals = []

    def process_ticker(sym, df):
        try:
            # Must have enough bars for SMA 200 (if Daily) or RSI/VWAP (if Intraday)
            if len(df) < 50:
                return None
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            price = float(df['Close'].iloc[-1])
            if recent_vol >= min_volume and price >= min_price:
                result = _analyze_stock(sym, df, rsi_bull_thresh, rsi_bear_thresh, swing_tolerance, skip_options=True)
                if result:
                    found_signals.append(sym)
                    return result
        except Exception:
            pass
        return None

    def _on_dl_progress(done, tot, sym):
        _update_progress("downloading",
                         f"Downloading & Analyzing... ({done}/{tot})",
                         done, tot,
                         ticker=sym, found=len(found_signals))
        elapsed = time.time() - start_time
        if done > 0:
            rate = elapsed / done
            remaining = (tot - done) * rate
            scan_progress["eta_seconds"] = int(remaining)

    # Use max_workers=8 for faster concurrent downloads (I/O-bound)
    stock_results = fetch_batch_concurrent(
        all_tickers, days=fetch_days, max_workers=8,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=includePrePost,
        process_fn=process_ticker
    )

    results = [r for r in stock_results.values() if r is not None]

    # ── Done ────────────────────────────────────────────────
    total_time = time.time() - start_time
    summary = f"Done in {total_time:.0f}s: {len(results)} signals found out of {total_tickers} tickers scanned"
    log.append(summary)
    print(f"\n[Done] {summary}")

    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} signals found",
        "current": total_tickers, "total": total_tickers,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Volume", ascending=False)


# =====================================================================
# Momentum Analysis Scoring
# =====================================================================

def _analyze_momentum(sym, df):
    """
    Momentum/Breakout scoring system.
    Identifies stocks with strong directional thrust (like the INTC rally).
    """
    try:
        if len(df) < 50: return None
        
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        last_price = float(curr['Close'])
        open_price = float(curr['Open'])
        prev_close = float(prev['Close'])
        
        # 1. Metadata
        fiftyTwoWeekHigh = df.attrs.get("fiftyTwoWeekHigh")
        fiftyTwoWeekLow = df.attrs.get("fiftyTwoWeekLow")
        
        # Yesterday's High/Low for "Gap and Go" detection
        yesterday_high = float(prev['High'])
        yesterday_low = float(prev['Low'])
        
        # 2. RVOL (20-day avg)
        rvol = compute_rvol(df)
        
        # 3. RSI
        rsi_series = compute_rsi(df['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])
        
        # 4. Moving Averages
        sma20 = float(compute_sma(df['Close'], 20).iloc[-1])
        sma50 = float(compute_sma(df['Close'], 50).iloc[-1])
        sma200 = float(compute_sma(df['Close'], 200).iloc[-1])
        
        # 5. MACD
        macd_line, signal_line, macd_hist = compute_macd(df['Close'])
        macd_val = float(macd_line.iloc[-1])
        sig_val = float(signal_line.iloc[-1])
        hist_val = float(macd_hist.iloc[-1])

        # 6. Returns
        day_chg_pct = ((last_price - prev_close) / prev_close) * 100
        gap_pct = ((open_price - prev_close) / prev_close) * 100
        five_day_ret = ((last_price - df['Close'].iloc[-max(len(df), 6)]) / df['Close'].iloc[-max(len(df), 6)]) * 100 if len(df) >= 6 else 0
        
        # 7. Prior Session High/Low (Robust detection for intraday bars)
        # Find the high/low of the ACTUAL previous trading day
        dates = df.index.date
        unique_dates = sorted(list(set(dates)))
        if len(unique_dates) >= 2:
            # unique_dates[-1] is today, unique_dates[-2] is the previous session
            prior_date = unique_dates[-2]
            prior_day_df = df[df.index.date == prior_date]
            prior_high = float(prior_day_df['High'].max())
            prior_low = float(prior_day_df['Low'].min())
        else:
            # Fallback to previous candle if only one day of data exists
            prior_high = float(prev['High'])
            prior_low = float(prev['Low'])

        # 8. Candle Shape
        total_range = curr['High'] - curr['Low'] if curr['High'] != curr['Low'] else 0.01
        upper_wick = curr['High'] - max(curr['Close'], curr['Open'])
        lower_wick = min(curr['Close'], curr['Open']) - curr['Low']
        is_green = last_price > open_price
        
        MIN_MOMENTUM_SCORE = 5

        # --- BULLISH MOMENTUM (BREAKOUT) ---
        bull_score = 0
        bull_tags = []

        if gap_pct > 3.0:
            bull_score += 2; bull_tags.append(f"Gap Up {gap_pct:.1f}% +2")
        
        if last_price > prior_high:
            bull_score += 3; bull_tags.append("Broke Prior Day High +3")
        elif last_price > prior_high * 0.995:
             bull_score += 1; bull_tags.append("Near Prior Day High +1")

        if fiftyTwoWeekHigh and last_price >= fiftyTwoWeekHigh * 0.98:
            bull_score += 3; bull_tags.append("At 52w High +3")
        
        if rvol > 2.0:
            bull_score += 2; bull_tags.append(f"RVOL {rvol:.1f}x +2")
            if rvol > 3.0:
                bull_score += 1; bull_tags.append("Extreme Vol +1")
        
        if rsi_val > 70:
            bull_score += 1; bull_tags.append(f"RSI {rsi_val:.0f} (Strength) +1")
            
        if last_price > sma20 and last_price > sma50 and last_price > sma200:
            bull_score += 1; bull_tags.append("Above SMAs +1")
            
        if hist_val > 0 and macd_val > sig_val:
            bull_score += 1; bull_tags.append("MACD Bullish +1")
            
        if five_day_ret > 10:
            bull_score += 1; bull_tags.append(f"5d Ret {five_day_ret:.0f}% +1")
            
        if is_green and upper_wick < 0.2 * total_range:
            bull_score += 1; bull_tags.append("Strong Close +1")

        # --- BEARISH MOMENTUM (BREAKDOWN) ---
        bear_score = 0
        bear_tags = []

        if gap_pct < -3.0:
            bear_score += 2; bear_tags.append(f"Gap Down {abs(gap_pct):.1f}% +2")
            
        if last_price < prior_low:
            bear_score += 3; bear_tags.append("Broke Prior Day Low +3")
        elif last_price < prior_low * 1.005:
            bear_score += 1; bear_tags.append("Near Prior Day Low +1")

        if fiftyTwoWeekLow and last_price <= fiftyTwoWeekLow * 1.02:
            bear_score += 3; bear_tags.append("At 52w Low +3")
            
        if rvol > 2.0:
            bear_score += 2; bear_tags.append(f"RVOL {rvol:.1f}x +2")
            if rvol > 3.0:
                bear_score += 1; bear_tags.append("Extreme Vol +1")
                
        if rsi_val < 30:
            bear_score += 1; bear_tags.append(f"RSI {rsi_val:.0f} (Weakness) +1")
            
        if last_price < sma20 and last_price < sma50 and last_price < sma200:
            bear_score += 1; bear_tags.append("Below SMAs +1")
            
        if hist_val < 0 and macd_val < sig_val:
            bear_score += 1; bear_tags.append("MACD Bearish +1")
            
        if five_day_ret < -10:
            bear_score += 1; bear_tags.append(f"5d Ret {five_day_ret:.0f}% +1")
            
        if not is_green and lower_wick < 0.2 * total_range:
            bear_score += 1; bear_tags.append("Weak Close +1")

        news_details = None
        # --- NEWS CATALYST ---
        if bull_score >= 3 or bear_score >= 3:
            has_news, news_tag, news_item = detect_news_catalyst(sym)
            if has_news and news_tag:
                news_details = news_item
                if bull_score >= 3:
                    bull_score += 2
                    bull_tags.append(f"{news_tag} (+2)")
                if bear_score >= 3:
                    bear_score += 2
                    bear_tags.append(f"{news_tag} (+2)")

        # --- DECISION ---
        is_bullish = bull_score >= MIN_MOMENTUM_SCORE
        is_bearish = bear_score >= MIN_MOMENTUM_SCORE

        if not is_bullish and not is_bearish:
            return None

        # Use stronger direction
        if is_bullish and is_bearish:
            if bull_score >= bear_score: is_bearish = False
            else: is_bullish = False

        score = bull_score if is_bullish else bear_score
        tags = bull_tags if is_bullish else bear_tags
        
        if score >= 8: grade = "A+"
        elif score >= 6: grade = "A"
        else: grade = "B"

        reasons = f"[{' | '.join(tags)}]"

        # Filter out B-grades
        if grade not in ["A", "A+"]:
            return None

        return {
            "Ticker": sym,
            "Last Price": round(last_price, 2),
            "Volume": int(curr['Volume']),
            "RSI": round(rsi_val, 1),
            "Score": score,
            "Grade": grade,
            "Bullish Signals": reasons if is_bullish else "—",
            "Bearish Signals": reasons if is_bearish else "—",
            "Suggested Option": "—", # Momentum trades usually need different strategy
            "News Details": news_details
        }
    except Exception as e:
        print(f"  Error analyzing momentum for {sym}: {e}")
    return None

# =====================================================================
# Momentum Scanners
# =====================================================================

def momentum_watchlist_scan(tickers, min_volume=500_000, min_price=5.0, extended_hours=False):
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    
    results = []
    total = len(tickers)
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)
    
    def _on_dl_progress(i, tot, sym):
        _update_progress("downloading", f"Downloading {sym}...", i, tot, ticker=sym, found=len(results))

    interval = "15m" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    fetch_days = 14 if extended_hours else 280

    stock_data = fetch_batch_concurrent(
        tickers, days=fetch_days, max_workers=4,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=includePrePost
    )

    for i, (sym, df) in enumerate(stock_data.items()):
        _update_progress("analyzing", f"Analyzing {sym}...", i, len(stock_data), ticker=sym, found=len(results))
        try:
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            last_price = float(df['Close'].iloc[-1])

            if recent_vol < min_volume or last_price < min_price:
                continue

            result = _analyze_momentum(sym, df)
            if result:
                results.append(result)
        except:
            continue

    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} momentum signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })
    
    if not results: return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False)

def momentum_full_market_scan(min_volume=500_000, min_price=5.0, extended_hours=False):
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    
    all_tickers = get_us_tickers()
    if not all_tickers: return pd.DataFrame()
    
    total_tickers = len(all_tickers)
    
    def _on_dl_progress(done, tot, sym):
        _update_progress("downloading", f"Downloading... ({done}/{tot})", done, tot, ticker=sym, found=0)
        elapsed = time.time() - start_time
        if done > 0:
            rate = elapsed / done
            scan_progress["eta_seconds"] = int((tot - done) * rate)

    interval = "15m" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    fetch_days = 14 if extended_hours else 280 

    stock_data = fetch_batch_concurrent(
        all_tickers, days=fetch_days, max_workers=8, 
        on_progress=_on_dl_progress, delay=0.05, 
        interval=interval, includePrePost=includePrePost
    )

    candidates = []
    for sym, df in stock_data.items():
        try:
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            price = float(df['Close'].iloc[-1])
            if recent_vol >= min_volume and price >= min_price:
                candidates.append((sym, df))
        except: continue

    results = []
    total_candidates = len(candidates)
    phase3_start = time.time()

    for j, (sym, df) in enumerate(candidates):
        _update_progress("analyzing", f"Analyzing {sym}...", j, total_candidates, ticker=sym, found=len(results))
        elapsed = time.time() - phase3_start
        if j > 0:
            rate = elapsed / j
            scan_progress["eta_seconds"] = int((total_candidates - j) * rate)

        result = _analyze_momentum(sym, df)
        if result: results.append(result)

    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} momentum signals found",
        "current": total_candidates, "total": total_candidates,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })
    
    if not results: return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False)

# =====================================================================
# IV Rank Tracker  (DIY — logs ATM IV per ticker per day)
# =====================================================================

IV_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "iv_history.json")
_IV_MAX_ENTRIES = 252  # 1 trading year

def _load_iv_history():
    """Load IV history from disk."""
    if os.path.exists(IV_HISTORY_FILE):
        try:
            with open(IV_HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def _save_iv_history(history):
    """Save IV history to disk."""
    try:
        with open(IV_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=1)
    except Exception as e:
        print(f"  Failed to save IV history: {e}")

def _update_iv_history(ticker, current_iv, history):
    """
    Record today's ATM IV for a ticker.
    Caps at _IV_MAX_ENTRIES per ticker (rolling window).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if ticker not in history:
        history[ticker] = {}
    history[ticker][today] = round(current_iv, 4)
    # Trim to max entries (keep most recent)
    if len(history[ticker]) > _IV_MAX_ENTRIES:
        sorted_dates = sorted(history[ticker].keys())
        for old_date in sorted_dates[:len(history[ticker]) - _IV_MAX_ENTRIES]:
            del history[ticker][old_date]

def _compute_iv_rank(ticker, current_iv, history):
    """
    Compute IV Rank as a percentile (0–100).
    Returns None if insufficient history (< 5 data points).
    """
    if ticker not in history or len(history[ticker]) < 5:
        return None
    iv_values = list(history[ticker].values())
    iv_low = min(iv_values)
    iv_high = max(iv_values)
    if iv_high == iv_low:
        return 50.0  # Flat IV — neutral
    rank = ((current_iv - iv_low) / (iv_high - iv_low)) * 100
    return round(max(0, min(100, rank)), 1)


# =====================================================================
# Options Setup Analyzer
# =====================================================================

def _get_atm_iv(chain_data, last_price, side="calls"):
    """
    Find the ATM implied volatility from a chain.
    Returns the IV of the strike closest to last_price.
    """
    contracts = chain_data.get(side, [])
    if not contracts:
        return None
    best = None
    best_dist = float('inf')
    for c in contracts:
        strike = c.get("strike", 0)
        iv = c.get("impliedVolatility", 0)
        if iv and iv > 0:
            dist = abs(strike - last_price)
            if dist < best_dist:
                best_dist = dist
                best = iv
    return best

def _analyze_options_setup(sym, df, iv_history):
    """
    Two-phase options setup analysis:
      Phase A: Lightweight momentum+reversal pre-screen (score >= 3)
      Phase B: Options chain scan with all 6 filters:
        1. Liquidity (Volume >= 50, OI >= 100, Spread < 15%)
        2. DTE 20-60
        3. Delta 0.30-0.70 (strike distance approximation)
        4. IV Rank < 30% (from DIY tracker)
        5. Stock momentum/catalyst alignment (Phase A score)
        6. Unusual options flow confirmation
    """
    try:
        if len(df) < 20:
            return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        last_price = float(curr['Close'])
        open_price = float(curr['Open'])
        prev_close = float(prev['Close'])

        # ── Phase A: Quick Catalyst Pre-Screen ──────────────
        rsi_series = compute_rsi(df['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])
        macd_line, signal_line, macd_hist = compute_macd(df['Close'])
        patterns = detect_patterns(df)
        trend = get_trend_context(df, days=5)
        rvol = compute_rvol(df)
        day_chg_pct = ((last_price - prev_close) / prev_close) * 100

        # Bullish catalyst score
        bull_catalyst = 0
        bull_reasons = []
        if patterns['hammer'] or patterns['bull_engulf'] or patterns['bottoming_tail']:
            bull_catalyst += 2; bull_reasons.append("Candle Pattern")
        if rsi_val < 40:
            bull_catalyst += 1; bull_reasons.append(f"RSI {rsi_val:.0f}")
        bull_div, bear_div = detect_rsi_divergence(df['Close'], rsi_series, lookback=20)
        if bull_div:
            bull_catalyst += 2; bull_reasons.append("RSI Divergence")
        if float(macd_hist.iloc[-1]) > 0 and float(macd_hist.iloc[-2]) < 0:
            bull_catalyst += 1; bull_reasons.append("MACD Cross")
        if float(macd_hist.iloc[-1]) > 0:
            bull_catalyst += 1; bull_reasons.append("MACD Bullish")
        if rvol > 1.3:
            bull_catalyst += 1; bull_reasons.append(f"RVOL {rvol:.1f}x")
        if day_chg_pct > 1.5:
            bull_catalyst += 1; bull_reasons.append(f"Day +{day_chg_pct:.1f}%")
        if trend == "downtrend" and (patterns['hammer'] or patterns['bull_engulf']):
            bull_catalyst += 1; bull_reasons.append("Reversal Context")
        # SMA trend alignment
        sma20 = df['Close'].rolling(20).mean().iloc[-1]
        if last_price > float(sma20):
            bull_catalyst += 1; bull_reasons.append("Above SMA20")
        if trend == "uptrend":
            bull_catalyst += 1; bull_reasons.append("Uptrend")

        # Bearish catalyst score
        bear_catalyst = 0
        bear_reasons = []
        if patterns['star'] or patterns['bear_engulf'] or patterns['topping_tail']:
            bear_catalyst += 2; bear_reasons.append("Candle Pattern")
        if rsi_val > 60:
            bear_catalyst += 1; bear_reasons.append(f"RSI {rsi_val:.0f}")
        if bear_div:
            bear_catalyst += 2; bear_reasons.append("RSI Divergence")
        if float(macd_hist.iloc[-1]) < 0 and float(macd_hist.iloc[-2]) > 0:
            bear_catalyst += 1; bear_reasons.append("MACD Cross")
        if float(macd_hist.iloc[-1]) < 0:
            bear_catalyst += 1; bear_reasons.append("MACD Bearish")
        if rvol > 1.3:
            bear_catalyst += 1; bear_reasons.append(f"RVOL {rvol:.1f}x")
        if day_chg_pct < -1.5:
            bear_catalyst += 1; bear_reasons.append(f"Day {day_chg_pct:.1f}%")
        if trend == "uptrend" and (patterns['star'] or patterns['bear_engulf']):
            bear_catalyst += 1; bear_reasons.append("Reversal Context")
        # SMA trend alignment
        if last_price < float(sma20):
            bear_catalyst += 1; bear_reasons.append("Below SMA20")
        if trend == "downtrend":
            bear_catalyst += 1; bear_reasons.append("Downtrend")

        news_details = None
        # --- NEWS CATALYST ---
        if bull_catalyst >= 3 or bear_catalyst >= 3:
            has_news, news_tag, news_item = detect_news_catalyst(sym)
            if has_news and news_tag:
                news_details = news_item
                if bull_catalyst >= 3:
                    bull_catalyst += 2
                    bull_reasons.append(f"{news_tag} (+2)")
                if bear_catalyst >= 3:
                    bear_catalyst += 2
                    bear_reasons.append(f"{news_tag} (+2)")

        # Need at least score 4 on one side to proceed (raised from 3 to filter out B-grades)
        max_catalyst = max(bull_catalyst, bear_catalyst)
        print(f"  {sym}: Bull={bull_catalyst} Bear={bear_catalyst} RSI={rsi_val:.1f} Chg={day_chg_pct:.1f}%")
        if max_catalyst < 4:
            return None

        # Determine dominant direction
        if bull_catalyst >= bear_catalyst:
            direction = "bullish"
            catalyst_score = bull_catalyst
            catalyst_tags = bull_reasons
        else:
            direction = "bearish"
            catalyst_score = bear_catalyst
            catalyst_tags = bear_reasons

        # ── Phase B: Options Chain Analysis ─────────────────
        chain_meta = fetch_options_chain(sym)
        if not chain_meta:
            return None

        now = time.time()

        # Step 1: Get ATM IV for IV Rank tracking
        first_chain = chain_meta.get("firstChain", {})
        side = "calls" if direction == "bullish" else "puts"
        atm_iv = _get_atm_iv(first_chain, last_price, side)
        if atm_iv and atm_iv > 0:
            _update_iv_history(sym, atm_iv, iv_history)

        # Step 2: IV Rank filter
        iv_rank = None
        if atm_iv and atm_iv > 0:
            iv_rank = _compute_iv_rank(sym, atm_iv, iv_history)
            if iv_rank is not None and iv_rank > 30:
                return None  # IV too high — skip

        # Step 3: Find valid expirations (DTE 20-60)
        valid_exps = []
        for exp in chain_meta.get("expirations", []):
            dte = (exp - now) / 86400
            if 20 <= dte <= 60:
                valid_exps.append(exp)

        if not valid_exps:
            return None

        # Step 4: Scan contracts with all filters
        best_contract = None

        for exp_ts in valid_exps:
            # Try to get the chain from our efficient allChains cache first
            # (bypassing the slow network API call entirely)
            all_chains = chain_meta.get("allChains", {})
            chain = all_chains.get(exp_ts)
            
            # Webull often returns empty pricing for far-out options in allChains after hours.
            # Check if the chain is valid (has at least one bid/ask)
            has_data = False
            if chain:
                for c in chain.get("calls", [])[:10]:
                    if c.get("bid") is not None or c.get("ask") is not None:
                        has_data = True
                        break
                        
            if not chain or not has_data:
                # Webull data is empty/missing pricing — fall back directly to Yahoo
                from data_fetcher import _fetch_yahoo_options_chain, _fetch_yahoo_options_for_expiration
                if "yahoo_meta" not in chain_meta:
                    chain_meta["yahoo_meta"] = _fetch_yahoo_options_chain(sym)
                    
                yahoo_meta = chain_meta.get("yahoo_meta")
                if yahoo_meta:
                    # Fuzzy match the expiration timestamp (find closest Yahoo timestamp)
                    closest_yahoo_exp = None
                    min_diff = 999999
                    for y_exp in yahoo_meta.get("expirations", []):
                        diff = abs(y_exp - exp_ts)
                        if diff < min_diff:
                            min_diff = diff
                            closest_yahoo_exp = y_exp
                            
                    if closest_yahoo_exp and min_diff < 86400 * 4: # within 4 days
                        chain = _fetch_yahoo_options_for_expiration(sym, closest_yahoo_exp)
                
            if not chain:
                continue

            contracts = chain.get(side, [])

            for c in contracts:
                strike = c.get("strike", 0)
                vol = c.get("volume", 0) or 0
                oi = c.get("openInterest", 0) or 0
                bid = c.get("bid", 0) or 0
                ask = c.get("ask", 0) or 0
                iv = c.get("impliedVolatility", 0) or 0

                # Filter 1: Liquidity
                if vol < 50 or oi < 100:
                    continue

                mid = (bid + ask) / 2
                if mid <= 0:
                    continue
                spread_pct = ((ask - bid) / mid) * 100
                if spread_pct > 15:
                    continue  # Spread too wide

                # Filter 3: Delta approximation (0.30-0.70)
                dist_pct = (strike - last_price) / last_price
                is_valid_delta = False
                if direction == "bullish":
                    # Call: 0.70Δ ≈ 5-7% ITM, 0.30Δ ≈ 3-5% OTM
                    if -0.07 <= dist_pct <= 0.05:
                        is_valid_delta = True
                else:
                    # Put: 0.70Δ ≈ 5-7% ITM, 0.30Δ ≈ 3-5% OTM
                    if -0.05 <= dist_pct <= 0.07:
                        is_valid_delta = True

                if not is_valid_delta:
                    continue

                # Estimate delta from distance
                abs_dist = abs(dist_pct)
                if direction == "bullish":
                    est_delta = 0.50 + (dist_pct * -10)  # ITM increases delta
                else:
                    est_delta = 0.50 + (dist_pct * 10)
                est_delta = max(0.30, min(0.80, est_delta))

                # Score: prefer higher liquidity
                score = vol + oi
                dte_days = int((exp_ts - now) / 86400)

                if not best_contract or score > best_contract["_score"]:
                    best_contract = {
                        "symbol": c.get("contractSymbol", ""),
                        "strike": strike,
                        "type": "CALL" if direction == "bullish" else "PUT",
                        "exp": datetime.fromtimestamp(exp_ts).strftime("%b %d"),
                        "dte": dte_days,
                        "mid": round(mid, 2),
                        "bid": round(bid, 2),
                        "ask": round(ask, 2),
                        "iv": round(iv * 100, 1),
                        "volume": vol,
                        "oi": oi,
                        "spread_pct": round(spread_pct, 1),
                        "est_delta": round(est_delta, 2),
                        "_score": score,
                    }

            if best_contract:
                break  # Found a good contract in this expiration

        if not best_contract:
            return None

        # Step 5: Unusual options flow check — reuse chain_meta already fetched above
        #         instead of calling detect_unusual_options() which would fetch it again
        bull_unusual = False
        bear_unusual = False
        flow_detail = ""
        first_chain = chain_meta.get("firstChain", {})
        if first_chain:
            _calls = first_chain.get("calls", [])
            _puts = first_chain.get("puts", [])
            total_call_vol = 0
            total_put_vol = 0
            unusual_call_contracts = 0
            unusual_put_contracts = 0
            max_call_vol_oi = 0.0
            max_put_vol_oi = 0.0

            for c in _calls:
                _vol = (c.get("volume", 0) or 0)
                _oi = (c.get("openInterest", 0) or 0)
                total_call_vol += _vol
                if _oi > 50 and _vol > 100:
                    _ratio = _vol / _oi
                    if _ratio > 2.0:
                        unusual_call_contracts += 1
                        max_call_vol_oi = max(max_call_vol_oi, _ratio)

            for p in _puts:
                _vol = (p.get("volume", 0) or 0)
                _oi = (p.get("openInterest", 0) or 0)
                total_put_vol += _vol
                if _oi > 50 and _vol > 100:
                    _ratio = _vol / _oi
                    if _ratio > 2.0:
                        unusual_put_contracts += 1
                        max_put_vol_oi = max(max_put_vol_oi, _ratio)

            _total_vol = total_call_vol + total_put_vol
            if _total_vol >= 500:
                _call_pct = total_call_vol / _total_vol if _total_vol > 0 else 0.5
                bull_unusual = (
                    (unusual_call_contracts >= 2 and _call_pct > 0.60) or
                    (unusual_call_contracts >= 3) or
                    (max_call_vol_oi >= 5.0 and _call_pct > 0.55)
                )
                bear_unusual = (
                    (unusual_put_contracts >= 2 and _call_pct < 0.40) or
                    (unusual_put_contracts >= 3) or
                    (max_put_vol_oi >= 5.0 and _call_pct < 0.45)
                )
                _details = []
                if bull_unusual:
                    _details.append(f"Calls {_call_pct*100:.0f}%")
                    if max_call_vol_oi >= 3.0: _details.append(f"V/OI {max_call_vol_oi:.1f}x")
                if bear_unusual:
                    _details.append(f"Puts {(1-_call_pct)*100:.0f}%")
                    if max_put_vol_oi >= 3.0: _details.append(f"V/OI {max_put_vol_oi:.1f}x")
                flow_detail = ", ".join(_details)

        has_unusual_flow = (bull_unusual if direction == "bullish" else bear_unusual)
        flow_str = flow_detail if has_unusual_flow else ""

        # Build result
        iv_rank_str = f"{iv_rank:.0f}%" if iv_rank is not None else "Building..."
        catalyst_str = " | ".join(catalyst_tags)

        return {
            "Ticker": sym,
            "Last Price": round(last_price, 2),
            "Direction": direction.capitalize(),
            "Catalyst Score": catalyst_score,
            "Catalyst Tags": catalyst_str,
            "Contract": f"{best_contract['exp']} ${best_contract['strike']} {best_contract['type']}",
            "Strike": best_contract["strike"],
            "Exp": best_contract["exp"],
            "Type": best_contract["type"],
            "DTE": best_contract["dte"],
            "Mid": best_contract["mid"],
            "Bid": best_contract["bid"],
            "Ask": best_contract["ask"],
            "IV": best_contract["iv"],
            "IV Rank": iv_rank_str,
            "IV Rank Value": iv_rank if iv_rank is not None else -1,
            "Volume": best_contract["volume"],
            "OI": best_contract["oi"],
            "Spread": f"{best_contract['spread_pct']}%",
            "Est Delta": best_contract["est_delta"],
            "Unusual Flow": has_unusual_flow,
            "Flow Detail": flow_str,
            "RSI": round(rsi_val, 1),
            "News Details": news_details
        }
    except Exception as e:
        print(f"  Error analyzing options for {sym}: {e}")
    return None


# =====================================================================
# Options Scanners
# =====================================================================

def options_watchlist_scan(tickers, min_volume=500_000, min_price=5.0, extended_hours=False):
    """Scan watchlist tickers for options setups."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    iv_history = _load_iv_history()

    results = []
    total = len(tickers)
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)

    def _on_dl_progress(i, tot, sym):
        _update_progress("downloading", f"Downloading {sym}...", i, tot, ticker=sym, found=len(results))

    stock_data = fetch_batch_concurrent(
        tickers, days=280, max_workers=4,
        on_progress=_on_dl_progress, delay=0.05, interval="1d"
    )

    for i, (sym, df) in enumerate(stock_data.items()):
        _update_progress("analyzing", f"Analyzing {sym} options...", i, len(stock_data), ticker=sym, found=len(results))
        try:
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            last_price = float(df['Close'].iloc[-1])
            if recent_vol < min_volume or last_price < min_price:
                continue
            result = _analyze_options_setup(sym, df, iv_history)
            if result:
                results.append(result)
        except:
            continue

    # Save updated IV history
    _save_iv_history(iv_history)

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} options setups found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Catalyst Score", ascending=False)


def options_full_market_scan(min_volume=500_000, min_price=5.0, extended_hours=False):
    """Scan full market for options setups."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    iv_history = _load_iv_history()

    all_tickers = get_us_tickers()
    if not all_tickers:
        scan_progress["status"] = "error"
        scan_progress["phase_label"] = "Failed to fetch ticker list"
        return pd.DataFrame()

    total_tickers = len(all_tickers)

    found_setups = []

    def process_options(sym, df):
        try:
            if len(df) < 50:
                return None
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            price = float(df['Close'].iloc[-1])
            if recent_vol >= min_volume and price >= min_price:
                result = _analyze_options_setup(sym, df, iv_history)
                if result:
                    found_setups.append(sym)
                    return result
        except Exception:
            pass
        return None

    def _on_dl_progress(done, tot, sym):
        _update_progress("downloading", f"Downloading & Analyzing... ({done}/{tot})", done, tot, ticker=sym, found=len(found_setups))
        elapsed = time.time() - start_time
        if done > 0:
            rate = elapsed / done
            scan_progress["eta_seconds"] = int((tot - done) * rate)

    stock_results = fetch_batch_concurrent(
        all_tickers, days=280, max_workers=8,
        on_progress=_on_dl_progress, delay=0.05, interval="1d",
        process_fn=process_options
    )

    results = [r for r in stock_results.values() if r is not None]

    # Save updated IV history
    _save_iv_history(iv_history)

    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} options setups found",
        "current": total_tickers, "total": total_tickers,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Catalyst Score", ascending=False)


# =====================================================================
# Breakout / Breakdown & Gap Scanner — Analysis
# =====================================================================

def _analyze_breakout_setup(sym, df):
    """
    Breakout/Breakdown & Gap scoring system.
    Detects stocks ready to break out of consolidation, gap up/down,
    or break down through support using multi-factor confirmation.
    """
    try:
        if len(df) < 50:
            return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        last_price = float(curr['Close'])
        open_price = float(curr['Open'])
        prev_close = float(prev['Close'])

        # Metadata
        fiftyTwoWeekHigh = df.attrs.get("fiftyTwoWeekHigh")
        fiftyTwoWeekLow = df.attrs.get("fiftyTwoWeekLow")

        # Prior session high/low (robust multi-day detection)
        dates = df.index.date
        unique_dates = sorted(list(set(dates)))
        if len(unique_dates) >= 2:
            prior_date = unique_dates[-2]
            prior_day_df = df[df.index.date == prior_date]
            prior_high = float(prior_day_df['High'].max())
            prior_low = float(prior_day_df['Low'].min())
        else:
            prior_high = float(prev['High'])
            prior_low = float(prev['Low'])

        # Indicators
        rsi_series = compute_rsi(df['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])

        rvol = compute_rvol(df)

        macd_line, signal_line, macd_hist = compute_macd(df['Close'])
        macd_val = float(macd_line.iloc[-1])
        sig_val = float(signal_line.iloc[-1])
        hist_val = float(macd_hist.iloc[-1])

        ema10 = float(compute_ema(df['Close'], 10).iloc[-1])
        ema21 = float(compute_ema(df['Close'], 21).iloc[-1])
        ema50 = float(compute_ema(df['Close'], 50).iloc[-1])

        adr_pct = compute_adr_pct(df, 14)

        # Gap
        gap_pct = ((open_price - prev_close) / prev_close) * 100

        # Candle shape
        total_range = curr['High'] - curr['Low'] if curr['High'] != curr['Low'] else 0.01
        upper_wick = curr['High'] - max(curr['Close'], curr['Open'])
        lower_wick = min(curr['Close'], curr['Open']) - curr['Low']
        is_green = last_price > open_price
        range_position = (last_price - curr['Low']) / total_range  # 0=bottom, 1=top

        # Squeeze
        try:
            squeeze_on, squeeze_mom_positive, squeeze_mom_increasing = detect_squeeze(df)
        except Exception:
            squeeze_on, squeeze_mom_positive, squeeze_mom_increasing = False, False, False

        # Triangle patterns
        try:
            ascending_tri, descending_tri = detect_triangle(df, lookback=20)
        except Exception:
            ascending_tri, descending_tri = False, False

        # Accumulation / Distribution
        try:
            accum_days, distrib_days = count_distribution_accumulation(df, lookback=10)
        except Exception:
            accum_days, distrib_days = 0, 0

        # ATR contraction (consolidation proxy)
        try:
            atr_series = compute_atr(df, 14)
            atr_now = float(atr_series.iloc[-1])
            atr_20ago = float(atr_series.iloc[-20]) if len(atr_series) >= 20 else atr_now
            atr_contracting = atr_now < atr_20ago * 0.75  # ATR dropped 25%+
        except Exception:
            atr_contracting = False

        # Consolidation: price range in last 10 bars
        try:
            recent_10 = df.iloc[-10:]
            range_10d_pct = ((recent_10['High'].max() - recent_10['Low'].min()) / last_price) * 100
            is_tight_range = range_10d_pct < 4  # Price contained within 4%
        except Exception:
            is_tight_range = False
            range_10d_pct = 99

        MIN_BREAKOUT_SCORE = 12

        # ═══════════════════════════════════════════════════════
        # BULLISH BREAKOUT SCORE
        # ═══════════════════════════════════════════════════════
        bull_score = 0
        bull_tags = []

        # 1. Consolidation near resistance (+3)
        if is_tight_range and atr_contracting:
            bull_score += 3
            bull_tags.append(f"Consolidation ({range_10d_pct:.1f}% range) +3")
        elif is_tight_range:
            bull_score += 1
            bull_tags.append(f"Tight Range ({range_10d_pct:.1f}%) +1")

        # 2. TTM Squeeze (+3)
        if squeeze_on and squeeze_mom_positive:
            bull_score += 3
            bull_tags.append("Squeeze Fire 🔥 +3")
        elif squeeze_on:
            bull_score += 1
            bull_tags.append("Squeeze Building +1")

        # 3. Volume surge on up day (+2)
        if rvol > 2.0 and is_green:
            bull_score += 2
            bull_tags.append(f"RVOL {rvol:.1f}x +2")
            if rvol > 3.0:
                bull_score += 1
                bull_tags.append("Extreme Vol +1")
        elif rvol > 1.5 and is_green:
            bull_score += 1
            bull_tags.append(f"RVOL {rvol:.1f}x +1")

        # 4. Ascending triangle (+2)
        if ascending_tri:
            bull_score += 2
            bull_tags.append("Asc Triangle +2")

        # 5. EMA alignment: stacked bullishly (+2)
        if last_price > ema10 > ema21 > ema50:
            bull_score += 2
            bull_tags.append("EMA Stacked ↑ +2")
        elif last_price > ema10 and last_price > ema21:
            bull_score += 1
            bull_tags.append("Above EMAs +1")

        # 6. RSI in strength zone 55-70 (+1)
        if 55 <= rsi_val <= 70:
            bull_score += 1
            bull_tags.append(f"RSI {rsi_val:.0f} (Strength) +1")

        # 7. MACD bullish (+1)
        if hist_val > 0 and macd_val > sig_val:
            bull_score += 1
            bull_tags.append("MACD Bullish +1")

        # 8. Gap up > 2% (+2)
        if gap_pct > 2.0:
            bull_score += 2
            bull_tags.append(f"Gap Up {gap_pct:.1f}% +2")

        # 9. Near 52w high (+2)
        if fiftyTwoWeekHigh and last_price >= fiftyTwoWeekHigh * 0.95:
            bull_score += 2
            bull_tags.append("Near 52w High +2")

        # 10. ADR% > 3% (+1)
        if adr_pct > 3.0:
            bull_score += 1
            bull_tags.append(f"ADR {adr_pct:.1f}% +1")

        # 11. Strong candle close — upper 25% of range, small upper wick (+1)
        if is_green and range_position >= 0.75 and upper_wick < 0.2 * total_range:
            bull_score += 1
            bull_tags.append("Strong Close +1")

        # 12. Accumulation days (+1)
        if accum_days >= 3:
            bull_score += 1
            bull_tags.append(f"Accum {accum_days}d +1")

        # 13. Broke prior day high (+2)
        if last_price > prior_high:
            bull_score += 2
            bull_tags.append("Broke PDH +2")
        elif last_price > prior_high * 0.995:
            bull_score += 1
            bull_tags.append("Near PDH +1")

        # ═══════════════════════════════════════════════════════
        # BEARISH BREAKDOWN SCORE
        # ═══════════════════════════════════════════════════════
        bear_score = 0
        bear_tags = []

        # 1. Consolidation near support (+3)
        if is_tight_range and atr_contracting:
            bear_score += 3
            bear_tags.append(f"Consolidation ({range_10d_pct:.1f}% range) +3")
        elif is_tight_range:
            bear_score += 1
            bear_tags.append(f"Tight Range ({range_10d_pct:.1f}%) +1")

        # 2. TTM Squeeze — bearish (+3)
        if squeeze_on and not squeeze_mom_positive:
            bear_score += 3
            bear_tags.append("Squeeze Fire 🔥 +3")
        elif squeeze_on:
            bear_score += 1
            bear_tags.append("Squeeze Building +1")

        # 3. Volume surge on down day (+2)
        if rvol > 2.0 and not is_green:
            bear_score += 2
            bear_tags.append(f"RVOL {rvol:.1f}x +2")
            if rvol > 3.0:
                bear_score += 1
                bear_tags.append("Extreme Vol +1")
        elif rvol > 1.5 and not is_green:
            bear_score += 1
            bear_tags.append(f"RVOL {rvol:.1f}x +1")

        # 4. Descending triangle (+2)
        if descending_tri:
            bear_score += 2
            bear_tags.append("Desc Triangle +2")

        # 5. EMA alignment: stacked bearishly (+2)
        if last_price < ema10 < ema21 < ema50:
            bear_score += 2
            bear_tags.append("EMA Stacked ↓ +2")
        elif last_price < ema10 and last_price < ema21:
            bear_score += 1
            bear_tags.append("Below EMAs +1")

        # 6. RSI in weakness zone 30-45 (+1)
        if 30 <= rsi_val <= 45:
            bear_score += 1
            bear_tags.append(f"RSI {rsi_val:.0f} (Weakness) +1")

        # 7. MACD bearish (+1)
        if hist_val < 0 and macd_val < sig_val:
            bear_score += 1
            bear_tags.append("MACD Bearish +1")

        # 8. Gap down > 2% (+2)
        if gap_pct < -2.0:
            bear_score += 2
            bear_tags.append(f"Gap Down {abs(gap_pct):.1f}% +2")

        # 9. Near 52w low (+2)
        if fiftyTwoWeekLow and last_price <= fiftyTwoWeekLow * 1.05:
            bear_score += 2
            bear_tags.append("Near 52w Low +2")

        # 10. ADR% > 3% (+1)
        if adr_pct > 3.0:
            bear_score += 1
            bear_tags.append(f"ADR {adr_pct:.1f}% +1")

        # 11. Weak candle close — lower 25% of range, small lower wick (+1)
        if not is_green and range_position <= 0.25 and lower_wick < 0.2 * total_range:
            bear_score += 1
            bear_tags.append("Weak Close +1")

        # 12. Distribution days (+1)
        if distrib_days >= 4:
            bear_score += 1
            bear_tags.append(f"Distrib {distrib_days}d +1")

        # 13. Broke prior day low (+2)
        if last_price < prior_low:
            bear_score += 2
            bear_tags.append("Broke PDL +2")
        elif last_price < prior_low * 1.005:
            bear_score += 1
            bear_tags.append("Near PDL +1")

        news_details = None
        # --- NEWS CATALYST ---
        if bull_score >= 9 or bear_score >= 9:
            has_news, news_tag, news_item = detect_news_catalyst(sym)
            if has_news and news_tag:
                news_details = news_item
                if bull_score >= 9:
                    bull_score += 3
                    bull_tags.append(f"{news_tag} (+3)")
                if bear_score >= 9:
                    bear_score += 3
                    bear_tags.append(f"{news_tag} (+3)")

        # ═══════════════════════════════════════════════════════
        # SIGNAL DECISION
        # ═══════════════════════════════════════════════════════
        has_bull_anchor = (is_tight_range and atr_contracting) or (squeeze_on and squeeze_mom_positive)
        has_bear_anchor = (is_tight_range and atr_contracting) or (squeeze_on and not squeeze_mom_positive)

        near_52w_high = fiftyTwoWeekHigh and last_price >= fiftyTwoWeekHigh * 0.90
        near_52w_low = fiftyTwoWeekLow and last_price <= fiftyTwoWeekLow * 1.10

        is_bullish = bull_score >= MIN_BREAKOUT_SCORE and has_bull_anchor and near_52w_high
        is_bearish = bear_score >= MIN_BREAKOUT_SCORE and has_bear_anchor and near_52w_low

        if not is_bullish and not is_bearish:
            return None

        # Use stronger direction
        if is_bullish and is_bearish:
            if bull_score >= bear_score:
                is_bearish = False
            else:
                is_bullish = False

        score = bull_score if is_bullish else bear_score
        tags = bull_tags if is_bullish else bear_tags

        # Grading
        if score >= 10:
            grade = "A+"
        elif score >= 8:
            grade = "A"
        else:
            grade = "B"

        # Filter out B-grades
        if grade not in ["A", "A+"]:
            return None

        reasons = f"[{' | '.join(tags)}]"

        return {
            "Ticker": sym,
            "Last Price": round(last_price, 2),
            "Volume": int(curr['Volume']),
            "RSI": round(rsi_val, 1),
            "Score": score,
            "Grade": grade,
            "Bullish Signals": reasons if is_bullish else "—",
            "Bearish Signals": reasons if is_bearish else "—",
            "Suggested Option": "—",
            "News Details": news_details
        }
    except Exception as e:
        print(f"  Error analyzing breakout for {sym}: {e}")
    return None


# =====================================================================
# Breakout Scanners (Watchlist + Full Market)
# =====================================================================

def breakout_watchlist_scan(tickers, min_volume=2_000_000, min_price=10.0, extended_hours=False):
    """Scan watchlist for breakout/breakdown & gap setups."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()

    results = []
    total = len(tickers)
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)

    def _on_dl_progress(i, tot, sym):
        _update_progress("downloading", f"Downloading {sym}...", i, tot, ticker=sym, found=len(results))

    interval = "15m" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    fetch_days = 14 if extended_hours else 280

    stock_data = fetch_batch_concurrent(
        tickers, days=fetch_days, max_workers=4,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=includePrePost
    )

    for i, (sym, df) in enumerate(stock_data.items()):
        _update_progress("analyzing", f"Analyzing {sym}...", i, len(stock_data), ticker=sym, found=len(results))
        try:
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            last_price = float(df['Close'].iloc[-1])
            dollar_volume = recent_vol * last_price

            # Big Cap / High Liquidity filter (requires ~$150M+ traded today)
            if dollar_volume < 150_000_000:
                continue

            if recent_vol < min_volume or last_price < min_price:
                continue
            result = _analyze_breakout_setup(sym, df)
            if result:
                results.append(result)
        except:
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} breakout signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] Breakout watchlist scan: {len(results)} signals in {total_time:.1f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False)


def breakout_full_market_scan(min_volume=2_000_000, min_price=10.0, extended_hours=False):
    """Scan the full US market for breakout/breakdown & gap setups."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()

    all_tickers = get_us_tickers()
    if not all_tickers:
        scan_progress["status"] = "error"
        scan_progress["phase_label"] = "Failed to fetch ticker list"
        return pd.DataFrame()

    total_tickers = len(all_tickers)
    found_signals = []

    def process_breakout(sym, df):
        try:
            if len(df) < 50:
                return None
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())
            price = float(df['Close'].iloc[-1])
            dollar_volume = recent_vol * price

            # Big Cap / High Liquidity filter (requires ~$150M+ traded today)
            if dollar_volume < 150_000_000:
                return None

            if recent_vol >= min_volume and price >= min_price:
                result = _analyze_breakout_setup(sym, df)
                if result:
                    found_signals.append(sym)
                    return result
        except Exception:
            pass
        return None

    def _on_dl_progress(done, tot, sym):
        _update_progress("downloading",
                         f"Downloading & Analyzing... ({done}/{tot})",
                         done, tot, ticker=sym, found=len(found_signals))
        elapsed = time.time() - start_time
        if done > 0:
            rate = elapsed / done
            scan_progress["eta_seconds"] = int((tot - done) * rate)

    interval = "15m" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    fetch_days = 14 if extended_hours else 280

    stock_results = fetch_batch_concurrent(
        all_tickers, days=fetch_days, max_workers=8,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=includePrePost,
        process_fn=process_breakout
    )

    results = [r for r in stock_results.values() if r is not None]

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} breakout signals found",
        "current": total_tickers, "total": total_tickers,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] Breakout full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False)


# =====================================================================
# Watchlist (for quick scans)
# =====================================================================

WATCHLIST = [
    "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NVDA",
    "NFLX", "PYPL", "INTC", "AMD", "SNAP", "UBER", "BABA",
    "PLTR", "F", "GM", "XOM", "OXY", "DIS", "BA", "COIN"
]

# =====================================================================
# CLI entry point
# =====================================================================    return df



if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "watchlist"

    print("=" * 60)
    print("  📈  STOCK REVERSAL SCANNER")
    print("=" * 60)
    print(f"  Date : {datetime.today().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Mode : {mode}")
    print("=" * 60)

    if mode == "full":
        result_df = full_market_scan()
    else:
        print(f"  Tickers : {len(WATCHLIST)}")
        print()
        result_df = reversal_scanner(WATCHLIST)

    print()
    if result_df.empty:
        print("No reversal setups found.")
    else:
        print("=" * 60)
        print("  POTENTIAL REVERSAL CANDIDATES")
        print("=" * 60)
        print(result_df.to_string(index=False))
        print()
