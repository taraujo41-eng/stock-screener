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

def trigger_alerts(ticker, action, signal_type, last_price, vwap_target, rsi=None, reason=None, score=None, grade=None, rvol=None, zone_summary=None, in_zone=False, sentiment_align=None, market_sentiment_summary=None, tech_trend=None):
    bb_mult = os.getenv("BB_MULT", "2.5")
    grade_str = grade or ("A+" if (score and score >= 12) else "A")
    grade_icon = "⭐️" if grade_str == "A+" else "🔥"
    logger.info(f"🔔 {grade_str} SIGNAL TRIGGERED on {ticker}: {action} | Setup: {reason} | Price={last_price:.2f}, RSI={f'{rsi:.1f}' if rsi else 'N/A'}, Target VWAP={vwap_target:.2f}")
    
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

    # 1. Send SMS Notification (only if reversal alerts explicitly enabled)
    if send_reversal_alert and alert_method in ("SMS", "BOTH"):
        zone_sms = f"Zone: {zone_summary}{' (In Zone)' if in_zone else ''}\n" if zone_summary and zone_summary != "None" else ""
        sms_msg = (
            f"{grade_icon} {grade_str} REVERSAL: {ticker}\n"
            f"Action: {action} ({signal_type.upper()})\n"
            f"Grade: {grade_icon} {grade_str} SETUP (Score: {score or 10}/22)\n"
            f"Sentiment: {align_icon} {align_badge}\n"
            f"Tech Trend: {tech_str}\n"
            f"{zone_sms}"
            f"Setup: {reason or (action + ' Reversal')}\n"
            f"Price: ${last_price:.2f}\n"
            f"RSI: {f'{rsi:.1f}' if rsi else 'N/A'}\n"
            f"RVOL: {f'{rvol:.1f}x' if rvol else 'N/A'}\n"
            f"VWAP (Retest Level): ${vwap_target:.2f}\nTarget: TP +20% | SL -20%\n"
            f"Suggested Option: {opt_str}"
        )
        send_sms_notification(sms_msg)
        
    # 2. Send Telegram Reversal Notification (only if reversal alerts explicitly enabled)
    if send_reversal_alert and alert_method in ("TELEGRAM", "BOTH"):
        rsi_formatted = f"{rsi:.1f}" if rsi is not None else "N/A"
        rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
        div_note = " (Divergence Confirmed)" if "Divergence" in (reason or "") else ""
        tg_msg = (
            f"🚨 <b>{grade_icon} {grade_str} REVERSAL ALERT: {ticker}</b> 🚨\n\n"
            f"<b>Action:</b> {action} ({signal_type.upper()})\n"
            f"<b>Grade:</b> {grade_icon} <b>{grade_str} SETUP</b> (Score: {score or 10}/22)\n"
            f"<b>Market Sentiment:</b> {align_icon} {align_badge}\n"
            f"<b>Technical Trend:</b> {tech_str}\n"
            f"<b>Zone:</b> {zone_str}\n"
            f"<b>Setup:</b> {reason or (action + ' Reversal')}\n"
            f"<b>Price:</b> ${last_price:.2f}\n"
            f"<b>RSI:</b> {rsi_formatted}{div_note}\n"
            f"<b>RVOL:</b> {rvol_formatted}\n"
            f"<b>VWAP (Retest Level):</b> ${vwap_target:.2f}\n<b>Targets:</b> 🎯 Take Profit +20% | 🛑 Stop Loss -20%\n"
            f"<b>Suggested Option:</b> {opt_str}"
        )
        send_telegram_notification(tg_msg)
    
    # 3. Place Paper Trade & Send Execution Notification
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
                setup_reason=reason
            )
            if result:
                mode_str = result.get('mode', 'Simulation')
                zone_title = f"{'DEMAND' if signal_type == 'bullish' else 'SUPPLY'} ZONE" if in_zone else "REVERSAL"
                logger.info(f"📈 {grade_str} {zone_title} Paper trade placed for {ticker}: {result.get('option_symbol', '')} @ ${result.get('entry_price', 0):.2f} ({mode_str})")
                
                # Send trade notification via same alert method
                trade_msg = (
                    f"📈 {grade_icon} {grade_str} {zone_title} TRADE PLACED: {ticker}\n"
                    f"Option: {result.get('type', '')} ${result.get('strike', '')} ({result.get('option_symbol', '')})\n"
                    f"Price: ${result.get('entry_price', 0):.2f}\n"
                    f"Qty: {result.get('quantity', 1)} contract(s)\n"
                    f"Stock Price: ${last_price:.2f}\n"
                    f"VWAP (Retest Level): ${vwap_target:.2f}\nTarget: TP +20% | SL -20%\n"
                    f"Zone: {zone_summary if zone_summary and zone_summary != 'None' else 'N/A'}\n"
                    f"Setup: {reason or (action + ' Reversal')}\n"
                    f"Mode: {mode_str}"
                )
                if alert_method in ("SMS", "BOTH"):
                    send_sms_notification(trade_msg)
                if alert_method in ("TELEGRAM", "BOTH"):
                    rsi_formatted = f"{rsi:.1f}" if rsi is not None else "N/A"
                    rvol_formatted = f"{rvol:.1f}x" if rvol is not None else "N/A"
                    tg_trade = (
                        f"📈 <b>{grade_icon} {grade_str} {zone_title} TRADE PLACED: {ticker}</b>\n\n"
                        f"<b>Option:</b> {result.get('type', '')} ${result.get('strike', '')} ({result.get('option_symbol', '')})\n"
                        f"<b>Entry Price:</b> ${result.get('entry_price', 0):.2f}\n"
                        f"<b>Qty:</b> {result.get('quantity', 1)} contract(s)\n"
                        f"<b>Stock Price:</b> ${last_price:.2f}\n"
                        f"<b>VWAP (Retest Level):</b> ${vwap_target:.2f}\n<b>Targets:</b> 🎯 Take Profit +20% | 🛑 Stop Loss -20%\n"
                        f"<b>Zone:</b> {zone_str}\n"
                        f"<b>RSI:</b> {rsi_formatted} | <b>RVOL:</b> {rvol_formatted}\n"
                        f"<b>Market Sentiment:</b> {align_icon} {align_badge}\n"
                        f"<b>Technical Trend:</b> {tech_str}\n"
                        f"<b>Setup:</b> {reason or (action + ' Reversal')}\n"
                        f"<b>Mode:</b> {mode_str}"
                    )
                    send_telegram_notification(tg_trade)
    except Exception as e:
        logger.error(f"Paper trade error for {ticker}: {e}")
        import traceback
        traceback.print_exc()

def evaluate_ticker_process(ticker, df):
    """
    Evaluates intraday data for Premarket Breakout + VWAP Retest Strategy:
    1. Extracts Premarket High (PMH) & Premarket Low (PML) between 04:00 and 09:29:59 ET.
    2. Computes Regular Trading Hours (RTH) VWAP starting from 09:30 ET.
    3. LONG Setup:
       - Price has broken above PMH during RTH and is above VWAP.
       - Price pulls back to retest VWAP (Low reaches VWAP within 0.25%, Close holds at/above VWAP).
       - Candle confirms bounce off VWAP (green candle or lower rejection wick).
    4. SHORT Setup:
       - Price has broken below PML during RTH and is below VWAP.
       - Price rallies to retest VWAP from below (High reaches VWAP within 0.25%, Close held at/below VWAP).
       - Candle confirms rejection at VWAP (red candle or upper rejection wick).
    5. Options Verification:
       - Confirms liquid option contract (CALL for Long, PUT for Short) with tight spread.
    """
    if df is None or len(df) < 5:
        return None

    try:
        # Filter today's session
        today_date = df.index[-1].date()
        today_df = df[df.index.date == today_date]
        
        pm_bars = today_df.between_time("04:00", "09:29:59")
        rth_bars = today_df.between_time("09:30", "16:00")
        
        if len(pm_bars) == 0 or len(rth_bars) < 2:
            return None
            
        pm_high = float(pm_bars["High"].max())
        pm_low = float(pm_bars["Low"].min())
        
        # Calculate RTH VWAP
        typical_price = (rth_bars["High"] + rth_bars["Low"] + rth_bars["Close"]) / 3.0
        cum_vol = rth_bars["Volume"].cumsum()
        cum_vol = cum_vol.replace(0, 1)
        vwap_series = (typical_price * rth_bars["Volume"]).cumsum() / cum_vol
        
        last_row = rth_bars.iloc[-1]
        close_price = float(last_row["Close"])
        open_price = float(last_row["Open"])
        high_price = float(last_row["High"])
        low_price = float(last_row["Low"])
        current_vwap = float(vwap_series.iloc[-1])
        
        # Check breakout conditions during RTH
        rth_highs = rth_bars["High"]
        rth_lows = rth_bars["Low"]
        broke_pmh = bool((rth_highs > pm_high).any())
        broke_pml = bool((rth_lows < pm_low).any())
        
        candle_rng = max(0.001, high_price - low_price)
        lower_wick = min(open_price, close_price) - low_price
        upper_wick = high_price - max(open_price, close_price)
        
        is_long = False
        is_short = False
        
        # Long: Broke PMH, now pulls back to retest VWAP and bounces
        if broke_pmh:
            # Low tested VWAP (within 0.25% or dipped through to test it), close held above VWAP
            tested_vwap = (low_price <= current_vwap * 1.0025) and (close_price >= current_vwap * 0.9975)
            confirmed_bounce = (close_price >= open_price) or (lower_wick / candle_rng >= 0.20)
            if tested_vwap and confirmed_bounce and close_price >= current_vwap:
                is_long = True
                
        # Short: Broke PML, now rallies to retest VWAP from below and gets rejected
        if broke_pml and not is_long:
            # High tested VWAP (within 0.25% or touched it from below), close held below VWAP
            tested_vwap = (high_price >= current_vwap * 0.9975) and (close_price <= current_vwap * 1.0025)
            confirmed_rejection = (close_price <= open_price) or (upper_wick / candle_rng >= 0.20)
            if tested_vwap and confirmed_rejection and close_price <= current_vwap:
                is_short = True
                
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
            f"Over PMH (${pm_high:.2f}) + VWAP Retest Bounce (${current_vwap:.2f}) 🚀"
            if is_long else
            f"Under PML (${pm_low:.2f}) + VWAP Retest Rejection (${current_vwap:.2f}) 🩸"
        )
        
        score = 15
        if sentiment_align.startswith("Market Aligned"):
            score += 2
        if rvol > 1.2:
            score += 2
        if (is_long and rsi_val and rsi_val < 45) or (is_short and rsi_val and rsi_val > 55):
            score += 1
            
        grade = "A+"
        
        logger.info(
            f"[{ticker}] 🎯 {grade} {action} SIGNAL! Price: ${close_price:.2f} | "
            f"VWAP: ${current_vwap:.2f} | PMH: ${pm_high:.2f} | PML: ${pm_low:.2f} | {setup_reason}"
        )
        
        return {
            'action': action,
            'type': signal_type,
            'price': close_price,
            'vwap': round(current_vwap, 2),
            'pm_high': round(pm_high, 2),
            'pm_low': round(pm_low, 2),
            'rsi': rsi_val or 50.0,
            'rvol': rvol,
            'score': score,
            'grade': grade,
            'reason': setup_reason,
            'time': rth_bars.index[-1],
            'zone_summary': f"PMH ${pm_high:.2f} / PML ${pm_low:.2f}",
            'in_zone': True,
            'zone_details': {'pm_high': pm_high, 'pm_low': pm_low},
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

            # Opening bell buffer (wait for initial 20 minutes of market open to stabilize)
            ny_tz = get_ny_timezone()
            now = datetime.now(ny_tz)
            current_mins = now.hour * 60 + now.minute
            earliest_trade_mins = int(os.getenv("EARLIEST_TRADE_TIME_MINS", "590"))  # 9:50 AM ET
            if current_mins < earliest_trade_mins:
                open_hr = earliest_trade_mins // 60
                open_mn = earliest_trade_mins % 60
                logger.info(f"Opening bell volatility buffer active (market settling until {open_hr}:{open_mn:02d} AM EST). Bot sleeping for 30s...")
                time.sleep(30)
                continue

            logger.info("--- Starting 3-Sigma A+ Reversal Bot Cycle ---")

            try:
                # 1. Determine tickers to scan: Restricted strictly to Watchlist tickers (165 tickers)
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

                tickers = list(dict.fromkeys([t.strip().upper() for t in watchlist_tickers if t and isinstance(t, str)]))
                if not tickers:
                    tickers = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]

                candle_interval = os.getenv("CANDLE_INTERVAL_3SIGMA", "5m")
                scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
                
                # 2. Pre-calculate daily bands
                precalculate_daily_bands(tickers)
                
                logger.info(f"Scanning {len(tickers)} watchlist tickers in parallel ({candle_interval} candles, PM Breakout + VWAP Retest)...")

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
                            "Profit Target": res['vwap'],
                            "Supply/Demand Zone": res.get('zone_summary', 'None'),
                            "In Zone": res.get('in_zone', False),
                            "Zone Details": res.get('zone_details'),
                            "Market Sentiment": res.get('market_sentiment', 'Neutral'),
                            "Market Sentiment Score": res.get('market_sentiment_score', 50),
                            "Sentiment Alignment": res.get('sentiment_align', 'Neutral'),
                            "Technical Trend": res.get('tech_trend', 'Neutral'),
                            "MACD": res.get('macd_status', 'Neutral'),
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
                            rvol=res.get('rvol'),
                            zone_summary=res.get('zone_summary'),
                            in_zone=res.get('in_zone', False),
                            sentiment_align=res.get('sentiment_align'),
                            market_sentiment_summary=res.get('market_sentiment_summary'),
                            tech_trend=res.get('tech_trend')
                        )
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

