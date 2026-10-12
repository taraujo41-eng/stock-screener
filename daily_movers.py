"""
Daily Movers Tracker for Watchlist Tickers.

Computes and archives the Top 10 Gainers and Top 10 Losers for all watchlist tickers
after market close for each trading day. Provides historical lookup across all trading days.
"""

import os
import sys
import json
import time
import threading
from datetime import datetime, timedelta
import pytz
import pandas as pd
import numpy as np

# Ensure access to sibling modules
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from data_fetcher import fetch_batch_concurrent

def get_ny_timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return pytz.timezone("America/New_York")

SCAN_DATA_DIR = os.environ.get("SCAN_DATA_DIR", _APP_DIR)
DAILY_MOVERS_FILE = os.path.join(SCAN_DATA_DIR, "daily_movers_history.json")
WATCHLIST_FILE = os.path.join(SCAN_DATA_DIR, "watchlist.json")

# Known company name mappings for rich UI presentation
COMPANY_NAMES = {
    "AAOI": "Applied Optoelectronics", "AAPL": "Apple Inc", "ABBV": "AbbVie Inc", "ABNB": "Airbnb Inc",
    "ADBE": "Adobe Inc", "ADI": "Analog Devices", "ADM": "Archer-Daniels-Midland", "ALAB": "Astera Labs",
    "AMAT": "Applied Materials", "AMD": "Advanced Micro Devices", "AMGN": "Amgen Inc", "AMZN": "Amazon.com Inc",
    "ANET": "Arista Networks", "APO": "Apollo Global Management", "APP": "AppLovin Corp", "ARM": "Arm Holdings",
    "AS": "Amer Sports", "ASTS": "AST SpaceMobile", "ASX": "ASE Technology", "AVAV": "AeroVironment",
    "AVGO": "Broadcom Inc", "AXP": "American Express", "BA": "Boeing Company", "BABA": "Alibaba Group",
    "BAC": "Bank of America", "BE": "Bloom Energy", "BSX": "Boston Scientific", "C": "Citigroup Inc",
    "CAG": "Conagra Brands", "CART": "Instacart (Maplebear)", "CAT": "Caterpillar Inc", "CBRS": "Cable One",
    "CCJ": "Cameco Corp", "CLX": "Clorox Company", "CMG": "Chipotle Mexican Grill", "COHR": "Coherent Corp",
    "COIN": "Coinbase Global", "COST": "Costco Wholesale", "CRCL": "Circle Internet Financial", "CRDO": "Credo Technology",
    "CRM": "Salesforce Inc", "CRWD": "CrowdStrike Holdings", "CRWV": "Corvus Pharmaceuticals", "CSCO": "Cisco Systems",
    "CVNA": "Carvana Co", "CVX": "Chevron Corp", "DAL": "Delta Air Lines", "DELL": "Dell Technologies",
    "DIA": "SPDR Dow Jones Industrial ETF", "DIS": "Walt Disney Company", "DJT": "Trump Media & Technology",
    "DKNG": "DraftKings Inc", "DOCU": "DocuSign Inc", "ECHO": "EchoStar Corp", "EL": "Estee Lauder",
    "ET": "Energy Transfer LP", "F": "Ford Motor Co", "FCX": "Freeport-McMoRan", "FIG": "Figma",
    "FIX": "Comfort Systems USA", "FMC": "FMC Corp", "FN": "Fabrinet", "FTAI": "FTAI Aviation",
    "GE": "GE Aerospace", "GH": "Guardant Health", "GLD": "SPDR Gold Shares", "GLW": "Corning Inc",
    "GOOG": "Alphabet Inc (Class C)", "GOOGL": "Alphabet Inc (Class A)", "GS": "Goldman Sachs Group",
    "HD": "Home Depot", "HL": "Hecla Mining", "HON": "Honeywell International", "HOOD": "Robinhood Markets",
    "HPE": "Hewlett Packard Enterprise", "IBIT": "iShares Bitcoin Trust", "IBM": "International Business Machines",
    "INTC": "Intel Corp", "INTU": "Intuit Inc", "IONQ": "IonQ Inc", "IREN": "Iris Energy",
    "IWM": "iShares Russell 2000 ETF", "JNJ": "Johnson & Johnson", "JPM": "JPMorgan Chase", "KHC": "Kraft Heinz",
    "KLAC": "KLA Corp", "KO": "Coca-Cola Co", "KTOS": "Kratos Defense", "LITE": "Lumentum Holdings",
    "LLY": "Eli Lilly & Co", "LOW": "Lowe's Companies", "LRCX": "Lam Research", "LULU": "Lululemon Athletica",
    "MA": "Mastercard Inc", "MCD": "McDonald's Corp", "META": "Meta Platforms", "MMM": "3M Company",
    "MP": "MP Materials", "MRK": "Merck & Co", "MRNA": "Moderna Inc", "MRVL": "Marvell Technology",
    "MSFT": "Microsoft Corp", "MSTR": "MicroStrategy Inc", "MTSI": "MACOM Technology", "MU": "Micron Technology",
    "NBIS": "Nebius Group", "NEE": "NextEra Energy", "NFLX": "Netflix Inc", "NKE": "Nike Inc",
    "NOK": "Nokia Corp", "NOW": "ServiceNow Inc", "NVDA": "NVIDIA Corp", "NVO": "Novo Nordisk",
    "NXT": "Nextracker Inc", "OKLO": "Oklo Inc", "ORCL": "Oracle Corp", "OXY": "Occidental Petroleum",
    "PANW": "Palo Alto Networks", "PFE": "Pfizer Inc", "PG": "Procter & Gamble", "PLTR": "Palantir Technologies",
    "PYPL": "PayPal Holdings", "QCOM": "Qualcomm Inc", "QQQ": "Invesco QQQ Trust", "RBLX": "Roblox Corp",
    "RDDT": "Reddit Inc", "RKLB": "Rocket Lab USA", "SBUX": "Starbucks Corp", "SCHW": "Charles Schwab",
    "SHOP": "Shopify Inc", "SHW": "Sherwin-Williams", "SKHY": "SK Hynix", "SLV": "iShares Silver Trust",
    "SMCI": "Super Micro Computer", "SMH": "VanEck Semiconductor ETF", "SMMT": "Summit Therapeutics",
    "SNDK": "SanDisk Corp", "SNOW": "Snowflake Inc", "SPCX": "CrossingBridge ETF", "SPY": "SPDR S&P 500 ETF",
    "STX": "Seagate Technology", "SU": "Suncor Energy", "T": "AT&T Inc", "TER": "Teradyne Inc",
    "TGT": "Target Corp", "TJX": "TJX Companies", "TRV": "Travelers Companies", "TSLA": "Tesla Inc",
    "TSM": "Taiwan Semiconductor Mfg", "TTD": "The Trade Desk", "TXN": "Texas Instruments", "U": "Unity Software",
    "UBER": "Uber Technologies", "UNH": "UnitedHealth Group", "USO": "United States Oil Fund", "V": "Visa Inc",
    "VRT": "Vertiv Holdings", "VST": "Vistra Corp", "VZ": "Verizon Communications", "WBD": "Warner Bros Discovery",
    "WDC": "Western Digital", "WFC": "Wells Fargo & Co", "WMT": "Walmart Inc", "XOM": "Exxon Mobil"
}

_movers_lock = threading.Lock()
_refresh_lock = threading.Lock()
_movers_progress = {
    "status": "idle",
    "pct": 0,
    "message": "",
    "timestamp": None
}

def load_watchlist():
    """Load current tickers from watchlist.json."""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    return [t.strip().upper() for t in data if isinstance(t, str) and t.strip()]
        except Exception:
            pass
    # Fallback to repo root watchlist.json
    base_file = os.path.join(_APP_DIR, "watchlist.json")
    if os.path.exists(base_file):
        try:
            with open(base_file, "r") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    return [t.strip().upper() for t in data if isinstance(t, str) and t.strip()]
        except Exception:
            pass
    return ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "SPY", "QQQ"]

def load_movers_cache():
    """Load history from daily_movers_history.json."""
    if os.path.exists(DAILY_MOVERS_FILE):
        try:
            with open(DAILY_MOVERS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict) and "history" in data:
                    return data
        except Exception as e:
            print(f"[DailyMovers] Error reading cache file: {e}")
    return {"updated_at": None, "dates": [], "history": {}}

def save_movers_cache(data):
    """Save history to daily_movers_history.json."""
    with _movers_lock:
        try:
            with open(DAILY_MOVERS_FILE, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            print(f"[DailyMovers] Error writing cache file: {e}")
            return False

def format_display_date(date_str):
    """Format YYYY-MM-DD to 'Friday, Oct 9, 2026'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%A, %b %-d, %Y")
    except Exception:
        return date_str

def calculate_movers_from_bars(data_dict, watchlist_tickers):
    """
    Given a dict of {ticker: DataFrame of daily bars},
    computes the gainers/losers for each available trading date.
    """
    date_records = {}  # {date_str: [list of ticker stats]}

    for ticker in watchlist_tickers:
        df = data_dict.get(ticker)
        if df is None or len(df) < 2:
            continue

        close_col = "Close"
        high_col = "High"
        low_col = "Low"
        open_col = "Open"
        vol_col = "Volume"

        # Calculate 20-day rolling average volume for RVOL
        rolling_vol = df[vol_col].rolling(window=20, min_periods=1).mean()

        for i in range(1, len(df)):
            try:
                date_val = df.index[i]
                date_str = str(date_val.date()) if hasattr(date_val, "date") else str(date_val)[:10]

                if date_str not in date_records:
                    date_records[date_str] = []

                c = float(df[close_col].iloc[i])
                pc = float(df[close_col].iloc[i - 1])
                chg = c - pc
                chg_pct = (chg / pc * 100.0) if pc > 0 else 0.0

                hi = float(df[high_col].iloc[i])
                lo = float(df[low_col].iloc[i])
                op = float(df[open_col].iloc[i])
                vol = int(df[vol_col].iloc[i])
                avg_vol = int(rolling_vol.iloc[i]) if not np.isnan(rolling_vol.iloc[i]) else vol
                rvol = round(vol / avg_vol, 2) if avg_vol > 0 else 1.0

                # Where in day's range did the stock close (0% = low, 100% = high)
                rng = hi - lo
                range_pos = round(((c - lo) / rng * 100.0), 1) if rng > 0 else 50.0

                company_name = COMPANY_NAMES.get(ticker, ticker)

                date_records[date_str].append({
                    "ticker": ticker,
                    "name": company_name,
                    "close": round(c, 2),
                    "prev_close": round(pc, 2),
                    "change": round(chg, 2),
                    "change_pct": round(chg_pct, 2),
                    "open": round(op, 2),
                    "high": round(hi, 2),
                    "low": round(lo, 2),
                    "volume": vol,
                    "avg_volume": avg_vol,
                    "rvol": rvol,
                    "range_pos": range_pos
                })
            except Exception:
                continue

    # Process each trading date into structured summary
    history = {}
    sorted_dates = sorted(date_records.keys())

    for d_str in sorted_dates:
        records = date_records[d_str]
        if not records:
            continue

        # Sort all tickers by change_pct descending
        all_sorted = sorted(records, key=lambda x: x["change_pct"], reverse=True)

        gainers_pool = [r for r in all_sorted if r["change_pct"] > 0]
        losers_pool = [r for r in all_sorted if r["change_pct"] < 0]
        # In case all are positive or negative, fallback to slice
        top_gainers_raw = gainers_pool[:10] if len(gainers_pool) >= 10 else all_sorted[:10]
        # For losers, sort ascending (biggest drop first)
        losers_sorted_asc = sorted(losers_pool, key=lambda x: x["change_pct"])
        top_losers_raw = losers_sorted_asc[:10] if len(losers_sorted_asc) >= 10 else sorted(all_sorted, key=lambda x: x["change_pct"])[:10]

        # Add ranks
        top_gainers = []
        for rank, item in enumerate(top_gainers_raw, 1):
            top_gainers.append(dict(item, rank=rank))

        top_losers = []
        for rank, item in enumerate(top_losers_raw, 1):
            top_losers.append(dict(item, rank=rank))

        # Stats
        total_scanned = len(all_sorted)
        gainers_count = sum(1 for r in all_sorted if r["change_pct"] > 0)
        losers_count = sum(1 for r in all_sorted if r["change_pct"] < 0)
        unchanged_count = total_scanned - gainers_count - losers_count
        avg_change = round(sum(r["change_pct"] for r in all_sorted) / total_scanned, 2) if total_scanned else 0.0

        top_gainer = all_sorted[0] if all_sorted else None
        top_loser = sorted(all_sorted, key=lambda x: x["change_pct"])[0] if all_sorted else None

        history[d_str] = {
            "date": d_str,
            "display_date": format_display_date(d_str),
            "market_closed": True,
            "stats": {
                "total_scanned": total_scanned,
                "gainers_count": gainers_count,
                "losers_count": losers_count,
                "unchanged_count": unchanged_count,
                "avg_change_pct": avg_change,
                "top_gainer": {"ticker": top_gainer["ticker"], "change_pct": top_gainer["change_pct"], "close": top_gainer["close"]} if top_gainer else None,
                "top_loser": {"ticker": top_loser["ticker"], "change_pct": top_loser["change_pct"], "close": top_loser["close"]} if top_loser else None
            },
            "top_gainers": top_gainers,
            "top_losers": top_losers,
            "all_movers": all_sorted
        }

    return sorted_dates, history

def build_full_history(lookback_days=45, on_progress=None):
    """
    Fetch daily candles for all watchlist tickers and generate historical
    top 10 gainers & losers for each trading day.
    """
    tickers = load_watchlist()
    total = len(tickers)
    print(f"[DailyMovers] Building full history for {total} watchlist tickers (lookback {lookback_days}d)...")

    def _prog(i, tot, sym):
        if on_progress:
            pct = int((i / tot) * 90) if tot else 0
            on_progress(pct, f"Fetching daily close data: {sym} ({i}/{tot})...")

    raw_data = fetch_batch_concurrent(
        tickers=tickers,
        days=lookback_days,
        max_workers=20,
        on_progress=_prog,
        interval="1d",
        includePrePost="false"
    )

    if on_progress:
        on_progress(95, "Analyzing daily close performance for all trading days...")

    sorted_dates, history = calculate_movers_from_bars(raw_data, tickers)

    ny_tz = get_ny_timezone()
    updated_at = datetime.now(ny_tz).isoformat()

    cache_data = {
        "updated_at": updated_at,
        "dates": sorted_dates,
        "history": history
    }

    save_movers_cache(cache_data)
    print(f"[DailyMovers] History built successfully. Saved {len(sorted_dates)} trading days.")
    if on_progress:
        on_progress(100, f"Complete! {len(sorted_dates)} trading days ready.")

    return cache_data

def get_daily_movers_data(target_date=None, force_refresh=False):
    """
    Retrieve top 10 gainers and losers for target_date.
    If target_date is None or 'latest', returns the most recent trading day.
    """
    cache = load_movers_cache()

    # If cache is empty or has no dates, build it
    if not cache.get("dates") or force_refresh:
        cache = build_full_history(lookback_days=45)

    dates = cache.get("dates", [])
    history = cache.get("history", {})

    if not dates:
        return {
            "ok": False,
            "error": "No trading data available for watchlist tickers."
        }

    # Select target date
    selected_date = None
    if target_date and target_date in history:
        selected_date = target_date
    elif target_date and target_date not in history:
        # If requested date is not in history, find closest or fallback to latest
        selected_date = dates[-1]
    else:
        selected_date = dates[-1]

    day_data = history.get(selected_date, {})

    # Generate available dates with formatted labels for the UI dropdown
    available_dates = []
    latest_date = dates[-1]
    for d in reversed(dates):
        label = format_display_date(d)
        if d == latest_date:
            label += " (Latest Market Close)"
        available_dates.append({
            "date": d,
            "label": label,
            "is_latest": (d == latest_date)
        })

    return {
        "ok": True,
        "date": selected_date,
        "display_date": day_data.get("display_date", format_display_date(selected_date)),
        "is_market_closed": day_data.get("market_closed", True),
        "available_dates": available_dates,
        "stats": day_data.get("stats", {}),
        "top_gainers": day_data.get("top_gainers", []),
        "top_losers": day_data.get("top_losers", []),
        "all_movers": day_data.get("all_movers", []),
        "updated_at": cache.get("updated_at")
    }

def refresh_movers_async():
    """Trigger background recalculation of daily movers."""
    global _movers_progress
    if not _refresh_lock.acquire(blocking=False):
        return {"ok": False, "message": "Scan already in progress."}

    def _worker():
        global _movers_progress
        try:
            _movers_progress["status"] = "running"
            _movers_progress["pct"] = 0
            _movers_progress["message"] = "Starting market close scan..."
            _movers_progress["timestamp"] = time.time()

            def _on_p(pct, msg):
                _movers_progress["pct"] = pct
                _movers_progress["message"] = msg

            build_full_history(lookback_days=45, on_progress=_on_p)
            _movers_progress["status"] = "complete"
            _movers_progress["pct"] = 100
            _movers_progress["message"] = "Scan completed successfully."
        except Exception as e:
            _movers_progress["status"] = "error"
            _movers_progress["message"] = f"Error: {e}"
        finally:
            _refresh_lock.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"ok": True, "message": "Scan initiated."}

def get_refresh_progress():
    """Return status of active refresh."""
    return dict(_movers_progress)

# ── Automated Post-Market-Close Background Monitor ──────────────────────
_auto_snapshot_last_date = None

def check_and_run_market_close_snapshot():
    """
    Checks if today is a weekday and regular market hours have closed (after 4:02 PM ET).
    If today's market close has not yet been snapshotted into history, runs it.
    """
    global _auto_snapshot_last_date
    ny_tz = get_ny_timezone()
    now = datetime.now(ny_tz)

    # Only run on trading days (Mon-Fri)
    if now.weekday() >= 5:
        return

    # Check time: between 4:02 PM and 11:59 PM ET
    minutes_now = now.hour * 60 + now.minute
    close_minutes = 16 * 60 + 2  # 4:02 PM ET

    if minutes_now >= close_minutes:
        today_str = now.strftime("%Y-%m-%d")
        if _auto_snapshot_last_date == today_str:
            return

        cache = load_movers_cache()
        dates = cache.get("dates", [])
        if today_str not in dates:
            print(f"[DailyMovers] Market has closed for trading day {today_str}. Taking official close snapshot...")
            try:
                build_full_history(lookback_days=45)
                _auto_snapshot_last_date = today_str
                print(f"[DailyMovers] Successfully saved official close snapshot for {today_str}.")
            except Exception as e:
                print(f"[DailyMovers] Failed to snapshot market close for {today_str}: {e}")
        else:
            _auto_snapshot_last_date = today_str

def start_movers_background_watcher():
    """Starts the background thread that automatically captures market close snapshots."""
    def _loop():
        print("[DailyMovers] Background market close monitor initialized.")
        while True:
            try:
                check_and_run_market_close_snapshot()
            except Exception as e:
                print(f"[DailyMovers] Background watcher exception: {e}")
            time.sleep(120)  # Check every 2 minutes

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
