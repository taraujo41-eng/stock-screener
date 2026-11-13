import os
import sys
import time
import logging
import smtplib
import threading
import pytz
import json
import traceback as tb

import numpy as np

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
    
    # Only attach console stream if stdout is an interactive terminal and not redirected to log
    if sys.stdout.isatty():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)

# Global map to store pre-calculated daily bands: {ticker: (upper_bb_daily, lower_bb_daily)}
_daily_bands_map = {}
# Track the date when daily bands were calculated to cache results
_daily_bands_last_date = None

# Market sentiment cache (refreshed periodically)
_market_sentiment_cache = None
_market_sentiment_last_time = 0

def get_cached_market_sentiment():
    """Fetches real-time SPY, QQQ, and VIX sentiment, caching for 5 minutes."""
    global _market_sentiment_cache, _market_sentiment_last_time
    now = time.time()
    if _market_sentiment_cache and (now - _market_sentiment_last_time) < 300:
        return _market_sentiment_cache
    try:
        from reversal_scanner import get_market_sentiment
        s = get_market_sentiment()
        if s and isinstance(s, dict):
            _market_sentiment_cache = s
            _market_sentiment_last_time = now
            return _market_sentiment_cache
    except Exception as e:
        logger.warning(f"Error fetching market sentiment for bot: {e}")
    return _market_sentiment_cache or {
        "sentiment": "Neutral",
        "label": "NEUTRAL ⚪",
        "score": 50,
        "is_bullish": False,
        "summary": "Market Sentiment: Neutral (50/100)"
    }
# Broad Market (SPY/QQQ) Session VWAP Alignment Cache
_index_vwap_cache = None
_index_vwap_last_time = 0

def get_market_index_alignment(max_age_seconds=45):
    """
    Checks if broad market (SPY or QQQ) is trading above or below session VWAP.
    Session VWAP is calculated strictly from 09:30 AM ET market open.
    Caches results for 45s to avoid duplicate network calls in scan cycles.
    """
    global _index_vwap_cache, _index_vwap_last_time
    now = time.time()
    if _index_vwap_cache and (now - _index_vwap_last_time) < max_age_seconds:
        return _index_vwap_cache

    try:
        from data_fetcher import fetch_one
        spy_df = fetch_one("SPY", days=2, interval="5m", includePrePost="true")
        qqq_df = fetch_one("QQQ", days=2, interval="5m", includePrePost="true")

        # Resilient fallback to yfinance if Webull data is unavailable or empty
        if spy_df is None or len(spy_df) < 2:
            try:
                import yfinance as yf
                spy_df = yf.Ticker("SPY").history(period="2d", interval="5m", prepost=True)
            except Exception:
                pass

        if qqq_df is None or len(qqq_df) < 2:
            try:
                import yfinance as yf
                qqq_df = yf.Ticker("QQQ").history(period="2d", interval="5m", prepost=True)
            except Exception:
                pass

        def _check_vwap(df):
            if df is None or len(df) < 2:
                return None, 0.0, 0.0
            today_date = df.index[-1].date()
            today_df = df[df.index.date == today_date]
            rth = today_df.between_time("09:30", "16:00")
            if len(rth) < 1:
                return None, 0.0, 0.0
            tp = (rth["High"] + rth["Low"] + rth["Close"]) / 3.0
            cum_vol = rth["Volume"].cumsum().replace(0, 1)
            vwap = (tp * rth["Volume"]).cumsum() / cum_vol
            last_c = float(rth["Close"].iloc[-1])
            cur_vwap = float(vwap.iloc[-1])
            return (last_c >= cur_vwap), last_c, cur_vwap

        spy_above, spy_c, spy_vwap = _check_vwap(spy_df)
        qqq_above, qqq_c, qqq_vwap = _check_vwap(qqq_df)

        if spy_above is None and qqq_above is None:
            long_ok = True
            short_ok = True
        else:
            long_ok = (spy_above is True) or (qqq_above is True)
            short_ok = (spy_above is False) or (qqq_above is False)

        summary_parts = []
        if spy_above is not None:
            summary_parts.append(f"SPY {'Above' if spy_above else 'Below'} VWAP (${spy_c:.2f}/${spy_vwap:.2f})")
        if qqq_above is not None:
            summary_parts.append(f"QQQ {'Above' if qqq_above else 'Below'} VWAP (${qqq_c:.2f}/${qqq_vwap:.2f})")

        _index_vwap_cache = {
            "spy_above_vwap": spy_above,
            "qqq_above_vwap": qqq_above,
            "long_aligned": long_ok,
            "short_aligned": short_ok,
            "summary": " | ".join(summary_parts) if summary_parts else "Index VWAP: Neutral"
        }
        _index_vwap_last_time = now
        return _index_vwap_cache
    except Exception as e:
        logger.warning(f"Error checking market index VWAP: {e}")
        return {
            "spy_above_vwap": None,
            "qqq_above_vwap": None,
            "long_aligned": True,
            "short_aligned": True,
            "summary": "Index VWAP: Neutral"
        }
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

def _daily_alert_count():
    """Count how many alerts have fired today."""
    ny_tz = get_ny_timezone()
    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    return sum(1 for info in _alerted_today.values() if isinstance(info, dict) and info.get("date") == today_str)

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
    alert_method = os.getenv("ALERT_METHOD", "TELEGRAM").upper()
    if alert_method not in ("SMS", "BOTH"):
        return

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

def send_telegram_notification(message, max_retries=3):
    """Sends a notification to Telegram with automatic retries and plain text fallback."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
        
    import requests, re
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    # 1. Try sending with HTML formatting (retrying on timeouts/network drops)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=(5, 25))
            if resp.status_code == 200:
                logger.info(f"Telegram alert delivered successfully (attempt {attempt}).")
                return True
            else:
                logger.warning(f"Telegram API non-200 (attempt {attempt}): {resp.status_code} {resp.text}")
                # If Telegram rejected HTML markup (e.g. parse error), break early to plain text
                break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
            logger.warning(f"Telegram timeout/network error (attempt {attempt}/{max_retries}): {net_err}")
            if attempt < max_retries:
                time.sleep(2)
        except Exception as e:
            logger.error(f"Telegram error on attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(2)

    # 2. Plain text fallback if HTML or network had issues
    plain_text = re.sub(r'<[^>]+>', '', message)
    fb_payload = {
        "chat_id": chat_id,
        "text": plain_text
    }
    for attempt in range(1, max_retries + 1):
        try:
            fb_resp = requests.post(url, json=fb_payload, timeout=(5, 25))
            if fb_resp.status_code == 200:
                logger.info("Telegram alert delivered successfully via plain text fallback.")
                return True
            else:
                logger.error(f"Telegram fallback failed (attempt {attempt}): {fb_resp.text}")
        except Exception as fb_err:
            logger.error(f"Telegram fallback error (attempt {attempt}/{max_retries}): {fb_err}")
            if attempt < max_retries:
                time.sleep(2)
                
    return False

def trigger_alerts(ticker, action, signal_type, last_price, vwap_target, rsi=None, reason=None, score=None, grade=None, rvol=None, zone_summary=None, in_zone=False, sentiment_align=None, market_sentiment_summary=None, tech_trend=None, hod=None, lod=None, pm_high=None, pm_low=None, ema8=None, confluence=None, stop_loss=None, target_1=None):
    grade_str = grade or ("A+" if (score and score >= 19) else "A")
    grade_icon = "⭐️" if grade_str == "A+" else "🔥"

    # Strictly enforce: Only make trades that are A+ setups
    only_a_plus = os.getenv("ONLY_A_PLUS_SETUPS", "true").lower() in ("true", "1", "yes")
    if only_a_plus and grade_str != "A+":
        logger.info(f"[{ticker}] ⚠️ Skipping trade execution: Grade {grade_str} is not A+.")
        return False

    sl_str = f"${stop_loss:.2f}" if stop_loss else "N/A"
    tp1_str = f"${target_1:.2f}" if target_1 else "N/A"
    pmh_str = f"${pm_high:.2f}" if pm_high else "N/A"
    pml_str = f"${pm_low:.2f}" if pm_low else "N/A"
    zone_title = "5-MIN PREMARKET BREAKOUT (PMB)"

    logger.info(f"🔔 {grade_str} {zone_title} SIGNAL on {ticker}: {action} | Setup: {reason} | Price=${last_price:.2f}, VWAP=${vwap_target:.2f}, 8EMA={f'${ema8:.2f}' if ema8 else 'N/A'} | Hard SL={sl_str} | 2R Target={tp1_str}")
    
    alert_method = os.getenv("ALERT_METHOD", "TELEGRAM").upper()
    
    # 0. Find best option contract suggestion (PMB specs: 0-30 DTE, Delta 0.45-0.60, OI >= 500, Spread <= $0.10/2%)
    opt_str = "None"
    opt_info = None
    try:
        from reversal_scanner import find_best_option
        opt_info = find_best_option(
            ticker,
            signal_type,
            last_price,
            min_dte=int(os.getenv("OPTIONS_MIN_DTE", "0")),
            max_dte=int(os.getenv("OPTIONS_MAX_DTE", "30")),
            target_delta_min=0.45,
            target_delta_max=0.60,
            min_oi=500,
            max_spread_dollars=float(os.getenv("OPTIONS_MAX_SPREAD_DOLLARS", "0.10")),
            max_spread_pct=2.0
        )
        if opt_info:
            opt_str = f"{opt_info.get('exp', '')} ${opt_info.get('strike', '')} {opt_info.get('type', '')} (@${opt_info.get('mid', 0):.2f}) [DTE:{opt_info.get('dte')} Δ:{opt_info.get('est_delta', '0.50')} OI:{opt_info.get('oi', 'N/A')}]"
    except Exception as e:
        logger.warning(f"Option lookup failed for {ticker}: {e}")

    send_reversal_alert = os.getenv("SEND_REVERSAL_ALERTS", "false").lower() in ("true", "1", "yes")

    struct_invalidation = f"5m candle close below PMH ({pmh_str})" if signal_type == "bullish" else f"5m candle close above PML ({pml_str})"

    # 1. Send SMS Notification
    if send_reversal_alert and alert_method in ("SMS", "BOTH"):
        action_label = "BUY CALL" if signal_type == "bullish" else "BUY PUT"
        sms_msg = (
            f"{grade_icon} {grade_str} {zone_title}: {ticker}\n"
            f"Action: {action_label} ({signal_type.upper()})\n"
            f"Price: ${last_price:.2f} | PMH: {pmh_str} | PML: {pml_str}\n"
            f"VWAP: ${vwap_target:.2f} | 8 EMA: ${ema8:.2f}\n"
            f"Hard SL: {sl_str} | 2R Target: {tp1_str}\n"
            f"Early Invalidation: {struct_invalidation}\n"
            f"Option: {opt_str}"
        )
        send_sms_notification(sms_msg)
        
    # Helper to escape HTML characters in dynamic data
    def _esc(val):
        if val is None: return ""
        return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 2. Send Telegram Notification
    if send_reversal_alert and alert_method in ("TELEGRAM", "BOTH"):
        rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
        action_label = "BUY CALL" if signal_type == "bullish" else "BUY PUT"
        tg_msg = (
            f"🚨 <b>{grade_icon} {grade_str} {zone_title}: {ticker}</b> 🚨\n\n"
            f"<b>Action:</b> {action_label} ({signal_type.upper()})\n"
            f"<b>Setup:</b> {_esc(reason or action)}\n"
            f"<b>Price:</b> ${last_price:.2f}\n"
            f"<b>Premarket Range:</b> PMH {pmh_str} | PML {pml_str}\n"
            f"<b>Intraday Anchors:</b> VWAP ${vwap_target:.2f} | 8 EMA ${ema8:.2f}\n"
            f"<b>Relative Volume (RVOL):</b> {rvol_formatted} (≥ 1.5x Volume SMA)\n"
            f"<b>Hard Emergency Stop:</b> {_esc(sl_str)}\n"
            f"<b>Profit Target (2R):</b> {_esc(tp1_str)} (2:1 Asymmetry)\n"
            f"<b>Early Invalidation:</b> 🏃 {_esc(struct_invalidation)}\n"
            f"<b>Market Filter:</b> {_esc(market_sentiment_summary or 'SPY/QQQ Confirmed 🎯')}\n"
            f"<b>Suggested Option:</b> {_esc(opt_str)}"
        )
        send_telegram_notification(tg_msg)
    
    # 3. Place Paper Trade & Send Execution Notification
    trade_executed = False
    try:
        from paper_trader import get_paper_trader
        pt = get_paper_trader()
        if pt.enabled:
            trade_signal = signal_type
            result = pt.place_option_order(
                ticker=ticker,
                signal_type=trade_signal,
                last_price=last_price,
                vwap_target=vwap_target,
                zone_summary=zone_summary,
                in_zone=in_zone,
                setup_reason=reason,
                hod=hod,
                lod=lod,
                pmh=pm_high,
                pml=pm_low,
                ema8=ema8,
                stop_loss_price=stop_loss,
                target_1_price=target_1
            )
            if result:
                trade_executed = True
                mode_str = result.get('mode', 'Simulation')
                feed_str = f"{'LIVE' if result.get('is_realtime') else 'DELAYED'} ({result.get('quote_source', 'Unknown')})"
                logger.info(f"📈 {grade_str} {zone_title} Paper trade placed for {ticker}: {result.get('option_symbol', '')} @ ${result.get('entry_price', 0):.2f} ({mode_str} | {feed_str})")
                
                # Send trade notification via same alert method
                trade_msg = (
                    f"📈 {grade_icon} {grade_str} {zone_title} TRADE PLACED: {ticker}\n"
                    f"Option: {result.get('type', '')} ${result.get('strike', '')} ({result.get('option_symbol', '')})\n"
                    f"Price: ${result.get('entry_price', 0):.2f}\n"
                    f"Qty: {result.get('quantity', 1)} contract(s)\n"
                    f"Stock: ${last_price:.2f} | PMH: {pmh_str} | PML: {pml_str}\n"
                    f"Hard SL: {sl_str} | 2R Target: {tp1_str}\n"
                    f"Early Invalidation: {struct_invalidation}\n"
                    f"Data Feed: {feed_str}\n"
                    f"Mode: {mode_str}"
                )
                if alert_method in ("SMS", "BOTH"):
                    send_sms_notification(trade_msg)
                if alert_method in ("TELEGRAM", "BOTH"):
                    rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
                    tg_trade = (
                        f"📈 <b>{grade_icon} {grade_str} {zone_title} TRADE PLACED: {ticker}</b>\n\n"
                        f"<b>Option:</b> {result.get('type', '')} ${result.get('strike', '')} ({_esc(result.get('option_symbol', ''))})\n"
                        f"<b>Entry Price:</b> ${result.get('entry_price', 0):.2f}\n"
                        f"<b>Qty:</b> {result.get('quantity', 1)} contract(s)\n"
                        f"<b>Stock Price:</b> ${last_price:.2f} | <b>VWAP:</b> ${vwap_target:.2f} | <b>8 EMA:</b> ${ema8:.2f}\n"
                        f"<b>PMH:</b> {pmh_str} | <b>PML:</b> {pml_str}\n"
                        f"<b>Hard Emergency Stop:</b> {_esc(sl_str)}\n"
                        f"<b>Profit Target (2R):</b> {_esc(tp1_str)} (2:1 Asymmetry)\n"
                        f"<b>Early Invalidation:</b> 🏃 {_esc(struct_invalidation)}\n"
                        f"<b>Setup:</b> {_esc(reason or action)}\n"
                        f"<b>Data Feed:</b> {_esc(feed_str)}\n"
                        f"<b>Mode:</b> {_esc(mode_str)}"
                    )
                    send_telegram_notification(tg_trade)
        else:
            trade_executed = True
    except Exception as e:
        logger.error(f"Paper trade error for {ticker}: {e}")
        import traceback
        traceback.print_exc()

    return trade_executed

def evaluate_ticker_process(ticker, df):
    """
    TRADING STRATEGY SPECIFICATION: 5-MINUTE PREMARKET BREAKOUT (PMB)
    
    1. Overview:
       - Asset Class: Large-Cap / High Liquidity Equities & Directional Options (Market Cap >= $10B)
       - Execution Timeframe: 5-Minute Chart
       - Session Reference Window: 04:00 - 09:30 ET (Premarket High / Low)
       - Active Trading Window: 09:30 - 11:30 ET
       - Edge: Opening expansion momentum outside established premarket balance area.
    
    2. Key Levels & Parameters:
       - PMH: Highest price printed between 04:00 and 09:30 ET.
       - PML: Lowest price printed between 04:00 and 09:30 ET.
       - Intraday Anchors: VWAP (from 09:30 AM open) and 8 EMA.
       - Relative Volume (RVOL): Breakout candle volume >= 1.5x 20-period Volume SMA.
       - Market Filter: SPY or QQQ trend alignment (Must hold above VWAP for Longs, below for Shorts).
    
    3. Entry Rules:
       - Long Setup:
         1. Time: 09:30 - 11:30 ET.
         2. Trigger: 5m candle closes strictly above PMH (Close > PMH).
         3. Confirmations:
            - Breakout volume >= 1.5x 20-period Volume SMA.
            - Price trading above VWAP and above 8 EMA.
            - Broad market (SPY/QQQ) trading above session VWAP.
         4. Execution: Enter at close of confirming 5m candle.
       - Short Setup:
         1. Time: 09:30 - 11:30 ET.
         2. Trigger: 5m candle closes strictly below PML (Close < PML).
         3. Confirmations:
            - Breakout volume >= 1.5x 20-period Volume SMA.
            - Price trading below VWAP and below 8 EMA.
            - Broad market (SPY/QQQ) trading below session VWAP.
         4. Execution: Enter at close of confirming 5m candle.
    
    4. Risk Management & Exits:
       - Structural Stop (Early Invalidation): Close < PMH (Long) / Close > PML (Short).
       - Hard Emergency Stop (Max Loss Protection):
         * Long: low of breakout candle OR Entry - (1.0 * ATR(14)), whichever is tighter (higher).
         * Short: high of breakout candle OR Entry + (1.0 * ATR(14)), whichever is tighter (lower).
       - Profit Target (2:1 Asymmetry / 2R):
         * Long: Entry + (2.0 * R)
         * Short: Entry - (2.0 * R)
    """
    if df is None or len(df) < 5:
        return None

    try:
        today_date = df.index[-1].date()
        today_df = df[df.index.date == today_date]

        pm_bars = today_df.between_time("04:00", "09:29:59")
        rth_bars = today_df.between_time("09:30", "16:00")

        if pm_bars.empty or len(rth_bars) < 1:
            return None

        pm_high = float(pm_bars["High"].max())
        pm_low = float(pm_bars["Low"].min())

        if pm_high <= 0 or pm_low <= 0 or pm_high <= pm_low:
            return None

        # Premarket range must be clearly established (filter flat/dead premarket sessions)
        pm_range_pct = ((pm_high - pm_low) / pm_low) * 100.0
        if pm_range_pct < 0.25:
            return None

        # 1. Active Trading Window: 09:30 - 11:30 ET
        last_dt = rth_bars.index[-1]
        ny_tz = get_ny_timezone()
        if hasattr(last_dt, "tzinfo") and last_dt.tzinfo is not None:
            dt_ny = last_dt.tz_convert(ny_tz)
        else:
            dt_ny = last_dt
        mins_from_midnight = dt_ny.hour * 60 + dt_ny.minute
        earliest_mins = int(os.getenv("EARLIEST_TRADE_TIME_MINS", "570"))  # 09:30 AM ET
        latest_mins = int(os.getenv("LATEST_TRADE_TIME_MINS", "690"))      # 11:30 AM ET
        if mins_from_midnight < earliest_mins or mins_from_midnight > latest_mins:
            return None

        # 2. Intraday Anchors
        # VWAP calculated strictly from 09:30 AM open
        typical_price_rth = (rth_bars["High"] + rth_bars["Low"] + rth_bars["Close"]) / 3.0
        cum_vol_rth = rth_bars["Volume"].cumsum().replace(0, 1)
        vwap_series = (typical_price_rth * rth_bars["Volume"]).cumsum() / cum_vol_rth
        current_vwap = float(vwap_series.iloc[-1])

        # 8 EMA on 5m candles
        ema8_series = rth_bars["Close"].ewm(span=8, adjust=False).mean()
        current_ema8 = float(ema8_series.iloc[-1])

        # 3. 20-Period Volume SMA on 5-Minute Candles
        rth_all = df.between_time("09:30", "16:00")
        vol_sma20_series = rth_all["Volume"].rolling(20, min_periods=5).mean()
        vol_sma20 = float(vol_sma20_series.iloc[-1]) if len(vol_sma20_series) > 0 else 1.0

        # 4. ATR(14) for Hard Emergency Stop
        hl = df["High"] - df["Low"]
        hc = (df["High"] - df["Close"].shift(1)).abs()
        lc = (df["Low"] - df["Close"].shift(1)).abs()
        tr = hl.combine(hc, max).combine(lc, max)
        atr_series = tr.rolling(14, min_periods=5).mean()
        current_atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else (rth_bars["High"].iloc[-1] - rth_bars["Low"].iloc[-1])
        if current_atr <= 0:
            current_atr = max(0.20, float(rth_bars["Close"].iloc[-1]) * 0.005)

        last_bar = rth_bars.iloc[-1]
        close_price = float(last_bar["Close"])
        open_price = float(last_bar["Open"])
        high_price = float(last_bar["High"])
        low_price = float(last_bar["Low"])
        vol = float(last_bar["Volume"])
        rvol = vol / max(1.0, vol_sma20)

        hod = float(rth_bars["High"].max())
        lod = float(rth_bars["Low"].min())

        # 5. Market Filter: SPY or QQQ trend alignment
        market_align = get_market_index_alignment()
        long_market_ok = market_align.get("long_aligned", True)
        short_market_ok = market_align.get("short_aligned", True)

        is_long = False
        is_short = False
        hard_stop_px = 0.0
        target_2r_px = 0.0
        r_risk = 0.0

        # Long Setup Evaluation (Monday Rules)
        if (close_price > pm_high) and (rvol >= 1.5) and (close_price > current_vwap) and (close_price > current_ema8) and long_market_ok:
            is_long = True
            atr_stop = close_price - (1.0 * current_atr)
            hard_stop_px = max(low_price, atr_stop)
            if hard_stop_px >= close_price:
                hard_stop_px = close_price - (0.5 * current_atr)
            r_risk = max(0.05, close_price - hard_stop_px)
            target_2r_px = close_price + (2.0 * r_risk)

        # Short Setup Evaluation (Monday Rules)
        elif (close_price < pm_low) and (rvol >= 1.5) and (close_price < current_vwap) and (close_price < current_ema8) and short_market_ok:
            is_short = True
            atr_stop = close_price + (1.0 * current_atr)
            hard_stop_px = min(high_price, atr_stop)
            if hard_stop_px <= close_price:
                hard_stop_px = close_price + (0.5 * current_atr)
            r_risk = max(0.05, hard_stop_px - close_price)
            target_2r_px = close_price - (2.0 * r_risk)

        if not (is_long or is_short):
            return None

        action = "BUY" if is_long else "SELL"
        signal_type = "bullish" if is_long else "bearish"

        # 6. Derivative / Option Contract Lookup (PMB Specs: 0-30 DTE, Delta 0.45-0.60, OI >= 500, Spread <= $0.10/2%)
        best_opt = None
        try:
            from reversal_scanner import find_best_option
            best_opt = find_best_option(
                ticker=ticker,
                signal_type=signal_type,
                last_price=close_price,
                min_dte=int(os.getenv("OPTIONS_MIN_DTE", "0")),
                max_dte=int(os.getenv("OPTIONS_MAX_DTE", "30")),
                target_delta_min=0.45,
                target_delta_max=0.60,
                min_oi=500,
                max_spread_dollars=float(os.getenv("OPTIONS_MAX_SPREAD_DOLLARS", "0.10")),
                max_spread_pct=2.0
            )
        except Exception as ex:
            logger.debug(f"Option search error for {ticker}: {ex}")

        if not best_opt:
            logger.info(f"[{ticker}] ⚠️ {signal_type.upper()} PMB Breakout detected @ ${close_price:.2f}, but no liquid 0-30 DTE option contract found.")
            return None

        # 7. Rigorous A+ Scoring Formula
        score = 14
        if rvol >= 2.0:
            score += 3
        elif rvol >= 1.5:
            score += 2

        if is_long and market_align.get("spy_above_vwap") and market_align.get("qqq_above_vwap"):
            score += 3
        elif is_short and (market_align.get("spy_above_vwap") is False) and (market_align.get("qqq_above_vwap") is False):
            score += 3
        else:
            score += 1

        if is_long and current_ema8 > current_vwap:
            score += 2
        elif is_short and current_ema8 < current_vwap:
            score += 2

        bar_range = high_price - low_price
        if bar_range > 0:
            if is_long and ((close_price - low_price) / bar_range) >= 0.70:
                score += 2
            elif is_short and ((high_price - close_price) / bar_range) >= 0.70:
                score += 2

        grade = "A+" if score >= 19 else "A"

        if is_long:
            setup_reason = f"5m PMB Long | Closed Above PMH (${pm_high:.2f}) | RVOL {rvol:.1f}x | Above VWAP (${current_vwap:.2f}) + 8EMA (${current_ema8:.2f}) | 2R Target: ${target_2r_px:.2f} 🚀"
            confluence_tag = f"PMH Breakout (${pm_high:.2f}) + RVOL {rvol:.1f}x"
        else:
            setup_reason = f"5m PMB Short | Closed Below PML (${pm_low:.2f}) | RVOL {rvol:.1f}x | Below VWAP (${current_vwap:.2f}) + 8EMA (${current_ema8:.2f}) | 2R Target: ${target_2r_px:.2f} 🩸"
            confluence_tag = f"PML Breakdown (${pm_low:.2f}) + RVOL {rvol:.1f}x"

        logger.info(
            f"[{ticker}] 🎯 {grade} PMB {action} SIGNAL! Price: ${close_price:.2f} | "
            f"PMH: ${pm_high:.2f} | PML: ${pm_low:.2f} | VWAP: ${current_vwap:.2f} | 8EMA: ${current_ema8:.2f} | "
            f"RVOL: {rvol:.1f}x | Hard SL: ${hard_stop_px:.2f} | 2R Target: ${target_2r_px:.2f}"
        )

        return {
            'action': action,
            'type': signal_type,
            'price': close_price,
            'vwap': round(current_vwap, 2),
            'ema8': round(current_ema8, 2),
            'stop_loss': round(hard_stop_px, 2),
            'target_1': round(target_2r_px, 2),
            'pm_high': round(pm_high, 2),
            'pm_low': round(pm_low, 2),
            'r_risk': round(r_risk, 2),
            'hod': round(hod, 2),
            'lod': round(lod, 2),
            'confluence': confluence_tag,
            'rsi': 50.0,
            'rvol': round(rvol, 1),
            'score': score,
            'grade': grade,
            'reason': setup_reason,
            'time': rth_bars.index[-1],
            'zone_summary': f"PMH ${pm_high:.2f} / PML ${pm_low:.2f}",
            'in_zone': True,
            'zone_details': {'pm_high': pm_high, 'pm_low': pm_low, 'hod': hod, 'lod': lod, 'stop_loss': hard_stop_px, 'target_1': target_2r_px, 'r_risk': r_risk},
            'sentiment_align': "Market Aligned 🎯" if (is_long and long_market_ok) or (is_short and short_market_ok) else "Neutral",
            'tech_trend': "Bullish Breakout" if is_long else "Bearish Breakdown",
            'market_sentiment': "Bullish" if long_market_ok else ("Bearish" if short_market_ok else "Neutral"),
            'market_sentiment_summary': market_align.get("summary", ""),
            'suggested_option': best_opt,
            'strategy': "PMB",
        }
    except Exception as e:
        logger.error(f"Error evaluating ticker {ticker}: {e}\n{tb.format_exc()}")
        return None

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
    logger.info("Starting background 5-Minute Premarket Breakout (PMB) alert bot loop (A+ Setups Only)...")
    
    while True:
        try:
            if not is_market_hours():
                logger.info("Market is closed (weekends or outside 9:30 AM - 4:15 PM EST). Bot sleeping for 5 minutes...")
                time.sleep(300)
                continue

            # Trading window check: 9:30 AM to 11:30 AM ET (scans & trade entries only within window)
            ny_tz = get_ny_timezone()
            now = datetime.now(ny_tz)
            current_mins = now.hour * 60 + now.minute
            earliest_trade_mins = int(os.getenv("EARLIEST_TRADE_TIME_MINS", "570"))  # 9:30 AM ET
            if current_mins < earliest_trade_mins:
                open_hr = earliest_trade_mins // 60
                open_mn = earliest_trade_mins % 60
                suffix = "AM" if open_hr < 12 else "PM"
                disp_hr = open_hr if open_hr <= 12 else open_hr - 12
                if disp_hr == 0:
                    disp_hr = 12
                logger.info(f"Outside trading window (PMB scanning & trading begins at {disp_hr}:{open_mn:02d} {suffix} ET). Bot sleeping for 30s...")
                time.sleep(30)
                continue

            # Check daily trade limit (max 5 trades/day) before scanning
            try:
                from paper_trader import get_paper_trader
                pt = get_paper_trader()
                if pt.enabled and pt._daily_trade_count() >= pt.max_daily_trades:
                    logger.info(f"Daily limit of {pt.max_daily_trades} trades reached for today ({pt._daily_trade_count()}/{pt.max_daily_trades}). Monitoring open positions only. Sleeping 60s...")
                    time.sleep(60)
                    continue
            except Exception:
                pass

            latest_trade_mins = int(os.getenv("LATEST_TRADE_TIME_MINS", "690"))  # 11:30 AM ET cutoff
            if current_mins > latest_trade_mins:
                close_hr = latest_trade_mins // 60
                close_mn = latest_trade_mins % 60
                suffix = "AM" if close_hr < 12 else "PM"
                disp_hr = close_hr if close_hr <= 12 else close_hr - 12
                if disp_hr == 0:
                    disp_hr = 12
                logger.info(f"Trading window closed for new entries (PMB trades permitted 9:30 AM - {disp_hr}:{close_mn:02d} {suffix} ET). Monitoring open positions only. Sleeping 60s...")
                time.sleep(60)
                continue

            logger.info("--- Starting 5-Minute Premarket Breakout (PMB) Bot Cycle ---")

            try:
                # 1. Determine tickers to scan: Strictly Top 50 Stocks list
                selected_tickers = []
                try:
                    top50_file = os.path.join(_SCAN_DATA_DIR, "top50_stocks.json")
                    if not os.path.exists(top50_file):
                        top50_file = os.path.join(os.path.dirname(__file__), "top50_stocks.json")
                    if os.path.exists(top50_file):
                        with open(top50_file, "r") as f:
                            data = json.load(f)
                            selected_tickers = [item["ticker"] for item in data if isinstance(item, dict) and item.get("ticker")]
                except Exception as e:
                    logger.error(f"Failed to load Top 50 tickers: {e}")

                tickers = list(dict.fromkeys([t.strip().upper() for t in selected_tickers if t and isinstance(t, str)]))
                if not tickers:
                    tickers = ["NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META", "AMD", "SPY", "QQQ"]

                candle_interval = os.getenv("CANDLE_INTERVAL_3SIGMA", "5m")
                scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
                
                # 2. Pre-calculate daily bands (cached)
                precalculate_daily_bands(tickers)
                
                logger.info(f"Scanning {len(tickers)} Top 50 tickers in parallel ({candle_interval} candles, 5-Minute PM Breakout (PMB))...")

                # 3. Download and compute in parallel
                results = fetch_batch_concurrent(
                    tickers=tickers,
                    days=5,
                    max_workers=25,
                    interval=candle_interval,
                    includePrePost="true",
                    process_fn=evaluate_ticker_process,
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
                            "VWAP Level": res['vwap'],
                            "Profit Target": "+20% (Option TP)",
                            "Stop Loss": "-20% (Option SL)",
                            "Supply/Demand Zone": res.get('zone_summary', 'None'),
                            "In Zone": res.get('in_zone', False),
                            "Zone Details": res.get('zone_details'),
                            "Market Sentiment": res.get('market_sentiment', 'Neutral'),
                            "Market Sentiment Score": res.get('market_sentiment_score', 50),
                            "Sentiment Alignment": res.get('sentiment_align', 'Neutral'),
                            "Technical Trend": res.get('tech_trend', 'Neutral'),
                            "MACD": res.get('macd_status', 'Neutral'),
                        })

                        # Strictly enforce ONLY A+ setups
                        grade = res.get('grade')
                        score = res.get('score', 0)
                        if grade != "A+":
                            logger.info(f"[{ticker}] ⚠️ Skipping non-A+ setup (Grade: {grade}, Score: {score})")
                            continue

                        # Enforce daily trade limit (Ultra-Selective: Max 5 daily trades)
                        max_daily_trades = int(os.getenv("MAX_DAILY_TRADES", "5"))
                        if _daily_alert_count() >= max_daily_trades:
                            logger.info(f"[{ticker}] ⚠️ Daily A+ trade limit reached ({_daily_alert_count()}/{max_daily_trades}). Skipping new trade entry.")
                            break

                        direction = res['action']  # "BUY" or "SELL"
                        if _already_alerted_today(ticker, direction):
                            skipped_count += 1
                            continue
                            
                        placed = trigger_alerts(
                            ticker=ticker,
                            action=res['action'],
                            signal_type=res['type'],
                            last_price=res['price'],
                            vwap_target=res['vwap'],
                            rsi=res.get('rsi'),
                            reason=res.get('reason'),
                            score=res.get('score'),
                            grade=res.get('grade'),
                            rvol=res.get('rvol'),
                            zone_summary=res.get('zone_summary'),
                            in_zone=res.get('in_zone', False),
                            sentiment_align=res.get('sentiment_align'),
                            market_sentiment_summary=res.get('market_sentiment_summary'),
                            tech_trend=res.get('tech_trend'),
                            hod=res.get('hod'),
                            lod=res.get('lod'),
                            pm_high=res.get('pm_high'),
                            pm_low=res.get('pm_low'),
                            ema8=res.get('ema8'),
                            confluence=res.get('confluence'),
                            stop_loss=res.get('stop_loss'),
                            target_1=res.get('target_1')
                        )
                        if placed:
                            _record_alert(ticker, direction, res['price'])
                            triggered_count += 1

                # Save 3-sigma scan results for web UI persistence
                try:
                    ny_tz = get_ny_timezone()
                    current_sentiment = get_cached_market_sentiment()
                    save_file = os.path.join(_SCAN_DATA_DIR, "last_3sigma_scan.json")
                    with open(save_file, "w") as f:
                        json.dump({
                            "ok": True,
                            "mode": "pmb",
                            "timestamp": datetime.now(ny_tz).strftime("%b %d, %Y  %I:%M %p"),
                            "count": len(all_signals),
                            "market_sentiment": current_sentiment,
                            "results": all_signals
                        }, f, indent=2)
                except Exception as save_err:
                    logger.warning(f"Could not save PMB bot results: {save_err}")

                logger.info(f"--- 5-Minute PMB Bot Cycle Complete. New A+ alerts: {triggered_count}, Suppressed (already alerted today): {skipped_count}. Sleeping for {scan_interval}s ---")
            except Exception as bot_err:
                logger.error(f"Error during bot scan cycle: {bot_err}")

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
                try:
                    from data_fetcher import test_connection
                    diag = test_connection("SPY")
                    if diag.get("realtime_options_ready"):
                        logger.info("✅ Real-time Webull options market data verified active on startup.")
                    else:
                        opt_err = diag.get("webull_openapi_options_error") or diag.get("webull_unofficial_options_mode")
                        logger.warning(f"⚠️ Real-time options data NOT active on startup ({opt_err}).")
                        if not pt.allow_delayed_options:
                            pt._notify_delayed_data_once("Bot startup check (SPY)", str(opt_err))
                except Exception as diag_err:
                    logger.warning(f"Startup market-data check failed: {diag_err}")

                # If requested, trigger a test trade on startup to verify execution and alert pipelines
                if os.getenv("EXECUTE_TEST_TRADE_ON_START", "false").lower() in ("true", "1", "yes"):
                    def _delayed_test_trade():
                        time.sleep(5)
                        try:
                            logger.info("⚡ Executing automatic startup test trade on SPY to verify bot is operational...")
                            pt.execute_test_trade(ticker="SPY", signal_type="bullish")
                        except Exception as ex:
                            logger.error(f"Error executing startup test trade: {ex}")
                    threading.Thread(target=_delayed_test_trade, daemon=True).start()
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

