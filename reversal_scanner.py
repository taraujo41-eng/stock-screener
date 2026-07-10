"""
Stock Reversal Scanner — Full Market Edition
Scans the entire US stock market for bullish/bearish reversal setups.

Modes:
  • Full market scan — fetches all US tickers (S&P 500 + NASDAQ 100 + ETFs + watchlist), pre-filters, then analyzes
  • Options scan    — full market options setup scanner
  • 3-Sigma Bot     — 15m regular-hours Close vs Daily 3-Sigma Bollinger Bands

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
    fetch_options_chain, fetch_options_for_expiration, fetch_news,
    fetch_quotes_batch, check_optionable_batch
)

warnings.filterwarnings("ignore")

# =====================================================================
# Global progress tracker  (read by the web server)
# =====================================================================

scan_progress = {
    "status": "idle",       # idle | running | done | error
    "mode": "",             # 3sigma | 2sigma | 52w
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
def _reset_progress(status="idle", mode=""):
    scan_progress.update({
        "status": status, "mode": mode, "phase": "", "phase_label": "",
        "current": 0, "total": 0, "found": 0,
        "ticker": "", "pct": 0, "eta_seconds": 0,
        "debug_log": [],
    })

def _update_progress(phase, label, current, total, ticker="", found=None, pct=None):
    scan_progress["status"] = "running"
    scan_progress["phase"] = phase
    scan_progress["phase_label"] = label
    scan_progress["current"] = current
    scan_progress["total"] = total
    scan_progress["ticker"] = ticker
    if pct is not None:
        scan_progress["pct"] = pct
    else:
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
            
        from datetime import timezone
        now = datetime.now(timezone.utc)
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
    """Fetch large-cap US stock tickers (S&P 500 + NASDAQ 100) + ETFs + Webull Watchlists."""
    from data_fetcher import get_unofficial_client
    wb = get_unofficial_client()
    tickers = set()
    headers = {"User-Agent": "Mozilla/5.0"}
    fallback_file = os.path.join(os.path.dirname(__file__), "sp500_nasdaq_fallback.json")

    # ── Source 1: S&P 500 ──
    try:
        html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers, timeout=10).text
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
        html = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=headers, timeout=10).text
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

    # ── Local Fallback for Cloud Environments ──
    if len(tickers) < 400:
        if os.path.exists(fallback_file):
            try:
                with open(fallback_file, "r") as f:
                    cached_list = json.load(f)
                added_cached = 0
                for sym in cached_list:
                    if sym not in tickers:
                        tickers.add(sym)
                        added_cached += 1
                print(f"  Loaded {added_cached} tickers from local fallback cache: {fallback_file}")
            except Exception as e:
                print(f"  Failed to load fallback tickers: {e}")
        else:
            print("  Warning: No local fallback cache file found.")
    else:
        # Save successfully fetched tickers to fallback cache
        try:
            with open(fallback_file, "w") as f:
                json.dump(list(tickers), f, indent=2)
            print(f"  Saved {len(tickers)} tickers to local fallback cache: {fallback_file}")
        except Exception as e:
            print(f"  Failed to save fallback tickers: {e}")

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
    print(f"  Source 3 (Major ETFs): added {added_etfs} unique ETFs")

    # ── Source 4: Local Watchlist ──
    watchlist_file = os.path.join(os.path.dirname(__file__), "watchlist.json")
    if os.path.exists(watchlist_file):
        try:
            with open(watchlist_file, "r") as f:
                wl = json.load(f)
            added_wl = 0
            for sym in wl:
                clean = str(sym).strip().upper()
                if clean and clean not in tickers:
                    tickers.add(clean)
                    added_wl += 1
            print(f"  Source 4 (Watchlist): +{added_wl} unique tickers")
        except Exception as e:
            print(f"  Source 4 (Watchlist): failed ({e})")

    # ── Source 5: Webull Watchlists ──
    if wb:
        try:
            watchlists = wb.get_watchlists()
            if watchlists:
                added_wb = 0
                for wl in watchlists:
                    wl_name = wl.get("name", "Unknown")
                    ticker_list = wl.get("tickerList", [])
                    for t in ticker_list:
                        template = t.get("template", "").lower()
                        if template in ("stock", "etf"):
                            symbol = t.get("symbol")
                            if symbol:
                                clean = symbol.strip().upper()
                                if clean not in tickers:
                                    tickers.add(clean)
                                    added_wb += 1
                print(f"  Source 5 (Webull Watchlists): +{added_wb} unique tickers")
            else:
                print("  Source 5 (Webull Watchlists): None found")
        except Exception as e:
            print(f"  Source 5 (Webull Watchlists): failed ({e})")
    else:
        print("  Source 5 (Webull Watchlists): No Webull client")

    # Remove known non-equity / test symbols
    exclude = {"TRUE", "NONE", "NULL", "CTEST", "NTEST", "ZTEST"}
    tickers -= exclude

    print(f"  Final Ticker Count (All Sources): {len(tickers)}")
    return sorted(tickers)





# =====================================================================
# Pre-filter: High Liquidity + Optionable Only
# =====================================================================

MIN_AVG_VOLUME = float(os.getenv("MIN_AVG_VOLUME", "1000000"))  # Minimum average daily volume (shares)
MIN_PRICE = float(os.getenv("MIN_PRICE", "20.0"))               # Minimum stock price ($)
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "10000000000"))  # Minimum market cap ($10B)

def prefilter_liquid_optionable(tickers):
    """
    Pre-filter tickers to only include high-liquidity, optionable stocks.
    Uses Webull live quote data to check:
      1. Market cap >= $10B
      2. Average daily volume >= 500K shares
      3. Last price >= $20
      4. Has an options chain (at least 1 expiration on Webull)
    Returns the filtered (sorted) ticker list.
    """
    print(f"\n{'='*60}")
    print(f"  🔍  PRE-FILTER: Liquidity + Optionable Check")
    print(f"{'='*60}")
    print(f"  Input tickers: {len(tickers)}")
    print(f"  Criteria: MktCap >= $10B | AvgVol >= {MIN_AVG_VOLUME:,} | Price >= ${MIN_PRICE:.0f} | Optionable")

    start_time = time.time()

    # Phase 1: Fetch live quotes for all tickers
    print(f"  Phase 1: Fetching live quotes from Webull...")
    _update_progress("prefilter", "Fetching live quotes for pre-filter...", 0, len(tickers), pct=0)

    def _on_quote_progress(i, tot, sym):
        pct = int((i / tot) * 50) if tot else 0
        _update_progress("prefilter", f"Pre-filter: fetching quotes ({i}/{tot})...", i, tot, ticker=sym, pct=pct)

    quotes = fetch_quotes_batch(tickers, max_workers=10, on_progress=_on_quote_progress)
    print(f"  Quotes fetched: {len(quotes)} / {len(tickers)}")

    if not quotes:
        print("  ⚠️ WARNING: FAILED TO FETCH ANY QUOTES FROM WEBULL. BYPASSING LIQUIDITY PRE-FILTER TO PREVENT EMPTY SCAN.")
        _update_progress("prefilter", "Pre-filter failed: Webull quotes unavailable, bypassing filter", len(tickers), len(tickers), pct=100)
        return sorted(tickers)

    # Phase 2: Apply market cap + volume + price filters
    volume_price_passed = []
    removed_low_vol = 0
    removed_low_price = 0
    removed_low_mktcap = 0
    removed_no_quote = 0

    for sym in tickers:
        q = quotes.get(sym)
        if not q:
            removed_no_quote += 1
            continue

        # Extract price
        price = None
        for key in ("close", "price", "lastPrice", "tradePrice"):
            val = q.get(key)
            if val is not None:
                try:
                    price = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Price filter
        if price is not None and price < MIN_PRICE:
            removed_low_price += 1
            continue

        # Market cap filter — calculate from totalShares * price
        market_cap = None
        total_shares = None
        for key in ("totalShares", "outstandingShares", "sharesOutstanding"):
            val = q.get(key)
            if val is not None:
                try:
                    total_shares = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Also check if Webull directly provides marketCap
        for key in ("marketCap", "marketValue", "mktCap"):
            val = q.get(key)
            if val is not None:
                try:
                    market_cap = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Calculate market cap from shares * price if not directly available
        if market_cap is None and total_shares is not None and price is not None:
            market_cap = total_shares * price

        if market_cap is not None and market_cap < MIN_MARKET_CAP:
            removed_low_mktcap += 1
            continue

        # Extract average volume
        avg_vol = None
        for key in ("avgVol10Day", "avgVolume", "avgVol", "avgVol30Day"):
            val = q.get(key)
            if val is not None:
                try:
                    avg_vol = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # If no avgVol field, estimate from totalVolume if available
        if avg_vol is None:
            vol = q.get("volume") or q.get("totalVolume")
            if vol:
                try:
                    avg_vol = float(vol)  # Use today's volume as rough proxy
                except (ValueError, TypeError):
                    pass

        # Volume filter
        if avg_vol is not None and avg_vol < MIN_AVG_VOLUME:
            removed_low_vol += 1
            continue

        volume_price_passed.append(sym)

    print(f"  Phase 2 results: {len(volume_price_passed)} passed market cap/volume/price filter")
    print(f"    Removed — low mkt cap: {removed_low_mktcap}, low volume: {removed_low_vol}, low price: {removed_low_price}, no quote: {removed_no_quote}")

    # Phase 3: Check optionability on the remaining tickers
    print(f"  Phase 3: Checking optionability for {len(volume_price_passed)} tickers...")
    _update_progress("prefilter", f"Checking optionability ({len(volume_price_passed)} tickers)...", 0, len(volume_price_passed), pct=55)

    optionable_set = check_optionable_batch(volume_price_passed, max_workers=10)
    filtered = sorted([sym for sym in volume_price_passed if sym in optionable_set])

    removed_not_optionable = len(volume_price_passed) - len(filtered)

    elapsed = time.time() - start_time
    print(f"  Phase 3 results: {len(filtered)} are optionable (removed {removed_not_optionable} non-optionable)")
    print(f"  ✅ Pre-filter complete: {len(tickers)} → {len(filtered)} tickers in {elapsed:.1f}s")
    print(f"{'='*60}\n")

    _update_progress("prefilter", f"Pre-filter done: {len(filtered)} liquid optionable tickers", len(filtered), len(filtered), pct=100)

    return filtered


def check_spy_regime():
    """Returns True if SPY is bullish (above its 50 SMA), False if bearish."""
    try:
        from data_fetcher import fetch_one
        spy_df = fetch_one("SPY", days=100, interval="1d")
        if spy_df is not None and len(spy_df) >= 50:
            spy_close = float(spy_df['Close'].iloc[-1])
            spy_sma50 = float(compute_sma(spy_df['Close'], 50).iloc[-1])
            is_bullish = spy_close >= spy_sma50
            print(f"  [Regime check] SPY Close: {spy_close:.2f}, SMA50: {spy_sma50:.2f} | Bullish: {is_bullish}")
            return is_bullish
    except Exception as e:
        print(f"  [Regime check] Error fetching SPY regime: {e}")
    return True  # Fallback to bullish if fetch fails

def fetch_upcoming_earnings(tickers):
    """
    Fetch upcoming earnings timestamps for a list of tickers.
    Returns a dict of {ticker: (start_timestamp, end_timestamp)}.
    """
    try:
        import data_fetcher
        if data_fetcher._yahoo_failures >= data_fetcher._YAHOO_MAX_FAILURES:
            return {}

        from data_fetcher import _ensure_session
        session, crumb = _ensure_session()
        symbols_str = ",".join(tickers)
        url = "https://query2.finance.yahoo.com/v7/finance/quote"
        params = {"symbols": symbols_str}
        if crumb:
            params["crumb"] = crumb
        resp = session.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data_fetcher._yahoo_failures = 0  # Reset on success
            data = resp.json()
            results = data.get("quoteResponse", {}).get("result", [])
            earnings = {}
            for r in results:
                sym = r.get("symbol")
                start = r.get("earningsTimestampStart") or r.get("earningsTimestamp")
                end = r.get("earningsTimestampEnd") or r.get("earningsTimestamp")
                if sym and (start or end):
                    earnings[sym] = (start, end)
            return earnings
        else:
            data_fetcher._yahoo_failures += 1
    except Exception as e:
        import data_fetcher
        data_fetcher._yahoo_failures += 1
        print(f"  Error fetching earnings dates: {e}")
    return {}


def get_upcoming_earnings_map(tickers):
    """
    Batch fetches upcoming earnings timestamps for all tickers.
    Returns a dict of {ticker: (start_time, end_time)}.
    """
    earnings_map = {}
    chunk_size = 100
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        try:
            chunk_earnings = fetch_upcoming_earnings(chunk)
            earnings_map.update(chunk_earnings)
        except Exception:
            pass
    return earnings_map

def is_earnings_imminent(ticker, earnings_map, days_buffer=4):
    """
    Checks if earnings date is within the days_buffer.
    """
    if not earnings_map or ticker not in earnings_map:
        return False
    start, end = earnings_map[ticker]
    now = time.time()
    buffer_seconds = days_buffer * 86400
    if start:
        time_to_earnings = start - now
        if -86400 <= time_to_earnings <= buffer_seconds:
            return True
    if end:
        time_to_earnings = end - now
        if -86400 <= time_to_earnings <= buffer_seconds:
            return True
    return False

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

def compute_bollinger_bands(series, length=20, num_std=3):
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

def find_pivots(series, window=5):
    """
    Find local peaks and troughs in a series.
    A peak is a value greater than its neighbors in a window on both sides.
    A trough is a value smaller than its neighbors in a window on both sides.
    Returns lists of (index, price, type)
    """
    pivots = []
    n = len(series)
    if n < window * 2 + 1:
        return pivots

    for i in range(window, n - window):
        val = series.iloc[i]
        left_vals = series.iloc[i-window:i]
        right_vals = series.iloc[i+1:i+window+1]
        
        # Local peak check
        if all(val > left_vals) and all(val > right_vals):
            pivots.append((i, float(val), 'peak'))
        # Local trough check
        elif all(val < left_vals) and all(val < right_vals):
            pivots.append((i, float(val), 'trough'))
    return pivots

def detect_double_top_bottom(df, pivots, tolerance=0.02, min_swing=0.03):
    """
    Detect Double Top & Double Bottom in the recent part of the pivots.
    """
    peaks = [p for p in pivots if p[2] == 'peak']
    troughs = [p for p in pivots if p[2] == 'trough']
    
    last_price = float(df['Close'].iloc[-1])
    double_bottom = False
    double_top = False
    
    # Double Bottom: Two recent troughs at similar levels with a peak between them
    if len(troughs) >= 2:
        t1 = troughs[-2]
        t2 = troughs[-1]
        
        price_diff = abs(t1[1] - t2[1]) / max(t1[1], t2[1])
        if price_diff <= tolerance:
            inter_peaks = [p for p in peaks if t1[0] < p[0] < t2[0]]
            if inter_peaks:
                neckline = max(p[1] for p in inter_peaks)
                swing_size = (neckline - t2[1]) / t2[1]
                if swing_size >= min_swing:
                    if last_price >= t2[1] and last_price >= neckline * 0.95:
                        double_bottom = True

    # Double Top: Two recent peaks at similar levels with a trough between them
    if len(peaks) >= 2:
        p1 = peaks[-2]
        p2 = peaks[-1]
        
        price_diff = abs(p1[1] - p2[1]) / max(p1[1], p2[1])
        if price_diff <= tolerance:
            inter_troughs = [t for t in troughs if p1[0] < t[0] < p2[0]]
            if inter_troughs:
                neckline = min(t[1] for t in inter_troughs)
                swing_size = (p2[1] - neckline) / neckline
                if swing_size >= min_swing:
                    if last_price <= p2[1] and last_price <= neckline * 1.05:
                        double_top = True
                        
    return double_bottom, double_top

def detect_head_and_shoulders(df, pivots, tolerance=0.04):
    """
    Detect Head & Shoulders and Inverse Head & Shoulders.
    """
    peaks = [p for p in pivots if p[2] == 'peak']
    troughs = [p for p in pivots if p[2] == 'trough']
    
    last_price = float(df['Close'].iloc[-1])
    hs = False
    ihs = False
    
    # H&S: 3 consecutive peaks: left shoulder, head (highest), right shoulder
    if len(peaks) >= 3:
        p1, p2, p3 = peaks[-3:]
        if p2[1] > p1[1] and p2[1] > p3[1]:
            shoulder_diff = abs(p1[1] - p3[1]) / max(p1[1], p3[1])
            if shoulder_diff <= tolerance:
                inter_troughs = [t for t in troughs if p1[0] < t[0] < p3[0]]
                if len(inter_troughs) >= 2:
                    neckline = max(t[1] for t in inter_troughs)
                    if last_price <= neckline * 1.05:
                        hs = True

    # Inverse H&S: 3 consecutive troughs: left shoulder, head (lowest), right shoulder
    if len(troughs) >= 3:
        t1, t2, t3 = troughs[-3:]
        if t2[1] < t1[1] and t2[1] < t3[1]:
            shoulder_diff = abs(t1[1] - t3[1]) / max(t1[1], t3[1])
            if shoulder_diff <= tolerance:
                inter_peaks = [p for p in peaks if t1[0] < p[0] < t3[0]]
                if len(inter_peaks) >= 2:
                    neckline = min(p[1] for p in inter_peaks)
                    if last_price >= neckline * 0.95:
                        ihs = True
                        
    return hs, ihs

def detect_cup_and_handle(df, pivots, tolerance=0.03):
    """
    Detect Cup & Handle.
    """
    peaks = [p for p in pivots if p[2] == 'peak']
    troughs = [p for p in pivots if p[2] == 'trough']
    
    last_price = float(df['Close'].iloc[-1])
    cup_handle = False
    
    if len(peaks) >= 2:
        p1 = peaks[-2]
        p2 = peaks[-1]
        
        rim_diff = abs(p1[1] - p2[1]) / max(p1[1], p2[1])
        if rim_diff <= tolerance:
            inter_troughs = [t for t in troughs if p1[0] < t[0] < p2[0]]
            if inter_troughs:
                bottom = min(t[1] for t in inter_troughs)
                cup_depth = p2[1] - bottom
                
                if cup_depth / p2[1] > 0.05:
                    post_p2_df = df.iloc[p2[0]:]
                    if len(post_p2_df) > 2:
                        handle_min = post_p2_df['Low'].min()
                        handle_max = post_p2_df['High'].max()
                        
                        if handle_min > (bottom + 0.5 * cup_depth):
                            if last_price >= handle_max * 0.97 or last_price >= p2[1] * 0.97:
                                cup_handle = True
                                
    return cup_handle

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

def _analyze_stock(sym, df, rsi_bull_thresh=35, rsi_bear_thresh=65, swing_tolerance=0.03, skip_options=False, is_market_bullish=True):
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
        prev_rsi = float(rsi_series.iloc[-2]) if len(rsi_series) > 1 else rsi_val
        rsi_bull_hook = prev_rsi < 30 <= rsi_val
        rsi_bear_hook = prev_rsi > 70 >= rsi_val
        
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
        hit_52w_low = last_price <= fiftyTwoWeekLow if fiftyTwoWeekLow else False
        hit_52w_high = last_price >= fiftyTwoWeekHigh if fiftyTwoWeekHigh else False

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

        # 15. Additional technicals computation
        adr_pct = compute_adr_pct(df, 14)
        
        ema20_series = compute_ema(df['Close'], 20)
        ema20 = float(ema20_series.iloc[-1]) if len(ema20_series) > 0 else None
        
        sma50_series = compute_sma(df['Close'], 50)
        sma50 = float(sma50_series.iloc[-1]) if len(sma50_series) > 0 and not np.isnan(sma50_series.iloc[-1]) else None
        
        ema20_dist = ((last_price - ema20) / ema20) * 100 if ema20 else 0.0
        sma50_dist = ((last_price - sma50) / sma50) * 100 if sma50 else 0.0
        sma200_dist = ((last_price - sma200) / sma200) * 100 if sma200 else 0.0
        
        bb_upper_series, bb_mid_series, bb_lower_series = compute_bollinger_bands(df['Close'], 20)
        bb_upper = float(bb_upper_series.iloc[-1]) if len(bb_upper_series) > 0 else None
        bb_lower = float(bb_lower_series.iloc[-1]) if len(bb_lower_series) > 0 else None
        bb_pct_b = 50.0
        if bb_upper is not None and bb_lower is not None and (bb_upper - bb_lower) != 0:
            bb_pct_b = ((last_price - bb_lower) / (bb_upper - bb_lower)) * 100
            
        try:
            squeeze_on, _, _ = detect_squeeze(df)
        except Exception:
            squeeze_on = False

        # 16. Chart Pattern Detections
        detected_patterns = []
        double_bottom, double_top = False, False
        hs, ihs = False, False
        cup_handle = False

        # ═══════════════════════════════════════════════════════
        # WEIGHTED SCORING SYSTEM
        # ═══════════════════════════════════════════════════════
        
        MIN_SCORE = 5  # Lowered from 7 to include A-grade signals

        # --- BULLISH SCORE ---
        bull_score = 0
        bull_tags = []
        if is_market_bullish:
            bull_score += 1
            bull_tags.append("Market Trend +1")

        # Chart pattern additions to bullish scoring
        if double_bottom:
            bull_score += 3
            bull_tags.append("Double Bottom +3")
        if ihs:
            bull_score += 3
            bull_tags.append("Inverse H&S +3")
        if cup_handle:
            bull_score += 3
            bull_tags.append("Cup & Handle +3")

        # Chart pattern additions to bearish scoring
        if double_top:
            # Note: We will add to bear_score down below in the bearish section
            pass

        has_bull_pattern = patterns['hammer'] or patterns['bull_engulf'] or patterns['bottoming_tail'] or double_bottom or ihs or cup_handle
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
        if rsi_bull_hook:
            bull_score += 3; bull_tags.append("RSI Hook ↑ +3")
        if hit_52w_low:
            bull_score += 3; bull_tags.append("Hits 52w Low +3")
        if has_bull_pattern:
            if not hit_52w_low and near_52w_low:
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
        if not is_market_bullish:
            bear_score += 1
            bear_tags.append("Market Trend +1")

        if double_top:
            bear_score += 3
            bear_tags.append("Double Top +3")
        if hs:
            bear_score += 3
            bear_tags.append("Head & Shoulders +3")

        has_bear_pattern = patterns['star'] or patterns['bear_engulf'] or patterns['topping_tail'] or double_top or hs
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
        if rsi_bear_hook:
            bear_score += 3; bear_tags.append("RSI Hook ↓ +3")
        if hit_52w_high:
            bear_score += 3; bear_tags.append("Hits 52w High +3")
        if has_bear_pattern:
            if not hit_52w_high and near_52w_high:
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
        # Only check news if the stock has a realistic chance of qualifying:
        # e.g., technical score >= 4, or technical score >= 3 with a matching candle pattern.
        # This prevents thousands of slow, rate-limited HTTP news calls for low-conviction setups.
        needs_news_check = (
            (bull_score >= 4 or (bull_score >= 3 and has_bull_pattern)) or
            (bear_score >= 4 or (bear_score >= 3 and has_bear_pattern))
        )
        if needs_news_check:
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

        # Confidence grade - require RSI divergence for A+ grade
        has_div = (is_bullish and bull_div) or (is_bearish and bear_div)
        if score >= 7 and has_div:
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

        atr_series = compute_atr(df, 14)
        atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.05 * last_price
        entry = last_price
        if is_bullish:
            sl = last_price - 2.0 * atr_val
            pt = last_price + 4.0 * atr_val
        else:
            sl = last_price + 2.0 * atr_val
            pt = last_price - 4.0 * atr_val

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
            "News Details": news_details,
            "RVOL": round(rvol, 2) if rvol is not None else 0.0,
            "ADR": round(adr_pct, 2) if adr_pct is not None else 0.0,
            "EMA20_Dist": round(ema20_dist, 2),
            "SMA50_Dist": round(sma50_dist, 2),
            "SMA200_Dist": round(sma200_dist, 2),
            "Squeeze": bool(squeeze_on),
            "BB_Pct": round(bb_pct_b, 1),
            "Patterns": " | ".join(detected_patterns) if detected_patterns else "—",
            "Entry": round(entry, 2),
            "Stop Loss": round(sl, 2),
            "Profit Target": round(pt, 2)
        }
    except Exception as e:
        print(f"  Error analyzing {sym}: {e}")
    return None


# =====================================================================
# Full market scanner  (batch download, pre-filter, then analyze)
# =====================================================================





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
        has_bull_pattern = patterns['hammer'] or patterns['bull_engulf'] or patterns['bottoming_tail']
        has_bear_pattern = patterns['star'] or patterns['bear_engulf'] or patterns['topping_tail']
        
        needs_news_check = (
            (bull_catalyst >= 4 or (bull_catalyst >= 3 and has_bull_pattern)) or
            (bear_catalyst >= 4 or (bear_catalyst >= 3 and has_bear_pattern))
        )
        if needs_news_check:
            has_news, news_tag, news_item = detect_news_catalyst(sym)
            if has_news and news_tag:
                news_details = news_item
                if bull_catalyst >= 3:
                    bull_catalyst += 2
                    bull_reasons.append(f"{news_tag} (+2)")
                if bear_catalyst >= 3:
                    bear_catalyst += 2
                    bear_reasons.append(f"{news_tag} (+2)")

        # Need at least score 5 on one side to proceed (raised from 4 to filter for top-tier candidates)
        max_catalyst = max(bull_catalyst, bear_catalyst)
        print(f"  {sym}: Bull={bull_catalyst} Bear={bear_catalyst} RSI={rsi_val:.1f} Chg={day_chg_pct:.1f}%")
        if max_catalyst < 5:
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

        # Additional technicals computation
        adr_pct = compute_adr_pct(df, 14)
        
        ema20_series = compute_ema(df['Close'], 20)
        ema20 = float(ema20_series.iloc[-1]) if len(ema20_series) > 0 else None
        
        sma50_series = compute_sma(df['Close'], 50)
        sma50 = float(sma50_series.iloc[-1]) if len(sma50_series) > 0 and not np.isnan(sma50_series.iloc[-1]) else None
        
        sma200_series = compute_sma(df['Close'], 200)
        sma200 = float(sma200_series.iloc[-1]) if len(sma200_series) > 0 and not np.isnan(sma200_series.iloc[-1]) else None
        
        ema20_dist = ((last_price - ema20) / ema20) * 100 if ema20 else 0.0
        sma50_dist = ((last_price - sma50) / sma50) * 100 if sma50 else 0.0
        sma200_dist = ((last_price - sma200) / sma200) * 100 if sma200 else 0.0
        
        bb_upper_series, bb_mid_series, bb_lower_series = compute_bollinger_bands(df['Close'], 20)
        bb_upper = float(bb_upper_series.iloc[-1]) if len(bb_upper_series) > 0 else None
        bb_lower = float(bb_lower_series.iloc[-1]) if len(bb_lower_series) > 0 else None
        bb_pct_b = 50.0
        if bb_upper is not None and bb_lower is not None and (bb_upper - bb_lower) != 0:
            bb_pct_b = ((last_price - bb_lower) / (bb_upper - bb_lower)) * 100
            
        try:
            squeeze_on, _, _ = detect_squeeze(df)
        except Exception:
            squeeze_on = False

        # Chart Pattern Detections
        detected_patterns = []

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
            "News Details": news_details,
            "RVOL": round(rvol, 2) if rvol is not None else 0.0,
            "ADR": round(adr_pct, 2) if adr_pct is not None else 0.0,
            "EMA20_Dist": round(ema20_dist, 2),
            "SMA50_Dist": round(sma50_dist, 2),
            "SMA200_Dist": round(sma200_dist, 2),
            "Squeeze": bool(squeeze_on),
            "BB_Pct": round(bb_pct_b, 1),
            "Patterns": " | ".join(detected_patterns) if detected_patterns else "—"
        }
    except Exception as e:
        print(f"  Error analyzing options for {sym}: {e}")
    return None








# =====================================================================
# 3-Sigma scanners (Manual Web-app trigger modes)
# =====================================================================

def _analyze_3sigma_setup(sym, df_15m, df_daily, is_market_bullish=True, std_dev_mult=3.0):
    """
    Evaluates 15m regular-hours Close against Daily Bollinger Bands (20 SMA, std_dev_mult std dev).
    Matches when the last 15m Close pierces the Daily Bollinger Bands.
    """
    try:
        if len(df_15m) < 20 or len(df_daily) < 20:
            return None

        try:
            from zoneinfo import ZoneInfo
            ny_tz = ZoneInfo("America/New_York")
        except Exception:
            import pytz
            ny_tz = pytz.timezone("America/New_York")
        today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
        
        middle_series = df_daily['Close'].rolling(window=20).mean()
        std_series = df_daily['Close'].rolling(window=20).std()
        upper_series = middle_series + std_dev_mult * std_series
        lower_series = middle_series - std_dev_mult * std_series
        
        last_daily_date_str = df_daily.index[-1].strftime("%Y-%m-%d")
        if last_daily_date_str == today_str and len(df_daily) > 1:
            daily_upper = float(upper_series.iloc[-2])
            daily_lower = float(lower_series.iloc[-2])
        else:
            daily_upper = float(upper_series.iloc[-1])
            daily_lower = float(lower_series.iloc[-1])

        # 2. Get last 15m row
        curr = df_15m.iloc[-1]
        last_price = float(curr['Close'])
        
        # 3. Check for touches/piercing
        is_bullish = last_price <= daily_lower
        is_bearish = last_price >= daily_upper
        
        if not is_bullish and not is_bearish:
            return None

        # 4. Standard indicators on 15m close
        rsi_series = compute_rsi(df_15m['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])
        rvol = compute_rvol(df_15m)
        adr_pct = compute_adr_pct(df_15m, 14)
        bull_div, bear_div = detect_rsi_divergence(df_15m['Close'], rsi_series, lookback=20)
        
        try:
            squeeze_on, _, _ = detect_squeeze(df_15m)
        except Exception:
            squeeze_on = False

        # Moving Averages distance on 15m
        ema20_series = compute_ema(df_15m['Close'], 20)
        ema20 = float(ema20_series.iloc[-1]) if len(ema20_series) > 0 else None
        
        sma50_series = compute_sma(df_15m['Close'], 50)
        sma50 = float(sma50_series.iloc[-1]) if len(sma50_series) > 0 and not np.isnan(sma50_series.iloc[-1]) else None
        
        sma200_series = compute_sma(df_15m['Close'], 200)
        sma200 = float(sma200_series.iloc[-1]) if len(sma200_series) > 0 and not np.isnan(sma200_series.iloc[-1]) else None
        
        ema20_dist = ((last_price - ema20) / ema20) * 100 if ema20 else 0.0
        sma50_dist = ((last_price - sma50) / sma50) * 100 if sma50 else 0.0
        sma200_dist = ((last_price - sma200) / sma200) * 100 if sma200 else 0.0

        bb_pct_b = 50.0
        if (daily_upper - daily_lower) != 0:
            bb_pct_b = ((last_price - daily_lower) / (daily_upper - daily_lower)) * 100

        # Constructing dynamic score & tags
        score = 10
        reasons_list = []
        
        if is_bullish:
            reasons_list.append(f"Pierced Daily Lower {int(std_dev_mult)}SD BB" if std_dev_mult.is_integer() else f"Pierced Daily Lower {std_dev_mult}SD BB")
            if bull_div:
                score += 4
                reasons_list.append("RSI Divergence")
            if rsi_val <= 30:
                score += 2
                reasons_list.append(f"RSI Oversold ({rsi_val:.1f})")
            if rvol > 1.5:
                score += 2
                reasons_list.append(f"High RVOL ({rvol:.1f}x)")
            if squeeze_on:
                score += 1
                reasons_list.append("Squeeze Active")
            if ema20_dist < -2.0:
                score += 1
                reasons_list.append("EMA Extension")
        else:
            reasons_list.append(f"Pierced Daily Upper {int(std_dev_mult)}SD BB" if std_dev_mult.is_integer() else f"Pierced Daily Upper {std_dev_mult}SD BB")
            if bear_div:
                score += 4
                reasons_list.append("RSI Divergence")
            if rsi_val >= 70:
                score += 2
                reasons_list.append(f"RSI Overbought ({rsi_val:.1f})")
            if rvol > 1.5:
                score += 2
                reasons_list.append(f"High RVOL ({rvol:.1f}x)")
            if squeeze_on:
                score += 1
                reasons_list.append("Squeeze Active")
            if ema20_dist > 2.0:
                score += 1
                reasons_list.append("EMA Extension")

        # Grade assignment - require RSI divergence for A+ grade
        has_div = (is_bullish and bull_div) or (is_bearish and bear_div)
        grade = "A+" if (score >= 12 and has_div) else "A"
        reasons = " | ".join(reasons_list)

        # 5. Options setups
        opt_str = "—"
        try:
            opt_setup = find_best_option(sym, "bullish" if is_bullish else "bearish", last_price)
            if opt_setup:
                opt_str = f"{opt_setup['symbol']} (${opt_setup['mid_price']:.2f})"
        except Exception:
            pass

        # 6. News details
        news_details = None
        try:
            has_news, tag, details = detect_news_catalyst(sym)
            if has_news and details:
                news_details = details
                # Add news tag to signals list
                headline_pill = f"News: {details['title'][:35]}..."
                reasons += f" | {headline_pill}"
                score += 2
        except Exception:
            pass

        # Calculate Stop Loss & Profit Target
        atr_series = compute_atr(df_15m, 14)
        atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.05 * last_price
        
        entry = last_price
        if is_bullish:
            sl = last_price - 2.0 * atr_val
            pt = last_price + 4.0 * atr_val
        else:
            sl = last_price + 2.0 * atr_val
            pt = last_price - 4.0 * atr_val

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
            "News Details": news_details,
            "RVOL": round(rvol, 2) if rvol is not None else 0.0,
            "ADR": round(adr_pct, 2) if adr_pct is not None else 0.0,
            "EMA20_Dist": round(ema20_dist, 2),
            "SMA50_Dist": round(sma50_dist, 2),
            "SMA200_Dist": round(sma200_dist, 2),
            "Squeeze": bool(squeeze_on),
            "BB_Pct": round(bb_pct_b, 1),
            "Patterns": "—",
            "Entry": round(entry, 2),
            "Stop Loss": round(sl, 2),
            "Profit Target": round(pt, 2)
        }
    except Exception as e:
        print(f"  Error analyzing 3-sigma for {sym}: {e}")
    return None


def three_sigma_full_market_scan(extended_hours=False):
    """Scan all US tickers for 3-Sigma Daily Bands + 15m regular hours crossings."""
    _reset_progress(status="running", mode="3sigma")
    start_time = time.time()

    tickers = get_us_tickers()
    tickers = prefilter_liquid_optionable(tickers)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)
    # Daily progress callback (0% - 40%)
    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 40)
        _update_progress("downloading", f"Downloading daily candles... ({i}/{tot})", i, tot, ticker=sym, pct=pct)

    _update_progress("downloading", "Initiating daily candle download...", 0, total, pct=0)
    daily_data = fetch_batch_concurrent(
        tickers, days=45, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost="false"
    )

    # 15m progress callback (40% - 85%)
    def _on_15m_progress(i, tot, sym):
        pct = 40 + int((i / tot) * 45)
        _update_progress("downloading", f"Downloading 15m bars... ({i}/{tot})", i, tot, ticker=sym, found=len(results), pct=pct)

    _update_progress("downloading", "Initiating 15m bar download...", 0, total, pct=40)
    stock_data = fetch_batch_concurrent(
        tickers, days=15, max_workers=6,
        on_progress=_on_15m_progress, delay=0.05, interval="15m", includePrePost="true" if extended_hours else "false"
    )

    for i, (sym, df_15m) in enumerate(stock_data.items()):
        pct = 85 + int((i / len(stock_data)) * 15) if len(stock_data) else 100
        _update_progress("analyzing", f"Analyzing 3-Sigma for {sym}...", i, len(stock_data), ticker=sym, found=len(results), pct=pct)
        try:
            df_daily = daily_data.get(sym)
            if df_15m is None or df_daily is None or len(df_15m) < 20 or len(df_daily) < 20:
                continue

            result = _analyze_3sigma_setup(sym, df_15m, df_daily, is_market_bullish=is_market_bullish)
            if result:
                results.append(result)
        except Exception as e:
            print(f"Error processing 3-sigma for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "finishing", "phase": "complete",
        "phase_label": f"Done — {len(results)} 3-sigma signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] 3-Sigma full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(15)


def two_sigma_full_market_scan(extended_hours=False):
    """Scan all US tickers for 2-Sigma Daily Bands + 15m regular hours crossings."""
    _reset_progress(status="running", mode="2sigma")
    start_time = time.time()

    tickers = get_us_tickers()
    tickers = prefilter_liquid_optionable(tickers)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)

    # Daily progress callback (0% - 40%)
    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 40)
        _update_progress("downloading", f"Downloading daily candles... ({i}/{tot})", i, tot, ticker=sym, pct=pct)

    _update_progress("downloading", "Initiating daily candle download...", 0, total, pct=0)
    daily_data = fetch_batch_concurrent(
        tickers, days=45, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost="false"
    )

    # 15m progress callback (40% - 85%)
    def _on_15m_progress(i, tot, sym):
        pct = 40 + int((i / tot) * 45)
        _update_progress("downloading", f"Downloading 15m bars... ({i}/{tot})", i, tot, ticker=sym, found=len(results), pct=pct)

    _update_progress("downloading", "Initiating 15m bar download...", 0, total, pct=40)
    stock_data = fetch_batch_concurrent(
        tickers, days=15, max_workers=6,
        on_progress=_on_15m_progress, delay=0.05, interval="15m", includePrePost="true" if extended_hours else "false"
    )

    for i, (sym, df_15m) in enumerate(stock_data.items()):
        pct = 85 + int((i / len(stock_data)) * 15) if len(stock_data) else 100
        _update_progress("analyzing", f"Analyzing 2-Sigma for {sym}...", i, len(stock_data), ticker=sym, found=len(results), pct=pct)
        try:
            df_daily = daily_data.get(sym)
            if df_15m is None or df_daily is None or len(df_15m) < 20 or len(df_daily) < 20:
                continue

            result = _analyze_3sigma_setup(sym, df_15m, df_daily, is_market_bullish=is_market_bullish, std_dev_mult=2.0)
            if result:
                results.append(result)
        except Exception as e:
            print(f"Error processing 2-sigma for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "finishing", "phase": "complete",
        "phase_label": f"Done — {len(results)} 2-sigma signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] 2-Sigma full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(15)


def fifty_two_week_reversal_scan(extended_hours=False):
    """Scan all US tickers for 52-week high/low with daily RSI divergence."""
    _reset_progress(status="running", mode="52w")
    start_time = time.time()

    tickers = get_us_tickers()
    tickers = prefilter_liquid_optionable(tickers)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)

    # 1. Fetch daily candles (365 days)
    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 85)
        _update_progress("downloading", f"Downloading daily candles... ({i}/{tot})", i, tot, ticker=sym, found=len(results), pct=pct)

    _update_progress("downloading", "Initiating daily candle download...", 0, total, pct=0)
    
    # 365 days of 1d bars
    daily_data = fetch_batch_concurrent(
        tickers, days=365, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost="false"
    )

    # 2. Analyze daily candles for 52w high/low and RSI divergence
    for i, (sym, df_daily) in enumerate(daily_data.items()):
        pct = 85 + int((i / len(daily_data)) * 15) if len(daily_data) else 100
        _update_progress("analyzing", f"Analyzing 52-week reversals for {sym}...", i, len(daily_data), ticker=sym, found=len(results), pct=pct)
        try:
            if df_daily is None or len(df_daily) < 50:
                continue

            curr = df_daily.iloc[-1]
            last_price = float(curr['Close'])
            
            # Retrieve 52-week High and Low
            fiftyTwoWeekHigh = float(df_daily['High'].max())
            fiftyTwoWeekLow = float(df_daily['Low'].min())
            
            # Proximity thresholds
            hit_52w_low = last_price <= fiftyTwoWeekLow
            hit_52w_high = last_price >= fiftyTwoWeekHigh
            near_52w_low_3pct = last_price <= fiftyTwoWeekLow * 1.03
            near_52w_high_3pct = last_price >= fiftyTwoWeekHigh * 0.97
            near_52w_low_5pct = last_price <= fiftyTwoWeekLow * 1.05
            near_52w_high_5pct = last_price >= fiftyTwoWeekHigh * 0.95
            
            is_bullish = near_52w_low_5pct
            is_bearish = near_52w_high_5pct
            
            if not is_bullish and not is_bearish:
                continue

            # Compute RSI
            rsi_series = compute_rsi(df_daily['Close'], 14)
            if len(rsi_series) < 22:
                continue
            rsi_val = float(rsi_series.iloc[-1])
            prev_rsi = float(rsi_series.iloc[-2]) if len(rsi_series) > 1 else rsi_val
            
            # RSI hooks
            rsi_bull_hook = prev_rsi < 30 <= rsi_val
            rsi_bear_hook = prev_rsi > 70 >= rsi_val
            
            # RSI Divergence detection
            bull_div, bear_div = detect_rsi_divergence(df_daily['Close'], rsi_series, lookback=20)
            
            # Confirmations score & tags
            score = 10
            reasons_list = []
            
            if is_bullish:
                if hit_52w_low:
                    score += 3
                    reasons_list.append("Hits 52w Low")
                elif near_52w_low_3pct:
                    score += 2
                    reasons_list.append("At 52w Low")
                else:
                    score += 1
                    reasons_list.append("Near 52w Low")
                
                if bull_div:
                    score += 4
                    reasons_list.append("RSI Divergence")
                if rsi_val <= 30:
                    score += 2
                    reasons_list.append(f"RSI Oversold ({rsi_val:.1f})")
                elif rsi_bull_hook:
                    score += 2
                    reasons_list.append("RSI Bull Hook")
            else:
                if hit_52w_high:
                    score += 3
                    reasons_list.append("Hits 52w High")
                elif near_52w_high_3pct:
                    score += 2
                    reasons_list.append("At 52w High")
                else:
                    score += 1
                    reasons_list.append("Near 52w High")
                    
                if bear_div:
                    score += 4
                    reasons_list.append("RSI Divergence")
                if rsi_val >= 70:
                    score += 2
                    reasons_list.append(f"RSI Overbought ({rsi_val:.1f})")
                elif rsi_bear_hook:
                    score += 2
                    reasons_list.append("RSI Bear Hook")

            # Technical indicators
            rvol = compute_rvol(df_daily)
            if rvol is not None and rvol > 1.5:
                score += 2
                reasons_list.append(f"High RVOL ({rvol:.1f}x)")
                
            try:
                squeeze_on, _, _ = detect_squeeze(df_daily)
            except Exception:
                squeeze_on = False
            if squeeze_on:
                score += 1
                reasons_list.append("Squeeze Active")
                
            adr_pct = compute_adr_pct(df_daily, 14)
            
            # EMA/SMA distances
            ema20_series = compute_ema(df_daily['Close'], 20)
            ema20 = float(ema20_series.iloc[-1]) if len(ema20_series) > 0 else None
            sma50_series = compute_sma(df_daily['Close'], 50)
            sma50 = float(sma50_series.iloc[-1]) if len(sma50_series) > 0 and not np.isnan(sma50_series.iloc[-1]) else None
            sma200_series = compute_sma(df_daily['Close'], 200)
            sma200 = float(sma200_series.iloc[-1]) if len(sma200_series) > 0 and not np.isnan(sma200_series.iloc[-1]) else None
            
            ema20_dist = ((last_price - ema20) / ema20) * 100 if ema20 else 0.0
            sma50_dist = ((last_price - sma50) / sma50) * 100 if sma50 else 0.0
            sma200_dist = ((last_price - sma200) / sma200) * 100 if sma200 else 0.0

            # Bollinger Bands %B
            middle = df_daily['Close'].rolling(window=20).mean()
            std = df_daily['Close'].rolling(window=20).std()
            upper = middle + 2.0 * std
            lower = middle - 2.0 * std
            upper_val = float(upper.iloc[-1])
            lower_val = float(lower.iloc[-1])
            bb_pct_b = ((last_price - lower_val) / (upper_val - lower_val)) * 100 if (upper_val - lower_val) != 0 else 50.0

            # Dynamic patterns
            patterns_list = detect_patterns(df_daily)
            patterns_str = " | ".join(patterns_list) if patterns_list else "—"

            reasons = " | ".join(reasons_list)

            # Options suggestion
            opt_str = "—"
            try:
                opt_setup = find_best_option(sym, "bullish" if is_bullish else "bearish", last_price)
                if opt_setup:
                    opt_str = f"{opt_setup['symbol']} (${opt_setup['mid_price']:.2f})"
            except Exception:
                pass

            # News Catalyst
            news_details = None
            try:
                has_news, tag, details = detect_news_catalyst(sym)
                if has_news and details:
                    news_details = details
                    headline_pill = f"News: {details['title'][:35]}..."
                    reasons += f" | {headline_pill}"
                    score += 2
            except Exception:
                pass

            # ATR and Trade Levels
            atr_series = compute_atr(df_daily, 14)
            atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.05 * last_price
            
            entry = last_price
            if is_bullish:
                sl = last_price - 2.0 * atr_val
                pt = last_price + 4.0 * atr_val
            else:
                sl = last_price + 2.0 * atr_val
                pt = last_price - 4.0 * atr_val

            has_div = (is_bullish and bull_div) or (is_bearish and bear_div)
            grade = "A+" if (score >= 12 and has_div) else "A"

            results.append({
                "Ticker": sym,
                "Last Price": round(last_price, 2),
                "Volume": int(curr['Volume']),
                "RSI": round(rsi_val, 1),
                "Score": score,
                "Grade": grade,
                "Bullish Signals": reasons if is_bullish else "—",
                "Bearish Signals": reasons if is_bearish else "—",
                "Suggested Option": opt_str,
                "News Details": news_details,
                "RVOL": round(rvol, 2) if rvol is not None else 0.0,
                "ADR": round(adr_pct, 2) if adr_pct is not None else 0.0,
                "EMA20_Dist": round(ema20_dist, 2),
                "SMA50_Dist": round(sma50_dist, 2),
                "SMA200_Dist": round(sma200_dist, 2),
                "Squeeze": bool(squeeze_on),
                "BB_Pct": round(bb_pct_b, 1),
                "Patterns": patterns_str,
                "Entry": round(entry, 2),
                "Stop Loss": round(sl, 2),
                "Profit Target": round(pt, 2)
            })

        except Exception as e:
            print(f"Error processing 52w reversal for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "finishing", "phase": "complete",
        "phase_label": f"Done — {len(results)} 52w reversals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] 52-week reversal full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(20)




# =====================================================================
# CLI entry point
# =====================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  📈  STOCK REVERSAL SCANNER")
    print("=" * 60)
    print(f"  Date : {datetime.today().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Mode : full market")
    print("=" * 60)

    result_df = full_market_scan()

    print()
    if result_df.empty:
        print("No reversal setups found.")
    else:
        print("=" * 60)
        print("  POTENTIAL REVERSAL CANDIDATES")
        print("=" * 60)
        print(result_df.to_string(index=False))
        print()

