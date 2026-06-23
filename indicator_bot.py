import os
import sys
import time
import logging
import smtplib
import threading
import pytz
from email.mime.text import MIMEText
from datetime import datetime

# Load parent directory to allow imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from indicator import calculate_3_sigma_divergence
from data_fetcher import fetch_batch_concurrent

# Set up local log file
logger = logging.getLogger("3sigma_bot")
logger.setLevel(logging.INFO)

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
# Global state to keep track of sent alerts: {ticker: last_alerted_candle_timestamp}
_last_alerts_sent = {}
# Global state to keep track of last self-ping timestamp (Render keep-alive)
_last_self_ping_time = 0

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

def trigger_alerts(ticker, action, signal_type, last_price, vwap_target):
    logger.info(f"🔔 SIGNAL TRIGGERED on {ticker}: {action} | Price={last_price:.2f}, Target VWAP={vwap_target:.2f}")
    
    alert_method = os.getenv("ALERT_METHOD", "SMS").upper()
    
    # 1. Send SMS Notification
    if alert_method in ("SMS", "BOTH"):
        sms_msg = (
            f"REVERSAL ALERT: {ticker}\n"
            f"Type: {signal_type.upper()} Reversal\n"
            f"Action: {action}\n"
            f"Price: ${last_price:.2f}\n"
            f"VWAP Target: ${vwap_target:.2f}"
        )
        send_sms_notification(sms_msg)
        
    # 2. Send Telegram Notification
    if alert_method in ("TELEGRAM", "BOTH"):
        tg_msg = (
            f"🚨 <b>REVERSAL ALERT: {ticker}</b> 🚨\n\n"
            f"<b>Type:</b> {signal_type.upper()} Reversal\n"
            f"<b>Action:</b> {action} Setup\n"
            f"<b>Price:</b> ${last_price:.2f}\n"
            f"<b>VWAP Target:</b> ${vwap_target:.2f}"
        )
        send_telegram_notification(tg_msg)

def evaluate_ticker_process(ticker, df):
    """
    Called in parallel background threads to evaluate the 15m dataframe against Daily Bollinger Bands.
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
    rsi_length = int(os.getenv("RSI_LENGTH", "14"))
    lookback = int(os.getenv("LOOKBACK", "15"))
    
    if len(df) < max(bb_length, rsi_length) + lookback + 5:
        return None
        
    # Compute 3-Sigma indicators using pre-calculated daily bands
    df_ind = calculate_3_sigma_divergence(
        df,
        bb_length=bb_length,
        bb_mult=bb_mult,
        rsi_length=rsi_length,
        lookback=lookback,
        daily_upper_bb=daily_upper_bb,
        daily_lower_bb=daily_lower_bb
    )
    
    # Inspect latest state
    last_row = df_ind.iloc[-1]
    long_trigger = last_row['long_trigger']
    short_trigger = last_row['short_trigger']
    close_price = last_row['Close']
    vwap_target = last_row['vwap']
    
    logger.info(f"[{ticker} 15m] Price: {close_price:.2f} | RSI: {last_row['rsi']:.1f} | Daily BB: [{daily_lower_bb:.2f} - {daily_upper_bb:.2f}] | Trigger: {'LONG' if long_trigger else 'SHORT' if short_trigger else 'None'}")
    
    if long_trigger:
        return {
            'action': 'BUY',
            'type': 'bullish',
            'price': close_price,
            'vwap': vwap_target,
            'time': df_ind.index[-1]
        }
    elif short_trigger:
        return {
            'action': 'SELL',
            'type': 'bearish',
            'price': close_price,
            'vwap': vwap_target,
            'time': df_ind.index[-1]
        }
    return None

def precalculate_daily_bands(tickers):
    """
    Fetches daily candles for all tickers in parallel and calculates their daily BB bands.
    Stores results in the global _daily_bands_map.
    """
    global _daily_bands_map
    _daily_bands_map.clear()
    
    bb_length = int(os.getenv("BB_LENGTH", "20"))
    bb_mult = float(os.getenv("BB_MULT", "3.0"))
    
    logger.info(f"Pre-calculating daily Bollinger Bands for {len(tickers)} tickers...")
    
    # Fetch 1d candles (45 days is enough for 20 BB)
    daily_dfs = fetch_batch_concurrent(
        tickers=tickers,
        days=45,
        max_workers=25,
        interval="1d",
        includePrePost="false",
        skip_webull=False
    )
    
    ny_tz = pytz.timezone("America/New_York")
    today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
    
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
            
    logger.info(f"Successfully pre-calculated daily bands for {len(_daily_bands_map)} tickers.")

def is_market_hours():
    """Returns True if current time is within regular market hours (9:30 AM to 4:15 PM EST, Mon-Fri)."""
    try:
        ny_tz = pytz.timezone("America/New_York")
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
    logger.info("Starting background 3-Sigma alert bot loop...")
    
    while True:
        try:
            if not is_market_hours():
                logger.info("Market is closed (weekends or outside 9:30 AM - 4:15 PM EST). Bot sleeping for 5 minutes...")
                time.sleep(300)
                continue
                
            # Perform self-ping to keep Render container awake during market hours
            global _last_self_ping_time
            now_ts = time.time()
            if now_ts - _last_self_ping_time >= 600:  # 10 minutes
                _last_self_ping_time = now_ts
                self_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_PING_URL")
                if self_url:
                    try:
                        import requests
                        ping_url = f"{self_url.rstrip('/')}/api/ping"
                        requests.get(ping_url, timeout=5)
                        logger.info(f"Self-ping sent to keep Render container awake: {ping_url}")
                    except Exception as e:
                        logger.warning(f"Self-ping failed: {e}")
                
            logger.info("--- Starting 3-Sigma Reversal Bot Cycle ---")
            
            # 1. Determine tickers to scan
            tickers_mode = os.getenv("TICKERS_3SIGMA", "ALL").upper().strip()
            
            tickers = []
            if tickers_mode == "ALL":
                try:
                    from reversal_scanner import get_us_tickers
                    tickers = get_us_tickers()
                except Exception as e:
                    logger.error(f"Failed to load full US tickers list: {e}")
            elif tickers_mode == "WATCHLIST":
                try:
                    from app import user_watchlist
                    tickers = list(user_watchlist)
                except Exception as e:
                    logger.warning(f"Could not load dynamic user_watchlist: {e}")
            
            # Fallback if watchlist/all fails or custom comma-separated list
            if not tickers:
                tickers_str = os.getenv("TICKERS_3SIGMA", "AAPL,MSFT,NVDA,SPY,QQQ")
                if tickers_str.upper() in ("ALL", "WATCHLIST"):
                    tickers_str = "AAPL,MSFT,NVDA,SPY,QQQ"
                tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
                
            candle_interval = os.getenv("CANDLE_INTERVAL_3SIGMA", "15m")
            scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
            
            # For 3-Sigma Daily Close + 15m strategy, force candle_interval to 15m
            candle_interval = "15m"
            
            # 2. Pre-calculate daily bands
            precalculate_daily_bands(tickers)
            
            logger.info(f"Scanning {len(tickers)} tickers in parallel (15m regular market hours)...")
            
            # 3. Download and compute 15m in parallel
            results = fetch_batch_concurrent(
                tickers=tickers,
                days=15,
                max_workers=25,
                interval=candle_interval,
                includePrePost="false",  # Regular market hours only
                process_fn=evaluate_ticker_process,
                skip_webull=False
            )
            
            # 4. Process matches in main thread
            triggered_count = 0
            for ticker, res in results.items():
                if res:
                    candle_time = res.get('time')
                    if candle_time and _last_alerts_sent.get(ticker) == candle_time:
                        continue
                        
                    trigger_alerts(
                        ticker=ticker,
                        action=res['action'],
                        signal_type=res['type'],
                        last_price=res['price'],
                        vwap_target=res['vwap']
                    )
                    if candle_time:
                        _last_alerts_sent[ticker] = candle_time
                    triggered_count += 1
                    
            logger.info(f"--- 3-Sigma Bot Cycle Complete. Triggers found: {triggered_count}. Sleeping for {scan_interval}s ---")
            time.sleep(scan_interval)
            
        except Exception as e:
            logger.error(f"General exception in bot_loop: {e}")
            time.sleep(60)

def start_bot_thread():
    """Starts the bot loop in a daemon background thread."""
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    logger.info("3-Sigma background alert bot thread spawned.")
