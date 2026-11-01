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
    bb_mult = os.getenv("BB_MULT", "2.5")
    grade_str = grade or ("A+" if (score and score >= 12) else "A")
    grade_icon = "⭐️" if grade_str == "A+" else "🔥"

    # Strictly enforce: Only make trades that are A+ setups
    only_a_plus = os.getenv("ONLY_A_PLUS_SETUPS", "true").lower() in ("true", "1", "yes")
    if only_a_plus and grade_str != "A+":
        logger.info(f"[{ticker}] ⚠️ Skipping trade execution: Grade {grade_str} is not A+.")
        return False

    sl_str = f"${stop_loss:.2f}" if stop_loss else "N/A"
    tp1_str = f"${target_1:.2f}" if target_1 else "N/A"
    logger.info(f"🔔 {grade_str} SIGNAL TRIGGERED on {ticker}: {action} | Setup: {reason} | Price=${last_price:.2f}, VWAP=${vwap_target:.2f}, 8EMA={f'${ema8:.2f}' if ema8 else 'N/A'} | SL={sl_str} | TP1={tp1_str}")
    
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

    # Format Zone and Sentiment Badges
    zone_str = f"🧱 {zone_summary}" if zone_summary and zone_summary != "None" else "None"
    if in_zone and zone_summary and zone_summary != "None":
        zone_str += " 🔥 <b>In Zone</b>"

    align_badge = sentiment_align or "Neutral"
    align_icon = "🎯" if "Aligned" in align_badge else ("🔄" if "Counter" in align_badge else "⚪")
    tech_str = f"📊 {tech_trend}" if tech_trend else "📊 Neutral"
    
    send_reversal_alert = os.getenv("SEND_REVERSAL_ALERTS", "false").lower() in ("true", "1", "yes")

    exit_8ema_label = f"Completed 5m Candle Close Below 8 EMA (${ema8:.2f})" if (signal_type == "bullish" and ema8) else (f"Completed 5m Candle Close Above 8 EMA (${ema8:.2f})" if ema8 else "Completed 5m Candle Close Past 8 EMA")

    # 1. Send SMS Notification (only if reversal alerts explicitly enabled)
    if send_reversal_alert and alert_method in ("SMS", "BOTH"):
        zone_sms = f"Zone: {zone_summary}{' (In Zone)' if in_zone else ''}\n" if zone_summary and zone_summary != "None" else ""
        sms_msg = (
            f"{grade_icon} {grade_str} SETUP: {ticker}\n"
            f"Action: {action} ({signal_type.upper()})\n"
            f"Setup: {reason or (action + ' Retest')}\n"
            f"Price: ${last_price:.2f} | VWAP: ${vwap_target:.2f} | 8 EMA: ${ema8:.2f}\n"
            f"SL: {sl_str} | TP1: {tp1_str}\n"
            f"Exit Rule: {exit_8ema_label}\n"
            f"Suggested Option: {opt_str}"
        )
        send_sms_notification(sms_msg)
        
    # Helper to escape HTML characters in dynamic data
    def _esc(val):
        if val is None: return ""
        return str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 2. Send Telegram Reversal Notification (only if reversal alerts explicitly enabled)
    if send_reversal_alert and alert_method in ("TELEGRAM", "BOTH"):
        rsi_formatted = f"{rsi:.1f}" if rsi is not None else "N/A"
        rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
        tg_msg = (
            f"🚨 <b>{grade_icon} {grade_str} STRATEGY ALERT: {ticker}</b> 🚨\n\n"
            f"<b>Action:</b> {action} ({signal_type.upper()})\n"
            f"<b>Setup:</b> {_esc(reason or (action + ' Retest'))}\n"
            f"<b>Price:</b> ${last_price:.2f}\n"
            f"<b>VWAP:</b> ${vwap_target:.2f} | <b>8 EMA:</b> ${ema8:.2f}\n"
            f"<b>Stop Loss:</b> {_esc(sl_str)} | <b>TP1:</b> {_esc(tp1_str)}\n"
            f"<b>Exit Rule:</b> 🏃 {_esc(exit_8ema_label)}\n"
            f"<b>Suggested Option:</b> {_esc(opt_str)}"
        )
        send_telegram_notification(tg_msg)
    
    # 3. Place Paper Trade & Send Execution Notification
    trade_executed = False
    try:
        from paper_trader import get_paper_trader
        pt = get_paper_trader()
        if pt.enabled:
            trade_signal = "bullish" if action == "BUY" else "bearish"
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
                zone_title = "VWAP TREND PULLBACK"
                logger.info(f"📈 {grade_str} {zone_title} Paper trade placed for {ticker}: {result.get('option_symbol', '')} @ ${result.get('entry_price', 0):.2f} ({mode_str})")
                
                # Send trade notification via same alert method
                trade_msg = (
                    f"📈 {grade_icon} {grade_str} {zone_title} TRADE PLACED: {ticker}\n"
                    f"Option: {result.get('type', '')} ${result.get('strike', '')} ({result.get('option_symbol', '')})\n"
                    f"Price: ${result.get('entry_price', 0):.2f}\n"
                    f"Qty: {result.get('quantity', 1)} contract(s)\n"
                    f"Stock: ${last_price:.2f} | VWAP: ${vwap_target:.2f} | 8 EMA: ${ema8:.2f}\n"
                    f"Stop Loss: {sl_str} | TP1: {tp1_str} (50% Scale-Out)\n"
                    f"Runner Exit: Completed 5m candle close across 8 EMA\n"
                    f"Setup: {reason or action}\n"
                    f"Mode: {mode_str}"
                )
                if alert_method in ("SMS", "BOTH"):
                    send_sms_notification(trade_msg)
                if alert_method in ("TELEGRAM", "BOTH"):
                    rsi_formatted = f"{rsi:.1f}" if rsi is not None else "N/A"
                    rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
                    tg_trade = (
                        f"📈 <b>{grade_icon} {grade_str} {zone_title} TRADE PLACED: {ticker}</b>\n\n"
                        f"<b>Option:</b> {result.get('type', '')} ${result.get('strike', '')} ({_esc(result.get('option_symbol', ''))})\n"
                        f"<b>Entry Price:</b> ${result.get('entry_price', 0):.2f}\n"
                        f"<b>Qty:</b> {result.get('quantity', 1)} contract(s)\n"
                        f"<b>Stock Price:</b> ${last_price:.2f} | <b>VWAP:</b> ${vwap_target:.2f} | <b>8 EMA:</b> ${ema8:.2f}\n"
                        f"<b>Stop Loss:</b> {_esc(sl_str)} | <b>TP1:</b> {_esc(tp1_str)} (50% Scale-Out)\n"
                        f"<b>Runner Exit:</b> 🏃 5m candle close across 8 EMA\n"
                        f"<b>Setup:</b> {_esc(reason or action)}\n"
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
    Strategy Blueprint: VWAP Trend Pullback
    1. Core Timeframes: 5-minute chart for entries; 15-minute resampled chart for macro trend confirmation.
    2. Ideal Window: 9:45 AM – 11:30 AM EST (allows morning VWAP to stabilize after the open).
    3. Rule 1 (Daylight Filter): Never trade flat moving averages. 8 EMA must maintain position on trending
       side of VWAP with daylight for at least 3 consecutive candles.
    4. Rule 2: Respect 9:30–9:45 AM noise (first 15 minutes whipsaws blocked).
    5. Rule 3 (Rubber Band Filter): Price not stretched > 2.5 ATRs away from 8 EMA.
    6. Long Setup (Bullish Momentum):
       - Price cleanly above VWAP (Close > VWAP).
       - 8 EMA sloping upward and positioned above VWAP (EMA8 > VWAP).
       - Macro 15m trend confirmation.
       - Pullback: Retraces toward 8 EMA or pocket between 8 EMA and VWAP.
       - Trigger: Rejection tail (hammer or close back above 8 EMA) on expanding volume.
       - SL: 1-2 ticks below rejection swing low or just beneath VWAP.
       - TP1: Prior swing high / HOD (50% scale-out). Runner: Trails 8 EMA.
    7. Short Setup (Bearish Momentum):
       - Price cleanly below VWAP (Close < VWAP).
       - 8 EMA sloping downward and positioned below VWAP (EMA8 < VWAP).
       - Macro 15m trend confirmation.
       - Pullback: Rallies up to test underside of 8 EMA or pocket between 8 EMA and VWAP.
       - Trigger: Topping tail / bearish close back below 8 EMA on expanding volume.
       - SL: 1-2 ticks above rejection swing high or just above VWAP.
       - TP1: Prior intraday low / LOD (50% scale-out). Runner: Trails 8 EMA.
    """
    if df is None or len(df) < 5:
        return None

    try:
        # Filter today's regular market hours session
        today_date = df.index[-1].date()
        today_df = df[df.index.date == today_date]
        
        pm_bars = today_df.between_time("04:00", "09:29:59")
        rth_bars = today_df.between_time("09:30", "16:00")
        
        if len(rth_bars) < 3:
            return None

        # 2. Execution Window & Rule 2: Respect 9:30-9:45 AM noise. Only trigger between 09:45 AM and 11:30 AM EST
        last_dt = rth_bars.index[-1]
        ny_tz = get_ny_timezone()
        if hasattr(last_dt, "tzinfo") and last_dt.tzinfo is not None:
            dt_ny = last_dt.tz_convert(ny_tz)
        else:
            dt_ny = last_dt
        mins_from_midnight = dt_ny.hour * 60 + dt_ny.minute
        earliest_mins = int(os.getenv("EARLIEST_TRADE_TIME_MINS", "585"))  # 09:45 AM
        latest_mins = int(os.getenv("LATEST_TRADE_TIME_MINS", "690"))    # 11:30 AM
        if mins_from_midnight < earliest_mins or mins_from_midnight > latest_mins:
            return None
            
        # 3. Key Levels to Calculate Daily
        pm_high = float(pm_bars["High"].max()) if not pm_bars.empty else 0.0
        pm_low = float(pm_bars["Low"].min()) if not pm_bars.empty else 0.0
        hod = float(rth_bars["High"].max())
        lod = float(rth_bars["Low"].min())
        
        # Indicator 1: RTH Market Hours VWAP (Calculated strictly from 09:30 AM market open)
        typical_price_rth = (rth_bars["High"] + rth_bars["Low"] + rth_bars["Close"]) / 3.0
        cum_vol_rth = rth_bars["Volume"].cumsum().replace(0, 1)
        vwap_series = (typical_price_rth * rth_bars["Volume"]).cumsum() / cum_vol_rth
        current_vwap = float(vwap_series.iloc[-1])
        
        # Indicator 2: 8 EMA on 5-minute regular market hours candles
        ema8_series = rth_bars["Close"].ewm(span=8, adjust=False).mean()
        current_ema8 = float(ema8_series.iloc[-1])
        prev_ema8 = float(ema8_series.iloc[-2]) if len(ema8_series) >= 2 else current_ema8
        
        # Indicator 3: 14 ATR on 5m candles for Rule 3 (Rubber Band Filter)
        hl = rth_bars["High"] - rth_bars["Low"]
        hc = (rth_bars["High"] - rth_bars["Close"].shift(1)).abs()
        lc = (rth_bars["Low"] - rth_bars["Close"].shift(1)).abs()
        tr = hl.combine(hc, max).combine(lc, max)
        atr_series = tr.rolling(14, min_periods=1).mean()
        current_atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 1.0

        # Indicator 4: 15-Minute Macro Confirmation (Resampled from 5m)
        rth_15m = rth_bars.resample("15min").agg({
            "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
        }).dropna()
        long_15m_confirmed = True
        short_15m_confirmed = True
        if len(rth_15m) >= 2:
            tp_15 = (rth_15m["High"] + rth_15m["Low"] + rth_15m["Close"]) / 3.0
            cv_15 = rth_15m["Volume"].cumsum().replace(0, 1)
            vwap_15m_series = (tp_15 * rth_15m["Volume"]).cumsum() / cv_15
            ema8_15m_series = rth_15m["Close"].ewm(span=8, adjust=False).mean()
            last_15m_c = float(rth_15m["Close"].iloc[-1])
            last_15m_vwap = float(vwap_15m_series.iloc[-1])
            last_15m_ema8 = float(ema8_15m_series.iloc[-1])
            long_15m_confirmed = (last_15m_c >= last_15m_vwap * 0.999) and (last_15m_ema8 >= last_15m_vwap * 0.998)
            short_15m_confirmed = (last_15m_c <= last_15m_vwap * 1.001) and (last_15m_ema8 <= last_15m_vwap * 1.002)

        last_row = rth_bars.iloc[-1]
        close_price = float(last_row["Close"])
        open_price = float(last_row["Open"])
        high_price = float(last_row["High"])
        low_price = float(last_row["Low"])
        vol = float(last_row["Volume"])
        prev_vol = float(rth_bars.iloc[-2]["Volume"]) if len(rth_bars) >= 2 else vol

        bar_range = high_price - low_price
        if bar_range <= 0:
            return None

        # Rule 3: The "Rubber Band" Rule
        # If price is stretched more than 2.5 ATRs away from the 8 EMA, never initiate a new trend position
        if current_atr > 0 and abs(close_price - current_ema8) > (2.5 * current_atr):
            return None

        # Rule 1: Never trade flat moving averages
        # Check last 3 candles: 8 EMA must maintain position on trending side of VWAP with daylight
        ema8_slice = ema8_series.iloc[-3:] if len(ema8_series) >= 3 else ema8_series
        vwap_slice = vwap_series.iloc[-3:] if len(vwap_series) >= 3 else vwap_series

        is_long = False
        is_short = False
        confluence_tag = ""
        stop_loss_px = 0.0
        target_1_px = 0.0

        # 4. Long Setup Rules (Bullish Momentum):
        # 1. Trend & Value Filter: Price trading cleanly above VWAP, 8 EMA sloping upward and positioned above VWAP
        # 2. Daylight: 8 EMA consistently above VWAP across recent bars
        # 3. Pullback: Candle tests 8 EMA or pocket between 8 EMA and VWAP
        # 4. Trigger: Rejection tail (hammer or close back above 8 EMA) on expanding volume
        is_long_trend = (
            (close_price > current_vwap) and
            (current_ema8 > current_vwap * 1.0003) and
            (current_ema8 >= prev_ema8) and
            (ema8_slice > vwap_slice).all() and
            long_15m_confirmed
        )

        if is_long_trend:
            pullback_tested = (low_price <= current_ema8 * 1.0015) and (low_price >= current_vwap * 0.998)
            lower_wick = min(open_price, close_price) - low_price
            is_hammer = (lower_wick >= 0.35 * bar_range) or ((close_price - low_price) >= 0.55 * bar_range)
            bullish_close = (close_price > current_ema8) and (close_price >= open_price)
            vol_expanding = (vol >= prev_vol * 0.85)

            if pullback_tested and (is_hammer or bullish_close) and vol_expanding:
                is_long = True
                confluence_tag = "★ 8 EMA Hammer Rejection" if is_hammer else "8 EMA Bullish Pullback Close"
                if low_price <= (current_vwap * 1.001):
                    confluence_tag = "★ 8 EMA + VWAP Pocket Retest"
                stop_loss_px = min(low_price, current_vwap) - 0.05
                target_1_px = hod

        # 5. Short Setup Rules (Bearish Momentum):
        # 1. Trend & Value Filter: Price trading cleanly below VWAP, 8 EMA sloping downward and positioned below VWAP
        # 2. Daylight: 8 EMA consistently below VWAP across recent bars
        # 3. Pullback: Candle tests underside of 8 EMA or pocket between 8 EMA and VWAP
        # 4. Trigger: Rejection tail (topping tail or close back below 8 EMA) on expanding volume
        is_short_trend = (
            (close_price < current_vwap) and
            (current_ema8 < current_vwap * 0.9997) and
            (current_ema8 <= prev_ema8) and
            (ema8_slice < vwap_slice).all() and
            short_15m_confirmed
        )

        if is_short_trend and not is_long:
            pullback_tested = (high_price >= current_ema8 * 0.9985) and (high_price <= current_vwap * 1.002)
            upper_wick = high_price - max(open_price, close_price)
            is_topping_tail = (upper_wick >= 0.35 * bar_range) or ((high_price - close_price) >= 0.55 * bar_range)
            bearish_close = (close_price < current_ema8) and (close_price <= open_price)
            vol_expanding = (vol >= prev_vol * 0.85)

            if pullback_tested and (is_topping_tail or bearish_close) and vol_expanding:
                is_short = True
                confluence_tag = "★ 8 EMA Topping Tail Rejection" if is_topping_tail else "8 EMA Bearish Pullback Close"
                if high_price >= (current_vwap * 0.999):
                    confluence_tag = "★ 8 EMA + VWAP Pocket Retest"
                stop_loss_px = max(high_price, current_vwap) + 0.05
                target_1_px = lod

        if not (is_long or is_short):
            return None

        action = "BUY" if is_long else "SELL"
        signal_type = "bullish" if is_long else "bearish"
        
        # Find liquid option contract
        best_opt = None
        try:
            from reversal_scanner import find_best_option
            best_opt = find_best_option(ticker, signal_type, close_price)
        except Exception as ex:
            logger.debug(f"Option search error for {ticker}: {ex}")
            
        if not best_opt:
            logger.info(f"[{ticker}] ⚠️ {action} Setup at VWAP (${current_vwap:.2f}), but no liquid option contract found.")
            return None
            
        # Compute technical confluences for scoring and display
        rsi_val = None
        rvol = 1.0
        try:
            from indicator import compute_rsi
            from reversal_scanner import compute_rvol
            rsi_s = compute_rsi(rth_bars["Close"], 14)
            if len(rsi_s) > 0 and not np.isnan(rsi_s.iloc[-1]):
                rsi_val = float(rsi_s.iloc[-1])
            rvol = compute_rvol(rth_bars)
        except Exception:
            pass
            
        # Market Sentiment
        sentiment_data = get_cached_market_sentiment()
        m_sentiment = sentiment_data.get('sentiment', 'Neutral')
        m_summary = sentiment_data.get('summary', '')
        
        sentiment_align = "Market Aligned 🎯" if (
            (is_long and m_sentiment == "Bullish") or (is_short and m_sentiment == "Bearish")
        ) else "Counter-Trend 🔄"
        
        setup_reason = (
            f"VWAP Trend Pullback | Above VWAP (${current_vwap:.2f}) + 8EMA Rising (${current_ema8:.2f}) | {confluence_tag} 🚀"
            if is_long else
            f"VWAP Trend Pullback | Below VWAP (${current_vwap:.2f}) + 8EMA Falling (${current_ema8:.2f}) | {confluence_tag} 🩸"
        )
        
        score = 16
        if "★" in confluence_tag:
            score += 3
        if sentiment_align.startswith("Market Aligned"):
            score += 2
        if rvol > 1.2:
            score += 1
            
        grade = "A+"
        
        logger.info(
            f"[{ticker}] 🎯 {grade} {action} SIGNAL! Price: ${close_price:.2f} | "
            f"VWAP: ${current_vwap:.2f} | 8EMA: ${current_ema8:.2f} | SL: ${stop_loss_px:.2f} | TP1: ${target_1_px:.2f} | {setup_reason}"
        )
        
        return {
            'action': action,
            'type': signal_type,
            'price': close_price,
            'vwap': round(current_vwap, 2),
            'ema8': round(current_ema8, 2),
            'stop_loss': round(stop_loss_px, 2),
            'target_1': round(target_1_px, 2),
            'pm_high': round(pm_high, 2),
            'pm_low': round(pm_low, 2),
            'hod': round(hod, 2),
            'lod': round(lod, 2),
            'confluence': confluence_tag,
            'rsi': rsi_val or 50.0,
            'rvol': rvol,
            'score': score,
            'grade': grade,
            'reason': setup_reason,
            'time': rth_bars.index[-1],
            'zone_summary': f"VWAP ${current_vwap:.2f} / 8EMA ${current_ema8:.2f}",
            'in_zone': True,
            'zone_details': {'pm_high': pm_high, 'pm_low': pm_low, 'hod': hod, 'lod': lod, 'stop_loss': stop_loss_px, 'target_1': target_1_px},
            'sentiment_align': sentiment_align,
            'tech_trend': "Bullish" if is_long else "Bearish",
            'market_sentiment': m_sentiment,
            'market_sentiment_summary': m_summary,
            'suggested_option': best_opt,
            'macd_cross': False,
            'macd_status': "Bullish" if is_long else "Bearish",
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
    logger.info("Starting background 3-Sigma alert bot loop (A+ Setups Only)...")
    
    while True:
        try:
            if not is_market_hours():
                logger.info("Market is closed (weekends or outside 9:30 AM - 4:15 PM EST). Bot sleeping for 5 minutes...")
                time.sleep(300)
                continue

            # Opening bell check (trading begins at 9:30 AM ET market open)
            ny_tz = get_ny_timezone()
            now = datetime.now(ny_tz)
            current_mins = now.hour * 60 + now.minute
            earliest_trade_mins = int(os.getenv("EARLIEST_TRADE_TIME_MINS", "570"))  # 9:30 AM ET (market open)
            if current_mins < earliest_trade_mins:
                open_hr = earliest_trade_mins // 60
                open_mn = earliest_trade_mins % 60
                suffix = "AM" if open_hr < 12 else "PM"
                disp_hr = open_hr if open_hr <= 12 else open_hr - 12
                if disp_hr == 0:
                    disp_hr = 12
                logger.info(f"Outside trading window (trading begins at {disp_hr}:{open_mn:02d} {suffix} ET). Bot sleeping for 30s...")
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

            latest_trade_mins = int(os.getenv("LATEST_TRADE_TIME_MINS", "660"))  # 11:00 AM ET morning cutoff
            if current_mins > latest_trade_mins:
                close_hr = latest_trade_mins // 60
                close_mn = latest_trade_mins % 60
                suffix = "AM" if close_hr < 12 else "PM"
                disp_hr = close_hr if close_hr <= 12 else close_hr - 12
                if disp_hr == 0:
                    disp_hr = 12
                logger.info(f"Trading window closed for new entries (trades permitted 9:30 AM - {disp_hr}:{close_mn:02d} {suffix} ET). Monitoring open positions only. Sleeping 60s...")
                time.sleep(60)
                continue

            logger.info("--- Starting 3-Sigma A+ Reversal Bot Cycle ---")

            try:
                # 1. Determine tickers to scan: Default to Top 50 Stocks
                tickers_mode = os.getenv("TICKERS_3SIGMA", "TOP50").strip().upper()
                selected_tickers = []
                try:
                    if tickers_mode in ("TOP50", "TOP_50"):
                        top50_file = os.path.join(_SCAN_DATA_DIR, "top50_stocks.json")
                        if not os.path.exists(top50_file):
                            top50_file = os.path.join(os.path.dirname(__file__), "top50_stocks.json")
                        if os.path.exists(top50_file):
                            with open(top50_file, "r") as f:
                                data = json.load(f)
                                selected_tickers = [item["ticker"] for item in data if isinstance(item, dict) and item.get("ticker")]
                    else:
                        watchlist_file = os.path.join(_SCAN_DATA_DIR, "watchlist.json")
                        if not os.path.exists(watchlist_file):
                            watchlist_file = os.path.join(os.path.dirname(__file__), "watchlist.json")
                        if os.path.exists(watchlist_file):
                            with open(watchlist_file, "r") as f:
                                selected_tickers = json.load(f)
                except Exception as e:
                    logger.error(f"Failed to load tickers for {tickers_mode}: {e}")

                tickers = list(dict.fromkeys([t.strip().upper() for t in selected_tickers if t and isinstance(t, str)]))
                if not tickers:
                    tickers = ["NVDA", "TSLA", "AAPL", "AMZN", "MSFT", "META", "AMD", "SPY", "QQQ"]

                candle_interval = os.getenv("CANDLE_INTERVAL_3SIGMA", "5m")
                scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
                
                # 2. Pre-calculate daily bands
                precalculate_daily_bands(tickers)
                
                logger.info(f"Scanning {len(tickers)} {tickers_mode} tickers in parallel ({candle_interval} candles, PM Breakout + VWAP Retest)...")

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
                        if grade != "A+" and score < 12:
                            logger.info(f"[{ticker}] ⚠️ Skipping non-A+ setup (Grade: {grade}, Score: {score})")
                            continue

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
                            "mode": "3sigma",
                            "timestamp": datetime.now(ny_tz).strftime("%b %d, %Y  %I:%M %p"),
                            "count": len(all_signals),
                            "market_sentiment": current_sentiment,
                            "results": all_signals
                        }, f, indent=2)
                except Exception as save_err:
                    logger.warning(f"Could not save 3sigma bot results: {save_err}")

                logger.info(f"--- 3-Sigma Bot Cycle Complete. New A+ alerts: {triggered_count}, Suppressed (already alerted today): {skipped_count}. Sleeping for {scan_interval}s ---")
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

