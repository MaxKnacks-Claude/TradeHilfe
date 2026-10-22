import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import BFIX, FakeBinance, bfix, load

entry = load("trader-entry", "entry")
bc = entry.bc

KL = os.path.join(BFIX, "klines-BTCUSDT-5m-2026-10-19.json")
ROWS = bfix("klines-BTCUSDT-5m-2026-10-19.json")


def at(hhmm, day=19):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 10, day, h, m)


def chart(now, rows=None):
    return entry.get_chart("BTCUSDT", "5m", now, {}, offline=KL) if rows is None else \
        entry.parse_chart(bc.chart_text(rows, "BTCUSDT", "5m", entry.tsess.ms_of(now)), "BTCUSDT")


def ev(hhmm, **kw):
    now = at(hhmm)
    return entry.evaluate(chart(now), now, **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class OpeningRangeBreakout(unittest.TestCase):
    def test_range_forming(self):
        r = ev("00:10")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertEqual(r["forming"]["bar_start"], "00:10")

    def test_waiting_below_the_high(self):
        r = ev("01:30")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["opening_range"]["high"], 52680.36)
        self.assertEqual(r["opening_range"]["low"], 52410.22)
        self.assertEqual(r["last"]["bar_start"], "01:25")

    def test_a_forming_candle_above_the_high_is_not_a_trigger(self):
        r = ev("01:58")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["price"], 52730.0)

    def test_the_trigger(self):
        r = ev("02:00")
        self.assertEqual(r["status"], "triggered")
        t = r["trigger"]
        self.assertEqual((t["time"], t["close_time"], t["bar_start"], t["bar_index"], t["price"]),
                         ("02:00 UTC", "2026-10-19T02:00:00Z", "01:55", 23, 52730.0))
        self.assertEqual(t["pct_over"], 0.09)
        self.assertEqual(r["minutes_since"], 0)

    def test_volume_filter_can_hold_a_trigger_back(self):
        r = ev("02:00", min_volume_x=50)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("volume", r["reason"])

    def test_level_setup(self):
        r = ev("02:00", setup="level", level=52700)
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["level"], 52700.0)
        r = ev("03:00", setup="level", level=10 ** 6)
        self.assertEqual(r["status"], "waiting")

    def test_expired_after_the_entry_window(self):
        r = ev("06:05", setup="level", level=10 ** 6)
        self.assertEqual(r["status"], "expired")
        self.assertIn("06:00 UTC", r["reason"])
        r = entry.evaluate(chart(at("05:00")), at("05:00"), setup="level", level=10 ** 6,
                           cfg={"entry_window_minutes": 240})
        self.assertEqual(r["status"], "expired")

    def test_no_candles(self):
        r = entry.evaluate({"symbol": "BTCUSDT", "bars": [], "data_time": None}, at("02:00"))
        self.assertEqual(r["status"], "no-data")

    def test_session_over_midnight(self):
        # session_start_utc 22:00: the session named 2026-10-18 has its range from 22:00 to 22:15 the day before.
        # We shift the 2026-10-19 candles back by 2 hours, so 00:00 becomes 22:00 of the 18th.
        shifted = [[r[0] - 7200000] + r[1:6] + [r[6] - 7200000] + r[7:] for r in ROWS]
        now = at("00:00")                            # 2026-10-19 00:00 = 2 hours into that session
        res = entry.evaluate(chart(now, shifted), now, cfg={"session_start_utc": "22:00"})
        self.assertEqual(res["date"], "2026-10-18")
        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["trigger"]["time"], "00:00 UTC")
        self.assertEqual(res["trigger"]["close_time"], "2026-10-19T00:00:00Z")
        self.assertEqual(res["trigger"]["bar_index"], 23)

    def test_entry_doc(self):
        r = ev("02:00")
        d = entry.entry_doc(r, at("02:00"))
        self.assertEqual(d["status"], "triggered")
        self.assertEqual(d["price"], 52730.0)
        self.assertEqual(d["opening_range"], {"high": 52680.36, "low": 52410.22, "minutes": 15})
        self.assertEqual(d["source"], "binance")
        self.assertEqual(d["session_start"], "2026-10-19T00:00:00Z")


class Commands(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cfg = os.path.join(self.d, "none.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_check_writes_entry_json(self):
        out = os.path.join(self.d, "entry.json")
        code, text, _ = run(["check", "BTCUSDT", "--offline", KL, "--now", "2026-10-19 02:00", "--write", out,
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 02:00 UTC at 52,730.00", text)
        self.assertIn("Nothing was placed", text)
        with open(out) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 23)

    def test_watch_replays_to_the_trigger(self):
        out = os.path.join(self.d, "entry.json")
        code, text, _ = run(["watch", "BTCUSDT", "--offline", KL, "--now", "2026-10-19 01:40", "--out", out,
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertTrue(any(l.startswith("01:55 candle  closed 52,730.00  TRIGGERED") for l in lines), text)
        self.assertTrue(os.path.exists(out))

    def test_short_is_refused(self):
        code, _, err = run(["check", "BTCUSDT", "--offline", KL, "--side", "sell", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("long only", err)

    def test_the_network_path_asks_from_the_session_start(self):
        def answer(p):
            return [r for r in ROWS if int(p["startTime"]) <= r[0] <= int(p["endTime"])][:int(p["limit"])]
        fake = FakeBinance({("GET", "/api/v3/klines"): answer})
        cl = bc.BinanceClient(bc.TESTNET_URL, opener=fake)
        ch = entry.get_chart("BTCUSDT", "5m", at("02:00"), {}, cl=cl)
        r = entry.evaluate(ch, at("02:00"))
        self.assertEqual(r["status"], "triggered")
        p = fake.calls[0]["params"]
        self.assertEqual((p["startTime"], p["endTime"]), ("1792368000000", "1792375200000"))


if __name__ == "__main__":
    unittest.main()
