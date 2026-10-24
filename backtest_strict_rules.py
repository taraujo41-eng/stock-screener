"""
Backtest Suite: Strict VWAP-Touch & PM Breakout Strategy
Rules Evaluated:
  1. Session: After 9:30 AM market open.
  2. Chop Zone Filter: Price strictly outside Premarket High and Low (pm_low <= close <= pm_high blocked).
  3. Long Entry: Price > PMH and Price > VWAP, triggered on candle touching VWAP (Low <= VWAP <= High).
  4. Short Entry: Price < PML and Price < VWAP, triggered on candle touching VWAP (Low <= VWAP <= High).
  5. Position Management: Fixed 20% Take Profit, 20% Stop Loss, EOD exit at 3:55 PM.
"""

import os
import sys
import json
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

def load_top50_tickers():
    wl_path = os.path.join(os.path.dirname(__file__), "top50_stocks.json")
    if os.path.exists(wl_path):
        with open(wl_path, "r") as f:
            data = json.load(f)
            tickers = []
            for item in data:
                if isinstance(item, dict):
                    t = item.get("ticker") or item.get("symbol")
                    if t:
                        tickers.append(t.upper())
                elif isinstance(item, str):
                    tickers.append(item.upper())
            if tickers:
                return tickers
    return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "WMT", "PLTR", "JPM", "BAC", "AMD", "CVNA", "F", "UBER"]

def run_backtest(target_date_str="2026-10-23", tickers=None):
    if tickers is None:
        tickers = load_top50_tickers()
        
    trades = []
    print(f"\n=========================================================================================")
    print(f"📊 RUNNING STRICT VWAP-TOUCH BACKTEST FOR: {target_date_str}")
    print(f"Universe: {len(tickers)} tickers | Interval: 5m (with premarket)")
    print(f"Rules: Outside PM Range (No Chop) + Candle Low <= VWAP <= High + Strict 20% SL / 20% TP")
    print(f"=========================================================================================\n")
    
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="5d", interval="5m", prepost=True, progress=False)
            if df is None or len(df) == 0:
                continue
                
            # Flatten multi-index columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
                
            # Filter for target date
            df_day = df[df.index.strftime("%Y-%m-%d") == target_date_str]
            if len(df_day) < 5:
                continue
                
            pm_bars = df_day.between_time("04:00", "09:29:59")
            rth_bars = df_day.between_time("09:30", "16:00")
            
            if len(pm_bars) == 0 or len(rth_bars) < 2:
                continue
                
            pm_high = float(pm_bars["High"].max())
            pm_low = float(pm_bars["Low"].min())
            
            # Cumulative RTH VWAP
            typical_price = (rth_bars["High"] + rth_bars["Low"] + rth_bars["Close"]) / 3.0
            cum_vol = rth_bars["Volume"].cumsum().replace(0, 1)
            vwap_series = (typical_price * rth_bars["Volume"]).cumsum() / cum_vol
            
            in_position = False
            active_trade = None
            
            for i in range(len(rth_bars)):
                bar_time = rth_bars.index[i]
                c_open = float(rth_bars["Open"].iloc[i])
                c_high = float(rth_bars["High"].iloc[i])
                c_low = float(rth_bars["Low"].iloc[i])
                c_close = float(rth_bars["Close"].iloc[i])
                c_vwap = float(vwap_series.iloc[i])
                
                # Position monitoring
                if in_position and active_trade:
                    entry_stock = active_trade["entry_stock"]
                    sig = active_trade["direction"]
                    est_option_entry = active_trade["est_option_entry"]
                    
                    # Estimate option PnL (~0.50 delta ATM option, leverage ~16x)
                    # 1.25% stock move = 20% option contract move
                    if sig == "LONG":
                        high_gain_pct = (c_high - entry_stock) / entry_stock
                        low_loss_pct = (c_low - entry_stock) / entry_stock
                        close_pct = (c_close - entry_stock) / entry_stock
                        
                        opt_peak_gain = high_gain_pct * 16.0 * 100.0
                        opt_trough_loss = low_loss_pct * 16.0 * 100.0
                        opt_curr_gain = close_pct * 16.0 * 100.0
                        
                        if opt_peak_gain >= 20.0:
                            active_trade["exit_time"] = bar_time.strftime("%H:%M")
                            active_trade["exit_price"] = round(est_option_entry * 1.20, 2)
                            active_trade["pnl_pct"] = +20.0
                            active_trade["outcome"] = "TP HIT (+20%)"
                            trades.append(active_trade)
                            in_position = False
                            active_trade = None
                            continue
                        elif opt_trough_loss <= -20.0:
                            active_trade["exit_time"] = bar_time.strftime("%H:%M")
                            active_trade["exit_price"] = round(est_option_entry * 0.80, 2)
                            active_trade["pnl_pct"] = -20.0
                            active_trade["outcome"] = "SL HIT (-20%)"
                            trades.append(active_trade)
                            in_position = False
                            active_trade = None
                            continue
                    else: # SHORT
                        high_loss_pct = (entry_stock - c_high) / entry_stock
                        low_gain_pct = (entry_stock - c_low) / entry_stock
                        close_pct = (entry_stock - c_close) / entry_stock
                        
                        opt_peak_gain = low_gain_pct * 16.0 * 100.0
                        opt_trough_loss = high_loss_pct * 16.0 * 100.0
                        opt_curr_gain = close_pct * 16.0 * 100.0
                        
                        if opt_peak_gain >= 20.0:
                            active_trade["exit_time"] = bar_time.strftime("%H:%M")
                            active_trade["exit_price"] = round(est_option_entry * 1.20, 2)
                            active_trade["pnl_pct"] = +20.0
                            active_trade["outcome"] = "TP HIT (+20%)"
                            trades.append(active_trade)
                            in_position = False
                            active_trade = None
                            continue
                        elif opt_trough_loss <= -20.0:
                            active_trade["exit_time"] = bar_time.strftime("%H:%M")
                            active_trade["exit_price"] = round(est_option_entry * 0.80, 2)
                            active_trade["pnl_pct"] = -20.0
                            active_trade["outcome"] = "SL HIT (-20%)"
                            trades.append(active_trade)
                            in_position = False
                            active_trade = None
                            continue
                            
                    # EOD Exit at 15:55
                    if bar_time.hour == 15 and bar_time.minute >= 50:
                        active_trade["exit_time"] = bar_time.strftime("%H:%M")
                        final_pnl = max(-20.0, min(20.0, opt_curr_gain))
                        active_trade["exit_price"] = round(est_option_entry * (1.0 + final_pnl / 100.0), 2)
                        active_trade["pnl_pct"] = round(final_pnl, 1)
                        active_trade["outcome"] = f"EOD CLOSE ({final_pnl:+.1f}%)"
                        trades.append(active_trade)
                        in_position = False
                        active_trade = None
                        continue

                # Scan for new signals
                if not in_position:
                    # 1. Chop Zone Filter: Never trade inside Premarket range
                    if pm_low <= c_close <= pm_high:
                        continue
                        
                    # 2. VWAP Touch Condition: Candle low <= VWAP <= high
                    touched_vwap = (c_low <= c_vwap <= c_high)
                    if not touched_vwap:
                        continue
                        
                    # 3. Long Condition: Price > PMH and Price > VWAP
                    if c_close > pm_high and c_close > c_vwap:
                        in_position = True
                        est_opt = round(c_close * 0.035, 2)
                        active_trade = {
                            "date": target_date_str,
                            "ticker": ticker,
                            "direction": "LONG",
                            "entry_time": bar_time.strftime("%H:%M"),
                            "entry_stock": round(c_close, 2),
                            "vwap": round(c_vwap, 2),
                            "pmh": round(pm_high, 2),
                            "pml": round(pm_low, 2),
                            "est_option_entry": est_opt,
                            "exit_time": None,
                            "exit_price": None,
                            "pnl_pct": None,
                            "outcome": None
                        }
                    # 4. Short Condition: Price < PML and Price < VWAP
                    elif c_close < pm_low and c_close < c_vwap:
                        in_position = True
                        est_opt = round(c_close * 0.035, 2)
                        active_trade = {
                            "date": target_date_str,
                            "ticker": ticker,
                            "direction": "SHORT",
                            "entry_time": bar_time.strftime("%H:%M"),
                            "entry_stock": round(c_close, 2),
                            "vwap": round(c_vwap, 2),
                            "pmh": round(pm_high, 2),
                            "pml": round(pm_low, 2),
                            "est_option_entry": est_opt,
                            "exit_time": None,
                            "exit_price": None,
                            "pnl_pct": None,
                            "outcome": None
                        }
        except Exception:
            continue
            
    # Print summary table
    if trades:
        print(f"{'Ticker':<7} | {'Type':<5} | {'Entry':<5} | {'Stock':<8} | {'VWAP':<8} | {'PM Reference':<14} | {'Exit':<5} | {'Result':<16} | {'Return'}")
        print("-" * 95)
        for t in trades:
            pm_ref = f"PMH ${t['pmh']:.2f}" if t['direction'] == "LONG" else f"PML ${t['pml']:.2f}"
            pnl_str = f"{t['pnl_pct']:+.1f}%" if t['pnl_pct'] is not None else "N/A"
            print(f"{t['ticker']:<7} | {t['direction']:<5} | {t['entry_time']:<5} | ${t['entry_stock']:<7.2f} | ${t['vwap']:<7.2f} | {pm_ref:<14} | {t['exit_time'] or 'Open':<5} | {t['outcome']:<16} | {pnl_str}")
            
        wins = [t for t in trades if (t['pnl_pct'] or 0) > 0]
        losses = [t for t in trades if (t['pnl_pct'] or 0) < 0]
        total_pnl = sum(t['pnl_pct'] for t in trades if t['pnl_pct'] is not None)
        win_rate = (len(wins) / len(trades)) * 100 if trades else 0
        
        print("\n" + "=" * 55)
        print(f"📊 SUMMARY FOR {target_date_str}:")
        print(f"Total Qualified Trades: {len(trades)}")
        print(f"Wins: {len(wins)} | Losses: {len(losses)}")
        print(f"Win Rate: {win_rate:.1f}%")
        print(f"Cumulative Option P&L: {total_pnl:+.1f}%")
        print("=" * 55 + "\n")
    else:
        print(f"No trades triggered on {target_date_str} under strict criteria.\n")
        
    return trades

if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-10-23"
    run_backtest(date)
