import sys

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from reversal_scanner import (
    three_sigma_full_market_scan,
    two_sigma_full_market_scan,
    fifty_two_week_reversal_scan,
    rsi_divergence_full_market_scan,
    options_directional_exhaustion_scan,
    watchlist_scan,
    options_watchlist_scan,
    scan_progress, _reset_progress
)
from datetime import datetime, timedelta
import socket
import threading
import json
import os
import traceback
import pytz

def get_ny_timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return pytz.timezone("America/New_York")

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # No browser caching of static files
CORS(app)

@app.after_request
def add_header(r):
    """Disable caching for all dynamic API responses."""
    if request.path.startswith("/api/"):
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, public, max-age=0"
        r.headers["Pragma"] = "no-cache"
        r.headers["Expires"] = "0"
    return r

# ── Start 3-Sigma Background Alerting Bot ──────────────────────────────
try:
    from indicator_bot import start_bot_thread
    start_bot_thread()
except Exception as e:
    print(f"Error starting background indicator bot: {e}")

# ── Scan Persistence ───────────────────────────────────────────────────

THREE_SIGMA_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_3sigma_scan.json")
TWO_SIGMA_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_2sigma_scan.json")
FIFTY_TWO_WEEK_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_52w_scan.json")
RSIDIV_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_rsidiv_scan.json")
OPTIONS_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_options_scan.json")
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
WATCHLIST_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_watchlist_scan.json")
OPTIONS_WATCHLIST_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_options_watchlist_scan.json")

DEFAULT_WATCHLIST = ["AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "NVDA", "NFLX", "AMD", "SPY", "QQQ"]

def load_watchlist():
    """Load watchlist from file, or use default."""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else DEFAULT_WATCHLIST[:]
        except Exception:
            pass
    return DEFAULT_WATCHLIST[:]

def save_watchlist(tickers):
    """Save watchlist to file."""
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(tickers, f, indent=2)
    except Exception as e:
        print(f"Failed to save watchlist to {WATCHLIST_FILE}: {e}")

user_watchlist = load_watchlist()

def load_last_scan(filepath=THREE_SIGMA_RESULTS_FILE):
    """Load the last scan results from file."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except:
            pass
    return None

def sanitize_for_json(obj):
    """Recursively convert timestamps, numpy types, and non-serializable objects for JSON."""
    if isinstance(obj, (datetime, pd.Timestamp)):
        return obj.isoformat()
    elif isinstance(obj, (np.integer, np.int64, np.int32)):
        return int(obj)
    elif isinstance(obj, (np.floating, np.float64, np.float32)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize_for_json(x) for x in obj]
    elif pd.isna(obj):
        return None
    return obj

def save_last_scan(data, filepath=THREE_SIGMA_RESULTS_FILE):
    """Save the scan results to file for persistence."""
    try:
        data = sanitize_for_json(data)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to save scan results to {filepath}: {e}")

# Track whether a full scan is in progress
_scan_lock = threading.Lock()
_scan_running = False

# ── Static files ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")



# ── API: Check scan progress ────────────────────────────────────────

@app.route("/api/scan/progress", methods=["GET"])
def scan_full_progress():
    """Return current progress of the scan."""
    return jsonify(scan_progress)


@app.route("/api/scan/cancel", methods=["POST"])
def scan_cancel():
    """Force-cancel any stuck/zombie scan and reset the lock."""
    global _scan_running
    with _scan_lock:
        was_running = _scan_running
        _scan_running = False
    _reset_progress()
    scan_progress["status"] = "idle"
    return jsonify({"ok": True, "was_running": was_running, "message": "Scan cancelled and lock released"})


# ── API: 3-Sigma Scans (async) ──────────────────────────────────────

def _scan_conflict_response():
    return jsonify({"ok": False, "error": "A scan is already running. Please wait for it to complete."}), 409

@app.route("/api/scan/3sigma", methods=["POST"])
def scan_3sigma():
    """Start a full market 3-sigma scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="3sigma")
        scan_progress["phase_label"] = "Initiating scan..."

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = three_sigma_full_market_scan(extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "3sigma",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_3SIGMA_RESULTS"] = results_data
            save_last_scan(results_data, THREE_SIGMA_RESULTS_FILE)
            scan_progress["status"] = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_3SIGMA_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = traceback.format_exc()
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "3-Sigma scan started"})

@app.route("/api/scan/3sigma/results", methods=["GET"])
def scan_3sigma_results():
    results = app.config.get("LAST_3SIGMA_RESULTS")
    if results is None:
        results = load_last_scan(THREE_SIGMA_RESULTS_FILE)
        if results:
            app.config["LAST_3SIGMA_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

# ── API: Options Extreme Scan (async) ───────────────────────────────

@app.route("/api/scan/options", methods=["POST"])
def scan_options():
    """Start a full market Options Directional Exhaustion scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="options")
        scan_progress["phase_label"] = "Initiating Options scan..."

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = options_directional_exhaustion_scan()
            results_data = {
                "ok": True,
                "mode": "options",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_OPTIONS_RESULTS"] = results_data
            save_last_scan(results_data, OPTIONS_RESULTS_FILE)
            scan_progress["status"] = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_OPTIONS_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = traceback.format_exc()
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "Options scan started"})

@app.route("/api/scan/options/results", methods=["GET"])
def scan_options_results():
    results = app.config.get("LAST_OPTIONS_RESULTS")
    if results is None:
        results = load_last_scan(OPTIONS_RESULTS_FILE)
        if results:
            app.config["LAST_OPTIONS_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)


# ── API: 2-Sigma Scans (async) ──────────────────────────────────────

@app.route("/api/scan/2sigma", methods=["POST"])
def scan_2sigma():
    """Start a full market 2-sigma scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="2sigma")
        scan_progress["phase_label"] = "Initiating scan..."

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = two_sigma_full_market_scan(extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "2sigma",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_2SIGMA_RESULTS"] = results_data
            save_last_scan(results_data, TWO_SIGMA_RESULTS_FILE)
            scan_progress["status"] = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_2SIGMA_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = traceback.format_exc()
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "2-Sigma scan started"})

@app.route("/api/scan/2sigma/results", methods=["GET"])
def scan_2sigma_results():
    results = app.config.get("LAST_2SIGMA_RESULTS")
    if results is None:
        results = load_last_scan(TWO_SIGMA_RESULTS_FILE)
        if results:
            app.config["LAST_2SIGMA_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

# ── API: 52-Week Reversal Scans (async) ─────────────────────────────

@app.route("/api/scan/52w", methods=["POST"])
def scan_52w():
    """Start a 52-week high/low reversal scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="52w")
        scan_progress["phase_label"] = "Initiating scan..."

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = fifty_two_week_reversal_scan(extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "52w",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_52W_RESULTS"] = results_data
            save_last_scan(results_data, FIFTY_TWO_WEEK_RESULTS_FILE)
            scan_progress["status"] = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_52W_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = traceback.format_exc()
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "52-week reversal scan started"})

@app.route("/api/scan/52w/results", methods=["GET"])
def scan_52w_results():
    results = app.config.get("LAST_52W_RESULTS")
    if results is None:
        results = load_last_scan(FIFTY_TWO_WEEK_RESULTS_FILE)
        if results:
            app.config["LAST_52W_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

# ── API: RSI Divergence Scans (async) ──────────────────────────────

@app.route("/api/scan/rsidiv", methods=["POST"])
def scan_rsidiv():
    """Start a full market RSI divergence scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="rsidiv")
        scan_progress["phase_label"] = "Initiating scan..."

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = rsi_divergence_full_market_scan(extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "rsidiv",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_RSIDIV_RESULTS"] = results_data
            save_last_scan(results_data, RSIDIV_RESULTS_FILE)
            scan_progress["status"] = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_RSIDIV_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = traceback.format_exc()
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "RSI divergence scan started"})

@app.route("/api/scan/rsidiv/results", methods=["GET"])
def scan_rsidiv_results():
    results = app.config.get("LAST_RSIDIV_RESULTS")
    if results is None:
        results = load_last_scan(RSIDIV_RESULTS_FILE)
        if results:
            app.config["LAST_RSIDIV_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

# ── API: Watchlist Scan ─────────────────────────────────────────────

@app.route("/api/scan/watchlist", methods=["POST"])
def scan_watchlist():
    """Start a watchlist reversal scan in the background."""
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="watchlist")
        scan_progress["phase_label"] = "Initiating watchlist scan (all criteria)..."

    data = request.get_json() or {}
    extended_hours = data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            current_watchlist = load_watchlist()
            df = watchlist_scan(current_watchlist, extended_hours=extended_hours)
            raw_records = df.to_dict(orient="records") if not df.empty else []
            clean_records = sanitize_for_json(raw_records)
            results_data = {
                "ok": True,
                "mode": "watchlist",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(clean_records),
                "results": clean_records,
            }
            app.config["LAST_WATCHLIST_RESULTS"] = results_data
            save_last_scan(results_data, WATCHLIST_RESULTS_FILE)
            scan_progress["status"] = "done"
            scan_progress["mode"] = "watchlist"
        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            print(f"[Watchlist Scan Error] {e}\n{tb_str}")
            app.config["LAST_WATCHLIST_RESULTS"] = {"ok": False, "error": str(e), "traceback": tb_str}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = str(e)
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "Watchlist scan started (all criteria)"})

@app.route("/api/scan/watchlist/sync", methods=["GET", "POST"])
def scan_watchlist_sync():
    """Run a synchronous mini watchlist scan for diagnostic testing."""
    try:
        current_watchlist = load_watchlist()[:5]
        df = watchlist_scan(current_watchlist, extended_hours=False)
        return jsonify({
            "ok": True,
            "tickers": current_watchlist,
            "count": len(df) if not df.empty else 0,
            "results": df.to_dict(orient="records") if not df.empty else []
        })
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500

@app.route("/api/scan/watchlist/results", methods=["GET"])
def scan_watchlist_results():
    results = app.config.get("LAST_WATCHLIST_RESULTS")
    if results is None:
        results = load_last_scan(WATCHLIST_RESULTS_FILE)
        if results:
            app.config["LAST_WATCHLIST_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

@app.route("/api/scan/options/watchlist", methods=["POST"])
def scan_options_watchlist():
    """Start an options watchlist scan in the background."""
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return jsonify({"ok": False, "error": "A scan is already running"}), 409
        _scan_running = True

    data = request.get_json() or {}
    extended_hours = data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = options_watchlist_scan(user_watchlist, extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "options_watchlist",
                "timestamp": datetime.now(et_tz).strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_OPTIONS_WATCHLIST_RESULTS"] = results_data
            save_last_scan(results_data, OPTIONS_WATCHLIST_RESULTS_FILE)
            scan_progress["status"] = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_OPTIONS_WATCHLIST_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = str(e)
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=False).start()
    return jsonify({"ok": True, "message": "Options watchlist scan started"})

@app.route("/api/scan/options/watchlist/results", methods=["GET"])
def scan_options_watchlist_results():
    results = app.config.get("LAST_OPTIONS_WATCHLIST_RESULTS")
    if results is None:
        results = load_last_scan(OPTIONS_WATCHLIST_RESULTS_FILE)
        if results:
            app.config["LAST_OPTIONS_WATCHLIST_RESULTS"] = results
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

# ── API: Watchlist CRUD ─────────────────────────────────────────────

@app.route("/api/watchlist", methods=["GET"])
def watchlist_get():
    """Return current watchlist."""
    return jsonify({"ok": True, "watchlist": user_watchlist})

@app.route("/api/watchlist", methods=["PUT"])
def watchlist_replace():
    """Replace entire watchlist."""
    global user_watchlist
    data = request.get_json() or {}
    tickers = data.get("watchlist", [])
    cleaned = []
    for t in tickers:
        sym = t.strip().upper().replace(" ", "")
        if sym and sym.isalpha() and 1 <= len(sym) <= 5:
            if sym not in cleaned:
                cleaned.append(sym)
    user_watchlist = cleaned
    save_watchlist(user_watchlist)
    return jsonify({"ok": True, "watchlist": user_watchlist})

@app.route("/api/watchlist/add", methods=["POST"])
def watchlist_add():
    """Add a ticker to the watchlist."""
    global user_watchlist
    data = request.get_json() or {}
    ticker = data.get("ticker", "").strip().upper().replace(" ", "")
    if not ticker or not ticker.isalpha() or len(ticker) > 5:
        return jsonify({"ok": False, "error": "Invalid ticker symbol"}), 400
    if ticker in user_watchlist:
        return jsonify({"ok": False, "error": f"{ticker} is already in watchlist"}), 409
    user_watchlist.append(ticker)
    save_watchlist(user_watchlist)
    return jsonify({"ok": True, "watchlist": user_watchlist})

@app.route("/api/watchlist/remove", methods=["POST"])
def watchlist_remove():
    """Remove a ticker from the watchlist."""
    global user_watchlist
    data = request.get_json() or {}
    ticker = data.get("ticker", "").strip().upper().replace(" ", "")
    if ticker in user_watchlist:
        user_watchlist.remove(ticker)
        save_watchlist(user_watchlist)
    return jsonify({"ok": True, "watchlist": user_watchlist})

@app.route("/api/watchlist/import-webull", methods=["POST"])
def watchlist_import_webull():
    """Import all watchlists from Webull account credentials."""
    global user_watchlist
    try:
        from data_fetcher import get_unofficial_client
        wb = get_unofficial_client()
        if not wb:
            return jsonify({"ok": False, "error": "Webull client authentication failed. Check credentials in .env"}), 400
        
        watchlists = wb.get_watchlists()
        if not watchlists:
            return jsonify({"ok": False, "error": "No watchlists found on Webull account"}), 400
        
        imported = set()
        if isinstance(watchlists, list):
            for wl in watchlists:
                ticker_list = wl.get("tickerList", [])
                for tick in ticker_list:
                    sym = tick.get("symbol")
                    if sym:
                        sym_clean = sym.strip().upper().replace(" ", "")
                        if sym_clean and sym_clean.isalpha() and 1 <= len(sym_clean) <= 5:
                            imported.add(sym_clean)
        
        added_count = 0
        for sym in sorted(imported):
            if sym not in user_watchlist:
                user_watchlist.append(sym)
                added_count += 1
        
        if added_count > 0:
            save_watchlist(user_watchlist)
            
        return jsonify({
            "ok": True,
            "watchlist": user_watchlist,
            "added_count": added_count,
            "total_imported": len(imported)
        })
    except Exception as e:
        print(f"Error importing Webull watchlists: {e}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ── API: Reset Stuck Scan State ─────────────────────────────────────

@app.route("/api/scan/reset", methods=["POST"])
def scan_reset():
    """Reset the scan running status to idle."""
    global _scan_running
    with _scan_lock:
        _scan_running = False
    _reset_progress()
    return jsonify({"ok": True, "message": "Scan status reset to idle"})

# ── API: Ping Endpoint ──────────────────────────────────────────────

@app.route("/api/ping", methods=["GET"])
def ping():
    """Lightweight health check endpoint to keep the server awake."""
    return jsonify({"ok": True, "status": "active", "timestamp": datetime.now().isoformat()})

@app.route("/api/check-imports", methods=["GET"])
def check_imports():
    res = {}
    try:
        import curl_cffi
        res["curl_cffi_ok"] = True
        res["curl_cffi_version"] = getattr(curl_cffi, "__version__", "unknown")
    except Exception as e:
        res["curl_cffi_ok"] = False
        res["curl_cffi_error"] = str(e)
    return jsonify(res)


@app.route("/api/debug-scan", methods=["GET"])
def debug_scan():
    """Synchronous mini-scan: fetches 5 tickers and runs 3-sigma analysis. Returns traceback on failure."""
    import traceback
    steps = {}
    try:
        steps["step1_imports"] = "starting"
        from reversal_scanner import (
            get_us_tickers, prefilter_liquid_optionable,
            _analyze_3sigma_setup, check_spy_regime
        )
        from data_fetcher import fetch_batch_concurrent
        steps["step1_imports"] = "ok"

        steps["step2_tickers"] = "starting"
        all_tickers = get_us_tickers()
        steps["step2_tickers"] = f"ok ({len(all_tickers)} tickers)"

        # Only test with 5 tickers
        test_tickers = all_tickers[:5]
        steps["step3_test_tickers"] = test_tickers

        steps["step4_webull_batch"] = "starting"
        daily_data = fetch_batch_concurrent(test_tickers, days=180, interval="1d")
        steps["step4_webull_batch"] = f"ok ({len(daily_data)} fetched)"

        steps["step5_regime"] = "starting"
        is_bullish = check_spy_regime()
        steps["step5_regime"] = f"ok (bullish={is_bullish})"

        steps["step6_analyze"] = "starting"
        results = []
        for sym, df_daily in daily_data.items():
            result = _analyze_3sigma_setup(sym, None, df_daily, is_market_bullish=is_bullish)
            if result:
                results.append(result)
        steps["step6_analyze"] = f"ok ({len(results)} signals)"

        return jsonify({"ok": True, "steps": steps, "results_count": len(results),
                        "results_sample": results[:2] if results else []})
    except Exception as e:
        steps["error"] = str(e)
        steps["traceback"] = traceback.format_exc()
        return jsonify({"ok": False, "steps": steps}), 500


# ── API: Diagnostics ────────────────────────────────────────────────

@app.route("/api/test", methods=["GET"])
def test_api():
    """Diagnostic endpoint: test if the data fetcher works on this server."""
    try:
        from data_fetcher import test_connection
        ticker = request.args.get("ticker", "AAPL")
        diag = test_connection(ticker)
        diag["server_time"] = datetime.now().isoformat()
        return jsonify(diag)
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500

@app.route("/api/test-options", methods=["GET"])
def test_options_api():
    """Diagnostic endpoint to test find_best_option on the server with verbose logging."""
    logs = []
    try:
        from data_fetcher import fetch_options_chain, fetch_options_for_expiration
        import time
        from datetime import datetime
        import numpy as np

        ticker = request.args.get("ticker", "AAPL")
        signal_type = request.args.get("type", "bullish")
        price = float(request.args.get("price", 327.0))
        
        logs.append(f"Starting test for {ticker} | Type: {signal_type} | Price: {price}")
        
        # 1. Fetch options chain
        try:
            chain_meta = fetch_options_chain(ticker)
            logs.append(f"fetch_options_chain returned keys: {list(chain_meta.keys()) if chain_meta else 'None'}")
        except Exception as e:
            logs.append(f"fetch_options_chain failed: {e}")
            chain_meta = None
            
        if not chain_meta:
            return jsonify({"ok": True, "logs": logs, "result": None})
            
        now = time.time()
        valid_exps = []
        for exp in chain_meta.get("expirations", []):
            dte = (exp - now) / 86400
            logs.append(f"Exp: {datetime.fromtimestamp(exp).strftime('%Y-%m-%d')} | DTE: {dte:.1f}")
            if 25 <= dte <= 65:
                valid_exps.append(exp)
                
        logs.append(f"Valid expirations in range: {[datetime.fromtimestamp(e).strftime('%Y-%m-%d') for e in valid_exps]}")
        if not valid_exps:
            return jsonify({"ok": True, "logs": logs, "result": None})
            
        best_contract = None
        for exp_ts in valid_exps:
            exp_str = datetime.fromtimestamp(exp_ts).strftime('%Y-%m-%d')
            logs.append(f"Checking exp: {exp_str}")
            
            try:
                chain = fetch_options_for_expiration(ticker, exp_ts)
                logs.append(f"fetch_options_for_expiration returned: {'dict' if isinstance(chain, dict) else 'None'}")
            except Exception as e:
                logs.append(f"fetch_options_for_expiration failed: {e}")
                chain = None
                
            has_data = False
            if chain:
                calls = chain.get("calls", [])
                logs.append(f"Webull calls count: {len(calls)}")
                for c in calls[:5]:
                    logs.append(f"  Sample call: strike={c.get('strike')}, bid={c.get('bid')}, ask={c.get('ask')}")
                for c in calls[:10]:
                    if c.get("bid") is not None or c.get("ask") is not None:
                        has_data = True
                        break
                        
            if not chain:
                logs.append("No chain data found on Webull")
                continue
                
            contracts = chain.get("calls" if signal_type == "bullish" else "puts", [])
            logs.append(f"Contracts count to analyze: {len(contracts)}")
            
            for c in contracts:
                strike = c.get("strike")
                vol = c.get("volume") or 0
                oi = c.get("openInterest") or 0
                bid = c.get("bid") or 0
                ask = c.get("ask") or 0
                iv = c.get("impliedVolatility") or 0
                
                mid = (bid + ask) / 2
                spread_pct = ((ask - bid) / mid) * 100 if mid > 0 else 999
                dist_pct = (strike - price) / price
                
                is_valid_strike = False
                if signal_type == "bullish":
                    if -0.05 <= dist_pct <= 0.01:
                        is_valid_strike = True
                else:
                    if -0.01 <= dist_pct <= 0.05:
                        is_valid_strike = True
                        
                # Log a couple of strikes near the spot price
                if abs(dist_pct) < 0.03:
                    logs.append(f"  Contract: strike={strike} | vol={vol} | oi={oi} | bid={bid} | ask={ask} | mid={mid} | spread={spread_pct:.1f}% | dist={dist_pct*100:.1f}% | valid_strike={is_valid_strike}")
                    
                if vol < 50 or oi < 100:
                    continue
                if mid <= 0:
                    continue
                if spread_pct > 12:
                    continue
                if not is_valid_strike:
                    continue
                    
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
                    logs.append(f"  *** NEW BEST: {best_contract['symbol']} at strike {strike}")
                    
            if best_contract:
                break
                
        return jsonify({
            "ok": True,
            "logs": logs,
            "result": best_contract
        })
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "logs": logs,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500

@app.route("/api/logs", methods=["GET"])
def get_logs():
    """Endpoint to fetch the last 200 lines of the bot log."""
    log_file = os.path.join(os.path.dirname(__file__), "3sigma_bot.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
            return "".join(lines[-200:]), 200, {"Content-Type": "text/plain"}
        except Exception as e:
            return f"Error reading log: {e}", 500
    return "Log file not found", 404

# ── Start ────────────────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"

if __name__ == "__main__":
    ip = get_local_ip()
    port = 5050
    print("=" * 55)
    print("  📈  STOCK REVERSAL & MOMENTUM SCANNER — WEB SERVER")
    print("=" * 55)
    print(f"  Local  :  http://localhost:{port}")
    print(f"  Phone  :  http://{ip}:{port}")
    print()
    print("  Open the Phone URL on your phone's browser")
    print("  (both devices must be on the same Wi-Fi)")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
