"""
Stock Reversal & Momentum Scanner – Web Server
Run:  python3 app.py
Then open http://<your-mac-ip>:5050 on your phone.
"""

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from reversal_scanner import (
    three_sigma_full_market_scan,
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

def load_last_scan(filepath=THREE_SIGMA_RESULTS_FILE):
    """Load the last scan results from file."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except:
            pass
    return None

def save_last_scan(data, filepath=THREE_SIGMA_RESULTS_FILE):
    """Save the scan results to file for persistence."""
    try:
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



# ── API: 3-Sigma Scans (async) ──────────────────────────────────────

@app.route("/api/scan/3sigma", methods=["POST"])
def scan_3sigma():
    """Start a full market 3-sigma scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return jsonify({"ok": False, "error": "A scan is already running"}), 409
        _scan_running = True

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
        except Exception as e:
            import traceback
            traceback.print_exc()
            app.config["LAST_3SIGMA_RESULTS"] = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
            scan_progress["status"] = "error"
            scan_progress["phase_label"] = traceback.format_exc()
        finally:
            with _scan_lock:
                _scan_running = False

    threading.Thread(target=_run, daemon=True).start()
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
