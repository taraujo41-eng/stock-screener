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
import warnings
from data_fetcher import fetch_batch, fetch_batch_concurrent, test_connection

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

# =====================================================================
# Candlestick Patterns & Trend Context
# =====================================================================

def detect_patterns(df):
    """Identify Hammer, Shooting Star, and Engulfing patterns."""
    if len(df) < 2:
        return {"hammer": False, "star": False, "bull_engulf": False, "bear_engulf": False}
    
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    body = abs(curr['Close'] - curr['Open'])
    total_range = curr['High'] - curr['Low']
    if total_range == 0: total_range = 0.001
    
    upper_wick = curr['High'] - max(curr['Close'], curr['Open'])
    lower_wick = min(curr['Close'], curr['Open']) - curr['Low']
    
    # 1. Hammer (Small body, long lower wick, tiny upper wick)
    is_hammer = (lower_wick > 2 * body) and (upper_wick < 0.1 * total_range) and (body > 0)
    
    # 2. Shooting Star (Small body, long upper wick, tiny lower wick)
    is_star = (upper_wick > 2 * body) and (lower_wick < 0.1 * total_range) and (body > 0)
    
    # 3. Bullish Engulfing (Green candle wraps previous Red candle)
    is_bull_engulf = (curr['Close'] > curr['Open']) and (prev['Close'] < prev['Open']) and \
                     (curr['Close'] >= prev['Open']) and (curr['Open'] <= prev['Close'])
    
    # 4. Bearish Engulfing (Red candle wraps previous Green candle)
    is_bear_engulf = (curr['Close'] < curr['Open']) and (prev['Close'] > prev['Open']) and \
                     (curr['Close'] <= prev['Open']) and (curr['Open'] >= prev['Close'])
    
    return {
        "hammer": is_hammer,
        "star": is_star,
        "bull_engulf": is_bull_engulf,
        "bear_engulf": is_bear_engulf
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
# Analyze a single stock DataFrame
# =====================================================================

def _analyze_stock(sym, df, rsi_bull_thresh=30, rsi_bear_thresh=70, swing_tolerance=0.03):
    """Run institutional-grade reversal analysis based on new criteria."""
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
        range_pos = (curr['Close'] - curr['Low']) / day_range # 0 to 1
        
        # 6. Candlestick Patterns & Trend
        patterns = detect_patterns(df)
        trend = get_trend_context(df, days=5)
        
        # 7. Support/Resistance Distance
        near_200sma = abs((last_price - sma200) / sma200) < 0.02 if sma200 else False
        near_52w_low = abs((last_price - fiftyTwoWeekLow) / fiftyTwoWeekLow) < 0.03 if fiftyTwoWeekLow else False
        near_52w_high = abs((last_price - fiftyTwoWeekHigh) / fiftyTwoWeekHigh) < 0.03 if fiftyTwoWeekHigh else False

        # --- BULLISH REVERSAL (BOUNCE) ---
        is_bullish = (
            (patterns['hammer'] or patterns['bull_engulf']) and
            (rsi_val < rsi_bull_thresh) and
            (rvol > 1.4) and
            (range_pos > 0.50) and
            (near_200sma or near_52w_low or trend == "downtrend")
        )

        # --- BEARISH REVERSAL (FADE) ---
        is_bearish = (
            (patterns['star'] or patterns['bear_engulf']) and
            (rsi_val > rsi_bear_thresh) and
            (rvol > 1.4) and
            (range_pos < 0.50) and
            (near_200sma or near_52w_high or trend == "uptrend")
        )

        if is_bullish or is_bearish:
            type_str = "Bullish Reversal" if is_bullish else "Bearish Reversal"
            signal_desc = []
            if patterns['hammer']: signal_desc.append("Hammer")
            if patterns['bull_engulf']: signal_desc.append("Bull Engulfing")
            if patterns['star']: signal_desc.append("Shooting Star")
            if patterns['bear_engulf']: signal_desc.append("Bear Engulfing")
            
            reasons = f"{', '.join(signal_desc)} | RSI: {rsi_val:.0f} | RVOL: {rvol:.1f}x | Range: {range_pos*100:.0f}%"
            if near_200sma: reasons += " | Near 200 SMA"
            if trend != "neutral": reasons += f" | Prior {trend}"

            return {
                "Ticker": sym,
                "Last Price": round(last_price, 2),
                "Volume": int(curr['Volume']),
                "RSI": round(rsi_val, 1),
                "Bullish Signals": reasons if is_bullish else "—",
                "Bearish Signals": reasons if is_bearish else "—",
            }
    except Exception as e:
        print(f"  Error analyzing {sym}: {e}")
    return None


# =====================================================================
# Watchlist scanner  (original, fast)
# =====================================================================

def reversal_scanner(tickers, min_volume=500_000, min_price=5.0,
                     rsi_bull_thresh=25, rsi_bear_thresh=75,
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

    interval = "5m"
    includePrePost = "true" if extended_hours else "false"
    # Need enough bars for 200 SMA on 5m chart (200 * 5m = ~17 hours, 10 days is plenty)
    fetch_days = 10 

    stock_data = fetch_batch(tickers, days=fetch_days, delay=0.05,
                             on_progress=_on_dl_progress, interval=interval, includePrePost=includePrePost)

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
                     rsi_bull_thresh=25, rsi_bear_thresh=75,
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

    interval = "1h" if extended_hours else "1d"
    includePrePost = "true" if extended_hours else "false"
    # 260+ days ensures we have a full year of data for the 200 SMA
    fetch_days = 60 if extended_hours else 280 

    stock_data = fetch_batch_concurrent(
        all_tickers, days=fetch_days, max_workers=8,
        on_progress=_on_dl_progress, delay=0.05, interval=interval, includePrePost=includePrePost
    )

    log.append(f"Downloaded: {len(stock_data)}/{total_tickers} tickers")
    print(f"\n  Downloaded: {len(stock_data)} tickers with data")

    # ── Phase 2b: Pre-filter by volume & price ──────────────
    candidates = []
    for sym, df in stock_data.items():
        try:
            today_date = df.index.date[-1]
            recent_vol = float(df[df.index.date == today_date]['Volume'].sum())

            price = float(df['Close'].iloc[-1])
            if recent_vol >= min_volume and price >= min_price:
                candidates.append((sym, df))
        except:
            continue

    log.append(f"Pre-filter: {len(candidates)} candidates pass vol/price filters")
    print(f"  Pre-filter: {len(candidates)} candidates from {len(stock_data)} tickers")

    # ── Phase 3: Analyze candidates ─────────────────────────
    print(f"\n[Phase 3] Analyzing {len(candidates)} candidates...")
    results = []
    total_candidates = len(candidates)
    phase3_start = time.time()

    for j, (sym, df) in enumerate(candidates):
        elapsed = time.time() - phase3_start
        if j > 0:
            rate = elapsed / j
            remaining = (total_candidates - j) * rate
        else:
            remaining = 0

        _update_progress("analyzing",
                         f"Analyzing {sym}...",
                         j, total_candidates,
                         ticker=sym, found=len(results))
        scan_progress["eta_seconds"] = int(remaining)

        result = _analyze_stock(sym, df, rsi_bull_thresh, rsi_bear_thresh, swing_tolerance)
        if result:
            results.append(result)

    # ── Done ────────────────────────────────────────────────
    total_time = time.time() - start_time
    summary = f"Done in {total_time:.0f}s: {len(results)} signals from {total_candidates} candidates"
    log.append(summary)
    print(f"\n[Done] {summary}")

    scan_progress.update({
        "status": "done", "phase": "complete",
        "phase_label": f"Done — {len(results)} signals found",
        "current": total_candidates, "total": total_candidates,
        "found": len(results), "pct": 100, "eta_seconds": 0,
    })

    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(by="Volume", ascending=False)


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
# =====================================================================

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
