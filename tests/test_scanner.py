import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, FakeBinance, bfix, load

scan = load("trader-scanner", "scan")
bc = scan.bc

SCAN5 = os.path.join(BFIX, "scan-5m-2026-10-19.json.gz")
SCAN1D = os.path.join(BFIX, "scan-1d-2026-10-19.json.gz")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


class Universe(unittest.TestCase):
    def test_usdt_spot_pairs_only(self):
        info, tick = bfix("exchangeInfo.json"), bfix("ticker24hr-mini.json")
        cfg = dict(scan.trader_config.DEFAULTS)
        kept, counts = scan.build_universe(info, tick, cfg, 0)
        syms = [k[0] for k in kept]
        self.assertEqual(sorted(syms), ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "PEPEUSDT", "SOLUSDT", "XRPUSDT"])
        self.assertEqual(counts["leveraged token"], 1)     # BTCUPUSDT
        self.assertEqual(counts["stablecoin pair"], 1)     # USDCUSDT
        self.assertEqual(counts["other quote asset"], 1)   # ETHBTC
        self.assertEqual(counts["not trading"], 1)         # LUNAUSDT in BREAK
        vols = [k[3] for k in kept]
        self.assertEqual(vols, sorted(vols, reverse=True))  # most traded first

    def test_volume_floor_and_blocked_list(self):
        info, tick = bfix("exchangeInfo.json"), bfix("ticker24hr-mini.json")
        cfg = dict(scan.trader_config.DEFAULTS, blocked=["PEPEUSDT"])
        kept, counts = scan.build_universe(info, tick, cfg, 1000000)
        syms = [k[0] for k in kept]
        self.assertNotIn("DOGEUSDT", syms)                 # 900,000 USDT in 24 hours
        self.assertNotIn("PEPEUSDT", syms)
        self.assertEqual(counts["blocked"], 1)

    def test_leveraged_and_stable_filters_can_be_switched_off(self):
        info, tick = bfix("exchangeInfo.json"), bfix("ticker24hr-mini.json")
        cfg = dict(scan.trader_config.DEFAULTS, exclude_leveraged_tokens=False, exclude_stable_pairs=False)
        syms = [k[0] for k in scan.build_universe(info, tick, cfg, 0)[0]]
        self.assertIn("BTCUPUSDT", syms)
        self.assertIn("USDCUSDT", syms)

    def test_command_writes_the_universe(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "universe.txt")
            code, text, _ = run(["universe", "--offline", BFIX, "--out", out, "--min-quote-volume", "0",
                                 "--config", os.path.join(d, "none.json")])
            self.assertEqual(code, 0)
            with open(out) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 6)
            self.assertRegex(lines[0], r"^[A-Z]+USDT\|[A-Z]+/USDT\|\d+$")
            self.assertIn("Kept 6 USDT spot pairs", text)
        finally:
            shutil.rmtree(d)


class Daily(unittest.TestCase):
    def setUp(self):
        self.data = scan.load_offline(SCAN1D)
        self.now = datetime.datetime(2026, 10, 19, 1, 0)

    def test_the_open_day_is_left_out(self):
        bars = scan.daily_bars(self.data["BTCUSDT"], self.now)
        self.assertEqual(bars[-1][0], datetime.date(2026, 10, 18))
        later = scan.daily_bars(self.data["BTCUSDT"], datetime.datetime(2026, 10, 20, 0, 0, 1))
        self.assertEqual(later[-1][0], datetime.date(2026, 10, 19))

    def test_setups(self):
        hits, counts, last = scan.scan_daily(self.data, sorted(self.data), self.now, 0)
        by = {h["symbol"]: h for h in hits}
        self.assertIn("breakout_20d", by["ETHUSDT"]["setups"])
        self.assertIn("oversold", by["SOLUSDT"]["setups"])
        self.assertLess(by["SOLUSDT"]["rsi"], 30)
        self.assertEqual(last, datetime.date(2026, 10, 18))
        self.assertEqual(counts["scanned"], 6)

    def test_rules_on_closes(self):
        up = [100.0 + i for i in range(40)]
        self.assertIn("breakout_20d", scan.find_setups(up)[0])
        down = [100.0 * 0.97 ** i for i in range(40)]
        tags, nums = scan.find_setups(down)
        self.assertIn("oversold", tags)
        self.assertLess(nums["rsi"], 30)
        flag = [100.0] * 20 + [100 + 2 * i for i in range(1, 11)] + [121, 120.5, 121.2, 120.8, 121.0]
        self.assertIn("bull_flag", scan.find_setups(flag)[0])

    def test_volume_floor(self):
        hits, counts, _ = scan.scan_daily(self.data, sorted(self.data), self.now, 10 ** 15)
        self.assertEqual(hits, [])
        self.assertEqual(counts["illiquid"], 6)


class OpeningRange(unittest.TestCase):
    def setUp(self):
        self.data = scan.load_offline(SCAN5)
        self.start = datetime.datetime(2026, 10, 19, 0, 0)

    def test_btc_breaks_out_at_two(self):
        bars = scan.session_bars(self.data["BTCUSDT"], self.start, datetime.datetime(2026, 10, 19, 3, 0))
        self.assertEqual(len(bars), 36)
        h = scan.orb_hit(bars, 3)
        self.assertEqual(h["at"], "02:00 UTC")
        self.assertEqual(h["bar_index"], 23)
        self.assertEqual(h["bar_start"], "01:55")
        self.assertEqual((h["or_low"], h["or_high"], h["price"]), (52410.22, 52680.36, 52730.0))

    def test_a_wick_is_not_a_breakout_and_a_forming_bar_does_not_count(self):
        cut = datetime.datetime(2026, 10, 19, 1, 58)          # the 01:55 candle is still forming
        bars = scan.session_bars(self.data["BTCUSDT"], self.start, cut)
        self.assertEqual(bars[-1]["start"], datetime.datetime(2026, 10, 19, 1, 50))
        self.assertIsNone(scan.orb_hit(bars, 3))
        self.assertTrue(any(b["h"] > 52680.36 for b in bars[3:]))   # wicks went over, no close did

    def test_scan_and_order(self):
        hits, counts = scan.scan_orb(self.data, sorted(self.data), self.start,
                                     datetime.datetime(2026, 10, 19, 3, 0), 15)
        self.assertEqual([h["symbol"] for h in hits], ["PEPEUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT"])
        self.assertEqual(counts["scanned"], 6)

    def test_a_missing_candle_makes_a_chart_unusable(self):
        s = dict(self.data["BTCUSDT"])
        for k in ("t", "o", "h", "l", "c", "v", "qv", "close_ms"):
            s[k] = s[k][:10] + s[k][11:]
        hits, counts = scan.scan_orb({"BTCUSDT": s}, ["BTCUSDT"], self.start,
                                     datetime.datetime(2026, 10, 19, 3, 0), 15)
        self.assertEqual(counts["unusable"], 1)

    def test_a_session_that_starts_at_22_utc(self):
        # the same candles, read as a session starting 2026-10-18 22:00: the range is 22:00 to 22:15,
        # and the 2026-10-19 bars are the 3rd hour onwards; the candles before 00:00 are missing
        w, cutoff = scan.orb_window(datetime.datetime(2026, 10, 19, 1, 0), None, {"session_start_utc": "22:00"})
        self.assertEqual(w["start"], datetime.datetime(2026, 10, 18, 22, 0))
        self.assertEqual(w["date"], "2026-10-18")
        hits, counts = scan.scan_orb(self.data, ["BTCUSDT"], w["start"], cutoff, 15)
        self.assertEqual(counts["unusable"], 1)

    def test_window_messages(self):
        with self.assertRaises(scan.ScanError) as cm:
            scan.orb_window(datetime.datetime(2026, 10, 19, 0, 12), None, {})
        self.assertIn("first candle after it closes at 00:20", str(cm.exception))
        with self.assertRaises(scan.ScanError) as cm:
            scan.orb_window(datetime.datetime(2026, 10, 19, 6, 30), None, {})
        self.assertIn("expired", str(cm.exception))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(datetime.datetime(2026, 10, 19, 1, 0), datetime.time(2, 0), {})   # in the future
        w, cut = scan.orb_window(datetime.datetime(2026, 10, 19, 5, 0), datetime.time(2, 0), {})
        self.assertEqual(cut, datetime.datetime(2026, 10, 19, 2, 0))

    def test_command_offline(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "watchlist.json")
            code, text, _ = run(["orb", "--offline", SCAN5, "--now", "2026-10-19 03:00", "--out", out,
                                 "--config", os.path.join(d, "none.json")])
            self.assertEqual(code, 0)
            self.assertIn("4 of 6 charts closed above their opening-range high by 03:00 UTC", text)
            self.assertIn("02:00 UTC  BTCUSDT", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["mode"], "orb")
            self.assertEqual(doc["session"], "2026-10-19")
            self.assertEqual(doc["hits"][1]["symbol"], "BTCUSDT")
        finally:
            shutil.rmtree(d)


class Network(unittest.TestCase):
    """The live path, against a fake Binance: one klines request per symbol, from the session start."""

    def test_orb_asks_for_the_session_candles(self):
        rows = bfix("klines-BTCUSDT-5m-2026-10-19.json")

        def answer(p):
            return [r for r in rows if int(p["startTime"]) <= r[0] <= int(p["endTime"])][:int(p["limit"])]
        fake = FakeBinance({("GET", "/api/v3/klines"): answer})
        saved = bc.client
        bc_client = lambda cfg, live=False, sleep=None, **k: bc.BinanceClient(bc.TESTNET_URL, opener=fake)
        scan.bc.client = bc_client
        d = tempfile.mkdtemp()
        try:
            code, text, err = run(["orb", "--symbols", "BTCUSDT", "--now", "2026-10-19 03:00", "--no-write",
                                   "--config", os.path.join(d, "none.json")])
        finally:
            scan.bc.client = saved
            shutil.rmtree(d)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.calls), 1)
        p = fake.calls[0]["params"]
        self.assertEqual((p["symbol"], p["interval"], p["startTime"]), ("BTCUSDT", "5m", "1792368000000"))
        self.assertEqual(fake.calls[0]["base"], bc.TESTNET_URL)
        self.assertIn("BTCUSDT", text)


if __name__ == "__main__":
    unittest.main()
