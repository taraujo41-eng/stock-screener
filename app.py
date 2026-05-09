"""
Stock Reversal & Momentum Scanner – Web Server
Run:  python3 app.py
Then open http://<your-mac-ip>:5050 on your phone.
"""

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from reversal_scanner import (
    reversal_scanner, full_market_scan, 
    momentum_watchlist_scan, momentum_full_market_scan,
    scan_progress, WATCHLIST, _reset_progress
)
from datetime import datetime, timedelta
import socket
import threading
import json
import os
import traceback

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # No browser caching of static files
CORS(app)

# ── Watchlist & Scan Persistence ───────────────────────────────────────

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
SCAN_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_scan.json")
MOMENTUM_RESULTS_FILE = os.path.join(os.path.dirname(__file__), "last_momentum_scan.json")

def load_watchlist():
    """Load watchlist from file, or use default."""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else WATCHLIST[:]
        except:
            pass
    return WATCHLIST[:]

def save_watchlist(tickers):
    """Save watchlist to file."""
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(tickers, f, indent=2)

def load_last_scan(filepath=SCAN_RESULTS_FILE):
    """Load the last scan results from file."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except:
            pass
    return None

def save_last_scan(data, filepath=SCAN_RESULTS_FILE):
    """Save the scan results to file for persistence."""
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to save scan results to {filepath}: {e}")

# In-memory watchlist (loaded on startup)
user_watchlist = load_watchlist()

# Track whether a full scan is in progress
_scan_lock = threading.Lock()
_scan_running = False

# ── Static files ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── API: Watchlist reversal scan (async) ────────────────────────────────

@app.route("/api/scan", methods=["POST"])
def scan():
    """Start a watchlist reversal scan in the background."""
    global _scan_running

    if _scan_running:
        return jsonify({"ok": False, "error": "A scan is already running"}), 409

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        _scan_running = True
        try:
            df = reversal_scanner(user_watchlist, extended_hours=extended_hours)
            app.config["LAST_SCAN_RESULTS"] = {
                "ok": True,
                "mode": "watchlist",
                "timestamp": datetime.now().strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "tickers_scanned": len(user_watchlist),
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
        except Exception as e:
            app.config["LAST_SCAN_RESULTS"] = {
                "ok": False,
                "error": str(e),
            }
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = str(e)
        finally:
            _scan_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": "Watchlist reversal scan started"})

# ── API: Get watchlist reversal scan results ───────────────────────────

@app.route("/api/scan/results", methods=["GET"])
def scan_results():
    """Return the results of the last watchlist reversal scan."""
    results = app.config.get("LAST_SCAN_RESULTS")
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

# ── API: Full market reversal scan (async) ─────────────────────────────

@app.route("/api/scan/full", methods=["POST"])
def scan_full():
    """Start a full market reversal scan in the background."""
    global _scan_running

    if _scan_running:
        return jsonify({"ok": False, "error": "A scan is already running"}), 409

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        _scan_running = True
        try:
            df = full_market_scan(extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "full_market",
                "timestamp": datetime.now().strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_FULL_RESULTS"] = results_data
            save_last_scan(results_data)
        except Exception as e:
            app.config["LAST_FULL_RESULTS"] = {
                "ok": False,
                "error": str(e),
            }
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = str(e)
        finally:
            _scan_running = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": "Full market reversal scan started"})

# ── API: Check scan progress ────────────────────────────────────────

@app.route("/api/scan/progress", methods=["GET"])
def scan_full_progress():
    """Return current progress of the scan."""
    return jsonify(scan_progress)

# ── API: Get full reversal scan results ────────────────────────────────

@app.route("/api/scan/full/results", methods=["GET"])
def scan_full_results():
    """Return the results of the last full market reversal scan."""
    results = app.config.get("LAST_FULL_RESULTS")
    
    # If not in memory, try loading from file
    if results is None:
        results = load_last_scan(SCAN_RESULTS_FILE)
        if results:
            app.config["LAST_FULL_RESULTS"] = results

    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
        
    return jsonify(results)

# ── API: Momentum scans (async) ────────────────────────────────────────

@app.route("/api/scan/momentum", methods=["POST"])
def scan_momentum():
    """Start a momentum watchlist scan."""
    global _scan_running
    if _scan_running:
        return jsonify({"ok": False, "error": "A scan is already running"}), 409

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        _scan_running = True
        try:
            df = momentum_watchlist_scan(user_watchlist, extended_hours=extended_hours)
            app.config["LAST_MOMENTUM_RESULTS"] = {
                "ok": True,
                "mode": "momentum_watchlist",
                "timestamp": datetime.now().strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "tickers_scanned": len(user_watchlist),
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
        except Exception as e:
            app.config["LAST_MOMENTUM_RESULTS"] = {"ok": False, "error": str(e)}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = str(e)
        finally:
            _scan_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Momentum scan started"})

@app.route("/api/scan/momentum/results", methods=["GET"])
def scan_momentum_results():
    results = app.config.get("LAST_MOMENTUM_RESULTS")
    if results is None:
        return jsonify({"ok": False, "error": "No scan results available"}), 404
    return jsonify(results)

@app.route("/api/scan/momentum/full", methods=["POST"])
def scan_momentum_full():
    """Start a full market momentum scan."""
    global _scan_running
    if _scan_running:
        return jsonify({"ok": False, "error": "A scan is already running"}), 409

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        _scan_running = True
        try:
            df = momentum_full_market_scan(extended_hours=extended_hours)
            results_data = {
                "ok": True,
                "mode": "momentum_full",
                "timestamp": datetime.now().strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
            app.config["LAST_MOMENTUM_FULL_RESULTS"] = results_data
            save_last_scan(results_data, MOMENTUM_RESULTS_FILE)
        except Exception as e:
            app.config["LAST_MOMENTUM_FULL_RESULTS"] = {"ok": False, "error": str(e)}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = str(e)
        finally:
            _scan_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Full momentum scan started"})

@app.route("/api/scan/momentum/full/results", methods=["GET"])
def scan_momentum_full_results():
    results = app.config.get("LAST_MOMENTUM_FULL_RESULTS")
    if results is None:
        results = load_last_scan(MOMENTUM_RESULTS_FILE)
        if results:
            app.config["LAST_MOMENTUM_FULL_RESULTS"] = results

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
    data = request.get_json()
    tickers = data.get("watchlist", [])
    # Clean and validate
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
    data = request.get_json()
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
    data = request.get_json()
    ticker = data.get("ticker", "").strip().upper()
    if ticker in user_watchlist:
        user_watchlist.remove(ticker)
        save_watchlist(user_watchlist)
    return jsonify({"ok": True, "watchlist": user_watchlist})

# ── API: Diagnostics ────────────────────────────────────────────────

@app.route("/api/test", methods=["GET"])
def test_api():
    """Diagnostic endpoint: test if the data fetcher works on this server."""
    from data_fetcher import test_connection
    ticker = request.args.get("ticker", "AAPL")
    diag = test_connection(ticker)
    diag["server_time"] = datetime.now().isoformat()
    return jsonify(diag)

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
    print(f"  Watchlist : {len(user_watchlist)} tickers")
    print()
    print("  Open the Phone URL on your phone's browser")
    print("  (both devices must be on the same Wi-Fi)")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
