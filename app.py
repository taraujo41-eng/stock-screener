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
    options_full_market_scan,
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
        "rsidiv": "RSI Divergence",
        "options": "A+ Options Plays"
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

# ── API: A+ Options Plays Scans (async) ──────────────────────────────

@app.route("/api/scan/options", methods=["POST"])
def scan_options():
    """Start a full market A+ options plays scan in the background."""
    global _scan_running

    with _scan_lock:
        if _scan_running:
            return _scan_conflict_response()
        _scan_running = True
        _reset_progress(status="running", mode="options")
        scan_progress["phase_label"] = "Initiating options scan..."

    req_data = request.get_json(silent=True) or {}
    extended_hours = req_data.get("extended_hours", False)

    def _run():
        global _scan_running
        try:
            et_tz = get_ny_timezone()
            df = options_full_market_scan(extended_hours=extended_hours)
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

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Options plays scan started"})

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
        
        # Test Yahoo Session directly
        logs.append("Testing Yahoo Finance cookie/crumb setup directly on server...")
        try:
            import requests
            test_sess = requests.Session()
            test_sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            })
            
            # Step 1: fc.yahoo.com
            try:
                r1 = test_sess.get("https://fc.yahoo.com", timeout=3)
                logs.append(f"  fc.yahoo.com returned status: {r1.status_code}")
                logs.append(f"  fc.yahoo.com cookies: {test_sess.cookies.get_dict()}")
            except Exception as e1:
                logs.append(f"  fc.yahoo.com failed: {e1}")
                
            # Step 2: getcrumb
            try:
                r2 = test_sess.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=3)
                logs.append(f"  getcrumb returned status: {r2.status_code}")
                logs.append(f"  getcrumb text: {r2.text.strip()}")
            except Exception as e2:
                logs.append(f"  getcrumb failed: {e2}")
        except Exception as e:
            logs.append(f"Yahoo diagnostic block failed: {e}")
        
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
                        
            if not chain or not has_data:
                logs.append("Webull chain empty/missing. Falling back to Yahoo Finance...")
                try:
                    from data_fetcher import _fetch_yahoo_options_chain, _fetch_yahoo_options_for_expiration
                    if "yahoo_meta" not in chain_meta:
                        logs.append("Fetching Yahoo options chain meta...")
                        chain_meta["yahoo_meta"] = _fetch_yahoo_options_chain(ticker)
                        logs.append(f"Yahoo options chain meta keys: {list(chain_meta['yahoo_meta'].keys()) if chain_meta['yahoo_meta'] else 'None'}")
                        
                    yahoo_meta = chain_meta.get("yahoo_meta")
                    if yahoo_meta:
                        closest_yahoo_exp = None
                        min_diff = 999999
                        for y_exp in yahoo_meta.get("expirations", []):
                            diff = abs(y_exp - exp_ts)
                            if diff < min_diff:
                                min_diff = diff
                                closest_yahoo_exp = y_exp
                        logs.append(f"Closest Yahoo exp: {datetime.fromtimestamp(closest_yahoo_exp).strftime('%Y-%m-%d') if closest_yahoo_exp else 'None'} | diff: {min_diff}")
                        if closest_yahoo_exp and min_diff < 86400 * 4:
                            logs.append("Fetching Yahoo options for expiration...")
                            chain = _fetch_yahoo_options_for_expiration(ticker, closest_yahoo_exp)
                            logs.append(f"Yahoo options fetched: {'dict' if isinstance(chain, dict) else 'None'}")
                except Exception as ye:
                    logs.append(f"Yahoo fallback failed: {ye}")
                    
            if not chain:
                logs.append("No chain data found (even from Yahoo)")
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
