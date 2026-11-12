"""
Unit tests for 52-week High/Low criteria integration in the Swing Trade Watchlist scanner.
"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import data_fetcher
import reversal_scanner


def _build_daily_df(closes, opens=None, highs=None, lows=None, volume=3_000_000):
    """Create a synthetic OHLCV DataFrame indexed by NY trading days."""
    n = len(closes)
    dates = pd.date_range("2026-01-02", periods=n, freq="B", tz="America/New_York")
    closes = [float(c) for c in closes]
    opens = [float(o) for o in (opens if opens is not None else closes)]
    if highs is None:
        highs = [max(o, c) + 0.5 for o, c in zip(opens, closes)]
    if lows is None:
        lows = [max(0.1, min(o, c) - 0.5) for o, c in zip(opens, closes)]
    vols = [int(volume) for _ in range(n)]
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=dates,
    )


class TestSwing52WeekCriteria(unittest.TestCase):
    """Verify 52-week high/low extraction, proximity classification, and swing setups."""

    def test_resolve_52w_range_precedence_and_bar_fallback(self):
        closes = [100.0 + (i % 5) for i in range(260)]
        df = _build_daily_df(closes)
        df.attrs["fiftyTwoWeekHigh"] = 140.0
        df.attrs["fiftyTwoWeekLow"] = 70.0

        # Quote metadata wins when present, expanded by current bar extremes if exceeded
        hi, lo = reversal_scanner._resolve_52w_range(
            df, quote={"fiftyTwoWeekHigh": 150.0, "fiftyTwoWeekLow": 65.0}
        )
        self.assertEqual(hi, 150.0)
        self.assertEqual(lo, 65.0)

        # Fallback to attrs when quote is absent
        hi2, lo2 = reversal_scanner._resolve_52w_range(df, quote=None)
        self.assertEqual(hi2, 140.0)
        self.assertEqual(lo2, 70.0)

        # Fallback to trailing 252 daily bars when neither quote nor attrs are populated
        df.attrs.clear()
        hi3, lo3 = reversal_scanner._resolve_52w_range(df, quote=None)
        self.assertEqual(hi3, float(df["High"].tail(252).max()))
        self.assertEqual(lo3, float(df["Low"].tail(252).min()))

    def test_evaluate_52w_proximity_tiers(self):
        # Hits 52w High (+3)
        p_hit_hi = reversal_scanner._evaluate_52w_proximity(200.0, 200.0, 196.0, 200.0, 120.0)
        self.assertEqual(p_hit_hi["status_label"], "Hits 52w High")
        self.assertEqual(p_hit_hi["points"], 3)
        self.assertTrue(p_hit_hi["near_high"])

        # At 52w High <= 3% (+2)
        p_at_hi = reversal_scanner._evaluate_52w_proximity(196.0, 197.0, 194.0, 200.0, 120.0)
        self.assertEqual(p_at_hi["status_label"], "At 52w High")
        self.assertEqual(p_at_hi["points"], 2)

        # Near 52w Low <= 5% (+1)
        p_near_lo = reversal_scanner._evaluate_52w_proximity(125.0, 126.0, 123.5, 200.0, 120.0)
        self.assertEqual(p_near_lo["status_label"], "Near 52w Low")
        self.assertEqual(p_near_lo["points"], 1)
        self.assertTrue(p_near_lo["near_low"])

        # Mid-range > 5% away from both extremes (0 pts)
        p_mid = reversal_scanner._evaluate_52w_proximity(160.0, 162.0, 158.0, 200.0, 120.0)
        self.assertEqual(p_mid["status_label"], "Mid-Range")
        self.assertEqual(p_mid["points"], 0)
        self.assertIsNone(p_mid["side"])

    def _mock_option(self, *args, **kwargs):
        return {
            "symbol": "TEST261218C00100000",
            "strike": 100.0,
            "type": "CALL",
            "exp": "Dec 18",
            "dte": 40,
            "mid": 4.2,
            "bid": 4.15,
            "ask": 4.25,
            "spread": 0.10,
            "spread_pct": 2.4,
            "iv": 30.0,
            "volume": 1200,
            "oi": 4500,
            "est_delta": 0.52,
            "score": 150.0,
        }

    def test_standalone_52w_low_reversal_bullish_hammer(self):
        # Downtrend into 52w low ($100) followed by strong bullish hammer closing at $102.00 (+2.0% above 52w low)
        closes = np.linspace(145.0, 103.0, 69).tolist() + [102.0]
        opens = closes[:-1] + [100.8]
        highs = [c + 0.6 for c in closes[:-1]] + [102.2]
        lows = [c - 0.6 for c in closes[:-1]] + [100.0]
        df = _build_daily_df(closes, opens=opens, highs=highs, lows=lows)

        with patch.object(reversal_scanner, "find_best_option", side_effect=self._mock_option):
            res = reversal_scanner._analyze_swing_trade_setup(
                "AAPL",
                df,
                quote={"marketCap": 2e12, "fiftyTwoWeekHigh": 155.0, "fiftyTwoWeekLow": 100.0},
                market_sentiment={"is_bullish": True},
            )

        self.assertIsNotNone(res)
        self.assertEqual(res["Setup Type"], "52w Low Reversal")
        self.assertEqual(res["Direction"], "Bullish")
        self.assertEqual(res["52W Status"], "At 52w Low")
        self.assertTrue(res["Near_52W"])
        self.assertTrue(res["Near_52W_Low"])
        self.assertFalse(res["Near_52W_High"])
        self.assertIn("At 52w Low", res["Bullish Signals"])
        self.assertIn("52-Week Range", res["Pre-Trade Log"])
        self.assertIn("[X] 52w High/Low Setup", res["Pre-Trade Log"])

    def test_standalone_52w_high_rejection_bearish_shooting_star(self):
        # Steady rally into $200 52w high, ending with upper-wick rejection red candle closing at $195.5 (-2.2% vs $200)
        closes = np.linspace(150.0, 197.0, 69).tolist() + [195.5]
        opens = [c - 0.5 for c in closes[:-1]] + [197.5]
        highs = [c + 0.5 for c in closes[:-1]] + [200.0]
        lows = [c - 0.8 for c in closes[:-1]] + [195.0]
        df = _build_daily_df(closes, opens=opens, highs=highs, lows=lows)

        with patch.object(reversal_scanner, "find_best_option", side_effect=self._mock_option):
            res = reversal_scanner._analyze_swing_trade_setup(
                "MSFT",
                df,
                quote={"marketCap": 2e12, "fiftyTwoWeekHigh": 200.0, "fiftyTwoWeekLow": 140.0},
                market_sentiment={"is_bullish": False},
            )

        self.assertIsNotNone(res)
        self.assertEqual(res["Setup Type"], "52w High Rejection")
        self.assertEqual(res["Direction"], "Bearish")
        self.assertEqual(res["52W Status"], "At 52w High")
        self.assertTrue(res["Near_52W"])
        self.assertTrue(res["Near_52W_High"])
        self.assertIn("At 52w High", res["Bearish Signals"])

    def test_standalone_52w_high_breakout_bullish(self):
        # Uptrend above 50 SMA closing strong green right at 52w high (RVOL = 1.0 so Key Level Breakout doesn't preempt)
        closes = np.linspace(140.0, 198.0, 69).tolist() + [200.0]
        opens = [c - 0.5 for c in closes[:-1]] + [197.8]
        highs = [c + 0.4 for c in closes[:-1]] + [200.1]
        lows = [c - 0.6 for c in closes[:-1]] + [197.5]
        df = _build_daily_df(closes, opens=opens, highs=highs, lows=lows)

        with patch.object(reversal_scanner, "find_best_option", side_effect=self._mock_option):
            res = reversal_scanner._analyze_swing_trade_setup(
                "NVDA",
                df,
                quote={"marketCap": 2e12, "fiftyTwoWeekHigh": 200.0, "fiftyTwoWeekLow": 130.0},
                market_sentiment={"is_bullish": True},
            )

        self.assertIsNotNone(res)
        self.assertEqual(res["Setup Type"], "52w High Breakout")
        self.assertEqual(res["Direction"], "Bullish")
        self.assertEqual(res["52W Status"], "Hits 52w High")
        self.assertTrue(res["Near_52W_High"])

    def test_existing_pullback_gains_52w_confluence_and_midrange_still_works(self):
        # Sawtooth uptrend followed by shallow pullback to 8 EMA so RSI stays inside 44..62
        base = []
        p = 120.0
        for i in range(60):
            p += 1.4 if i % 2 == 0 else -0.6
            base.append(round(p, 2))
        closes = base + [p + 0.5, p - 0.8, p - 1.2, p - 1.4, p - 0.9]
        opens = [c + 0.3 for c in closes[:-1]] + [closes[-1] - 0.3]
        highs = [max(o, c) + 0.6 for o, c in zip(opens, closes)]
        lows = [min(o, c) - 0.8 for o, c in zip(opens, closes)]
        # Ensure final bar touches rising 8 EMA cleanly
        df_tmp = _build_daily_df(closes, opens=opens, highs=highs, lows=lows)
        ema8_val = float(df_tmp["Close"].ewm(span=8, adjust=False).mean().iloc[-1])
        closes[-1] = round(ema8_val, 2)
        lows[-1] = round(ema8_val * 0.995, 2)
        df = _build_daily_df(closes, opens=opens, highs=highs, lows=lows)
        high_ref = round(closes[-1] * 1.02, 2)

        with patch.object(reversal_scanner, "find_best_option", side_effect=self._mock_option):
            near_res = reversal_scanner._analyze_swing_trade_setup(
                "AMD",
                df,
                quote={"marketCap": 2e11, "fiftyTwoWeekHigh": high_ref, "fiftyTwoWeekLow": 95.0},
                market_sentiment={"is_bullish": True},
            )
            mid_res = reversal_scanner._analyze_swing_trade_setup(
                "AMD",
                df,
                quote={"marketCap": 2e11, "fiftyTwoWeekHigh": 250.0, "fiftyTwoWeekLow": 95.0},
                market_sentiment={"is_bullish": True},
            )

        self.assertIsNotNone(near_res)
        self.assertIsNotNone(mid_res)
        self.assertEqual(near_res["Setup Type"], "8 EMA Trend Pullback")
        self.assertEqual(mid_res["Setup Type"], "8 EMA Trend Pullback")
        self.assertEqual(near_res["52W Status"], "At 52w High")
        self.assertEqual(mid_res["52W Status"], "Mid-Range")
        self.assertGreater(near_res["Score"], mid_res["Score"])
        self.assertTrue(near_res["Near_52W"])
        self.assertFalse(mid_res["Near_52W"])

    def test_watchlist_scan_downloads_full_52w_history(self):
        with patch.object(reversal_scanner, "fetch_batch_concurrent", return_value={}) as mock_fetch, \
             patch.object(data_fetcher, "fetch_quotes_batch", return_value={}), \
             patch.object(reversal_scanner, "get_market_sentiment", return_value={"is_bullish": True}):
            df_out = reversal_scanner.watchlist_scan(["AAPL"], mode="watchlist")
            self.assertTrue(df_out.empty)
            self.assertEqual(mock_fetch.call_args.kwargs.get("days"), 260)

    def test_fetch_quotes_batch_extracts_52w_fields(self):
        mock_wb = MagicMock()
        mock_wb.get_quote.return_value = {
            "close": "352.10",
            "preClose": "350.00",
            "marketValue": "4000000000000",
            "volume": "45000000",
            "avgVol3M": "50000000",
            "fiftyTwoWkHigh": "355.91",
            "fiftyTwoWkLow": "242.76",
            "name": "Apple Inc",
        }
        with patch.object(data_fetcher, "get_unofficial_client", return_value=mock_wb):
            quotes = data_fetcher.fetch_quotes_batch(["AAPL"], max_workers=1)

        self.assertIn("AAPL", quotes)
        self.assertEqual(quotes["AAPL"]["fiftyTwoWeekHigh"], 355.91)
        self.assertEqual(quotes["AAPL"]["fiftyTwoWeekLow"], 242.76)


if __name__ == "__main__":
    unittest.main()
