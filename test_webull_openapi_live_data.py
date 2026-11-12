"""
Unit tests for Webull OpenAPI live-data integration, 403 entitlement handling,
unofficial session verification, and PaperTrader real-time entry guards.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import data_fetcher
import paper_trader


class DummyServerException(Exception):
    """Mimics webull.core.exception.exceptions.ServerException."""

    def __init__(self, http_status, error_code, error_msg):
        self.http_status = http_status
        self.error_code = error_code
        self.error_msg = error_msg
        super().__init__(f"HTTP Status: {http_status}, Code: {error_code}, Msg: {error_msg}")


class DummyResp:
    """Simple HTTP response stub."""

    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class TestWebullOpenAPILiveData(unittest.TestCase):
    """Verify OpenAPI primary paths, fallback behavior, and PaperTrader guards."""

    def setUp(self):
        data_fetcher._clear_openapi_scope_block()
        data_fetcher.reset_webull_circuit_breaker()
        self._orig_auth = data_fetcher._unofficial_authenticated

    def tearDown(self):
        data_fetcher._clear_openapi_scope_block()
        data_fetcher._unofficial_authenticated = self._orig_auth

    def test_ny_date_roundtrip_timezone_invariant(self):
        """Noon NY timestamp maps back to exact YYYY-MM-DD in America/New_York."""
        for date_str in ("2026-01-15", "2026-07-17", "2026-11-20"):
            ts = data_fetcher._ny_date_to_ts(date_str)
            self.assertEqual(data_fetcher._ts_to_ny_date_str(ts), date_str)

    def test_openapi_20_symbol_batching_and_contract_parsing(self):
        """OpenAPI snapshot batching splits >20 symbols into chunks of 20 and marks live."""
        mock_client = MagicMock()

        contracts = []
        snap_by_sym = {}
        for i in range(45):
            sym = f"AMD261120C{150 + i:05d}000"
            contracts.append({
                "instrument_id": str(1000 + i),
                "symbol": sym,
                "status": "LISTING",
                "expiration_date": "2026-11-20",
                "option_type": "CALL" if i % 2 == 0 else "PUT",
                "strike_price": f"{150 + i}.0000",
                "multiplier": "100.0000",
                "def_type": "STANDARD",
            })
            snap_by_sym[sym] = {
                "instrument_id": str(1000 + i),
                "symbol": sym,
                "bid": "2.40",
                "ask": "2.60",
                "price": "2.50",
                "volume": "320",
                "open_interest": "1500",
                "implied_volatility": "0.55",
                "delta": "0.52",
                "quote_time": 1794400000000,
            }

        mock_client.instrument.list_option_contracts.return_value = DummyResp(
            200, {"data": contracts, "pagination_key": None}
        )

        batch_sizes = []

        def fake_get_snapshot(symbols, category="US_OPTION"):
            batch_sizes.append(len(symbols))
            self.assertLessEqual(len(symbols), 20)
            self.assertEqual(category, "US_OPTION")
            return DummyResp(200, [snap_by_sym[s] for s in symbols])

        mock_client.option_market_data.get_option_snapshot.side_effect = fake_get_snapshot

        with patch.object(data_fetcher, "get_webull_client", return_value=mock_client):
            result = data_fetcher._fetch_openapi_options_for_expiration("AMD", "2026-11-20", spot_price=170.0)

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "Webull_OpenAPI")
        self.assertTrue(result["is_realtime"])
        # 45 contracts -> batches of [20, 20, 5]
        self.assertEqual(batch_sizes, [20, 20, 5])
        total = len(result["calls"]) + len(result["puts"])
        self.assertEqual(total, 45)
        sample = result["calls"][0]
        self.assertEqual(sample["bid"], 2.40)
        self.assertEqual(sample["ask"], 2.60)
        self.assertEqual(sample["volume"], 320)
        self.assertEqual(sample["openInterest"], 1500)
        self.assertEqual(sample["delta"], 0.52)
        self.assertTrue(sample["is_realtime"])
        self.assertEqual(sample["quote_source"], "Webull_OpenAPI")

    def test_openapi_403_not_subscribed_triggers_cooldown_and_fallback(self):
        """HTTP 403 MARKET_DATA_NOT_SUBSCRIBED caches block and falls back without realtime=True."""
        mock_client = MagicMock()
        mock_client.instrument.list_option_contracts.return_value = DummyResp(
            200,
            {
                "data": [{
                    "instrument_id": "1043714005",
                    "symbol": "AMD261120C00150000",
                    "expiration_date": "2026-11-20",
                    "option_type": "CALL",
                    "strike_price": "150.00",
                    "multiplier": "100.0",
                    "def_type": "STANDARD",
                }],
                "pagination_key": None,
            },
        )
        mock_client.option_market_data.get_option_snapshot.side_effect = DummyServerException(
            403, "MARKET_DATA_NOT_SUBSCRIBED", "Insufficient permission, please subscribe to US_OPTION."
        )

        # Unofficial fallback marked as unauthenticated delayed guest
        data_fetcher._unofficial_authenticated = False
        fake_un = MagicMock()
        fake_un.timeout = 5
        fake_un.build_req_headers.return_value = {}
        fake_un._urls.options_exp_dat_new.return_value = "http://localhost/unused"
        fake_un.get_quote.return_value = {"close": "150.0"}

        exp_ts = data_fetcher._ny_date_to_ts("2026-11-20")
        mock_un_resp = DummyResp(
            200,
            {
                "expireDateList": [{
                    "from": {"date": "2026-11-20"},
                    "data": [{
                        "symbol": "AMD261120C00150000",
                        "strikePrice": "150",
                        "direction": "call",
                        "tickerId": "1043714005",
                        "bidList": [{"price": "4.00"}],
                        "askList": [{"price": "4.10"}],
                        "volume": "100",
                        "openInterest": "900",
                        "delta": "0.51",
                    }],
                }]
            },
        )

        with patch.object(data_fetcher, "get_webull_client", return_value=mock_client), \
             patch.object(data_fetcher, "get_unofficial_client", return_value=fake_un), \
             patch.object(data_fetcher, "get_stock_ticker_id", return_value="913254235"), \
             patch("requests.post", return_value=mock_un_resp):
            chain = data_fetcher.fetch_options_for_expiration("AMD", exp_ts, spot_price=150.0)
            self.assertTrue(data_fetcher._is_openapi_scope_blocked("US_OPTION"))
            self.assertEqual(chain["source"], "Webull_Delayed_Guest")
            self.assertFalse(chain["is_realtime"])

            # Second call within cooldown should NOT hit OpenAPI snapshot again
            calls_before = mock_client.option_market_data.get_option_snapshot.call_count
            chain2 = data_fetcher.fetch_options_for_expiration("AMD", exp_ts, spot_price=150.0)
            self.assertEqual(mock_client.option_market_data.get_option_snapshot.call_count, calls_before)
            self.assertFalse(chain2["is_realtime"])

    def test_openapi_live_quote_uses_option_symbol_directly(self):
        """get_option_live_quote queries snapshot directly when OCC option_symbol is passed."""
        mock_client = MagicMock()
        mock_client.option_market_data.get_option_snapshot.return_value = DummyResp(
            200,
            [{
                "instrument_id": "1043714005",
                "symbol": "AMD261120C00150000",
                "bid": "5.10",
                "ask": "5.30",
                "price": "5.20",
                "volume": "42",
                "open_interest": "800",
                "delta": "0.55",
            }],
        )
        with patch.object(data_fetcher, "get_webull_client", return_value=mock_client):
            q = data_fetcher.get_option_live_quote(
                "AMD", 150.0, "CALL", "2026-11-20", option_symbol="AMD261120C00150000"
            )

        mock_client.instrument.list_option_contracts.assert_not_called()
        self.assertEqual(q["source"], "Webull_OpenAPI")
        self.assertTrue(q["is_realtime"])
        self.assertEqual(q["mid"], 5.20)
        self.assertEqual(q["bid"], 5.10)
        self.assertEqual(q["ask"], 5.30)

    def test_paper_trader_blocks_delayed_entry_by_default_and_allows_live(self):
        """PaperTrader blocks new option orders on delayed quotes unless ALLOW_DELAYED_OPTIONS_TRADING=true."""
        with patch.object(paper_trader, "_load_trade_log", return_value={"trades": []}), \
             patch.object(paper_trader, "_save_trade_log"):
            trader = paper_trader.PaperTrader()

        trader.allow_delayed_options = False
        trader.earliest_trade_mins = 0
        trader.latest_trade_mins = 0
        trader._logged_in = True
        trader._is_real_paper_account = False

        delayed_contract = {
            "symbol": "AMD261120C00150000",
            "tickerId": "1043714005",
            "strike": 150.0,
            "type": "CALL",
            "exp": "Nov 20",
            "exp_date": "2026-11-20",
            "dte": 9,
            "mid": 5.0,
            "bid": 4.9,
            "ask": 5.1,
            "spread": 0.2,
            "spread_pct": 4.0,
            "quote_source": "Webull_Delayed_Guest",
            "is_realtime": False,
        }

        with patch.object(trader, "_check_risk_limits", return_value=(True, "")), \
             patch.object(trader, "get_available_funds", return_value=3000.0), \
             patch("reversal_scanner.find_best_option", return_value=delayed_contract), \
             patch("indicator_bot.send_telegram_notification") as mock_tg:
            blocked_res = trader.place_option_order("AMD", "bullish", 150.0, 149.0)
            self.assertIsNone(blocked_res)
            self.assertEqual(mock_tg.call_count, 1)

            # Calling again on the same day should NOT spam Telegram
            blocked_res_2 = trader.place_option_order("AMD", "bullish", 150.0, 149.0)
            self.assertIsNone(blocked_res_2)
            self.assertEqual(mock_tg.call_count, 1)

            # Live Webull OpenAPI contract should succeed and store source metadata
            live_contract = dict(delayed_contract, quote_source="Webull_OpenAPI", is_realtime=True)
            with patch("reversal_scanner.find_best_option", return_value=live_contract), \
                 patch.object(paper_trader, "_save_trade_log"):
                live_res = trader.place_option_order("AMD", "bullish", 150.0, 149.0)

        self.assertIsNotNone(live_res)
        self.assertEqual(live_res["quote_source"], "Webull_OpenAPI")
        self.assertTrue(live_res["is_realtime"])

    def test_paper_trader_get_trade_option_quote_passes_option_symbol(self):
        """get_trade_option_quote forwards OCC option_symbol for O(1) OpenAPI snapshot lookup."""
        with patch.object(paper_trader, "_load_trade_log", return_value={"trades": []}):
            trader = paper_trader.PaperTrader()

        trade = {
            "ticker": "AMD",
            "option_symbol": "AMD261120C00150000",
            "strike": 150.0,
            "type": "CALL",
            "expiration_date": "2026-11-20",
        }
        expected = {"bid": 5.0, "ask": 5.2, "mid": 5.1, "source": "Webull_OpenAPI", "is_realtime": True}
        with patch("data_fetcher.get_option_live_quote", return_value=expected) as mock_q:
            res = trader.get_trade_option_quote(trade)

        mock_q.assert_called_once_with(
            ticker="AMD",
            strike=150.0,
            option_type="CALL",
            exp_date_str="2026-11-20",
            option_symbol="AMD261120C00150000",
        )
        self.assertEqual(res, expected)

    def test_test_connection_reports_openapi_options_and_resets_cooldown(self):
        """test_connection clears cached 403 block and reports live OpenAPI US_OPTION status."""
        data_fetcher._mark_openapi_scope_blocked("US_OPTION", "cached 403")
        self.assertTrue(data_fetcher._is_openapi_scope_blocked("US_OPTION"))

        mock_client = MagicMock()
        mock_client.market_data.get_history_bar.side_effect = DummyServerException(
            403, "MARKET_DATA_NOT_SUBSCRIBED", "subscribe to STOCK QUOTES"
        )
        mock_client.instrument.list_option_contracts.return_value = DummyResp(
            200,
            {"data": [{"symbol": "AAPL261120C00225000", "multiplier": "100.0", "def_type": "STANDARD"}]},
        )
        mock_client.option_market_data.get_option_snapshot.return_value = DummyResp(
            200, [{"symbol": "AAPL261120C00225000", "bid": "3.10", "ask": "3.20"}]
        )

        with patch.object(data_fetcher, "get_unofficial_client", return_value=None), \
             patch.object(data_fetcher, "get_webull_client", return_value=mock_client), \
             patch.object(data_fetcher, "_fetch_yahoo_fallback_one", return_value=None):
            diag = data_fetcher.test_connection("AAPL")

        self.assertFalse(diag["webull_openapi_stock_ok"])
        self.assertTrue(diag["webull_openapi_contracts_ok"])
        self.assertTrue(diag["webull_openapi_options_ok"])
        self.assertTrue(diag["realtime_options_ready"])
        self.assertFalse(data_fetcher._is_openapi_scope_blocked("US_OPTION"))


if __name__ == "__main__":
    unittest.main()
