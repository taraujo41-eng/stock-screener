"""
Premarket Movers Scanner for Watchlist + Top 50 Universe.

Scans the combined universe of Watchlist tickers and Top 50 stocks for premarket price action:
- During active Pre-Market (04:00 - 09:30 AM ET): Scans real-time live premarket quotes and 5m candles.
- During Regular / After-Hours / Weekends: Provides the official premarket session recap of the latest trading session.
- Computes Top Gainers, Top Losers, Most Active by Volume, and full universe performance.
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

from data_fetcher import fetch_batch_concurrent, get_unofficial_client

def get_ny_timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return pytz.timezone("America/New_York")

SCAN_DATA_DIR = os.environ.get("SCAN_DATA_DIR", _APP_DIR)
PREMARKET_CACHE_FILE = os.path.join(SCAN_DATA_DIR, "last_premarket_scan.json")
WATCHLIST_FILE = os.path.join(SCAN_DATA_DIR, "watchlist.json")
TOP_50_STOCKS_FILE = os.path.join(os.path.dirname(__file__), "top50_stocks.json")

# Known company name mappings
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

_premarket_lock = threading.Lock()
_premarket_refresh_lock = threading.Lock()
_premarket_progress = {
    "status": "idle",
    "pct": 0,
    "message": "",
    "timestamp": None
}

def load_watchlist_tickers():
    """Load watchlist tickers from primary or fallback watchlist.json."""
    wl = []
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    wl = [t.strip().upper() for t in data if isinstance(t, str) and t.strip()]
        except Exception:
            pass
    if not wl:
        base_file = os.path.join(_APP_DIR, "watchlist.json")
        if os.path.exists(base_file):
            try:
                with open(base_file, "r") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        wl = [t.strip().upper() for t in data if isinstance(t, str) and t.strip()]
            except Exception:
                pass
    return wl or ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "SPY", "QQQ"]

def load_top50_stocks():
    """Load top 50 stocks with ranks and metadata."""
    if os.path.exists(TOP_50_STOCKS_FILE):
        try:
            with open(TOP_50_STOCKS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def get_combined_universe():
    """
    Combines Watchlist and Top 50 stocks into a single deduplicated universe.
    Returns:
      tickers: list of uppercase ticker strings
      meta_map: dict of {ticker: {"name": ..., "in_watchlist": bool, "in_top50": bool, "top50_rank": int|None, "tags": [...]}}
    """
    wl_tickers = set(load_watchlist_tickers())
    top50_list = load_top50_stocks()
    top50_map = {s["ticker"].strip().upper(): s for s in top50_list if isinstance(s, dict) and "ticker" in s}

    all_symbols = sorted(list(wl_tickers.union(set(top50_map.keys()))))
    meta_map = {}

    for sym in all_symbols:
        in_wl = sym in wl_tickers
        in_t50 = sym in top50_map
        t50_data = top50_map.get(sym, {})
        rank = t50_data.get("rank")

        # Resolve name
        name = COMPANY_NAMES.get(sym) or t50_data.get("name") or sym

        tags = []
        if in_wl:
            tags.append("Watchlist")
        if in_t50:
            tags.append(f"Top 50 #{rank}" if rank else "Top 50")

        meta_map[sym] = {
            "name": name,
            "in_watchlist": in_wl,
            "in_top50": in_t50,
            "top50_rank": rank,
            "tags": tags
        }

    return all_symbols, meta_map

def format_display_date(date_str):
    """Format YYYY-MM-DD to 'Tuesday, Oct 27, 2026'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%A, %b %-d, %Y")
    except Exception:
        return date_str

def get_market_session_info():
    """
    Determine the current market session in America/New_York.
    Returns:
      session_type: 'LIVE_PREMARKET' | 'REGULAR' | 'AFTER_HOURS' | 'CLOSED'
      status_text: user-facing string description
      is_live_premarket: bool
      current_time_str: formatted time
    """
    ny_tz = get_ny_timezone()
    now = datetime.now(ny_tz)
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    time_minutes = now.hour * 60 + now.minute

    time_str = now.strftime("%I:%M %p ET")

    # Pre-market is 4:00 AM (240 min) to 9:30 AM (570 min)
    # Regular is 9:30 AM (570 min) to 4:00 PM (960 min)
    # After-hours is 4:00 PM (960 min) to 8:00 PM (1200 min)
    if weekday < 5:  # Monday to Friday
        if 240 <= time_minutes < 570:
            return "LIVE_PREMARKET", "🟢 Live Pre-Market Trading (04:00 - 09:30 AM ET)", True, time_str
        elif 570 <= time_minutes < 960:
            return "REGULAR", "🕒 Regular Market Hours Active (Pre-Market Session Closed)", False, time_str
        elif 960 <= time_minutes < 1200:
            return "AFTER_HOURS", "🌙 After-Hours Session Active (Showing Latest Pre-Market Session)", False, time_str
        else:
            return "CLOSED", "⚪ Market Closed (Showing Latest Pre-Market Session)", False, time_str
    else:
        return "CLOSED", "⚪ Weekend (Showing Friday Pre-Market Session)", False, time_str

def load_premarket_cache():
    """Load cached premarket scan results from file."""
    if os.path.exists(PREMARKET_CACHE_FILE):
        try:
            with open(PREMARKET_CACHE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict) and "all_movers" in data and len(data["all_movers"]) > 0:
                    return data
        except Exception as e:
            print(f"[PremarketMovers] Error reading cache: {e}")
    return None

def save_premarket_cache(data):
    """Save premarket scan results to file."""
    with _premarket_lock:
        try:
            with open(PREMARKET_CACHE_FILE, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            print(f"[PremarketMovers] Error saving cache: {e}")
            return False

def calculate_premarket_from_bars(data_dict, tickers, meta_map):
    """
    Analyzes 5m candlestick data (with extended hours) to extract exact premarket
    statistics for the latest trading day across all tickers.
    """
    records = []
    detected_dates = set()

    for sym in tickers:
        df = data_dict.get(sym)
        meta = meta_map.get(sym, {})
        name = meta.get("name", sym)

        if df is None or len(df) < 5:
            continue

        try:
            # Dates present in the DataFrame
            dates = sorted(list(set(df.index.strftime("%Y-%m-%d"))))
            if not dates:
                continue

            latest_date = dates[-1]
            detected_dates.add(latest_date)

            # Extract 04:00 to 09:29:59 premarket candles of the latest date
            pm_bars = df[(df.index.strftime("%Y-%m-%d") == latest_date) & 
                         (df.index.time >= datetime.strptime("04:00", "%H:%M").time()) & 
                         (df.index.time < datetime.strptime("09:30", "%H:%M").time())]

            # Determine previous regular session close (prior trading day <= 16:00 ET)
            prior_bars = df[(df.index.strftime("%Y-%m-%d") < latest_date) & 
                            (df.index.time <= datetime.strptime("16:00", "%H:%M").time())]
            
            if len(prior_bars) > 0:
                prev_close = float(prior_bars["Close"].iloc[-1])
            else:
                prev_close = float(df["Close"].iloc[0])

            if len(pm_bars) > 0:
                pm_open = float(pm_bars["Open"].iloc[0])
                pm_high = float(pm_bars["High"].max())
                pm_low = float(pm_bars["Low"].min())
                pm_last = float(pm_bars["Close"].iloc[-1])
                pm_vol = int(pm_bars["Volume"].sum())
            else:
                # No separate premarket candles traded; stock opened at prev_close or gap
                pm_open = prev_close
                pm_high = prev_close
                pm_low = prev_close
                pm_last = prev_close
                pm_vol = 0

            # Calculate change and percent
            chg = pm_last - prev_close
            chg_pct = (chg / prev_close * 100.0) if prev_close > 0 else 0.0

            # Premarket range calculation
            rng = pm_high - pm_low
            range_pos = round(((pm_last - pm_low) / rng * 100.0), 1) if rng > 0 else 50.0

            # Average volume for RVOL calculation if available
            day_vol = int(df["Volume"].sum())
            rvol = round((pm_vol / 100000.0), 2) if pm_vol > 0 else 0.0

            gap_type = "GAP UP" if chg_pct >= 0.5 else ("GAP DOWN" if chg_pct <= -0.5 else "FLAT")

            records.append({
                "ticker": sym,
                "name": name,
                "in_watchlist": meta.get("in_watchlist", True),
                "in_top50": meta.get("in_top50", False),
                "top50_rank": meta.get("top50_rank"),
                "tags": meta.get("tags", ["Watchlist"]),
                "price": round(pm_last, 2),
                "prev_close": round(prev_close, 2),
                "change": round(chg, 2),
                "change_pct": round(chg_pct, 2),
                "open": round(pm_open, 2),
                "high": round(pm_high, 2),
                "low": round(pm_low, 2),
                "volume": pm_vol,
                "range_pos": range_pos,
                "gap_type": gap_type,
                "rvol": rvol
            })
        except Exception as e:
            continue

    latest_date_str = sorted(list(detected_dates))[-1] if detected_dates else datetime.now(get_ny_timezone()).strftime("%Y-%m-%d")
    return records, latest_date_str

def scan_premarket_universe(on_progress=None):
    """
    Executes a complete scan of the combined Watchlist + Top 50 universe.
    Fetches 5m candles with premarket enabled and analyzes the results.
    """
    tickers, meta_map = get_combined_universe()
    total = len(tickers)

    print(f"[PremarketMovers] Initiating premarket scan for {total} combined universe stocks...")
    if on_progress:
        on_progress(5, f"Starting premarket scan for {total} stocks (Watchlist + Top 50)...")

    def _prog(i, tot, sym):
        if on_progress:
            pct = 5 + int((i / tot) * 85) if tot else 5
            on_progress(pct, f"Scanning premarket: {sym} ({i}/{tot})...")

    # Fetch 5m bars with premarket enabled (days=3 covers weekend and holidays)
    raw_data = fetch_batch_concurrent(
        tickers=tickers,
        days=3,
        max_workers=25,
        on_progress=_prog,
        interval="5m",
        includePrePost="true"
    )

    if on_progress:
        on_progress(92, "Ranking biggest premarket gainers & losers...")

    records, session_date_str = calculate_premarket_from_bars(raw_data, tickers, meta_map)

    if not records:
        print("[PremarketMovers] Warning: No premarket records calculated.")
        return None

    # Sort all records by percentage change descending
    all_sorted = sorted(records, key=lambda x: x["change_pct"], reverse=True)

    # Gainers & Losers Pools
    gainers_pool = [r for r in all_sorted if r["change_pct"] > 0]
    losers_pool = [r for r in all_sorted if r["change_pct"] < 0]

    # Top Gainers (ordered biggest gain first)
    top_gainers_raw = gainers_pool[:15] if len(gainers_pool) >= 15 else all_sorted[:15]
    top_gainers = [dict(item, rank=i + 1) for i, item in enumerate(top_gainers_raw)]

    # Top Losers (ordered biggest drop first)
    losers_sorted_asc = sorted(losers_pool, key=lambda x: x["change_pct"])
    top_losers_raw = losers_sorted_asc[:15] if len(losers_sorted_asc) >= 15 else sorted(all_sorted, key=lambda x: x["change_pct"])[:15]
    top_losers = [dict(item, rank=i + 1) for i, item in enumerate(top_losers_raw)]

    # Most Active by Premarket Volume
    active_sorted = sorted(all_sorted, key=lambda x: x["volume"], reverse=True)
    most_active = [dict(item, rank=i + 1) for i, item in enumerate(active_sorted[:15])]

    # Summary Statistics
    total_scanned = len(all_sorted)
    gainers_count = sum(1 for r in all_sorted if r["change_pct"] > 0)
    losers_count = sum(1 for r in all_sorted if r["change_pct"] < 0)
    unchanged_count = total_scanned - gainers_count - losers_count
    avg_change = round(sum(r["change_pct"] for r in all_sorted) / total_scanned, 2) if total_scanned else 0.0

    session_type, status_text, is_live, time_str = get_market_session_info()
    ny_tz = get_ny_timezone()
    updated_at = datetime.now(ny_tz).isoformat()

    result_data = {
        "ok": True,
        "date": session_date_str,
        "display_date": format_display_date(session_date_str),
        "session_type": session_type,
        "status_text": status_text,
        "is_live_premarket": is_live,
        "updated_at": updated_at,
        "updated_at_display": datetime.now(ny_tz).strftime("%I:%M:%S %p ET"),
        "universe_info": {
            "total_tickers": total_scanned,
            "watchlist_count": sum(1 for r in all_sorted if r["in_watchlist"]),
            "top50_count": sum(1 for r in all_sorted if r["in_top50"])
        },
        "stats": {
            "total_scanned": total_scanned,
            "gainers_count": gainers_count,
            "losers_count": losers_count,
            "unchanged_count": unchanged_count,
            "avg_change_pct": avg_change,
            "top_gainer": {"ticker": top_gainers[0]["ticker"], "change_pct": top_gainers[0]["change_pct"], "price": top_gainers[0]["price"], "name": top_gainers[0]["name"]} if top_gainers else None,
            "top_loser": {"ticker": top_losers[0]["ticker"], "change_pct": top_losers[0]["change_pct"], "price": top_losers[0]["price"], "name": top_losers[0]["name"]} if top_losers else None,
            "most_active": {"ticker": most_active[0]["ticker"], "volume": most_active[0]["volume"], "change_pct": most_active[0]["change_pct"]} if most_active else None
        },
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "most_active": most_active,
        "all_movers": all_sorted
    }

    save_premarket_cache(result_data)
    print(f"[PremarketMovers] Scan complete. Saved {total_scanned} stocks to cache.")

    if on_progress:
        on_progress(100, f"Complete! {total_scanned} stocks analyzed.")

    return result_data

def get_premarket_movers_data(force_refresh=False):
    """
    Returns premarket movers data. If cached and not force_refresh, returns cache.
    Otherwise runs a fresh scan.
    """
    cache = load_premarket_cache()
    if cache and not force_refresh:
        # Dynamically refresh current session status info in response
        session_type, status_text, is_live, time_str = get_market_session_info()
        cache["session_type"] = session_type
        cache["status_text"] = status_text
        cache["is_live_premarket"] = is_live
        return cache

    return scan_premarket_universe()

def refresh_premarket_async():
    """Trigger background refresh of premarket movers."""
    global _premarket_progress
    if not _premarket_refresh_lock.acquire(blocking=False):
        return {"ok": False, "message": "Premarket scan already running."}

    def _worker():
        global _premarket_progress
        try:
            _premarket_progress["status"] = "running"
            _premarket_progress["pct"] = 0
            _premarket_progress["message"] = "Initializing premarket scan..."
            _premarket_progress["timestamp"] = time.time()

            def _on_p(pct, msg):
                _premarket_progress["pct"] = pct
                _premarket_progress["message"] = msg

            scan_premarket_universe(on_progress=_on_p)
            _premarket_progress["status"] = "complete"
            _premarket_progress["pct"] = 100
            _premarket_progress["message"] = "Scan completed successfully."
        except Exception as e:
            _premarket_progress["status"] = "error"
            _premarket_progress["message"] = f"Error during scan: {e}"
        finally:
            _premarket_refresh_lock.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"ok": True, "message": "Premarket scan initiated."}

def get_premarket_refresh_progress():
    """Returns progress of current premarket scan."""
    return dict(_premarket_progress)

# ── Automated Background Monitor for Pre-Market ─────────────────────────
_last_pm_auto_scan_minute = -1
_last_pm_snapshot_date = None

def check_and_run_premarket_schedule():
    """
    Background checker:
    - During premarket (04:00 - 09:30 ET Mon-Fri): auto-refreshes every 3 minutes.
    - At 09:30 ET: takes final official premarket snapshot for the day.
    """
    global _last_pm_auto_scan_minute, _last_pm_snapshot_date
    ny_tz = get_ny_timezone()
    now = datetime.now(ny_tz)

    if now.weekday() >= 5:  # Weekend
        return

    minutes_now = now.hour * 60 + now.minute
    today_str = now.strftime("%Y-%m-%d")

    # 04:00 AM to 09:29 AM ET (Pre-Market active)
    if 240 <= minutes_now < 570:
        if _last_pm_auto_scan_minute == -1 or (minutes_now - _last_pm_auto_scan_minute >= 3):
            print(f"[PremarketMovers] Running scheduled premarket scan at {now.strftime('%H:%M:%S')} ET...")
            try:
                scan_premarket_universe()
                _last_pm_auto_scan_minute = minutes_now
            except Exception as e:
                print(f"[PremarketMovers] Scheduled premarket scan error: {e}")

    # 09:30 AM to 09:35 AM ET (Market open snapshot)
    elif 570 <= minutes_now <= 575:
        if _last_pm_snapshot_date != today_str:
            print(f"[PremarketMovers] Taking official final premarket snapshot at market open for {today_str}...")
            try:
                scan_premarket_universe()
                _last_pm_snapshot_date = today_str
            except Exception as e:
                print(f"[PremarketMovers] Market open snapshot error: {e}")

def start_premarket_background_watcher():
    """Starts background thread to keep premarket data fresh."""
    def _loop():
        print("[PremarketMovers] Background premarket monitor thread initialized.")
        while True:
            try:
                check_and_run_premarket_schedule()
            except Exception as e:
                print(f"[PremarketMovers] Background watcher exception: {e}")
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
