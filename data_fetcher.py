"""
Cloud-safe stock data fetcher.

Uses Yahoo Finance's chart API directly with browser-like headers
and proper cookie/crumb authentication. This bypasses the
bot-detection that blocks the yfinance library on cloud servers.

Now integrates:
1. Official Webull OpenAPI (Option A) support.
2. Unofficial Webull SDK (Option B) support which logs in using account credentials,
   allowing users to inherit their personal real-time stock and options data subscriptions!
3. Seamless, automatic fallback to Yahoo Finance on permission or access errors.
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import warnings
import traceback as tb
import os
import pickle
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

warnings.filterwarnings("ignore")

# ── Webull Circuit Breaker (skip after N consecutive failures) ───────────

_webull_unofficial_failures = 0
_webull_openapi_failures = 0
_WEBULL_MAX_FAILURES = 5  # After this many consecutive failures, skip for the rest of the scan

_yahoo_failures = 0
_YAHOO_MAX_FAILURES = 10  # After this many consecutive failures, skip Yahoo Finance

def reset_webull_circuit_breaker():
    """Reset the circuit breaker — call at the start of each new scan."""
    global _webull_unofficial_failures, _webull_openapi_failures, _yahoo_failures
    _webull_unofficial_failures = 0
    _webull_openapi_failures = 0
    _yahoo_failures = 0


# ── Webull Unofficial Client Loader (Option B — inherits personal subscriptions) ──

_unofficial_client = None
_unofficial_initialized = False

def get_unofficial_client():
    """Retrieve and initialize the unofficial Webull client using account credentials."""
    global _unofficial_client, _unofficial_initialized
    if _unofficial_initialized:
        return _unofficial_client

    # Bypass only if we would need to perform an interactive MFA login
    token_path = os.path.dirname(__file__)
    credentials_file = os.path.join(token_path, "webull_credentials.json")
    has_env_token = bool(os.getenv("WEBULL_ACCESS_TOKEN") and os.getenv("WEBULL_DID"))
    has_cached_file = os.path.exists(credentials_file)

    import sys
    if not (has_env_token or has_cached_file):
        if os.getenv("RENDER") or not (sys.stdin and sys.stdin.isatty()):
            print("[Webull Unofficial] Skipping Webull client in cloud/non-interactive environment to prevent hangs.")
            _unofficial_client = None
            _unofficial_initialized = True
            return None
        
    email = os.getenv("WEBULL_EMAIL")

    password = os.getenv("WEBULL_PASSWORD")
    trade_pin = os.getenv("WEBULL_TRADE_PIN")
    
    if (email and email != "your_email_here" and password and password != "your_password_here") or os.getenv("WEBULL_ACCESS_TOKEN"):
        try:
            from webull import webull
            wb = webull()
            token_path = os.path.dirname(__file__)
            credentials_file = os.path.join(token_path, "webull_credentials.json")
            
            # 1. Try to load cached token from environment variables (great for cloud environments like Render!)
            env_access_token = (os.getenv("WEBULL_ACCESS_TOKEN") or "").strip() or None
            env_did = (os.getenv("WEBULL_DID") or "").strip() or None
            if env_access_token and env_did:
                try:
                    wb._access_token = env_access_token
                    wb._did = env_did
                    wb._refresh_token = (os.getenv("WEBULL_REFRESH_TOKEN") or "dummy_refresh_token_bypassed").strip()
                    
                    # Ensure we set did.bin cache if running locally
                    try:
                        did_bin_file = os.path.join(token_path, "did.bin")
                        if not os.path.exists(did_bin_file):
                            with open(did_bin_file, "wb") as f:
                                pickle.dump(env_did, f)
                    except Exception:
                        pass
                    
                    # Trust env-var tokens directly — skip is_logged_in() which fails
                    # from cloud/datacenter IPs (Render, AWS, etc). The circuit breaker
                    # in fetch_one will handle expired tokens gracefully.
                    try:
                        wb._account_id = wb.get_account_id()
                    except Exception:
                        wb._account_id = None  # Non-critical; data fetching still works
                    
                    print("[Webull Unofficial] Successfully authenticated using Environment Variables.")
                    _unofficial_client = wb
                    _unofficial_initialized = True
                    return _unofficial_client
                except Exception as e:
                    print(f"[Webull Unofficial] Environment token load failed: {e}")
            
            # 2. Try to load cached token from local file
            if os.path.exists(credentials_file):
                try:
                    with open(credentials_file, "rb") as f:
                        token_data = pickle.load(f)
                    
                    wb._access_token = token_data.get("accessToken")
                    wb._refresh_token = token_data.get("refreshToken")
                    wb._token_expire = token_data.get("tokenExpireTime")
                    wb._uuid = token_data.get("uuid")
                    
                    if wb.is_logged_in():
                        wb._account_id = token_data.get("account_id") or wb.get_account_id()
                        print("[Webull Unofficial] Successfully logged in using cached credentials.")
                        _unofficial_client = wb
                        _unofficial_initialized = True
                        return _unofficial_client
                except Exception as e:
                    print(f"[Webull Unofficial] Cached token load failed: {e}")
            
            # 2. Perform Login if cached token fails/does not exist
            import sys
            is_interactive = sys.stdin and sys.stdin.isatty() and not os.getenv("RENDER")
            if not is_interactive:
                print("[Webull Unofficial] Skipping fresh login in non-interactive/Render environment to prevent hanging.")
                _unofficial_client = None
                _unofficial_initialized = True
                return None

            print(f"[Webull Unofficial] Logging in as '{email}'...")
            res = wb.login(email, password, save_token=True, token_path=token_path)
            
            if 'accessToken' in res:
                wb._account_id = wb.get_account_id()
                print("[Webull Unofficial] Login successful!")
                
                # Cache the account_id inside credentials file too
                try:
                    res['account_id'] = wb._account_id
                    wb._save_token(res, token_path)
                except Exception:
                    pass
                
                # If Trade PIN is provided, unlock trading token as well
                if trade_pin:
                    try:
                        wb.get_trade_token(trade_pin)
                        print("[Webull Unofficial] Trade token verified.")
                    except Exception as e:
                        print(f"[Webull Unofficial] Trade PIN verification error: {e}")
                        
                _unofficial_client = wb
            else:
                print(f"[Webull Unofficial] Login failed: {res}")
                _unofficial_client = None
                
        except Exception as e:
            print(f"[Webull Unofficial] Login exception: {e}")
            _unofficial_client = None
    else:
        _unofficial_client = None
        
    _unofficial_initialized = True
    return _unofficial_client


# ── Webull OpenAPI Lazy Client Loader (Option A) ─────────────────────────

_webull_client = None
_webull_initialized = False

def get_webull_client():
    """Retrieve and initialize the Webull OpenAPI client dynamically."""
    global _webull_client, _webull_initialized
    if _webull_initialized:
        return _webull_client

    # Bypass only if credentials are not configured
    app_key = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")
    has_credentials = bool(app_key and app_key != "your_app_key_here" and app_secret and app_secret != "your_app_secret_here")

    import sys
    if not has_credentials:
        if os.getenv("RENDER") or not (sys.stdin and sys.stdin.isatty()):
            print("[Webull OpenAPI] Skipping Webull OpenAPI client in cloud/non-interactive environment to prevent hangs.")
            _webull_client = None
            _webull_initialized = True
            return None
        
    app_key = os.getenv("WEBULL_APP_KEY")

    app_secret = os.getenv("WEBULL_APP_SECRET")
    region_str = os.getenv("WEBULL_REGION", "us").lower()
    
    if app_key and app_key != "your_app_key_here" and app_secret and app_secret != "your_app_secret_here":
        try:
            from webull.core.client import ApiClient
            from webull.core.common.region import Region
            from webull.data.data_client import DataClient
            
            # Map region string to Region enum
            region = Region.US.value
            for r in Region:
                if r.value == region_str:
                    region = r.value
                    break
            
            print(f"[Webull OpenAPI] Initializing client for region '{region}'...")
            api_client = ApiClient(app_key, app_secret, region)
            _webull_client = DataClient(api_client)
            print("[Webull OpenAPI] Client initialized successfully.")
        except Exception as e:
            print(f"[Webull OpenAPI] Error initializing client: {e}")
            _webull_client = None
    else:
        _webull_client = None
        
    _webull_initialized = True
    return _webull_client


# ── Session management for Yahoo Finance Fallback ──────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

import threading

_session = None
_crumb = None
_session_time = 0
_last_force_new_time = 0
_session_lock = threading.Lock()


def _get_session(force_new=False):
    """Create (or reuse) a session with Yahoo cookie + crumb."""
    global _session, _crumb, _session_time, _last_force_new_time

    with _session_lock:
        # Reuse session for up to 10 minutes
        if not force_new and _session is not None and (time.time() - _session_time) < 600:
            return _session, _crumb

        # Throttle force_new to at most once per 60 seconds
        if force_new:
            now = time.time()
            if now - _last_force_new_time < 60:
                return _session, _crumb
            _last_force_new_time = now

        _session = requests.Session()
        _session.headers.update(_HEADERS)

        # Step 1: Get A3 cookie from fc.yahoo.com (returns 404 but sets cookie)
        try:
            _session.get("https://fc.yahoo.com", timeout=3)
        except Exception:
            pass

        # Step 2: Get crumb using the cookie
        _crumb = None
        try:
            resp = _session.get(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                timeout=3,
            )
            if resp.status_code == 200 and len(resp.text) < 50:
                _crumb = resp.text.strip()
        except Exception:
            pass

        _session_time = time.time()
        return _session, _crumb



def _ensure_session():
    """Get session, retry once if crumb is missing."""
    session, crumb = _get_session()
    if crumb is None:
        time.sleep(1)
        session, crumb = _get_session(force_new=True)
    return session, crumb


# ── Webull OpenAPI Fetcher (Option A) ───────────────────────────────────

def _fetch_webull_openapi_one(ticker, days=180, interval="1d", includePrePost="false"):
    """Query historical candlestick data from Webull OpenAPI."""
    client = get_webull_client()
    if not client:
        return None
        
    try:
        from webull.data.common.category import Category
        from webull.data.common.timespan import Timespan
        
        # Mapped intervals
        interval_map = {
            "1d": Timespan.D,
            "5m": Timespan.M5,
            "15m": Timespan.M15,
            "30m": Timespan.M30,
            "60m": Timespan.M60
        }
        timespan = interval_map.get(interval, Timespan.D)
        
        # K-line count
        if interval == "1d":
            count = str(min(1200, days))
        else:
            count = "600"
            
        print(f"[Webull OpenAPI] Fetching {ticker} historical bars ({interval}, count={count})...")
        
        resp = client.market_data.get_history_bar(
            symbol=ticker,
            category=Category.US_STOCK,
            timespan=timespan,
            count=count
        )
        
        if resp.status_code != 200:
            return None
            
        res_data = resp.json()
        bars_list = []
        if isinstance(res_data, list):
            bars_list = res_data
        elif isinstance(res_data, dict):
            bars_list = res_data.get("data", res_data.get("bars", res_data.get("results", [])))
            
        if not bars_list:
            return None
            
        records = []
        for bar in bars_list:
            t = bar.get("time") or bar.get("t")
            o = bar.get("open") or bar.get("o")
            h = bar.get("high") or bar.get("h")
            l = bar.get("low") or bar.get("l")
            c = bar.get("close") or bar.get("c")
            v = bar.get("volume") or bar.get("v")
            
            if t is not None and c is not None:
                records.append({
                    "Date": t,
                    "Open": float(o) if o is not None else None,
                    "High": float(h) if h is not None else None,
                    "Low": float(l) if l is not None else None,
                    "Close": float(c),
                    "Volume": int(float(v)) if v is not None else 0
                })
                
        if not records:
            return None
            
        df = pd.DataFrame(records)
        df["Date"] = pd.to_datetime(df["Date"], utc=True)
        df = df.set_index("Date")
        df.index = df.index.tz_convert("America/New_York")
        df = df.sort_index()
        
        df["Volume"] = df["Volume"].fillna(0).astype(np.int64)
        
        pre_close = float(df["Close"].iloc[-2]) if len(df) > 1 else float(df["Close"].iloc[0])
        high_52w = float(df["High"].max())
        low_52w = float(df["Low"].min())
        
        try:
            snapshot_resp = client.market_data.get_snapshot([ticker], Category.US_STOCK)
            if snapshot_resp.status_code == 200:
                snap_data = snapshot_resp.json()
                snap_list = snap_data if isinstance(snap_data, list) else snap_data.get("data", [])
                if snap_list:
                    snap = snap_list[0]
                    pre_close = float(snap.get("pre_close", pre_close))
        except Exception:
            pass
            
        df.attrs["fiftyTwoWeekHigh"] = high_52w
        df.attrs["fiftyTwoWeekLow"] = low_52w
        df.attrs["previousClose"] = pre_close
        
        print(f"[Webull OpenAPI] Successfully fetched {ticker} ({len(df)} rows)")
        return df
        
    except Exception as e:
        print(f"[Webull OpenAPI] Error fetching {ticker}: {e}")
        return None



# ── Webull Ticker Lookup Helper (Bypasses Webull SDK crypto bugs) ───────

def get_stock_ticker_id(wb_un, ticker):
    """
    Look up Webull's ticker ID for a stock/ETF, strictly ignoring cryptos.
    This fixes the issue where symbols like AMP/DASH get mapped to crypto instead of the stock.
    """
    try:
        headers = wb_un.build_req_headers()
        url = wb_un._urls.stock_id(ticker, wb_un._region_code)
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            data = result.get('data', [])
            if data:
                # 1. Look for a stock or ETF template that matches the ticker exactly
                for item in data:
                    template = item.get("template", "").lower()
                    if template in ("stock", "etf"):
                        if item.get("symbol") == ticker or item.get("disSymbol") == ticker:
                            return item["tickerId"]
                # 2. Fallback to any non-crypto template that matches exactly
                for item in data:
                    template = item.get("template", "").lower()
                    if template != "crypto":
                        if item.get("symbol") == ticker or item.get("disSymbol") == ticker:
                            return item["tickerId"]
                # 3. Fallback to the first non-crypto item
                for item in data:
                    if item.get("template", "").lower() != "crypto":
                        return item["tickerId"]
                # 4. Final fallback to get_ticker standard method
                return wb_un.get_ticker(ticker)
    except Exception:
        pass
    # Standard Webull fallback
    try:
        return wb_un.get_ticker(ticker)
    except Exception:
        return 0


# ── Webull Unofficial Fetcher (Option B) ────────────────────────────────

def _fetch_webull_unofficial_one(ticker, days=180, interval="1d", includePrePost="false"):
    """Query historical candlestick data from Webull using unofficial credentials login."""
    wb_un = get_unofficial_client()
    if not wb_un:
        return None
        
    try:
        # Map intervals
        interval_map = {
            "1d": "d1",
            "5m": "m5",
            "15m": "m15",
            "30m": "m30",
            "60m": "h1"
        }
        mapped_interval = interval_map.get(interval, "d1")
        count = days if interval == "1d" else 600
        extend = 1 if includePrePost == "true" else 0
        
        import requests
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo as _ZI
        except ImportError:
            from backports.zoneinfo import ZoneInfo as _ZI
        
        tId = get_stock_ticker_id(wb_un, ticker)
        timeStamp = int(time.time())
        params = {'extendTrading': extend}
        headers = wb_un.build_req_headers()
        
        print(f"[Webull Unofficial] Fetching {ticker} bars ({interval} -> {mapped_interval}, count={count}, extend={extend})...")
        resp = requests.get(
            wb_un._urls.bars(tId, mapped_interval, count, timeStamp),
            params=params,
            headers=headers,
            timeout=wb_un.timeout
        )
        if resp.status_code != 200:
            return None
            
        result = resp.json()
        if not result or not result[0].get('data'):
            return None
            
        time_zone = _ZI(result[0]['timeZone'])
        records = []
        for row in result[0]['data']:
            parts = row.split(',')
            parts = ['0' if v == 'null' else v for v in parts]
            dt = datetime.fromtimestamp(int(parts[0])).astimezone(time_zone)
            records.append({
                "Date": dt,
                "Open": float(parts[1]),
                "High": float(parts[3]),
                "Low": float(parts[4]),
                "Close": float(parts[2]),
                "Volume": int(float(parts[6]))
            })
            
        if records:
            df = pd.DataFrame(records)
            df = df.set_index("Date")
            df.index = df.index.tz_convert("America/New_York")
            df = df.sort_index()
            
            df["Volume"] = df["Volume"].fillna(0).astype(np.int64)
            
            # Fetch 52-week metrics
            high_52w = float(df["High"].max())
            low_52w = float(df["Low"].min())
            pre_close = float(df["Close"].iloc[-2]) if len(df) > 1 else float(df["Close"].iloc[0])
            
            try:
                quote = wb_un.get_quote(stock=ticker)
                if quote:
                    pre_close = float(quote.get("close", pre_close))
            except Exception:
                pass
                
            df.attrs["fiftyTwoWeekHigh"] = high_52w
            df.attrs["fiftyTwoWeekLow"] = low_52w
            df.attrs["previousClose"] = pre_close
            
            print(f"[Webull Unofficial] Successfully fetched {ticker} ({len(df)} rows)")
            return df
            
        return None
    except Exception as e:
        print(f"[Webull Unofficial] Error fetching {ticker}: {e}")
        return None


# ── Yahoo Finance Fetcher ─────────────────────────────────────────────

def _fetch_yahoo_one(ticker, days=180, interval="1d", includePrePost="false"):
    """Fetch OHLCV data using yfinance."""
    global _yahoo_failures
    try:
        import yfinance as yf
        
        # Map parameters
        prepost = True if includePrePost.lower() == "true" else False
        
        # yfinance period format
        if days <= 7:
            period = f"{days}d"
        elif days <= 60:
            period = "1mo"
        elif days <= 180:
            period = "6mo"
        elif days <= 365:
            period = "1y"
        else:
            period = "2y"
            
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval, prepost=prepost)
        
        if df.empty:
            return None
            
        # Standardize columns to match Webull
        df.index.name = "Date"
        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")
            
        # Ensure we just have Open, High, Low, Close, Volume
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df["Volume"] = df["Volume"].fillna(0).astype(np.int64)
        
        # Try to attach 52w info if possible
        info = {}
        try:
            info = t.info
        except Exception:
            pass
            
        high_52w = info.get("fiftyTwoWeekHigh", df["High"].max())
        low_52w = info.get("fiftyTwoWeekLow", df["Low"].min())
        pre_close = info.get("previousClose", df["Close"].iloc[-2] if len(df) > 1 else df["Close"].iloc[0])
        
        df.attrs["fiftyTwoWeekHigh"] = high_52w
        df.attrs["fiftyTwoWeekLow"] = low_52w
        df.attrs["previousClose"] = pre_close
        
        return df

    except Exception as e:
        _yahoo_failures += 1
        if _yahoo_failures >= _YAHOO_MAX_FAILURES:
            print(f"[Circuit Breaker] Yahoo Finance failed {_YAHOO_MAX_FAILURES}x consecutively — skipping for rest of scan")
        return None



# ── Unified Single Ticker Fetcher (Resilient Ordering) ────────────────

def fetch_one(ticker, days=180, interval="1d", includePrePost="false", skip_webull=False):
    """
    Fetch OHLCV data for one ticker.
    Resilient multi-layered ordering:
    1. Try Unofficial Webull account credentials (Option B — inherits user's real-time subscriptions).
    2. Try Official Webull OpenAPI developer credentials (Option A).
    3. Seamless, automatic fallback to Yahoo Finance chart scraping.
    
    Circuit breaker: after 2 consecutive Webull failures, auto-skip Webull.
    """
    global _webull_unofficial_failures, _webull_openapi_failures

    # 1. Try Option B (Unofficial Session — Inherits all real-time quotes subscriptions)
    if not skip_webull and _webull_unofficial_failures < _WEBULL_MAX_FAILURES:
        wb_un = get_unofficial_client()
        if wb_un:
            df = _fetch_webull_unofficial_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
            if df is not None:
                _webull_unofficial_failures = 0  # Reset on success
                return df
            else:
                _webull_unofficial_failures += 1
                if _webull_unofficial_failures >= _WEBULL_MAX_FAILURES:
                    print(f"[Circuit Breaker] Webull Unofficial failed {_WEBULL_MAX_FAILURES}x consecutively — skipping for rest of scan")
            
    # 2. Try Option A (Official OpenAPI — Requires separate developer subscription toggles)
    if not skip_webull and _webull_openapi_failures < _WEBULL_MAX_FAILURES:
        wb_openapi = get_webull_client()
        if wb_openapi:
            df = _fetch_webull_openapi_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
            if df is not None:
                _webull_openapi_failures = 0  # Reset on success
                return df
            else:
                _webull_openapi_failures += 1
                if _webull_openapi_failures >= _WEBULL_MAX_FAILURES:
                    print(f"[Circuit Breaker] Webull OpenAPI failed {_WEBULL_MAX_FAILURES}x consecutively — skipping for rest of scan")

    # 3. Fallback to Yahoo Finance
    global _yahoo_failures
    if _yahoo_failures < _YAHOO_MAX_FAILURES:
        df = _fetch_yahoo_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
        if df is not None:
            _yahoo_failures = 0
            return df
        else:
            _yahoo_failures += 1
            if _yahoo_failures >= _YAHOO_MAX_FAILURES:
                print(f"[Circuit Breaker] Yahoo Finance failed {_YAHOO_MAX_FAILURES}x consecutively — skipping for rest of scan")

    return None



# ── Options Chain Download (Webull Unofficial & Yahoo) ───────────────

def _fetch_yahoo_options_chain(ticker):
    """Fetch option chain metadata using Yahoo Finance fallback."""
    global _yahoo_failures
    try:
        import yfinance as yf
        from datetime import datetime
        
        t = yf.Ticker(ticker)
        dates = t.options
        if not dates:
            return None
            
        expirations = []
        for d in dates:
            try:
                ts = int(datetime.strptime(d, "%Y-%m-%d").timestamp())
                expirations.append(ts)
            except Exception:
                pass
                
        underlyingPrice = None
        try:
            underlyingPrice = t.info.get("regularMarketPrice")
            if underlyingPrice is None:
                underlyingPrice = float(t.history(period="1d")["Close"].iloc[-1])
        except Exception:
            pass
            
        return {
            "ticker": ticker,
            "expirations": expirations,
            "underlyingPrice": underlyingPrice,
            "firstChain": None # Not strictly needed initially
        }
    except Exception as e:
        _yahoo_failures += 1
        if _yahoo_failures >= _YAHOO_MAX_FAILURES:
            print(f"[Circuit Breaker] Yahoo Finance options chain failed {_YAHOO_MAX_FAILURES}x consecutively — skipping for rest of scan")
        return None



def fetch_options_chain(ticker):
    """
    Fetch options chain with dynamic inheritance of Webull subscriptions.
    1. Try Unofficial Webull Account Client (Option B — fetches real-time options data).
    2. Fallback to Yahoo Finance options data.
    """
    wb_un = get_unofficial_client()
    if wb_un:
        try:
            import requests
            print(f"[Webull Unofficial] Fetching options chain for {ticker}...")
            
            headers = wb_un.build_req_headers()
            data = {'count': -1, 'direction': 'all', 'tickerId': get_stock_ticker_id(wb_un, ticker)}
            res = requests.post(wb_un._urls.options_exp_dat_new(), json=data, headers=headers, timeout=wb_un.timeout)
            
            if res.status_code == 200:
                res_json = res.json()
                expirations = []
                all_chains = {}
                first_chain_data = None
                
                for entry in res_json.get('expireDateList', []):
                    date_str = str(entry['from']['date'])
                    try:
                        ts = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
                        expirations.append(ts)
                    except Exception:
                        continue
                        
                    t_data = entry.get('data', [])
                    calls = []
                    puts = []
                    for item in t_data:
                        strike = float(item.get("strikePrice", 0))
                        direction = item.get("direction")
                        
                        bid_val = None
                        if item.get("bidList") and isinstance(item["bidList"], list) and len(item["bidList"]) > 0:
                            bid_val = float(item["bidList"][0].get("price", 0))
                            
                        ask_val = None
                        if item.get("askList") and isinstance(item["askList"], list) and len(item["askList"]) > 0:
                            ask_val = float(item["askList"][0].get("price", 0))
                            
                        contract_data = {
                            "contractSymbol": item.get("symbol"),
                            "strike": strike,
                            "bid": bid_val,
                            "ask": ask_val,
                            "volume": int(float(item.get("volume", 0))) if item.get("volume") else None,
                            "openInterest": int(float(item.get("openInterest", 0))) if item.get("openInterest") else None,
                            "impliedVolatility": float(item.get("impVol", 0)) if item.get("impVol") else None
                        }
                        if direction == "call":
                            calls.append(contract_data)
                        elif direction == "put":
                            puts.append(contract_data)
                            
                    chain = {"calls": calls, "puts": puts}
                    all_chains[ts] = chain
                    if not first_chain_data:
                        first_chain_data = chain
                        
                if expirations:
                    underlyingPrice = None
                    try:
                        quote = wb_un.get_quote(stock=ticker)
                        underlyingPrice = float(quote.get("close")) if quote.get("close") else None
                    except Exception:
                        pass
                        
                    print(f"[Webull Unofficial] Option chain parsed successfully for {ticker} (got {len(expirations)} expirations)")
                    return {
                        "ticker": ticker,
                        "expirations": expirations,
                        "underlyingPrice": underlyingPrice,
                        "firstChain": first_chain_data,
                        "allChains": all_chains
                    }
        except Exception as e:
            print(f"[Webull Unofficial] Error fetching option chain for {ticker}: {e}")
            
    # Fallback to Yahoo
    global _yahoo_failures
    if _yahoo_failures < _YAHOO_MAX_FAILURES:
        res = _fetch_yahoo_options_chain(ticker)
        if res is not None:
            _yahoo_failures = 0
            return res
        else:
            _yahoo_failures += 1
            if _yahoo_failures >= _YAHOO_MAX_FAILURES:
                print(f"[Circuit Breaker] Yahoo Finance failed {_YAHOO_MAX_FAILURES}x consecutively — skipping for rest of scan")

    return None



def _fetch_yahoo_options_for_expiration(ticker, expiration_ts):
    """Fetch the full option chain for a specific expiration date from Yahoo Finance."""
    try:
        import yfinance as yf
        from datetime import datetime
        
        date_str = datetime.fromtimestamp(expiration_ts).strftime("%Y-%m-%d")
        t = yf.Ticker(ticker)
        chain = t.option_chain(date_str)
        
        calls = chain.calls.to_dict(orient="records")
        puts = chain.puts.to_dict(orient="records")
        
        # Convert NaN to None for JSON serialization
        import math
        for c in calls + puts:
            for k, v in c.items():
                if isinstance(v, float) and math.isnan(v):
                    c[k] = None
                    
        return {"calls": calls, "puts": puts}
    except Exception as e:
        print(f"[Yahoo Fallback] Failed to fetch options for {ticker} at {expiration_ts}: {e}")
        return None


def fetch_options_for_expiration(ticker, expiration_ts):
    """Fetch options for a specific expiration timestamp from Webull or Yahoo."""
    wb_un = get_unofficial_client()
    if wb_un:
        try:
            date_str = datetime.fromtimestamp(expiration_ts).strftime("%Y-%m-%d")
            print(f"[Webull Unofficial] Fetching options chain for {ticker} at {date_str}...")
            
            webull_chain = wb_un.get_options(stock=ticker, expireDate=date_str)
            calls = []
            puts = []
            for entry in webull_chain:
                strike = float(entry.get("strikePrice", 0))
                
                if "call" in entry:
                    c_data = entry["call"]
                    calls.append({
                        "contractSymbol": c_data.get("symbol"),
                        "strike": strike,
                        "tickerId": c_data.get("tickerId"),
                        "bid": float(c_data.get("bid", 0)) if c_data.get("bid") else None,
                        "ask": float(c_data.get("ask", 0)) if c_data.get("ask") else None,
                        "volume": int(float(c_data.get("volume", 0))) if c_data.get("volume") else None,
                        "openInterest": int(float(c_data.get("openInterest", 0))) if c_data.get("openInterest") else None,
                        "impliedVolatility": float(c_data.get("impliedVolatility", 0)) if c_data.get("impliedVolatility") else None
                    })
                if "put" in entry:
                    p_data = entry["put"]
                    puts.append({
                        "contractSymbol": p_data.get("symbol"),
                        "strike": strike,
                        "tickerId": p_data.get("tickerId"),
                        "bid": float(p_data.get("bid", 0)) if p_data.get("bid") else None,
                        "ask": float(p_data.get("ask", 0)) if p_data.get("ask") else None,
                        "volume": int(float(p_data.get("volume", 0))) if p_data.get("volume") else None,
                        "openInterest": int(float(p_data.get("openInterest", 0))) if p_data.get("openInterest") else None,
                        "impliedVolatility": float(p_data.get("impliedVolatility", 0)) if p_data.get("impliedVolatility") else None
                    })
            
            return {
                "calls": calls,
                "puts": puts
            }
        except Exception as e:
            print(f"[Webull Unofficial] Error fetching options at expiration timestamp {expiration_ts}: {e}")
            
    # Fallback to Yahoo
    global _yahoo_failures
    if _yahoo_failures < _YAHOO_MAX_FAILURES:
        res = _fetch_yahoo_options_for_expiration(ticker, expiration_ts)
        if res is not None:
            _yahoo_failures = 0
            return res
        else:
            _yahoo_failures += 1
            if _yahoo_failures >= _YAHOO_MAX_FAILURES:
                print(f"[Circuit Breaker] Yahoo Finance failed {_YAHOO_MAX_FAILURES}x consecutively — skipping for rest of scan")

    return None



# ── Batch download (sequential — for watchlists) ────────────────────

def fetch_batch(tickers, days=180, delay=0.05, on_progress=None, interval="1d", includePrePost="false", skip_webull=False):
    """
    Download data for a list of tickers sequentially.
    Returns dict of {ticker: DataFrame}.
    """
    data = {}
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        if on_progress:
            on_progress(i, total, ticker)

        df = fetch_one(ticker, days=days, interval=interval, includePrePost=includePrePost, skip_webull=skip_webull)
        if df is not None and len(df) >= 50:
            data[ticker] = df

        if delay > 0 and i < total - 1:
            time.sleep(delay)

    return data


# ── Batch download (concurrent — for full market scan) ───────────────

def fetch_batch_concurrent(tickers, days=180, max_workers=8,
                           on_progress=None, delay=0.05, interval="1d", includePrePost="false",
                           process_fn=None, skip_webull=False):
    """
    Download data for many tickers using a thread pool.
    If process_fn is provided, it processes each DataFrame on-the-fly and returns the result,
    completely discarding the DataFrame to keep memory footprint close to zero!
    """
    # Reset circuit breaker at the start of each batch scan
    reset_webull_circuit_breaker()

    data = {}
    completed = 0
    total = len(tickers)

    def _fetch(i, ticker):
        time.sleep(delay * (i % max_workers))
        df = fetch_one(ticker, days=days, interval=interval, includePrePost=includePrePost, skip_webull=skip_webull)
        if df is not None:
            if process_fn:
                try:
                    res = process_fn(ticker, df)
                    return ticker, res
                except Exception as e:
                    print(f"[fetch_batch_concurrent] Error in process_fn for {ticker}: {e}")
                    return ticker, None
            else:
                return ticker, df
        return ticker, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch, i, t): t for i, t in enumerate(tickers)}

        for future in as_completed(futures):
            completed += 1
            try:
                ticker, res = future.result(timeout=20)
                if res is not None:
                    data[ticker] = res
                if on_progress:
                    on_progress(completed, total, ticker)
            except Exception as e:
                print(f"[fetch_batch_concurrent] Error/timeout downloading {futures[future]}: {e}")
                if on_progress:
                    on_progress(completed, total, futures[future])

    return data


# ── Batch Quote Fetcher (for pre-filtering) ──────────────────────────

def fetch_quotes_batch(tickers, max_workers=10, on_progress=None):
    """
    Fetch live Webull quote data for many tickers concurrently.
    Returns dict of {ticker: quote_dict} where quote_dict has keys like:
      avgVol10Day, close, volume, name, totalShares, etc.
    Only returns entries where the quote was successfully fetched.
    """
    wb_un = get_unofficial_client()
    if not wb_un:
        print("  [fetch_quotes_batch] No Webull client — cannot pre-filter")
        return {}

    quotes = {}
    completed = 0
    total = len(tickers)
    
    consecutive_failures = 0
    circuit_broken = False
    wb_un.timeout = 3

    def _fetch_quote(ticker):
        if circuit_broken:
            return ticker, None
        try:
            q = wb_un.get_quote(stock=ticker)
            if q:
                return ticker, q
        except Exception:
            pass
        return ticker, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_quote, t): t for t in tickers}
        for future in as_completed(futures):
            if circuit_broken:
                completed += 1
                if on_progress:
                    on_progress(completed, total, futures[future])
                continue
            completed += 1
            try:
                ticker, q = future.result(timeout=10)
                if q:
                    quotes[ticker] = q
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    
                if consecutive_failures > 15:
                    print("  [fetch_quotes_batch] CIRCUIT BREAKER TRIPPED! Too many Webull fetch failures.")
                    circuit_broken = True
                    
                if on_progress:
                    on_progress(completed, total, ticker)
            except Exception:
                consecutive_failures += 1
                if consecutive_failures > 15:
                    circuit_broken = True

    return quotes


def check_optionable_batch(tickers, max_workers=10):
    """
    Check which tickers have options chains available via Webull.
    Returns a set of tickers that are optionable.
    """
    wb_un = get_unofficial_client()
    if not wb_un:
        print("  [check_optionable_batch] No Webull client — cannot check optionability")
        return set(tickers)  # Assume all are optionable if we can't check

    optionable = set()
    consecutive_failures = 0
    circuit_broken = False

    def _check_options(ticker):
        if circuit_broken:
            return ticker, False
        try:
            import requests as _req
            headers = wb_un.build_req_headers()
            ticker_id = get_stock_ticker_id(wb_un, ticker)
            if not ticker_id:
                return ticker, False
            data = {'count': -1, 'direction': 'all', 'tickerId': ticker_id}
            res = _req.post(wb_un._urls.options_exp_dat_new(), json=data, headers=headers, timeout=3)
            if res.status_code == 200:
                res_json = res.json()
                exp_list = res_json.get('expireDateList', [])
                return ticker, len(exp_list) > 0
        except Exception:
            pass
        return ticker, False

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check_options, t): t for t in tickers}
        for future in as_completed(futures):
            if circuit_broken:
                continue
            try:
                ticker, has_options = future.result(timeout=10)
                if has_options:
                    optionable.add(ticker)
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    
                if consecutive_failures > 15:
                    print("  [check_optionable_batch] CIRCUIT BREAKER TRIPPED! Too many Webull optionability failures.")
                    circuit_broken = True
            except Exception:
                consecutive_failures += 1
                if consecutive_failures > 15:
                    circuit_broken = True

    if not optionable:
        print("  [check_optionable_batch] Optionability check returned 0 matches — falling back to input ticker list")
        return set(tickers)

    return optionable


# ── Connectivity test ────────────────────────────────────────────────

def test_connection(ticker="AAPL"):
    """Test connection for all layers (Webull Unofficial, Webull OpenAPI, and Yahoo)."""
    diag = {"ticker": ticker, "time": datetime.now().isoformat()}

    # 1. Test Webull Unofficial (Option B)
    wb_un = get_unofficial_client()
    diag["webull_unofficial_configured"] = wb_un is not None
    if wb_un:
        try:
            df = wb_un.get_bars(stock=ticker, interval="d1", count=10)
            diag["webull_unofficial_ok"] = df is not None and not df.empty
            if diag["webull_unofficial_ok"]:
                diag["webull_unofficial_rows"] = len(df)
        except Exception as e:
            diag["webull_unofficial_ok"] = False
            diag["webull_unofficial_error"] = str(e)
    else:
        diag["webull_unofficial_ok"] = False
        diag["webull_unofficial_error"] = "Account credentials not configured in .env"

    # 2. Test Webull OpenAPI (Option A)
    wb_client = get_webull_client()
    diag["webull_openapi_configured"] = wb_client is not None
    if wb_client:
        try:
            from webull.data.common.category import Category
            from webull.data.common.timespan import Timespan
            
            resp = wb_client.market_data.get_history_bar(
                symbol=ticker,
                category=Category.US_STOCK,
                timespan=Timespan.D,
                count="10"
            )
            diag["webull_openapi_ok"] = resp.status_code == 200
            diag["webull_openapi_status_code"] = resp.status_code
            if resp.status_code == 200:
                diag["webull_openapi_data_sample"] = str(resp.json()[:2])
            else:
                diag["webull_openapi_error"] = f"HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            diag["webull_openapi_ok"] = False
            diag["webull_openapi_error"] = str(e)
    else:
        diag["webull_openapi_ok"] = False
        diag["webull_openapi_error"] = "OpenAPI credentials not configured in .env"

    # 3. Test Yahoo
    try:
        session, crumb = _ensure_session()
        diag["yahoo_has_crumb"] = crumb is not None

        df = _fetch_yahoo_one(ticker, days=30)
        if df is not None:
            diag["yahoo_ok"] = True
            diag["yahoo_rows"] = len(df)
            diag["yahoo_last_close"] = round(float(df["Close"].iloc[-1]), 2)
        else:
            diag["yahoo_ok"] = False
            diag["yahoo_error"] = "No data returned from Yahoo"
    except Exception as e:
        diag["yahoo_ok"] = False
        diag["yahoo_error"] = str(e)

    diag["ok"] = diag["webull_unofficial_ok"] or diag["webull_openapi_ok"] or diag["yahoo_ok"]
    return diag


# ── News Fetcher ─────────────────────────────────────────────────────

def clean_company_name(name):
    """Clean company name to its core brand/identifier by removing suffixes."""
    if not name:
        return ""
    import re
    # Remove parentheses and contents
    name = re.sub(r'\(.*?\)', '', name)
    # Common corporate designators (case-insensitive)
    suffixes = [
        r'\binc\b\.?', r'\bcorp\b\.?', r'\bcorporation\b', r'\bco\b\.?', 
        r'\bltd\b\.?', r'\blimited\b', r'\bplc\b\.?', r'\bincorporated\b',
        r'\bclass [a-z]\b', r'\bholding\b\.?', r'\bholdings\b\.?'
    ]
    for suffix in suffixes:
        name = re.sub(suffix, '', name, flags=re.IGNORECASE)
    # Strip whitespace, commas, periods, dashes
    return name.strip(" ,.-")


def is_news_relevant(title, ticker, company_name=None):
    """Determine if a news headline is relevant to the target stock."""
    if not title:
        return False
    title_lower = title.lower()
    ticker_lower = ticker.lower()
    
    import re
    # Check 1: Ticker as a standalone word (e.g. AAPL, $AAPL, (AAPL))
    if re.search(r'\b' + re.escape(ticker_lower) + r'\b', title_lower):
        return True
        
    # Check 2: Company name or its distinct words
    if company_name:
        cleaned = clean_company_name(company_name).lower()
        if cleaned:
            # Match entire cleaned name (e.g. "apple")
            if cleaned in title_lower:
                return True
            # Match non-generic words in cleaned name
            generic_words = {
                "inc", "corp", "corporation", "co", "ltd", "limited", "plc", "incorporated",
                "group", "holdings", "holding", "industries", "technologies", "technology",
                "solutions", "financial", "systems", "trust", "energy", "resources", "global",
                "international", "national", "american", "united", "partners", "capital",
                "china", "us", "usa", "first", "new", "health", "therapeutics", "pharmaceuticals",
                "biosciences", "biotech", "devices", "mining"
            }
            words = [w for w in re.split(r'\W+', cleaned) if w]
            for w in words:
                if len(w) >= 3 and w not in generic_words:
                    if re.search(r'\b' + re.escape(w) + r'\b', title_lower):
                        return True
    return False


def fetch_news(ticker, limit=5):
    """
    Fetch news articles for a single ticker.
    First tries Webull Unofficial, then Yahoo Finance.
    Returns standard list of dicts:
      [{"title": "...", "publisher": "...", "publish_time": datetime_object, "url": "..."}, ...]
    """
    from datetime import timezone as _tz
    
    # 1. Try Webull Unofficial
    wb_un = get_unofficial_client()
    if wb_un:
        try:
            print(f"[Webull Unofficial] Fetching news for {ticker}...")
            # Retrieve company name to filter articles accurately
            company_name = None
            try:
                quote = wb_un.get_quote(stock=ticker)
                if quote:
                    company_name = quote.get("name")
            except Exception:
                pass

            # Fetch more news items than limit so we can filter and still return enough
            raw_limit = max(20, limit * 3)
            news_list = wb_un.get_news(stock=ticker, items=raw_limit)
            if isinstance(news_list, list) and len(news_list) > 0:
                normalized = []
                for item in news_list:
                    title = item.get("title", "")
                    
                    # Filter: Only keep articles relevant to this specific ticker/company
                    if not is_news_relevant(title, ticker, company_name):
                        continue
                        
                    publisher = item.get("sourceName", "Webull")
                    url = item.get("newsUrl", "")
                    
                    # Filter out TipRanks articles (paid site)
                    if publisher.lower() == "tipranks" or "tipranks.com" in url.lower():
                        continue
                        
                    date_str = item.get("newsTime", "")
                    publish_time = None
                    if date_str:
                        try:
                            # Format: 2026-06-04T00:10:00.000+0000
                            base_dt = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
                            publish_time = base_dt.replace(tzinfo=_tz.utc)
                        except Exception:
                            publish_time = datetime.now(_tz.utc)
                    else:
                        publish_time = datetime.now(_tz.utc)
                    
                    normalized.append({
                        "title": title,
                        "publisher": publisher,
                        "publish_time": publish_time,
                        "url": url
                    })
                    if len(normalized) >= limit:
                        break
                return normalized
        except Exception as e:
            print(f"[Webull Unofficial] News fetch error for {ticker}: {e}")

    # 2. Fallback to Yahoo Finance search endpoint disabled (strictly use Webull)
    pass

    return []

