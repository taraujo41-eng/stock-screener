"""
Stock Reversal Scanner – Web Server
Run:  python3 app.py
Then open http://<your-mac-ip>:5050 on your phone.
"""

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from reversal_scanner import (
    reversal_scanner, full_market_scan, scan_progress,
    WATCHLIST, _reset_progress
)
from datetime import datetime
import socket
import threading
import json
import os

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ── Watchlist persistence ────────────────────────────────────────────

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")

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

# In-memory watchlist (loaded on startup)
user_watchlist = load_watchlist()

# Track whether a full scan is in progress
_scan_lock = threading.Lock()
_scan_running = False

# ── Static files ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── API: Quick watchlist scan ────────────────────────────────────────

@app.route("/api/scan", methods=["GET"])
def scan():
    """Run the quick watchlist scanner and return JSON results."""
    try:
        df = reversal_scanner(user_watchlist)
        results = df.to_dict(orient="records") if not df.empty else []
        return jsonify({
            "ok": True,
            "mode": "watchlist",
            "timestamp": datetime.now().strftime("%b %d, %Y  %I:%M %p"),
            "count": len(results),
            "tickers_scanned": len(user_watchlist),
            "results": results,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── API: Full market scan (async) ───────────────────────────────────

@app.route("/api/scan/full", methods=["POST"])
def scan_full():
    """Start a full market scan in the background."""
    global _scan_running

    if _scan_running:
        return jsonify({"ok": False, "error": "A scan is already running"}), 409

    def _run():
        global _scan_running
        _scan_running = True
        try:
            df = full_market_scan()
            app.config["LAST_FULL_RESULTS"] = {
                "ok": True,
                "mode": "full_market",
                "timestamp": datetime.now().strftime("%b %d, %Y  %I:%M %p"),
                "count": len(df) if not df.empty else 0,
                "results": df.to_dict(orient="records") if not df.empty else [],
            }
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

    return jsonify({"ok": True, "message": "Full market scan started"})

# ── API: Check scan progress ────────────────────────────────────────

@app.route("/api/scan/progress", methods=["GET"])
def scan_full_progress():
    """Return current progress of the full market scan."""
    return jsonify(scan_progress)

# ── API: Get full scan results ──────────────────────────────────────

@app.route("/api/scan/full/results", methods=["GET"])
def scan_full_results():
    """Return the results of the last full market scan."""
    results = app.config.get("LAST_FULL_RESULTS")
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
    print("  📈  STOCK REVERSAL SCANNER — WEB SERVER")
    print("=" * 55)
    print(f"  Local  :  http://localhost:{port}")
    print(f"  Phone  :  http://{ip}:{port}")
    print(f"  Watchlist : {len(user_watchlist)} tickers")
    print()
    print("  Open the Phone URL on your phone's browser")
    print("  (both devices must be on the same Wi-Fi)")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
