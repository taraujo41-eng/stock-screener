import os
import sys
import time
import logging
import smtplib
import threading
import pytz
import json
import traceback as tb

def get_ny_timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return pytz.timezone("America/New_York")
from email.mime.text import MIMEText
from datetime import datetime

# Load parent directory to allow imports
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_APP_DIR)
sys.path.insert(0, _APP_DIR)

from indicator import calculate_3_sigma_divergence
from data_fetcher import fetch_batch_concurrent

# Set up local log file
logger = logging.getLogger("3sigma_bot")
logger.setLevel(logging.INFO)
logger.propagate = False  # Prevent duplicate log lines from root logger propagation

# Make sure we don't duplicate handlers if script is reloaded
if not logger.handlers:
    handler = logging.FileHandler(os.path.join(os.path.dirname(__file__), "3sigma_bot.log"))
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    # Also log to standard out
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

# Global map to store pre-calculated daily bands: {ticker: (upper_bb_daily, lower_bb_daily)}
_daily_bands_map = {}
# Track the date when daily bands were calculated to cache results
_daily_bands_last_date = None
# Daily alert cooldown: {ticker: {"direction": "BUY"|"SELL", "date": "YYYY-MM-DD", "price": float}}
_SCAN_DATA_DIR = os.environ.get("SCAN_DATA_DIR", os.path.dirname(__file__))
_ALERTED_TODAY_FILE = os.path.join(_SCAN_DATA_DIR, "alerted_today.json")
_alerted_today = {}
_alerted_today_lock = threading.Lock()

def _load_alerted_today():
    """Load daily alert state from disk (survives container restarts)."""
    global _alerted_today
    try:
        if os.path.exists(_ALERTED_TODAY_FILE):
            with open(_ALERTED_TODAY_FILE, "r") as f:
                _alerted_today = json.load(f)
            logger.info(f"Loaded {len(_alerted_today)} alerted tickers from disk.")
    except Exception as e:
        logger.warning(f"Could not load alerted_today.json: {e}")
        _alerted_today = {}

def _save_alerted_today():
    """Persist daily alert state to disk."""
    try:
        with open(_ALERTED_TODAY_FILE, "w") as f:
            json.dump(_alerted_today, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save alerted_today.json: {e}")

def _purge_old_alerts():
    """Remove entries from previous trading days so tickers can alert again."""
    global _alerted_today
    ny_tz = get_ny_timezone()
    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    stale = [t for t, info in _alerted_today.items() if info.get("date") != today_str]
    if stale:
        for t in stale:
            del _alerted_today[t]
        _save_alerted_today()
        logger.info(f"Purged {len(stale)} stale alert entries from previous days.")

def _already_alerted_today(ticker, direction):
    """Check if this ticker+direction already fired today. Direction flip is allowed."""
    ny_tz = get_ny_timezone()
    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    info = _alerted_today.get(ticker)
    if not info:
        return False
    return info.get("date") == today_str and info.get("direction") == direction

def _record_alert(ticker, direction, price):
    """Record that this ticker+direction alerted today."""
    ny_tz = get_ny_timezone()
    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    _alerted_today[ticker] = {
        "direction": direction,
        "date": today_str,
        "price": price,
    }
    _save_alerted_today()

# Load persisted state on module import
_load_alerted_today()

def send_sms_notification(message):
    """Sends a text message alert using Yahoo SMTP and mobile carrier SMS gateway."""
    gateway = os.getenv("SMS_GATEWAY_EMAIL")
    yahoo_pwd = os.getenv("YAHOO_APP_PASSWORD")
    if not gateway or not yahoo_pwd:
        logger.warning("SMS notification skipped: credentials or gateway not configured in .env")
        return
        
    from_email = "taraujo99@yahoo.com"
    msg = MIMEText(message)
    msg["Subject"] = "Reversal Alert"
    msg["From"] = from_email
    msg["To"] = gateway
    
    try:
        smtp_server = "smtp.mail.yahoo.com"
        smtp_port = 465
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(from_email, yahoo_pwd)
            server.sendmail(from_email, [gateway], msg.as_string())
        logger.info(f"SMS notification sent successfully to {gateway}.")
    except Exception as e:
        logger.error(f"Failed to send SMS notification: {e}")

def send_telegram_notification(message):
    """Sends a notification to Telegram."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
        
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram API error: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")

def trigger_alerts(ticker, action, signal_type, last_price, vwap_target, rsi=None, reason=None, score=None, grade=None, rvol=None):
    bb_mult = os.getenv("BB_MULT", "3.0")
    logger.info(f"🔔 A+ SIGNAL TRIGGERED on {ticker}: {action} | Setup: {reason} | Price={last_price:.2f}, RSI={f'{rsi:.1f}' if rsi else 'N/A'}, Target VWAP={vwap_target:.2f}")
    
    alert_method = os.getenv("ALERT_METHOD", "TELEGRAM").upper()
    
    # 0. Find best option contract suggestion
    opt_str = "None"
    opt_info = None
    try:
        from reversal_scanner import find_best_option
        opt_info = find_best_option(ticker, signal_type, last_price)
        if opt_info:
            opt_str = f"{opt_info.get('exp', '')} ${opt_info.get('strike', '')} {opt_info.get('type', '')} (@${opt_info.get('mid', 0):.2f})"
    except Exception as e:
        logger.warning(f"Option lookup failed for {ticker}: {e}")
    
    # 1. Send SMS Notification
    if alert_method in ("SMS", "BOTH"):
        sms_msg = (
            f"⭐️ A+ 3-SIGMA REVERSAL: {ticker}\n"
            f"Action: {action} ({signal_type.upper()})\n"
            f"Grade: ⭐️ A+ SETUP (Score: {score or 14}/18)\n"
            f"Setup: {reason or (action + ' Reversal')}\n"
            f"Price: ${last_price:.2f}\n"
            f"RSI: {f'{rsi:.1f}' if rsi else 'N/A'}\n"
            f"RVOL: {f'{rvol:.1f}x' if rvol else 'N/A'}\n"
            f"VWAP Target: ${vwap_target:.2f}\n"
            f"Suggested Option: {opt_str}"
        )
        send_sms_notification(sms_msg)
        
    # 2. Send Telegram Notification
    if alert_method in ("TELEGRAM", "BOTH"):
        rsi_formatted = f"{rsi:.1f}" if rsi is not None else "N/A"
        rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
        tg_msg = (
            f"🚨 <b>⭐️ A+ 3-SIGMA REVERSAL ALERT: {ticker}</b> 🚨\n\n"
            f"<b>Action:</b> {action} ({signal_type.upper()})\n"
            f"<b>Grade:</b> ⭐️ <b>A+ SETUP</b> (Score: {score or 14}/18)\n"
            f"<b>Setup:</b> {reason or '3-Sigma Breach Reversal'}\n"
            f"<b>Price:</b> ${last_price:.2f}\n"
            f"<b>RSI:</b> {rsi_formatted} (Divergence Confirmed)\n"
            f"<b>RVOL:</b> {rvol_formatted}\n"
            f"<b>VWAP Target:</b> ${vwap_target:.2f}\n"
            f"<b>Suggested Option:</b> {opt_str}"
        )
        send_telegram_notification(tg_msg)
    
    # 3. Place Paper Trade (if enabled)
    try:
        from paper_trader import get_paper_trader
        pt = get_paper_trader()
        if pt.enabled:
            trade_signal = "bullish" if action == "BUY" else "bearish"
            result = pt.place_option_order(
                ticker=ticker,
                signal_type=trade_signal,
                last_price=last_price,
                vwap_target=vwap_target
            )
            if result:
                mode_str = result.get('mode', 'Simulation')
                logger.info(f"📈 A+ Paper trade placed for {ticker}: {result.get('option_symbol', '')} @ ${result.get('entry_price', 0):.2f} ({mode_str})")
                
                # Send trade notification via same alert method
                trade_msg = (
                    f"📈 ⭐️ A+ PAPER TRADE PLACED: {ticker}\n"
                    f"Option: {result.get('type', '')} ${result.get('strike', '')} ({result.get('option_symbol', '')})\n"
                    f"Price: ${result.get('entry_price', 0):.2f}\n"
                    f"Qty: {result.get('quantity', 1)} contract(s)\n"
                    f"Mode: {mode_str}"
                )
                if alert_method in ("SMS", "BOTH"):
                    send_sms_notification(trade_msg)
                if alert_method in ("TELEGRAM", "BOTH"):
                    tg_trade = (
                        f"📈 <b>⭐️ A+ PAPER TRADE PLACED: {ticker}</b>\n\n"
                        f"<b>Option:</b> {result.get('type', '')} ${result.get('strike', '')}\n"
                        f"<b>Contract:</b> {result.get('option_symbol', '')}\n"
                        f"<b>Entry Price:</b> ${result.get('entry_price', 0):.2f}\n"
                        f"<b>Qty:</b> {result.get('quantity', 1)} contract(s)\n"
                        f"<b>Mode:</b> {mode_str}"
                    )
                    send_telegram_notification(tg_trade)
    except Exception as e:
        logger.error(f"Paper trade error for {ticker}: {e}")
        tb.print_exc()

def evaluate_ticker_process(ticker, df):
    """
    Called in parallel background threads to evaluate the 15m dataframe against Daily Bollinger Bands.
    Enforces 3.0-Sigma actual band breach (no proximity) and strict A+ Setup confirmation
    (Piercing + RSI Divergence + Confluence Score >= 12).
    """
    global _daily_bands_map
    
    daily_upper_bb = None
    daily_lower_bb = None
    
    if ticker in _daily_bands_map:
        daily_upper_bb, daily_lower_bb = _daily_bands_map[ticker]
    else:
        # Fallback if no daily bands pre-calculated
        return None
        
    bb_length = int(os.getenv("BB_LENGTH", "20"))
    bb_mult = float(os.getenv("BB_MULT", "3.0"))
    proximity_pct = float(os.getenv("PROXIMITY_PCT", "0.0"))
    only_a_plus = os.getenv("ONLY_A_PLUS_SETUPS", "true").lower() in ("true", "1", "yes")
    rsi_length = int(os.getenv("RSI_LENGTH", "14"))
    lookback = int(os.getenv("LOOKBACK", "15"))
    
    if len(df) < max(bb_length, rsi_length) + lookback + 5:
        return None
        
    # Compute Reversal indicators using pre-calculated daily bands
    df_ind = calculate_3_sigma_divergence(
        df,
        bb_length=bb_length,
        bb_mult=bb_mult,
        rsi_length=rsi_length,
        lookback=lookback,
        daily_upper_bb=daily_upper_bb,
        daily_lower_bb=daily_lower_bb,
        proximity_pct=proximity_pct
    )
    
    # Inspect latest state
    last_row = df_ind.iloc[-1]
    is_bullish_pierced = bool(last_row.get('is_bullish_pierced', False))
    is_bearish_pierced = bool(last_row.get('is_bearish_pierced', False))
    close_price = float(last_row['Close'])
    vwap_target = float(last_row['vwap'])
    rsi_val = float(last_row['rsi'])
    
    sd_label = f"{int(bb_mult)}SD" if bb_mult.is_integer() else f"{bb_mult}SD"

    # Strict check: Must actually breach the Daily Bollinger Band
    if not (is_bullish_pierced or is_bearish_pierced):
        return None

    # Calculate Confluence Factors for A+ Setup
    bull_div, bear_div = False, False
    rvol = 1.0
    ema20_dist = 0.0
    try:
        from reversal_scanner import detect_rsi_divergence, compute_rvol, compute_ema
        bull_div, bear_div = detect_rsi_divergence(df['Close'], df_ind['rsi'], lookback=lookback)
        rvol = compute_rvol(df)
        ema20_series = compute_ema(df['Close'], 20)
        ema20 = float(ema20_series.iloc[-1]) if len(ema20_series) > 0 else None
        ema20_dist = ((close_price - ema20) / ema20) * 100 if ema20 else 0.0
    except Exception as e:
        logger.warning(f"Error computing confluence for {ticker}: {e}")

    # Score Calculation: Pierced 3SD = 10, Divergence = +4, RSI Extreme = +2, RVOL = +2, EMA Ext = +1
    score = 10
    reasons_list = [f"Pierced Daily {'Lower' if is_bullish_pierced else 'Upper'} {sd_label} BB"]
    has_div = False
    
    if is_bullish_pierced:
        if bull_div:
            score += 4
            has_div = True
            reasons_list.append("RSI Bullish Divergence")
        if rsi_val <= 30:
            score += 2
            reasons_list.append(f"RSI Oversold ({rsi_val:.1f})")
        if rvol > 1.5:
            score += 2
            reasons_list.append(f"High RVOL ({rvol:.1f}x)")
        if ema20_dist < -2.0:
            score += 1
            reasons_list.append("EMA Extension")
    else:
        if bear_div:
            score += 4
            has_div = True
            reasons_list.append("RSI Bearish Divergence")
        if rsi_val >= 70:
            score += 2
            reasons_list.append(f"RSI Overbought ({rsi_val:.1f})")
        if rvol > 1.5:
            score += 2
            reasons_list.append(f"High RVOL ({rvol:.1f}x)")
        if ema20_dist > 2.0:
            score += 1
            reasons_list.append("EMA Extension")

    is_a_plus = (score >= 12 and has_div)
    grade = "A+" if is_a_plus else "A"
    reasons = " | ".join(reasons_list)

    if only_a_plus and not is_a_plus:
        logger.info(f"[{ticker} 15m] Price: {close_price:.2f} | Pierced {sd_label} BB (Score: {score}, Grade: {grade}) — Skipped (Requires A+ setup with RSI Divergence)")
        return None

    logger.info(f"[{ticker} 15m] 🔥 ⭐️ A+ 3-SIGMA REVERSAL CONFIRMED! Score: {score}, RSI: {rsi_val:.1f}, RVOL: {rvol:.1f}x | {reasons}")

    return {
        'action': 'BUY' if is_bullish_pierced else 'SELL',
        'type': 'bullish' if is_bullish_pierced else 'bearish',
        'price': close_price,
        'vwap': vwap_target,
        'rsi': rsi_val,
        'rvol': rvol,
        'score': score,
        'grade': 'A+',
        'reason': reasons,
        'time': df_ind.index[-1]
    }

def precalculate_daily_bands(tickers):
    """
    Fetches daily candles for all tickers in parallel and calculates their daily BB bands.
    Stores results in the global _daily_bands_map. Caches results for the day.
    """
    global _daily_bands_map, _daily_bands_last_date
    
    ny_tz = get_ny_timezone()
    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    
    # If we already have bands calculated today, only fetch missing ones (if any)
    if _daily_bands_map and _daily_bands_last_date == today_str:
        missing = [t for t in tickers if t not in _daily_bands_map]
        if not missing:
            logger.info(f"Using cached daily Bollinger Bands for all {len(tickers)} tickers (calculated today).")
            return
        else:
            logger.info(f"Daily bands cache missing {len(missing)} tickers. Fetching only those...")
            tickers_to_fetch = missing
    else:
        _daily_bands_map.clear()
        _daily_bands_last_date = today_str
        tickers_to_fetch = tickers

    if not tickers_to_fetch:
        return
        
    bb_length = int(os.getenv("BB_LENGTH", "20"))
    bb_mult = float(os.getenv("BB_MULT", "3.0"))
    
    logger.info(f"Pre-calculating daily Bollinger Bands for {len(tickers_to_fetch)} tickers...")
    
    # Fetch 1d candles (45 days is enough for 20 BB)
    daily_dfs = fetch_batch_concurrent(
        tickers=tickers_to_fetch,
        days=45,
        max_workers=25,
        interval="1d",
        includePrePost="false",
        skip_webull=False
    )
    
    for ticker, df in daily_dfs.items():
        if df is None or len(df) < bb_length:
            continue
        try:
            # Calculate Bollinger Bands on Daily Close
            middle = df['Close'].rolling(window=bb_length).mean()
            std = df['Close'].rolling(window=bb_length).std()
            upper = middle + bb_mult * std
            lower = middle - bb_mult * std
            
            # Check if last row is today (still forming)
            last_idx = df.index[-1]
            last_date_str = last_idx.strftime("%Y-%m-%d")
            
            if last_date_str == today_str and len(df) > 1:
                u_val = upper.iloc[-2]
                l_val = lower.iloc[-2]
            else:
                u_val = upper.iloc[-1]
                l_val = lower.iloc[-1]
                
            _daily_bands_map[ticker] = (float(u_val), float(l_val))
        except Exception as e:
            logger.error(f"Error calculating daily bands for {ticker}: {e}")
            
    logger.info(f"Successfully calculated/cached daily bands for {len(_daily_bands_map)} total tickers.")

def is_market_hours():
    """Returns True if current time is within regular market hours (9:30 AM to 4:15 PM EST, Mon-Fri)."""
    try:
        ny_tz = get_ny_timezone()
        now = datetime.now(ny_tz)
        
        # Weekends (Saturday=5, Sunday=6)
        if now.weekday() >= 5:
            return False
            
        # Convert to minutes since midnight
        current_minutes = now.hour * 60 + now.minute
        
        start_minutes = 9 * 60 + 30   # 9:30 AM
        end_minutes = 16 * 60 + 15    # 4:15 PM (15m buffer after close)
        
        return start_minutes <= current_minutes <= end_minutes
    except Exception as e:
        logger.error(f"Error checking market hours: {e}")
        return True  # Default to True on exception to ensure we don't block bot permanently



def bot_loop():
    logger.info("Starting background 3-Sigma alert bot loop (A+ Setups Only)...")
    
    while True:
        try:
            if not is_market_hours():
                logger.info("Market is closed (weekends or outside 9:30 AM - 4:15 PM EST). Bot sleeping for 5 minutes...")
                time.sleep(300)
                continue

            from reversal_scanner import acquire_scan_lock, release_scan_lock, scan_progress, _reset_progress, _update_progress
            if not acquire_scan_lock("Background-3Sigma-Bot"):
                logger.info("A user manual scan is currently running — skipping background bot cycle for 60s...")
                time.sleep(60)
                continue

            logger.info("--- Starting 3-Sigma A+ Reversal Bot Cycle ---")
            _reset_progress(status="running", mode="3sigma_bot")
            scan_progress["phase_label"] = "Initiating 3-Sigma Bot scan..."

            try:
                # 1. Determine tickers to scan: Combine all 287 US Optionable tickers AND Watchlist tickers
                from reversal_scanner import get_us_tickers
                try:
                    us_tickers = get_us_tickers()
                except Exception as e:
                    logger.error(f"Failed to load full US tickers list: {e}")
                    us_tickers = []

                watchlist_tickers = []
                try:
                    watchlist_file = os.path.join(_SCAN_DATA_DIR, "watchlist.json")
                    if not os.path.exists(watchlist_file):
                        watchlist_file = os.path.join(os.path.dirname(__file__), "watchlist.json")
                    if os.path.exists(watchlist_file):
                        with open(watchlist_file, "r") as f:
                            watchlist_tickers = json.load(f)
                except Exception as e:
                    logger.error(f"Failed to load watchlist.json: {e}")

                tickers = list(dict.fromkeys(us_tickers + watchlist_tickers))
                if not tickers:
                    tickers = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]

                candle_interval = os.getenv("CANDLE_INTERVAL_3SIGMA", "15m")
                scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
                
                # 2. Pre-calculate daily bands
                _update_progress("init", "Pre-calculating daily bands...", 0, len(tickers), pct=5)
                precalculate_daily_bands(tickers)
                
                logger.info(f"Scanning {len(tickers)} tickers in parallel ({candle_interval} candles, 3.0SD Breach + A+ Setups)...")
                _update_progress("downloading", f"Downloading candles for {len(tickers)} tickers...", 0, len(tickers), pct=10)

                def _on_bot_dl_progress(i, tot, sym):
                    pct = 10 + int((i / max(1, tot)) * 85)
                    _update_progress("downloading", f"3-Sigma Bot scanning {sym} ({i}/{tot})...", i, tot, ticker=sym, pct=pct)

                # 3. Download and compute in parallel
                results = fetch_batch_concurrent(
                    tickers=tickers,
                    days=15,
                    max_workers=25,
                    interval=candle_interval,
                    includePrePost="false",
                    process_fn=evaluate_ticker_process,
                    on_progress=_on_bot_dl_progress,
                    skip_webull=False
                )
                
                # 4. Process matches — skip tickers already alerted today (same direction)
                _purge_old_alerts()  # clear stale entries from previous days
                triggered_count = 0
                skipped_count = 0
                all_signals = []

                for ticker, res in results.items():
                    if res:
                        all_signals.append({
                            "Ticker": ticker,
                            "Direction": "Bullish" if res['action'] == "BUY" else "Bearish",
                            "Last Price": res['price'],
                            "RSI": res.get('rsi', 50),
                            "Score": res.get('score', 0),
                            "Grade": res.get('grade', 'B'),
                            "Bullish Signals": res['type'] if res['action'] == "BUY" else "—",
                            "Bearish Signals": res['type'] if res['action'] == "SELL" else "—",
                            "Patterns": res.get('reason', ''),
                            "RVOL": res.get('rvol', 1.0),
                            "Entry": res['price'],
                            "Profit Target": res['vwap'],
                        })

                        direction = res['action']  # "BUY" or "SELL"
                        if _already_alerted_today(ticker, direction):
                            skipped_count += 1
                            continue
                            
                        trigger_alerts(
                            ticker=ticker,
                            action=res['action'],
                            signal_type=res['type'],
                            last_price=res['price'],
                            vwap_target=res['vwap'],
                            rsi=res.get('rsi'),
                            reason=res.get('reason'),
                            score=res.get('score'),
                            grade=res.get('grade'),
                            rvol=res.get('rvol')
                        )
                        _record_alert(ticker, direction, res['price'])
                        triggered_count += 1

                # Save 3-sigma scan results for web UI persistence
                try:
                    ny_tz = get_ny_timezone()
                    save_file = os.path.join(_SCAN_DATA_DIR, "last_3sigma_scan.json")
                    with open(save_file, "w") as f:
                        json.dump({
                            "ok": True,
                            "mode": "3sigma",
                            "timestamp": datetime.now(ny_tz).strftime("%b %d, %Y  %I:%M %p"),
                            "count": len(all_signals),
                            "results": all_signals
                        }, f, indent=2)
                except Exception as save_err:
                    logger.warning(f"Could not save 3sigma bot results: {save_err}")

                scan_progress.update({
                    "status": "done",
                    "phase": "complete",
                    "phase_label": f"3-Sigma Bot cycle complete — {triggered_count} new alerts",
                    "pct": 100
                })
                logger.info(f"--- 3-Sigma Bot Cycle Complete. New A+ alerts: {triggered_count}, Suppressed (already alerted today): {skipped_count}. Sleeping for {scan_interval}s ---")
            finally:
                release_scan_lock()

            time.sleep(scan_interval)
            
        except Exception as e:
            logger.error(f"General exception in bot_loop: {e}")
            time.sleep(60)

def start_bot_thread():
    """Starts the bot loop and paper trader in daemon background threads."""
    
    # Main bot loop thread — runs scans during market hours
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    logger.info("3-Sigma background alert bot thread spawned.")
    
    # Paper trader — authenticate and start position monitor
    try:
        from paper_trader import get_paper_trader
        pt = get_paper_trader()
        if pt.enabled:
            if pt.login():
                pt.start_monitor_thread()
                logger.info("Paper trader initialized and position monitor started.")
            else:
                logger.warning("Paper trader login failed — auto-trading disabled.")
        else:
            logger.info("Paper trading is disabled (PAPER_TRADE_ENABLED=false).")
    except Exception as e:
        logger.error(f"Failed to initialize paper trader: {e}")


if __name__ == "__main__":
    logger.info("🤖 Starting 3-Sigma Indicator Bot process...")
    start_bot_thread()
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot process stopped.")

