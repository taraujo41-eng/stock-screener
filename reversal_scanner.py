"""
Stock Reversal Scanner — Full Market Edition
Scans the entire US stock market for bullish/bearish reversal setups.

Modes:
  • Watchlist scan  — fast, scans a custom list (~30s)
  • Full market scan — fetches all US tickers, pre-filters, then analyzes (~3-8 min)
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from io import StringIO
import time
import warnings

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
}

def _reset_progress():
    scan_progress.update({
        "status": "idle", "phase": "", "phase_label": "",
        "current": 0, "total": 0, "found": 0,
        "ticker": "", "pct": 0, "eta_seconds": 0,
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
    """Fetch all actively traded US stock tickers from multiple sources."""
    tickers = set()

    # ── Source 1: NASDAQ Traded List (official, ~5000+ tickers) ──
    try:
        url = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqtraded.txt"
        resp = requests.get(url, timeout=15)
        lines = resp.text.strip().split("\n")
        # Header: Nasdaq Traded|Symbol|Security Name|...
        for line in lines[1:]:  # skip header
            parts = line.split("|")
            if len(parts) >= 2:
                sym = parts[1].strip()
                traded = parts[0].strip()
                # Filter: only actively traded, normal symbols (no test/special)
                if (traded == "Y" and sym and 1 <= len(sym) <= 5
                        and sym.isalpha() and sym.isupper()):
                    tickers.add(sym)
        print(f"  Source 1 (NASDAQ Traded): {len(tickers)} tickers")
    except Exception as e:
        print(f"  Source 1 (NASDAQ Traded): failed ({e})")

    # ── Source 2: S&P 500 from Wikipedia (backup / supplement) ──
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"}
        )
        sp = tables[0]
        added = 0
        for sym in sp["Symbol"]:
            clean = sym.strip().replace(".", "-")
            if clean and clean not in tickers:
                tickers.add(clean)
                added += 1
        print(f"  Source 2 (S&P 500): +{added} tickers")
    except Exception as e:
        print(f"  Source 2 (S&P 500): failed ({e})")

    # Remove known non-equity / test symbols
    exclude = {"TRUE", "NONE", "NULL", "CTEST", "NTEST", "ZTEST", "ZVZZT", "ZJZZT"}
    tickers -= exclude

    print(f"  Total unique tickers: {len(tickers)}")
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
# Candlestick pattern helpers
# =====================================================================

def _body(o, c):
    return abs(c - o)

def _upper_shadow(h, o, c):
    return h - max(o, c)

def _lower_shadow(l, o, c):
    return min(o, c) - l

def detect_patterns(df):
    """Simple candlestick pattern detection on the last 3 bars."""
    bullish, bearish = [], []
    if len(df) < 3:
        return bullish, bearish

    o1, h1, l1, c1 = (float(df['Open'].iloc[-3]), float(df['High'].iloc[-3]),
                       float(df['Low'].iloc[-3]),  float(df['Close'].iloc[-3]))
    o2, h2, l2, c2 = (float(df['Open'].iloc[-2]), float(df['High'].iloc[-2]),
                       float(df['Low'].iloc[-2]),  float(df['Close'].iloc[-2]))
    o3, h3, l3, c3 = (float(df['Open'].iloc[-1]), float(df['High'].iloc[-1]),
                       float(df['Low'].iloc[-1]),  float(df['Close'].iloc[-1]))

    body3 = _body(o3, c3)
    avg_body = (_body(o1, c1) + _body(o2, c2) + body3) / 3 or 0.01

    # Bullish Engulfing
    if c2 < o2 and c3 > o3 and o3 <= c2 and c3 >= o2:
        bullish.append("Bullish Engulfing")

    # Bearish Engulfing
    if c2 > o2 and c3 < o3 and o3 >= c2 and c3 <= o2:
        bearish.append("Bearish Engulfing")

    # Hammer (bullish)
    lower = _lower_shadow(l3, o3, c3)
    upper = _upper_shadow(h3, o3, c3)
    if body3 > 0 and lower >= 2 * body3 and upper <= body3 * 0.3:
        bullish.append("Hammer")

    # Shooting Star (bearish)
    if body3 > 0 and upper >= 2 * body3 and lower <= body3 * 0.3:
        bearish.append("Shooting Star")

    # Morning Star (bullish, 3-bar)
    body1 = _body(o1, c1)
    body2 = _body(o2, c2)
    if (c1 < o1 and body1 > avg_body and
        body2 < body1 * 0.3 and
        c3 > o3 and c3 > (o1 + c1) / 2):
        bullish.append("Morning Star")

    # Evening Star (bearish, 3-bar)
    if (c1 > o1 and body1 > avg_body and
        body2 < body1 * 0.3 and
        c3 < o3 and c3 < (o1 + c1) / 2):
        bearish.append("Evening Star")

    # Piercing Line (bullish, 2-bar)
    if (c2 < o2 and c3 > o3 and
        o3 < l2 and c3 > (o2 + c2) / 2 and c3 < o2):
        bullish.append("Piercing Line")

    # Dark Cloud Cover (bearish, 2-bar)
    if (c2 > o2 and c3 < o3 and
        o3 > h2 and c3 < (o2 + c2) / 2 and c3 > o2):
        bearish.append("Dark Cloud Cover")

    # Three White Soldiers (bullish)
    if (c1 > o1 and c2 > o2 and c3 > o3 and
        c2 > c1 and c3 > c2 and
        o2 > o1 and o3 > o2):
        bullish.append("Three White Soldiers")

    # Three Black Crows (bearish)
    if (c1 < o1 and c2 < o2 and c3 < o3 and
        c2 < c1 and c3 < c2 and
        o2 < o1 and o3 < o2):
        bearish.append("Three Black Crows")

    return bullish, bearish


# =====================================================================
# Swing high / low helpers
# =====================================================================

def is_near_swing_low(df, lookback=20, tolerance=0.03):
    if len(df) < lookback:
        return False
    recent_low = float(df['Low'].iloc[-lookback:].min())
    last_close = float(df['Close'].iloc[-1])
    if recent_low == 0:
        return False
    return (last_close - recent_low) / recent_low <= tolerance

def is_near_swing_high(df, lookback=20, tolerance=0.03):
    if len(df) < lookback:
        return False
    recent_high = float(df['High'].iloc[-lookback:].max())
    last_close = float(df['Close'].iloc[-1])
    if recent_high == 0:
        return False
    return (recent_high - last_close) / recent_high <= tolerance


# =====================================================================
# Analyze a single stock DataFrame
# =====================================================================

def _analyze_stock(sym, df, rsi_bull_thresh, rsi_bear_thresh,
                   swing_lookback, swing_tolerance):
    """Run full reversal analysis on one stock. Returns dict or None."""
    try:
        last_vol = float(df['Volume'].iloc[-1])
        last_price = float(df['Close'].iloc[-1])

        rsi_series = compute_rsi(df['Close'], 14)
        rsi_val = float(rsi_series.iloc[-1])
        bull_pats, bear_pats = detect_patterns(df)
        near_support = is_near_swing_low(df, swing_lookback, swing_tolerance)
        near_resist  = is_near_swing_high(df, swing_lookback, swing_tolerance)

        bullish_signals = []
        bearish_signals = []

        if not np.isnan(rsi_val):
            if rsi_val < rsi_bull_thresh:
                bullish_signals.append(f"RSI={rsi_val:.1f}")
            if rsi_val > rsi_bear_thresh:
                bearish_signals.append(f"RSI={rsi_val:.1f}")

        bullish_signals.extend(bull_pats)
        bearish_signals.extend(bear_pats)

        if near_support:
            bullish_signals.append("Near Support")
        if near_resist:
            bearish_signals.append("Near Resistance")

        if bullish_signals or bearish_signals:
            return {
                "Ticker": sym,
                "Last Price": round(last_price, 2),
                "Volume": int(last_vol),
                "RSI": round(rsi_val, 1) if not np.isnan(rsi_val) else None,
                "Bullish Signals": ", ".join(bullish_signals) if bullish_signals else "—",
                "Bearish Signals": ", ".join(bearish_signals) if bearish_signals else "—",
            }
    except:
        pass
    return None


# =====================================================================
# Watchlist scanner  (original, fast)
# =====================================================================

def reversal_scanner(tickers, min_volume=500_000, min_price=5.0,
                     rsi_bull_thresh=30, rsi_bear_thresh=70,
                     swing_lookback=20, swing_tolerance=0.03):
    """Scan a watchlist using batch download for speed + progress tracking."""
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()

    results = []
    end = datetime.today()
    start = end - timedelta(days=180)
    total = len(tickers)

    # ── Phase 1: Batch download all tickers at once (~2-3s) ──
    _update_progress("downloading", f"Downloading {total} tickers...", 0, total)
    print(f"\n[Phase 1] Batch downloading {total} tickers...")

    try:
        batch_df = yf.download(
            tickers, start=start, end=end,
            progress=False, auto_adjust=True,
            group_by="ticker", threads=True
        )
    except Exception as e:
        print(f"  Batch download error: {e}")
        scan_progress["status"] = "error"
        scan_progress["phase_label"] = str(e)
        return pd.DataFrame()

    if batch_df.empty:
        print("  No data returned")
        scan_progress.update({
            "status": "done", "phase": "complete",
            "phase_label": "Done — 0 signals found",
            "current": total, "total": total,
            "found": 0, "pct": 100, "eta_seconds": 0,
        })
        return pd.DataFrame()

    # ── Phase 2: Analyze each ticker from the batch ──────────
    print(f"\n[Phase 2] Analyzing {total} tickers...")

    for i, sym in enumerate(tickers):
        _update_progress("analyzing", f"Analyzing {sym}...", i, total,
                         ticker=sym, found=len(results))

        try:
            # Extract single ticker from batch result
            if len(tickers) == 1:
                stock_df = batch_df.copy()
                if isinstance(stock_df.columns, pd.MultiIndex):
                    stock_df.columns = stock_df.columns.get_level_values(0)
            else:
                if sym not in batch_df.columns.get_level_values(0):
                    print(f"  [{i+1}/{total}] {sym}... not in batch")
                    continue
                stock_df = batch_df[sym].copy()

            stock_df = stock_df.dropna(subset=["Close"])
            if len(stock_df) < 50:
                print(f"  [{i+1}/{total}] {sym}... skip (insufficient data)")
                continue

            last_vol = float(stock_df['Volume'].iloc[-1])
            last_price = float(stock_df['Close'].iloc[-1])

            if last_vol < min_volume or last_price < min_price:
                print(f"  [{i+1}/{total}] {sym}... skip (filter)")
                continue

            result = _analyze_stock(sym, stock_df, rsi_bull_thresh, rsi_bear_thresh,
                                    swing_lookback, swing_tolerance)
            if result:
                results.append(result)
                print(f"  [{i+1}/{total}] {sym}... ✓ signal")
            else:
                print(f"  [{i+1}/{total}] {sym}... no signal")
        except Exception as e:
            print(f"  [{i+1}/{total}] {sym}... error ({e})")
            continue

    # ── Done ─────────────────────────────────────────────────
    total_time = time.time() - start_time
    print(f"\n[Done] Found {len(results)} signals in {total_time:.0f}s")

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
                     rsi_bull_thresh=30, rsi_bear_thresh=70,
                     swing_lookback=20, swing_tolerance=0.03,
                     chunk_size=80):
    """
    Scan the entire US stock market:
      1. Fetch all US ticker symbols
      2. Batch download price data in chunks
      3. Pre-filter by volume & price
      4. Run full reversal analysis on candidates
    """
    _reset_progress()
    scan_progress["status"] = "running"
    start_time = time.time()

    # ── Phase 1: Get ticker list ────────────────────────────
    _update_progress("fetching_tickers", "Fetching ticker list...", 0, 1)
    print("\n[Phase 1] Fetching US ticker list...")
    all_tickers = get_us_tickers()

    if not all_tickers:
        scan_progress["status"] = "error"
        return pd.DataFrame()

    total_tickers = len(all_tickers)
    print(f"\n[Phase 2] Batch downloading {total_tickers} tickers...")

    # ── Phase 2: Batch download & pre-filter ────────────────
    candidates = []   # list of (sym, DataFrame) tuples
    end = datetime.today()
    start = end - timedelta(days=180)
    downloaded = 0
    phase2_start = time.time()

    for i in range(0, total_tickers, chunk_size):
        chunk = all_tickers[i : i + chunk_size]
        chunk_label = f"{chunk[0]}–{chunk[-1]}"

        elapsed = time.time() - phase2_start
        if downloaded > 0:
            rate = elapsed / downloaded
            remaining = (total_tickers - downloaded) * rate
        else:
            remaining = 0

        _update_progress("downloading",
                         f"Downloading {chunk_label}...",
                         downloaded, total_tickers,
                         ticker=chunk_label, found=len(candidates))
        scan_progress["eta_seconds"] = int(remaining)

        try:
            batch_df = yf.download(
                chunk, start=start, end=end,
                progress=False, auto_adjust=True,
                group_by="ticker", threads=True
            )

            if batch_df.empty:
                downloaded += len(chunk)
                continue

            for sym in chunk:
                try:
                    # Extract single ticker from batch
                    if len(chunk) == 1:
                        stock_df = batch_df.copy()
                        if isinstance(stock_df.columns, pd.MultiIndex):
                            stock_df.columns = stock_df.columns.get_level_values(0)
                    else:
                        if sym not in batch_df.columns.get_level_values(0):
                            continue
                        stock_df = batch_df[sym].copy()

                    stock_df = stock_df.dropna(subset=["Close"])
                    if len(stock_df) < 50:
                        continue

                    vol = float(stock_df['Volume'].iloc[-1])
                    price = float(stock_df['Close'].iloc[-1])

                    if vol >= min_volume and price >= min_price:
                        candidates.append((sym, stock_df))
                except:
                    continue

        except Exception as e:
            print(f"  Chunk error: {e}")

        downloaded += len(chunk)

    print(f"\n  Pre-filter: {len(candidates)} candidates from {total_tickers} tickers")

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

        result = _analyze_stock(sym, df, rsi_bull_thresh, rsi_bear_thresh,
                                swing_lookback, swing_tolerance)
        if result:
            results.append(result)

    # ── Done ────────────────────────────────────────────────
    total_time = time.time() - start_time
    print(f"\n[Done] Found {len(results)} signals in {total_time:.0f}s")

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
