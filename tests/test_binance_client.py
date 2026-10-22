import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal

from _load import FakeBinance, FakeClock, Seq, bfix, common, NetworkBlocked

bc = common("binance_client")
sf = common("symbol_filters")
ts = common("trading_session")
tc = common("trader_config")

# The HMAC example of the Binance spot docs (rest-api.md, "SIGNED Endpoint Examples for POST /api/v3/order").
DOC_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
DOC_PAYLOAD = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000"
               "&timestamp=1499827319559")
DOC_SIG = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
DOC_SIG_UNICODE = "e1353ec6b14d888f1164ae9af8228a3dbd508bc82eb867db8ab6046442f33ef3"
ENV = {"BINANCE_API_KEY": DOC_KEY, "BINANCE_API_SECRET": DOC_SECRET}


def signed_client(routes, clock=None):
    table = {("GET", "/api/v3/time"): {"serverTime": 1499827319559}}
    table.update(routes)
    fake = FakeBinance(table)
    clock = clock or FakeClock(1499827319.559)
    c = bc.BinanceClient(bc.TESTNET_URL, bc.keys_from_env(ENV), opener=fake, sleep=clock.sleep, clock=clock)
    return c, fake, clock


class Signature(unittest.TestCase):
    def test_the_docs_example(self):
        self.assertEqual(bc.sign(DOC_SECRET, DOC_PAYLOAD), DOC_SIG)

    def test_the_docs_unicode_example_after_percent_encoding(self):
        q = bc.encode_params({"symbol": "１２３４５６", "side": "BUY", "type": "LIMIT",
                              "timeInForce": "GTC", "quantity": 1, "price": "0.1", "recvWindow": 5000,
                              "timestamp": 1499827319559})
        self.assertTrue(q.startswith("symbol=%EF%BC%91%EF%BC%92"))
        self.assertEqual(bc.sign(DOC_SECRET, q), DOC_SIG_UNICODE)

    def test_a_signed_request_carries_timestamp_signature_and_key_header(self):
        c, fake, _ = signed_client({("POST", "/api/v3/order/test"): {}})
        c.new_order({"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "1",
                     "price": "0.1"}, test=True)
        call = fake.calls[-1]
        self.assertEqual(call["path"], "/api/v3/order/test")
        self.assertEqual(call["headers"]["X-MBX-APIKEY"], DOC_KEY)
        self.assertEqual(call["params"]["recvWindow"], "5000")
        self.assertEqual(call["params"]["timestamp"], "1499827319559")
        # the same parameters in the same order as the docs give the docs' signature
        self.assertEqual(call["params"]["signature"], DOC_SIG)
        self.assertEqual(call["base"], bc.TESTNET_URL)

    def test_a_public_request_is_not_signed(self):
        fake = FakeBinance({("GET", "/api/v3/klines"): []})
        c = bc.BinanceClient(bc.TESTNET_URL, bc.keys_from_env(ENV), opener=fake)
        c.klines("BTCUSDT", "5m", limit=5)
        self.assertNotIn("signature", fake.calls[0]["params"])
        self.assertNotIn("X-MBX-APIKEY", fake.calls[0]["headers"])


class Safety(unittest.TestCase):
    def test_only_the_listed_endpoints(self):
        c, fake, _ = signed_client({})
        for method, path in (("POST", "/sapi/v1/capital/withdraw/apply"), ("POST", "/sapi/v1/margin/order"),
                             ("POST", "/fapi/v1/order"), ("GET", "/sapi/v1/capital/config/getall"),
                             ("POST", "/api/v3/userDataStream"), ("DELETE", "/api/v3/openOrders")):
            with self.assertRaises(bc.BinanceError):
                c.request(method, path, {}, signed=True)
            with self.assertRaises(bc.BinanceError):
                c.request(method, path, {})
        self.assertEqual(fake.calls, [])

    def test_no_withdraw_margin_or_futures_anywhere_in_the_code(self):
        from _load import ROOT
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "tests")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("/sapi/", "/fapi/", "/dapi/", "withdraw/apply", "/api/v3/margin"):
                    self.assertNotIn(bad, src, "%s has %s" % (f, bad))

    def test_keys_never_show(self):
        c, fake, _ = signed_client({("GET", "/api/v3/account"): (400, {}, json.dumps({"code": -2015,
                                                                                    "msg": "Invalid API-key"}))})
        for text in (repr(c), str(c), repr(c._keys), str(c._keys)):
            self.assertNotIn(DOC_KEY, text)
            self.assertNotIn(DOC_SECRET, text)
            self.assertNotIn(DOC_SECRET[:6], text)
        with self.assertRaises(bc.BinanceError) as cm:
            c.account()
        self.assertNotIn(DOC_SECRET, str(cm.exception))
        self.assertNotIn(DOC_KEY, str(cm.exception))
        self.assertNotIn("signature", str(cm.exception))
        self.assertIn("-2015", str(cm.exception))

    def test_no_keys_no_signed_call(self):
        fake = FakeBinance({})
        c = bc.BinanceClient(bc.TESTNET_URL, bc.keys_from_env({}), opener=fake)
        with self.assertRaises(bc.ConfigError) as cm:
            c.account()
        self.assertIn("BINANCE_API_KEY", str(cm.exception))
        self.assertEqual(fake.calls, [])

    def test_testnet_is_the_default_and_live_needs_both(self):
        self.assertEqual(bc.base_url_for({}), bc.TESTNET_URL)
        self.assertEqual(bc.base_url_for({"testnet": True}), bc.TESTNET_URL)
        self.assertEqual(bc.base_url_for({"testnet": False}, live=True), bc.LIVE_URL)
        with self.assertRaises(bc.ConfigError):
            bc.base_url_for({"testnet": False})            # config alone is not enough
        with self.assertRaises(bc.ConfigError):
            bc.base_url_for({"testnet": True}, live=True)  # the flag alone is not enough
        with self.assertRaises(bc.ConfigError):
            bc.BinanceClient("https://example.com")

    def test_market_data_can_read_live_while_orders_stay_on_the_testnet(self):
        cfg = {"testnet": True, "market_data": "live"}
        self.assertEqual(bc.market_base_url(cfg), bc.LIVE_URL)
        self.assertEqual(bc.client(cfg, signed=True, env={}).base_url, bc.TESTNET_URL)

    def test_the_tests_themselves_cannot_reach_the_network(self):
        with self.assertRaises(NetworkBlocked):
            bc._urlopen("GET", bc.TESTNET_URL + "/api/v3/ping", {}, 5)


class RateLimits(unittest.TestCase):
    def test_429_waits_retry_after_then_one_retry_for_a_public_get(self):
        clock = FakeClock()
        fake = FakeBinance({("GET", "/api/v3/klines"): Seq([(429, {"Retry-After": "7"}, {"code": -1003, "msg": "x"}),
                                                        (200, {"X-MBX-USED-WEIGHT-1M": "12"}, [])])})
        c = bc.BinanceClient(bc.TESTNET_URL, opener=fake, sleep=clock.sleep, clock=clock)
        self.assertEqual(c.klines("BTCUSDT", "5m"), [])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(clock.slept, [7])
        self.assertEqual(c.used_weight, 12)

    def test_429_twice_gives_up(self):
        clock = FakeClock()
        fake = FakeBinance({("GET", "/api/v3/klines"): Seq([(429, {"Retry-After": "3"}, "{}"),
                                                        (429, {"Retry-After": "3"}, "{}")])})
        c = bc.BinanceClient(bc.TESTNET_URL, opener=fake, sleep=clock.sleep, clock=clock)
        with self.assertRaises(bc.RateLimitError):
            c.klines("BTCUSDT", "5m")
        self.assertEqual(len(fake.calls), 2)

    def test_an_order_is_never_retried_on_429(self):
        c, fake, clock = signed_client({("POST", "/api/v3/order"): Seq([(429, {"Retry-After": "2"}, "{}"),
                                                                    (200, {}, {"orderId": 1})])})
        with self.assertRaises(bc.RateLimitError):
            c.new_order({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
        self.assertEqual(fake.paths().count("/api/v3/order"), 1)

    def test_418_is_a_ban_and_nothing_is_sent_until_it_is_over(self):
        clock = FakeClock()
        fake = FakeBinance({("GET", "/api/v3/klines"): (418, {"Retry-After": "300"}, "{}")})
        c = bc.BinanceClient(bc.TESTNET_URL, opener=fake, sleep=clock.sleep, clock=clock)
        with self.assertRaises(bc.BannedError):
            c.klines("BTCUSDT", "5m")
        with self.assertRaises(bc.BannedError):
            c.klines("BTCUSDT", "5m")
        self.assertEqual(len(fake.calls), 1)
        clock.t += 301
        fake.routes[("GET", "/api/v3/klines")] = []
        self.assertEqual(c.klines("BTCUSDT", "5m"), [])

    def test_heavy_weight_pauses_until_the_next_minute(self):
        clock = FakeClock(1792368030.0)    # 30 seconds into a minute
        fake = FakeBinance({("GET", "/api/v3/klines"): (200, {"X-MBX-USED-WEIGHT-1M": "4300"}, [])})
        c = bc.BinanceClient(bc.TESTNET_URL, opener=fake, sleep=clock.sleep, clock=clock)
        c.klines("BTCUSDT", "5m")
        self.assertEqual(clock.slept, [])
        c.klines("BTCUSDT", "5m")          # 4300 of 6000 is over 70 %: sleep first
        self.assertEqual(clock.slept, [31.0])

    def test_clock_drift_resyncs_once_for_a_read_never_for_an_order(self):
        c, fake, clock = signed_client({("GET", "/api/v3/account"): Seq([(400, {}, {"code": -1021, "msg": "ts"}),
                                                                     (200, {}, {"balances": []})]),
                                        ("POST", "/api/v3/order"): (400, {}, {"code": -1021, "msg": "ts"})})
        self.assertEqual(c.account(), {"balances": []})
        with self.assertRaises(bc.BinanceError):
            c.new_order({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
        self.assertEqual(fake.paths().count("/api/v3/order"), 1)


class Candles(unittest.TestCase):
    def setUp(self):
        self.rows = bfix("klines-BTCUSDT-5m-2026-10-19.json")

    def test_klines_become_the_yahoo_chart_shape(self):
        doc = bc.klines_to_chart(self.rows, "btcusdt", "5m")
        res = doc["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        self.assertEqual(len(res["timestamp"]), 288)
        self.assertEqual(res["timestamp"][0], 1792368000)            # 2026-10-19 00:00 UTC, the OPEN time
        self.assertEqual(res["meta"]["symbol"], "BTCUSDT")
        self.assertEqual(res["meta"]["dataGranularity"], "5m")
        r0 = self.rows[0]
        self.assertEqual((q["open"][0], q["high"][0], q["low"][0], q["close"][0], q["volume"][0]),
                         tuple(float(x) for x in r0[1:6]))
        self.assertIsInstance(q["close"][0], float)
        self.assertEqual(res["meta"]["regularMarketTime"], (self.rows[-1][6] + 1) // 1000)

    def test_regular_market_time_is_capped_at_now(self):
        now_ms = self.rows[10][0] + 120000    # 2 minutes into bar 10: bar 10 is still forming
        doc = bc.klines_to_chart(self.rows[:11], "BTCUSDT", "5m", now_ms=now_ms)
        self.assertEqual(doc["chart"]["result"][0]["meta"]["regularMarketTime"], now_ms // 1000)

    def test_kline_rows_are_numbers(self):
        k = bc.kline_rows(self.rows[:1])[0]
        self.assertEqual(k["open_time"], 1792368000000)
        self.assertEqual(k["close_time"], 1792368299999)
        self.assertIsInstance(k["trades"], int)

    def test_klines_range_pages_1000_at_a_time(self):
        rows = bfix("klines-BTCUSDT-5m-60d.json.gz")

        def answer(p):
            start, end, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
            return [r for r in rows if start <= r[0] <= end][:lim]
        fake = FakeBinance({("GET", "/api/v3/klines"): answer})
        c = bc.BinanceClient(bc.TESTNET_URL, opener=fake)
        got = c.klines_range("BTCUSDT", "5m", rows[0][0], rows[-1][0])
        self.assertEqual(len(got), len(rows))
        self.assertEqual(len(fake.calls), (len(rows) + 999) // 1000)


class Filters(unittest.TestCase):
    def setUp(self):
        info = bfix("exchangeInfo.json")
        self.btc = sf.rules_of(sf.find_symbol(info, "BTCUSDT"))
        self.pepe = sf.rules_of(sf.find_symbol(info, "PEPEUSDT"))
        self.doge = sf.rules_of(sf.find_symbol(info, "DOGEUSDT"))

    def test_step_size_rounds_down(self):
        self.assertEqual(sf.floor_step("0.001239999", self.btc["step"]), Decimal("0.00123"))
        self.assertEqual(sf.floor_step("0.00123", self.btc["step"]), Decimal("0.00123"))
        self.assertEqual(sf.floor_step("1234567.9", self.pepe["step"]), Decimal("1234567"))
        self.assertEqual(sf.fmt(sf.floor_step(0.1 + 0.2, "0.00001000")), "0.30000")

    def test_tick_size_rounding(self):
        self.assertEqual(sf.floor_tick("52555.678", self.btc["tick"]), Decimal("52555.67"))
        self.assertEqual(sf.ceil_tick("52555.671", self.btc["tick"]), Decimal("52555.68"))
        self.assertEqual(sf.round_tick("52555.675", self.btc["tick"]), Decimal("52555.68"))
        self.assertEqual(sf.round_tick("0.000008734", self.pepe["tick"]), Decimal("0.00000873"))
        self.assertEqual(sf.fmt(sf.floor_tick("0.0000087399", self.pepe["tick"])), "0.00000873")

    def test_decimals(self):
        self.assertEqual(sf.decimals("0.01000000"), 2)
        self.assertEqual(sf.decimals("1.00000000"), 0)
        self.assertEqual(sf.decimals("0.00000001"), 8)

    def test_filter_checks(self):
        self.assertEqual(sf.check_order(self.btc, "0.00100", "52000", prices=[("stop", "51000.00")]), [])
        bad = sf.check_order(self.btc, "0.000015", "52000", prices=[("stop", "51000.005")])
        self.assertTrue(any("step" in x for x in bad), bad)
        self.assertTrue(any("tick size" in x for x in bad), bad)
        small = sf.check_order(self.btc, "0.00009", "52000")      # 4.68 USDT
        self.assertTrue(any("minimum notional 5" in x for x in small), small)

    def test_old_min_notional_filter_is_read_too(self):
        self.assertEqual(self.doge["min_notional"], "1.00000000")
        self.assertNotIn("STOP_LOSS", self.doge["order_types"])
        self.assertTrue(sf.check_notional(self.doge, "5", "0.11"))


class Session(unittest.TestCase):
    def test_default_session_is_the_utc_day(self):
        w = ts.current(datetime.datetime(2026, 10, 19, 0, 10))
        self.assertEqual(w["date"], "2026-10-19")
        self.assertEqual(w["phase"], "range")
        self.assertEqual(w["range_end"], datetime.datetime(2026, 10, 19, 0, 15))
        self.assertEqual(w["entry_end"], datetime.datetime(2026, 10, 19, 6, 0))
        self.assertEqual(ts.current(datetime.datetime(2026, 10, 19, 6, 1))["phase"], "late")

    def test_a_session_over_midnight_utc(self):
        cfg = {"session_start_utc": "22:00"}
        w = ts.current(datetime.datetime(2026, 10, 20, 1, 30), cfg)
        self.assertEqual(w["date"], "2026-10-19")
        self.assertEqual(w["start"], datetime.datetime(2026, 10, 19, 22, 0))
        self.assertEqual(w["end"], datetime.datetime(2026, 10, 20, 22, 0))
        self.assertEqual(w["phase"], "entries")
        self.assertEqual(ts.session_date_of(datetime.datetime(2026, 10, 19, 21, 59), cfg), "2026-10-18")
        self.assertEqual(ts.session_date_of(datetime.datetime(2026, 10, 19, 22, 0), cfg), "2026-10-19")

    def test_exit_by_crosses_midnight(self):
        self.assertEqual(ts.exit_by(datetime.datetime(2026, 10, 19, 22, 30), {"max_hold_minutes": 240}),
                         datetime.datetime(2026, 10, 20, 2, 30))

    def test_now_without_offset_is_utc(self):
        self.assertEqual(ts.parse_now("2026-10-19 10:15"), datetime.datetime(2026, 10, 19, 10, 15))
        self.assertEqual(ts.parse_now("2026-10-19T12:15+02:00"), datetime.datetime(2026, 10, 19, 10, 15))
        self.assertEqual(ts.parse_now("2026-10-19T10:15Z"), datetime.datetime(2026, 10, 19, 10, 15))

    def test_bad_settings_are_refused(self):
        for cfg in ({"session_start_utc": "25:00"}, {"opening_range_minutes": 0},
                    {"opening_range_minutes": 400, "entry_window_minutes": 360}):
            with self.assertRaises(ts.SessionError):
                ts.settings(cfg)


class Config(unittest.TestCase):
    def test_example_config_loads_with_testnet_on(self):
        from _load import ROOT
        cfg = tc.load(os.path.join(ROOT, "config.example.json"))
        self.assertIs(cfg["testnet"], True)
        self.assertEqual(cfg["quote_asset"], "USDT")
        self.assertEqual(cfg["fee_rate"], 0.001)
        for k in ("max_loss_per_trade", "max_daily_loss", "max_trades_per_day", "min_reward_risk",
                  "session_start_utc", "opening_range_minutes", "max_hold_minutes", "symbols"):
            self.assertIn(k, cfg)

    def test_a_config_with_keys_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as fh:
                json.dump({"testnet": True, "api_secret": "abc"}, fh)
            with self.assertRaises(tc.ConfigFileError) as cm:
                tc.load(p)
            self.assertNotIn("abc", str(cm.exception))

    def test_testnet_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as fh:
                json.dump({"testnet": "false"}, fh)
            with self.assertRaises(tc.ConfigFileError):
                tc.load(p)

    def test_token_filters(self):
        self.assertTrue(tc.is_leveraged_token("BTCUP"))
        self.assertTrue(tc.is_leveraged_token("ETHBEAR"))
        self.assertFalse(tc.is_leveraged_token("JUP"))
        self.assertFalse(tc.is_leveraged_token("BTC"))
        self.assertTrue(tc.is_stable_pair("USDC", "USDT"))
        self.assertFalse(tc.is_stable_pair("BTC", "USDT"))


if __name__ == "__main__":
    unittest.main()
