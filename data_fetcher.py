"""
Cloud-safe stock data fetcher.

Uses Yahoo Finance's chart API directly with browser-like headers
and proper cookie/crumb authentication. This bypasses the
bot-detection that blocks the yfinance library on cloud servers.
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import warnings
import traceback as tb

warnings.filterwarnings("ignore")

# ── Session management ──────────────────────────────────────────────

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


# ── Single ticker download ──────────────────────────────────────────

def fetch_one(ticker, days=180, interval="1d", includePrePost="false"):
    """
    Fetch OHLCV data for one ticker.
    Returns a pandas DataFrame or None on failure.
    """
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

        # On 401/403, refresh session and retry once
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

        quote = result["indicators"]["quote"][0]

        df = pd.DataFrame(
            {
                "Open": quote.get("open"),
                "High": quote.get("high"),
                "Low": quote.get("low"),
                "Close": quote.get("close"),
                "Volume": quote.get("volume"),
            },
            index=pd.DatetimeIndex(
                pd.to_datetime(timestamps, unit="s", utc=True)
                .tz_convert("America/New_York")
                .normalize(),
                name="Date",
            ),
        )
        df = df.dropna(subset=["Close"])

        # Convert Volume to int where possible
        df["Volume"] = df["Volume"].fillna(0).astype(np.int64)

        return df if not df.empty else None

    except Exception:
        return None


# ── Batch download (sequential — for watchlists) ────────────────────

def fetch_batch(tickers, days=180, delay=0.05, on_progress=None, interval="1d", includePrePost="false"):
    """
    Download data for a list of tickers sequentially.
    Returns dict of {ticker: DataFrame}.
    """
    data = {}
    total = len(tickers)

    for i, ticker in enumerate(tickers):
        if on_progress:
            on_progress(i, total, ticker)

        df = fetch_one(ticker, days=days, interval=interval, includePrePost=includePrePost)
        if df is not None and len(df) >= 50:
            data[ticker] = df

        if delay > 0 and i < total - 1:
            time.sleep(delay)

    return data


# ── Batch download (concurrent — for full market scan) ───────────────

def fetch_batch_concurrent(tickers, days=180, max_workers=8,
                           on_progress=None, delay=0.05, interval="1d", includePrePost="false"):
    """
    Download data for many tickers using a thread pool.
    Returns dict of {ticker: DataFrame}.
    """
    data = {}
    completed = 0
    total = len(tickers)

    def _fetch(i, ticker):
        # Small stagger to spread requests
        time.sleep(delay * (i % max_workers))
        return ticker, fetch_one(ticker, days=days, interval=interval, includePrePost=includePrePost)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch, i, t): t for i, t in enumerate(tickers)}

        for future in as_completed(futures):
            completed += 1
            try:
                ticker, df = future.result()
                if df is not None and len(df) >= 50:
                    data[ticker] = df
                if on_progress:
                    on_progress(completed, total, ticker)
            except Exception:
                if on_progress:
                    on_progress(completed, total, futures[future])

    return data


# ── Connectivity test ────────────────────────────────────────────────

def test_connection(ticker="AAPL"):
    """Test if we can download data. Returns diagnostic dict."""
    diag = {"ticker": ticker, "time": datetime.now().isoformat()}

    try:
        session, crumb = _ensure_session()
        diag["has_crumb"] = crumb is not None

        df = fetch_one(ticker, days=30)
        if df is not None:
            diag["ok"] = True
            diag["rows"] = len(df)
            diag["last_date"] = str(df.index[-1].date())
            diag["last_close"] = round(float(df["Close"].iloc[-1]), 2)
            diag["last_volume"] = int(df["Volume"].iloc[-1])
        else:
            diag["ok"] = False
            diag["error"] = "No data returned — API may be rate-limiting or blocking"
    except Exception as e:
        diag["ok"] = False
        diag["error"] = str(e)
        diag["traceback"] = tb.format_exc()

    return diag
