"""
Stock Reversal & Momentum Scanner – Web Server
Run:  python3 app.py
Then open http://<your-mac-ip>:5050 on your phone.
"""

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from reversal_scanner import (
    three_sigma_full_market_scan,
    two_sigma_full_market_scan,
    fifty_two_week_reversal_scan,
    rsi_divergence_full_market_scan,
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

def _scan_conflict_response():
    running_mode = scan_progress.get("mode")
    mode_names = {
        "3sigma": "3-Sigma Bands",
        "2sigma": "2-Sigma Bands",
        "52w": "52-Week Reversal",
        "rsidiv": "RSI Divergence"
    }
    friendly_name = mode_names.get(running_mode, "another tab")
    return jsonify({"ok": False, "error": f"A scan is already running on the {friendly_name} tab"}), 409

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

    threading.Thread(target=_run, daemon=True).start()
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

    threading.Thread(target=_run, daemon=True).start()
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

    threading.Thread(target=_run, daemon=True).start()
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

@app.route("/api/test-options", methods=["GET"])
def test_options_api():
    """Diagnostic endpoint to test find_best_option on the server."""
    try:
        from reversal_scanner import find_best_option
        ticker = request.args.get("ticker", "AAPL")
        signal_type = request.args.get("type", "bullish")
        price = float(request.args.get("price", 327.0))
        
        opt = find_best_option(ticker, signal_type, price)
        return jsonify({
            "ok": True,
            "ticker": ticker,
            "signal_type": signal_type,
            "price": price,
            "result": opt
        })
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
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
