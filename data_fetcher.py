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
_WEBULL_MAX_FAILURES = 2  # After this many consecutive failures, skip for the rest of the scan

def reset_webull_circuit_breaker():
    """Reset the circuit breaker — call at the start of each new scan."""
    global _webull_unofficial_failures, _webull_openapi_failures
    _webull_unofficial_failures = 0
    _webull_openapi_failures = 0

# ── Webull Unofficial Client Loader (Option B — inherits personal subscriptions) ──

_unofficial_client = None
_unofficial_initialized = False

def get_unofficial_client():
    """Retrieve and initialize the unofficial Webull client using account credentials."""
    global _unofficial_client, _unofficial_initialized
    if _unofficial_initialized:
        return _unofficial_client
        
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

_session = None
_crumb = None
_session_time = 0


def _get_session(force_new=False):
    """Create (or reuse) a session with Yahoo cookie + crumb."""
    global _session, _crumb, _session_time

    # Reuse session for up to 10 minutes
    if not force_new and _session is not None and (time.time() - _session_time) < 600:
        return _session, _crumb

    _session = requests.Session()
    _session.headers.update(_HEADERS)

    # Step 1: Get A3 cookie from fc.yahoo.com (returns 404 but sets cookie)
    try:
        _session.get("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass

    # Step 2: Get crumb using the cookie
    _crumb = None
    try:
        resp = _session.get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            timeout=10,
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
        
        tId = wb_un.get_ticker(ticker)
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
    """Fetch OHLCV data using direct Yahoo Finance chart API."""
    session, crumb = _ensure_session()

    end_ts = int(datetime.now().timestamp())
    start_ts = int((datetime.now() - timedelta(days=days)).timestamp())

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {
        "period1": start_ts,
        "period2": end_ts,
        "interval": interval,
        "includePrePost": includePrePost,
    }
    if crumb:
        params["crumb"] = crumb

    try:
        resp = session.get(url, params=params, timeout=15)

        if resp.status_code in (401, 403):
            session, crumb = _get_session(force_new=True)
            if crumb:
                params["crumb"] = crumb
            resp = session.get(url, params=params, timeout=15)

        if resp.status_code != 200:
            return None

        data = resp.json()
        chart = data.get("chart", {})
        result = chart.get("result")

        if not result:
            return None

        result = result[0]
        timestamps = result.get("timestamp")
        if not timestamps:
            return None

        meta = result.get("meta", {})
        quote = result["indicators"]["quote"][0]

        dt_index = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("America/New_York")
        if interval == "1d":
            dt_index = dt_index.normalize()

        df = pd.DataFrame(
            {
                "Open": quote.get("open"),
                "High": quote.get("high"),
                "Low": quote.get("low"),
                "Close": quote.get("close"),
                "Volume": quote.get("volume"),
            },
            index=dt_index,
        )
        df.index.name = "Date"
        df = df.dropna(subset=["Close"])
        df["Volume"] = df["Volume"].fillna(0).astype(np.int64)

        df.attrs["fiftyTwoWeekHigh"] = meta.get("fiftyTwoWeekHigh")
        df.attrs["fiftyTwoWeekLow"] = meta.get("fiftyTwoWeekLow")
        df.attrs["previousClose"] = meta.get("previousClose", meta.get("chartPreviousClose"))

        return df if not df.empty else None

    except Exception:
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
    return _fetch_yahoo_one(ticker, days=days, interval=interval, includePrePost=includePrePost)


# ── Options Chain Download (Webull Unofficial & Yahoo) ───────────────

def _fetch_yahoo_options_chain(ticker):
    """Fetch option chain metadata using Yahoo Finance fallback."""
    session, crumb = _ensure_session()
    
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}"
    params = {}
    if crumb: params["crumb"] = crumb

    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        result = data.get("optionChain", {}).get("result", [])
        if not result:
            return None
        
        result = result[0]
        expirations = result.get("expirationDates", [])
        
        return {
            "ticker": ticker,
            "expirations": expirations,
            "underlyingPrice": result.get("quote", {}).get("regularMarketPrice"),
            "firstChain": result.get("options", [{}])[0]
        }
    except Exception:
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
            data = {'count': -1, 'direction': 'all', 'tickerId': wb_un.get_ticker(ticker)}
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
    return _fetch_yahoo_options_chain(ticker)


def _fetch_yahoo_options_for_expiration(ticker, expiration_ts):
    """Fetch the full option chain for a specific expiration date from Yahoo Finance."""
    session, crumb = _ensure_session()
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}"
    params = {"date": expiration_ts}
    if crumb: params["crumb"] = crumb

    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code != 200: return None
        
        data = resp.json()
        result = data.get("optionChain", {}).get("result", [])
        if not result: return None
        
        return result[0].get("options", [{}])[0]
    except Exception:
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
    return _fetch_yahoo_options_for_expiration(ticker, expiration_ts)


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

    # 2. Fallback to Yahoo Finance search endpoint
    try:
        print(f"[Yahoo Fallback] Fetching news for {ticker}...")
        session, crumb = _ensure_session()
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        raw_limit = max(20, limit * 3)
        params = {"q": ticker, "newsCount": raw_limit}
        if crumb:
            params["crumb"] = crumb
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            yahoo_news = data.get("news", [])
            normalized = []
            for item in yahoo_news:
                title = item.get("title", "")
                
                # Check related tickers tag
                related_tickers = item.get("relatedTickers", [])
                
                # Yahoo search results filtering:
                # Must be tagged with ticker, AND must apply to <= 3 tickers (avoiding macro index roundups).
                # If relatedTickers is completely missing, fallback to title/regex checking.
                ticker_upper = ticker.upper()
                is_target_stock_specific = False
                
                if related_tickers:
                    if ticker_upper in related_tickers and len(related_tickers) <= 3:
                        is_target_stock_specific = True
                else:
                    # Fallback if relatedTickers list is not present
                    if is_news_relevant(title, ticker):
                        is_target_stock_specific = True
                        
                if not is_target_stock_specific:
                    continue
                    
                publisher = item.get("publisher", "Yahoo Finance")
                url = item.get("link", "")
                
                # Filter out TipRanks articles (paid site)
                if publisher.lower() == "tipranks" or "tipranks.com" in url.lower():
                    continue
                    
                pub_ts = item.get("providerPublishTime")
                if pub_ts:
                    publish_time = datetime.fromtimestamp(int(pub_ts), _tz.utc)
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
        print(f"[Yahoo Fallback] News fetch error for {ticker}: {e}")

    return []

