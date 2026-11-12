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

import sys
import threading
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

# ── Shared Timezone, Parsing & Entitlement Helpers ────────────────────

def _get_ny_tz():
    """Return America/New_York tzinfo using zoneinfo with pytz fallback."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        import pytz
        return pytz.timezone("America/New_York")


def _ny_date_to_ts(date_str):
    """Convert 'YYYY-MM-DD' to noon America/New_York Unix timestamp so UTC/EST map to the same calendar date."""
    dt = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=_get_ny_tz())
    return int(dt.timestamp())


def _ts_to_ny_date_str(ts):
    """Convert Unix timestamp to 'YYYY-MM-DD' in America/New_York timezone."""
    return datetime.fromtimestamp(float(ts), tz=_get_ny_tz()).strftime("%Y-%m-%d")


def _safe_float(val, default=None):
    """Safely parse numeric strings/numbers while treating None and '' as missing."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=0):
    """Safely parse integer strings/numbers."""
    f = _safe_float(val, None)
    return int(f) if f is not None else default


def _chunks(seq, size=20):
    """Yield successive slices of length `size` from `seq`."""
    for idx in range(0, len(seq), size):
        yield seq[idx:idx + size]


# ── Webull Circuit Breaker & OpenAPI Subscription Cache ───────────────

_webull_unofficial_failures = 0
_webull_openapi_failures = 0
_WEBULL_MAX_FAILURES = 30  # High resilience threshold to ensure Webull is always the primary data provider

_OPENAPI_NOT_SUBSCRIBED_TTL = int(os.getenv("OPENAPI_RETRY_SECONDS", "300"))
_openapi_sub_lock = threading.Lock()
_openapi_sub_blocked_until = {}  # scope -> epoch seconds
_openapi_sub_errors = {}         # scope -> last error message


def reset_webull_circuit_breaker():
    """Reset the circuit breaker — call at the start of each new scan."""
    global _webull_unofficial_failures, _webull_openapi_failures
    _webull_unofficial_failures = 0
    _webull_openapi_failures = 0


def _is_openapi_scope_blocked(scope):
    """Return True if an OpenAPI market-data scope recently returned MARKET_DATA_NOT_SUBSCRIBED."""
    with _openapi_sub_lock:
        until = _openapi_sub_blocked_until.get(scope, 0.0)
        if until > time.time():
            return True
        if scope in _openapi_sub_blocked_until:
            _openapi_sub_blocked_until.pop(scope, None)
        return False


def _mark_openapi_scope_blocked(scope, reason):
    """Cache an OpenAPI MARKET_DATA_NOT_SUBSCRIBED error for _OPENAPI_NOT_SUBSCRIBED_TTL seconds."""
    now = time.time()
    with _openapi_sub_lock:
        already_blocked = _openapi_sub_blocked_until.get(scope, 0.0) > now
        _openapi_sub_blocked_until[scope] = now + _OPENAPI_NOT_SUBSCRIBED_TTL
        _openapi_sub_errors[scope] = str(reason)
    if not already_blocked:
        print(
            f"[Webull OpenAPI] ⚠️ Scope '{scope}' not subscribed ({reason}) — "
            f"retrying automatically in {_OPENAPI_NOT_SUBSCRIBED_TTL}s."
        )


def _clear_openapi_scope_block(scope=None):
    """Clear negative subscription cache for `scope`, or all scopes when `scope` is None."""
    with _openapi_sub_lock:
        if scope is None:
            _openapi_sub_blocked_until.clear()
            _openapi_sub_errors.clear()
        else:
            _openapi_sub_blocked_until.pop(scope, None)
            _openapi_sub_errors.pop(scope, None)


def _extract_openapi_error(obj):
    """Inspect a Webull SDK ServerException or HTTP Response -> (is_not_subscribed, status_code, message)."""
    if hasattr(obj, "http_status") or hasattr(obj, "error_code"):
        status = getattr(obj, "http_status", None)
        code = str(getattr(obj, "error_code", "") or "")
        msg = str(getattr(obj, "error_msg", "") or str(obj))
        combined = f"{code}: {msg}" if code else msg
    elif hasattr(obj, "status_code"):
        status = getattr(obj, "status_code", None)
        text = str(getattr(obj, "text", "") or "")
        combined = f"HTTP {status}: {text[:300]}"
    else:
        status = None
        combined = str(obj)

    upper = combined.upper()
    is_not_sub = (
        "MARKET_DATA_NOT_SUBSCRIBED" in upper
        or (status == 403 and ("SUBSCRIBE" in upper or "INSUFFICIENT PERMISSION" in upper))
    )
    return is_not_sub, status, combined


# ── Webull Unofficial Client Loader (Option B — Legacy / Fallback) ────

_unofficial_lock = threading.Lock()
_unofficial_client = None
_unofficial_initialized = False
_unofficial_authenticated = False


def is_unofficial_authenticated():
    """Return True only when the unofficial Webull session passed wb.is_logged_in()."""
    return _unofficial_authenticated


def get_unofficial_client():
    """Retrieve and initialize the unofficial Webull client cleanly and verify login state."""
    global _unofficial_client, _unofficial_initialized, _unofficial_authenticated
    if _unofficial_initialized:
        return _unofficial_client

    with _unofficial_lock:
        if _unofficial_initialized:
            return _unofficial_client

        token_path = os.path.dirname(__file__)
        credentials_file = os.path.join(token_path, "webull_credentials.json")
        env_access_token = (os.getenv("WEBULL_ACCESS_TOKEN") or "").strip() or None
        env_did = (os.getenv("WEBULL_DID") or "").strip() or None

        try:
            try:
                from webull.webull import webull
            except ImportError:
                from webull import webull
            wb = webull()

            # Sync did.bin to WEBULL_DID when provided so internal SDK calls match the env DID
            if env_did:
                did_bin_file = os.path.join(token_path, "did.bin")
                try:
                    existing_did = None
                    if os.path.exists(did_bin_file):
                        with open(did_bin_file, "rb") as f:
                            existing_did = pickle.load(f)
                    if existing_did != env_did:
                        with open(did_bin_file, "wb") as f:
                            pickle.dump(env_did, f)
                except Exception:
                    pass

            # 1. Try to load token from environment variables
            if env_access_token:
                try:
                    wb._access_token = env_access_token
                    if env_did:
                        wb._did = env_did
                    wb._refresh_token = (os.getenv("WEBULL_REFRESH_TOKEN") or "dummy_refresh_token_bypassed").strip()
                    wb._account_id = None

                    try:
                        _unofficial_authenticated = bool(wb.is_logged_in())
                    except Exception:
                        _unofficial_authenticated = False

                    if _unofficial_authenticated:
                        print("[Webull Unofficial] Authenticated session verified via Environment Variables.")
                    else:
                        print("[Webull Unofficial] ⚠️ Env token expired/invalid (is_logged_in=False) — options fallback quotes are 15-min DELAYED guest data.")
                    _unofficial_client = wb
                    _unofficial_initialized = True
                    return _unofficial_client
                except Exception as e:
                    print(f"[Webull Unofficial] Environment token load failed: {e}")

            # 2. Try to load cached token from local credentials file
            if os.path.exists(credentials_file):
                try:
                    with open(credentials_file, "rb") as f:
                        token_data = pickle.load(f)

                    wb._access_token = token_data.get("accessToken")
                    wb._refresh_token = token_data.get("refreshToken")
                    wb._token_expire = token_data.get("tokenExpireTime")
                    wb._uuid = token_data.get("uuid")

                    did_bin_file = os.path.join(token_path, "did.bin")
                    if os.path.exists(did_bin_file):
                        try:
                            with open(did_bin_file, "rb") as f:
                                wb._did = pickle.load(f)
                        except Exception:
                            pass

                    if wb._access_token:
                        wb._account_id = token_data.get("account_id")
                        try:
                            _unofficial_authenticated = bool(wb.is_logged_in())
                        except Exception:
                            _unofficial_authenticated = False

                        if _unofficial_authenticated:
                            print("[Webull Unofficial] Verified cached session from webull_credentials.json.")
                        else:
                            print("[Webull Unofficial] ⚠️ Cached credentials expired — running as delayed guest session.")
                        _unofficial_client = wb
                        _unofficial_initialized = True
                        return _unofficial_client
                except Exception as e:
                    print(f"[Webull Unofficial] Cached token load failed: {e}")

            # 3. Perform Login if cached token fails/does not exist
            email = os.getenv("WEBULL_EMAIL")
            password = os.getenv("WEBULL_PASSWORD")
            is_interactive = sys.stdin and sys.stdin.isatty()
            if not is_interactive or not (email and password):
                print("[Webull Unofficial] Skipping fresh login in non-interactive environment.")
                _unofficial_client = None
                _unofficial_authenticated = False
                _unofficial_initialized = True
                return None

            print(f"[Webull Unofficial] Logging in as '{email}'...")
            res = wb.login(email, password, save_token=True, token_path=token_path)
            if "accessToken" in res:
                print("[Webull Unofficial] Login successful!")
                _unofficial_client = wb
                _unofficial_authenticated = True
            else:
                print(f"[Webull Unofficial] Login failed: {res}")
                _unofficial_client = None
                _unofficial_authenticated = False
        except Exception as e:
            print(f"[Webull Unofficial] Initialization error: {e}")
            _unofficial_client = None
            _unofficial_authenticated = False

        _unofficial_initialized = True
        return _unofficial_client


# ── Webull OpenAPI Lazy Client Loader (Option A — Primary) ────────────

_openapi_lock = threading.Lock()
_webull_client = None
_webull_initialized = False


def get_webull_client():
    """Retrieve and initialize the Webull OpenAPI DataClient dynamically."""
    global _webull_client, _webull_initialized
    if _webull_initialized:
        return _webull_client

    with _openapi_lock:
        if _webull_initialized:
            return _webull_client

        app_key = (os.getenv("WEBULL_APP_KEY") or "").strip()
        app_secret = (os.getenv("WEBULL_APP_SECRET") or "").strip()
        has_credentials = bool(
            app_key and app_key != "your_app_key_here"
            and app_secret and app_secret != "your_app_secret_here"
        )

        if not has_credentials:
            print("[Webull OpenAPI] Credentials not configured — OpenAPI client disabled.")
            _webull_client = None
            _webull_initialized = True
            return None

        region_str = (os.getenv("WEBULL_REGION") or "us").strip().lower()
        try:
            # Ensure unofficial `webull` package does not break OpenAPI SDK's `__version__` check
            import webull
            if not hasattr(webull, "__version__"):
                webull.__version__ = "3.0.4"

            from webull.core.client import ApiClient
            from webull.core.common.region import Region
            from webull.data.data_client import DataClient

            region = Region.US.value
            for r in Region:
                if r.value == region_str:
                    region = r.value
                    break

            print(f"[Webull OpenAPI] Initializing DataClient for region '{region}'...")
            api_client = ApiClient(app_key, app_secret, region)
            _webull_client = DataClient(api_client)
            print("[Webull OpenAPI] DataClient initialized successfully.")
        except Exception as e:
            print(f"[Webull OpenAPI] Error initializing DataClient: {e}")
            _webull_client = None

        _webull_initialized = True
        return _webull_client


def get_market_data_status():
    """Summarize live market data readiness across Webull OpenAPI and Unofficial fallback."""
    app_key = (os.getenv("WEBULL_APP_KEY") or "").strip()
    app_secret = (os.getenv("WEBULL_APP_SECRET") or "").strip()
    openapi_configured = bool(
        app_key and app_key != "your_app_key_here"
        and app_secret and app_secret != "your_app_secret_here"
    )
    wb_api = get_webull_client() if openapi_configured else None
    wb_un = get_unofficial_client()

    with _openapi_sub_lock:
        now = time.time()
        stock_blocked = _openapi_sub_blocked_until.get("US_STOCK", 0.0) > now
        option_blocked = _openapi_sub_blocked_until.get("US_OPTION", 0.0) > now
        stock_err = _openapi_sub_errors.get("US_STOCK")
        option_err = _openapi_sub_errors.get("US_OPTION")

    openapi_opt_ready = bool(wb_api is not None and not option_blocked)
    return {
        "openapi_configured": openapi_configured,
        "openapi_client_ready": wb_api is not None,
        "openapi_stock_blocked": stock_blocked,
        "openapi_stock_error": stock_err,
        "openapi_option_blocked": option_blocked,
        "openapi_option_error": option_err,
        "unofficial_configured": wb_un is not None,
        "unofficial_authenticated": _unofficial_authenticated,
        "realtime_options_ready": openapi_opt_ready or _unofficial_authenticated,
    }





# ── Webull OpenAPI Fetcher (Option A) ───────────────────────────────────

def _fetch_webull_openapi_one(ticker, days=180, interval="1d", includePrePost="false"):
    """Query historical candlestick data from Webull OpenAPI."""
    if _is_openapi_scope_blocked("US_STOCK"):
        return None

    client = get_webull_client()
    if not client:
        return None

    try:
        from webull.data.common.category import Category
        from webull.data.common.timespan import Timespan

        interval_map = {
            "1d": Timespan.D,
            "5m": Timespan.M5,
            "15m": Timespan.M15,
            "30m": Timespan.M30,
            "60m": Timespan.M60
        }
        timespan = interval_map.get(interval, Timespan.D)
        count = str(min(1200, days)) if interval == "1d" else "600"

        resp = client.market_data.get_history_bar(
            symbol=ticker,
            category=Category.US_STOCK.name,
            timespan=timespan.name,
            count=count
        )

        if resp.status_code != 200:
            is_not_sub, _, err_msg = _extract_openapi_error(resp)
            if is_not_sub:
                _mark_openapi_scope_blocked("US_STOCK", err_msg)
            return None

        _clear_openapi_scope_block("US_STOCK")
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
                    "Open": _safe_float(o),
                    "High": _safe_float(h),
                    "Low": _safe_float(l),
                    "Close": float(c),
                    "Volume": _safe_int(v, 0)
                })

        if not records:
            return None

        df = pd.DataFrame(records)
        if pd.api.types.is_numeric_dtype(df["Date"]):
            sample_t = float(df["Date"].iloc[0])
            unit = "ms" if sample_t > 1e11 else "s"
            df["Date"] = pd.to_datetime(df["Date"], unit=unit, utc=True)
        else:
            df["Date"] = pd.to_datetime(df["Date"], utc=True)
        df = df.set_index("Date")
        df.index = df.index.tz_convert("America/New_York")
        df = df.sort_index()
        df["Volume"] = df["Volume"].fillna(0).astype(np.int64)

        pre_close = float(df["Close"].iloc[-2]) if len(df) > 1 else float(df["Close"].iloc[0])
        high_52w = float(df["High"].max())
        low_52w = float(df["Low"].min())

        try:
            snapshot_resp = client.market_data.get_snapshot([ticker], Category.US_STOCK.name)
            if snapshot_resp.status_code == 200:
                snap_data = snapshot_resp.json()
                snap_list = snap_data if isinstance(snap_data, list) else snap_data.get("data", [])
                if snap_list:
                    snap = snap_list[0]
                    pre_close = _safe_float(snap.get("pre_close"), pre_close)
        except Exception:
            pass

        df.attrs["fiftyTwoWeekHigh"] = high_52w
        df.attrs["fiftyTwoWeekLow"] = low_52w
        df.attrs["previousClose"] = pre_close
        df.attrs["quoteSource"] = "Webull_OpenAPI"
        df.attrs["isRealtime"] = True

        return df

    except Exception as e:
        is_not_sub, _, err_msg = _extract_openapi_error(e)
        if is_not_sub:
            _mark_openapi_scope_blocked("US_STOCK", err_msg)
        else:
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
                    p_price = quote.get("pPrice")
                    if p_price and float(p_price) > 0 and includePrePost == "true":
                        ext_price = float(p_price)
                        df.loc[df.index[-1], "Close"] = ext_price
                        if ext_price > df.loc[df.index[-1], "High"]:
                            df.loc[df.index[-1], "High"] = ext_price
                        if ext_price < df.loc[df.index[-1], "Low"]:
                            df.loc[df.index[-1], "Low"] = ext_price
                        print(f"  🌙 [Extended Hours] {ticker} live price updated to ${ext_price:.2f}")
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






# ── Unified Single Ticker Fetcher (Resilient Ordering) ────────────────

# ── Unified Single Ticker Fetcher (Webull Primary) ────────────────

def fetch_one(ticker, days=180, interval="1d", includePrePost="false", skip_webull=False):
    """
    Fetch OHLCV data for one ticker using Webull API.
    1. Try Official Webull OpenAPI developer credentials (Option A — persistent real-time tokens).
    2. Fall back to Unofficial Webull SDK (Option B) if OpenAPI US_STOCK is unsubscribed or unavailable.
    """
    global _webull_unofficial_failures, _webull_openapi_failures

    # 1. Try Option A (Official OpenAPI)
    if not skip_webull and not _is_openapi_scope_blocked("US_STOCK") and _webull_openapi_failures < _WEBULL_MAX_FAILURES:
        try:
            wb_openapi = get_webull_client()
            if wb_openapi:
                df = _fetch_webull_openapi_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
                if df is not None:
                    _webull_openapi_failures = 0
                    return df
                elif not _is_openapi_scope_blocked("US_STOCK"):
                    _webull_openapi_failures += 1
        except Exception as e:
            print(f"[Webull OpenAPI] Error fetching {ticker}: {e}")
            _webull_openapi_failures += 1
            if _webull_openapi_failures >= _WEBULL_MAX_FAILURES:
                print(f"[Circuit Breaker] Webull OpenAPI failed {_WEBULL_MAX_FAILURES}x consecutively — skipping for rest of scan")

    # 2. Fallback to Option B (Unofficial Session — works for stock OHLCV bars even on guest/delayed sessions)
    if not skip_webull and _webull_unofficial_failures < _WEBULL_MAX_FAILURES:
        try:
            wb_un = get_unofficial_client()
            if wb_un:
                df = _fetch_webull_unofficial_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
                if df is not None:
                    _webull_unofficial_failures = 0
                    return df
                else:
                    _webull_unofficial_failures += 1
        except Exception as e:
            print(f"[Webull Unofficial] Error fetching {ticker}: {e}")
            _webull_unofficial_failures += 1
            if _webull_unofficial_failures >= _WEBULL_MAX_FAILURES:
                print(f"[Circuit Breaker] Webull Unofficial failed {_WEBULL_MAX_FAILURES}x consecutively — skipping for rest of scan")

    # 3. Fallback to Yahoo Finance public chart API (disabled by default per instructions)
    disable_yahoo = os.getenv("DISABLE_YAHOO", "true").lower() in ("true", "1", "yes")
    if not disable_yahoo:
        try:
            df = _fetch_yahoo_fallback_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
            if df is not None and len(df) >= 20:
                return df
        except Exception as e:
            print(f"[Yahoo Fallback] Error fetching {ticker}: {e}")

    return None


# ── Yahoo Finance Public Chart API Fallback ───────────────────────────

def _fetch_yahoo_fallback_one(ticker, days=180, interval="1d", includePrePost="false"):
    """
    Fallback data fetcher using yfinance package.
    Zero authentication required, 100% reliable for standard OHLCV bars on cloud platforms.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # Map intervals and periods correctly for all modes
        interval_map = {"1d": ("1d", "1y"), "5m": ("5m", "5d"), "15m": ("15m", "1mo"), "30m": ("30m", "1mo"), "60m": ("60m", "1mo")}
        yf_intv, period = interval_map.get(interval, ("1d", "1y"))
        prepost_bool = True if (interval != "1d" or includePrePost == "true") else False
        df = t.history(period=period, interval=yf_intv, prepost=prepost_bool)
        if df is None or df.empty or len(df) < 20:
            return None
            
        df = df.reset_index()
        col_map = {}
        for c in df.columns:
            cl = str(c).lower()
            if "date" in cl or "time" in cl:
                col_map[c] = "Date"
            elif "open" in cl:
                col_map[c] = "Open"
            elif "high" in cl:
                col_map[c] = "High"
            elif "low" in cl:
                col_map[c] = "Low"
            elif "close" in cl:
                col_map[c] = "Close"
            elif "vol" in cl:
                col_map[c] = "Volume"
                
        df = df.rename(columns=col_map)
        df["Date"] = pd.to_datetime(df["Date"], utc=True)
        df = df.set_index("Date")
        df.index = df.index.tz_convert("America/New_York")
        df = df.sort_index()
        
        req_cols = ["Open", "High", "Low", "Close", "Volume"]
        for c in req_cols:
            if c not in df.columns:
                return None
                
        return df[req_cols]
    except Exception as e:
        print(f"[Yahoo Fallback Error] {ticker}: {e}")
        return None


# ── Options Chain Download (Webull Unofficial) ───────────────

def _fetch_yahoo_options_chain(ticker):
    """Fallback options chain fetcher using yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        if not t.options:
            return None
        
        expirations = []
        for exp_str in t.options:
            try:
                expirations.append(_ny_date_to_ts(exp_str))
            except Exception:
                continue

        if not expirations:
            return None

        underlyingPrice = None
        try:
            fast_info = getattr(t, "fast_info", None)
            if fast_info and hasattr(fast_info, "last_price"):
                underlyingPrice = float(fast_info.last_price)
            if not underlyingPrice:
                hist = t.history(period="1d")
                if not hist.empty:
                    underlyingPrice = float(hist["Close"].iloc[-1])
        except Exception:
            pass

        print(f"[Yahoo Finance Fallback] Fetched options chain for {ticker} (got {len(expirations)} expirations)")
        return {
            "ticker": ticker,
            "expirations": expirations,
            "underlyingPrice": underlyingPrice,
            "firstChain": None,
            "allChains": {}
        }
    except Exception as e:
        print(f"[Yahoo Options Fallback] Error fetching options chain for {ticker}: {e}")
        return None


def _fetch_yahoo_options_for_expiration(ticker, expiration_ts):
    """Fetch options for a specific expiration timestamp from Yahoo Finance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        if not t.options:
            return None

        date_str = _ts_to_ny_date_str(expiration_ts)
        target_exp = None
        if date_str in t.options:
            target_exp = date_str
        else:
            t_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            min_diff = 999
            for o_exp in t.options:
                try:
                    o_dt = datetime.strptime(o_exp, "%Y-%m-%d").date()
                    diff = abs((o_dt - t_dt).days)
                    if diff < min_diff and diff <= 3:
                        min_diff = diff
                        target_exp = o_exp
                except Exception:
                    continue

        if not target_exp:
            return None

        chain = t.option_chain(target_exp)
        calls = []
        puts = []

        # Get underlying price for delta approximation if delta missing
        und_price = None
        try:
            hist = t.history(period="1d")
            if not hist.empty:
                und_price = float(hist["Close"].iloc[-1])
        except Exception:
            pass

        for df_side, side_name in [(chain.calls, "calls"), (chain.puts, "puts")]:
            if df_side is None or df_side.empty:
                continue
            for _, item in df_side.iterrows():
                strike = float(item.get("strike", 0))
                bid_val = float(item.get("bid", 0)) if pd.notnull(item.get("bid")) else None
                ask_val = float(item.get("ask", 0)) if pd.notnull(item.get("ask")) else None
                last_p = float(item.get("lastPrice", 0)) if pd.notnull(item.get("lastPrice")) else None

                if (bid_val is None or bid_val <= 0) and (ask_val is None or ask_val <= 0) and last_p:
                    bid_val = last_p
                    ask_val = last_p

                # Estimate ATM delta if delta not in yfinance
                est_delta = 0.50
                if und_price and und_price > 0:
                    moneyness = (und_price - strike) / und_price
                    if side_name == "calls":
                        est_delta = max(0.05, min(0.95, 0.50 + moneyness * 3.0))
                    else:
                        est_delta = max(0.05, min(0.95, 0.50 - moneyness * 3.0))

                c_data = {
                    "contractSymbol": item.get("contractSymbol"),
                    "strike": strike,
                    "tickerId": None,
                    "bid": bid_val,
                    "ask": ask_val,
                    "close": last_p,
                    "volume": int(item.get("volume", 0)) if pd.notnull(item.get("volume")) else 0,
                    "openInterest": int(item.get("openInterest", 0)) if pd.notnull(item.get("openInterest")) else 0,
                    "impliedVolatility": float(item.get("impliedVolatility", 0)) if pd.notnull(item.get("impliedVolatility")) else None,
                    "delta": round(est_delta, 2),
                    "quote_source": "YahooFinance",
                    "is_realtime": False,
                }
                if side_name == "calls":
                    calls.append(c_data)
                else:
                    puts.append(c_data)

        print(f"[Yahoo Finance Fallback] Fetched {len(calls)} calls & {len(puts)} puts for {ticker} at {target_exp}")
        return {"calls": calls, "puts": puts, "source": "YahooFinance", "is_realtime": False}
    except Exception as e:
        print(f"[Yahoo Options Fallback] Error fetching options at exp {expiration_ts} for {ticker}: {e}")
        return None


def _unofficial_source_meta():
    """Return (source_label, is_realtime) for unofficial Webull options endpoints."""
    is_rt = is_unofficial_authenticated()
    return ("Webull_Authenticated" if is_rt else "Webull_Delayed_Guest"), is_rt


def _extract_bid_ask_from_unofficial(item, price_key):
    """Extract top-of-book bid or ask price from an unofficial Webull option item."""
    list_key = "bidList" if price_key == "bid" else "askList"
    book = item.get(list_key)
    if isinstance(book, list) and book:
        return _safe_float(book[0].get("price"))
    return _safe_float(item.get(price_key))


def _parse_unofficial_option_item(item, src_label, is_rt):
    """Normalize an unofficial Webull option JSON item to internal contract schema."""
    return {
        "contractSymbol": item.get("symbol"),
        "strike": _safe_float(item.get("strikePrice"), 0.0),
        "tickerId": item.get("tickerId"),
        "bid": _extract_bid_ask_from_unofficial(item, "bid"),
        "ask": _extract_bid_ask_from_unofficial(item, "ask"),
        "close": _safe_float(item.get("close")),
        "volume": _safe_int(item.get("volume"), 0),
        "openInterest": _safe_int(item.get("openInterest"), 0),
        "impliedVolatility": _safe_float(item.get("impVol")),
        "delta": _safe_float(item.get("delta")),
        "quote_source": src_label,
        "is_realtime": is_rt,
    }


def _openapi_get_spot_price(client, ticker):
    """Resolve current underlying price via OpenAPI stock snapshot (if subscribed) or unofficial quote."""
    if client and not _is_openapi_scope_blocked("US_STOCK"):
        try:
            from webull.data.common.category import Category
            resp = client.market_data.get_snapshot([ticker], Category.US_STOCK.name)
            if resp.status_code == 200:
                _clear_openapi_scope_block("US_STOCK")
                snap_data = resp.json()
                snap_list = snap_data if isinstance(snap_data, list) else snap_data.get("data", [])
                if snap_list:
                    price = _safe_float(snap_list[0].get("price") or snap_list[0].get("close"))
                    if price and price > 0:
                        return price
            else:
                is_not_sub, _, err_msg = _extract_openapi_error(resp)
                if is_not_sub:
                    _mark_openapi_scope_blocked("US_STOCK", err_msg)
        except Exception as e:
            is_not_sub, _, err_msg = _extract_openapi_error(e)
            if is_not_sub:
                _mark_openapi_scope_blocked("US_STOCK", err_msg)

    wb_un = get_unofficial_client()
    if wb_un:
        try:
            q = wb_un.get_quote(stock=ticker)
            if isinstance(q, dict):
                return _safe_float(q.get("close") or q.get("pPrice") or q.get("preClose"))
        except Exception:
            pass
    return None


def _openapi_list_contracts(client, ticker, start_date, end_date, option_type=None,
                            strike_gte=None, strike_lte=None, max_pages=5):
    """Fetch standard option contracts from Webull OpenAPI with pagination."""
    contracts = []
    pagination_key = None
    for _ in range(max_pages):
        resp = client.instrument.list_option_contracts(
            category="US_OPTION",
            underlying_symbols=ticker,
            status="LISTING",
            start_date=start_date,
            end_date=end_date,
            option_type=option_type,
            strike_price_gte=strike_gte,
            strike_price_lte=strike_lte,
            pagination_key=pagination_key,
        )
        if resp.status_code != 200:
            break
        payload = resp.json() or {}
        batch = payload.get("data", [])
        if not batch:
            break
        contracts.extend(batch)
        pagination_key = payload.get("pagination_key")
        if not pagination_key:
            break

    standard = [
        c for c in contracts
        if (not c.get("def_type") or c.get("def_type") == "STANDARD")
        and _safe_float(c.get("multiplier"), 100.0) == 100.0
    ]
    return standard if standard else contracts


def _openapi_get_option_snapshots(client, symbols):
    """Batch-query OpenAPI US_OPTION snapshots (up to 20 symbols per request)."""
    if not symbols or _is_openapi_scope_blocked("US_OPTION"):
        return None

    snapshots = {}
    for batch in _chunks(list(dict.fromkeys(symbols)), 20):
        try:
            resp = client.option_market_data.get_option_snapshot(batch, category="US_OPTION")
            if resp.status_code != 200:
                is_not_sub, _, err_msg = _extract_openapi_error(resp)
                if is_not_sub:
                    _mark_openapi_scope_blocked("US_OPTION", err_msg)
                return None
            _clear_openapi_scope_block("US_OPTION")
            raw = resp.json()
            items = raw if isinstance(raw, list) else raw.get("data", [])
            for snap in items:
                sym = snap.get("symbol")
                if sym:
                    snapshots[sym] = snap
        except Exception as e:
            is_not_sub, _, err_msg = _extract_openapi_error(e)
            if is_not_sub:
                _mark_openapi_scope_blocked("US_OPTION", err_msg)
            else:
                print(f"[Webull OpenAPI] Option snapshot batch error: {e}")
            return None
    return snapshots


def _parse_openapi_snapshot_prices(snap):
    """Extract normalized bid, ask, last/close, mid, volume, oi, delta, iv from an OpenAPI option snapshot."""
    bid = _safe_float(snap.get("bid") if "bid" in snap else snap.get("bid_price"))
    ask = _safe_float(snap.get("ask") if "ask" in snap else snap.get("ask_price"))
    last_p = _safe_float(snap.get("price") if "price" in snap else snap.get("last_price"))
    close_p = last_p if last_p is not None else _safe_float(snap.get("pre_close"))
    iv_val = _safe_float(snap.get("implied_volatility") if "implied_volatility" in snap else snap.get("imp_vol"))
    if bid is not None and bid > 0 and ask is not None and ask > 0:
        mid = round((bid + ask) / 2.0, 2)
    else:
        mid = close_p
    return {
        "bid": bid,
        "ask": ask,
        "close": close_p,
        "mid": mid,
        "volume": _safe_int(snap.get("volume"), 0),
        "openInterest": _safe_int(snap.get("open_interest"), 0),
        "impliedVolatility": iv_val,
        "delta": _safe_float(snap.get("delta")),
        "quoteTime": snap.get("quote_time"),
    }


def _select_contracts_near_spot(contracts, spot_price, max_per_side=60):
    """Balance and cap option contracts closest to spot_price so snapshot batching stays fast."""
    if not spot_price or spot_price <= 0 or len(contracts) <= max_per_side * 2:
        return contracts
    calls = [c for c in contracts if str(c.get("option_type", "")).upper() == "CALL"]
    puts = [c for c in contracts if str(c.get("option_type", "")).upper() == "PUT"]
    calls.sort(key=lambda c: abs(_safe_float(c.get("strike_price"), 0.0) - spot_price))
    puts.sort(key=lambda c: abs(_safe_float(c.get("strike_price"), 0.0) - spot_price))
    return calls[:max_per_side] + puts[:max_per_side]


def _fetch_openapi_options_for_expiration(ticker, exp_date_str, spot_price=None):
    """Fetch live Webull OpenAPI option chain (contracts + snapshots) for one expiration date."""
    if _is_openapi_scope_blocked("US_OPTION"):
        return None
    client = get_webull_client()
    if not client:
        return None

    try:
        if spot_price is None:
            spot_price = _openapi_get_spot_price(client, ticker)

        s_min = str(round(spot_price * 0.75, 2)) if (spot_price and spot_price > 0) else None
        s_max = str(round(spot_price * 1.25, 2)) if (spot_price and spot_price > 0) else None

        contracts = _openapi_list_contracts(
            client, ticker, start_date=exp_date_str, end_date=exp_date_str,
            strike_gte=s_min, strike_lte=s_max, max_pages=3
        )
        if not contracts and (s_min or s_max):
            contracts = _openapi_list_contracts(
                client, ticker, start_date=exp_date_str, end_date=exp_date_str, max_pages=3
            )
        if not contracts:
            return None

        selected = _select_contracts_near_spot(contracts, spot_price, max_per_side=60)
        symbols = [c["symbol"] for c in selected if c.get("symbol")]
        snapshots = _openapi_get_option_snapshots(client, symbols)
        if snapshots is None:
            return None

        calls, puts = [], []
        for c in selected:
            snap = snapshots.get(c.get("symbol"))
            if not snap:
                continue
            parsed = _parse_openapi_snapshot_prices(snap)
            entry = {
                "contractSymbol": c.get("symbol") or snap.get("symbol"),
                "strike": _safe_float(c.get("strike_price"), 0.0),
                "tickerId": str(c.get("instrument_id") or snap.get("instrument_id") or ""),
                **parsed,
                "quote_source": "Webull_OpenAPI",
                "is_realtime": True,
            }
            opt_type = str(c.get("option_type", "")).upper()
            if opt_type == "CALL":
                calls.append(entry)
            elif opt_type == "PUT":
                puts.append(entry)

        if calls or puts:
            return {
                "calls": calls,
                "puts": puts,
                "source": "Webull_OpenAPI",
                "is_realtime": True,
            }
        return None
    except Exception as e:
        is_not_sub, _, err_msg = _extract_openapi_error(e)
        if is_not_sub:
            _mark_openapi_scope_blocked("US_OPTION", err_msg)
        else:
            print(f"[Webull OpenAPI] Error fetching options for {ticker} at {exp_date_str}: {e}")
        return None


def _fetch_openapi_options_chain(ticker):
    """Fetch option expirations + front chain from Webull OpenAPI."""
    if _is_openapi_scope_blocked("US_OPTION"):
        return None
    client = get_webull_client()
    if not client:
        return None

    try:
        spot_price = _openapi_get_spot_price(client, ticker)
        today_ny = datetime.now(_get_ny_tz()).date()
        start_str = today_ny.strftime("%Y-%m-%d")
        end_str = (today_ny + timedelta(days=65)).strftime("%Y-%m-%d")

        s_min = str(round(spot_price * 0.85, 2)) if (spot_price and spot_price > 0) else None
        s_max = str(round(spot_price * 1.15, 2)) if (spot_price and spot_price > 0) else None

        contracts = _openapi_list_contracts(
            client, ticker, start_date=start_str, end_date=end_str,
            strike_gte=s_min, strike_lte=s_max, max_pages=3
        )
        if not contracts and (s_min or s_max):
            contracts = _openapi_list_contracts(
                client, ticker, start_date=start_str, end_date=end_str, max_pages=2
            )
        if not contracts:
            return None

        exp_date_strs = sorted({c.get("expiration_date") for c in contracts if c.get("expiration_date")})
        if not exp_date_strs:
            return None

        expirations = [_ny_date_to_ts(d) for d in exp_date_strs]

        # Probe front expiration snapshot (verifies live US_OPTION entitlement and provides firstChain)
        first_chain = _fetch_openapi_options_for_expiration(ticker, exp_date_strs[0], spot_price=spot_price)
        if first_chain is None:
            return None

        print(f"[Webull OpenAPI] Option chain loaded for {ticker} ({len(expirations)} expirations, live=True)")
        return {
            "ticker": ticker,
            "expirations": expirations,
            "underlyingPrice": spot_price,
            "firstChain": first_chain,
            "allChains": {expirations[0]: first_chain},
            "source": "Webull_OpenAPI",
            "is_realtime": True,
        }
    except Exception as e:
        is_not_sub, _, err_msg = _extract_openapi_error(e)
        if is_not_sub:
            _mark_openapi_scope_blocked("US_OPTION", err_msg)
        else:
            print(f"[Webull OpenAPI] Error fetching options chain for {ticker}: {e}")
        return None


def _fetch_openapi_option_live_quote(ticker, strike, option_type, exp_date_str, option_symbol=None):
    """Fetch single-contract live OPRA quote from Webull OpenAPI."""
    if _is_openapi_scope_blocked("US_OPTION"):
        return None
    client = get_webull_client()
    if not client:
        return None

    try:
        contract = None
        clean_sym = (option_symbol or "").strip()
        is_occ_sym = bool(clean_sym and not clean_sym.startswith("SIM_") and len(clean_sym) >= 12)

        snapshots = None
        if is_occ_sym:
            snapshots = _openapi_get_option_snapshots(client, [clean_sym])
            if snapshots is None:
                return None

        if not snapshots or clean_sym not in snapshots:
            target_strike = float(strike)
            contracts = _openapi_list_contracts(
                client,
                ticker,
                start_date=exp_date_str,
                end_date=exp_date_str,
                option_type=str(option_type).upper(),
                strike_gte=str(round(target_strike - 0.05, 2)),
                strike_lte=str(round(target_strike + 0.05, 2)),
                max_pages=1,
            )
            if not contracts:
                return None
            contracts.sort(key=lambda c: abs(_safe_float(c.get("strike_price"), 0.0) - target_strike))
            contract = contracts[0]
            clean_sym = contract.get("symbol")
            if not clean_sym:
                return None
            snapshots = _openapi_get_option_snapshots(client, [clean_sym])
            if not snapshots or clean_sym not in snapshots:
                return None

        snap = snapshots[clean_sym]
        parsed = _parse_openapi_snapshot_prices(snap)
        bid, ask, mid = parsed["bid"], parsed["ask"], parsed["mid"]

        if (bid is not None and bid > 0) or (ask is not None and ask > 0) or (mid is not None and mid > 0):
            return {
                "symbol": clean_sym,
                "tickerId": str((contract or {}).get("instrument_id") or snap.get("instrument_id") or ""),
                **parsed,
                "source": "Webull_OpenAPI",
                "is_realtime": True,
            }
        return None
    except Exception as e:
        is_not_sub, _, err_msg = _extract_openapi_error(e)
        if is_not_sub:
            _mark_openapi_scope_blocked("US_OPTION", err_msg)
        else:
            print(f"[Webull OpenAPI] Error fetching live quote for {ticker} ${strike} {option_type}: {e}")
        return None


def fetch_options_chain(ticker):
    """
    Fetch options chain prioritizing Webull OpenAPI (real-time US_OPTION),
    then Webull Unofficial (authenticated real-time or delayed guest), then Yahoo Finance.
    """
    openapi_chain = _fetch_openapi_options_chain(ticker)
    if openapi_chain:
        return openapi_chain

    wb_un = get_unofficial_client()
    if wb_un:
        try:
            src_label, is_rt = _unofficial_source_meta()
            print(f"[Webull Unofficial] Fetching options chain for {ticker} ({src_label})...")

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
                        ts = _ny_date_to_ts(date_str)
                        expirations.append(ts)
                    except Exception:
                        continue

                    calls, puts = [], []
                    for item in entry.get('data', []):
                        direction = item.get("direction")
                        contract_data = _parse_unofficial_option_item(item, src_label, is_rt)
                        if direction == "call":
                            calls.append(contract_data)
                        elif direction == "put":
                            puts.append(contract_data)

                    chain = {"calls": calls, "puts": puts, "source": src_label, "is_realtime": is_rt}
                    all_chains[ts] = chain
                    if not first_chain_data:
                        first_chain_data = chain

                if expirations:
                    underlyingPrice = None
                    try:
                        quote = wb_un.get_quote(stock=ticker)
                        underlyingPrice = _safe_float(quote.get("close"))
                    except Exception:
                        pass

                    print(f"[Webull Unofficial] Option chain parsed for {ticker} ({len(expirations)} expirations, source={src_label})")
                    return {
                        "ticker": ticker,
                        "expirations": expirations,
                        "underlyingPrice": underlyingPrice,
                        "firstChain": first_chain_data,
                        "allChains": all_chains,
                        "source": src_label,
                        "is_realtime": is_rt,
                    }
        except Exception as e:
            print(f"[Webull Unofficial] Error fetching option chain for {ticker}: {e}")

    yf_chain = _fetch_yahoo_options_chain(ticker)
    if yf_chain:
        yf_chain["source"] = "YahooFinance"
        yf_chain["is_realtime"] = False
    return yf_chain


def fetch_options_for_expiration(ticker, expiration_ts, spot_price=None):
    """Fetch options chain for a specific expiration timestamp (OpenAPI primary -> Unofficial -> Yahoo)."""
    date_str = _ts_to_ny_date_str(expiration_ts)

    openapi_exp = _fetch_openapi_options_for_expiration(ticker, date_str, spot_price=spot_price)
    if openapi_exp:
        return openapi_exp

    wb_un = get_unofficial_client()
    if wb_un:
        try:
            src_label, is_rt = _unofficial_source_meta()
            print(f"[Webull Unofficial] Fetching options chain for {ticker} at {date_str} ({src_label})...")

            headers = wb_un.build_req_headers()
            tid = get_stock_ticker_id(wb_un, ticker)
            data = {
                'count': -1,
                'direction': 'all',
                'tickerId': tid,
                'expireDate': date_str,
                'unSymbol': ticker
            }
            res = requests.post(wb_un._urls.options_exp_dat_new(), json=data, headers=headers, timeout=wb_un.timeout)
            if res.status_code == 200:
                res_json = res.json()
                calls, puts = [], []
                for entry in res_json.get('expireDateList', []):
                    if entry.get('from', {}).get('date') == date_str:
                        for item in entry.get('data', []):
                            direction = item.get('direction')
                            c_data = _parse_unofficial_option_item(item, src_label, is_rt)
                            if direction == "call":
                                calls.append(c_data)
                            elif direction == "put":
                                puts.append(c_data)
                        break
                if calls or puts:
                    return {"calls": calls, "puts": puts, "source": src_label, "is_realtime": is_rt}
        except Exception as e:
            print(f"[Webull Unofficial] Error fetching options at expiration timestamp {expiration_ts}: {e}")

    # Fallback to Yahoo Finance options chain for expiration
    return _fetch_yahoo_options_for_expiration(ticker, expiration_ts)


def _fetch_yahoo_option_live_quote(ticker, strike, option_type, exp_date_str):
    """Direct live option quote fetch from Yahoo Finance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        if not t.options:
            return None

        target_exp = None
        if exp_date_str in t.options:
            target_exp = exp_date_str
        else:
            t_dt = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
            min_diff = 999
            for o_exp in t.options:
                try:
                    o_dt = datetime.strptime(o_exp, "%Y-%m-%d").date()
                    diff = abs((o_dt - t_dt).days)
                    if diff < min_diff and diff <= 3:
                        min_diff = diff
                        target_exp = o_exp
                except Exception:
                    continue

        if not target_exp:
            return None

        chain = t.option_chain(target_exp)
        df_side = chain.calls if str(option_type).lower() in ("call", "calls") else chain.puts
        if df_side.empty:
            return None

        target_strike = float(strike)
        matches = df_side[abs(df_side["strike"] - target_strike) < 0.05]
        if matches.empty:
            diffs = (df_side["strike"] - target_strike).abs()
            closest_idx = diffs.idxmin()
            if diffs.loc[closest_idx] <= max(1.0, target_strike * 0.02):
                matches = df_side.loc[[closest_idx]]
            else:
                return None

        row = matches.iloc[0]
        bid = float(row.get("bid", 0)) if pd.notnull(row.get("bid")) else 0.0
        ask = float(row.get("ask", 0)) if pd.notnull(row.get("ask")) else 0.0
        last_p = float(row.get("lastPrice", 0)) if pd.notnull(row.get("lastPrice")) else 0.0

        if bid <= 0 and ask <= 0:
            bid = last_p
            ask = last_p

        mid = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else last_p
        vol = int(row.get("volume", 0)) if pd.notnull(row.get("volume")) else 0
        oi = int(row.get("openInterest", 0)) if pd.notnull(row.get("openInterest")) else 0

        return {
            "symbol": row.get("contractSymbol"),
            "tickerId": None,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "close": last_p,
            "volume": vol,
            "openInterest": oi,
            "delta": None,
            "source": "YahooFinance",
            "is_realtime": False,
        }
    except Exception as e:
        print(f"[Yahoo Options Fallback] Error fetching live quote for {ticker} ${strike} {option_type}: {e}")
        return None


def get_option_live_quote(ticker, strike, option_type, exp_date_str, option_symbol=None):
    """
    Fetch the exact bid, ask, mid and details for a specific option contract.
    Priority: Webull OpenAPI (real-time US_OPTION) -> Webull Unofficial -> Yahoo Finance.
    exp_date_str format: 'YYYY-MM-DD'
    """
    openapi_q = _fetch_openapi_option_live_quote(
        ticker, strike, option_type, exp_date_str, option_symbol=option_symbol
    )
    if openapi_q:
        return openapi_q

    wb_un = get_unofficial_client()
    if wb_un:
        try:
            src_label, is_rt = _unofficial_source_meta()
            headers = wb_un.build_req_headers()
            tid = get_stock_ticker_id(wb_un, ticker)
            data = {
                'count': -1,
                'direction': 'all',
                'tickerId': tid,
                'expireDate': exp_date_str,
                'unSymbol': ticker
            }
            res = requests.post(wb_un._urls.options_exp_dat_new(), json=data, headers=headers, timeout=wb_un.timeout)
            if res.status_code == 200:
                for entry in res.json().get('expireDateList', []):
                    if entry.get('from', {}).get('date') == exp_date_str:
                        for item in entry.get('data', []):
                            item_strike = float(item.get('strikePrice', 0))
                            item_dir = item.get('direction', '').lower()
                            if abs(item_strike - float(strike)) < 0.01 and item_dir == option_type.lower():
                                bid = _extract_bid_ask_from_unofficial(item, 'bid')
                                ask = _extract_bid_ask_from_unofficial(item, 'ask')
                                close_p = _safe_float(item.get('close'))
                                mid = round((bid + ask) / 2.0, 2) if (bid is not None and ask is not None) else close_p
                                if (bid is not None and bid > 0) or (ask is not None and ask > 0):
                                    return {
                                        'symbol': item.get('symbol'),
                                        'tickerId': item.get('tickerId'),
                                        'bid': bid,
                                        'ask': ask,
                                        'mid': mid,
                                        'close': close_p,
                                        'volume': _safe_int(item.get('volume'), 0),
                                        'openInterest': _safe_int(item.get('openInterest'), 0),
                                        'delta': _safe_float(item.get('delta')),
                                        'source': src_label,
                                        'is_realtime': is_rt,
                                    }
        except Exception as e:
            print(f"[Webull Unofficial] Error fetching live quote for {ticker} ${strike} {option_type}: {e}")

    # Fallback to Yahoo Finance live quote
    return _fetch_yahoo_option_live_quote(ticker, strike, option_type, exp_date_str)


# ── Batch download (sequential — for watchlists) ────────────────────

def fetch_batch(tickers, days=180, delay=0.05, on_progress=None, interval="1d", includePrePost="false", skip_webull=False):
    """
    Download data for a list of tickers sequentially via Webull.
    Returns dict of {ticker: DataFrame}.
    """
    data = {}
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        if on_progress:
            on_progress(i, total, ticker)

        df = fetch_one(ticker, days=days, interval=interval, includePrePost=includePrePost, skip_webull=skip_webull)
        # Use lower minimum for intraday intervals (15m/5m/30m) where fewer bars are expected
        min_rows = 20 if interval in ("5m", "15m", "30m", "60m") else 50
        if df is not None and len(df) >= min_rows:
            data[ticker] = df

        if delay > 0 and i < total - 1:
            time.sleep(delay)

    return data


# ── Batch download (concurrent — for full market scan) ───────────────

def fetch_batch_concurrent(tickers, days=180, max_workers=8,
                           on_progress=None, delay=0.05, interval="1d", includePrePost="false",
                           process_fn=None, skip_webull=False):
    """
    Download data for many tickers using a thread pool via Webull API.
    """
    reset_webull_circuit_breaker()

    wb_un = None if skip_webull else get_unofficial_client()
    wb_api = None if skip_webull else get_webull_client()

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
            ticker = futures[future]
            if on_progress:
                try:
                    on_progress(completed, total, ticker)
                except Exception:
                    pass
            try:
                t, res = future.result(timeout=20)
                if res is not None:
                    data[t] = res
            except Exception as e:
                print(f"[fetch_batch_concurrent] Error fetching {ticker}: {e}")
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
    Fetch quotes for many tickers from Webull directly.
    """
    print(f"[fetch_quotes_batch] Fetching {len(tickers)} quotes from Webull...")
    wb_un = get_unofficial_client()
    if not wb_un:
        print("[fetch_quotes_batch] Webull client unavailable.")
        return {}

    quotes = {}
    completed = 0
    total = len(tickers)

    def _fetch_quote_one(sym):
        try:
            q = wb_un.get_quote(stock=sym)
            if q and isinstance(q, dict):
                price = float(q["close"]) if q.get("close") else None
                pre_close = float(q["preClose"]) if q.get("preClose") else None
                mcap = float(q["marketValue"]) if q.get("marketValue") else None
                vol = int(float(q["volume"])) if q.get("volume") else None
                avg_vol = int(float(q["avgVol3M"])) if q.get("avgVol3M") else (int(float(q["avgVol10D"])) if q.get("avgVol10D") else None)
                name = q.get("name")
                earnings = q.get("nextEarningDay") or q.get("estimateEarningsDate") or q.get("latestEarningsDate")
                return sym, {
                    "price": price,
                    "close": pre_close,
                    "marketCap": mcap,
                    "avgVolume": avg_vol,
                    "volume": vol,
                    "name": name,
                    "earnings": earnings,
                }
        except Exception:
            pass
        return sym, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_quote_one, t): t for t in tickers}
        for future in as_completed(futures):
            completed += 1
            sym, q_data = future.result()
            if q_data:
                quotes[sym] = q_data
            if on_progress:
                on_progress(completed, total, sym)

    return quotes


def check_optionable_batch(tickers, max_workers=10):
    """
    Check which tickers have options chains available via Webull.
    Returns a set of tickers that are optionable.
    """
    wb_un = get_unofficial_client()
    if not wb_un:
        print("  [check_optionable_batch] No Webull client — cannot check optionability")
        return set(tickers)

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
    """Test connectivity and real-time entitlements across Webull OpenAPI, Unofficial Webull, and Yahoo."""
    _clear_openapi_scope_block()
    diag = {"ticker": ticker, "time": datetime.now().isoformat()}

    # 1. Test Webull Unofficial (Option B)
    wb_un = get_unofficial_client()
    diag["webull_unofficial_configured"] = wb_un is not None
    diag["webull_unofficial_authenticated"] = is_unofficial_authenticated()
    diag["webull_unofficial_options_mode"] = (
        "Real-Time Authenticated" if is_unofficial_authenticated() else "15-Min Delayed Guest"
    )
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

    # 2. Test Webull OpenAPI (Option A: US_STOCK, option contracts, US_OPTION snapshots)
    wb_client = get_webull_client()
    diag["webull_openapi_configured"] = wb_client is not None
    if wb_client:
        from webull.data.common.category import Category
        from webull.data.common.timespan import Timespan

        # 2a. US_STOCK historical bar entitlement
        try:
            resp = wb_client.market_data.get_history_bar(
                symbol=ticker,
                category=Category.US_STOCK.name,
                timespan=Timespan.D.name,
                count="10"
            )
            diag["webull_openapi_stock_ok"] = resp.status_code == 200
            diag["webull_openapi_stock_status"] = resp.status_code
            if resp.status_code == 200:
                diag["webull_openapi_data_sample"] = str(resp.json()[:2])
            else:
                is_not_sub, status_c, err_m = _extract_openapi_error(resp)
                if is_not_sub:
                    _mark_openapi_scope_blocked("US_STOCK", err_m)
                diag["webull_openapi_stock_error"] = err_m
        except Exception as e:
            diag["webull_openapi_stock_ok"] = False
            is_not_sub, status_c, err_m = _extract_openapi_error(e)
            if is_not_sub:
                _mark_openapi_scope_blocked("US_STOCK", err_m)
            diag["webull_openapi_stock_status"] = status_c
            diag["webull_openapi_stock_error"] = err_m

        diag["webull_openapi_ok"] = diag["webull_openapi_stock_ok"]
        if not diag["webull_openapi_stock_ok"]:
            diag["webull_openapi_error"] = diag.get("webull_openapi_stock_error")

        # 2b. Option contract catalog (free/included check) + US_OPTION live snapshot entitlement
        sample_contract_sym = None
        try:
            today_ny = datetime.now(_get_ny_tz()).date()
            contracts = _openapi_list_contracts(
                wb_client,
                ticker,
                start_date=today_ny.strftime("%Y-%m-%d"),
                end_date=(today_ny + timedelta(days=30)).strftime("%Y-%m-%d"),
                max_pages=1,
            )
            diag["webull_openapi_contracts_ok"] = len(contracts) > 0
            diag["webull_openapi_contracts_count"] = len(contracts)
            if contracts:
                sample_contract_sym = contracts[0].get("symbol")
        except Exception as e:
            diag["webull_openapi_contracts_ok"] = False
            diag["webull_openapi_contracts_error"] = str(e)

        if sample_contract_sym:
            try:
                snap_resp = wb_client.option_market_data.get_option_snapshot(
                    [sample_contract_sym], category="US_OPTION"
                )
                diag["webull_openapi_options_ok"] = snap_resp.status_code == 200
                diag["webull_openapi_options_status"] = snap_resp.status_code
                diag["webull_openapi_options_sample_symbol"] = sample_contract_sym
                if snap_resp.status_code == 200:
                    _clear_openapi_scope_block("US_OPTION")
                else:
                    is_not_sub, status_c, err_m = _extract_openapi_error(snap_resp)
                    if is_not_sub:
                        _mark_openapi_scope_blocked("US_OPTION", err_m)
                    diag["webull_openapi_options_error"] = err_m
            except Exception as e:
                diag["webull_openapi_options_ok"] = False
                is_not_sub, status_c, err_m = _extract_openapi_error(e)
                if is_not_sub:
                    _mark_openapi_scope_blocked("US_OPTION", err_m)
                diag["webull_openapi_options_status"] = status_c
                diag["webull_openapi_options_error"] = err_m
                diag["webull_openapi_options_sample_symbol"] = sample_contract_sym
        else:
            diag["webull_openapi_options_ok"] = False
            diag["webull_openapi_options_error"] = "No listing option contracts found to probe US_OPTION snapshot"
    else:
        diag["webull_openapi_ok"] = False
        diag["webull_openapi_stock_ok"] = False
        diag["webull_openapi_options_ok"] = False

    # 3. Test Yahoo Finance Fallback
    try:
        df_yf = _fetch_yahoo_fallback_one(ticker, days=10, interval="1d")
        diag["yahoo_fallback_ok"] = df_yf is not None and not df_yf.empty
        if diag["yahoo_fallback_ok"]:
            diag["yahoo_fallback_rows"] = len(df_yf)
    except Exception as e:
        diag["yahoo_fallback_ok"] = False
        diag["yahoo_fallback_error"] = str(e)

    diag["market_data_status"] = get_market_data_status()
    diag["realtime_options_ready"] = diag["market_data_status"]["realtime_options_ready"]
    diag["ok"] = (
        diag.get("webull_unofficial_ok", False)
        or diag.get("webull_openapi_ok", False)
        or diag.get("yahoo_fallback_ok", False)
    )
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

