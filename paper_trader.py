"""
Webull Paper Trading Module — Options Auto-Trader

Uses the official Webull OpenAPI Python SDK (`webull-openapi-python-sdk`) for 
trading operations and the unofficial `webull` library for market data/options chains.

The OpenAPI requires a one-time 2FA approval from the Webull app. Run the login 
helper script (`python3 paper_trader.py --login`) to authenticate, then the bot
will use the cached token for all subsequent trades.

Features:
  • Authenticates via Webull OpenAPI (app_key + app_secret + 2FA)
  • Places option orders (buy to open / sell to close) on paper or live account
  • Monitors open positions for take-profit, stop-loss, and time-based exits
  • Enforces risk controls (max contracts, max risk, max positions)
  • Persists trade history to trade_log.json
"""

import os
import sys
import time
import json
import uuid
import logging
import threading
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()

# ── Logging ─────────────────────────────────────────────────────────────

logger = logging.getLogger("paper_trader")
logger.setLevel(logging.INFO)
logger.propagate = False  # Prevent propagation duplicates

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


def get_ny_timezone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return pytz.timezone("America/New_York")


# ── Config ──────────────────────────────────────────────────────────────

_SCAN_DATA_DIR = os.environ.get("SCAN_DATA_DIR", os.path.dirname(__file__))
TRADE_LOG_FILE = os.path.join(_SCAN_DATA_DIR, "trade_log.json")

def _cfg(key, default, cast=str):
    """Read a config value from .env with type casting."""
    val = os.getenv(key, default)
    if cast == bool:
        return str(val).lower() in ("true", "1", "yes")
    return cast(val)


# ── Trade Log Persistence ──────────────────────────────────────────────

def _load_trade_log():
    """Load trade history from disk."""
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"trades": [], "daily_stats": {}}


def _save_trade_log(log_data):
    """Persist trade history to disk."""
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save trade log: {e}")


# =====================================================================
# PaperTrader Class
# =====================================================================

class PaperTrader:
    """
    Manages Webull paper trading for options using the Official OpenAPI SDK.

    Authentication:
      The OpenAPI requires a one-time 2FA approval from the Webull mobile app.
      Run `python3 paper_trader.py --login` to authenticate interactively.
      The token is cached locally and reused until it expires (~90 days).

    Lifecycle:
      1. login()               — initialize API client with cached token
      2. place_option_order()   — find the best contract and buy it
      3. monitor_positions()    — background loop to manage exits
    """

    def __init__(self):
        self._api_client = None
        self._trade_client = None
        self._account_id = None
        self._logged_in = False
        self._trade_log = _load_trade_log()
        self._open_positions = [t for t in self._trade_log.get("trades", []) if t.get("status") == "open"]
        self._monitor_thread = None
        self._lock = threading.Lock()

        # Risk controls (loaded from .env)
        self.enabled = _cfg("PAPER_TRADE_ENABLED", "true", bool)
        self.starting_balance = _cfg("PAPER_STARTING_BALANCE", "3000.0", float)
        self.max_contracts = _cfg("PAPER_MAX_CONTRACTS", "1", int)
        self.max_risk_per_trade = _cfg("PAPER_MAX_RISK_PER_TRADE", "0", float)  # 0 = disabled, bounded by available funds
        self.max_open_positions = _cfg("PAPER_MAX_OPEN_POSITIONS", "3", int)
        self.max_daily_trades = _cfg("PAPER_MAX_DAILY_TRADES", "5", int)
        self.max_daily_loss = _cfg("PAPER_MAX_DAILY_LOSS", "150.0", float)
        self.min_entry_interval_secs = _cfg("PAPER_MIN_ENTRY_INTERVAL_SECS", "60", int)
        self.stop_loss_pct = _cfg("PAPER_STOP_LOSS_PCT", "20", float)
        self.take_profit_vwap = _cfg("PAPER_TAKE_PROFIT_VWAP", "false", bool)
        self.vwap_min_profit_pct = _cfg("PAPER_VWAP_MIN_PROFIT_PCT", "5", float)
        self.take_profit_pct = _cfg("PAPER_TAKE_PROFIT_PCT", "20", float)
        self.use_strategy_vwap_exits = _cfg("PAPER_USE_STRATEGY_VWAP_EXITS", "true", bool)
        self.trailing_stop_enabled = _cfg("PAPER_TRAILING_STOP_ENABLED", "false", bool)
        self.trailing_stop_activation_pct = _cfg("PAPER_TRAILING_STOP_ACTIVATION_PCT", "5.0", float)
        self.trailing_stop_callback_pct = _cfg("PAPER_TRAILING_STOP_CALLBACK_PCT", "2.5", float)
        self.breakeven_stop_pct = _cfg("PAPER_BREAKEVEN_STOP_PCT", "0.0", float)
        self.max_hold_minutes = _cfg("PAPER_MAX_HOLD_MINUTES", "0", int)
        self.earliest_trade_mins = _cfg("EARLIEST_TRADE_TIME_MINS", "580", int)
        self.latest_trade_mins = _cfg("LATEST_TRADE_TIME_MINS", "690", int)
        self.close_before_eod = _cfg("PAPER_CLOSE_BEFORE_MARKET_CLOSE", "true", bool)
        self.allow_delayed_options = _cfg("ALLOW_DELAYED_OPTIONS_TRADING", "false", bool)
        self._last_delayed_alert_date = None

    def _notify_delayed_data_once(self, ticker_or_ctx, detail):
        """Send at most one Telegram warning per trading day when live real-time options data is unavailable."""
        today_ny = datetime.now(get_ny_timezone()).date()
        if self._last_delayed_alert_date == today_ny:
            return
        self._last_delayed_alert_date = today_ny
        try:
            from indicator_bot import send_telegram_notification
            msg = (
                "⚠️ <b>LIVE OPTIONS DATA NOT ACTIVE</b>\n\n"
                f"<b>Context:</b> {ticker_or_ctx}\n"
                f"<b>Status:</b> {detail}\n\n"
                "New automated option trades are <b>paused</b> so the bot does not trade on 15-minute delayed quotes.\n\n"
                "<b>Action required:</b> Open the <b>Webull OpenAPI Developer Portal</b> and enable/subscribe to "
                "<code>US_OPTION</code> (OPRA Real-Time) under your App Key. The bot re-checks every 5 minutes automatically."
            )
            send_telegram_notification(msg)
        except Exception as e:
            logger.debug(f"[PaperTrader] Failed to send delayed-data Telegram warning: {e}")

    # ── Authentication ──────────────────────────────────────────────

    def login(self):
        """
        Initialize the Webull OpenAPI trade client using cached token.
        
        The OpenAPI SDK stores the token in conf/token.txt after initial 2FA.
        If the token is expired, you must run `python3 paper_trader.py --login`
        interactively to re-authenticate via 2FA.
        """
        if self._logged_in and self._trade_client:
            return True

        try:
            # Monkey-patch: the unofficial `webull` package (0.6.1) doesn't expose
            # __version__, but the OpenAPI SDK's ApiClient.default_user_agent() does
            # __import__('webull.core').__version__ which returns the top-level `webull`
            # module and crashes with AttributeError. Inject it before importing the SDK.
            import webull as _webull_mod
            if not hasattr(_webull_mod, '__version__'):
                _webull_mod.__version__ = '0.6.1'

            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient

            app_key = os.getenv("WEBULL_APP_KEY")
            app_secret = os.getenv("WEBULL_APP_SECRET")
            region = os.getenv("WEBULL_REGION", "us")

            if not app_key or not app_secret or app_key == "your_app_key_here":
                logger.info("[PaperTrader] ℹ️ Webull OpenAPI keys not present in environment. Activating 100% Safe Simulation Mode for paper trading.")
                self._account_id = "SIMULATED_PAPER_ACCOUNT"
                self._is_real_paper_account = False
                self._logged_in = True
                return True

            # Check if we have a valid cached token
            token_file = os.path.join(os.path.dirname(__file__), "conf", "token.txt")
            if not os.path.exists(token_file):
                logger.info(
                    "[PaperTrader] ℹ️ No cached OpenAPI token found on server. "
                    "Activating 100% Safe Simulation Mode for paper trading."
                )
                self._account_id = "SIMULATED_PAPER_ACCOUNT"
                self._is_real_paper_account = False
                self._logged_in = True
                return True

            # Read and validate token
            with open(token_file, "r") as f:
                lines = f.readlines()
            if len(lines) >= 3:
                token_val = lines[0].strip()
                expires_ms = int(lines[1].strip())
                status = lines[2].strip()

                now_ms = int(time.time() * 1000)
                if status != "NORMAL" or now_ms >= expires_ms:
                    logger.error(
                        f"[PaperTrader] Cached token is {'expired' if now_ms >= expires_ms else status}. "
                        f"Run `python3 paper_trader.py --login` to re-authenticate."
                    )
                    return False

                remaining_days = (expires_ms - now_ms) / (1000 * 86400)
                logger.info(f"[PaperTrader] Valid token found (expires in {remaining_days:.0f} days)")

            # Initialize API client — pass check_token=False to skip 2FA loop
            # We'll inject the token directly
            logger.info(f"[PaperTrader] Initializing OpenAPI client (region={region})...")
            
            # The ApiClient constructor triggers 2FA if token is invalid
            # We need to skip that by using the cached token
            api_client = ApiClient.__new__(ApiClient)
            api_client._app_key = app_key
            api_client._app_secret = app_secret
            api_client._region_id = region
            api_client._stream_logger_set = True  # Suppress default logging
            api_client._file_logger_set = True
            
            # Set up the client properly using internal initialization
            from webull.core.http.initializer.client_initializer import ClientInitializer
            # Store the token so initializer finds it
            api_client = ApiClient(app_key, app_secret, region)
            
            self._api_client = api_client
            self._trade_client = TradeClient(api_client)
            
            # Get account list to find paper trading account
            logger.info("[PaperTrader] Fetching account list...")
            resp = self._trade_client.account_v2.get_account_list()
            
            if resp.status_code != 200:
                logger.error(f"[PaperTrader] Failed to get accounts: {resp.status_code} {resp.text[:200]}")
                return False
            
            accounts = resp.json()
            logger.info(f"[PaperTrader] Accounts: {json.dumps(accounts, indent=2)[:500]}")
            
            # Find paper trading account
            account_list = accounts if isinstance(accounts, list) else accounts.get("data", accounts.get("accounts", []))
            self._is_real_paper_account = False

            # Log every account for debugging
            for i, acc in enumerate(account_list):
                logger.info(
                    f"[PaperTrader] Account[{i}]: id={acc.get('account_id')} "
                    f"number={acc.get('account_number')} "
                    f"type={acc.get('account_type')} "
                    f"label={acc.get('account_label')}"
                )

            # Priority 1: Explicit PAPER_ACCOUNT_ID from .env (account_id or account_number)
            explicit_id = os.getenv("PAPER_ACCOUNT_ID", "").strip()
            if explicit_id:
                # Match by account_id or account_number
                for acc in account_list:
                    if acc.get("account_id") == explicit_id or acc.get("account_number") == explicit_id:
                        self._account_id = acc.get("account_id", acc.get("secAccountId"))
                        self._is_real_paper_account = True
                        logger.info(
                            f"[PaperTrader] ✅ Matched explicit PAPER_ACCOUNT_ID={explicit_id} → "
                            f"account_id={self._account_id} ({acc.get('account_type')})"
                        )
                        break
                if not self._is_real_paper_account:
                    # Account not in API list — use the explicit ID directly
                    # (Webull may not list paper accounts but still accept orders on them)
                    self._account_id = explicit_id
                    self._is_real_paper_account = True
                    logger.warning(
                        f"[PaperTrader] ⚠️ PAPER_ACCOUNT_ID={explicit_id} not found in API account list. "
                        f"Using it directly — orders may fail if the ID is wrong."
                    )

            # Priority 2: Auto-detect by account type/label keywords
            if not self._is_real_paper_account:
                for acc in account_list:
                    acc_type = acc.get("account_type", acc.get("accountType", "")).upper()
                    acc_label = acc.get("account_label", "").upper()
                    if "PAPER" in acc_type or "SIMULATION" in acc_type or "DEMO" in acc_type or "PAPER" in acc_label:
                        self._account_id = acc.get("account_id", acc.get("secAccountId"))
                        self._is_real_paper_account = True
                        logger.info(f"[PaperTrader] Found Webull paper account: {self._account_id}")
                        break
            
            if not self._is_real_paper_account:
                # Webull OpenAPI is connected to a LIVE account.
                # FOR SAFETY: We WILL NOT place real orders on live CASH/MARGIN accounts.
                # Instead, paper trading operates in 100% SAFE LOCAL SIMULATION MODE
                # using real-time market options prices and live P&L tracking.
                if account_list:
                    live_acc = account_list[0].get("account_id", account_list[0].get("secAccountId"))
                    logger.warning(
                        f"[PaperTrader] 🛡️ SAFETY PROTECTION ACTIVE: Account '{live_acc}' is a LIVE account ({account_list[0].get('account_type', 'LIVE')}). "
                        f"Real orders are BLOCKED. Paper trading running in 100% SAFE SIMULATION MODE with real-time market data. "
                        f"Set PAPER_ACCOUNT_ID in .env to target your paper account explicitly."
                    )
                self._account_id = "SIMULATED_PAPER_ACCOUNT"
                self._is_real_paper_account = False
            
            self._logged_in = True
            logger.info(f"[PaperTrader] ✅ Paper Trading active (Mode: {'Webull API' if self._is_real_paper_account else '100% Safe Local Simulation'})")
            return True

        except Exception as e:
            logger.error(f"[PaperTrader] Login error: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ── Account Info ────────────────────────────────────────────────

    def get_account(self):
        """Get account balance and details."""
        if not self._logged_in:
            if not self.login():
                return None
        if not getattr(self, "_is_real_paper_account", False):
            # Simulated paper account balance ($100,000 virtual balance)
            trades = self._trade_log.get("trades", [])
            closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
            total_pnl = sum(t["pnl"] for t in closed)
            open_cost = sum(p.get("entry_price", 0) * 100 * p.get("quantity", 1) for p in self._open_positions if p.get("status") == "open")
            
            starting_balance = getattr(self, "starting_balance", 3000.0)
            net_liq = starting_balance + total_pnl
            cash_bal = net_liq - open_cost
            
            return {
                "account_id": "SIMULATED_PAPER_ACCOUNT",
                "account_type": "PAPER_SIMULATION",
                "total_net_liquidation_value": f"{net_liq:.2f}",
                "total_cash_balance": f"{cash_bal:.2f}",
                "total_unrealized_profit_loss": "0.00",
                "total_day_profit_loss": f"{total_pnl:.2f}",
                "buying_power": f"{cash_bal:.2f}",
                "mode": "100% Safe Local Simulation"
            }

        try:
            resp = self._trade_client.account_v2.get_account_balance(self._account_id)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            logger.error(f"[PaperTrader] get_account error: {e}")
            return None

    def get_positions(self):
        """Get current open positions."""
        if not self._logged_in:
            if not self.login():
                return []
        if not getattr(self, "_is_real_paper_account", False):
            return [p for p in self._open_positions if p.get("status") == "open"]
        try:
            resp = self._trade_client.account_v2.get_account_position(self._account_id)
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logger.error(f"[PaperTrader] get_positions error: {e}")
            return []

    def get_orders(self, count=20, status=None, **kwargs):
        """Get recent order history."""
        if not self._logged_in:
            if not self.login():
                return []
        if not getattr(self, "_is_real_paper_account", False):
            trades = self._trade_log.get("trades", [])
            return trades[-count:]
        try:
            resp = self._trade_client.order_v2.get_order_history(
                self._account_id, page_size=count
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logger.error(f"[PaperTrader] get_orders error: {e}")
            return []

    def get_current_orders(self):
        """Get currently open (unfilled) orders."""
        if not self._logged_in:
            if not self.login():
                return []
        if not getattr(self, "_is_real_paper_account", False):
            return []
        try:
            resp = self._trade_client.order_v2.get_order_open(self._account_id)
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logger.error(f"[PaperTrader] get_current_orders error: {e}")
            return []

    # ── Risk Checks ─────────────────────────────────────────────────

    def _daily_trade_count(self):
        """Count how many trades were placed today."""
        ny_tz = get_ny_timezone()
        today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
        count = 0
        for trade in self._trade_log.get("trades", []):
            if (trade.get("entry_time") or "").startswith(today_str):
                count += 1
        return count

    def _daily_realized_pnl(self):
        """Calculate total realized PnL for trades closed today."""
        ny_tz = get_ny_timezone()
        today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
        total_pnl = 0.0
        for trade in self._trade_log.get("trades", []):
            exit_time_str = trade.get("exit_time") or ""
            if exit_time_str.startswith(today_str) and trade.get("status") == "closed":
                total_pnl += float(trade.get("pnl") or 0.0)
        return total_pnl

    def _last_entry_time(self):
        """Return datetime of most recent trade entered today, or None."""
        ny_tz = get_ny_timezone()
        today_str = datetime.now(ny_tz).strftime("%Y-%m-%d")
        latest = None
        for trade in self._trade_log.get("trades", []):
            etime_str = trade.get("entry_time") or ""
            if etime_str.startswith(today_str):
                try:
                    dt = datetime.fromisoformat(etime_str)
                    if latest is None or dt > latest:
                        latest = dt
                except Exception:
                    pass
        return latest

    def _open_position_count(self):
        """Count currently tracked open positions."""
        return len([p for p in self.get_open_positions() if p.get("status") == "open"])

    def get_available_funds(self):
        """Returns the current available cash / buying power in the account."""
        acc = self.get_account()
        if not acc:
            return getattr(self, "starting_balance", 3000.0)
        
        for field in ["total_cash_balance", "buying_power", "usable_cash", "day_buying_power", "cash_balance"]:
            val = acc.get(field)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        if isinstance(acc, dict) and "account_members" in acc:
            for member in acc.get("account_members", []):
                if member.get("key") in ("usable_cash", "day_buying_power", "total_cash_balance"):
                    try:
                        return float(member.get("value", 0))
                    except (ValueError, TypeError):
                        pass
        return getattr(self, "starting_balance", 3000.0)

    def _check_risk_limits(self, ask_price):
        """
        Validate that placing a new trade doesn't violate risk controls.
        Returns (ok: bool, reason: str).
        """
        if self._daily_trade_count() >= self.max_daily_trades:
            return False, f"Daily trade limit reached ({self.max_daily_trades})"

        # Daily Max Loss Circuit Breaker
        if self.max_daily_loss > 0:
            realized_pnl = self._daily_realized_pnl()
            if realized_pnl <= -abs(self.max_daily_loss):
                return False, f"Daily max loss circuit breaker triggered (${realized_pnl:.2f} <= -${self.max_daily_loss:.2f})"

        # Entry pacing / interval cooldown (avoid entering multiple trades in seconds)
        if self.min_entry_interval_secs > 0:
            last_entry = self._last_entry_time()
            if last_entry:
                ny_tz = get_ny_timezone()
                now = datetime.now(ny_tz)
                elapsed = (now - last_entry).total_seconds()
                if elapsed < self.min_entry_interval_secs:
                    wait_left = int(self.min_entry_interval_secs - elapsed)
                    return False, f"Entry pacing: waiting {wait_left}s before next entry ({elapsed:.0f}s / {self.min_entry_interval_secs}s)"

        if self._open_position_count() >= self.max_open_positions:
            return False, f"Max open positions reached ({self.max_open_positions})"

        if ask_price and ask_price > 0:
            single_cost = ask_price * 100
            available_funds = self.get_available_funds()
            if single_cost > available_funds:
                return False, f"Single contract cost ${single_cost:.0f} exceeds account available funds (${available_funds:.2f})"
            if self.max_risk_per_trade > 0 and single_cost > self.max_risk_per_trade:
                return False, f"Trade cost ${single_cost:.0f} exceeds max risk ${self.max_risk_per_trade:.0f}"
        return True, "OK"

    # ── Option Contract Discovery ───────────────────────────────────

    def _get_option_instrument_id(self, ticker, strike, option_type, exp_str):
        """
        Look up the Webull instrument_id for a specific option contract.
        Uses the unofficial webull library for options chain data.
        """
        try:
            from data_fetcher import get_unofficial_client
            wb = get_unofficial_client()
            if not wb:
                logger.warning("[PaperTrader] Unofficial client not available for options lookup")
                return None

            current_year = datetime.now().year
            exp_date = datetime.strptime(f"{exp_str} {current_year}", "%b %d %Y")
            exp_date_str = exp_date.strftime("%Y-%m-%d")

            chain = wb.get_options(
                stock=ticker,
                expireDate=exp_date_str,
                direction=option_type.lower()
            )

            if not chain:
                return None

            target_strike = float(strike)
            for entry in chain:
                entry_strike = float(entry.get("strikePrice", 0))
                if abs(entry_strike - target_strike) < 0.01:
                    direction_key = option_type.lower()
                    if direction_key in entry:
                        tid = entry[direction_key].get("tickerId")
                        if tid:
                            logger.info(f"[PaperTrader] Resolved instrumentId={tid} for {ticker} {option_type} ${strike} exp {exp_date_str}")
                            return str(tid)

            return None
        except Exception as e:
            logger.error(f"[PaperTrader] Error resolving option instrument: {e}")
            return None

    # ── Order Placement ─────────────────────────────────────────────

    def place_option_order(self, ticker, signal_type, last_price, vwap_target=None, zone_summary=None, in_zone=False, setup_reason=None, hod=None, lod=None, pmh=None, pml=None, ema8=None, stop_loss_price=None, target_1_price=None, force=False):
        """
        Find the best option contract for the signal and place an order.

        Args:
            ticker: Stock symbol (e.g. "AAPL")
            signal_type: "bullish" or "bearish"
            last_price: Current stock price
            vwap_target: Target VWAP price for take-profit / entry (optional)
            zone_summary: Supply/Demand zone range string (optional)
            in_zone: Whether stock was inside an active zone (optional)
            setup_reason: Text description of the confluence setup (optional)
            hod: High of Day at trigger
            lod: Low of Day at trigger
            pmh: Premarket High
            pml: Premarket Low
            ema8: 8 EMA at trigger
            stop_loss_price: Stock price stop loss (optional)
            target_1_price: Target 1 scale-out stock price (optional)
            force: If True, bypasses trading window, duplicate position, risk limit checks, and delayed options check for test/manual execution.

        Returns:
            dict with order details, or None on failure
        """
        if not self.enabled and not force:
            logger.info(f"[PaperTrader] Paper trading disabled — skipping {ticker}")
            return None

        if not self._logged_in:
            if not self.login():
                logger.error("[PaperTrader] Cannot place order — not logged in")
                return None

        # Guard: Trading window check (allow trade entries only from 9:40 AM to 11:30 AM ET)
        if not force and (self.earliest_trade_mins > 0 or self.latest_trade_mins > 0):
            ny_tz = get_ny_timezone()
            now = datetime.now(ny_tz)
            current_mins = now.hour * 60 + now.minute
            if self.earliest_trade_mins > 0 and current_mins < self.earliest_trade_mins:
                open_hr = self.earliest_trade_mins // 60
                open_mn = self.earliest_trade_mins % 60
                suffix = "AM" if open_hr < 12 else "PM"
                disp_hr = open_hr if open_hr <= 12 else open_hr - 12
                if disp_hr == 0:
                    disp_hr = 12
                logger.info(
                    f"[PaperTrader] ⏳ Outside trading window (trading begins at {disp_hr}:{open_mn:02d} {suffix} ET) — skipping {ticker}"
                )
                return None
            if self.latest_trade_mins > 0 and current_mins > self.latest_trade_mins:
                close_hr = self.latest_trade_mins // 60
                close_mn = self.latest_trade_mins % 60
                suffix = "AM" if close_hr < 12 else "PM"
                disp_hr = close_hr if close_hr <= 12 else close_hr - 12
                if disp_hr == 0:
                    disp_hr = 12
                logger.info(
                    f"[PaperTrader] ⏳ Outside trading window (trading allowed until {disp_hr}:{close_mn:02d} {suffix} ET) — skipping {ticker}"
                )
                return None

        # Verify ticker is in approved Top 50 list (skipped if force=True)
        if not force:
            try:
                top50_file = os.path.join(_SCAN_DATA_DIR, "top50_stocks.json")
                if not os.path.exists(top50_file):
                    top50_file = os.path.join(os.path.dirname(__file__), "top50_stocks.json")
                if os.path.exists(top50_file):
                    with open(top50_file, "r") as f:
                        data = json.load(f)
                        allowed = set(item["ticker"].strip().upper() for item in data if isinstance(item, dict) and item.get("ticker"))
                    if ticker.upper() not in allowed:
                        logger.warning(f"[PaperTrader] Ticker {ticker} is not in approved Top 50 list — skipping order.")
                        return None
            except Exception as e:
                logger.debug(f"[PaperTrader] Top 50 validation warning: {e}")

        # Guard: Check if an active open position already exists for this ticker (skipped if force=True)
        if not force:
            open_positions = self.get_open_positions()
            existing = [p for p in open_positions if p.get("ticker", "").upper() == ticker.upper() and p.get("status") == "open"]
            if existing:
                logger.warning(
                    f"[PaperTrader] ⛔ Active position already open for {ticker} "
                    f"({existing[0].get('option_symbol', '')}) — skipping duplicate order."
                )
                return None

        try:
            # 1. Find the best option contract using existing scanner logic
            sys.path.insert(0, os.path.dirname(__file__))
            from reversal_scanner import find_best_option

            logger.info(f"[PaperTrader] 🔍 Searching for best {signal_type} option on {ticker} @ ${last_price:.2f}...")
            best = find_best_option(
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

            # If no market option found and force=True, synthesize a realistic contract
            if not best and force:
                exp_dt = datetime.now() + timedelta(days=14)
                exp_date_str = exp_dt.strftime("%Y-%m-%d")
                exp_short = exp_dt.strftime("%b %d")
                strike_val = float(round(last_price))
                opt_type = "CALL" if str(signal_type).lower() == "bullish" else "PUT"
                sym = f"{ticker}{exp_dt.strftime('%y%m%d')}{'C' if opt_type == 'CALL' else 'P'}{int(strike_val * 1000):08d}"
                est_mid = max(1.25, round(last_price * 0.025, 2))
                best = {
                    "symbol": sym,
                    "tickerId": f"SIM_{sym}",
                    "strike": strike_val,
                    "type": opt_type,
                    "exp": exp_short,
                    "exp_date": exp_date_str,
                    "dte": 14,
                    "mid": est_mid,
                    "bid": round(est_mid * 0.95, 2),
                    "ask": round(est_mid * 1.05, 2),
                    "spread": round(est_mid * 0.10, 2),
                    "spread_pct": 5.0,
                    "quote_source": "Simulated_Test_Feed",
                    "is_realtime": True,
                }
                logger.info(f"[PaperTrader] Synthesized test option contract for {ticker}: {sym} (${est_mid:.2f})")

            if not best:
                logger.warning(f"[PaperTrader] No suitable option contract found for {ticker} ({signal_type})")
                return None

            is_realtime = bool(best.get("is_realtime", False))
            quote_source = best.get("quote_source", "Unknown")
            rt_tag = "LIVE" if is_realtime else "DELAYED"

            logger.info(
                f"[PaperTrader] Found {rt_tag} Option ({quote_source}): {best['symbol']} | {best['type']} | "
                f"Strike ${best['strike']} | Exp {best['exp']} | "
                f"Bid ${best['bid']:.2f} | Ask ${best['ask']:.2f} | Mid ${best['mid']:.2f} | Spread {best.get('spread_pct', 0)}%"
            )

            if not is_realtime and not self.allow_delayed_options and not force:
                logger.warning(
                    f"[PaperTrader] ⛔ Blocked entry on {ticker}: options data source '{quote_source}' is NOT real-time. "
                    f"Enable US_OPTION in Webull OpenAPI Portal (or set ALLOW_DELAYED_OPTIONS_TRADING=true)."
                )
                self._notify_delayed_data_once(
                    f"Blocked entry on {ticker} ({best['symbol']})",
                    f"Quote source was {quote_source} (15-min delayed)"
                )
                return None

            # 2. Risk check (skipped if force=True)
            ask_price = best.get("ask", best["mid"])
            if not force:
                ok, reason = self._check_risk_limits(ask_price)
                if not ok:
                    logger.warning(f"[PaperTrader] ⛔ Risk check failed for {ticker}: {reason}")
                    return None

            # 3. Get the option's instrument ID (from live chain or lookup)
            instrument_id = best.get("tickerId")
            if not instrument_id:
                instrument_id = self._get_option_instrument_id(
                    ticker, best["strike"], best["type"], best["exp"]
                )

            if not instrument_id:
                if getattr(self, "_is_real_paper_account", False):
                    logger.error(f"[PaperTrader] Could not resolve instrument ID for {best['symbol']}")
                    return None
                else:
                    instrument_id = f"SIM_{ticker}_{best['type']}_{best['strike']}_{best.get('exp', '').replace(' ', '')}"
                    logger.info(f"[PaperTrader] Generated synthetic ID for local simulation: {instrument_id}")

            # 4. Place the order
            client_order_id = str(uuid.uuid4())[:20]
            available_funds = self.get_available_funds()
            single_cost = ask_price * 100
            desired_qty = max(1, self.max_contracts)
            if single_cost * desired_qty > available_funds:
                order_qty = max(1, int(available_funds // single_cost))
                logger.info(f"[PaperTrader] Sizing down {ticker} from {desired_qty} to {order_qty} contract(s) to fit available funds (${available_funds:.2f})")
            else:
                order_qty = desired_qty

            if getattr(self, "_is_real_paper_account", False):
                logger.info(
                    f"[PaperTrader] 📝 Placing Webull Paper API BUY order: {best['type']} on {ticker} | "
                    f"Strike ${best['strike']} | Qty {order_qty} | "
                    f"Limit ${best['mid']:.2f} | instrument_id={instrument_id}"
                )

                new_orders = [{
                    "client_order_id": client_order_id,
                    "order_type": "LIMIT",
                    "time_in_force": "DAY",
                    "side": "BUY",
                    "extended_hours_trading": False,
                    "legs": [{
                        "instrument_id": instrument_id,
                        "instrument_type": "OPTION",
                        "market": "US",
                        "side": "BUY",
                        "qty": str(order_qty),
                        "limit_price": str(best["mid"]),
                    }]
                }]

                resp = self._trade_client.order_v2.place_option(
                    account_id=self._account_id,
                    new_orders=new_orders
                )

                result = resp.json() if resp.status_code == 200 else {"error": resp.text[:500], "status": resp.status_code}
                logger.info(f"[PaperTrader] ✅ Webull API Order response (status={resp.status_code}): {json.dumps(result)[:300]}")
            else:
                logger.info(
                    f"[PaperTrader] 🛡️ [SIMULATION MODE] Executing Simulated Paper BUY: {best['type']} on {ticker} | "
                    f"Strike ${best['strike']} | Qty {order_qty} | "
                    f"Simulated Fill Price ${best['mid']:.2f}"
                )
                result = {"mode": "100% Safe Local Simulation", "status": "FILLED", "fill_price": best["mid"]}

            # 5. Record the trade
            ny_tz = get_ny_timezone()
            trade_record = {
                "id": len(self._trade_log.get("trades", [])) + 1,
                "ticker": ticker,
                "option_symbol": best.get("symbol", ""),
                "instrument_id": instrument_id,
                "client_order_id": client_order_id,
                "strike": best["strike"],
                "type": best["type"],
                "signal_type": signal_type,
                "entry_price": best["mid"],
                "bid": best.get("bid"),
                "ask": best.get("ask"),
                "spread": best.get("spread"),
                "spread_pct": best.get("spread_pct"),
                "entry_time": datetime.now(ny_tz).isoformat(),
                "vwap_entry": vwap_target,
                "vwap_target": None,
                "ema8_entry": ema8,
                "hod": hod,
                "lod": lod,
                "pmh": pmh,
                "pml": pml,
                "stop_loss": stop_loss_price,
                "tp1_target": target_1_price or (hod if signal_type == "bullish" else lod),
                "tp1_hit": False,
                "runner_active": False,
                "stock_price_at_entry": last_price,
                "quantity": order_qty,
                "dte": best["dte"],
                "expiration": best.get("exp", ""),
                "expiration_date": best.get("exp_date", ""),
                "zone": zone_summary,
                "in_zone": in_zone,
                "setup": setup_reason,
                "status": "open",
                "is_test": bool(force),
                "exit_price": None,
                "exit_time": None,
                "pnl": None,
                "exit_reason": None,
                "mode": "WEBULL_PAPER_API" if getattr(self, "_is_real_paper_account", False) else "LOCAL_SIMULATION",
                "quote_source": quote_source,
                "is_realtime": is_realtime,
                "order_response": result,
            }

            with self._lock:
                self._trade_log.setdefault("trades", []).append(trade_record)
                self._open_positions.append(trade_record)
                _save_trade_log(self._trade_log)

            return trade_record

        except Exception as e:
            logger.error(f"[PaperTrader] ❌ Error placing option order for {ticker}: {e}")
            import traceback
            traceback.print_exc()
    def get_open_positions(self):
        """Return all open positions dynamically reloaded from disk."""
        with self._lock:
            self._trade_log = _load_trade_log()
            self._open_positions = [t for t in self._trade_log.get("trades", []) if t.get("status") == "open"]
            return list(self._open_positions)

    # ── Position Management / Exits ─────────────────────────────────

    def get_trade_option_quote(self, trade_record):
        """Fetch live option quote (bid, ask, mid) for an open trade using Webull or Yahoo fallback."""
        ticker = trade_record.get("ticker")
        strike = trade_record.get("strike")
        opt_type = trade_record.get("type")
        exp_date_str = trade_record.get("expiration_date")

        # 1. Primary: Resolve clean YYYY-MM-DD expiration date
        if not exp_date_str:
            exp_str = trade_record.get("expiration", "")
            if "-" in exp_str and len(exp_str) == 10:
                exp_date_str = exp_str
            elif exp_str:
                curr_year = datetime.now().year
                try:
                    exp_date_str = datetime.strptime(f"{exp_str} {curr_year}", "%b %d %Y").strftime("%Y-%m-%d")
                except Exception:
                    pass

        if ticker and strike and opt_type and exp_date_str:
            try:
                from data_fetcher import get_option_live_quote
                live_q = get_option_live_quote(
                    ticker=ticker,
                    strike=strike,
                    option_type=opt_type,
                    exp_date_str=exp_date_str,
                    option_symbol=trade_record.get("option_symbol")
                )
                if live_q:
                    return live_q
            except Exception as ex:
                logger.debug(f"[PaperTrader] Live option quote error for {ticker}: {ex}")

        # 2. Fallback: Query Webull quote by instrument_id if available
        instrument_id = trade_record.get("instrument_id")
        if ticker and instrument_id and not instrument_id.startswith("SIM_"):
            try:
                from data_fetcher import get_unofficial_client
                wb = get_unofficial_client()
                if wb:
                    quote = wb.get_option_quote(stock=ticker, optionId=instrument_id)
                    if quote and "data" in quote and quote["data"]:
                        q = quote["data"][0]
                        bid_list = q.get("bidList", [])
                        ask_list = q.get("askList", [])
                        bid = float(bid_list[0]["price"]) if bid_list and len(bid_list) > 0 else None
                        ask = float(ask_list[0]["price"]) if ask_list and len(ask_list) > 0 else None
                        mid = round((bid + ask) / 2.0, 2) if (bid is not None and ask is not None) else None
                        return {"bid": bid, "ask": ask, "mid": mid}
            except Exception:
                pass

        return None

    def estimate_option_exit_price(self, trade_record, current_stock_price=None):
        """
        Realistic fallback estimate for option price when live option quote is unavailable.
        Calculates intrinsic value and delta-based price movement from entry.
        Strictly prevents fabricating a gain if the underlying stock moved against the position.
        """
        try:
            entry_opt_price = float(trade_record.get("entry_price") or 1.0)
            entry_stock_price = trade_record.get("stock_price_at_entry")
            strike = float(trade_record.get("strike") or 0.0)
            opt_type = str(trade_record.get("type") or "CALL").upper()

            # If current stock price not provided, attempt a quick fetch
            if current_stock_price is None or current_stock_price <= 0:
                try:
                    from data_fetcher import fetch_one
                    df_bar = fetch_one(trade_record.get("ticker"), days=1, interval="5m")
                    if df_bar is not None and not df_bar.empty:
                        current_stock_price = float(df_bar["Close"].iloc[-1])
                except Exception:
                    pass

            if current_stock_price is None or current_stock_price <= 0:
                return max(0.05, round(entry_opt_price * 0.50, 2))

            current_stock_price = float(current_stock_price)

            if opt_type == "PUT":
                intrinsic_val = max(0.0, strike - current_stock_price)
                if entry_stock_price and entry_stock_price > 0:
                    dS = current_stock_price - float(entry_stock_price)
                    # Put delta is negative (~ -0.50): as stock rises, put premium drops
                    delta_change = -0.50 * dS
                    est_val = max(intrinsic_val, entry_opt_price + delta_change)
                else:
                    est_val = intrinsic_val
                return max(0.05, round(est_val, 2))
            else:  # CALL
                intrinsic_val = max(0.0, current_stock_price - strike)
                if entry_stock_price and entry_stock_price > 0:
                    dS = current_stock_price - float(entry_stock_price)
                    # Call delta is positive (~ +0.50): as stock drops, call premium drops
                    delta_change = 0.50 * dS
                    est_val = max(intrinsic_val, entry_opt_price + delta_change)
                else:
                    est_val = intrinsic_val
                return max(0.05, round(est_val, 2))
        except Exception as e:
            logger.error(f"[PaperTrader] Error estimating option exit price: {e}")
            return max(0.05, round(float(trade_record.get("entry_price", 1.0)) * 0.50, 2))

    def close_position(self, trade_record, exit_price=None, reason="manual", qty_to_close=None):
        """Sell to close an open option position (supports full or partial exit)."""
        if not self._logged_in:
            return False

        try:
            instrument_id = trade_record.get("instrument_id")
            total_qty = trade_record.get("quantity", 1)
            close_qty = int(qty_to_close) if (qty_to_close and 1 <= int(qty_to_close) < total_qty) else total_qty
            is_partial = (close_qty < total_qty)

            # Get current option quote for realistic exit price
            if exit_price is None or exit_price <= 0:
                opt_q = self.get_trade_option_quote(trade_record)
                if opt_q:
                    bid = opt_q.get("bid")
                    mid = opt_q.get("mid")
                    if bid is not None and bid > 0:
                        exit_price = bid
                    elif mid is not None and mid > 0:
                        exit_price = mid

            # If live quote is still missing, calculate delta-adjusted intrinsic price
            if exit_price is None or exit_price <= 0:
                exit_price = self.estimate_option_exit_price(trade_record)
                logger.info(
                    f"[PaperTrader] ℹ️ Live quote unavailable on exit — estimated realistic option price for "
                    f"{trade_record['ticker']} {trade_record['type']} as ${exit_price:.2f}"
                )

            client_order_id = str(uuid.uuid4())[:20]

            logger.info(
                f"[PaperTrader] 📤 Closing {'partial' if is_partial else 'full'} position: {trade_record['ticker']} "
                f"{trade_record['type']} ${trade_record['strike']} | Qty: {close_qty}/{total_qty} | "
                f"Exit Price: ${exit_price:.2f} | Reason: {reason}"
            )

            if getattr(self, "_is_real_paper_account", False) and instrument_id and not instrument_id.startswith("SIM_"):
                new_orders = [{
                    "client_order_id": client_order_id,
                    "order_type": "MARKET" if not exit_price else "LIMIT",
                    "time_in_force": "DAY",
                    "side": "SELL",
                    "extended_hours_trading": False,
                    "legs": [{
                        "instrument_id": instrument_id,
                        "instrument_type": "OPTION",
                        "market": "US",
                        "side": "SELL",
                        "qty": str(close_qty),
                        "limit_price": str(exit_price) if exit_price else None,
                    }]
                }]

                resp = self._trade_client.order_v2.place_option(
                    account_id=self._account_id,
                    new_orders=new_orders
                )

                result = resp.json() if resp.status_code == 200 else {"error": resp.text[:500]}
                logger.info(f"[PaperTrader] Close order result: {json.dumps(result)[:300]}")
            else:
                logger.info(
                    f"[PaperTrader] 🛡️ [SIMULATION MODE] Simulated Close executed for {trade_record['ticker']} "
                    f"(Qty: {close_qty}) @ realistic exit price ${exit_price:.2f}"
                )

            # Update trade record
            ny_tz = get_ny_timezone()
            entry_price = trade_record.get("entry_price", 0)
            pnl = ((exit_price or 0) - entry_price) * 100 * close_qty if exit_price is not None else 0.0

            with self._lock:
                if is_partial:
                    trade_record["quantity"] = total_qty - close_qty
                    trade_record.setdefault("partial_exits", []).append({
                        "exit_price": exit_price,
                        "exit_time": datetime.now(ny_tz).isoformat(),
                        "exit_reason": reason,
                        "quantity": close_qty,
                        "pnl": round(pnl, 2)
                    })
                    trade_record["pnl"] = round((trade_record.get("pnl") or 0.0) + (pnl or 0.0), 2)
                else:
                    trade_record["status"] = "closed"
                    trade_record["exit_price"] = exit_price
                    trade_record["exit_time"] = datetime.now(ny_tz).isoformat()
                    trade_record["exit_reason"] = reason
                    trade_record["pnl"] = round((trade_record.get("pnl") or 0.0) + (pnl or 0.0), 2)
                    self._open_positions = [
                        p for p in self._open_positions if p.get("id") != trade_record.get("id")
                    ]
                _save_trade_log(self._trade_log)

            pnl_str = f"${pnl:+.2f}" if pnl is not None else "unknown"
            logger.info(
                f"[PaperTrader] {'🟢' if (pnl or 0) >= 0 else '🔴'} "
                f"Closed {'partial' if is_partial else 'full'} {trade_record['ticker']} {trade_record['type']} | "
                f"P&L: {pnl_str} | Reason: {reason}"
            )

            # Send Telegram Exit Notification
            try:
                from indicator_bot import send_telegram_notification, send_sms_notification
                exit_icon = "🎯" if (pnl or 0) >= 0 else "🛑"
                pnl_pct_val = ((exit_price - entry_price) / entry_price * 100) if entry_price and exit_price else 0.0
                reason_clean = reason.replace('_', ' ').title()
                if trade_record.get("is_test") or str(trade_record.get("setup", "")).startswith("Verification Test Trade"):
                    reason_clean = f"⚡ {reason_clean} (Bot Auto-Exit)"
                exit_msg = (
                    f"{exit_icon} <b>POSITION CLOSED: {trade_record['ticker']}</b>\n\n"
                    f"<b>Option:</b> {trade_record.get('type', '')} ${trade_record.get('strike', '')} ({trade_record.get('option_symbol', '')})\n"
                    f"<b>Entry:</b> ${entry_price:.2f} | <b>Exit:</b> ${exit_price:.2f}\n"
                    f"<b>P&L:</b> {pnl_str} ({pnl_pct_val:+.1f}%)\n"
                    f"<b>Type:</b> {'Partial (Scale Out)' if is_partial else 'Full Exit'}\n"
                    f"<b>Reason:</b> {reason_clean}"
                )
                send_telegram_notification(exit_msg)
                alert_method = os.getenv("ALERT_METHOD", "TELEGRAM").upper()
                if alert_method in ("SMS", "BOTH"):
                    send_sms_notification(f"{exit_icon} POSITION CLOSED: {trade_record['ticker']} | P&L: {pnl_str} ({pnl_pct_val:+.1f}%) | {reason_clean}")
            except Exception as notify_err:
                logger.warning(f"Failed to send exit notification: {notify_err}")

            return True

        except Exception as e:
            logger.error(f"[PaperTrader] Error closing position: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _check_exits(self):
        """Check all open positions for exit conditions (stop-loss, take-profit, trailing-stop, EOD)."""
        if not self._logged_in or not self._open_positions:
            return

        ny_tz = get_ny_timezone()
        now = datetime.now(ny_tz)
        current_minutes = now.hour * 60 + now.minute
        eod_minutes = 15 * 60 + 55  # 3:55 PM

        positions_to_close = []
        trade_log_dirty = False

        for trade in list(self._open_positions):
            if trade.get("status") != "open":
                continue

            try:
                # 1. Fetch live option quote
                opt_q = self.get_trade_option_quote(trade)
                current_bid = opt_q.get("bid") if opt_q else None
                current_mid = opt_q.get("mid") if opt_q else None

                # For real executable exits and P&L, executable bid is primary
                current_price = current_bid if (current_bid is not None and current_bid > 0) else current_mid

                entry_price = trade.get("entry_price", 0)
                pnl_pct = 0.0
                if current_price and entry_price > 0:
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100.0

                    # Track peak P&L for trailing stop
                    if pnl_pct > trade.get("peak_pnl_pct", -999.0):
                        trade["peak_pnl_pct"] = round(pnl_pct, 2)
                        trade_log_dirty = True

                # ── Fast Test Trade Auto-Close Lifecycle ──
                # Allows test trades to auto-close after 60-90s so the user verifies the full lifecycle:
                # Entry -> Monitor -> Auto-Exit -> P&L Calculation -> Telegram Exit Alert
                is_test_trade = trade.get("is_test") or str(trade.get("setup", "")).startswith("Verification Test Trade")
                if is_test_trade:
                    entry_time_str = trade.get("entry_time")
                    if entry_time_str:
                        try:
                            entry_dt = datetime.fromisoformat(entry_time_str)
                            hold_secs = (now - entry_dt).total_seconds()
                            test_auto_close_secs = int(os.getenv("TEST_TRADE_AUTO_CLOSE_SECS", "60"))
                            if hold_secs >= test_auto_close_secs:
                                exit_px = current_price
                                if not exit_px or exit_px <= 0:
                                    # Realistic simulated exit price (+6% simulated profit on test)
                                    exit_px = round(entry_price * 1.06, 2)
                                logger.info(
                                    f"[PaperTrader] ⚡ Test trade {trade['ticker']} held {hold_secs:.0f}s >= {test_auto_close_secs}s. "
                                    f"Bot auto-closing test trade to complete verification lifecycle."
                                )
                                positions_to_close.append((trade, "test_lifecycle_auto_exit", exit_px))
                                continue
                        except Exception as e:
                            logger.debug(f"[PaperTrader] Test trade timer error: {e}")

                # ── Strategy 5-Minute Candle Exits (8 EMA Trend Exit & VWAP Stop Loss) ──
                if getattr(self, "use_strategy_vwap_exits", False):
                    try:
                        from data_fetcher import fetch_one
                        df_bars = fetch_one(trade["ticker"], days=2, interval="5m", includePrePost="true")
                        if df_bars is not None and len(df_bars) >= 5:
                            today_date = df_bars.index[-1].date()
                            today_df = df_bars[df_bars.index.date == today_date]
                            rth_bars = today_df.between_time("09:30", "16:00")
                            if len(rth_bars) >= 2:
                                # Market Hours VWAP (starts at 09:30 AM market open) & 8 EMA
                                typical_price = (rth_bars["High"] + rth_bars["Low"] + rth_bars["Close"]) / 3.0
                                cum_vol = rth_bars["Volume"].cumsum().replace(0, 1)
                                vwap_s = (typical_price * rth_bars["Volume"]).cumsum() / cum_vol
                                ema8_s = rth_bars["Close"].ewm(span=8, adjust=False).mean()

                                # Last closed 5m candle
                                last_closed = rth_bars.iloc[-2]
                                c_open = float(last_closed["Open"])
                                c_high = float(last_closed["High"])
                                c_low = float(last_closed["Low"])
                                c_close = float(last_closed["Close"])
                                c_vwap = float(vwap_s.loc[last_closed.name]) if last_closed.name in vwap_s.index else float(vwap_s.iloc[-1])
                                c_ema8 = float(ema8_s.loc[last_closed.name]) if last_closed.name in ema8_s.index else float(ema8_s.iloc[-1])
                                sig = trade.get("signal_type", "bullish")

                                # Guard: Ensure last_closed candle completed AFTER the position was opened
                                # A 5-minute candle timestamp in Webull marks the start of the bar.
                                # The trade was entered during entry_dt. The candle that was forming at entry_dt
                                # is the entry candle. We MUST NOT evaluate any candle from before entry or the entry candle itself!
                                # The first candle to evaluate is the NEXT completed 5-minute candle (candle_dt > entry_dt).
                                entry_time_str = trade.get("entry_time")
                                if entry_time_str:
                                    try:
                                        entry_dt = datetime.fromisoformat(entry_time_str)
                                        candle_ts = last_closed.name
                                        if hasattr(candle_ts, "tzinfo") and candle_ts.tzinfo is not None:
                                            candle_dt = candle_ts.tz_convert(ny_tz)
                                        else:
                                            candle_dt = candle_ts.replace(tzinfo=ny_tz) if hasattr(candle_ts, "replace") else candle_ts
                                        
                                        # If candle started at or before the entry timestamp, it is NOT the next candle!
                                        # Only evaluate completed candles that started strictly AFTER entry.
                                        if candle_dt <= entry_dt:
                                            continue
                                    except Exception as ex:
                                        logger.warning(f"[PaperTrader] Candle timing check error: {ex} — skipping exit check for safety")
                                        continue

                                # ── PMB Strategy Exits (Hybrid Invalidation & 2:1 Asymmetry) ──
                                hard_sl = trade.get("stop_loss")
                                tp_2r = trade.get("tp1_target")
                                pmh = trade.get("pmh")
                                pml = trade.get("pml")
                                entry_stock = trade.get("stock_price_at_entry") or c_open
                                curr_stock_price = float(rth_bars["Close"].iloc[-1])

                                if sig == "bullish":
                                    # 1. Hard Emergency Stop (Max Loss Protection: candle low <= hard_sl OR current stock <= hard_sl)
                                    if hard_sl and (c_low <= hard_sl or curr_stock_price <= hard_sl):
                                        logger.info(
                                            f"[PaperTrader] 🛑 HARD EMERGENCY STOP on {trade['ticker']} | "
                                            f"Stock=${curr_stock_price:.2f} <= Hard SL=${hard_sl:.2f}"
                                        )
                                        positions_to_close.append((trade, "hard_emergency_stop", current_price))
                                        continue

                                    # 2. Structural Stop (Early Invalidation: candle closes below PMH OR current stock below PMH)
                                    if pmh and (c_close < pmh or curr_stock_price < pmh):
                                        logger.info(
                                            f"[PaperTrader] 🛑 STRUCTURAL STOP (PMH Invalidation) on {trade['ticker']} | "
                                            f"Stock Below PMH (Close=${c_close:.2f}, Current=${curr_stock_price:.2f} < PMH=${pmh:.2f})"
                                        )
                                        positions_to_close.append((trade, "structural_stop_pmh", current_price))
                                        continue

                                    # 3. Profit Target (2:1 Asymmetry / 2R) — Strictly requires stock trading ABOVE entry price!
                                    if tp_2r and (c_high >= tp_2r or curr_stock_price >= tp_2r) and (curr_stock_price >= entry_stock):
                                        logger.info(
                                            f"[PaperTrader] 🎯 2:1 PROFIT TARGET HIT on {trade['ticker']} | "
                                            f"Stock High=${c_high:.2f} >= 2R Target=${tp_2r:.2f}"
                                        )
                                        positions_to_close.append((trade, "profit_target_2r", current_price))
                                        continue

                                elif sig == "bearish":
                                    # 1. Hard Emergency Stop (Max Loss Protection: candle high >= hard_sl OR current stock >= hard_sl)
                                    if hard_sl and (c_high >= hard_sl or curr_stock_price >= hard_sl):
                                        logger.info(
                                            f"[PaperTrader] 🛑 HARD EMERGENCY STOP on {trade['ticker']} | "
                                            f"Stock=${curr_stock_price:.2f} >= Hard SL=${hard_sl:.2f}"
                                        )
                                        positions_to_close.append((trade, "hard_emergency_stop", current_price))
                                        continue

                                    # 2. Structural Stop (Early Invalidation: candle closes above PML OR current stock above PML)
                                    if pml and (c_close > pml or curr_stock_price > pml):
                                        logger.info(
                                            f"[PaperTrader] 🛑 STRUCTURAL STOP (PML Invalidation) on {trade['ticker']} | "
                                            f"Stock Above PML (Close=${c_close:.2f}, Current=${curr_stock_price:.2f} > PML=${pml:.2f})"
                                        )
                                        positions_to_close.append((trade, "structural_stop_pml", current_price))
                                        continue

                                    # 3. Profit Target (2:1 Asymmetry / 2R) — Strictly requires stock trading BELOW entry price!
                                    if tp_2r and (c_low <= tp_2r or curr_stock_price <= tp_2r) and (curr_stock_price <= entry_stock):
                                        logger.info(
                                            f"[PaperTrader] 🎯 2:1 PROFIT TARGET HIT on {trade['ticker']} | "
                                            f"Stock Low=${c_low:.2f} <= 2R Target=${tp_2r:.2f}"
                                        )
                                        positions_to_close.append((trade, "profit_target_2r", current_price))
                                        continue
                    except Exception as ex:
                        logger.debug(f"[PaperTrader] 5m strategy exit check error for {trade['ticker']}: {ex}")

                # 2. Hard Stop-Loss (Emergency capital protection)
                if current_price and entry_price > 0:
                    loss_pct = -pnl_pct
                    if loss_pct >= self.stop_loss_pct:
                        logger.warning(
                            f"[PaperTrader] 🛑 STOP-LOSS triggered on {trade['ticker']} | "
                            f"Entry: ${entry_price:.2f} → Bid: ${current_price:.2f} | Loss: {loss_pct:.1f}% >= {self.stop_loss_pct}%"
                        )
                        positions_to_close.append((trade, "stop_loss", current_price))
                        continue

                # 3. Breakeven Stop Protection (Locks in Breakeven once trade gains >= breakeven_stop_pct)
                if self.breakeven_stop_pct > 0:
                    peak = trade.get("peak_pnl_pct", 0.0)
                    if peak >= self.breakeven_stop_pct:
                        if not trade.get("breakeven_locked"):
                            trade["breakeven_locked"] = True
                            trade_log_dirty = True
                            logger.info(f"[PaperTrader] 🔒 BREAKEVEN LOCKED for {trade['ticker']} (Peak: +{peak:.1f}% >= +{self.breakeven_stop_pct}%)")

                        if pnl_pct <= 0.0:
                            logger.info(
                                f"[PaperTrader] 🛡️ BREAKEVEN STOP triggered on {trade['ticker']} | "
                                f"Peak was +{peak:.1f}%, now {pnl_pct:+.1f}% <= 0.0% | Exiting to protect capital."
                            )
                            positions_to_close.append((trade, "breakeven_stop", current_price))
                            continue

                # 4. Fixed Option Take-Profit Target (only when strategy 8 EMA exits are disabled)
                if not getattr(self, "use_strategy_vwap_exits", False) and current_price and entry_price > 0 and self.take_profit_pct > 0:
                    if pnl_pct >= self.take_profit_pct:
                        logger.info(
                            f"[PaperTrader] 🎯 TAKE-PROFIT (Target Reached) on {trade['ticker']} | "
                            f"Entry: ${entry_price:.2f} → Bid: ${current_price:.2f} | Gain: +{pnl_pct:.1f}% >= +{self.take_profit_pct}%"
                        )
                        positions_to_close.append((trade, "take_profit", current_price))
                        continue

                # 5. Trailing Stop Protection
                if self.trailing_stop_enabled and trade.get("peak_pnl_pct", 0) >= self.trailing_stop_activation_pct:
                    peak = trade["peak_pnl_pct"]
                    min_floor = 0.5 if trade.get("breakeven_locked") else 0.0
                    stop_floor_pct = max(min_floor, peak - self.trailing_stop_callback_pct)
                    if pnl_pct <= stop_floor_pct:
                        logger.info(
                            f"[PaperTrader] 🛡️ TRAILING STOP triggered on {trade['ticker']} | "
                            f"Peak: +{peak:.1f}% → Now: {pnl_pct:+.1f}% (Floor: +{stop_floor_pct:.1f}%) | Locking in gains"
                        )
                        positions_to_close.append((trade, "trailing_stop", current_price))
                        continue

                # 5. Take-Profit @ VWAP (Requires Option Profitability Gate)
                if self.take_profit_vwap and trade.get("vwap_target"):
                    try:
                        from data_fetcher import get_unofficial_client
                        wb = get_unofficial_client()
                        if wb:
                            stock_quote = wb.get_quote(stock=trade["ticker"])
                            stock_price = float(stock_quote.get("close", 0)) if stock_quote else 0
                            vwap_target = trade["vwap_target"]

                            hit = (trade["signal_type"] == "bullish" and stock_price >= vwap_target) or \
                                  (trade["signal_type"] == "bearish" and stock_price <= vwap_target)
                            if hit:
                                if current_price and entry_price > 0:
                                    if pnl_pct >= self.vwap_min_profit_pct:
                                        logger.info(
                                            f"[PaperTrader] 🎯 TAKE-PROFIT @ VWAP on {trade['ticker']} | "
                                            f"Stock: ${stock_price:.2f} (Target: ${vwap_target:.2f}) | Option P&L: +{pnl_pct:.1f}% >= +{self.vwap_min_profit_pct}%"
                                        )
                                        positions_to_close.append((trade, "take_profit", current_price))
                                        continue
                                    else:
                                        logger.info(
                                            f"[PaperTrader] ⏳ VWAP target touched on {trade['ticker']} (${stock_price:.2f}), "
                                            f"but option P&L is {pnl_pct:+.1f}% (< min +{self.vwap_min_profit_pct}%). Holding position."
                                        )
                                else:
                                    # Fallback if quote is unavailable
                                    positions_to_close.append((trade, "take_profit", current_price))
                                    continue
                    except Exception as ex:
                        logger.debug(f"[PaperTrader] VWAP exit check error: {ex}")

                # 6. Stagnation / Time-Stop Exit (Exits trades open >= max_hold_minutes to avoid theta drag)
                if self.max_hold_minutes > 0 and trade.get("entry_time"):
                    try:
                        entry_dt = datetime.fromisoformat(trade["entry_time"])
                        hold_duration_mins = (now - entry_dt).total_seconds() / 60.0
                        if hold_duration_mins >= self.max_hold_minutes:
                            logger.info(
                                f"[PaperTrader] ⌛ TIME-STOP / STAGNATION on {trade['ticker']} | "
                                f"Held {hold_duration_mins:.0f}m >= {self.max_hold_minutes}m | P&L: {pnl_pct:+.1f}% | Exiting to avoid theta bleed"
                            )
                            positions_to_close.append((trade, "time_stop", current_price))
                            continue
                    except Exception as ex:
                        logger.debug(f"[PaperTrader] Time-stop check error: {ex}")

                # 7. End-of-Day (EOD) Close
                if self.close_before_eod and current_minutes >= eod_minutes:
                    logger.info(f"[PaperTrader] 🕓 EOD close for {trade['ticker']} at {now.strftime('%H:%M')}")
                    positions_to_close.append((trade, "eod_close", current_price))
                    continue

                # Status log for monitoring
                if current_price and entry_price > 0:
                    logger.info(
                        f"[PaperTrader] 📊 {trade['ticker']} {trade['type']} ${trade['strike']} | "
                        f"Entry: ${entry_price:.2f} → Bid: ${current_price:.2f} | P&L: {pnl_pct:+.1f}% (Peak: +{trade.get('peak_pnl_pct', 0):.1f}%)"
                    )

            except Exception as e:
                logger.error(f"[PaperTrader] Error checking exit for {trade['ticker']}: {e}")

        if trade_log_dirty:
            with self._lock:
                _save_trade_log(self._trade_log)

        for item in positions_to_close:
            if len(item) == 3:
                trade, reason, px = item
                self.close_position(trade, exit_price=px, reason=reason)
            else:
                trade, reason = item
                self.close_position(trade, reason=reason)

    def monitor_loop(self):
        """Background thread: checks open positions every 30s for exit conditions."""
        logger.info("[PaperTrader] Position monitor thread started.")
        while True:
            try:
                ny_tz = get_ny_timezone()
                now = datetime.now(ny_tz)
                mins = now.hour * 60 + now.minute
                if now.weekday() >= 5 or mins < 570 or mins > 975:  # 9:30-4:15
                    time.sleep(60)
                    continue
                if self._open_positions:
                    self._check_exits()
                time.sleep(30)
            except Exception as e:
                logger.error(f"[PaperTrader] Monitor loop error: {e}")
                time.sleep(60)

    def start_monitor_thread(self):
        """Start the position monitor as a daemon background thread."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("[PaperTrader] Position monitor thread spawned.")

    # ── Status & Stats ──────────────────────────────────────────────

    def get_status(self):
        """Get paper trader status summary."""
        account = self.get_account()
        trades = self._trade_log.get("trades", [])
        closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
        total_pnl = sum(t["pnl"] for t in closed)
        wins = len([t for t in closed if t["pnl"] > 0])
        losses = len([t for t in closed if t["pnl"] <= 0])

        return {
            "enabled": self.enabled,
            "logged_in": self._logged_in,
            "account_id": self._account_id,
            "open_positions": len(self._open_positions),
            "daily_trades": self._daily_trade_count(),
            "total_trades": len(trades),
            "total_pnl": round(total_pnl, 2),
            "win_count": wins,
            "loss_count": losses,
            "win_rate": round(wins / max(1, wins + losses) * 100, 1),
            "account_balance": account,
            "status": account,
            "risk_limits": {
                "max_contracts": self.max_contracts,
                "max_risk_per_trade": self.max_risk_per_trade,
                "max_open_positions": self.max_open_positions,
                "max_daily_trades": self.max_daily_trades,
                "max_daily_loss": self.max_daily_loss,
                "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct,
                "breakeven_stop_pct": self.breakeven_stop_pct,
                "trailing_stop_activation_pct": self.trailing_stop_activation_pct,
                "trailing_stop_callback_pct": self.trailing_stop_callback_pct,
                "max_hold_minutes": self.max_hold_minutes,
                "min_entry_interval_secs": self.min_entry_interval_secs,
                "earliest_trade_mins": self.earliest_trade_mins,
                "latest_trade_mins": self.latest_trade_mins,
            }
        }

    def get_trade_log(self):
        """Return full trade history."""
        return self._trade_log

    def toggle(self, enabled=None):
        """Enable or disable paper trading."""
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = not self.enabled
        logger.info(f"[PaperTrader] Paper trading {'ENABLED' if self.enabled else 'DISABLED'}")
        return self.enabled

    def reset_trade_log(self):
        """Reset trade history and open positions to start fresh."""
        with self._lock:
            self._trade_log = {"trades": [], "daily_stats": {}}
            self._open_positions = []
            _save_trade_log(self._trade_log)
        logger.info("[PaperTrader] 🔄 Trade log and open positions have been reset to empty.")
        return True

    def execute_test_trade(self, ticker="SPY", signal_type="bullish"):
        """
        Executes a real paper/simulated trade immediately for system verification.
        - Resolves current market price for ticker (with resilient fallbacks)
        - Locates liquid option contract (or synthesizes a test contract)
        - Places the order via PaperTrader with force=True
        - Sends Telegram & SMS notification
        - Returns trade record dict
        """
        ticker = (ticker or "SPY").upper().strip()
        signal_type = (signal_type or "bullish").lower().strip()
        logger.info(f"[PaperTrader] ⚡ Initiating immediate test trade for {ticker} ({signal_type.upper()})...")

        # 1. Resolve current market price
        last_price = None
        try:
            from data_fetcher import fetch_one
            df = fetch_one(ticker, days=1, interval="5m")
            if df is not None and not df.empty:
                last_price = float(df["Close"].iloc[-1])
        except Exception as e:
            logger.warning(f"[PaperTrader] fetch_one error for test trade: {e}")

        if not last_price or last_price <= 0:
            try:
                import yfinance as yf
                t = yf.Ticker(ticker)
                fi = getattr(t, "fast_info", None)
                if fi and hasattr(fi, "last_price") and fi.last_price:
                    last_price = float(fi.last_price)
            except Exception:
                pass

        if not last_price or last_price <= 0:
            benchmarks = {"SPY": 595.0, "QQQ": 510.0, "AAPL": 225.0, "NVDA": 145.0, "TSLA": 320.0, "AMD": 140.0}
            last_price = benchmarks.get(ticker, 150.0)

        # 2. Derive reasonable technical anchors
        is_bull = (signal_type == "bullish")
        vwap = round(last_price * (0.998 if is_bull else 1.002), 2)
        ema8 = round(last_price * (0.999 if is_bull else 1.001), 2)
        pmh = round(last_price * (0.995 if is_bull else 1.005), 2)
        pml = round(last_price * (0.985 if is_bull else 0.995), 2)
        stop_loss = round(last_price * 0.99, 2) if is_bull else round(last_price * 1.01, 2)
        target_1 = round(last_price * 1.02, 2) if is_bull else round(last_price * 0.98, 2)
        setup_reason = f"Verification Test Trade ({ticker} {signal_type.upper()}) ⚡"

        # 3. Place order with force=True
        trade_record = self.place_option_order(
            ticker=ticker,
            signal_type=signal_type,
            last_price=last_price,
            vwap_target=vwap,
            zone_summary=f"PMH ${pmh:.2f} / PML ${pml:.2f}",
            in_zone=True,
            setup_reason=setup_reason,
            hod=round(last_price * 1.005, 2),
            lod=round(last_price * 0.995, 2),
            pmh=pmh,
            pml=pml,
            ema8=ema8,
            stop_loss_price=stop_loss,
            target_1_price=target_1,
            force=True
        )

        # 4. Dispatch Telegram & SMS notifications
        if trade_record:
            try:
                from indicator_bot import send_telegram_notification, send_sms_notification
                feed_str = f"{'LIVE' if trade_record.get('is_realtime') else 'SIMULATED'} ({trade_record.get('quote_source', 'Test')})"
                mode_str = trade_record.get('mode', 'Simulation')
                opt_type = trade_record.get('type', 'CALL')
                strike = trade_record.get('strike', 0)
                opt_sym = trade_record.get('option_symbol', '')
                entry_px = trade_record.get('entry_price', 0)
                qty = trade_record.get('quantity', 1)

                tg_msg = (
                    f"⚡ <b>TEST TRADE EXECUTED: {ticker}</b> ⚡\n\n"
                    f"<b>Status:</b> ✅ Verified Working\n"
                    f"<b>Option:</b> {opt_type} ${strike} (<code>{opt_sym}</code>)\n"
                    f"<b>Entry Price:</b> ${entry_px:.2f}\n"
                    f"<b>Quantity:</b> {qty} contract(s)\n"
                    f"<b>Stock Price:</b> ${last_price:.2f} | <b>VWAP:</b> ${vwap:.2f}\n"
                    f"<b>Hard Stop:</b> ${stop_loss:.2f} | <b>2R Target:</b> ${target_1:.2f}\n"
                    f"<b>Setup:</b> {setup_reason}\n"
                    f"<b>Data Feed:</b> {feed_str}\n"
                    f"<b>Mode:</b> {mode_str}"
                )
                send_telegram_notification(tg_msg)
                alert_method = os.getenv("ALERT_METHOD", "TELEGRAM").upper()
                if alert_method in ("SMS", "BOTH"):
                    send_sms_notification(f"⚡ TEST TRADE EXECUTED: {ticker} {opt_type} ${strike} @ ${entry_px:.2f} (Verified Working)")
                logger.info(f"[PaperTrader] 🚀 Test trade executed successfully for {ticker}: {opt_sym} @ ${entry_px:.2f}")
            except Exception as notify_err:
                logger.warning(f"[PaperTrader] Test trade notification error: {notify_err}")

        return trade_record


# ── Module-Level Singleton ──────────────────────────────────────────────

_paper_trader_instance = None

def get_paper_trader():
    """Get or create the global PaperTrader singleton."""
    global _paper_trader_instance
    if _paper_trader_instance is None:
        _paper_trader_instance = PaperTrader()
    return _paper_trader_instance


# ── Interactive Login Helper ────────────────────────────────────────────

def interactive_login():
    """
    Run this interactively to authenticate with Webull via 2FA.
    
    The OpenAPI SDK will:
      1. Create a new token request
      2. Send a notification to your Webull app
      3. Wait for you to approve the 2FA request
      4. Save the token to conf/token.txt (valid ~90 days)
    
    Usage:
      python3 paper_trader.py --login
    """
    print("=" * 60)
    print("  🔐 Webull OpenAPI Login (2FA Required)")
    print("=" * 60)
    print()
    print("This will request a new API token from Webull.")
    print("You MUST approve the request in your Webull mobile app.")
    print("The process will wait up to 5 minutes for your approval.")
    print()

    app_key = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")
    region = os.getenv("WEBULL_REGION", "us")

    if not app_key or not app_secret:
        print("❌ ERROR: Set WEBULL_APP_KEY and WEBULL_APP_SECRET in .env first.")
        return

    print(f"App Key: {app_key[:10]}...")
    print(f"Region: {region}")
    print()
    print("⏳ Requesting token... Check your Webull app for the 2FA prompt!")
    print()

    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient

    api_client = ApiClient(app_key, app_secret, region)
    trade_client = TradeClient(api_client)

    # If we get here, 2FA was approved and token is cached
    print()
    print("✅ Authentication successful! Token saved to conf/token.txt")
    print()

    # Test: get account list
    resp = trade_client.account_v2.get_account_list()
    if resp.status_code == 200:
        accounts = resp.json()
        print("📋 Your accounts:")
        acct_list = accounts if isinstance(accounts, list) else accounts.get("data", accounts.get("accounts", []))
        for acc in acct_list:
            print(f"  • {acc.get('account_id', acc.get('secAccountId', 'N/A'))} — {acc.get('account_type', acc.get('accountType', 'N/A'))}")
    else:
        print(f"⚠️  Could not fetch accounts: {resp.status_code}")

    print()
    print("🎉 You're ready to trade! Start the bot with `python3 app.py`")


if __name__ == "__main__":
    if "--login" in sys.argv:
        interactive_login()
    elif "--test-trade" in sys.argv:
        ticker = "SPY"
        direction = "bullish"
        # Parse optional positional arguments after --test-trade
        idx = sys.argv.index("--test-trade")
        if len(sys.argv) > idx + 1 and not sys.argv[idx + 1].startswith("-"):
            ticker = sys.argv[idx + 1].upper()
        if len(sys.argv) > idx + 2 and not sys.argv[idx + 2].startswith("-"):
            direction = sys.argv[idx + 2].lower()
        pt = get_paper_trader()
        pt.login()
        res = pt.execute_test_trade(ticker=ticker, signal_type=direction)
        print("Test Trade Result:", json.dumps(res, indent=2, default=str))
    else:
        print("Usage:")
        print("  python3 paper_trader.py --login                        # Authenticate with Webull (2FA)")
        print("  python3 paper_trader.py --test-trade [TICKER] [DIR]   # Execute immediate test trade")
        print()
        print("After authentication, the bot will auto-trade when started via app.py")
