import os
import sys
import time
import logging
import smtplib
import threading
from email.mime.text import MIMEText
from datetime import datetime

# Load parent directory to allow imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from indicator import calculate_3_sigma_divergence
from data_fetcher import fetch_one

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

def evaluate_ticker(ticker):
    candle_interval = os.getenv("CANDLE_INTERVAL_3SIGMA", "5m")
    bb_length = int(os.getenv("BB_LENGTH", "20"))
    bb_mult = float(os.getenv("BB_MULT", "3.0"))
    rsi_length = int(os.getenv("RSI_LENGTH", "14"))
    lookback = int(os.getenv("LOOKBACK", "15"))
    
    # Fetch candles (Leverage data_fetcher)
    df = fetch_one(ticker, days=15, interval=candle_interval)
    if df is None or df.empty:
        return
        
    if len(df) < max(bb_length, rsi_length) + lookback + 5:
        return
        
    # Compute 3-Sigma indicators
    df_ind = calculate_3_sigma_divergence(
        df,
        bb_length=bb_length,
        bb_mult=bb_mult,
        rsi_length=rsi_length,
        lookback=lookback
    )
    
    # Inspect latest state
    last_row = df_ind.iloc[-1]
    long_trigger = last_row['long_trigger']
    short_trigger = last_row['short_trigger']
    close_price = last_row['Close']
    vwap_target = last_row['vwap']
    
    logger.info(f"[{ticker} {candle_interval}] Price: {close_price:.2f} | RSI: {last_row['rsi']:.1f} | Trigger: {'LONG' if long_trigger else 'SHORT' if short_trigger else 'None'}")
    
    if long_trigger:
        trigger_alerts(ticker, "BUY", "bullish", close_price, vwap_target)
    elif short_trigger:
        trigger_alerts(ticker, "SELL", "bearish", close_price, vwap_target)

def bot_loop():
    logger.info("Starting background 3-Sigma alert bot loop...")
    
    # Load tickers from config
    tickers_str = os.getenv("TICKERS_3SIGMA", "AAPL,MSFT,NVDA,SPY,QQQ")
    tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
    
    scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
    
    while True:
        try:
            logger.info("--- Starting 3-Sigma Reversal Bot Cycle ---")
            
            # Reload settings in case they change on the server .env
            tickers_str = os.getenv("TICKERS_3SIGMA", "AAPL,MSFT,NVDA,SPY,QQQ")
            tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
            scan_interval = int(os.getenv("SCAN_INTERVAL_3SIGMA", "60"))
            
            for ticker in tickers:
                try:
                    evaluate_ticker(ticker)
                except Exception as e:
                    logger.error(f"Error evaluating {ticker}: {e}")
                time.sleep(1)
                
            logger.info(f"--- 3-Sigma Bot Cycle Complete. Sleeping for {scan_interval}s ---")
            time.sleep(scan_interval)
        except Exception as e:
            logger.error(f"General exception in bot_loop: {e}")
            time.sleep(60)

def start_bot_thread():
    """Starts the bot loop in a daemon background thread."""
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    logger.info("3-Sigma background alert bot thread spawned.")
