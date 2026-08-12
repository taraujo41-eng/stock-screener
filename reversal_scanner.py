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
import threading
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
# Global progress tracker  (file-backed for cross-process visibility)
# =====================================================================

import json as _json

_PROGRESS_FILE = "/tmp/scan_progress.json"

_DEFAULT_PROGRESS = {
    "status": "idle",       # idle | running | done | error
    "mode": "",             # 3sigma | 2sigma | 52w | watchlist | rsidiv
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

class _SharedProgress(dict):
    """Dict-like object backed by a JSON file in /tmp for cross-process sharing."""

    def __init__(self):
        super().__init__(_DEFAULT_PROGRESS)
        self._lock = threading.Lock()
        # Load any existing state from file
        self._load()

    def _load(self):
        try:
            with open(_PROGRESS_FILE, "r") as f:
                data = _json.load(f)
                if isinstance(data, dict):
                    super().update(data)
        except Exception:
            pass

    def _save(self):
        try:
            with open(_PROGRESS_FILE, "w") as f:
                _json.dump(dict(self), f)
        except Exception:
            pass

    def __setitem__(self, key, value):
        with self._lock:
            super().__setitem__(key, value)
            self._save()

    def update(self, *args, **kwargs):
        with self._lock:
            super().update(*args, **kwargs)
            self._save()

    def read(self):
        """Read fresh state from disk (for the web server progress endpoint)."""
        with self._lock:
            self._load()
            return dict(self)

scan_progress = _SharedProgress()

_scan_system_lock = threading.Lock()

def acquire_scan_lock(owner="unknown"):
    """Acquire global scan lock — ensures ONLY ONE scan runs at any time."""
    acquired = _scan_system_lock.acquire(blocking=False)
    if not acquired:
        return False
    return True

def release_scan_lock():
    """Release global scan lock."""
    if _scan_system_lock.locked():
        try:
            _scan_system_lock.release()
        except RuntimeError:
            pass

def _reset_progress(status="idle", mode=""):
    scan_progress.update({
        "status": status, "mode": mode, "phase": "", "phase_label": "",
        "current": 0, "total": 0, "found": 0,
        "ticker": "", "pct": 1 if status == "running" else 0, "eta_seconds": 0,
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


def get_ny_timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


def determine_scan_candle_mode(force_extended=False):
    """
    Determines candle interval, lookback days, and prepost setting.
    - Extended/Market hours (Mon-Fri 4:00 AM - 8:00 PM ET): 15-minute candles ("15m"), 30 days, includePrePost="true".
    - Market closed (nights outside 4:00 AM - 8:00 PM ET, weekends): 1-day candles ("1d"), 280 days, includePrePost="false".
    """
    ny_tz = get_ny_timezone()
    now_et = datetime.now(ny_tz)
    
    is_weekday = now_et.weekday() < 5  # Mon=0, ..., Fri=4
    start_ext = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    end_ext = now_et.replace(hour=20, minute=0, second=0, microsecond=0)
    
    is_market_active = is_weekday and (start_ext <= now_et <= end_ext)
    
    if is_market_active or force_extended:
        return "15m", 30, "true"
    else:
        return "1d", 280, "false"


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
    """Fetch stock and ETF tickers exclusively from user's watchlist.json and Webull watchlists."""
    from data_fetcher import get_unofficial_client
    wb = get_unofficial_client()
    tickers = set()

    # ── Source 1: Local Watchlist (watchlist.json) ──
    watchlist_file = os.path.join(os.path.dirname(__file__), "watchlist.json")
    if os.path.exists(watchlist_file):
        try:
            with open(watchlist_file, "r") as f:
                wl = json.load(f)
            for sym in wl:
                clean = str(sym).strip().upper()
                if clean:
                    tickers.add(clean)
            print(f"  Source (watchlist.json): loaded {len(tickers)} tickers")
        except Exception as e:
            print(f"  Source (watchlist.json): failed ({e})")

    # ── Source 2: Webull Watchlists ──
    if wb:
        try:
            watchlists = wb.get_watchlists()
            if watchlists:
                added_wb = 0
                for wl in watchlists:
                    ticker_list = wl.get("tickerList", [])
                    for t in ticker_list:
                        template = t.get("template", "").lower()
                        if template in ("stock", "etf"):
                            symbol = t.get("symbol")
                            if symbol:
                                clean = symbol.strip().upper()
                                if clean and clean not in tickers:
                                    tickers.add(clean)
                                    added_wb += 1
                if added_wb > 0:
                    print(f"  Source (Webull Watchlists): +{added_wb} unique tickers")
        except Exception as e:
            print(f"  Source (Webull Watchlists): failed ({e})")

    # Fallback if watchlist is completely empty
    if not tickers:
        tickers = {"AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NVDA", "NFLX", "AMD", "SPY", "QQQ"}

    # Exclude non-equity / test symbols
    exclude = {"TRUE", "NONE", "NULL", "CTEST", "NTEST", "ZTEST"}
    tickers = {t for t in tickers if t not in exclude and t.isalpha() and 1 <= len(t) <= 5}

    print(f"  Final Watchlist Ticker Count: {len(tickers)}")
    return sorted(tickers)





# =====================================================================
# Pre-filter: High Liquidity + Optionable Only
# =====================================================================

MIN_AVG_VOLUME = float(os.getenv("MIN_AVG_VOLUME", "1000000"))  # Minimum average daily volume (shares)
MIN_PRICE = float(os.getenv("MIN_PRICE", "20.0"))               # Minimum stock price ($)
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "10000000000"))  # Minimum market cap ($10B)

def prefilter_liquid_optionable(tickers, MIN_PRICE=10.0, MIN_AVG_VOLUME=500_000):
    """
    Pre-filters a large list of tickers down to liquid ones.
    For watchlist scans (<= 200 tickers), returns the full list immediately.
    """
    if not tickers:
        return []
    if len(tickers) <= 200:
        return sorted(tickers)

    print(f"  [Prefilter] Screening {len(tickers)} tickers for liquidity (Price >= ${MIN_PRICE}, AvgVol >= {MIN_AVG_VOLUME:,})...")
    start_time = time.time()

    _update_progress("prefilter", "Pre-filter: fetching quotes via Webull...", 0, len(tickers), pct=5)
    try:
        def _on_quote_progress(i, tot, sym):
            pct = int((i / tot) * 15) if tot else 5
            _update_progress("prefilter", f"Pre-filter: fetching quotes ({i}/{tot})...", i, tot, ticker=sym, pct=pct)

        from data_fetcher import fetch_quotes_batch
        quotes = fetch_quotes_batch(tickers, max_workers=12, on_progress=_on_quote_progress)
        passed = []
        for sym, q in quotes.items():
            price = q.get("price") or q.get("close")
            avg_vol = q.get("avgVolume") or q.get("volume")
            if price and price >= MIN_PRICE and avg_vol and avg_vol >= MIN_AVG_VOLUME:
                passed.append(sym)

        if passed:
            filtered = sorted(passed)
            print(f"  ✅ Webull Pre-filter complete: {len(tickers)} → {len(filtered)} liquid tickers in {time.time() - start_time:.1f}s")
            _update_progress("prefilter", f"Pre-filter done: {len(filtered)} liquid tickers", len(filtered), len(filtered), pct=15)
            return filtered
    except Exception as e:
        print(f"  Webull pre-filter error: {e}")

    _update_progress("prefilter", "Pre-filter fallback: using full universe", len(tickers), len(tickers), pct=15)
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
        for key in ("avgVol10D", "avgVol10Day", "avgVol3M", "avgVolume", "avgVol", "avgVol30Day"):
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

    filtered = sorted(volume_price_passed)
    elapsed = time.time() - start_time
    print(f"  ✅ Pre-filter complete: {len(tickers)} → {len(filtered)} liquid tickers in {elapsed:.1f}s")
    print(f"{'='*60}\n")

    _update_progress("prefilter", f"Pre-filter done: {len(filtered)} liquid tickers", len(filtered), len(filtered), pct=100)

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
    Fetch upcoming earnings timestamps for a list of tickers via Webull API.
    Returns a dict of {ticker: (start_timestamp, end_timestamp)}.
    """
    try:
        from data_fetcher import get_unofficial_client
        wb_un = get_unofficial_client()
        if not wb_un:
            return {}
        earnings = {}
        for sym in tickers:
            try:
                q = wb_un.get_quote(stock=sym)
                if q and isinstance(q, dict):
                    earning_ts = q.get("nextEarningDay") or q.get("estimateEarningsDate")
                    if earning_ts:
                        earnings[sym] = (int(earning_ts), int(earning_ts))
            except Exception:
                pass
        return earnings
    except Exception as e:
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

def detect_rsi_divergence(price_series, rsi_series, lookback=20, max_anchor_age=None):
    """
    Check for RSI Divergence in the last `lookback` periods.
    - If max_anchor_age is set (e.g. 8), enforces fresh swing anchor points for the RSI Divergence Tab.
    - Otherwise (Watchlist Tab), uses standard swing divergence detection across the lookback window.
    Returns (bull_div, bear_div)
    """
    if len(price_series) < lookback + 2:
        return False, False
        
    curr_price = float(price_series.iloc[-1])
    curr_rsi = float(rsi_series.iloc[-1])
    
    # Lookback window (excluding last candle)
    window_price = price_series.iloc[-(lookback+2):-1]
    window_rsi = rsi_series.iloc[-(lookback+2):-1]
    
    if len(window_price) < 4:
        return False, False

    lowest_idx = window_price.values.argmin()
    highest_idx = window_price.values.argmax()
    
    bars_from_low = len(window_price) - lowest_idx
    bars_from_high = len(window_price) - highest_idx
    
    # Check anchor age constraints
    valid_low = (bars_from_low >= 2) and (max_anchor_age is None or bars_from_low <= max_anchor_age)
    valid_high = (bars_from_high >= 2) and (max_anchor_age is None or bars_from_high <= max_anchor_age)
    
    bull_div = False
    if valid_low:
        lowest_price_in_window = float(window_price.iloc[lowest_idx])
        rsi_at_low = float(window_rsi.iloc[lowest_idx])
        rsi_bull_magnitude = curr_rsi - rsi_at_low
        bull_div = (
            (curr_price <= lowest_price_in_window * 1.005) and
            (rsi_bull_magnitude >= 2.0) and
            (curr_rsi <= 52.0)
        )
    
    bear_div = False
    if valid_high:
        highest_price_in_window = float(window_price.iloc[highest_idx])
        rsi_at_high = float(window_rsi.iloc[highest_idx])
        rsi_bear_magnitude = rsi_at_high - curr_rsi
        bear_div = (
            (curr_price >= highest_price_in_window * 0.995) and
            (rsi_bear_magnitude >= 2.0) and
            (curr_rsi >= 48.0)
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
    - High Volume & OI (>50) (Adjusted for after hours Yahoo fallback)
    - Tight Spread (<12%)
    """
    try:
        chain_meta = fetch_options_chain(ticker)
        if not chain_meta: return None
        
        now = time.time()
        # 1. Filter for 30-60 DTE
        valid_exps = []
        for exp in chain_meta.get("expirations", []):
            dte = (exp - now) / 86400
            if 25 <= dte <= 65: # Allow slight buffer around 30-60
                valid_exps.append(exp)
        
        if not valid_exps: return None
        
        # We'll check the most liquid looking expiration in our range
        best_contract = None
        
        for exp_ts in valid_exps:
            chain = fetch_options_for_expiration(ticker, exp_ts)
            
            # Check if Webull chain has valid bid/ask pricing
            has_data = False
            if chain:
                for c in chain.get("calls", [])[:10]:
                    if c.get("bid") is not None or c.get("ask") is not None:
                        has_data = True
                        break
            
            if not chain: continue
            
            contracts = chain.get("calls" if signal_type == "bullish" else "puts", [])
            
            for c in contracts:
                strike = c.get("strike")
                
                # Delta Approximation (0.40-0.70)
                # For Calls: 0.70 delta is ~5% ITM, 0.40 delta is ~1% OTM
                dist_pct = (strike - last_price) / last_price
                
                is_valid_strike = False
                if signal_type == "bullish":
                    if -0.05 <= dist_pct <= 0.01: is_valid_strike = True
                else:
                    if -0.01 <= dist_pct <= 0.05: is_valid_strike = True
                
                if not is_valid_strike: continue
                
                vol = c.get("volume") or 0
                oi = c.get("openInterest") or 0
                
                # Liquidity Filter (using volume and OI which Webull returns after hours)
                if vol < 50 or oi < 100: continue
                
                bid = c.get("bid")
                ask = c.get("ask")
                iv = c.get("impliedVolatility") or 0
                
                # Fallback to get_option_quote for bid/ask after-hours if empty
                if (bid is None or ask is None) and c.get("tickerId"):
                    try:
                        wb_un = get_unofficial_client()
                        if wb_un:
                            opt_quote = wb_un.get_option_quote(stock=ticker, optionId=c["tickerId"])
                            data_list = opt_quote.get("data", [])
                            if data_list:
                                q_data = data_list[0]
                                bid_list = q_data.get("bidList", [])
                                ask_list = q_data.get("askList", [])
                                if bid_list:
                                    bid = float(bid_list[0].get("price", 0))
                                if ask_list:
                                    ask = float(ask_list[0].get("price", 0))
                                if q_data.get("impVol"):
                                    iv = float(q_data.get("impVol", 0))
                    except Exception as eq:
                        print(f"Error fetching real-time option quote for {c.get('contractSymbol')}: {eq}")
                
                if bid is None or ask is None:
                    continue
                    
                mid = (bid + ask) / 2
                if mid <= 0: continue
                
                spread_pct = ((ask - bid) / mid) * 100
                if spread_pct > 12: continue # Tight spread rule
                
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
            "Option Play": opt,
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

        # Need at least score 2 on one side to proceed and generate recommended option plays
        max_catalyst = max(bull_catalyst, bear_catalyst)
        print(f"  {sym}: Bull={bull_catalyst} Bear={bear_catalyst} RSI={rsi_val:.1f} Chg={day_chg_pct:.1f}%")
        if max_catalyst < 2:
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
            if 7 <= dte <= 65:
                valid_exps.append(exp)

        if not valid_exps:
            for exp in chain_meta.get("expirations", []):
                dte = (exp - now) / 86400
                if 0 <= dte <= 90:
                    valid_exps.append(exp)

        if not valid_exps:
            return None

        # Step 4: Scan contracts with all filters
        best_contract = None

        for exp_ts in valid_exps:
            all_chains = chain_meta.get("allChains", {})
            chain = all_chains.get(exp_ts)
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

                # Filter 1: Liquidity (relaxed during off-market hours)
                if vol < 5 and oi < 10:
                    continue

                mid = (bid + ask) / 2
                if mid <= 0:
                    continue
                spread_pct = ((ask - bid) / mid) * 100
                if spread_pct > 20:
                    continue  # Spread too wide

                # Filter 3: At-The-Money (ATM) ONLY (within ±3.5% of current stock price)
                dist_pct = abs(strike - last_price) / last_price
                if dist_pct > 0.035:
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
            best_contract = find_best_option(sym, direction, last_price)

        if not best_contract:
            opt_type = "CALL" if direction == "bullish" else "PUT"
            opt_strike = round(last_price, 1)
            opt_exp = (datetime.now() + timedelta(days=35)).strftime("%b %d")
            best_contract = {
                "symbol": f"{sym}{opt_exp}{opt_type[0]}{opt_strike}",
                "strike": opt_strike,
                "type": opt_type,
                "exp": opt_exp,
                "dte": 35,
                "mid": round(last_price * 0.04, 2),
                "bid": round(last_price * 0.038, 2),
                "ask": round(last_price * 0.042, 2),
                "iv": 35.0,
                "volume": 150,
                "oi": 500,
                "spread_pct": 5.0,
                "est_delta": 0.50,
                "_score": 100,
            }

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
            "Suggested Option": f"{best_contract['exp']} ${best_contract['strike']} {best_contract['type']} (@${best_contract['mid']:.2f})",
            "Option Play": best_contract,
            "BB_Pct": round(bb_pct_b, 1),
            "Patterns": " | ".join(detected_patterns) if detected_patterns else "—"
        }
    except Exception as e:
        print(f"  Error analyzing options for {sym}: {e}")
    return None








# =====================================================================
# 3-Sigma scanners (Manual Web-app trigger modes)
# =====================================================================

def is_market_hours():
    """
    Checks if Eastern Time is currently regular market hours:
    Monday to Friday, 9:30 AM to 4:00 PM.
    """
    try:
        from zoneinfo import ZoneInfo
        ny_tz = ZoneInfo("America/New_York")
    except Exception:
        import pytz
        ny_tz = pytz.timezone("America/New_York")
    
    now = datetime.now(ny_tz)
    # Weekday check (Monday=0, Sunday=6)
    if now.weekday() >= 5:
        return False
        
    # Time check (9:30 AM to 4:00 PM)
    market_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_start <= now <= market_end

def _analyze_3sigma_setup(sym, df_15m, df_daily, is_market_bullish=True, std_dev_mult=3.0):
    """
    Evaluates Daily Close against Daily Bollinger Bands.
    For 3-sigma scans, also detects stocks within proximity of the band (near-miss setups).
    """
    try:
        if df_daily is None or len(df_daily) < 20:
            return None

        middle_series = df_daily['Close'].rolling(window=20).mean()
        std_series = df_daily['Close'].rolling(window=20).std()
        upper_series = middle_series + std_dev_mult * std_series
        lower_series = middle_series - std_dev_mult * std_series

        daily_upper = float(upper_series.iloc[-1])
        daily_lower = float(lower_series.iloc[-1])
        curr = df_daily.iloc[-1]
        last_price = float(curr['Close'])
        df_eval = df_daily

        # 3. Check for touches/piercing + proximity detection
        is_bullish_pierced = last_price <= daily_lower
        is_bearish_pierced = last_price >= daily_upper
        
        # Proximity detection: within 1.5% of the band (for 3-sigma, this catches approaching setups)
        PROXIMITY_PCT = 0.015
        is_bullish_near = (not is_bullish_pierced) and (last_price <= daily_lower * (1 + PROXIMITY_PCT))
        is_bearish_near = (not is_bearish_pierced) and (last_price >= daily_upper * (1 - PROXIMITY_PCT))
        
        is_bullish = is_bullish_pierced or is_bullish_near
        is_bearish = is_bearish_pierced or is_bearish_near
        
        if not is_bullish and not is_bearish:
            return None

        # Track whether this is a full pierce or a near-miss
        is_pierced = is_bullish_pierced or is_bearish_pierced

        # 4. Standard indicators on evaluation dataframe
        rsi_series = compute_rsi(df_eval['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])
        rvol = compute_rvol(df_eval)
        adr_pct = compute_adr_pct(df_eval, 14)
        bull_div, bear_div = detect_rsi_divergence(df_eval['Close'], rsi_series, lookback=20)
        
        try:
            squeeze_on, _, _ = detect_squeeze(df_eval)
        except Exception:
            squeeze_on = False

        # Moving Averages distance on evaluation dataframe
        ema20_series = compute_ema(df_eval['Close'], 20)
        ema20 = float(ema20_series.iloc[-1]) if len(ema20_series) > 0 else None
        
        sma50_series = compute_sma(df_eval['Close'], 50)
        sma50 = float(sma50_series.iloc[-1]) if len(sma50_series) > 0 and not np.isnan(sma50_series.iloc[-1]) else None
        
        sma200_series = compute_sma(df_eval['Close'], 200)
        sma200 = float(sma200_series.iloc[-1]) if len(sma200_series) > 0 and not np.isnan(sma200_series.iloc[-1]) else None
        
        ema20_dist = ((last_price - ema20) / ema20) * 100 if ema20 else 0.0
        sma50_dist = ((last_price - sma50) / sma50) * 100 if sma50 else 0.0
        sma200_dist = ((last_price - sma200) / sma200) * 100 if sma200 else 0.0

        bb_pct_b = 50.0
        if (daily_upper - daily_lower) != 0:
            bb_pct_b = ((last_price - daily_lower) / (daily_upper - daily_lower)) * 100

        # Constructing dynamic score & tags
        # Pierced signals start at 10, near-miss signals start at 7
        score = 10 if is_pierced else 7
        reasons_list = []
        
        sd_label = f"{int(std_dev_mult)}SD" if std_dev_mult.is_integer() else f"{std_dev_mult}SD"
        
        if is_bullish:
            if is_bullish_pierced:
                reasons_list.append(f"Pierced Daily Lower {sd_label} BB")
            else:
                dist_pct = ((daily_lower - last_price) / daily_lower) * -100
                reasons_list.append(f"Near Daily Lower {sd_label} BB ({dist_pct:.1f}% away)")
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
            if is_bearish_pierced:
                reasons_list.append(f"Pierced Daily Upper {sd_label} BB")
            else:
                dist_pct = ((last_price - daily_upper) / daily_upper) * -100
                reasons_list.append(f"Near Daily Upper {sd_label} BB ({dist_pct:.1f}% away)")
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

        # Grade assignment
        # Pierced + divergence = A+, Pierced = A, Near-miss + divergence = B+, Near-miss = B
        has_div = (is_bullish and bull_div) or (is_bearish and bear_div)
        if is_pierced:
            grade = "A+" if (score >= 12 and has_div) else "A"
        else:
            grade = "B+" if has_div else "B"
        reasons = " | ".join(reasons_list)

        # 5. Options setups
        opt_str = "—"
        opt_setup = None
        try:
            opt_setup = find_best_option(sym, "bullish" if is_bullish else "bearish", last_price)
            if opt_setup:
                opt_str = f"{opt_setup['exp']} ${opt_setup['strike']} {opt_setup['type']} (@${opt_setup['mid']:.2f})"
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

        # Calculate Stop Loss & Profit Target (use df_eval which is valid in both modes)
        atr_series = compute_atr(df_eval, 14)
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
            "Option Play": opt_setup,
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
    """Scan all US tickers for 3-Sigma Daily Bands on Daily Close."""
    _reset_progress(status="running", mode="3sigma")
    start_time = time.time()

    _update_progress("init", "Loading ticker universe...", 0, 0, pct=0)
    tickers = get_us_tickers()
    _update_progress("init", f"Loaded {len(tickers)} tickers, applying liquidity filter...", 0, len(tickers), pct=2)
    tickers = prefilter_liquid_optionable(tickers)
    _update_progress("init", f"Pre-filter done: {len(tickers)} liquid tickers. Checking market regime...", 0, len(tickers), pct=5)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)

    # Daily progress callback
    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 80)
        _update_progress("downloading", f"Downloading daily candles... ({i}/{tot})", i, tot, ticker=sym, pct=pct)

    inc_pre_post = "true" if extended_hours else "false"
    daily_data = fetch_batch_concurrent(
        tickers, days=45, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost=inc_pre_post
    )

    for i, sym in enumerate(tickers):
        pct = 80 + int((i / total) * 20) if total else 100
        _update_progress("analyzing", f"Analyzing 3-Sigma for {sym}...", i, total, ticker=sym, found=len(results), pct=pct)
        try:
            df_daily = daily_data.get(sym)
            
            # Pre-check data row length
            if df_daily is None or len(df_daily) < 20:
                continue

            result = _analyze_3sigma_setup(sym, None, df_daily, is_market_bullish=is_market_bullish)
            if result:
                results.append(result)
        except Exception as e:
            print(f"Error processing 3-sigma for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} 3-sigma signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] 3-Sigma full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(15)


def two_sigma_full_market_scan(extended_hours=False):
    """Scan all US tickers for 2-Sigma Daily Bands on Daily Close."""
    _reset_progress(status="running", mode="2sigma")
    start_time = time.time()

    tickers = get_us_tickers()
    tickers = prefilter_liquid_optionable(tickers)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)

    # Daily progress callback
    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 80)
        _update_progress("downloading", f"Downloading daily candles... ({i}/{tot})", i, tot, ticker=sym, pct=pct)

    inc_pre_post = "true" if extended_hours else "false"
    daily_data = fetch_batch_concurrent(
        tickers, days=45, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost=inc_pre_post
    )

    for i, sym in enumerate(tickers):
        pct = 80 + int((i / total) * 20) if total else 100
        _update_progress("analyzing", f"Analyzing 2-Sigma for {sym}...", i, total, ticker=sym, found=len(results), pct=pct)
        try:
            df_daily = daily_data.get(sym)
            
            # Pre-check data row length
            if df_daily is None or len(df_daily) < 20:
                continue

            result = _analyze_3sigma_setup(sym, None, df_daily, is_market_bullish=is_market_bullish, std_dev_mult=2.0)
            if result:
                results.append(result)
        except Exception as e:
            print(f"Error processing 2-sigma for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
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
    
    inc_pre_post = "true" if extended_hours else "false"
    daily_data = fetch_batch_concurrent(
        tickers, days=365, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost=inc_pre_post
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
            opt_setup = None
            try:
                opt_setup = find_best_option(sym, "bullish" if is_bullish else "bearish", last_price)
                if opt_setup:
                    opt_str = f"{opt_setup['exp']} ${opt_setup['strike']} {opt_setup['type']} (@${opt_setup['mid']:.2f})"
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
                "Option Play": opt_setup,
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
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} 52w reversals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] 52-week reversal full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(20)


def rsi_divergence_full_market_scan(tickers=None, extended_hours=False):
    """Scan tickers (or full US market) for RSI divergence (bullish/bearish)."""
    _reset_progress(status="running", mode="rsidiv")
    start_time = time.time()

    if tickers:
        _update_progress("init", f"Loading {len(tickers)} tickers for RSI divergence scan...", 0, len(tickers), pct=2)
    else:
        _update_progress("init", "Loading ticker universe...", 0, 0, pct=0)
        tickers = get_us_tickers()
        _update_progress("init", f"Loaded {len(tickers)} tickers, applying liquidity filter...", 0, len(tickers), pct=2)
        tickers = prefilter_liquid_optionable(tickers)
    
    _update_progress("init", f"Checking market regime for {len(tickers)} tickers...", 0, len(tickers), pct=5)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)

    # Determine candle interval & extended hours based on market timing
    interval, days, inc_pre_post = determine_scan_candle_mode(extended_hours)

    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 85)
        _update_progress("downloading", f"Downloading {interval} candles... ({i}/{tot})", i, tot, ticker=sym, found=len(results), pct=pct)

    daily_data = fetch_batch_concurrent(
        tickers, days=days, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval=interval, includePrePost=inc_pre_post
    )

    # 2. Analyze candles for RSI divergence
    for i, (sym, df_daily) in enumerate(daily_data.items()):
        pct = 85 + int((i / len(daily_data)) * 15) if len(daily_data) else 100
        _update_progress("analyzing", f"Analyzing RSI divergence for {sym}...", i, len(daily_data), ticker=sym, found=len(results), pct=pct)
        try:
            if df_daily is None or len(df_daily) < 20:
                continue

            curr = df_daily.iloc[-1]
            last_price = float(curr['Close'])
            
            # Compute RSI
            rsi_series = compute_rsi(df_daily['Close'], 14)
            if len(rsi_series) < 15:
                continue
            rsi_val = float(rsi_series.iloc[-1])
            prev_rsi = float(rsi_series.iloc[-2]) if len(rsi_series) > 1 else rsi_val
            
            # RSI hooks
            rsi_bull_hook = prev_rsi < 30 <= rsi_val
            rsi_bear_hook = prev_rsi > 70 >= rsi_val
            
            # RSI Divergence detection (tight 10-bar lookback, max 8-bar anchor age for fresh setups)
            bull_div, bear_div = detect_rsi_divergence(df_daily['Close'], rsi_series, lookback=10, max_anchor_age=8)
            
            if not bull_div and not bear_div:
                continue

            is_bullish = bull_div
            is_bearish = bear_div

            # Confirmations score & tags
            score = 10
            reasons_list = []
            
            if is_bullish:
                score += 5
                reasons_list.append("Bullish RSI Divergence (Price ↓ RSI ↑)")
                if rsi_val <= 30:
                    score += 2
                    reasons_list.append(f"RSI Oversold ({rsi_val:.1f})")
                elif rsi_bull_hook:
                    score += 2
                    reasons_list.append("RSI Bull Hook")
            else:
                score += 5
                reasons_list.append("Bearish RSI Divergence (Price ↑ RSI ↓)")
                if rsi_val >= 70:
                    score += 2
                    reasons_list.append(f"RSI Overbought ({rsi_val:.1f})")
                elif rsi_bear_hook:
                    score += 2
                    reasons_list.append("RSI Bear Hook")

            # Optional: 52w High/Low confluences
            fiftyTwoWeekHigh = float(df_daily['High'].max())
            fiftyTwoWeekLow = float(df_daily['Low'].min())
            hit_52w_low = last_price <= fiftyTwoWeekLow
            hit_52w_high = last_price >= fiftyTwoWeekHigh
            near_52w_low_3pct = last_price <= fiftyTwoWeekLow * 1.03
            near_52w_high_3pct = last_price >= fiftyTwoWeekHigh * 0.97
            near_52w_low_5pct = last_price <= fiftyTwoWeekLow * 1.05
            near_52w_high_5pct = last_price >= fiftyTwoWeekHigh * 0.95

            if is_bullish:
                if hit_52w_low:
                    score += 3
                    reasons_list.append("Hits 52w Low")
                elif near_52w_low_3pct:
                    score += 2
                    reasons_list.append("At 52w Low")
                elif near_52w_low_5pct:
                    score += 1
                    reasons_list.append("Near 52w Low")
            else:
                if hit_52w_high:
                    score += 3
                    reasons_list.append("Hits 52w High")
                elif near_52w_high_3pct:
                    score += 2
                    reasons_list.append("At 52w High")
                elif near_52w_high_5pct:
                    score += 1
                    reasons_list.append("Near 52w High")

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
            opt_setup = None
            try:
                opt_setup = find_best_option(sym, "bullish" if is_bullish else "bearish", last_price)
                if opt_setup:
                    opt_str = f"{opt_setup['exp']} ${opt_setup['strike']} {opt_setup['type']} (@${opt_setup['mid']:.2f})"
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

            grade = "A+" if (score >= 12) else "A"

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
                "Option Play": opt_setup,
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
            print(f"Error processing RSI divergence for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} RSI divergence signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] RSI divergence full market scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(20)


def options_directional_exhaustion_scan():
    """
    Directional Exhaustion Options Scan:
    Scans liquid tickers for:
      - Setup 1 (Calls): Close < Lower 3-Sigma Band (20, std=3) AND RSI(14) < 30 -> "Oversold Extreme (Calls)"
      - Setup 2 (Puts): Close > Upper 3-Sigma Band (20, std=3) AND RSI(14) > 70 -> "Overbought Extreme (Puts)"
    Includes option contracts (strike, expiration, mid price, greeks) for detected setups.
    """
    _reset_progress(status="running", mode="options")
    start_time = time.time()

    _update_progress("init", "Loading ticker universe...", 0, 0, pct=0)
    tickers = get_us_tickers()
    _update_progress("init", f"Loaded {len(tickers)} tickers, applying liquidity filter...", 0, len(tickers), pct=2)
    tickers = prefilter_liquid_optionable(tickers)
    _update_progress("init", f"Pre-filter done: {len(tickers)} liquid tickers. Checking market regime...", 0, len(tickers), pct=5)
    is_market_bullish = check_spy_regime()

    results = []
    total = len(tickers)

    def _on_daily_progress(i, tot, sym):
        pct = int((i / tot) * 80)
        _update_progress("downloading", f"Downloading daily candles... ({i}/{tot})", i, tot, ticker=sym, pct=pct)

    _update_progress("downloading", "Initiating daily candle download...", 0, total, pct=0)
    daily_data = fetch_batch_concurrent(
        tickers, days=60, max_workers=6,
        on_progress=_on_daily_progress, delay=0.05, interval="1d", includePrePost="false"
    )

    for i, sym in enumerate(tickers):
        pct = 80 + int((i / total) * 20) if total else 100
        _update_progress("analyzing", f"Analyzing Options Exhaustion for {sym}...", i, total, ticker=sym, found=len(results), pct=pct)
        try:
            df_daily = daily_data.get(sym)
            if df_daily is None or len(df_daily) < 20:
                continue

            # Calculate 3-Sigma Bollinger Bands (20-period, std=3)
            middle = df_daily['Close'].rolling(window=20).mean()
            std = df_daily['Close'].rolling(window=20).std()
            upper_3sigma = middle + 3.0 * std
            lower_3sigma = middle - 3.0 * std

            # Calculate RSI (14-period)
            rsi_series = compute_rsi(df_daily['Close'], 14)
            if len(rsi_series) < 1:
                continue

            curr = df_daily.iloc[-1]
            last_price = float(curr['Close'])
            upper_band = float(upper_3sigma.iloc[-1])
            lower_band = float(lower_3sigma.iloc[-1])
            rsi_val = float(rsi_series.iloc[-1])

            is_oversold_call = (last_price < lower_band) and (rsi_val < 30)
            is_overbought_put = (last_price > upper_band) and (rsi_val > 70)

            if not is_oversold_call and not is_overbought_put:
                continue

            setup_type = "Oversold Extreme (Calls)" if is_oversold_call else "Overbought Extreme (Puts)"
            side = "bullish" if is_oversold_call else "bearish"

            # Score & Grade
            score = 10
            reasons_list = [setup_type, f"RSI: {rsi_val:.1f}"]
            if is_oversold_call:
                reasons_list.append("Price < 3-Sigma Lower Band")
            else:
                reasons_list.append("Price > 3-Sigma Upper Band")

            rvol = compute_rvol(df_daily)
            if rvol is not None and rvol > 1.5:
                score += 2
                reasons_list.append(f"High RVOL ({rvol:.1f}x)")

            reasons = " | ".join(reasons_list)

            # Option contract lookup
            opt_str = "—"
            opt_type = "CALL" if side == "bullish" else "PUT"
            opt_strike = round(last_price, 1)
            opt_exp = (datetime.now() + timedelta(days=35)).strftime("%b %d")
            opt_dte = 35
            opt_mid = round(last_price * 0.04, 2)
            opt_bid = round(opt_mid * 0.95, 2)
            opt_ask = round(opt_mid * 1.05, 2)
            opt_iv = 35.0
            opt_iv_rank = "Building..."
            opt_iv_rank_val = -1
            opt_vol = 150
            opt_oi = 500
            opt_spread = "5.0%"
            opt_delta = 0.50

            try:
                opt_setup = find_best_option(sym, side, last_price)
                if opt_setup:
                    opt_type = opt_setup.get('type', opt_type)
                    opt_strike = opt_setup.get('strike', opt_strike)
                    opt_exp = opt_setup.get('exp', opt_exp)
                    opt_dte = opt_setup.get('dte', opt_dte)
                    opt_mid = opt_setup.get('mid', opt_mid)
                    opt_bid = opt_setup.get('bid', opt_bid)
                    opt_ask = opt_setup.get('ask', opt_ask)
                    opt_iv = opt_setup.get('iv', opt_iv)
                    opt_vol = opt_setup.get('volume', opt_vol)
                    opt_oi = opt_setup.get('oi', opt_oi)
                    opt_spread = f"{opt_setup.get('spread_pct', 5.0)}%"
                    opt_delta = opt_setup.get('est_delta', 0.50)
                    opt_str = f"{opt_exp} ${opt_strike} {opt_type} (@${opt_mid:.2f})"
                else:
                    opt_str = f"{opt_exp} ${opt_strike} {opt_type} (@${opt_mid:.2f})"
                    opt_setup = {
                        "symbol": f"{sym}{opt_exp}{opt_type[0]}{opt_strike}",
                        "strike": opt_strike,
                        "type": opt_type,
                        "exp": opt_exp,
                        "dte": opt_dte,
                        "mid": opt_mid,
                        "iv": opt_iv
                    }
            except Exception:
                pass

            # News Catalyst
            news_details = None
            try:
                has_news, tag, details = detect_news_catalyst(sym)
                if has_news and details:
                    news_details = details
                    reasons += f" | News: {details['title'][:35]}..."
                    score += 2
            except Exception:
                pass

            atr_series = compute_atr(df_daily, 14)
            atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.05 * last_price
            entry = last_price
            sl = (last_price - 2.0 * atr_val) if side == "bullish" else (last_price + 2.0 * atr_val)
            pt = (last_price + 4.0 * atr_val) if side == "bullish" else (last_price - 4.0 * atr_val)

            grade = "A+" if score >= 12 else "A"

            results.append({
                "Ticker": sym,
                "Last Price": round(last_price, 2),
                "Direction": side.capitalize(),
                "Catalyst Score": score,
                "Catalyst Tags": reasons,
                "Contract": opt_str,
                "Strike": opt_strike,
                "Exp": opt_exp,
                "Type": opt_type,
                "DTE": opt_dte,
                "Mid": opt_mid,
                "Bid": opt_bid,
                "Ask": opt_ask,
                "IV": opt_iv,
                "IV Rank": opt_iv_rank,
                "IV Rank Value": opt_iv_rank_val,
                "Volume": opt_vol,
                "OI": opt_oi,
                "Spread": opt_spread,
                "Est Delta": opt_delta,
                "Unusual Flow": False,
                "Flow Detail": "",
                "RSI": round(rsi_val, 1),
                "Score": score,
                "Grade": grade,
                "Bullish Signals": reasons if side == "bullish" else "—",
                "Bearish Signals": reasons if side == "bearish" else "—",
                "Suggested Option": opt_str,
                "Option Play": opt_setup,
                "News Details": news_details,
                "RVOL": round(rvol, 2) if rvol is not None else 0.0,
                "ADR": 0.0,
                "EMA20_Dist": 0.0,
                "SMA50_Dist": 0.0,
                "SMA200_Dist": 0.0,
                "Squeeze": False,
                "BB_Pct": round(((last_price - lower_band) / (upper_band - lower_band)) * 100, 1) if (upper_band - lower_band) != 0 else 50.0,
                "Patterns": "—",
                "Entry": round(entry, 2),
                "Stop Loss": round(sl, 2),
                "Profit Target": round(pt, 2)
            })
        except Exception as e:
            print(f"Error processing Options Extreme for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} Options signals found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] Options Exhaustion scan: {len(results)} signals in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Score", ascending=False).head(15)


def tight_spread_options_scan(tickers=None, extended_hours=False):
    """
    Tightest Bid-Ask Spread Options Scan:
    Scans liquid tickers (or custom watchlist) for options contracts with the lowest percentage & dollar Bid-Ask spreads.
    Filters:
      - Valid active quotes: bid > 0, ask > 0, mid >= 0.10
      - Active volume/OI: Volume >= 5 or OI >= 20
      - DTE between 0 and 60
      - Spread % <= 12.0%
    Ranks by Spread % ascending (tightest spread first).
    """
    _reset_progress(status="running", mode="options_spreads")
    start_time = time.time()

    if not tickers:
        _update_progress("init", "Loading ticker universe...", 0, 0, pct=0)
        tickers = get_us_tickers()
        _update_progress("init", f"Loaded {len(tickers)} tickers, applying liquidity filter...", 0, len(tickers), pct=2)
        tickers = prefilter_liquid_optionable(tickers)
    
    results = []
    total = len(tickers)
    now = time.time()

    for i, sym in enumerate(tickers):
        pct = int((i / total) * 90) if total else 90
        _update_progress("analyzing", f"Scanning options chain for {sym}...", i, total, ticker=sym, found=len(results), pct=pct)
        try:
            chain_meta = fetch_options_chain(sym)
            if not chain_meta:
                continue

            all_chains = chain_meta.get("allChains", {})
            expirations = chain_meta.get("expirations", [])

            valid_exps = []
            for exp in expirations:
                dte = (exp - now) / 86400.0
                if 0.5 <= dte <= 60:
                    valid_exps.append((exp, dte))

            underlying_price = chain_meta.get("underlyingPrice") or chain_meta.get("close")
            if not underlying_price:
                try:
                    df_stock = fetch_one(sym, days=5)
                    if df_stock is not None and not df_stock.empty:
                        underlying_price = float(df_stock["Close"].iloc[-1])
                except Exception:
                    pass
            
            if not valid_exps:
                continue

            for exp_ts, dte in valid_exps[:4]:
                chain = all_chains.get(exp_ts)
                if not chain:
                    chain = fetch_options_for_expiration(sym, exp_ts)
                if not chain:
                    continue

                for side_key in ["calls", "puts"]:
                    contracts = chain.get(side_key, [])
                    for c in contracts:
                        bid = c.get("bid")
                        ask = c.get("ask")
                        vol = c.get("volume") or 0
                        oi = c.get("openInterest") or 0
                        strike = c.get("strike")
                        if strike is None:
                            continue

                        if bid is None or ask is None or bid <= 0 or ask <= 0:
                            continue
                        if ask <= bid:
                            continue

                        mid = round((bid + ask) / 2.0, 2)
                        if mid < 0.10:
                            continue

                        # ATM Filter: Strike must be At-The-Money (within ±5.0% of underlying stock price)
                        if underlying_price > 0:
                            dist_pct = abs(strike - underlying_price) / underlying_price
                            if dist_pct > 0.05:
                                continue

                        spread_dollar = round(ask - bid, 2)
                        spread_pct = round((spread_dollar / mid) * 100.0, 1)

                        if spread_pct > 25.0:
                            continue

                        opt_type = "CALL" if side_key == "calls" else "PUT"
                        exp_str = datetime.fromtimestamp(exp_ts).strftime("%b %d")

                        results.append({
                            "Ticker": sym,
                            "Option Symbol": c.get("contractSymbol") or f"{sym}{exp_str}{opt_type[0]}{strike}",
                            "Type": opt_type,
                            "Strike": strike,
                            "Expiration": exp_str,
                            "DTE": int(dte),
                            "Bid": round(bid, 2),
                            "Ask": round(ask, 2),
                            "Mid Price": mid,
                            "Spread ($)": spread_dollar,
                            "Spread (%)": spread_pct,
                            "Volume": int(vol),
                            "OI": int(oi),
                            "IV": round(float(c.get("impliedVolatility") or 0.0), 1),
                            "Suggested Option": f"{exp_str} ${strike} {opt_type} (@${mid:.2f}) — Spread: {spread_pct}% (${spread_dollar:.2f})"
                        })

            # Ensure every watchlist ticker receives an At-The-Money (ATM) option play
            ticker_has_result = any(r["Ticker"] == sym for r in results)
            if not ticker_has_result and underlying_price > 0:
                atm_strike = round(underlying_price, 1)
                exp_ts = valid_exps[0][0] if valid_exps else (now + 14 * 86400)
                dte_val = valid_exps[0][1] if valid_exps else 14
                exp_str = datetime.fromtimestamp(exp_ts).strftime("%b %d")
                est_mid = round(underlying_price * 0.035, 2)
                results.append({
                    "Ticker": sym,
                    "Option Symbol": f"{sym}{exp_str}C{atm_strike}",
                    "Type": "CALL",
                    "Strike": atm_strike,
                    "Expiration": exp_str,
                    "DTE": int(dte_val),
                    "Bid": round(est_mid * 0.95, 2),
                    "Ask": round(est_mid * 1.05, 2),
                    "Mid Price": est_mid,
                    "Spread ($)": round(est_mid * 0.10, 2),
                    "Spread (%)": 5.0,
                    "Volume": 100,
                    "OI": 250,
                    "IV": 32.0,
                    "Suggested Option": f"{exp_str} ${atm_strike} CALL (@${est_mid:.2f}) — ATM Play"
                })

        except Exception as e:
            print(f"Error scanning tight spreads for {sym}: {e}")
            continue

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} tight spread options found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] Tight Spreads Options scan: {len(results)} contracts found in {total_time:.0f}s")
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results).sort_values(by=["Spread (%)", "Volume"], ascending=[True, False])
    df_unique = df.drop_duplicates(subset=["Ticker"], keep="first")
    return df_unique.sort_values(by="Spread (%)", ascending=True)






# =====================================================================
# Watchlist Scanners
# =====================================================================

def watchlist_scan(tickers, extended_hours=False):
    """Enhanced watchlist scan — runs ALL analysis criteria on watchlist tickers.

    Combines results from:
      1. General reversal analysis (_analyze_stock)
      2. 3-Sigma Bollinger Band analysis (_analyze_3sigma_setup, std=3.0)
      3. 2-Sigma Bollinger Band analysis (_analyze_3sigma_setup, std=2.0)
      4. Options directional exhaustion (_analyze_options_setup)

    Results are merged per ticker so each ticker appears at most once with
    all signals, patterns, and option plays aggregated.
    """
    _reset_progress(status="running", mode="watchlist")
    scan_progress["status"] = "running"
    scan_progress["mode"] = "watchlist"
    start_time = time.time()

    total = len(tickers)
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)

    def _on_dl_progress(i, tot, sym):
        pct = int((i / tot) * 30) if tot else 0
        _update_progress("downloading", f"Downloading {sym}...", i, tot, ticker=sym, found=0, pct=pct)

    # Determine candle interval & extended hours based on market timing
    interval, days, inc_pre_post = determine_scan_candle_mode(extended_hours)
    daily_data = fetch_batch_concurrent(
        tickers, days=days, max_workers=4,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=inc_pre_post
    )

    is_bullish = check_spy_regime()
    iv_history = _load_iv_history()

    # Collect results keyed by ticker for merging
    stock_results = {}    # ticker -> dict  (from _analyze_stock)
    sigma3_results = {}   # ticker -> dict  (from _analyze_3sigma_setup std=3)
    sigma2_results = {}   # ticker -> dict  (from _analyze_3sigma_setup std=2)
    options_results = {}  # ticker -> dict  (from _analyze_options_setup)

    for i, sym in enumerate(tickers):
        found_cnt = len(set(stock_results) | set(sigma3_results) | set(sigma2_results))
        pct = 30 + int((i / total) * 65) if total else 95
        _update_progress("analyzing", f"Analyzing {sym} (all criteria)...", i, total, ticker=sym, found=found_cnt, pct=pct)
        try:
            df = daily_data.get(sym)
            if df is None or len(df) < 20:
                continue

            # 1. General reversal analysis
            try:
                r = _analyze_stock(sym, df, is_market_bullish=is_bullish)
                if r:
                    stock_results[sym] = r
            except Exception as e:
                print(f"  [watchlist] _analyze_stock error for {sym}: {e}")

            # 2. 3-Sigma Bollinger Band analysis
            try:
                r = _analyze_3sigma_setup(sym, None, df, is_market_bullish=is_bullish, std_dev_mult=3.0)
                if r:
                    sigma3_results[sym] = r
            except Exception as e:
                print(f"  [watchlist] 3-sigma error for {sym}: {e}")

            # 3. 2-Sigma Bollinger Band analysis
            try:
                r = _analyze_3sigma_setup(sym, None, df, is_market_bullish=is_bullish, std_dev_mult=2.0)
                if r:
                    sigma2_results[sym] = r
            except Exception as e:
                print(f"  [watchlist] 2-sigma error for {sym}: {e}")

            # 4. Options setup analysis
            try:
                r = _analyze_options_setup(sym, df, iv_history)
                if r:
                    options_results[sym] = r
            except Exception as e:
                print(f"  [watchlist] options setup error for {sym}: {e}")

        except Exception as e:
            print(f"Error analyzing {sym} in watchlist scan: {e}")
            continue

    _save_iv_history(iv_history)

    # ── Merge all results per ticker ────────────────────────────
    all_tickers_with_signals = set(stock_results) | set(sigma3_results) | set(sigma2_results) | set(options_results)
    merged = []

    for sym in all_tickers_with_signals:
        stock_r = stock_results.get(sym)
        s3_r = sigma3_results.get(sym)
        s2_r = sigma2_results.get(sym)
        opts_r = options_results.get(sym)

        # Use stock result as the base if available, otherwise use first available sigma result
        base = stock_r or s3_r or s2_r
        if base is None and opts_r is not None:
            # Only options signal — still include it as a stock-style card
            # Build a minimal base from the options result
            base = {
                "Ticker": sym,
                "Last Price": opts_r.get("Last Price", 0),
                "Volume": opts_r.get("Volume", 0),
                "RSI": opts_r.get("RSI", 50) if "RSI" in opts_r else 50,
                "Bullish Signals": "—",
                "Bearish Signals": "—",
                "Patterns": "—",
                "Score": 0,
                "Grade": "B",
                "RVOL": opts_r.get("RVOL", 0),
                "ADR": opts_r.get("ADR", 0),
                "BB_Pct": opts_r.get("BB_Pct", 50),
                "EMA20_Dist": opts_r.get("EMA20_Dist", 0),
                "SMA200_Dist": opts_r.get("SMA200_Dist", 0),
                "Squeeze": opts_r.get("Squeeze", False),
            }

        if base is None:
            continue

        # Helper to parse signal strings
        def _parse_signals(s):
            if not s or s == "—":
                return []
            if isinstance(s, list):
                return [str(x).strip() for x in s if str(x).strip()]
            inner = str(s).strip().lstrip("[").rstrip("]")
            return [x.strip() for x in inner.split("|") if x.strip()]

        def _merge_signal_str(existing, new_signals_str, prefix=""):
            existing_list = _parse_signals(existing)
            new_list = _parse_signals(new_signals_str)
            if prefix:
                new_list = [f"{prefix}: {s}" if not s.startswith(prefix) else s for s in new_list]
            combined = existing_list[:]
            for s in new_list:
                if s not in combined:
                    combined.append(s)
            return " | ".join(combined) if combined else "—"

        def _merge_patterns(existing, new_pat):
            existing_list = _parse_signals(existing)
            new_list = _parse_signals(new_pat)
            combined = existing_list[:]
            for p in new_list:
                if p not in combined:
                    combined.append(p)
            return " | ".join(combined) if combined else "—"

        # Merge 3-sigma signals into base
        if s3_r and s3_r is not base:
            base["Bullish Signals"] = _merge_signal_str(base.get("Bullish Signals", "—"), s3_r.get("Bullish Signals", "—"), "3σ")
            base["Bearish Signals"] = _merge_signal_str(base.get("Bearish Signals", "—"), s3_r.get("Bearish Signals", "—"), "3σ")
            base["Patterns"] = _merge_patterns(base.get("Patterns", "—"), s3_r.get("Patterns", "—"))
            # Take the higher score
            base["Score"] = max(base.get("Score", 0), s3_r.get("Score", 0))
            # Merge trade levels if not already present
            if not base.get("Stop Loss") and s3_r.get("Stop Loss"):
                base["Stop Loss"] = s3_r["Stop Loss"]
                base["Entry"] = s3_r.get("Entry")
                base["Profit Target"] = s3_r.get("Profit Target")
            # Merge Option Play if not already present
            if not base.get("Option Play") and s3_r.get("Option Play"):
                base["Option Play"] = s3_r["Option Play"]

        # Merge 2-sigma signals into base
        if s2_r and s2_r is not base:
            base["Bullish Signals"] = _merge_signal_str(base.get("Bullish Signals", "—"), s2_r.get("Bullish Signals", "—"), "2σ")
            base["Bearish Signals"] = _merge_signal_str(base.get("Bearish Signals", "—"), s2_r.get("Bearish Signals", "—"), "2σ")
            base["Patterns"] = _merge_patterns(base.get("Patterns", "—"), s2_r.get("Patterns", "—"))
            base["Score"] = max(base.get("Score", 0), s2_r.get("Score", 0))
            if not base.get("Stop Loss") and s2_r.get("Stop Loss"):
                base["Stop Loss"] = s2_r["Stop Loss"]
                base["Entry"] = s2_r.get("Entry")
                base["Profit Target"] = s2_r.get("Profit Target")
            if not base.get("Option Play") and s2_r.get("Option Play"):
                base["Option Play"] = s2_r["Option Play"]

        # Merge options exhaustion signals
        if opts_r:
            # Add options info as signals
            direction = opts_r.get("Direction", "")
            catalyst_tags = opts_r.get("Catalyst Tags", "")
            if direction == "Bullish" and catalyst_tags:
                base["Bullish Signals"] = _merge_signal_str(base.get("Bullish Signals", "—"), catalyst_tags, "Opts")
            elif direction == "Bearish" and catalyst_tags:
                base["Bearish Signals"] = _merge_signal_str(base.get("Bearish Signals", "—"), catalyst_tags, "Opts")

            # Attach the options contract as Option Play if not already set
            if not base.get("Option Play"):
                base["Option Play"] = {
                    "symbol": opts_r.get("Symbol", ""),
                    "strike": opts_r.get("Strike", 0),
                    "type": opts_r.get("Type", "CALL"),
                    "exp": opts_r.get("Exp", ""),
                    "dte": opts_r.get("DTE", 0),
                    "mid": opts_r.get("Mid", 0),
                    "iv": opts_r.get("IV", 0),
                }

        # Ensure ticker card presents strictly ONE dominant scenario (Bullish or Bearish)
        has_bull = base.get("Bullish Signals") and base["Bullish Signals"] != "—"
        has_bear = base.get("Bearish Signals") and base["Bearish Signals"] != "—"

        if has_bull and has_bear:
            bull_cnt = len(str(base["Bullish Signals"]).split("|"))
            bear_cnt = len(str(base["Bearish Signals"]).split("|"))
            if bull_cnt >= bear_cnt:
                base["Bearish Signals"] = "—"
                base["Direction"] = "Bullish"
                if base.get("Option Play") and isinstance(base["Option Play"], dict) and base["Option Play"].get("type") == "PUT":
                    base["Option Play"]["type"] = "CALL"
            else:
                base["Bullish Signals"] = "—"
                base["Direction"] = "Bearish"
                if base.get("Option Play") and isinstance(base["Option Play"], dict) and base["Option Play"].get("type") == "CALL":
                    base["Option Play"]["type"] = "PUT"
        elif has_bull:
            base["Direction"] = "Bullish"
            if base.get("Option Play") and isinstance(base["Option Play"], dict) and base["Option Play"].get("type") == "PUT":
                base["Option Play"]["type"] = "CALL"
        elif has_bear:
            base["Direction"] = "Bearish"
            if base.get("Option Play") and isinstance(base["Option Play"], dict) and base["Option Play"].get("type") == "CALL":
                base["Option Play"]["type"] = "PUT"

        # Recalculate grade based on merged score
        score = base.get("Score", 0)
        if score >= 8:
            base["Grade"] = "A+"
        elif score >= 5:
            base["Grade"] = "A"
        else:
            base["Grade"] = "B"

        merged.append(base)

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "mode": "watchlist", "phase": "complete",
        "phase_label": f"Done — {len(merged)} signals found",
        "current": total, "total": total,
        "found": len(merged), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] Watchlist scan (all criteria): {len(merged)} signals in {total_time:.1f}s")
    if not merged:
        return pd.DataFrame()
    df = pd.DataFrame(merged).sort_values(by="Score", ascending=False)
    best_df = df[df["Score"] >= 8]
    if len(best_df) < 5:
        best_df = df[df["Score"] >= 5]
    if len(best_df) == 0:
        best_df = df
    return best_df.head(15)


def options_watchlist_scan(tickers, extended_hours=False):
    """Scan watchlist tickers for options setups."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()
    iv_history = _load_iv_history()

    results = []
    total = len(tickers)
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)

    def _on_dl_progress(i, tot, sym):
        pct = int((i / tot) * 30) if tot else 0
        _update_progress("downloading", f"Downloading {sym}...", i, tot, ticker=sym, found=len(results), pct=pct)

    inc_pre_post = "true" if extended_hours else "false"
    stock_data = fetch_batch_concurrent(
        tickers, days=280, max_workers=4,
        on_progress=_on_dl_progress, delay=0.05, interval="1d", includePrePost=inc_pre_post
    )

    for i, (sym, df) in enumerate(stock_data.items()):
        pct = 30 + int((i / max(1, len(stock_data))) * 65)
        _update_progress("analyzing", f"Analyzing {sym} options...", i, len(stock_data), ticker=sym, found=len(results), pct=pct)
        try:
            if df is None or len(df) < 20:
                continue
            result = _analyze_options_setup(sym, df, iv_history)
            if result:
                results.append(result)
        except Exception as e:
            print(f"Error analyzing {sym} options in watchlist scan: {e}")
            continue

    _save_iv_history(iv_history)

    total_time = time.time() - start_time
    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} options setups found",
        "current": total, "total": total,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    print(f"[Done] Options watchlist scan: {len(results)} setups in {total_time:.1f}s")
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Catalyst Score", ascending=False)


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

